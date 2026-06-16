"""Integration tests for the carpenter-gmail capability package.

Loads the package from a fixture path (a copy of the
``carpenter-packages`` repo's ``packages/carpenter-gmail/``
directory placed under ``tests/packages/fixtures/`` for hermetic
testing) and exercises:

* Manifest validation (D24-conformant: data_models, arc_templates,
  judge_handlers, kb_articles, allowlist_proposals, credential_requirements
  all parse cleanly).
* Data-model registration (3 extracts + briefing).
* JUDGE handler registration (3 handlers, one per template).
* Approve / reject paths through each JUDGE handler.
* Template loading (3 templates land in the platform template store).
* Chat-tool registration (the package's @chat_tool functions are
  picked up by the loader).
* The send-side allowlist precheck rejects unknown recipients.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest


# Path to the package source: we expect a sibling carpenter-packages
# checkout next to carpenter-core.  CI sets CARPENTER_PACKAGES_DIR;
# locally we autodiscover.
def _find_email_package() -> Path | None:
    """Locate the carpenter-gmail package source dir.

    Resolution order:
      1. ``CARPENTER_PACKAGES_DIR`` env var (preferred for CI).
      2. ``../carpenter-packages/packages/carpenter-gmail`` relative
         to the carpenter-core checkout — checked for both the
         resolved repo path and ``__file__``'s lexical parents so the
         symlinked-install layout (e.g. carpenter-core → /media/.../repo)
         still discovers a sibling carpenter-packages clone under
         ~/repos/.
    """
    import os

    env = os.environ.get("CARPENTER_PACKAGES_DIR")
    if env:
        candidate = Path(env) / "carpenter-gmail"
        if candidate.is_dir():
            return candidate

    here_resolved = Path(__file__).resolve().parents[2]
    here_lexical = Path(__file__).parents[2]
    home_repos = Path.home() / "repos"
    for repo_root in (here_resolved, here_lexical, home_repos / "carpenter-core"):
        candidate = repo_root.parent / "carpenter-packages" / "packages" / "carpenter-gmail"
        if candidate.is_dir():
            return candidate
    return None


@pytest.fixture
def email_pkg_src() -> Path:
    """Skip the test module if the package source can't be located."""
    src = _find_email_package()
    if src is None:
        pytest.skip(
            "carpenter-gmail package not found; set CARPENTER_PACKAGES_DIR "
            "or check out carpenter-packages alongside carpenter-core",
        )
    return src


@pytest.fixture
def email_pkg(tmp_path: Path, email_pkg_src: Path) -> Path:
    """Copy the package into a temp dir for hermetic loading.

    Loaders import package modules from disk via importlib; we copy
    so the test never mutates the source-of-truth checkout.
    """
    dst = tmp_path / "carpenter-gmail"
    shutil.copytree(email_pkg_src, dst)
    return dst


@pytest.fixture
def fresh_registry():
    """Reset the global handler registry before each test."""
    from carpenter.packages.handler_registry import get_handler_registry

    reg = get_handler_registry()
    reg.reset()
    yield reg
    reg.reset()


@pytest.fixture
def policies_with_test_emails():
    """Pre-populate the SecurityPolicies email allowlist for JUDGE tests."""
    from carpenter.security import get_policies

    pol = get_policies()
    # Save originals so we can restore after.
    saved = pol.get_allowlist("email")
    for addr in (
        "ben@example.com",
        "alice@example.com",
        "vendor@shop.example.com",
        "calendar@meeting.example.com",
    ):
        pol.add("email", addr)
    yield pol
    pol.clear("email")
    for addr in saved:
        pol.add("email", addr)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_manifest_loads(self, email_pkg: Path):
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        assert m.name == "carpenter-gmail"
        # Version history: 0.1.0 (Phase 1 read) -> 0.2.0 (Phase 1.5
        # archive/mark_read/draft) -> 0.3.0 (Phase 1.5 v2 write
        # graduation) -> 0.4.0 (Phase 3a inbound triage) ->
        # 0.5.0 (Phase 3b attachment metadata) ->
        # 0.6.0 (Phase 4 semantic resource index) ->
        # 0.6.1 (Phase 4 bug fix: PackageStateHandle import) ->
        # 0.7.0 (rename carpenter-email -> carpenter-gmail).
        assert m.version.startswith("0.7.")

    def test_manifest_declares_eleven_templates(self, email_pkg: Path):
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        names = {t.name for t in m.arc_templates}
        # Phase 4 added three indexer templates to the 3a/3b set of eight.
        assert names == {
            "email_read_simple_text",
            "email_read_meeting_invite",
            "email_read_order_confirmation",
            "email_write_send",
            "email_write_archive",
            "email_write_mark_read",
            "email_write_draft",
            "email_triage",
            "email_index_phase1",
            "email_index_phase2",
            "email_index_incremental",
        }

    def test_manifest_declares_data_models(self, email_pkg: Path):
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        assert "EmailReviewBriefing" in m.data_models
        assert "EmailSimpleTextExtract" in m.data_models
        assert "EmailMeetingInviteExtract" in m.data_models
        assert "EmailOrderConfirmationExtract" in m.data_models
        # Phase 1.5 v2 write-side result dataclasses
        assert "EmailSendResult" in m.data_models
        assert "EmailArchiveResult" in m.data_models
        assert "EmailMarkReadResult" in m.data_models
        assert "EmailDraftResult" in m.data_models

    def test_manifest_has_oauth_credentials(self, email_pkg: Path):
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        assert m.credential_requirements
        cred = m.credential_requirements[0]
        assert cred.kind == "oauth"
        assert cred.provider == "google"
        assert cred.env_key_prefix == "GMAIL_OAUTH"
        # All three scopes present
        scopes = set(cred.scopes)
        assert "https://www.googleapis.com/auth/gmail.readonly" in scopes
        assert "https://www.googleapis.com/auth/gmail.send" in scopes
        assert "https://www.googleapis.com/auth/userinfo.email" in scopes

    def test_manifest_proposes_gmail_domains(self, email_pkg: Path):
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        proposals = {(p.policy_type, p.value) for p in m.allowlist_proposals}
        assert ("domain", "gmail.googleapis.com") in proposals
        assert ("domain", "oauth2.googleapis.com") in proposals

    def test_manifest_no_sender_proposals(self, email_pkg: Path):
        """T9 mitigation: zero email-sender allowlist proposals on day 1."""
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        email_proposals = [
            p for p in m.allowlist_proposals if p.policy_type == "email"
        ]
        assert email_proposals == []


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class TestDataModels:
    def test_all_data_models_register(
        self, email_pkg: Path, fresh_registry,
    ):
        from carpenter.packages.loaders import load_data_models
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        n, errors = load_data_models(m)
        assert errors == []
        # Phase 3a added ``EmailTriageExtract`` (9 total); Phase 3b
        # added ``AttachmentMetadata`` (10); Phase 4 added three
        # index dataclasses (``EmailIndexFetchedEntry``,
        # ``EmailIndexFetchedBatch``, ``EmailIndexBatchReceipt``) for
        # a total of 13.
        assert n == 13
        for kind in (
            "EmailReviewBriefing",
            "EmailSimpleTextExtract",
            "EmailMeetingInviteExtract",
            "EmailOrderConfirmationExtract",
            "EmailSendResult",
            "EmailArchiveResult",
            "EmailMarkReadResult",
            "EmailDraftResult",
            "EmailTriageExtract",
            "AttachmentMetadata",
            "EmailIndexFetchedEntry",
            "EmailIndexFetchedBatch",
            "EmailIndexBatchReceipt",
        ):
            assert fresh_registry.lookup_kind(kind) is not None, kind

    def test_simple_extract_construction(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_data_models
        from carpenter.packages.manifest import load_manifest
        from carpenter_tools.policy.types import EmailPolicy, Url

        m = load_manifest(email_pkg / "manifest.yaml")
        load_data_models(m)
        cls = fresh_registry.lookup_kind("EmailSimpleTextExtract")

        # Add a URL prefix so Url can validate.
        from carpenter.security import get_policies
        get_policies().add("url", "https://example.com")

        extract = cls(
            provider_message_id="abc123",
            expected_account_email=EmailPolicy("ben@example.com"),
            from_address=EmailPolicy("alice@example.com"),
            to_addresses=(EmailPolicy("ben@example.com"),),
            subject="hi",
            received_at="2026-05-06T14:00:00Z",
            body_summary="quick note",
            extracted_urls=(Url("https://example.com/foo"),),
            flags=(),
        )
        assert extract.subject == "hi"
        assert extract.body_summary == "quick note"
        get_policies().remove("url", "https://example.com")


# ---------------------------------------------------------------------------
# JUDGE handlers
# ---------------------------------------------------------------------------


def _make_simple_extract(fresh_registry, **overrides):
    """Build an EmailSimpleTextExtract with sensible test defaults."""
    from carpenter_tools.policy.types import EmailPolicy

    cls = fresh_registry.lookup_kind("EmailSimpleTextExtract")
    base = dict(
        provider_message_id="m1",
        expected_account_email=EmailPolicy("ben@example.com"),
        from_address=EmailPolicy("alice@example.com"),
        to_addresses=(EmailPolicy("ben@example.com"),),
        cc_addresses=(),
        subject="hello",
        received_at="2026-05-06T14:00:00Z",
        schema_version="1.0",
        body_summary="short body",
        extracted_urls=(),
        flags=(),
    )
    base.update(overrides)
    return cls(**base)


class TestJudgeHandlers:
    def test_all_judge_handlers_register(
        self, email_pkg: Path, fresh_registry,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)

        # Phase 4 added three indexer templates that share the same
        # JUDGE handler (judge_email_index_fetched_batch) plus a
        # second JUDGE for the post-embed receipt
        # (judge_email_index_batch).  The receipt JUDGE is not bound
        # to any arc template, only to manifest.judge_handlers — we
        # exercise it via the Phase 4 JUDGE test class below.
        for tn in (
            "email_read_simple_text",
            "email_read_meeting_invite",
            "email_read_order_confirmation",
            "email_write_send",
            "email_write_archive",
            "email_write_mark_read",
            "email_write_draft",
            "email_triage",
            "email_index_phase1",
            "email_index_phase2",
            "email_index_incremental",
        ):
            handler = fresh_registry.lookup_judge(tn)
            assert handler is not None, tn
            assert callable(handler)

    def test_simple_text_judge_approves_minimal(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_read_simple_text")
        ext = _make_simple_extract(fresh_registry)
        result = handler(ext)
        assert result.approved is True, result.reason

    def test_simple_text_judge_rejects_control_char_subject(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_read_simple_text")
        ext = _make_simple_extract(fresh_registry, subject="hi\x00there")
        result = handler(ext)
        assert result.approved is False
        assert "control" in result.reason.lower()

    def test_simple_text_judge_rejects_oversize_body(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_read_simple_text")
        ext = _make_simple_extract(fresh_registry, body_summary="x" * 501)
        result = handler(ext)
        assert result.approved is False
        assert "500" in result.reason

    def test_simple_text_judge_rejects_recipient_mismatch(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest
        from carpenter_tools.policy.types import EmailPolicy

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_read_simple_text")
        # expected_account is ben@, but to is just alice@ — mismatch.
        ext = _make_simple_extract(
            fresh_registry,
            to_addresses=(EmailPolicy("alice@example.com"),),
        )
        result = handler(ext)
        assert result.approved is False
        assert "expected_account_email" in result.reason

    def test_simple_text_judge_rejects_bad_schema_version(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_read_simple_text")
        ext = _make_simple_extract(fresh_registry, schema_version="9.9")
        result = handler(ext)
        assert result.approved is False
        assert "schema_version" in result.reason

    def test_simple_text_judge_rejects_too_many_urls(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest
        from carpenter.security import get_policies
        from carpenter_tools.policy.types import Url

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        get_policies().add("url", "https://example.com")
        try:
            handler = fresh_registry.lookup_judge("email_read_simple_text")
            urls = tuple(Url(f"https://example.com/{i}") for i in range(17))
            ext = _make_simple_extract(fresh_registry, extracted_urls=urls)
            result = handler(ext)
            assert result.approved is False
            assert "URLs" in result.reason or "16" in result.reason
        finally:
            get_policies().remove("url", "https://example.com")

    def test_meeting_invite_judge_approves(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest
        from carpenter_tools.policy.types import EmailPolicy

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        cls = fresh_registry.lookup_kind("EmailMeetingInviteExtract")
        handler = fresh_registry.lookup_judge("email_read_meeting_invite")
        ext = cls(
            provider_message_id="m2",
            expected_account_email=EmailPolicy("ben@example.com"),
            from_address=EmailPolicy("calendar@meeting.example.com"),
            to_addresses=(EmailPolicy("ben@example.com"),),
            subject="meeting tomorrow",
            received_at="2026-05-06T14:00:00Z",
            start_at="2026-05-07T10:00:00Z",
            end_at="2026-05-07T11:00:00Z",
            location="Zoom",
            organizer=EmailPolicy("calendar@meeting.example.com"),
            body_summary="meet at 10",
        )
        result = handler(ext)
        assert result.approved is True, result.reason

    def test_meeting_invite_judge_rejects_bad_start_time(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest
        from carpenter_tools.policy.types import EmailPolicy

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        cls = fresh_registry.lookup_kind("EmailMeetingInviteExtract")
        handler = fresh_registry.lookup_judge("email_read_meeting_invite")
        ext = cls(
            provider_message_id="m2",
            expected_account_email=EmailPolicy("ben@example.com"),
            from_address=EmailPolicy("calendar@meeting.example.com"),
            to_addresses=(EmailPolicy("ben@example.com"),),
            subject="meeting",
            received_at="2026-05-06T14:00:00Z",
            start_at="not a date",
            end_at="2026-05-07T11:00:00Z",
            location="Zoom",
            organizer=EmailPolicy("calendar@meeting.example.com"),
        )
        result = handler(ext)
        assert result.approved is False
        assert "start_at" in result.reason

    def test_order_confirmation_judge_approves(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest
        from carpenter_tools.policy.types import EmailPolicy

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        cls = fresh_registry.lookup_kind("EmailOrderConfirmationExtract")
        handler = fresh_registry.lookup_judge("email_read_order_confirmation")
        ext = cls(
            provider_message_id="m3",
            expected_account_email=EmailPolicy("ben@example.com"),
            from_address=EmailPolicy("vendor@shop.example.com"),
            to_addresses=(EmailPolicy("ben@example.com"),),
            subject="Your order",
            received_at="2026-05-06T14:00:00Z",
            vendor="Shop",
            total="$42.99",
            order_id="ORD-123",
            items=("Widget", "Sprocket"),
            body_summary="thanks for your order",
        )
        result = handler(ext)
        assert result.approved is True, result.reason

    def test_order_confirmation_judge_rejects_too_many_items(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest
        from carpenter_tools.policy.types import EmailPolicy

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        cls = fresh_registry.lookup_kind("EmailOrderConfirmationExtract")
        handler = fresh_registry.lookup_judge("email_read_order_confirmation")
        ext = cls(
            provider_message_id="m3",
            expected_account_email=EmailPolicy("ben@example.com"),
            from_address=EmailPolicy("vendor@shop.example.com"),
            to_addresses=(EmailPolicy("ben@example.com"),),
            subject="big order",
            received_at="2026-05-06T14:00:00Z",
            vendor="Shop",
            total="$1000",
            order_id="X",
            items=tuple(f"item-{i}" for i in range(9)),
        )
        result = handler(ext)
        assert result.approved is False
        assert "items" in result.reason


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


class TestTemplates:
    def test_eleven_templates_load(
        self, email_pkg: Path, fresh_registry,
    ):
        """All eleven template.yamls parse and register without error.

        Phase 3a added ``email_triage`` (8 total); Phase 4 added
        three semantic-index templates for 11.
        """
        from carpenter.packages.loaders import load_arc_templates
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        n, errors, names = load_arc_templates(m)
        assert errors == [], errors
        assert n == 11
        assert set(names) == {
            "email_read_simple_text",
            "email_read_meeting_invite",
            "email_read_order_confirmation",
            "email_write_send",
            "email_write_archive",
            "email_write_mark_read",
            "email_write_draft",
            "email_triage",
            "email_index_phase1",
            "email_index_phase2",
            "email_index_incremental",
        }

    def test_each_template_has_static_reviewer_prompt(
        self, email_pkg: Path,
    ):
        """Static REVIEWER prompt files ship next to each template.yaml."""
        for tdir in (
            "email-read-simple-text",
            "email-read-meeting-invite",
            "email-read-order-confirmation",
            "email-write-send",
            "email-write-archive",
            "email-write-mark-read",
            "email-write-draft",
            "email-triage",
            "email-index-phase1",
            "email-index-phase2",
            "email-index-incremental",
        ):
            prompt = email_pkg / "templates" / tdir / "reviewer.txt"
            assert prompt.is_file(), f"missing {prompt}"
            content = prompt.read_text()
            # Sanity: no template-injection placeholders, no trailing
            # control chars in the static prompt.
            assert "{user_input}" not in content
            assert "{briefing}" not in content
            # Every prompt warns the agent to ignore body instructions.
            assert (
                "ignore" in content.lower()
                or "do not follow" in content.lower()
                or "may contain instructions" in content.lower()
            )


# ---------------------------------------------------------------------------
# KB articles
# ---------------------------------------------------------------------------


class TestKbArticles:
    def test_all_kb_files_present(self, email_pkg: Path):
        # Phase 1: overview, policy-setup, trust-warning, style.
        # Phase 3a: inbound-triage.
        # Phase 3b: attachments.
        # Phase 4:  index, search.
        for name in (
            "overview.md",
            "policy-setup.md",
            "trust-warning.md",
            "style.md",
            "inbound-triage.md",
            "attachments.md",
            "index.md",
            "search.md",
        ):
            path = email_pkg / "kb" / name
            assert path.is_file(), f"missing {path}"


# ---------------------------------------------------------------------------
# Chat tools (smoke — full e2e requires a running platform)
# ---------------------------------------------------------------------------


class TestChatTools:
    def test_tools_module_imports(self, email_pkg: Path, monkeypatch):
        """Importing the package's tools.py succeeds and registers
        @chat_tool-decorated functions.

        Uses the platform's ``_import_package_module`` so the relative
        imports inside tools.py (``from .arc_builders import ...`` and
        ``from .scripts import ...``) resolve via the synthetic
        ``_carpenter_pkg_`` parent package — the same package-aware path
        the registry now takes when registering chat tools.
        """
        from carpenter.packages.loaders import _import_package_module

        # Load the sibling modules tools.py relatively imports first so
        # they're cached under the right namespaced names.
        _import_package_module("carpenter-gmail", "data_models", email_pkg)
        _import_package_module("carpenter-gmail", "scripts", email_pkg)
        _import_package_module("carpenter-gmail", "arc_builders", email_pkg)
        module = _import_package_module(
            "carpenter-gmail", "tools", email_pkg,
        )

        # @chat_tool decorator attaches _chat_tool_meta to each function.
        names = {
            attr for attr in dir(module)
            if hasattr(getattr(module, attr), "_chat_tool_meta")
        }
        assert "pkg_gmail_authorize" in names
        assert "pkg_gmail_list_inbox" in names
        assert "pkg_gmail_search_emails" in names
        assert "pkg_gmail_read_email" in names
        assert "pkg_gmail_send_email" in names
        assert "pkg_gmail_trust_sender" in names
        assert "pkg_gmail_untrust_sender" in names

    def test_send_email_requires_user_confirm(self, email_pkg: Path):
        """The send tool MUST set requires_user_confirm=True."""
        from carpenter.packages.loaders import _import_package_module

        _import_package_module("carpenter-gmail", "data_models", email_pkg)
        _import_package_module("carpenter-gmail", "scripts", email_pkg)
        _import_package_module("carpenter-gmail", "arc_builders", email_pkg)
        module = _import_package_module(
            "carpenter-gmail", "tools", email_pkg,
        )

        meta = module.pkg_gmail_send_email._chat_tool_meta
        assert meta["requires_user_confirm"] is True
        # Capabilities must include both arc_create and external_effect.
        caps = set(meta["capabilities"])
        assert "arc_create" in caps
        assert "external_effect" in caps

    def test_trust_sender_requires_user_confirm(self, email_pkg: Path):
        """Allowlist-mutation tools MUST set requires_user_confirm=True."""
        from carpenter.packages.loaders import _import_package_module

        _import_package_module("carpenter-gmail", "data_models", email_pkg)
        _import_package_module("carpenter-gmail", "scripts", email_pkg)
        _import_package_module("carpenter-gmail", "arc_builders", email_pkg)
        module = _import_package_module(
            "carpenter-gmail", "tools", email_pkg,
        )

        for name in ("pkg_gmail_trust_sender", "pkg_gmail_untrust_sender"):
            meta = getattr(module, name)._chat_tool_meta
            assert meta["requires_user_confirm"] is True, name

    def test_send_email_blocks_unallowlisted_recipient(
        self, email_pkg: Path, policies_with_test_emails,
    ):
        """Calling pkg_gmail_send_email with a non-allowlisted ``to`` returns
        an error JSON — the EXECUTOR is never created.

        Uses the platform's ``_import_package_module`` so the relative
        ``from .scripts import`` inside tools.py resolves correctly.
        The policies fixture seeded only specific addresses; ``mallory@``
        is deliberately not in the allowlist.
        """
        from carpenter.packages.loaders import _import_package_module

        # First load data_models + scripts so they're cached in sys.modules
        # under the right namespaced names, then load tools.py.
        _import_package_module("carpenter-gmail", "data_models", email_pkg)
        _import_package_module("carpenter-gmail", "scripts", email_pkg)
        module = _import_package_module(
            "carpenter-gmail", "tools", email_pkg,
        )

        result_json = module.pkg_gmail_send_email(
            {
                "to": ["mallory@evil.example.com"],
                "subject": "hi",
                "body": "test",
            },
        )
        result = json.loads(result_json)
        assert "error" in result
        assert "allowlist" in result["error"].lower()


# ---------------------------------------------------------------------------
# Pre-verified scripts
# ---------------------------------------------------------------------------


class TestScripts:
    def test_fetch_script_uses_only_allowed_dispatches(self, email_pkg: Path):
        """The pre-verified Gmail-fetch script may only call the dispatch
        labels the EXECUTOR is allowed to use.  This is a static lint —
        the actual sandbox enforcement happens at runtime, but a
        regression here would surface immediately.

        We import the scripts module and inspect each constant string
        rather than scanning the source file (which would also pick up
        regex-like substrings inside the source comments and the
        dispatch-label strings themselves)."""
        import importlib.util
        import re

        spec = importlib.util.spec_from_file_location(
            "_test_pkg_gmail_scripts", email_pkg / "scripts.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        allowed = {
            "state.get", "state.set", "web.get", "web.post",
            "files.write", "resource.finalize",
        }
        for script_name in (
            "GMAIL_FETCH_SCRIPT", "GMAIL_SEND_SCRIPT", "GMAIL_SEARCH_SCRIPT",
        ):
            script = getattr(module, script_name)
            calls = re.findall(
                r'dispatch\(\s*Label\("([a-z_.]+)"\)', script,
            )
            assert calls, f"{script_name} contains no dispatch calls"
            unexpected = set(calls) - allowed
            assert not unexpected, (
                f"{script_name} uses disallowed dispatch labels: "
                f"{unexpected}"
            )

    def test_send_script_does_expected_account_check(self, email_pkg: Path):
        """The send script must call userinfo before posting the message —
        defence against a swapped-in refresh token."""
        text = (email_pkg / "scripts.py").read_text()
        userinfo_idx = text.find("oauth2/v3/userinfo")
        send_idx = text.find("messages/send")
        assert userinfo_idx > 0
        assert send_idx > 0
        assert userinfo_idx < send_idx, (
            "userinfo check must precede messages/send in the script body"
        )

    def test_search_script_url_encodes_special_chars(self, email_pkg: Path):
        """The search script must use ``urllib.parse.quote_plus`` (not a
        naive space-to-plus replacement) so reserved URL characters like
        ``&``, ``#``, ``?`` in a Gmail query don't break the request URL.

        We exercise the script's quoting by lifting the body of the
        relevant snippet and running it against a query that contains
        special characters.
        """
        import importlib.util
        from urllib.parse import quote_plus

        spec = importlib.util.spec_from_file_location(
            "_test_pkg_gmail_scripts_quote", email_pkg / "scripts.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        text = module.GMAIL_SEARCH_SCRIPT
        # The script must import quote_plus (the canonical encoder) and
        # not fall back to a bare ``replace(" ", "+")`` substitution.
        assert "quote_plus" in text, (
            "search script must use urllib.parse.quote_plus for q encoding"
        )
        assert 'q.replace(" ", "+")' not in text, (
            "search script still uses naive space-to-plus quoting; "
            "switch to urllib.parse.quote_plus(q)"
        )
        # Sanity check that quote_plus would have handled a query with
        # &/?/# correctly (this is just exercising the stdlib helper to
        # document the expected output for the next maintainer).
        encoded = quote_plus("from:a@b.com & subject:#tag?")
        assert "&" not in encoded
        assert "#" not in encoded
        assert "?" not in encoded
        assert "+" in encoded  # spaces encoded


# ---------------------------------------------------------------------------
# Fail-closed read tools when expected_account is unset
# ---------------------------------------------------------------------------


class TestExpectedAccountFailClosed:
    """When neither GMAIL_OAUTH_ACCOUNT_EMAIL nor operator_email is
    configured the read tools must refuse rather than silently
    bypassing the T1 envelope check.
    """

    def _load_tools(self, email_pkg: Path):
        from carpenter.packages.loaders import _import_package_module

        _import_package_module("carpenter-gmail", "data_models", email_pkg)
        _import_package_module("carpenter-gmail", "scripts", email_pkg)
        return _import_package_module(
            "carpenter-gmail", "tools", email_pkg,
        )

    def test_read_email_refuses_when_unconfigured(
        self, email_pkg: Path, monkeypatch,
    ):
        from carpenter import config

        monkeypatch.setitem(config.CONFIG, "GMAIL_OAUTH_ACCOUNT_EMAIL", "")
        monkeypatch.setitem(config.CONFIG, "operator_email", "")
        module = self._load_tools(email_pkg)
        result = json.loads(
            module.pkg_gmail_read_email({"provider_message_id": "abc"}),
        )
        assert "error" in result
        assert "expected_account" in result["error"].lower()
        assert "authorize" in result["error"].lower()

    def test_search_emails_refuses_when_unconfigured(
        self, email_pkg: Path, monkeypatch,
    ):
        from carpenter import config

        monkeypatch.setitem(config.CONFIG, "GMAIL_OAUTH_ACCOUNT_EMAIL", "")
        monkeypatch.setitem(config.CONFIG, "operator_email", "")
        module = self._load_tools(email_pkg)
        result = json.loads(
            module.pkg_gmail_search_emails({"query": "in:inbox"}),
        )
        assert "error" in result
        assert "expected_account" in result["error"].lower()


# ---------------------------------------------------------------------------
# Phase 1.5 modify tools (archive / mark-read / draft)
# ---------------------------------------------------------------------------


class TestPhase15ModifyTools:
    """Surface-level tests for the Phase 1.5 external-effect tools.

    Each modify tool mirrors ``pkg_gmail_send_email``'s trust shape:
    single-arc untrusted EXECUTOR pipeline guarded by
    ``requires_user_confirm=True`` plus an in-script expected-account
    check.  These tests cover the chat-tool front door (manifest +
    decorator metadata + allowlist precheck + fail-closed when the
    expected account is unconfigured) and the script-AST surface (only
    the allowed dispatch labels).  Runtime behaviour of the EXECUTOR
    scripts themselves is exercised by the carpenter-linux acceptance
    story s055.
    """

    # ----- Manifest -----

    def test_manifest_version_at_or_above_0_3_0(self, email_pkg: Path):
        """Sanity check that the write-tool refactor's manifest bump
        wasn't reverted.  Phase 3a (0.4.0) is the current version; any
        future bump should keep semver ordering above 0.3.0."""
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        parts = [int(p) for p in m.version.split(".")[:3]]
        assert parts >= [0, 3, 0], m.version

    def test_manifest_declares_modify_and_compose_scopes(
        self, email_pkg: Path,
    ):
        """v0.2.0 must declare gmail.modify (archive + mark-read) and
        gmail.compose (drafts) in addition to the Phase 1 scope set."""
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        cred = m.credential_requirements[0]
        scopes = set(cred.scopes)
        for required in (
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.compose",
            "https://www.googleapis.com/auth/userinfo.email",
        ):
            assert required in scopes, f"missing scope {required}"

    # ----- Tools loaded with @chat_tool metadata -----

    def _load_tools(self, email_pkg: Path):
        from carpenter.packages.loaders import _import_package_module

        _import_package_module("carpenter-gmail", "data_models", email_pkg)
        _import_package_module("carpenter-gmail", "scripts", email_pkg)
        return _import_package_module(
            "carpenter-gmail", "tools", email_pkg,
        )

    def test_three_new_tools_registered(self, email_pkg: Path):
        """The package's tools.py exports three new @chat_tool functions."""
        module = self._load_tools(email_pkg)
        for name in (
            "pkg_gmail_archive_email",
            "pkg_gmail_mark_read_email",
            "pkg_gmail_draft_email",
        ):
            fn = getattr(module, name, None)
            assert fn is not None, f"missing {name}"
            assert hasattr(fn, "_chat_tool_meta"), (
                f"{name} missing @chat_tool decorator metadata"
            )

    def test_each_modify_tool_requires_user_confirm(self, email_pkg: Path):
        """Every external-effect modify tool MUST set
        requires_user_confirm=True — this is the load-bearing chat-boundary
        gate that mirrors pkg_gmail_send_email."""
        module = self._load_tools(email_pkg)
        for name in (
            "pkg_gmail_archive_email",
            "pkg_gmail_mark_read_email",
            "pkg_gmail_draft_email",
        ):
            meta = getattr(module, name)._chat_tool_meta
            assert meta["requires_user_confirm"] is True, (
                f"{name} must set requires_user_confirm=True"
            )
            caps = set(meta["capabilities"])
            assert "arc_create" in caps, (
                f"{name} must declare arc_create capability"
            )
            assert "external_effect" in caps, (
                f"{name} must declare external_effect capability"
            )

    # ----- Draft blocks un-allowlisted recipients -----

    def test_draft_email_blocks_unallowlisted_recipient(
        self, email_pkg: Path, policies_with_test_emails,
    ):
        """pkg_gmail_draft_email must mirror pkg_gmail_send_email's
        chat-boundary allowlist check.  A draft staged with an
        un-allowlisted recipient would be a foothold for a later
        send-bypass and is refused up-front."""
        module = self._load_tools(email_pkg)
        result = json.loads(module.pkg_gmail_draft_email({
            "to": ["mallory@evil.example.com"],
            "subject": "phishy",
            "body": "click here",
        }))
        assert "error" in result
        assert "allowlist" in result["error"].lower()

    def test_draft_email_validates_to_subject_body(
        self, email_pkg: Path, policies_with_test_emails,
    ):
        """The chat tool returns shape-of-input errors before recipient
        validation kicks in."""
        module = self._load_tools(email_pkg)
        # Empty to list
        result = json.loads(module.pkg_gmail_draft_email({
            "to": [], "subject": "x", "body": "y",
        }))
        assert "error" in result
        # Missing subject
        result = json.loads(module.pkg_gmail_draft_email({
            "to": ["alice@example.com"], "subject": "", "body": "y",
        }))
        assert "error" in result
        # Missing body
        result = json.loads(module.pkg_gmail_draft_email({
            "to": ["alice@example.com"], "subject": "x", "body": "",
        }))
        assert "error" in result

    # ----- Fail-closed when expected_account is unconfigured -----

    def test_archive_refuses_when_expected_account_unconfigured(
        self, email_pkg: Path, monkeypatch,
    ):
        """Without GMAIL_OAUTH_ACCOUNT_EMAIL or operator_email the
        archive tool must fail closed — the in-script expected-account
        check is unenforceable without a configured mailbox."""
        from carpenter import config

        monkeypatch.setitem(config.CONFIG, "GMAIL_OAUTH_ACCOUNT_EMAIL", "")
        monkeypatch.setitem(config.CONFIG, "operator_email", "")
        module = self._load_tools(email_pkg)
        result = json.loads(module.pkg_gmail_archive_email({
            "provider_message_id": "abc123",
        }))
        assert "error" in result
        assert "expected-account" in result["error"].lower() or (
            "expected_account" in result["error"].lower()
        )

    def test_mark_read_refuses_when_expected_account_unconfigured(
        self, email_pkg: Path, monkeypatch,
    ):
        from carpenter import config

        monkeypatch.setitem(config.CONFIG, "GMAIL_OAUTH_ACCOUNT_EMAIL", "")
        monkeypatch.setitem(config.CONFIG, "operator_email", "")
        module = self._load_tools(email_pkg)
        result = json.loads(module.pkg_gmail_mark_read_email({
            "provider_message_id": "abc123",
        }))
        assert "error" in result
        assert "expected-account" in result["error"].lower() or (
            "expected_account" in result["error"].lower()
        )

    def test_draft_refuses_when_expected_account_unconfigured(
        self, email_pkg: Path, monkeypatch, policies_with_test_emails,
    ):
        """The draft tool must do the expected-account check too —
        otherwise a swapped token could stage drafts in the wrong
        mailbox."""
        from carpenter import config

        monkeypatch.setitem(config.CONFIG, "GMAIL_OAUTH_ACCOUNT_EMAIL", "")
        monkeypatch.setitem(config.CONFIG, "operator_email", "")
        module = self._load_tools(email_pkg)
        result = json.loads(module.pkg_gmail_draft_email({
            "to": ["alice@example.com"],
            "subject": "hi",
            "body": "test",
        }))
        assert "error" in result
        assert "expected-account" in result["error"].lower() or (
            "expected_account" in result["error"].lower()
        )

    # ----- Tools also require provider_message_id -----

    def test_archive_requires_provider_message_id(self, email_pkg: Path):
        module = self._load_tools(email_pkg)
        result = json.loads(module.pkg_gmail_archive_email({}))
        assert "error" in result
        assert "provider_message_id" in result["error"]

    def test_mark_read_requires_provider_message_id(self, email_pkg: Path):
        module = self._load_tools(email_pkg)
        result = json.loads(module.pkg_gmail_mark_read_email({}))
        assert "error" in result
        assert "provider_message_id" in result["error"]

    # ----- AST-lint: new scripts only use allowed dispatch labels -----

    def test_phase15_scripts_use_only_allowed_dispatches(
        self, email_pkg: Path,
    ):
        """The Phase 1.5 v2 EXECUTOR scripts may only call the dispatch
        labels the EXECUTOR is allowed to use.  v0.3.0 expanded the
        allowlist to include ``files.write`` and ``resource.finalize``
        because every write script now persists a structured JSON
        receipt to a raw Resource on disk so the REVIEWER + JUDGE can
        graduate a typed EmailXxxResult."""
        import importlib.util
        import re

        spec = importlib.util.spec_from_file_location(
            "_test_pkg_gmail_scripts_p15", email_pkg / "scripts.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # v0.3.0 allowlist: receipt-Resource persistence requires
        # files.write + resource.finalize.
        allowed = {
            "state.get", "state.set", "web.get", "web.post",
            "files.write", "resource.finalize",
        }
        for script_name in (
            "GMAIL_ARCHIVE_SCRIPT",
            "GMAIL_MARK_READ_SCRIPT",
            "GMAIL_DRAFT_SCRIPT",
        ):
            script = getattr(module, script_name, None)
            assert script is not None, f"missing {script_name}"
            calls = set(re.findall(
                r'dispatch\(\s*Label\("([a-z_.]+)"\)', script,
            ))
            assert calls, f"{script_name} contains no dispatch calls"
            unexpected = calls - allowed
            assert not unexpected, (
                f"{script_name} uses disallowed dispatch labels: "
                f"{unexpected} (allowed for Phase 1.5 v2 modify scripts: "
                f"{allowed})"
            )
            # Every v0.3.0 modify script must persist the receipt.
            assert "files.write" in calls, (
                f"{script_name} must write the receipt to a raw Resource"
            )
            assert "resource.finalize" in calls, (
                f"{script_name} must finalize the receipt Resource"
            )

    def test_phase15_scripts_do_expected_account_check_before_modify(
        self, email_pkg: Path,
    ):
        """Each modify script must hit the userinfo endpoint before
        the mutating POST — defence against swapped-in refresh tokens."""
        text = (email_pkg / "scripts.py").read_text()

        # Each script must contain BOTH the userinfo check AND the
        # mutating call, with userinfo appearing first inside the script.
        # We extract each script's body and check the ordering inside it.
        import re
        script_bodies = re.findall(
            r"(GMAIL_(?:ARCHIVE|MARK_READ|DRAFT)_SCRIPT) = '''(.*?)'''",
            text,
            re.DOTALL,
        )
        assert len(script_bodies) == 3, (
            f"expected 3 Phase 1.5 scripts, found {len(script_bodies)}"
        )
        for name, body in script_bodies:
            userinfo_idx = body.find("oauth2/v3/userinfo")
            assert userinfo_idx > 0, (
                f"{name} does not call userinfo endpoint"
            )
            if name == "GMAIL_DRAFT_SCRIPT":
                # Drafts POST to /users/me/drafts
                mutate_idx = body.find("/users/me/drafts")
            else:
                # Archive + mark-read POST to /messages/{id}/modify
                mutate_idx = body.find("/modify")
            assert mutate_idx > 0, (
                f"{name} does not perform the expected mutating call"
            )
            assert userinfo_idx < mutate_idx, (
                f"{name}: userinfo check must precede the mutating call "
                f"(userinfo@{userinfo_idx}, mutate@{mutate_idx})"
            )

    def test_archive_and_mark_read_scripts_compute_idempotency_flag(
        self, email_pkg: Path,
    ):
        """The archive script must compute was_already_archived from
        the pre-modify labelIds; mark-read must compute was_already_read.
        Both rely on a metadata GET before the modify POST."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_test_pkg_gmail_scripts_idem", email_pkg / "scripts.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        archive_src = module.GMAIL_ARCHIVE_SCRIPT
        assert "was_already_archived" in archive_src
        assert "format=metadata" in archive_src
        assert '"INBOX"' in archive_src

        mark_src = module.GMAIL_MARK_READ_SCRIPT
        assert "was_already_read" in mark_src
        assert "format=metadata" in mark_src
        assert '"UNREAD"' in mark_src

    def test_draft_script_returns_draft_id_and_message_id(
        self, email_pkg: Path,
    ):
        """The draft script must extract both the Gmail-assigned
        draft_id and the embedded message id so the chat agent can
        phrase a follow-up correctly."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_test_pkg_gmail_scripts_draft", email_pkg / "scripts.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        text = module.GMAIL_DRAFT_SCRIPT
        assert "draft_id" in text
        assert "provider_message_id" in text
        # The drafts.create endpoint returns {id, message: {id, ...}}.
        assert 'draft_body.get("id")' in text
        assert 'message.get("id")' in text

    # ----- include_granted_scopes carries the OAuth migration -----

    def test_authorize_passes_include_granted_scopes(self, email_pkg: Path):
        """pkg_gmail_authorize must request include_granted_scopes=true so
        existing v0.1.0 users can augment their grant non-destructively
        when re-running authorize after the v0.2.0 upgrade."""
        text = (email_pkg / "tools.py").read_text()
        assert "include_granted_scopes" in text, (
            "pkg_gmail_authorize must pass include_granted_scopes via "
            "extra_authorize_params for the v0.1.0 -> v0.2.0 OAuth "
            "scope migration"
        )


# ---------------------------------------------------------------------------
# Phase 1.5 v2 write-side JUDGE handlers
# ---------------------------------------------------------------------------


def _make_send_result(fresh_registry, **overrides):
    """Build an EmailSendResult with sensible test defaults."""
    from carpenter_tools.policy.types import EmailPolicy

    cls = fresh_registry.lookup_kind("EmailSendResult")
    base = dict(
        status="sent",
        expected_account_email=EmailPolicy("ben@example.com"),
        provider_message_id="msg_abc123",
        to_addresses=(EmailPolicy("alice@example.com"),),
        schema_version="1.0",
    )
    base.update(overrides)
    return cls(**base)


def _make_archive_result(fresh_registry, **overrides):
    from carpenter_tools.policy.types import EmailPolicy

    cls = fresh_registry.lookup_kind("EmailArchiveResult")
    base = dict(
        status="archived",
        expected_account_email=EmailPolicy("ben@example.com"),
        provider_message_id="msg_abc123",
        was_already_archived=False,
        schema_version="1.0",
    )
    base.update(overrides)
    return cls(**base)


def _make_mark_read_result(fresh_registry, **overrides):
    from carpenter_tools.policy.types import EmailPolicy

    cls = fresh_registry.lookup_kind("EmailMarkReadResult")
    base = dict(
        status="marked_read",
        expected_account_email=EmailPolicy("ben@example.com"),
        provider_message_id="msg_abc123",
        was_already_read=False,
        schema_version="1.0",
    )
    base.update(overrides)
    return cls(**base)


def _make_draft_result(fresh_registry, **overrides):
    from carpenter_tools.policy.types import EmailPolicy

    cls = fresh_registry.lookup_kind("EmailDraftResult")
    base = dict(
        status="drafted",
        expected_account_email=EmailPolicy("ben@example.com"),
        provider_message_id="msg_abc123",
        draft_id="r-7654321",
        to_addresses=(EmailPolicy("alice@example.com"),),
        schema_version="1.0",
    )
    base.update(overrides)
    return cls(**base)


class TestPhase15WriteJudges:
    """Deterministic JUDGE handlers for the four Phase 1.5 v2 write
    receipts.  Each handler must approve a well-formed extract and
    reject every documented failure mode: wrong dataclass type, wrong
    status literal, bad provider_message_id shape, schema_version
    mismatch, missing recipient set (for send/draft), non-bool
    idempotency flag (for archive/mark-read), and wrong draft_id
    shape (for draft).
    """

    # ----- email_write_send -----

    def test_send_judge_approves_well_formed(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_send")
        ext = _make_send_result(fresh_registry)
        verdict = handler(ext)
        assert verdict.approved is True, verdict.reason

    def test_send_judge_rejects_wrong_dataclass_type(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_send")
        # Pass an EmailArchiveResult instead — must reject.
        wrong = _make_archive_result(fresh_registry)
        verdict = handler(wrong)
        assert verdict.approved is False
        assert "EmailSendResult" in verdict.reason

    def test_send_judge_rejects_non_sent_status(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_send")
        ext = _make_send_result(fresh_registry, status="SENT")
        verdict = handler(ext)
        assert verdict.approved is False
        assert "sent" in verdict.reason.lower()

    def test_send_judge_rejects_bad_provider_message_id(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_send")
        # Too short (regex requires 5+ chars)
        verdict = handler(_make_send_result(
            fresh_registry, provider_message_id="ab",
        ))
        assert verdict.approved is False
        assert "provider_message_id" in verdict.reason

    def test_send_judge_rejects_empty_recipient_list(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_send")
        verdict = handler(_make_send_result(
            fresh_registry, to_addresses=(),
        ))
        assert verdict.approved is False
        assert "to_addresses" in verdict.reason or (
            "empty" in verdict.reason.lower()
        )

    def test_send_judge_rejects_schema_version_mismatch(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_send")
        verdict = handler(_make_send_result(
            fresh_registry, schema_version="9.9",
        ))
        assert verdict.approved is False
        assert "schema_version" in verdict.reason

    # ----- email_write_archive -----

    def test_archive_judge_approves_well_formed(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_archive")
        verdict = handler(_make_archive_result(fresh_registry))
        assert verdict.approved is True, verdict.reason
        # Also approves the idempotent already-archived case.
        verdict = handler(_make_archive_result(
            fresh_registry, was_already_archived=True,
        ))
        assert verdict.approved is True, verdict.reason

    def test_archive_judge_rejects_wrong_dataclass_type(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_archive")
        wrong = _make_send_result(fresh_registry)
        verdict = handler(wrong)
        assert verdict.approved is False
        assert "EmailArchiveResult" in verdict.reason

    def test_archive_judge_rejects_non_archived_status(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_archive")
        verdict = handler(_make_archive_result(
            fresh_registry, status="ARCHIVED",
        ))
        assert verdict.approved is False
        assert "archived" in verdict.reason.lower()

    def test_archive_judge_rejects_non_bool_idempotency_flag(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_archive")
        # frozen dataclass + plain bool field; bypass with object.__setattr__
        ext = _make_archive_result(fresh_registry)
        object.__setattr__(ext, "was_already_archived", "true")
        verdict = handler(ext)
        assert verdict.approved is False
        assert "bool" in verdict.reason.lower()

    # ----- email_write_mark_read -----

    def test_mark_read_judge_approves_well_formed(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_mark_read")
        verdict = handler(_make_mark_read_result(fresh_registry))
        assert verdict.approved is True, verdict.reason

    def test_mark_read_judge_rejects_wrong_dataclass_type(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_mark_read")
        verdict = handler(_make_archive_result(fresh_registry))
        assert verdict.approved is False
        assert "EmailMarkReadResult" in verdict.reason

    def test_mark_read_judge_rejects_non_marked_read_status(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_mark_read")
        verdict = handler(_make_mark_read_result(
            fresh_registry, status="read",
        ))
        assert verdict.approved is False

    def test_mark_read_judge_rejects_non_bool_idempotency_flag(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_mark_read")
        ext = _make_mark_read_result(fresh_registry)
        object.__setattr__(ext, "was_already_read", 1)
        verdict = handler(ext)
        assert verdict.approved is False
        assert "bool" in verdict.reason.lower()

    # ----- email_write_draft -----

    def test_draft_judge_approves_well_formed(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_draft")
        verdict = handler(_make_draft_result(fresh_registry))
        assert verdict.approved is True, verdict.reason

    def test_draft_judge_rejects_wrong_dataclass_type(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_draft")
        verdict = handler(_make_send_result(fresh_registry))
        assert verdict.approved is False
        assert "EmailDraftResult" in verdict.reason

    def test_draft_judge_rejects_bad_draft_id(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_draft")
        verdict = handler(_make_draft_result(
            fresh_registry, draft_id="@@",
        ))
        assert verdict.approved is False
        assert "draft_id" in verdict.reason

    def test_draft_judge_rejects_empty_recipient_list(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_draft")
        verdict = handler(_make_draft_result(
            fresh_registry, to_addresses=(),
        ))
        assert verdict.approved is False
        assert "to_addresses" in verdict.reason or (
            "empty" in verdict.reason.lower()
        )

    def test_draft_judge_rejects_non_drafted_status(
        self, email_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(email_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_write_draft")
        verdict = handler(_make_draft_result(
            fresh_registry, status="created",
        ))
        assert verdict.approved is False


# ---------------------------------------------------------------------------
# Phase 1.5 v2 write-tool arc-tree shape
# ---------------------------------------------------------------------------


def _seed_email_config(monkeypatch):
    """Set a synthetic operator email so the in-tool expected-account
    check passes."""
    from carpenter import config

    monkeypatch.setitem(
        config.CONFIG, "GMAIL_OAUTH_ACCOUNT_EMAIL", "ben@example.com",
    )
    monkeypatch.setitem(config.CONFIG, "operator_email", "ben@example.com")


def _load_email_tools(email_pkg: Path):
    """Load the package's tools.py as the platform does."""
    from carpenter.packages.loaders import _import_package_module

    _import_package_module("carpenter-gmail", "data_models", email_pkg)
    _import_package_module("carpenter-gmail", "scripts", email_pkg)
    return _import_package_module(
        "carpenter-gmail", "tools", email_pkg,
    )


class TestPhase15WriteToolArcShape:
    """Each Phase 1.5 v2 write chat tool must build a 4-arc tree
    (PLANNER + EXECUTOR + REVIEWER + JUDGE) that the platform's
    untrusted-batch validator accepts, and must wire raw + extract
    Resources plus seed the EXECUTOR and REVIEWER arc state.

    These tests call the chat tool with the in-tool allowlist
    pre-seeded and the expected-account config set, then inspect the
    resulting DB rows to assert the arc tree, Resources, and arc
    state are as the plan v2 demands.
    """

    def _read_arcs_with_parent(self, parent_id: int) -> list[dict]:
        from carpenter.db import get_db

        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT id, name, agent_type, integrity_level, "
                "step_order FROM arcs WHERE parent_id = ? "
                "ORDER BY step_order",
                (parent_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _read_arc_state(self, arc_id: int) -> dict:
        import json as _json
        from carpenter.db import get_db

        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT key, value_json FROM arc_state WHERE arc_id = ?",
                (arc_id,),
            ).fetchall()
            return {r["key"]: _json.loads(r["value_json"]) for r in rows}
        finally:
            conn.close()

    def _read_resources_for_arc(self, arc_id: int) -> list[dict]:
        from carpenter.db import get_db

        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT r.id, r.content_type, r.produced_by_template, "
                "r.template_verdict, ar.role "
                "FROM arc_resources ar JOIN resources r "
                "ON ar.resource_id = r.id "
                "WHERE ar.arc_id = ?",
                (arc_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ----- send -----

    def test_send_email_builds_four_arc_tree(
        self, email_pkg, monkeypatch, policies_with_test_emails,
    ):
        _seed_email_config(monkeypatch)
        module = _load_email_tools(email_pkg)
        result = json.loads(module.pkg_gmail_send_email({
            "to": ["alice@example.com"],
            "subject": "hi",
            "body": "hello",
        }))
        assert "arc_id" in result, result
        parent_id = result["arc_id"]

        children = self._read_arcs_with_parent(parent_id)
        agent_types = [c["agent_type"] for c in children]
        assert agent_types == ["EXECUTOR", "REVIEWER", "JUDGE"], children
        executor, reviewer, judge = children
        assert executor["integrity_level"] == "untrusted"
        assert reviewer["integrity_level"] == "trusted"
        assert judge["integrity_level"] == "trusted"

    def test_send_email_seeds_executor_arc_state(
        self, email_pkg, monkeypatch, policies_with_test_emails,
    ):
        _seed_email_config(monkeypatch)
        module = _load_email_tools(email_pkg)
        result = json.loads(module.pkg_gmail_send_email({
            "to": ["alice@example.com"],
            "subject": "hi",
            "body": "hello",
        }))
        parent_id = result["arc_id"]
        children = self._read_arcs_with_parent(parent_id)
        executor_id = next(
            c["id"] for c in children if c["agent_type"] == "EXECUTOR"
        )
        state = self._read_arc_state(executor_id)
        assert "raw_message_b64" in state
        assert state.get("expected_account_email") == "ben@example.com"
        assert "raw_resource_path" in state
        assert "raw_resource_id" in state

    def test_send_email_seeds_parent_staged_to_addresses(
        self, email_pkg, monkeypatch, policies_with_test_emails,
    ):
        _seed_email_config(monkeypatch)
        module = _load_email_tools(email_pkg)
        result = json.loads(module.pkg_gmail_send_email({
            "to": ["alice@example.com", "vendor@shop.example.com"],
            "subject": "hi",
            "body": "hello",
        }))
        parent_id = result["arc_id"]
        state = self._read_arc_state(parent_id)
        assert state["staged_to_addresses"] == [
            "alice@example.com", "vendor@shop.example.com",
        ]
        assert state["expected_account_email"] == "ben@example.com"
        assert state["template_name"] == "email_write_send"
        assert state["extract_kind"] == "EmailSendResult"

    def test_send_email_wires_raw_and_extract_resources(
        self, email_pkg, monkeypatch, policies_with_test_emails,
    ):
        _seed_email_config(monkeypatch)
        module = _load_email_tools(email_pkg)
        result = json.loads(module.pkg_gmail_send_email({
            "to": ["alice@example.com"],
            "subject": "hi",
            "body": "hello",
        }))
        parent_id = result["arc_id"]
        children = self._read_arcs_with_parent(parent_id)
        executor_id = next(
            c["id"] for c in children if c["agent_type"] == "EXECUTOR"
        )
        reviewer_id = next(
            c["id"] for c in children if c["agent_type"] == "REVIEWER"
        )
        # EXECUTOR has the raw receipt Resource as output.
        exec_res = self._read_resources_for_arc(executor_id)
        outputs = [r for r in exec_res if r["role"] == "output"]
        assert len(outputs) == 1
        raw = outputs[0]
        assert raw["produced_by_template"] is None  # untrusted ingest
        # REVIEWER has briefing + raw as input, extract as output.
        rev_res = self._read_resources_for_arc(reviewer_id)
        rev_inputs = [r for r in rev_res if r["role"] == "input"]
        rev_outputs = [r for r in rev_res if r["role"] == "output"]
        assert len(rev_inputs) == 2
        assert len(rev_outputs) == 1
        extract = rev_outputs[0]
        assert extract["produced_by_template"] == "email_write_send"
        assert extract["template_verdict"] == "pending"

    # ----- archive -----

    def test_archive_builds_four_arc_tree(
        self, email_pkg, monkeypatch,
    ):
        _seed_email_config(monkeypatch)
        module = _load_email_tools(email_pkg)
        result = json.loads(module.pkg_gmail_archive_email({
            "provider_message_id": "msg_abc123",
        }))
        assert "arc_id" in result, result
        parent_id = result["arc_id"]
        children = self._read_arcs_with_parent(parent_id)
        agent_types = [c["agent_type"] for c in children]
        assert agent_types == ["EXECUTOR", "REVIEWER", "JUDGE"], children

    def test_archive_seeds_provider_message_id(
        self, email_pkg, monkeypatch,
    ):
        _seed_email_config(monkeypatch)
        module = _load_email_tools(email_pkg)
        result = json.loads(module.pkg_gmail_archive_email({
            "provider_message_id": "msg_abc123",
        }))
        parent_id = result["arc_id"]
        children = self._read_arcs_with_parent(parent_id)
        executor_id = next(
            c["id"] for c in children if c["agent_type"] == "EXECUTOR"
        )
        state = self._read_arc_state(executor_id)
        assert state.get("provider_message_id") == "msg_abc123"
        assert state.get("expected_account_email") == "ben@example.com"
        assert "raw_resource_path" in state

    def test_archive_parent_state_has_empty_staged_to_addresses(
        self, email_pkg, monkeypatch,
    ):
        """Archive has no recipient surface; staged_to_addresses must
        be empty (defence in depth — a REVIEWER cannot inject recipients
        the chat tool never approved)."""
        _seed_email_config(monkeypatch)
        module = _load_email_tools(email_pkg)
        result = json.loads(module.pkg_gmail_archive_email({
            "provider_message_id": "msg_abc123",
        }))
        parent_id = result["arc_id"]
        state = self._read_arc_state(parent_id)
        assert state["staged_to_addresses"] == []
        assert state["template_name"] == "email_write_archive"
        assert state["extract_kind"] == "EmailArchiveResult"

    # ----- mark-read -----

    def test_mark_read_builds_four_arc_tree(
        self, email_pkg, monkeypatch,
    ):
        _seed_email_config(monkeypatch)
        module = _load_email_tools(email_pkg)
        result = json.loads(module.pkg_gmail_mark_read_email({
            "provider_message_id": "msg_abc123",
        }))
        assert "arc_id" in result, result
        parent_id = result["arc_id"]
        children = self._read_arcs_with_parent(parent_id)
        agent_types = [c["agent_type"] for c in children]
        assert agent_types == ["EXECUTOR", "REVIEWER", "JUDGE"], children

    def test_mark_read_parent_state(
        self, email_pkg, monkeypatch,
    ):
        _seed_email_config(monkeypatch)
        module = _load_email_tools(email_pkg)
        result = json.loads(module.pkg_gmail_mark_read_email({
            "provider_message_id": "msg_abc123",
        }))
        parent_id = result["arc_id"]
        state = self._read_arc_state(parent_id)
        assert state["staged_to_addresses"] == []
        assert state["template_name"] == "email_write_mark_read"
        assert state["extract_kind"] == "EmailMarkReadResult"

    # ----- draft -----

    def test_draft_builds_four_arc_tree(
        self, email_pkg, monkeypatch, policies_with_test_emails,
    ):
        _seed_email_config(monkeypatch)
        module = _load_email_tools(email_pkg)
        result = json.loads(module.pkg_gmail_draft_email({
            "to": ["alice@example.com"],
            "subject": "drafty",
            "body": "draft body",
        }))
        assert "arc_id" in result, result
        parent_id = result["arc_id"]
        children = self._read_arcs_with_parent(parent_id)
        agent_types = [c["agent_type"] for c in children]
        assert agent_types == ["EXECUTOR", "REVIEWER", "JUDGE"], children

    def test_draft_parent_state_staged_to_addresses_matches_input(
        self, email_pkg, monkeypatch, policies_with_test_emails,
    ):
        _seed_email_config(monkeypatch)
        module = _load_email_tools(email_pkg)
        result = json.loads(module.pkg_gmail_draft_email({
            "to": ["alice@example.com", "vendor@shop.example.com"],
            "subject": "drafty",
            "body": "draft body",
        }))
        parent_id = result["arc_id"]
        state = self._read_arc_state(parent_id)
        assert state["staged_to_addresses"] == [
            "alice@example.com", "vendor@shop.example.com",
        ]
        assert state["template_name"] == "email_write_draft"
        assert state["extract_kind"] == "EmailDraftResult"

    # ----- validator no longer rejects the batch -----

    def test_send_batch_does_not_trigger_untrusted_arc_validator_error(
        self, email_pkg, monkeypatch, policies_with_test_emails,
    ):
        """The whole point of Phase 1.5 v2: the EXECUTOR is paired with a
        REVIEWER + JUDGE, so the validator at carpenter/tool_backends/arc.py:250
        no longer returns "Untrusted arcs require at least one REVIEWER or
        JUDGE arc"."""
        _seed_email_config(monkeypatch)
        module = _load_email_tools(email_pkg)
        result = json.loads(module.pkg_gmail_send_email({
            "to": ["alice@example.com"],
            "subject": "hi",
            "body": "hello",
        }))
        # The tool returns either {arc_id: N} on success or {error: ...}
        # on validator failure.  We assert success and explicitly check
        # the validator's error string is NOT what came back.
        assert "arc_id" in result, result
        assert "error" not in result, result

    def test_archive_batch_does_not_trigger_untrusted_arc_validator_error(
        self, email_pkg, monkeypatch,
    ):
        _seed_email_config(monkeypatch)
        module = _load_email_tools(email_pkg)
        result = json.loads(module.pkg_gmail_archive_email({
            "provider_message_id": "msg_abc123",
        }))
        assert "arc_id" in result, result
        assert "error" not in result, result

    def test_mark_read_batch_does_not_trigger_untrusted_arc_validator_error(
        self, email_pkg, monkeypatch,
    ):
        _seed_email_config(monkeypatch)
        module = _load_email_tools(email_pkg)
        result = json.loads(module.pkg_gmail_mark_read_email({
            "provider_message_id": "msg_abc123",
        }))
        assert "arc_id" in result, result
        assert "error" not in result, result

    def test_draft_batch_does_not_trigger_untrusted_arc_validator_error(
        self, email_pkg, monkeypatch, policies_with_test_emails,
    ):
        _seed_email_config(monkeypatch)
        module = _load_email_tools(email_pkg)
        result = json.loads(module.pkg_gmail_draft_email({
            "to": ["alice@example.com"],
            "subject": "drafty",
            "body": "draft body",
        }))
        assert "arc_id" in result, result
        assert "error" not in result, result


# =====================================================================
# Phase 3b — attachment metadata
# =====================================================================
#
# The Phase 3b tests need the v0.5.0 version of the carpenter-gmail
# package on disk.  Our on-disk fixture (the ``email_pkg`` fixture
# above) points at the merged-on-main carpenter-packages clone, which
# is v0.4.0.  To exercise the new shape we resolve a worktree-aware
# package path the same way the Phase 3a triage tests do.


def _phase_3b_package_path():
    """Locate the carpenter-gmail package at v0.5.0 (or later).

    Resolution order:
      1. ``CARPENTER_PACKAGES_DIR`` env var (for CI).
      2. ``/tmp/cc-3b-pkg/packages/carpenter-gmail`` worktree.
      3. The sibling carpenter-packages checkout (only if it is
         already at v0.5.0; v0.4.0 / earlier gets skipped).
    """
    import os
    from pathlib import Path

    env = os.environ.get("CARPENTER_PACKAGES_DIR")
    if env:
        cand = Path(env) / "carpenter-gmail"
        if cand.is_dir():
            return cand

    cand = Path("/tmp/cc-3b-pkg/packages/carpenter-gmail")
    if cand.is_dir():
        return cand

    src = _find_email_package()
    if src is not None:
        # Only accept the on-disk copy when it carries 0.5.0+.
        try:
            text = (src / "manifest.yaml").read_text()
        except OSError:
            return None
        if 'version: "0.5.0"' in text or 'version: "0.6' in text:
            return src
    return None


@pytest.fixture
def phase_3b_pkg_src() -> Path:
    src = _phase_3b_package_path()
    if src is None:
        pytest.skip(
            "carpenter-gmail Phase 3b (v0.5.0+) worktree not found; set "
            "CARPENTER_PACKAGES_DIR or create /tmp/cc-3b-pkg",
        )
    return src


@pytest.fixture
def phase_3b_pkg(tmp_path: Path, phase_3b_pkg_src: Path) -> Path:
    dst = tmp_path / "carpenter-gmail"
    shutil.copytree(phase_3b_pkg_src, dst)
    return dst


def _make_attachment(**overrides):
    """Build a minimal-but-valid AttachmentMetadata kwargs dict.

    The Phase 3b loader registers ``AttachmentMetadata`` as its own kind
    on the package's namespaced module slot, so callers should pass the
    kwargs through ``fresh_registry.lookup_kind("AttachmentMetadata")``
    to get a constructor identical to the one the JUDGE sees.
    """
    base = dict(
        filename_clean="report.pdf",
        claimed_mime_type="application/pdf",
        size_bytes=12345,
        attachment_id="abcd1234efgh",
        is_inline=False,
        schema_version="1.0",
    )
    base.update(overrides)
    return base


class TestPhase3bAttachmentJudge:
    """Direct tests of ``_check_attachment_metadata`` rejection rules.

    Loads the package, grabs the JUDGE module via the registered
    handlers, and calls the private helper.  We exercise it via the
    public ``judge_simple_text`` path in TestPhase3bExtractIntegration;
    here we go a level deeper to assert one rule per test.
    """

    def _load_helpers(self, phase_3b_pkg: Path, fresh_registry):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(phase_3b_pkg / "manifest.yaml")
        load_package_artifacts(m)
        # The judges module is the one registered for the email_triage
        # template — it's the same module for every handler in the
        # package.  Reach into the registry to get an identical class
        # object to the one isinstance() inside the JUDGE expects.
        am_cls = fresh_registry.lookup_kind("AttachmentMetadata")
        assert am_cls is not None
        # Pull the helper off the same module as the handlers.
        handler = fresh_registry.lookup_judge("email_read_simple_text")
        judges_module = handler.__module__
        import sys
        mod = sys.modules[judges_module]
        return mod, am_cls

    def test_approves_valid_attachment(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        am = am_cls(**_make_attachment())
        assert mod._check_attachment_metadata(am) is None

    def test_rejects_empty_filename(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        am = am_cls(**_make_attachment(filename_clean=""))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None and "filename" in reason

    def test_rejects_oversize_filename(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        am = am_cls(**_make_attachment(filename_clean="a" * 200))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None and "128" in reason

    def test_rejects_filename_with_nul(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        am = am_cls(**_make_attachment(filename_clean="bad\x00.pdf"))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None
        assert "control" in reason.lower() or "banned" in reason.lower()

    def test_rejects_filename_with_forward_slash(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        am = am_cls(**_make_attachment(filename_clean="a/b.pdf"))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None and "path separator" in reason

    def test_rejects_filename_with_backslash(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        am = am_cls(**_make_attachment(filename_clean="a\\b.pdf"))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None and "path separator" in reason

    def test_rejects_filename_dot_dot(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        am = am_cls(**_make_attachment(filename_clean=".."))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None and "traversal" in reason

    def test_rejects_filename_single_dot(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        am = am_cls(**_make_attachment(filename_clean="."))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None and "traversal" in reason

    def test_rejects_filename_with_rtl_override(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        # U+202E: RIGHT-TO-LEFT OVERRIDE.  Classic
        # "invoice\u202Efdp.exe" -> visually reads "invoiceexe.pdf".
        am = am_cls(**_make_attachment(
            filename_clean="invoice\u202Efdp.exe",
        ))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None and "bidirectional" in reason

    def test_rejects_size_bytes_negative(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        am = am_cls(**_make_attachment(size_bytes=-1))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None and "negative" in reason

    def test_rejects_size_bytes_over_100mib(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        am = am_cls(**_make_attachment(size_bytes=200 * 1024 * 1024))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None and "100 MiB" in reason

    def test_rejects_size_bytes_non_int(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        # bool is a subclass of int but should also be rejected.
        am = am_cls(**_make_attachment(size_bytes=True))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None and "not an int" in reason

    def test_rejects_malformed_mime_type(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        # No subtype.
        am = am_cls(**_make_attachment(claimed_mime_type="application"))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None and "type/subtype" in reason

    def test_rejects_mime_with_extra_slash(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        am = am_cls(**_make_attachment(claimed_mime_type="app/x/y/z"))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None and "type/subtype" in reason

    def test_rejects_malformed_attachment_id(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        # Too short.
        am = am_cls(**_make_attachment(attachment_id="abc"))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None and "attachment_id" in reason

    def test_rejects_attachment_id_with_equals(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        # base64-url-safe forbids '='; we accept only [a-zA-Z0-9_-].
        am = am_cls(**_make_attachment(attachment_id="abc==def123"))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None and "attachment_id" in reason

    def test_rejects_non_bool_is_inline(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        am = am_cls(**_make_attachment(is_inline="yes"))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None and "is_inline" in reason

    def test_rejects_schema_version_mismatch(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        am = am_cls(**_make_attachment(schema_version="9.9"))
        reason = mod._check_attachment_metadata(am)
        assert reason is not None and "schema_version" in reason

    def test_rejects_duplicate_attachment_ids_in_list(
        self, phase_3b_pkg: Path, fresh_registry,
    ):
        mod, am_cls = self._load_helpers(phase_3b_pkg, fresh_registry)
        am1 = am_cls(**_make_attachment(attachment_id="dup12345"))
        am2 = am_cls(**_make_attachment(
            attachment_id="dup12345", filename_clean="other.pdf",
        ))
        reason = mod._check_attachments_list((am1, am2))
        assert reason is not None and "duplicate" in reason


class TestPhase3bExtractIntegration:
    """End-to-end tests through the four affected JUDGE handlers."""

    def _load(self, phase_3b_pkg: Path, fresh_registry):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(phase_3b_pkg / "manifest.yaml")
        load_package_artifacts(m)

    def test_simple_text_judge_approves_zero_attachments(
        self, phase_3b_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        self._load(phase_3b_pkg, fresh_registry)
        handler = fresh_registry.lookup_judge("email_read_simple_text")
        ext = _make_simple_extract(fresh_registry)
        result = handler(ext)
        assert result.approved is True, result.reason

    def test_simple_text_judge_approves_one_valid_attachment(
        self, phase_3b_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        self._load(phase_3b_pkg, fresh_registry)
        am_cls = fresh_registry.lookup_kind("AttachmentMetadata")
        am = am_cls(**_make_attachment())
        handler = fresh_registry.lookup_judge("email_read_simple_text")
        ext = _make_simple_extract(fresh_registry, attachments=(am,))
        result = handler(ext)
        assert result.approved is True, result.reason
        assert len(ext.attachments) == 1
        assert ext.attachments[0].filename_clean == "report.pdf"

    def test_simple_text_judge_rejects_one_invalid_attachment(
        self, phase_3b_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        """One bad attachment rolls up to whole-extract rejection."""
        self._load(phase_3b_pkg, fresh_registry)
        am_cls = fresh_registry.lookup_kind("AttachmentMetadata")
        bad = am_cls(**_make_attachment(filename_clean="a/b.pdf"))
        handler = fresh_registry.lookup_judge("email_read_simple_text")
        ext = _make_simple_extract(fresh_registry, attachments=(bad,))
        result = handler(ext)
        assert result.approved is False
        assert "attachments[0]" in result.reason

    def test_simple_text_judge_rejects_33_attachments(
        self, phase_3b_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        self._load(phase_3b_pkg, fresh_registry)
        am_cls = fresh_registry.lookup_kind("AttachmentMetadata")
        atts = tuple(
            am_cls(**_make_attachment(attachment_id=f"id_{i:05d}_aa"))
            for i in range(33)
        )
        handler = fresh_registry.lookup_judge("email_read_simple_text")
        ext = _make_simple_extract(fresh_registry, attachments=atts)
        result = handler(ext)
        assert result.approved is False
        assert "33" in result.reason and "max 32" in result.reason

    def test_meeting_invite_judge_approves_with_ics_attachment(
        self, phase_3b_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        self._load(phase_3b_pkg, fresh_registry)
        from carpenter_tools.policy.types import EmailPolicy
        am_cls = fresh_registry.lookup_kind("AttachmentMetadata")
        ics = am_cls(**_make_attachment(
            filename_clean="invite.ics",
            claimed_mime_type="text/calendar",
            attachment_id="ics_id_abc",
            is_inline=False,
        ))
        cls = fresh_registry.lookup_kind("EmailMeetingInviteExtract")
        handler = fresh_registry.lookup_judge("email_read_meeting_invite")
        ext = cls(
            provider_message_id="m2",
            expected_account_email=EmailPolicy("ben@example.com"),
            from_address=EmailPolicy("calendar@meeting.example.com"),
            to_addresses=(EmailPolicy("ben@example.com"),),
            subject="meeting tomorrow",
            received_at="2026-05-06T14:00:00Z",
            start_at="2026-05-07T10:00:00Z",
            end_at="2026-05-07T11:00:00Z",
            location="Zoom",
            organizer=EmailPolicy("calendar@meeting.example.com"),
            body_summary="meet at 10",
            attachments=(ics,),
        )
        result = handler(ext)
        assert result.approved is True, result.reason

    def test_triage_judge_approves_with_mixed_inline_and_attachment(
        self, phase_3b_pkg: Path, fresh_registry, policies_with_test_emails,
    ):
        self._load(phase_3b_pkg, fresh_registry)
        from carpenter_tools.policy.types import EmailPolicy
        am_cls = fresh_registry.lookup_kind("AttachmentMetadata")
        signature_png = am_cls(**_make_attachment(
            filename_clean="signature.png",
            claimed_mime_type="image/png",
            attachment_id="sig_id_xyz",
            is_inline=True,
            size_bytes=4096,
        ))
        invoice_pdf = am_cls(**_make_attachment(
            filename_clean="invoice.pdf",
            claimed_mime_type="application/pdf",
            attachment_id="inv_id_qrs",
            is_inline=False,
            size_bytes=200_000,
        ))
        cls = fresh_registry.lookup_kind("EmailTriageExtract")
        handler = fresh_registry.lookup_judge("email_triage")
        ext = cls(
            provider_message_id="msg12",
            received_history_id="1234567",
            category="transactional",
            from_address=EmailPolicy("vendor@shop.example.com"),
            subject_clean="Order #1234 confirmed",
            importance_flags=(),
            attachments=(signature_png, invoice_pdf),
            schema_version="1.0",
        )
        result = handler(ext)
        assert result.approved is True, result.reason
        # And the is_inline distinction round-trips honestly.
        assert ext.attachments[0].is_inline is True
        assert ext.attachments[1].is_inline is False


# ---------------------------------------------------------------------------
# Phase 4 (semantic resource index, v0.6.0)
# ---------------------------------------------------------------------------
#
# These tests cover:
#   * Manifest shape (triggers, data_models, judge_handlers, KB articles).
#   * The two deterministic JUDGE handlers — ``judge_email_index_fetched_batch``
#     and ``judge_email_index_batch`` — against approve and reject paths.
#   * The three new chat tools (``pkg_gmail_reindex``,
#     ``pkg_gmail_reindex_pause``, ``pkg_gmail_reindex_resume``) require
#     user confirm.
#   * ``pkg_gmail_search_emails``'s vector-or-keyword backend selection.
#
# The trigger lifecycle (cadence, mutex, drain, embed+upsert) is
# package-internal and is exercised in carpenter-packages' own tests
# under a sibling harness; here we only verify the trigger module
# *imports* and the class hierarchy is sound.


def _phase_4_package_path():
    """Locate the carpenter-gmail package at v0.6.0 (or later).

    Resolution order:
      1. ``CARPENTER_PACKAGES_DIR`` env var (for CI).
      2. The sibling carpenter-packages checkout when it carries 0.6.x.

    Historical note: an earlier revision of this helper also probed
    ``/tmp/cc-phase4-pkg/packages/carpenter-gmail`` (the original
    Phase 4 development worktree).  That path was removed because the
    worktree gets stranded with stale code (e.g. carrying the 0.6.0
    PackageStateHandle import bug while the canonical sibling checkout
    is at 0.6.1+).  Set ``CARPENTER_PACKAGES_DIR`` if you need an
    explicit override during development.
    """
    import os
    from pathlib import Path

    env = os.environ.get("CARPENTER_PACKAGES_DIR")
    if env:
        cand = Path(env) / "carpenter-gmail"
        if cand.is_dir():
            return cand

    src = _find_email_package()
    if src is not None:
        try:
            text = (src / "manifest.yaml").read_text()
        except OSError:
            return None
        if 'version: "0.6' in text:
            return src
    return None


@pytest.fixture
def phase_4_pkg_src() -> Path:
    src = _phase_4_package_path()
    if src is None:
        pytest.skip(
            "carpenter-gmail Phase 4 (v0.6.0+) worktree not found; set "
            "CARPENTER_PACKAGES_DIR, create /tmp/cc-phase4-pkg, or "
            "update the sibling carpenter-packages clone",
        )
    return src


@pytest.fixture
def phase_4_pkg(tmp_path: Path, phase_4_pkg_src: Path) -> Path:
    dst = tmp_path / "carpenter-gmail"
    shutil.copytree(phase_4_pkg_src, dst)
    return dst


class TestPhase4ManifestShape:
    """Manifest-level invariants introduced by Phase 4."""

    def test_manifest_version_is_0_7_x(self, phase_4_pkg: Path):
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(phase_4_pkg / "manifest.yaml")
        # 0.6.0 shipped Phase 4; 0.6.1 fixed the PackageStateHandle
        # import bug in tools.py; 0.7.0 renamed carpenter-email ->
        # carpenter-gmail.  Anything in the 0.7.x line is fine.
        assert m.version.startswith("0.7.")

    def test_manifest_declares_three_index_data_models(
        self, phase_4_pkg: Path,
    ):
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(phase_4_pkg / "manifest.yaml")
        for kind in (
            "EmailIndexFetchedEntry",
            "EmailIndexFetchedBatch",
            "EmailIndexBatchReceipt",
        ):
            assert kind in m.data_models, kind

    def test_manifest_declares_three_index_templates(
        self, phase_4_pkg: Path,
    ):
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(phase_4_pkg / "manifest.yaml")
        names = {t.name for t in m.arc_templates}
        for tn in (
            "email_index_phase1",
            "email_index_phase2",
            "email_index_incremental",
        ):
            assert tn in names, tn

    def test_manifest_declares_three_index_triggers(
        self, phase_4_pkg: Path,
    ):
        """Phase 4 ships three PollableTriggers (60s cadence)."""
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(phase_4_pkg / "manifest.yaml")
        trigger_names = {t.name for t in m.triggers}
        trigger_types = {t.type for t in m.triggers}
        # Phase 3a's gmail-inbound-poll plus Phase 4's three indexers
        assert "gmail-inbound-poll" in trigger_names
        for tn in (
            "gmail-index-phase1",
            "gmail-index-phase2",
            "gmail-index-incremental",
        ):
            assert tn in trigger_names, tn
        for tt in (
            "email_index_phase1",
            "email_index_phase2",
            "email_index_incremental",
        ):
            assert tt in trigger_types, tt

    def test_index_triggers_have_60s_cadence(self, phase_4_pkg: Path):
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(phase_4_pkg / "manifest.yaml")
        for t in m.triggers:
            if t.name.startswith("gmail-index-"):
                cadence = (t.config or {}).get("cadence_seconds")
                assert cadence == 60, (
                    f"{t.name}: cadence_seconds={cadence!r} expected 60"
                )

    def test_manifest_declares_index_judge_handlers(
        self, phase_4_pkg: Path,
    ):
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(phase_4_pkg / "manifest.yaml")
        names = {h.name for h in m.judge_handlers}
        assert "judge_email_index_fetched_batch" in names
        assert "judge_email_index_batch" in names

    def test_index_and_search_kb_articles_declared(
        self, phase_4_pkg: Path,
    ):
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(phase_4_pkg / "manifest.yaml")
        slugs = {a.slug for a in m.kb_articles}
        assert "email/index" in slugs
        assert "email/search" in slugs


def _make_index_entry(fresh_registry, **overrides):
    """Build a minimal-but-valid EmailIndexFetchedEntry."""
    cls = fresh_registry.lookup_kind("EmailIndexFetchedEntry")
    base = dict(
        provider_message_id="abc_12345",
        thread_id="thr_12345",
        from_address="alice@example.com",
        from_display_clean="Alice",
        date_iso="2026-05-10T15:30:00+00:00",
        subject_raw="hello",
        gmail_snippet="hi there",
        body_text_or_null="",
        has_attachment=False,
        labels=("INBOX", "UNREAD"),
        schema_version="1.0",
    )
    base.update(overrides)
    return cls(**base)


def _make_index_batch(fresh_registry, entries=(), **overrides):
    cls = fresh_registry.lookup_kind("EmailIndexFetchedBatch")
    base = dict(
        phase="1",
        batch_id="abc_12345",
        watermark_before="",
        watermark_after="9999",
        entries=tuple(entries),
        fetched_count=len(entries),
        skipped_count=0,
        error_kind="",
        schema_version="1.0",
    )
    base.update(overrides)
    return cls(**base)


def _make_index_receipt(fresh_registry, **overrides):
    cls = fresh_registry.lookup_kind("EmailIndexBatchReceipt")
    base = dict(
        phase="1",
        batch_id="abc_12345",
        watermark_before="",
        watermark_after="9999",
        embedded_count=0,
        error_count=0,
        sample_error_message="",
        schema_version="1.0",
    )
    base.update(overrides)
    return cls(**base)


class TestPhase4FetchedBatchJudge:
    """Direct tests of ``judge_email_index_fetched_batch``.

    The handler is registered three times (once per indexer template).
    All three resolve to the SAME callable — we exercise via the
    ``email_index_phase1`` registration.
    """

    def _load(self, phase_4_pkg: Path, fresh_registry):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(phase_4_pkg / "manifest.yaml")
        load_package_artifacts(m)

    def _handler(self, fresh_registry):
        return fresh_registry.lookup_judge("email_index_phase1")

    def test_approves_empty_batch(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        self._load(phase_4_pkg, fresh_registry)
        b = _make_index_batch(fresh_registry)
        r = self._handler(fresh_registry)(b)
        assert r.approved is True, r.reason

    def test_approves_single_valid_entry(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        self._load(phase_4_pkg, fresh_registry)
        e = _make_index_entry(fresh_registry)
        b = _make_index_batch(fresh_registry, entries=(e,))
        r = self._handler(fresh_registry)(b)
        assert r.approved is True, r.reason

    def test_rejects_bad_phase(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        self._load(phase_4_pkg, fresh_registry)
        b = _make_index_batch(fresh_registry, phase="bogus")
        r = self._handler(fresh_registry)(b)
        assert r.approved is False
        assert "phase" in r.reason

    def test_rejects_bad_batch_id(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        self._load(phase_4_pkg, fresh_registry)
        b = _make_index_batch(fresh_registry, batch_id="!!")
        r = self._handler(fresh_registry)(b)
        assert r.approved is False
        assert "batch_id" in r.reason

    def test_rejects_count_invariant_violation(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        """len(entries) + skipped_count must equal fetched_count."""
        self._load(phase_4_pkg, fresh_registry)
        e = _make_index_entry(fresh_registry)
        b = _make_index_batch(
            fresh_registry, entries=(e,), fetched_count=5, skipped_count=0,
        )
        r = self._handler(fresh_registry)(b)
        assert r.approved is False
        assert "count invariant" in r.reason

    def test_rejects_error_kind_with_entries(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        """Pause-marker batches MUST carry empty entries."""
        self._load(phase_4_pkg, fresh_registry)
        e = _make_index_entry(fresh_registry)
        b = _make_index_batch(
            fresh_registry, entries=(e,),
            error_kind="history_expired",
            fetched_count=1,
        )
        r = self._handler(fresh_registry)(b)
        assert r.approved is False
        assert "error_kind" in r.reason and "empty entries" in r.reason

    def test_approves_history_expired_pause_marker(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        """history_expired with empty entries is the recovery path."""
        self._load(phase_4_pkg, fresh_registry)
        b = _make_index_batch(
            fresh_registry, phase="incremental",
            entries=(), fetched_count=0, skipped_count=0,
            error_kind="history_expired",
            watermark_before="9999", watermark_after="9999",
        )
        r = self._handler(fresh_registry)(b)
        assert r.approved is True, r.reason

    def test_approves_model_identity_mismatch_pause_marker(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        self._load(phase_4_pkg, fresh_registry)
        b = _make_index_batch(
            fresh_registry,
            entries=(), fetched_count=0, skipped_count=0,
            error_kind="model_identity_mismatch",
        )
        r = self._handler(fresh_registry)(b)
        assert r.approved is True, r.reason

    def test_rejects_unknown_error_kind(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        self._load(phase_4_pkg, fresh_registry)
        b = _make_index_batch(
            fresh_registry,
            entries=(), fetched_count=0, skipped_count=0,
            error_kind="not_a_real_error",
        )
        r = self._handler(fresh_registry)(b)
        assert r.approved is False
        assert "error_kind" in r.reason

    def test_rejects_bidi_override_in_display_name(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        """U+202E (right-to-left override) MUST be rejected."""
        self._load(phase_4_pkg, fresh_registry)
        e = _make_index_entry(
            fresh_registry, from_display_clean="Alice\u202eEvil",
        )
        b = _make_index_batch(
            fresh_registry, entries=(e,), fetched_count=1,
        )
        r = self._handler(fresh_registry)(b)
        assert r.approved is False
        assert "bidirectional" in r.reason

    def test_rejects_invalid_from_address(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        self._load(phase_4_pkg, fresh_registry)
        e = _make_index_entry(fresh_registry, from_address="not-an-email")
        b = _make_index_batch(
            fresh_registry, entries=(e,), fetched_count=1,
        )
        r = self._handler(fresh_registry)(b)
        assert r.approved is False
        assert "from_address" in r.reason

    def test_rejects_date_out_of_range(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        """date_iso year must lie in [1990, 2100]."""
        self._load(phase_4_pkg, fresh_registry)
        e = _make_index_entry(
            fresh_registry, date_iso="1900-01-01T00:00:00+00:00",
        )
        b = _make_index_batch(
            fresh_registry, entries=(e,), fetched_count=1,
        )
        r = self._handler(fresh_registry)(b)
        assert r.approved is False
        assert "date_iso" in r.reason

    def test_rejects_duplicate_provider_message_id(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        self._load(phase_4_pkg, fresh_registry)
        e1 = _make_index_entry(fresh_registry, provider_message_id="dup_12345")
        e2 = _make_index_entry(fresh_registry, provider_message_id="dup_12345")
        b = _make_index_batch(
            fresh_registry, entries=(e1, e2), fetched_count=2,
        )
        r = self._handler(fresh_registry)(b)
        assert r.approved is False
        assert "duplicate" in r.reason

    def test_rejects_oversize_entries_tuple(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        """Batch cap is EMAIL_INDEX_MAX_BATCH = 100.

        The JUDGE may reject on any of three closely-related rules
        (``fetched_count`` exceeds cap, ``entries`` len exceeds cap,
        or the count-invariant); whichever fires first is fine — we
        just verify the cap is enforced.
        """
        self._load(phase_4_pkg, fresh_registry)
        es = tuple(
            _make_index_entry(
                fresh_registry,
                provider_message_id=f"id_{i:05d}",
            )
            for i in range(101)
        )
        b = _make_index_batch(
            fresh_registry, entries=es, fetched_count=101,
        )
        r = self._handler(fresh_registry)(b)
        assert r.approved is False
        assert (
            "cap 100" in r.reason
            or "max 100" in r.reason
            or "exceeds" in r.reason
        ), r.reason


class TestPhase4ReceiptJudge:
    """Direct tests of ``judge_email_index_batch`` (the post-embed
    audit receipt JUDGE).

    Receipt is constructed in trusted post-JUDGE context by the
    trigger; this JUDGE is not bound to any arc template, only
    declared in ``manifest.judge_handlers`` so it can be discovered
    and exercised by package-internal harness code.  We reach it via
    the package's ``judges`` module directly.
    """

    def _load(self, phase_4_pkg: Path, fresh_registry):
        from carpenter.packages.loaders import (
            _import_package_module,
            load_package_artifacts,
        )
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(phase_4_pkg / "manifest.yaml")
        load_package_artifacts(m)
        # The package's data_models / judges modules are import-cached
        # under namespaced keys (``carpenter_packages.carpenter_gmail.*``)
        # so this resolves the SAME callable that the trigger uses.
        judges_mod = _import_package_module(
            "carpenter-gmail", "judges", phase_4_pkg,
        )
        return judges_mod.judge_email_index_batch

    def test_approves_empty_receipt(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        handler = self._load(phase_4_pkg, fresh_registry)
        assert handler is not None, "receipt JUDGE handler not registered"
        r = handler(_make_index_receipt(fresh_registry))
        assert r.approved is True, r.reason

    def test_approves_normal_receipt(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        handler = self._load(phase_4_pkg, fresh_registry)
        r = handler(_make_index_receipt(
            fresh_registry, embedded_count=15, error_count=0,
        ))
        assert r.approved is True, r.reason

    def test_approves_receipt_with_error_and_sample(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        handler = self._load(phase_4_pkg, fresh_registry)
        r = handler(_make_index_receipt(
            fresh_registry,
            embedded_count=10, error_count=2,
            sample_error_message="upstream embed failed",
        ))
        assert r.approved is True, r.reason

    def test_rejects_sample_error_with_zero_errors(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        """sample_error_message must be empty when error_count == 0."""
        handler = self._load(phase_4_pkg, fresh_registry)
        r = handler(_make_index_receipt(
            fresh_registry, embedded_count=5, error_count=0,
            sample_error_message="ghost error",
        ))
        assert r.approved is False
        assert "error_count is 0" in r.reason

    def test_rejects_total_over_batch_cap(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        handler = self._load(phase_4_pkg, fresh_registry)
        # embedded + error > 100
        r = handler(_make_index_receipt(
            fresh_registry, embedded_count=90, error_count=15,
        ))
        assert r.approved is False
        assert "cap" in r.reason or "exceeds" in r.reason

    def test_rejects_sample_error_with_control_chars(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        handler = self._load(phase_4_pkg, fresh_registry)
        r = handler(_make_index_receipt(
            fresh_registry, embedded_count=1, error_count=1,
            sample_error_message="oops\x00",
        ))
        assert r.approved is False
        assert "control" in r.reason.lower()

    def test_rejects_bad_phase(
        self, phase_4_pkg: Path, fresh_registry,
    ):
        handler = self._load(phase_4_pkg, fresh_registry)
        r = handler(_make_index_receipt(fresh_registry, phase="99"))
        assert r.approved is False
        assert "phase" in r.reason


class TestPhase4ChatTools:
    """Phase 4 surfaces three new chat tools and updates
    ``pkg_gmail_search_emails`` to a vector-or-keyword backend.  Each
    is verified to register and to declare ``requires_user_confirm``
    where expected.
    """

    def _import_tools(self, phase_4_pkg: Path):
        from carpenter.packages.loaders import _import_package_module

        _import_package_module("carpenter-gmail", "data_models", phase_4_pkg)
        _import_package_module("carpenter-gmail", "scripts", phase_4_pkg)
        return _import_package_module(
            "carpenter-gmail", "tools", phase_4_pkg,
        )

    def test_reindex_tools_registered(self, phase_4_pkg: Path):
        module = self._import_tools(phase_4_pkg)
        for name in (
            "pkg_gmail_reindex",
            "pkg_gmail_reindex_pause",
            "pkg_gmail_reindex_resume",
        ):
            fn = getattr(module, name, None)
            assert fn is not None, f"{name} not exported"
            meta = getattr(fn, "_chat_tool_meta", None)
            assert meta is not None, f"{name} missing @chat_tool meta"

    def test_reindex_tools_require_user_confirm(self, phase_4_pkg: Path):
        """All three reindex tools mutate user-visible state and MUST
        gate at the chat boundary."""
        module = self._import_tools(phase_4_pkg)
        for name in (
            "pkg_gmail_reindex",
            "pkg_gmail_reindex_pause",
            "pkg_gmail_reindex_resume",
        ):
            fn = getattr(module, name)
            meta = fn._chat_tool_meta
            assert meta["requires_user_confirm"] is True, name

    def test_search_emails_has_backend_param(self, phase_4_pkg: Path):
        """The updated ``pkg_gmail_search_emails`` accepts a
        ``backend`` parameter in its input schema."""
        module = self._import_tools(phase_4_pkg)
        fn = module.pkg_gmail_search_emails
        meta = fn._chat_tool_meta
        schema = meta["input_schema"]
        props = schema.get("properties", {})
        assert "backend" in props, (
            "pkg_gmail_search_emails missing 'backend' input"
        )
        backend = props["backend"]
        assert backend.get("enum") == ["auto", "vector", "keyword"]
        assert backend.get("default") == "auto"


class TestPhase4Triggers:
    """The three new triggers are importable, declare correct
    ``trigger_type()``, and share the package-internal mutex via
    :class:`IndexTriggerBase`."""

    def _import_trigger_module(self, phase_4_pkg: Path, mod_name: str):
        from carpenter.packages.loaders import _import_package_module

        # Side-load the shared base first so the subclass imports
        # resolve correctly.
        _import_package_module(
            "carpenter-gmail", "triggers._index_common", phase_4_pkg,
        )
        return _import_package_module(
            "carpenter-gmail", f"triggers.{mod_name}", phase_4_pkg,
        )

    def test_phase1_trigger_imports_and_declares_type(
        self, phase_4_pkg: Path,
    ):
        mod = self._import_trigger_module(
            phase_4_pkg, "email_index_phase1",
        )
        cls = mod.EmailIndexPhase1Trigger
        assert cls.trigger_type() == "email_index_phase1"
        assert cls.phase == "1"

    def test_phase2_trigger_imports_and_declares_type(
        self, phase_4_pkg: Path,
    ):
        mod = self._import_trigger_module(
            phase_4_pkg, "email_index_phase2",
        )
        cls = mod.EmailIndexPhase2Trigger
        assert cls.trigger_type() == "email_index_phase2"
        assert cls.phase == "2"

    def test_incremental_trigger_imports_and_declares_type(
        self, phase_4_pkg: Path,
    ):
        mod = self._import_trigger_module(
            phase_4_pkg, "email_index_incremental",
        )
        cls = mod.EmailIndexIncrementalTrigger
        assert cls.trigger_type() == "email_index_incremental"
        assert cls.phase == "incremental"

    def test_all_three_triggers_share_index_common_base(
        self, phase_4_pkg: Path,
    ):
        common = self._import_trigger_module(
            phase_4_pkg, "_index_common",
        )
        base = common.IndexTriggerBase
        for mod_name, cls_attr in (
            ("email_index_phase1", "EmailIndexPhase1Trigger"),
            ("email_index_phase2", "EmailIndexPhase2Trigger"),
            ("email_index_incremental", "EmailIndexIncrementalTrigger"),
        ):
            mod = self._import_trigger_module(phase_4_pkg, mod_name)
            cls = getattr(mod, cls_attr)
            assert issubclass(cls, base), (
                f"{cls_attr} does not inherit from IndexTriggerBase"
            )

    def test_phase2_seed_no_op_when_no_candidates(
        self, phase_4_pkg: Path,
    ):
        """Phase 2 must return ``None`` from ``build_executor_seed``
        when there are no candidate message ids — that's the signal
        the base class uses to skip the tick."""
        from unittest.mock import MagicMock

        mod = self._import_trigger_module(
            phase_4_pkg, "email_index_phase2",
        )
        cls = mod.EmailIndexPhase2Trigger
        state = MagicMock()
        state.get.return_value = None  # no candidates
        # Direct-construct the trigger with stub package_state.
        trigger = cls.__new__(cls)
        trigger.package_state = state
        trigger.max_batch = 50
        seed = trigger.build_executor_seed(watermark_before="")
        assert seed is None


# ---------------------------------------------------------------------------
# Phase 4 PackageStateHandle wiring (regression tests for the
# get_package_state_handle ImportError bug shipped in 0.6.0).
#
# Phase 4 PR-A shipped four call sites in tools.py that imported a
# non-existent ``get_package_state_handle`` factory.  The import was
# wrapped in ``try/except ImportError`` so the failure was silent:
#   * ``_index_status_snapshot``     -> always returned the zero
#                                       default, so auto-routing in
#                                       ``pkg_gmail_search_emails``
#                                       NEVER picked vector.
#   * ``pkg_gmail_reindex``          -> always returned
#                                       ``{"error": "package.state /
#                                       package.vectors unavailable"}``.
#   * ``pkg_gmail_reindex_pause``    -> same.
#   * ``pkg_gmail_reindex_resume``   -> same.
#
# These tests exercise the actual wiring end-to-end so the original
# bug cannot recur.
# ---------------------------------------------------------------------------


def _seed_installed_package(name: str = "carpenter-gmail") -> None:
    """Insert a minimal ``installed_packages`` row so the FK on
    ``package_state.package_name`` and ``package_vectors.package_name``
    is satisfied for the duration of the test.

    Also calls ``ensure_installer_tables`` first because the
    session-template DB used by the autouse ``test_db`` fixture does
    not necessarily run the installer migrations.
    """
    from carpenter.db import get_db
    from carpenter.packages.installer import ensure_installer_tables

    conn = get_db()
    try:
        ensure_installer_tables(conn)
        conn.execute(
            "INSERT OR IGNORE INTO installed_packages "
            "(name, version, hash, source_path, install_path, installed_at) "
            "VALUES (?, '0.7.0', 'testhash', '/tmp/s', '/tmp/d', "
            "'2026-05-21T00:00:00Z')",
            (name,),
        )
        conn.commit()
    finally:
        conn.close()


class TestPhase4StateHandleWiring:
    """Regression tests for the PackageStateHandle import bug in
    carpenter-gmail 0.6.0 tools.py.

    Each test loads the package's tools module from disk (so we exercise
    the real import path, not a re-export), seeds an
    ``installed_packages`` row so the FK constraints on
    ``package_state`` and ``package_vectors`` are satisfied, and then
    invokes the bug-affected tool to confirm it succeeds.
    """

    def _import_tools(self, phase_4_pkg: Path):
        from carpenter.packages.loaders import _import_package_module

        _import_package_module(
            "carpenter-gmail", "data_models", phase_4_pkg,
        )
        _import_package_module("carpenter-gmail", "scripts", phase_4_pkg)
        return _import_package_module(
            "carpenter-gmail", "tools", phase_4_pkg,
        )

    def test_index_status_snapshot_returns_real_count(
        self, phase_4_pkg: Path,
    ):
        """``_index_status_snapshot`` must return a real
        ``vector_count`` derived from the vector store, NOT the zero
        fallback that ``except ImportError`` produced before the fix.
        """
        from carpenter.packages.vectors import PackageVectorStore

        _seed_installed_package()
        module = self._import_tools(phase_4_pkg)

        # Write three vectors under the package's namespace.
        store = PackageVectorStore("carpenter-gmail")
        mid = "local:test-model:4"
        for i, vid in enumerate(("m-1", "m-2", "m-3")):
            store.upsert(vid, [1.0, 0.0, 0.0, float(i)], mid)

        snap = module._index_status_snapshot()
        assert snap["vector_count"] == 3, snap
        # Phase 1 has NOT been marked complete in package_state, so:
        assert snap["phase1_complete"] is False
        assert snap["incremental_ready"] is False
        assert snap["paused"] is False

    def test_pkg_gmail_reindex_clears_vectors(self, phase_4_pkg: Path):
        """``pkg_gmail_reindex`` must clear vectors and wipe indexer
        watermarks.  Before the fix it short-circuited with
        ``{"error": "package.state / package.vectors unavailable"}``
        because the import inside the ``try/except ImportError`` raised
        on the missing ``get_package_state_handle`` factory.
        """
        from carpenter.packages import state as pkg_state_mod
        from carpenter.packages.state import PackageStateHandle
        from carpenter.packages.vectors import PackageVectorStore

        _seed_installed_package()
        module = self._import_tools(phase_4_pkg)

        # Seed both vectors and a couple of watermarks.
        store = PackageVectorStore("carpenter-gmail")
        mid = "local:test-model:4"
        for vid in ("m-1", "m-2"):
            store.upsert(vid, [1.0, 0.0, 0.0, 0.0], mid)
        handle = PackageStateHandle("carpenter-gmail")
        handle.set("index_phase1_watermark", "abc123")
        handle.set("index_phase1_completed_at", "2026-05-21T00:00:00Z")
        assert store.count() == 2
        assert pkg_state_mod.get(
            "carpenter-gmail", "index_phase1_watermark",
        ) == "abc123"

        result = json.loads(module.pkg_gmail_reindex({
            "reason": "regression test",
        }))
        assert "error" not in result, result
        assert result["ok"] is True
        assert result["vectors_cleared"] == 2

        # Vectors actually cleared.
        assert PackageVectorStore("carpenter-gmail").count() == 0
        # Watermarks gone.
        assert pkg_state_mod.get(
            "carpenter-gmail", "index_phase1_watermark",
        ) is None
        assert pkg_state_mod.get(
            "carpenter-gmail", "index_phase1_completed_at",
        ) is None
        # Audit trail written.
        last = pkg_state_mod.get(
            "carpenter-gmail", "index_last_reindex",
        )
        assert last is not None
        last_decoded = json.loads(last)
        assert last_decoded["reason"] == "regression test"
        assert last_decoded["vectors_cleared"] == 2

    def test_pkg_gmail_reindex_pause_and_resume_round_trip(
        self, phase_4_pkg: Path,
    ):
        """``pkg_gmail_reindex_pause`` must set ``index_paused`` and
        ``pkg_gmail_reindex_resume`` must delete it.  Before the fix
        both tools short-circuited with
        ``{"error": "package.state unavailable"}``.
        """
        from carpenter.packages import state as pkg_state_mod

        _seed_installed_package()
        module = self._import_tools(phase_4_pkg)

        pause = json.loads(module.pkg_gmail_reindex_pause({
            "reason": "user requested",
        }))
        assert "error" not in pause, pause
        assert pause["ok"] is True
        paused_raw = pkg_state_mod.get("carpenter-gmail", "index_paused")
        assert paused_raw is not None
        paused = json.loads(paused_raw)
        assert paused["paused"] is True
        assert paused["reason"] == "user requested"

        resume = json.loads(module.pkg_gmail_reindex_resume({}))
        assert "error" not in resume, resume
        assert resume["ok"] is True
        assert pkg_state_mod.get(
            "carpenter-gmail", "index_paused",
        ) is None

    def test_pkg_gmail_search_emails_auto_routes_to_vector_when_index_ready(
        self, phase_4_pkg: Path, monkeypatch,
    ):
        """``pkg_gmail_search_emails`` with ``backend="auto"`` (the
        default) must route to vector once
        ``_index_status_snapshot`` reports
        ``vector_count > 0`` AND ``phase1_complete is True``.  Before
        the fix the snapshot always returned the zero default and the
        auto path NEVER picked vector — so the s058 acceptance only
        passed because it explicitly passed ``backend="vector"``.

        We patch ``_vector_search`` to return a deterministic stub so
        the test does NOT depend on a real embedding service.
        """
        from carpenter.packages.state import PackageStateHandle
        from carpenter.packages.vectors import PackageVectorStore

        _seed_email_config(monkeypatch)
        _seed_installed_package()
        module = self._import_tools(phase_4_pkg)

        # Seed one real vector so ``vector_count > 0``.
        PackageVectorStore("carpenter-gmail").upsert(
            "m-1", [1.0, 0.0, 0.0, 0.0], "local:test-model:4",
        )
        # Mark Phase 1 complete so auto-routing's second precondition
        # is satisfied.
        PackageStateHandle("carpenter-gmail").set(
            "index_phase1_completed_at", "2026-05-21T00:00:00Z",
        )

        # Stub the embedding-backed vector search so we don't need the
        # embedding service in test context.
        stub_hits = [{
            "provider_message_id": "m-1",
            "score": 0.99,
            "metadata": {},
        }]
        monkeypatch.setattr(
            module, "_vector_search", lambda q, n: list(stub_hits),
        )

        # No ``backend`` argument: must auto-route to vector now that
        # both preconditions are real (not the zero-default fallback).
        result = json.loads(module.pkg_gmail_search_emails({
            "query": "natural language question",
        }))
        assert result.get("backend") == "vector", result
        # Sanity: the status snapshot in the response is the live one,
        # not the zero default.
        idx = result.get("index_status", {})
        assert idx.get("vector_count", 0) > 0
        assert idx.get("phase1_complete") is True

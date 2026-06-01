"""Phase 3a tests for the carpenter-gmail inbound-triage pipeline.

Covers (PR-C):

* Manifest declares the ``email_triage`` arc template, the
  ``EmailTriageExtract`` data model, the ``judge_email_triage`` JUDGE,
  one ``gmail-inbound-poll`` trigger, and one ``email.received``
  trigger subscription.  Manifest version bumped to 0.4.0.
* ``EmailTriageExtract`` registers via ``load_data_models`` and the
  ``judge_email_triage`` handler approves/rejects the extract
  according to the closed-enum / shape rules in
  :mod:`carpenter_gmail.judges`.
* The :class:`GmailPollTrigger` first-run init stores the
  ``historyId`` watermark and emits nothing; subsequent polls call
  ``users.history.list``, emit one ``email.received`` event per
  newly-arrived message, and CAS-advance the watermark.  The 401 path
  emits ``email.auth_revoked`` once and disables in-process; the 429
  path stores a backoff timestamp; the concurrent-poll guard skips
  re-entry; the cadence guard suppresses too-frequent polls; the
  per-poll emit cap is enforced.
* The manifest's ``trigger_subscriptions`` entry round-trips through
  the loader: an emitted event tagged with the package's
  ``_source_package`` reaches the dispatch action while a forged
  event from a different package is ignored (I9 isolation invariant).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


# ── Fixture: locate a carpenter-gmail package checkout that contains
# the Phase 3a triggers/ directory.
def _phase_3a_package_path() -> Path | None:
    """Resolve to a worktree or on-disk checkout shipped with PR-C+."""
    env = os.environ.get("CARPENTER_PACKAGES_DIR")
    if env:
        cand = Path(env) / "carpenter-gmail"
        if cand.is_dir() and (cand / "triggers" / "gmail_poll.py").is_file():
            return cand
    # Canonical on-disk clone next to carpenter-core (post-merge).
    # Set CARPENTER_PACKAGES_DIR to override (e.g. for an in-flight
    # worktree); previously this fixture hard-coded /tmp/cc-3*-pkg
    # candidate paths, but those go stale and silently pin tests to
    # outdated manifest versions.
    candidates = [
        Path.home() / "repos" / "carpenter-packages"
            / "packages" / "carpenter-gmail",
    ]
    for cand in candidates:
        if cand.is_dir() and (cand / "triggers" / "gmail_poll.py").is_file():
            return cand
    return None


@pytest.fixture
def triage_pkg_src() -> Path:
    src = _phase_3a_package_path()
    if src is None:
        pytest.skip(
            "carpenter-gmail Phase 3a worktree not found; set "
            "CARPENTER_PACKAGES_DIR to the carpenter-packages dir",
        )
    return src


@pytest.fixture
def triage_pkg(tmp_path: Path, triage_pkg_src: Path) -> Path:
    dst = tmp_path / "carpenter-gmail"
    shutil.copytree(triage_pkg_src, dst)
    return dst


@pytest.fixture
def fresh_registry():
    from carpenter.packages.handler_registry import get_handler_registry

    reg = get_handler_registry()
    reg.reset()
    yield reg
    reg.reset()


@pytest.fixture
def email_allowlist():
    """Pre-populate the SecurityPolicies email allowlist for JUDGE tests."""
    from carpenter.security import get_policies

    pol = get_policies()
    saved = pol.get_allowlist("email")
    for addr in (
        "ben@example.com",
        "alice@example.com",
        "newsletter@news.example.com",
    ):
        pol.add("email", addr)
    yield pol
    pol.clear("email")
    for addr in saved:
        pol.add("email", addr)


# =====================================================================
# Manifest
# =====================================================================


class TestTriageManifest:
    def test_manifest_version_at_least_0_5_0(self, triage_pkg: Path):
        from carpenter.packages.manifest import load_manifest

        # Phase 3a bumped to 0.4.0; Phase 3b (attachment metadata) bumps
        # to 0.5.0; Phase 4 (semantic resource index) bumps to 0.6.0;
        # 0.7.0 renames the package carpenter-email -> carpenter-gmail.
        # The triage shape itself is unchanged in 3b/4/0.7.0 except for
        # the Phase 3b ``attachments`` field on EmailTriageExtract.
        m = load_manifest(triage_pkg / "manifest.yaml")
        assert m.version in {"0.5.0", "0.6.0", "0.7.0"}, m.version

    def test_manifest_declares_email_triage_template(self, triage_pkg: Path):
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(triage_pkg / "manifest.yaml")
        names = {t.name for t in m.arc_templates}
        assert "email_triage" in names
        triage = next(t for t in m.arc_templates if t.name == "email_triage")
        assert triage.extract_kind == "EmailTriageExtract"
        assert triage.judge_handler == "judges:judge_email_triage"

    def test_manifest_declares_triage_extract_model(self, triage_pkg: Path):
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(triage_pkg / "manifest.yaml")
        assert "EmailTriageExtract" in m.data_models

    def test_manifest_declares_gmail_poll_trigger(self, triage_pkg: Path):
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(triage_pkg / "manifest.yaml")
        # Phase 3a shipped one trigger (``gmail-inbound-poll``).
        # Phase 4 added three semantic-index triggers
        # (``gmail-index-phase1/phase2/incremental``); the inbound
        # poll's shape is unchanged.
        triggers_by_name = {t.name: t for t in m.triggers}
        assert "gmail-inbound-poll" in triggers_by_name
        trig = triggers_by_name["gmail-inbound-poll"]
        assert trig.type == "gmail_poll"
        assert trig.module == "triggers/gmail_poll.py"
        assert trig.enabled is True
        assert int(trig.config.get("cadence_seconds")) == 900
        assert trig.config.get("event_type") == "email.received"

    def test_manifest_declares_email_received_subscription(
        self, triage_pkg: Path,
    ):
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(triage_pkg / "manifest.yaml")
        assert len(m.trigger_subscriptions) == 1
        sub = m.trigger_subscriptions[0]
        assert sub.event == "email.received"
        assert sub.handler == "handlers.triage_inbound:handle_email_received"

    def test_manifest_declares_inbound_triage_kb_article(
        self, triage_pkg: Path,
    ):
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(triage_pkg / "manifest.yaml")
        slugs = {kb.slug for kb in m.kb_articles}
        assert "email/inbound-triage" in slugs


# =====================================================================
# Data model + JUDGE
# =====================================================================


def _make_triage_extract(
    fresh_registry, *, from_addr: str = "alice@example.com", **overrides,
):
    """Build a minimal-but-valid EmailTriageExtract for JUDGE tests."""
    from carpenter_tools.policy.types import EmailPolicy

    cls = fresh_registry.lookup_kind("EmailTriageExtract")
    base = dict(
        provider_message_id="msg12",
        received_history_id="1234567",
        category="personal",
        from_address=EmailPolicy(from_addr),
        subject_clean="hello there",
        importance_flags=("personal",),
        schema_version="1.0",
    )
    base.update(overrides)
    return cls(**base)


class TestTriageDataModel:
    def test_triage_extract_registers(
        self, triage_pkg: Path, fresh_registry,
    ):
        from carpenter.packages.loaders import load_data_models
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(triage_pkg / "manifest.yaml")
        n, errors = load_data_models(m)
        assert errors == []
        # 8 from earlier phases + EmailTriageExtract == 9; Phase 3b
        # added AttachmentMetadata == 10; Phase 4 added three index
        # dataclasses == 13.  We accept either the 3b or 4 shape so
        # this test passes against both versions while the package
        # is mid-rollout.
        assert n in {10, 13}, n
        cls = fresh_registry.lookup_kind("EmailTriageExtract")
        assert cls is not None

    def test_triage_extract_is_frozen(
        self, triage_pkg: Path, fresh_registry, email_allowlist,
    ):
        from carpenter.packages.loaders import load_data_models
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(triage_pkg / "manifest.yaml")
        load_data_models(m)
        ext = _make_triage_extract(fresh_registry)
        # frozen=True dataclasses raise FrozenInstanceError on assignment.
        with pytest.raises(Exception):
            ext.category = "promotional"  # type: ignore[misc]


class TestTriageJudge:
    def test_judge_approves_minimal(
        self, triage_pkg: Path, fresh_registry, email_allowlist,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(triage_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_triage")
        ext = _make_triage_extract(fresh_registry)
        result = handler(ext)
        assert result.approved is True, result.reason

    def test_judge_rejects_unknown_category(
        self, triage_pkg: Path, fresh_registry, email_allowlist,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(triage_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_triage")
        ext = _make_triage_extract(fresh_registry, category="spam_unspecified")
        result = handler(ext)
        assert result.approved is False
        assert "category" in result.reason

    def test_judge_rejects_control_char_subject(
        self, triage_pkg: Path, fresh_registry, email_allowlist,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(triage_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_triage")
        ext = _make_triage_extract(
            fresh_registry, subject_clean="hi\x07there",
        )
        result = handler(ext)
        assert result.approved is False
        assert "control" in result.reason.lower()

    def test_judge_rejects_url_in_subject(
        self, triage_pkg: Path, fresh_registry, email_allowlist,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(triage_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_triage")
        ext = _make_triage_extract(
            fresh_registry,
            subject_clean="Click https://evil.example.com/login now",
        )
        result = handler(ext)
        assert result.approved is False
        assert "URL" in result.reason or "url" in result.reason.lower()

    def test_judge_rejects_too_many_flags(
        self, triage_pkg: Path, fresh_registry, email_allowlist,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(triage_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_triage")
        # All 6 enum entries plus duplicates pushes past max=8.
        too_many = (
            "personal", "newsletter", "promotional", "automated",
            "high_priority", "suspicious_keyword", "personal",
            "newsletter", "promotional",
        )
        ext = _make_triage_extract(fresh_registry, importance_flags=too_many)
        result = handler(ext)
        assert result.approved is False
        assert "max" in result.reason.lower() or "8" in result.reason

    def test_judge_rejects_unknown_flag(
        self, triage_pkg: Path, fresh_registry, email_allowlist,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(triage_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_triage")
        ext = _make_triage_extract(
            fresh_registry, importance_flags=("totally_bogus_flag",),
        )
        result = handler(ext)
        assert result.approved is False
        assert "closed enum" in result.reason or "totally_bogus_flag" in result.reason

    def test_judge_rejects_bad_history_id_shape(
        self, triage_pkg: Path, fresh_registry, email_allowlist,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(triage_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_triage")
        # history ids are decimal strings; reject letters / slashes.
        ext = _make_triage_extract(
            fresh_registry, received_history_id="abc/123",
        )
        result = handler(ext)
        assert result.approved is False
        assert "received_history_id" in result.reason

    def test_judge_rejects_bad_schema_version(
        self, triage_pkg: Path, fresh_registry, email_allowlist,
    ):
        from carpenter.packages.loaders import load_package_artifacts
        from carpenter.packages.manifest import load_manifest

        m = load_manifest(triage_pkg / "manifest.yaml")
        load_package_artifacts(m)
        handler = fresh_registry.lookup_judge("email_triage")
        ext = _make_triage_extract(fresh_registry, schema_version="9.9")
        result = handler(ext)
        assert result.approved is False
        assert "schema_version" in result.reason


# =====================================================================
# GmailPollTrigger
# =====================================================================


def _load_trigger_module(triage_pkg: Path):
    """Import the trigger module from the worktree as a fresh module."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_test_gmail_poll_trigger",
        triage_pkg / "triggers" / "gmail_poll.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_packages_table(name: str) -> None:
    """Insert a minimal installed_packages row so the FK is satisfied."""
    from carpenter.db import db_transaction
    from carpenter.packages.installer import ensure_installer_tables

    with db_transaction() as db:
        ensure_installer_tables(db)
    with db_transaction() as db:
        db.execute(
            "INSERT OR IGNORE INTO installed_packages "
            "(name, version, hash, source_path, install_path, installed_at) "
            "VALUES (?, '0.5.0', 'abc', '/tmp/s', '/tmp/d', "
            "'2026-05-20T00:00:00Z')",
            (name,),
        )


def _fake_urlopen_factory(
    responses: list[Any],
    captured_urls: list[str],
):
    """Build a fake urlopen that returns scripted responses in order.

    Each entry in ``responses`` is one of:

      * a ``dict`` — returned as a JSON-encoded HTTP 200 body.
      * an ``urllib.error.HTTPError`` instance — re-raised.
      * any other ``Exception`` instance — re-raised verbatim.

    Captured request URLs land in ``captured_urls`` for assertions.
    """
    import io

    class _FakeResponse:
        def __init__(self, body: bytes):
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    iter_resp = iter(responses)

    def _fake_urlopen(req, timeout=None):
        # urllib.request.Request: .full_url; bare-string URL: pass-through.
        url = getattr(req, "full_url", None) or req
        captured_urls.append(url)
        try:
            nxt = next(iter_resp)
        except StopIteration as exc:
            raise AssertionError(
                f"fake urlopen exhausted; got extra call to {url}",
            ) from exc
        if isinstance(nxt, Exception):
            raise nxt
        body = json.dumps(nxt).encode("utf-8")
        return _FakeResponse(body)

    return _fake_urlopen


class TestGmailPollTrigger:
    """Behaviour tests for the in-process GmailPollTrigger.

    Each test seeds a fresh installed_packages row and a
    PackageStateHandle bound to the same package.  Network calls are
    mocked at the ``urllib.request.urlopen`` level so the trigger's
    body runs end-to-end without hitting the real Gmail API.
    """

    def test_trigger_type_is_gmail_poll(self, triage_pkg: Path):
        gp = _load_trigger_module(triage_pkg)
        assert gp.GmailPollTrigger.trigger_type() == "gmail_poll"

    def test_cadence_below_floor_is_clamped(self, triage_pkg: Path):
        gp = _load_trigger_module(triage_pkg)
        trig = gp.GmailPollTrigger(
            "p", {"cadence_seconds": 5}, source_package="carpenter-gmail",
            package_state=None,
        )
        assert trig.cadence_seconds >= 60

    def test_first_run_init_stores_history_id_from_profile(
        self, triage_pkg: Path, monkeypatch,
    ):
        gp = _load_trigger_module(triage_pkg)
        from carpenter.packages.state import PackageStateHandle

        _seed_packages_table("carpenter-gmail")
        handle = PackageStateHandle("carpenter-gmail")
        monkeypatch.setenv("GMAIL_OAUTH_ACCESS_TOKEN", "tok")

        urls: list[str] = []
        responses: list[Any] = [
            {"historyId": "42", "emailAddress": "ben@example.com"},
        ]
        with patch.object(
            gp.urllib.request, "urlopen",
            _fake_urlopen_factory(responses, urls),
        ):
            trig = gp.GmailPollTrigger(
                "p", {"cadence_seconds": 900},
                source_package="carpenter-gmail", package_state=handle,
            )
            trig.start()
        assert handle.get("history_id") == "42"
        assert handle.get("gmail_account_email") == "ben@example.com"
        # Exactly one HTTP call — the getProfile.
        assert len(urls) == 1
        assert "profile" in urls[0]

    def test_start_without_token_defers_init(
        self, triage_pkg: Path, monkeypatch,
    ):
        gp = _load_trigger_module(triage_pkg)
        from carpenter.packages.state import PackageStateHandle

        _seed_packages_table("carpenter-gmail")
        handle = PackageStateHandle("carpenter-gmail")
        monkeypatch.delenv("GMAIL_OAUTH_ACCESS_TOKEN", raising=False)

        urls: list[str] = []
        with patch.object(
            gp.urllib.request, "urlopen",
            _fake_urlopen_factory([], urls),
        ):
            trig = gp.GmailPollTrigger(
                "p", {"cadence_seconds": 900},
                source_package="carpenter-gmail", package_state=handle,
            )
            trig.start()
        # No HTTP call, no watermark.
        assert urls == []
        assert handle.get("history_id") is None

    def test_check_emits_one_event_per_new_message(
        self, triage_pkg: Path, monkeypatch,
    ):
        gp = _load_trigger_module(triage_pkg)
        from carpenter.packages.state import PackageStateHandle

        _seed_packages_table("carpenter-gmail")
        handle = PackageStateHandle("carpenter-gmail")
        handle.set("history_id", "100")
        handle.set("gmail_account_email", "ben@example.com")
        monkeypatch.setenv("GMAIL_OAUTH_ACCESS_TOKEN", "tok")

        urls: list[str] = []
        responses = [
            {
                "historyId": "105",
                "history": [
                    {
                        "id": "101",
                        "messagesAdded": [
                            {"message": {"id": "mAAAAA"}},
                            {"message": {"id": "mBBBBB"}},
                        ],
                    },
                    {
                        "id": "105",
                        "messagesAdded": [
                            {"message": {"id": "mCCCCC"}},
                        ],
                    },
                ],
            },
        ]
        with patch.object(
            gp.urllib.request, "urlopen",
            _fake_urlopen_factory(responses, urls),
        ):
            trig = gp.GmailPollTrigger(
                "p", {"cadence_seconds": 900},
                source_package="carpenter-gmail", package_state=handle,
            )
            trig.check()

        # Three events recorded.
        with sqlite3.connect(
            __import__("carpenter").config.CONFIG["database_path"],
        ) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT event_type, payload_json, idempotency_key "
                "FROM events WHERE event_type = ? ORDER BY id",
                ("email.received",),
            ).fetchall()
        assert len(rows) == 3
        ids = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            assert payload["_source_package"] == "carpenter-gmail"
            assert payload["account"] == "ben@example.com"
            assert payload["received_history_id"] == "105"
            # Payload must NOT contain subject/from/snippet/body fields.
            for forbidden in ("subject", "from", "snippet", "body", "headers"):
                assert forbidden not in payload, (
                    f"emitted payload smuggled {forbidden!r}: {payload}"
                )
            ids.append(payload["provider_message_id"])
        assert ids == ["mAAAAA", "mBBBBB", "mCCCCC"]
        # Idempotency keys are unique per message id.
        keys = {row["idempotency_key"] for row in rows}
        assert keys == {
            "gmail-poll-mAAAAA",
            "gmail-poll-mBBBBB",
            "gmail-poll-mCCCCC",
        }
        # Watermark advanced.
        assert handle.get("history_id") == "105"

    def test_emit_cap_enforced_per_poll(
        self, triage_pkg: Path, monkeypatch,
    ):
        gp = _load_trigger_module(triage_pkg)
        from carpenter.packages.state import PackageStateHandle

        _seed_packages_table("carpenter-gmail")
        handle = PackageStateHandle("carpenter-gmail")
        handle.set("history_id", "100")
        monkeypatch.setenv("GMAIL_OAUTH_ACCESS_TOKEN", "tok")

        # 40 messages on one page — cap is 25.
        added = [
            {"message": {"id": f"msg{i:05d}"}}
            for i in range(40)
        ]
        responses = [
            {
                "historyId": "200",
                "history": [{"id": "150", "messagesAdded": added}],
            },
        ]
        urls: list[str] = []
        with patch.object(
            gp.urllib.request, "urlopen",
            _fake_urlopen_factory(responses, urls),
        ):
            trig = gp.GmailPollTrigger(
                "p", {"cadence_seconds": 900},
                source_package="carpenter-gmail", package_state=handle,
            )
            trig.check()
        with sqlite3.connect(
            __import__("carpenter").config.CONFIG["database_path"],
        ) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM events WHERE event_type=?",
                ("email.received",),
            ).fetchone()[0]
        assert count == gp._MAX_EMITS_PER_POLL == 25

    def test_auth_revoked_path_emits_and_disables(
        self, triage_pkg: Path, monkeypatch,
    ):
        import urllib.error

        gp = _load_trigger_module(triage_pkg)
        from carpenter.packages.state import PackageStateHandle

        _seed_packages_table("carpenter-gmail")
        handle = PackageStateHandle("carpenter-gmail")
        handle.set("history_id", "100")
        monkeypatch.setenv("GMAIL_OAUTH_ACCESS_TOKEN", "tok")

        err = urllib.error.HTTPError(
            url="x", code=401, msg="Unauthorized", hdrs=None, fp=None,
        )
        urls: list[str] = []
        with patch.object(
            gp.urllib.request, "urlopen",
            _fake_urlopen_factory([err], urls),
        ):
            trig = gp.GmailPollTrigger(
                "p", {"cadence_seconds": 900},
                source_package="carpenter-gmail", package_state=handle,
            )
            trig.check()
            # Subsequent check() must be a no-op — _disabled_in_process.
            urls_before_second = len(urls)
            trig.check()
            assert len(urls) == urls_before_second
        assert trig._disabled_in_process is True
        # Exactly one email.auth_revoked event was recorded.
        with sqlite3.connect(
            __import__("carpenter").config.CONFIG["database_path"],
        ) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT payload_json FROM events WHERE event_type=?",
                ("email.auth_revoked",),
            ).fetchall()
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload.get("_source_package") == "carpenter-gmail"

    def test_rate_limited_path_sets_backoff(
        self, triage_pkg: Path, monkeypatch,
    ):
        import urllib.error

        gp = _load_trigger_module(triage_pkg)
        from carpenter.packages.state import PackageStateHandle

        _seed_packages_table("carpenter-gmail")
        handle = PackageStateHandle("carpenter-gmail")
        handle.set("history_id", "100")
        monkeypatch.setenv("GMAIL_OAUTH_ACCESS_TOKEN", "tok")

        err = urllib.error.HTTPError(
            url="x", code=429, msg="Too Many Requests",
            hdrs=None, fp=None,
        )
        urls: list[str] = []
        with patch.object(
            gp.urllib.request, "urlopen",
            _fake_urlopen_factory([err], urls),
        ):
            trig = gp.GmailPollTrigger(
                "p", {"cadence_seconds": 900},
                source_package="carpenter-gmail", package_state=handle,
            )
            trig.check()
        backoff = handle.get("gmail_poll_backoff_until")
        assert backoff is not None
        # Parseable ISO-format timestamp, in the future.
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(backoff)
        assert parsed > datetime.now(timezone.utc)

    def test_backoff_skips_poll(
        self, triage_pkg: Path, monkeypatch,
    ):
        gp = _load_trigger_module(triage_pkg)
        from carpenter.packages.state import PackageStateHandle
        from datetime import datetime, timedelta, timezone

        _seed_packages_table("carpenter-gmail")
        handle = PackageStateHandle("carpenter-gmail")
        handle.set("history_id", "100")
        # 1h in the future — every poll should bail out before HTTP.
        handle.set(
            "gmail_poll_backoff_until",
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        monkeypatch.setenv("GMAIL_OAUTH_ACCESS_TOKEN", "tok")

        urls: list[str] = []
        with patch.object(
            gp.urllib.request, "urlopen",
            _fake_urlopen_factory([], urls),
        ):
            trig = gp.GmailPollTrigger(
                "p", {"cadence_seconds": 900},
                source_package="carpenter-gmail", package_state=handle,
            )
            trig.check()
        assert urls == []  # No HTTP call attempted.

    def test_cadence_guard_skips_too_frequent_calls(
        self, triage_pkg: Path, monkeypatch,
    ):
        gp = _load_trigger_module(triage_pkg)
        from carpenter.packages.state import PackageStateHandle

        _seed_packages_table("carpenter-gmail")
        handle = PackageStateHandle("carpenter-gmail")
        handle.set("history_id", "100")
        monkeypatch.setenv("GMAIL_OAUTH_ACCESS_TOKEN", "tok")

        # First call succeeds with empty history (one HTTP request).
        # Second call within cadence must NOT make another HTTP call.
        responses: list[Any] = [
            {"historyId": "100", "history": []},
        ]
        urls: list[str] = []
        with patch.object(
            gp.urllib.request, "urlopen",
            _fake_urlopen_factory(responses, urls),
        ):
            trig = gp.GmailPollTrigger(
                "p", {"cadence_seconds": 900},
                source_package="carpenter-gmail", package_state=handle,
            )
            trig.check()
            assert len(urls) == 1
            # Immediate re-check — cadence guard fires.
            trig.check()
            assert len(urls) == 1, (
                "second check() must be skipped by the cadence guard"
            )

    def test_concurrent_poll_guard_blocks_reentry(
        self, triage_pkg: Path, monkeypatch,
    ):
        gp = _load_trigger_module(triage_pkg)
        from carpenter.packages.state import PackageStateHandle

        _seed_packages_table("carpenter-gmail")
        handle = PackageStateHandle("carpenter-gmail")
        handle.set("history_id", "100")
        # Simulate a prior poll that left the in-progress flag set
        # (e.g., daemon crash mid-poll).  A new check() must NOT acquire
        # the slot and must NOT issue any HTTP request.
        handle.set("gmail_poll_in_progress", True)
        monkeypatch.setenv("GMAIL_OAUTH_ACCESS_TOKEN", "tok")

        urls: list[str] = []
        with patch.object(
            gp.urllib.request, "urlopen",
            _fake_urlopen_factory([], urls),
        ):
            trig = gp.GmailPollTrigger(
                "p", {"cadence_seconds": 900},
                source_package="carpenter-gmail", package_state=handle,
            )
            trig.check()
        assert urls == []
        # Flag remains set (we didn't own it, didn't release it).
        assert handle.get("gmail_poll_in_progress") is True


# =====================================================================
# Subscription wiring (I9 isolation invariant)
# =====================================================================


class TestSubscriptionWiring:
    """Tests for the manifest's ``trigger_subscriptions`` block.

    We exercise the subscription installer (``_install_trigger_subscriptions``)
    directly rather than going through the full ``install_package`` flow,
    because the latter pulls in security checks (e.g. KB namespace
    scoping) that are out of scope for these tests — the existing
    ``test_carpenter_gmail_pkg.py`` follows the same pattern.
    """

    def test_install_registers_email_received_subscription(
        self, triage_pkg: Path, tmp_path: Path,
    ):
        """The manifest's ``email.received`` subscription lands in the
        platform subscription store with ``source_package=carpenter-gmail``.
        """
        from carpenter.packages.manifest import load_manifest
        from carpenter.packages.installer import _install_trigger_subscriptions
        from carpenter.core.engine import subscriptions as subs

        # Wipe any state from prior tests.
        if hasattr(subs, "unregister_for_package"):
            subs.unregister_for_package("carpenter-gmail")

        m = load_manifest(triage_pkg / "manifest.yaml")
        # _install_trigger_subscriptions writes a record file under the
        # install_path; we use a tmp dir as the stand-in install path.
        install_path = tmp_path / "fake-install"
        install_path.mkdir()
        n = _install_trigger_subscriptions(m, install_path)
        assert n == 1

        try:
            matches = [
                s for s in subs._subscriptions
                if s.event_type == "email.received"
                and s.source_package == "carpenter-gmail"
            ]
            assert len(matches) == 1, (
                f"expected exactly one carpenter-gmail subscription for "
                f"email.received, found {len(matches)}"
            )
            assert matches[0].action_type == "package_dispatch"
            assert matches[0].action_config["package"] == "carpenter-gmail"
            assert matches[0].action_config["handler"] == (
                "handlers.triage_inbound:handle_email_received"
            )
        finally:
            if hasattr(subs, "unregister_for_package"):
                subs.unregister_for_package("carpenter-gmail")

    def test_source_package_isolation_blocks_foreign_emit(
        self, triage_pkg: Path, tmp_path: Path,
    ):
        """An ``email.received`` event whose ``_source_package`` is NOT
        ``carpenter-gmail`` must not reach the package's handler.  This
        is I9: a different package cannot forge an event that targets
        carpenter-gmail's triage handler.
        """
        from carpenter.packages.manifest import load_manifest
        from carpenter.packages.installer import _install_trigger_subscriptions
        from carpenter.core.engine import subscriptions as subs

        if hasattr(subs, "unregister_for_package"):
            subs.unregister_for_package("carpenter-gmail")

        m = load_manifest(triage_pkg / "manifest.yaml")
        install_path = tmp_path / "fake-install"
        install_path.mkdir()
        _install_trigger_subscriptions(m, install_path)

        try:
            sub = next(
                s for s in subs._subscriptions
                if s.event_type == "email.received"
                and s.source_package == "carpenter-gmail"
            )
            # The cross-check helper lives in the subscriptions module.
            # Genuine event from carpenter-gmail passes; forgery from
            # some other package is rejected.
            assert subs._source_package_matches(
                sub, {"_source_package": "carpenter-gmail"},
            )
            assert not subs._source_package_matches(
                sub, {"_source_package": "intruder-pkg"},
            )
            # Untagged event (no _source_package, e.g. external HTTP
            # webhook) is permissive — that's the legacy
            # ``trigger_subscriptions`` pattern for "package responds
            # to external event".  See
            # carpenter.core.engine.subscriptions._source_package_matches
            # for the full back-compat rationale.
            assert subs._source_package_matches(sub, {})
        finally:
            if hasattr(subs, "unregister_for_package"):
                subs.unregister_for_package("carpenter-gmail")

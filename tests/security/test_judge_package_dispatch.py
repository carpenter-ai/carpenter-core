"""Tests for package-shipped JUDGE dispatch + policy-field validation.

PR #306 followup tests.  Covers:

* BLOCKING #1: ``_resolve_package_template`` correctly looks up the
  ``installed_packages_templates`` row by template name (the column is
  TEXT and stores the template name directly, not a numeric id).
* IMPORTANT #2: ``_validate_policy_fields`` validates ALL 9
  ``PolicyLiteral`` subclasses (EmailPolicy, Domain, Url, FilePath,
  Command, IntRange, Enum, Bool, Pattern), not the prior 5.
* IMPORTANT #3: list / nested-dataclass fields are walked and each
  reachable ``PolicyLiteral`` instance is validated.
* IMPORTANT #4: narrowed exception handlers don't accidentally swallow
  real bugs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from carpenter.core.resources import (
    derive_resource,
    get_resource,
    link_arc_resource,
    resource_storage_path,
)
from carpenter.db import get_db
from carpenter.packages.handler_registry import get_handler_registry
from carpenter.security import policy_store
from carpenter.security.judge import (
    JudgeResult,
    PolicyCheck,
    _resolve_package_template,
    _validate_policy_fields,
    run_policy_checks,
)
from carpenter.tool_backends import arc as arc_backend
from carpenter_tools.policy.types import (
    Bool,
    Command,
    Domain,
    EmailPolicy,
    Enum,
    FilePath,
    IntRange,
    Pattern,
    PolicyLiteral,
    Url,
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def fresh_registry():
    """Reset the global package-handler registry before/after each test."""
    reg = get_handler_registry()
    reg.reset()
    yield reg
    reg.reset()


def _create_batch_with_judge():
    """Create the standard target/reviewer/judge arc batch."""
    arcs = [
        {"name": "target", "integrity_level": "untrusted"},
        {
            "name": "reviewer",
            "agent_type": "REVIEWER",
            "reviewer_profile": "security-reviewer",
        },
        {
            "name": "judge",
            "agent_type": "JUDGE",
            "reviewer_profile": "judge",
        },
    ]
    result = arc_backend.handle_create_batch({"arcs": arcs})
    return result["arc_ids"]


def _record_installed_template(
    *, package_name: str, template_name: str,
) -> None:
    """Insert a row into ``installed_packages`` + ``installed_packages_templates``.

    Mirrors what ``installer.install_package`` would do — without
    materialising an actual on-disk install — so JUDGE-dispatch tests
    can exercise the resolution path without spinning up a full package
    install.

    The shared test template DB is built with ``skip_migrations=True``
    so the installer tables aren't created by default.  We call
    ``ensure_installer_tables`` here to make this helper self-contained
    (idempotent CREATE IF NOT EXISTS, so it's free if already present).
    """
    from carpenter.packages.installer import ensure_installer_tables

    db = get_db()
    try:
        ensure_installer_tables(db)
        db.execute(
            "INSERT OR REPLACE INTO installed_packages "
            "(name, version, hash, source_path, install_path, installed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (package_name, "0.0.1", "deadbeef", "/src", "/install", "2026-01-01"),
        )
        db.execute(
            "INSERT OR REPLACE INTO installed_packages_templates "
            "(package_name, template_name, kind) VALUES (?, ?, ?)",
            (package_name, template_name, "arc_template"),
        )
        db.commit()
    finally:
        db.close()


def _ensure_installer_tables_in_test_db() -> None:
    """Ensure ``installed_packages_templates`` exists in the test DB.

    Used by tests that don't insert rows but still query the table.
    """
    from carpenter.packages.installer import ensure_installer_tables
    db = get_db()
    try:
        ensure_installer_tables(db)
        db.commit()
    finally:
        db.close()


def _emit_resource(
    *,
    reviewer_arc_id: int,
    template_name: str,
    payload_obj: Any,
    kind: str,
) -> int:
    """Emit a pending Resource as a REVIEWER would (test helper)."""
    rid = derive_resource(
        content_type="application/json",
        file_path=None,
        produced_by_arc_id=reviewer_arc_id,
        produced_by_template=template_name,
        template_verdict="pending",
        kind=kind,
    )
    path = resource_storage_path(rid, "extraction.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload_obj), encoding="utf-8")
    db = get_db()
    try:
        db.execute(
            "UPDATE resources SET file_path = ? WHERE id = ?",
            (str(path), rid),
        )
        db.commit()
    finally:
        db.close()
    link_arc_resource(arc_id=reviewer_arc_id, resource_id=rid, role="output")
    return rid


# ── BLOCKING #1: package JUDGE dispatch resolution ──────────────────


class TestResolvePackageTemplate:
    """``_resolve_package_template`` must use the template NAME, not int(id)."""

    def test_resolves_by_template_name(self, fresh_registry):
        """The blocking-bug regression: produced_by_template is TEXT, holds
        the template name. The original code did int(produced_by) which
        raised ValueError on every package-shipped Resource."""
        _record_installed_template(
            package_name="testpkg", template_name="my-package-template",
        )
        row = {"id": 99, "produced_by_template": "my-package-template"}
        result = _resolve_package_template(row)
        assert result == ("my-package-template", "testpkg")

    def test_returns_none_for_platform_template(self, fresh_registry):
        _ensure_installer_tables_in_test_db()
        row = {"id": 100, "produced_by_template": "reflection"}
        # Not registered as a package template — should return None.
        assert _resolve_package_template(row) is None

    def test_returns_none_for_null(self, fresh_registry):
        assert _resolve_package_template(
            {"id": 1, "produced_by_template": None},
        ) is None
        assert _resolve_package_template(
            {"id": 1, "produced_by_template": ""},
        ) is None

    def test_handles_template_name_with_special_chars(self, fresh_registry):
        """Template names with dashes / underscores must round-trip."""
        _record_installed_template(
            package_name="pkg", template_name="email-triage_v2",
        )
        row = {"id": 1, "produced_by_template": "email-triage_v2"}
        assert _resolve_package_template(row) == ("email-triage_v2", "pkg")


# ── BLOCKING #1 (integration): full run_policy_checks path ──────────


@dataclass
class _PkgExtract:
    """Test extract dataclass for a package-shipped template."""

    label: str
    qty: int


def _judge_pkg_extract(extract):
    """Test handler that approves iff qty <= 10."""
    @dataclass
    class _R:
        approved: bool
        reason: str = ""
        checks: list = field(default_factory=list)
    if extract.qty <= 10:
        return _R(approved=True, reason="ok")
    return _R(approved=False, reason=f"qty {extract.qty} > 10")


class TestPackageJudgeIntegration:
    """End-to-end: derive_resource → run_policy_checks → package handler.

    These tests would have FAILED on PR #306 main because
    ``_resolve_package_template`` always returned None, the package
    handler was never invoked, and the Resource fell through to
    ``_extraction_to_checks`` which doesn't recognise the dataclass
    shape.
    """

    def test_package_handler_invoked_when_registered(self, fresh_registry):
        # Register a package's data model + judge.
        fresh_registry.register_kind(
            "testpkg", "_PkgExtract", _PkgExtract,
        )
        fresh_registry.register_judge(
            "testpkg", "pkg-template", _judge_pkg_extract,
        )
        _record_installed_template(
            package_name="testpkg", template_name="pkg-template",
        )

        target_id, reviewer_id, judge_id = _create_batch_with_judge()

        # Emit a Resource that the package's _PkgExtract dataclass can
        # round-trip.
        rid = _emit_resource(
            reviewer_arc_id=reviewer_id,
            template_name="pkg-template",
            payload_obj={"label": "widget", "qty": 5},
            kind="_PkgExtract",
        )

        result = run_policy_checks(judge_id)
        assert isinstance(result, JudgeResult)
        assert result.approved is True
        # The Resource was flipped to 'approved' by the package path.
        assert get_resource(rid)["template_verdict"] == "approved"
        # The reason came from our handler (passed through coerce step).
        assert "ok" in result.reason

    def test_package_handler_can_reject(self, fresh_registry):
        fresh_registry.register_kind(
            "testpkg", "_PkgExtract", _PkgExtract,
        )
        fresh_registry.register_judge(
            "testpkg", "pkg-template", _judge_pkg_extract,
        )
        _record_installed_template(
            package_name="testpkg", template_name="pkg-template",
        )

        target_id, reviewer_id, judge_id = _create_batch_with_judge()
        rid = _emit_resource(
            reviewer_arc_id=reviewer_id,
            template_name="pkg-template",
            payload_obj={"label": "widget", "qty": 99},
            kind="_PkgExtract",
        )

        result = run_policy_checks(judge_id)
        assert result.approved is False
        assert get_resource(rid)["template_verdict"] == "rejected"
        assert "99" in result.reason or "qty" in result.reason

    def test_package_path_skipped_when_no_handler(self, fresh_registry):
        """If the package has a kind but no JUDGE registered, fall back."""
        # Register the kind but NOT the judge.
        fresh_registry.register_kind(
            "testpkg", "_PkgExtract", _PkgExtract,
        )
        _record_installed_template(
            package_name="testpkg", template_name="pkg-template",
        )

        target_id, reviewer_id, judge_id = _create_batch_with_judge()
        # Provide a payload the kind decoder accepts but the platform
        # fallback can't interpret as a check list — should reject.
        rid = _emit_resource(
            reviewer_arc_id=reviewer_id,
            template_name="pkg-template",
            payload_obj={"label": "widget", "qty": 1},
            kind="_PkgExtract",
        )
        result = run_policy_checks(judge_id)
        # No package handler → fell through to platform default.  The
        # platform's ``_extraction_to_checks`` doesn't know how to read
        # a ``_PkgExtract``, so the JUDGE rejects loudly.
        assert result.approved is False


# ── IMPORTANT #2: all 9 PolicyLiteral subclasses are validated ──────


@dataclass
class _AllTypesExtract:
    """Extract with one field per PolicyLiteral subclass."""
    e: EmailPolicy | None = None
    d: Domain | None = None
    u: Url | None = None
    fp: FilePath | None = None
    c: Command | None = None
    ir: IntRange | None = None
    en: Enum | None = None
    b: Bool | None = None
    p: Pattern | None = None


def _seed_allowlists():
    """Populate every policy allowlist with permissive entries."""
    # email/domain/url/filepath/command/enum/pattern: exact/prefix/match
    policy_store.add_to_allowlist("email", "ok@example.com")
    policy_store.add_to_allowlist("domain", "example.com")
    policy_store.add_to_allowlist("url", "https://example.com/")
    policy_store.add_to_allowlist("filepath", "/tmp/safe/")
    policy_store.add_to_allowlist("command", "ls")
    policy_store.add_to_allowlist("enum", "approved")
    policy_store.add_to_allowlist("pattern", r"v\d+\.\d+")
    # IntRange / Bool: their _serialized_value() yields "lo:hi" / "true"
    # respectively.  validate("int_range", "80:443") fails by design (the
    # validator parses the value as an integer); we add an entry that
    # covers the literal's range AS A RANGE so the existence-check below
    # documents the behavior even when the literal carries a range.
    policy_store.add_to_allowlist("int_range", "80:443")
    policy_store.add_to_allowlist("bool", "true")


class TestPolicyFieldValidationCoversAllTypes:
    """IMPORTANT #2: every PolicyLiteral subclass must be checked."""

    def test_all_nine_subclasses_produce_checks(self):
        """The walker must reach every PolicyLiteral instance."""
        _seed_allowlists()
        # Create one instance of each subclass.  Several will FAIL the
        # allowlist (Url prefix mismatch, FilePath prefix mismatch, etc.)
        # but every field must produce a PolicyCheck row — that's the
        # invariant the prior code violated for IntRange/Enum/Bool/Pattern.
        extract = _AllTypesExtract(
            e=EmailPolicy("ok@example.com"),
            d=Domain("example.com"),
            u=Url("https://example.com/path"),
            fp=FilePath("/tmp/safe/file"),
            c=Command("ls"),
            ir=IntRange(80, 443),
            en=Enum("approved"),
            b=Bool(True),
            p=Pattern(r"v\d+\.\d+"),
        )
        checks = _validate_policy_fields(extract)
        # Every subclass yields exactly one check (one PolicyLiteral per
        # field).  The PRIOR code only handled the first 5, so the test
        # would have only seen 5 checks.
        assert len(checks) == 9, f"Got {len(checks)} checks, want 9: {checks}"
        seen_types = {c.policy_type for c in checks}
        assert seen_types == {
            "email", "domain", "url", "filepath", "command",
            "int_range", "enum", "bool", "pattern",
        }

    def test_intrange_enum_bool_pattern_are_validated(self):
        """The four types previously skipped now appear in the result set."""
        _seed_allowlists()
        extract = _AllTypesExtract(
            ir=IntRange(80, 443),
            en=Enum("approved"),
            b=Bool(True),
            p=Pattern(r"v\d+\.\d+"),
        )
        checks = _validate_policy_fields(extract)
        types = {c.policy_type for c in checks}
        # All four NEW types must be present.  None of these would
        # appear under the old hardcoded 5-class map.
        assert "int_range" in types
        assert "enum" in types
        assert "bool" in types
        assert "pattern" in types

    def test_field_name_recorded(self):
        """Each check carries its field name for diagnostics."""
        _seed_allowlists()
        extract = _AllTypesExtract(en=Enum("approved"), b=Bool(True))
        checks = _validate_policy_fields(extract)
        names = {c.field_name for c in checks}
        assert names == {"en", "b"}


# ── IMPORTANT #3: list / nested-dataclass walking ───────────────────


@dataclass
class _Inner:
    addr: EmailPolicy
    tag: Enum


@dataclass
class _Outer:
    senders: list[EmailPolicy] = field(default_factory=list)
    inner: _Inner | None = None
    domains_by_tag: dict[str, Domain] = field(default_factory=dict)


class TestPolicyFieldsWalksContainers:
    """IMPORTANT #3: list / dict / nested dataclass elements are walked."""

    def test_list_of_policy_literals_each_validated(self):
        policy_store.add_to_allowlist("email", "a@example.com")
        policy_store.add_to_allowlist("email", "b@example.com")
        extract = _Outer(
            senders=[
                EmailPolicy("a@example.com"),
                EmailPolicy("b@example.com"),
                EmailPolicy("c@evil.com"),  # not on allowlist → fails
            ],
        )
        checks = _validate_policy_fields(extract)
        # 3 elements × 1 EmailPolicy each = 3 checks.
        email_checks = [c for c in checks if c.policy_type == "email"]
        assert len(email_checks) == 3
        assert {c.passed for c in email_checks} == {True, False}
        # Field names indexed.
        names = sorted(c.field_name for c in email_checks)
        assert names == ["senders[0]", "senders[1]", "senders[2]"]

    def test_nested_dataclass_recurses(self):
        policy_store.add_to_allowlist("email", "x@example.com")
        policy_store.add_to_allowlist("enum", "high")
        extract = _Outer(
            inner=_Inner(addr=EmailPolicy("x@example.com"), tag=Enum("high")),
        )
        checks = _validate_policy_fields(extract)
        names = {c.field_name for c in checks}
        # Both nested fields validated, with dotted names.
        assert "inner.addr" in names
        assert "inner.tag" in names

    def test_dict_values_validated(self):
        policy_store.add_to_allowlist("domain", "example.com")
        extract = _Outer(
            domains_by_tag={"primary": Domain("example.com")},
        )
        checks = _validate_policy_fields(extract)
        domain_checks = [c for c in checks if c.policy_type == "domain"]
        assert len(domain_checks) == 1
        assert domain_checks[0].passed is True
        assert "domains_by_tag" in domain_checks[0].field_name

    def test_empty_containers_produce_no_checks(self):
        extract = _Outer(senders=[], domains_by_tag={})
        checks = _validate_policy_fields(extract)
        assert checks == []

    def test_no_dataclass_returns_empty(self):
        # Plain dict / list / int / string passed straight in: not a
        # dataclass, walker bails early.
        assert _validate_policy_fields({"x": 1}) == []
        assert _validate_policy_fields([1, 2, 3]) == []
        assert _validate_policy_fields("hello") == []


# ── IMPORTANT #4: narrowed exception handlers ───────────────────────


class TestNarrowedExceptions:
    """The PR-306 broad ``except Exception`` swallowed real bugs.

    The fix narrows each catch to ``ImportError`` (for soft-fallback when
    optional submodules are absent).  Real KeyErrors / AttributeErrors /
    sqlite3.Errors should now propagate.
    """

    def test_resolve_package_template_propagates_unexpected_error(
        self, monkeypatch,
    ):
        """An sqlite3-level corruption from db_connection should NOT be
        swallowed by ``_resolve_package_template``."""
        from carpenter.security import judge as judge_mod

        class _ExplodingConn:
            def __enter__(self):
                raise RuntimeError("simulated DB explosion")
            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            judge_mod, "db_connection", lambda: _ExplodingConn(),
        )
        with pytest.raises(RuntimeError, match="simulated DB explosion"):
            _resolve_package_template(
                {"id": 1, "produced_by_template": "any"},
            )

    def test_validate_policy_fields_skips_when_carpenter_tools_absent(
        self, monkeypatch, caplog,
    ):
        """When ``carpenter_tools.policy.types`` is unavailable, the
        function returns [] and logs a warning.  This is the ONLY
        deliberately swallowed exception in the new code."""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "carpenter_tools.policy.types":
                raise ImportError("simulated missing carpenter_tools")
            return real_import(name, *args, **kwargs)

        # This dataclass instance won't be used because the import is
        # short-circuited before any field walking happens.
        @dataclass
        class _X:
            v: Any = None

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with caplog.at_level("WARNING"):
            result = _validate_policy_fields(_X(v="anything"))
        assert result == []
        assert any(
            "carpenter_tools.policy.types unavailable" in rec.message
            for rec in caplog.records
        )

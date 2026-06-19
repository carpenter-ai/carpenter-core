"""Tests for carpenter.security.judge (deterministic JUDGE).

D24 §11: REVIEWER → JUDGE handoff rides on the Resources pipeline rather
than the legacy ``_extraction_output``/``_judge_policy_checks`` arc-state
shortcut.  These tests drive the JUDGE through pending Resources and
verify ``mark_template_verdict`` is flipped to match the verdict.
"""

import json

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import (
    derive_resource,
    get_resource,
    link_arc_resource,
    resource_storage_path,
)
from carpenter.core.workflows import review_manager
from carpenter.security.judge import (
    PolicyCheck,
    PolicyCheckList,
    JudgeResult,
    _find_pending_extraction_resource,
    _get_review_target,
    run_policy_checks,
)
from carpenter.security import policy_store
from carpenter.tool_backends import arc as arc_backend
from carpenter.db import get_db


def _create_batch_with_judge(extra_arcs=None, parent_id=None):
    """Helper to create a standard batch with untrusted + reviewer + judge."""
    arcs = [
        {
            "name": "target",
            "integrity_level": "untrusted",
        },
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
    if extra_arcs:
        arcs.extend(extra_arcs)
    if parent_id:
        for a in arcs:
            a["parent_id"] = parent_id

    result = arc_backend.handle_create_batch({"arcs": arcs})
    assert "arc_ids" in result
    return result["arc_ids"]


def _emit_extraction_resource(
    *,
    reviewer_arc_id: int,
    template_name: str = "test-template",
    checks: list[dict],
    kind: str | None = "PolicyCheckList",
    payload_override: object | None = None,
) -> int:
    """Emit a pending extraction Resource for a REVIEWER arc.

    Mirrors the post-D24 §11 reviewer-side emission pattern: derive a
    Resource with ``produced_by_template=<template>``,
    ``template_verdict='pending'``, ``kind=<kind>``, write the JSON
    bytes, and link it as the reviewer's output.

    ``payload_override`` lets tests inject malformed/legacy shapes.
    """
    if payload_override is not None:
        payload_obj = payload_override
    elif kind == "PolicyCheckList":
        payload_obj = {"checks": checks}
    else:
        # Legacy / kind-less raw-list shape.
        payload_obj = checks

    resource_id = derive_resource(
        content_type="application/json",
        file_path=None,
        produced_by_arc_id=reviewer_arc_id,
        produced_by_template=template_name,
        template_verdict="pending",
        kind=kind,
    )
    path = resource_storage_path(resource_id, "extraction.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload_obj), encoding="utf-8")
    # Update file_path on the row.
    db = get_db()
    try:
        db.execute(
            "UPDATE resources SET file_path = ? WHERE id = ?",
            (str(path), resource_id),
        )
        db.commit()
    finally:
        db.close()
    link_arc_resource(arc_id=reviewer_arc_id, resource_id=resource_id, role="output")
    return resource_id


class TestGetReviewTarget:

    def test_returns_target_id(self):
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        # Judge should have _review_target pointing to the target
        assert _get_review_target(judge_id) == target_id

    def test_returns_none_for_non_judge(self):
        arc_id = arc_manager.create_arc("regular")
        assert _get_review_target(arc_id) is None


class TestFindPendingExtractionResource:

    def test_returns_none_when_no_resource(self):
        ids = _create_batch_with_judge()
        target_id = ids[0]
        assert _find_pending_extraction_resource(target_id) is None

    def test_finds_pending_resource_from_reviewer(self):
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        rid = _emit_extraction_resource(
            reviewer_arc_id=reviewer_id,
            checks=[{"field": "domain", "policy_type": "domain", "value": "safe.example.com"}],
        )

        row = _find_pending_extraction_resource(target_id)
        assert row is not None
        assert int(row["id"]) == rid
        assert row["template_verdict"] == "pending"
        assert row["kind"] == "PolicyCheckList"

    def test_skips_resources_already_verdicted(self):
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        # Approved Resource — should not be returned as pending.
        rid = derive_resource(
            content_type="application/json",
            file_path=None,
            produced_by_arc_id=reviewer_id,
            produced_by_template="t",
            template_verdict="approved",
            kind="PolicyCheckList",
        )
        link_arc_resource(arc_id=reviewer_id, resource_id=rid, role="output")
        assert _find_pending_extraction_resource(target_id) is None

    def test_multi_pending_collision_raises(self):
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        for _ in range(2):
            _emit_extraction_resource(
                reviewer_arc_id=reviewer_id,
                checks=[{"field": "x", "policy_type": "", "value": "y"}],
            )

        with pytest.raises(ValueError, match="pending extraction Resources"):
            _find_pending_extraction_resource(target_id)


class TestRunPolicyChecks:

    def test_auto_approve_when_no_extraction_resource(self):
        """With no Resource, judge approves by default."""
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        result = run_policy_checks(judge_id)
        assert result.approved is True
        assert "no_extraction" in result.reason.lower() or "no structured" in result.reason.lower()

    def test_approve_when_all_checks_pass(self):
        """Judge approves when all policy checks pass."""
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        policy_store.add_to_allowlist("email", "safe@example.com")

        rid = _emit_extraction_resource(
            reviewer_arc_id=reviewer_id,
            checks=[{"field": "recipient", "policy_type": "email", "value": "safe@example.com"}],
        )

        result = run_policy_checks(judge_id)
        assert result.approved is True
        assert len(result.checks) == 1
        assert result.checks[0].passed is True

        # Resource verdict was flipped to approved.
        row = get_resource(rid)
        assert row["template_verdict"] == "approved"

    def test_reject_when_check_fails(self):
        """Judge rejects when a policy check fails."""
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        rid = _emit_extraction_resource(
            reviewer_arc_id=reviewer_id,
            checks=[{"field": "target_email", "policy_type": "email", "value": "evil@hacker.com"}],
        )

        result = run_policy_checks(judge_id)
        assert result.approved is False
        assert len(result.failed_checks) == 1
        assert result.failed_checks[0].field_name == "target_email"

        row = get_resource(rid)
        assert row["template_verdict"] == "rejected"

    def test_mixed_pass_and_fail(self):
        """Judge rejects if any check fails, even if others pass."""
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        policy_store.add_to_allowlist("email", "good@example.com")

        rid = _emit_extraction_resource(
            reviewer_arc_id=reviewer_id,
            checks=[
                {"field": "to", "policy_type": "email", "value": "good@example.com"},
                {"field": "cc", "policy_type": "email", "value": "bad@evil.com"},
            ],
        )

        result = run_policy_checks(judge_id)
        assert result.approved is False
        assert len(result.checks) == 2
        assert len(result.failed_checks) == 1

        assert get_resource(rid)["template_verdict"] == "rejected"

    def test_fields_without_policy_type_pass(self):
        """Fields without policy_type constraint are auto-approved."""
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        rid = _emit_extraction_resource(
            reviewer_arc_id=reviewer_id,
            checks=[{"field": "summary", "value": "Any text here"}],  # no policy_type
        )

        result = run_policy_checks(judge_id)
        assert result.approved is True
        assert get_resource(rid)["template_verdict"] == "approved"

    def test_nonexistent_judge_arc(self):
        result = run_policy_checks(99999)
        assert result.approved is False
        assert "not found" in result.reason

    def test_domain_policy_check(self):
        """Test domain policy validation through judge."""
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        policy_store.add_to_allowlist("domain", "api.example.com")

        rid = _emit_extraction_resource(
            reviewer_arc_id=reviewer_id,
            checks=[{"field": "api_host", "policy_type": "domain", "value": "api.example.com"}],
        )

        result = run_policy_checks(judge_id)
        assert result.approved is True

    def test_int_range_policy_check(self):
        """Test int_range policy validation through judge."""
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        policy_store.add_to_allowlist("int_range", "80:443")

        rid = _emit_extraction_resource(
            reviewer_arc_id=reviewer_id,
            checks=[{"field": "port", "policy_type": "int_range", "value": 443}],
        )

        result = run_policy_checks(judge_id)
        assert result.approved is True

    def test_kindless_legacy_payload_still_works(self):
        """A pending Resource with kind=NULL is decoded as raw JSON list.

        D24 §11 keeps the kind-less back-compat path for in-flight rows
        until B-min phases it out.  Verify the path still works.
        """
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        policy_store.add_to_allowlist("email", "ok@example.com")

        # No kind, payload is a bare list (the legacy shape).
        rid = _emit_extraction_resource(
            reviewer_arc_id=reviewer_id,
            kind=None,
            checks=[{"field": "to", "policy_type": "email", "value": "ok@example.com"}],
        )

        result = run_policy_checks(judge_id)
        assert result.approved is True

    def test_unknown_kind_is_rejected(self):
        """Unknown kind on the Resource is a JUDGE-time rejection."""
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        rid = _emit_extraction_resource(
            reviewer_arc_id=reviewer_id,
            kind="NoSuchKind",
            checks=[{"field": "x", "policy_type": "", "value": "y"}],
        )

        result = run_policy_checks(judge_id)
        assert result.approved is False
        assert "Unknown extraction kind" in result.reason
        assert get_resource(rid)["template_verdict"] == "rejected"

    def test_malformed_payload_is_rejected(self):
        """Bytes that aren't JSON cause the JUDGE to reject."""
        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        rid = derive_resource(
            content_type="application/json",
            file_path=None,
            produced_by_arc_id=reviewer_id,
            produced_by_template="t",
            template_verdict="pending",
            kind="PolicyCheckList",
        )
        path = resource_storage_path(rid, "extraction.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json", encoding="utf-8")
        db = get_db()
        try:
            db.execute(
                "UPDATE resources SET file_path = ? WHERE id = ?",
                (str(path), rid),
            )
            db.commit()
        finally:
            db.close()
        link_arc_resource(arc_id=reviewer_id, resource_id=rid, role="output")

        result = run_policy_checks(judge_id)
        assert result.approved is False
        assert "decode" in result.reason.lower() or "Failed" in result.reason
        assert get_resource(rid)["template_verdict"] == "rejected"


class TestJudgeResultDataclass:

    def test_failed_checks_property(self):
        result = JudgeResult(
            approved=False,
            checks=[
                PolicyCheck("a", "email", "x", True),
                PolicyCheck("b", "email", "y", False, "denied"),
            ],
        )
        assert len(result.failed_checks) == 1
        assert result.failed_checks[0].field_name == "b"


class TestPolicyCheckListDataclass:

    def test_default_empty(self):
        pcl = PolicyCheckList()
        assert pcl.checks == []

    def test_round_trip_via_kind_dispatch(self):
        """A Resource emitted with kind=PolicyCheckList is read back as the dataclass."""
        from carpenter.security.judge import (
            _load_extraction_resource,
            _extraction_to_checks,
        )

        ids = _create_batch_with_judge()
        target_id, reviewer_id, judge_id = ids

        rid = _emit_extraction_resource(
            reviewer_arc_id=reviewer_id,
            checks=[{"field": "f", "policy_type": "", "value": "v"}],
        )
        row = get_resource(rid)
        extraction = _load_extraction_resource(row)
        assert isinstance(extraction, PolicyCheckList)
        assert _extraction_to_checks(extraction) == [
            {"field": "f", "policy_type": "", "value": "v"}
        ]


class TestConstructDataclassCoercion:
    """JSON-decoded payloads must rebuild the dataclass's runtime types.

    JSON has no tuple type and no dataclass type, so a REVIEWER that
    persists its extract via ``resource.write`` (the only reliable
    persistence path) stores tuples as lists and nested dataclasses as
    dicts.  The JUDGE handlers assert ``isinstance(x, tuple)`` /
    ``isinstance(x, SomeDataclass)`` and the policy-field validator only
    runs on ``PolicyLiteral`` instances, so the deserialiser must coerce
    these back or otherwise-valid extracts reject and allowlist checks
    silently skip.
    """

    def test_list_field_coerced_to_tuple(self):
        from dataclasses import dataclass, field
        from carpenter.security.judge import _construct_dataclass

        @dataclass(frozen=True)
        class _Extract:
            flags: tuple[str, ...] = ()
            schema_version: str = "1.0"

        obj = _construct_dataclass(
            _Extract, {"flags": ["a", "b"], "schema_version": "1.0"},
        )
        assert isinstance(obj.flags, tuple)
        assert obj.flags == ("a", "b")

    def test_nested_dataclass_list_coerced(self):
        from dataclasses import dataclass, field
        from carpenter.security.judge import _construct_dataclass

        @dataclass(frozen=True)
        class _Inner:
            name: str = ""
            size: int = 0

        @dataclass(frozen=True)
        class _Outer:
            items: tuple[_Inner, ...] = ()

        obj = _construct_dataclass(
            _Outer, {"items": [{"name": "x", "size": 3}]},
        )
        assert isinstance(obj.items, tuple)
        assert isinstance(obj.items[0], _Inner)
        assert obj.items[0].name == "x"
        assert obj.items[0].size == 3

    def test_policy_literal_field_reconstructed(self):
        # SECURITY: a PolicyLiteral field arriving as a bare JSON string
        # must be rebuilt into the literal so _validate_policy_fields
        # (which keys off isinstance(.., PolicyLiteral)) actually runs the
        # allowlist check rather than silently skipping the field.
        pytest.importorskip("carpenter_tools.policy.types")
        from dataclasses import dataclass, field
        from carpenter_tools.policy.types import EmailPolicy, PolicyLiteral
        from carpenter.security.judge import (
            _construct_dataclass,
            _validate_policy_fields,
        )

        @dataclass(frozen=True)
        class _Extract:
            sender: EmailPolicy = field(
                default_factory=lambda: EmailPolicy(""),
            )
            recipients: tuple[EmailPolicy, ...] = ()

        obj = _construct_dataclass(
            _Extract,
            {"sender": "a@b.com", "recipients": ["c@d.com", "e@f.com"]},
        )
        assert isinstance(obj.sender, PolicyLiteral)
        assert all(isinstance(r, PolicyLiteral) for r in obj.recipients)
        # The validator now sees PolicyLiteral instances and emits a check
        # per field (3 total: sender + 2 recipients).
        checks = _validate_policy_fields(obj)
        assert len(checks) == 3

    def test_unexpected_key_still_rejects(self):
        # Smuggled extra fields must still raise (caller maps to reject).
        from dataclasses import dataclass
        from carpenter.security.judge import _construct_dataclass

        @dataclass(frozen=True)
        class _Extract:
            ok: str = ""

        with pytest.raises(TypeError):
            _construct_dataclass(_Extract, {"ok": "v", "evil": "x"})

    def test_primitive_fields_passed_through(self):
        from dataclasses import dataclass
        from carpenter.security.judge import _construct_dataclass

        @dataclass(frozen=True)
        class _Extract:
            count: int = 0
            name: str = ""
            flag: bool = False

        obj = _construct_dataclass(
            _Extract, {"count": 5, "name": "hi", "flag": True},
        )
        assert obj.count == 5
        assert obj.name == "hi"
        assert obj.flag is True


class TestResolveKindDataclass:
    """resolve_kind_dataclass is the single source of truth for kind ->
    dataclass used by both the JUDGE decoder and the submit_extract
    field-schema validator."""

    def test_platform_kind_resolves(self):
        from carpenter.security.judge import (
            PolicyCheckList,
            resolve_kind_dataclass,
        )

        assert resolve_kind_dataclass("PolicyCheckList") is PolicyCheckList

    def test_unknown_kind_returns_none(self):
        from carpenter.security.judge import resolve_kind_dataclass

        assert resolve_kind_dataclass("NoSuchKindXyz") is None

    def test_package_kind_resolves_via_registry(self):
        from dataclasses import dataclass
        from carpenter.packages.handler_registry import get_handler_registry
        from carpenter.security.judge import resolve_kind_dataclass

        @dataclass
        class _PkgExtract:
            a: str = ""

        reg = get_handler_registry()
        reg.register_kind("test-pkg", "_PkgExtract", _PkgExtract)
        try:
            assert resolve_kind_dataclass("_PkgExtract") is _PkgExtract
        finally:
            reg.unregister_package("test-pkg")


class TestValidateExtractFields:
    """validate_extract_fields returns None on a match, else a corrective
    error string naming the unexpected/missing keys + the expected list."""

    def _cls(self):
        from dataclasses import dataclass, field

        @dataclass(frozen=True)
        class _Triage:
            provider_message_id: str = ""
            category: str = "unknown"
            importance_flags: tuple = ()
            required_thing: str = field(default_factory=str)

        return _Triage

    def test_correct_fields_pass(self):
        from carpenter.security.judge import validate_extract_fields

        cls = self._cls()
        err = validate_extract_fields(cls, {
            "provider_message_id": "x",
            "category": "personal",
            "importance_flags": [],
            "required_thing": "y",
        })
        assert err is None

    def test_unknown_field_rejected_with_corrective(self):
        from carpenter.security.judge import validate_extract_fields

        cls = self._cls()
        err = validate_extract_fields(cls, {
            "provider_message_id": "x",
            "category": "personal",
            "attachment_count": 3,
            "classification": "spam",
        })
        assert err is not None
        assert "rejected" in err.lower()
        assert "attachment_count" in err
        assert "classification" in err
        # The exact expected field names must be present.
        assert "provider_message_id" in err
        assert "category" in err
        assert "Re-call submit_extract" in err

    def test_missing_required_field_rejected(self):
        from carpenter.security.judge import validate_extract_fields

        cls = self._cls()
        # 'provider_message_id' has a plain default -> NOT required.
        # 'required_thing' has a default_factory -> also not required.
        # Construct a class with a truly-required field to exercise this.
        from dataclasses import dataclass

        @dataclass
        class _NeedsField:
            must_have: str
            opt: str = ""

        err = validate_extract_fields(_NeedsField, {"opt": "x"})
        assert err is not None
        assert "must_have" in err
        assert "missing required" in err.lower()

    def test_non_dataclass_kind_passes(self):
        from carpenter.security.judge import validate_extract_fields

        class _NotDataclass:
            pass

        assert validate_extract_fields(_NotDataclass, {"anything": 1}) is None

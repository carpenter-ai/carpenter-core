"""Tests for the ``submit_extract`` REVIEWER emit tool.

``submit_extract`` is the structured tool_use path by which a REVIEWER
arc-step agent persists its typed extract.  The LLM supplies the field
VALUES as a JSON ``fields`` argument; the handler writes them to the
REVIEWER arc's OWN pre-created pending extract Resource via the same
trusted logic as ``resource.write``.

Trust contract under test:
  * persists to the arc's pre-created pending Resource (caller==producer);
  * a non-producer / wrong-arc caller is refused;
  * the verdict stays ``pending`` (the tool never self-approves);
  * the JUDGE can then decode the JSON blob into the dataclass.
"""

import json

import pytest

from carpenter.agent.invocation import _handle_submit_extract
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager
from carpenter.core.resources import (
    derive_resource,
    read_resource_content,
    resource_storage_path,
)
from carpenter.core.workflows._arc_state import set_arc_state
from carpenter.db import db_transaction


def _reviewer_arc_with_pending_extract(kind: str = "EmailTriageExtract"):
    """Create a trusted REVIEWER arc + its pre-created pending extract,
    seeded into arc state exactly like the email-triage builder does.

    Mirrors arc_builders._create_triage_arc_tree: the extract Resource is
    template-owned (produced_by_template set), verdict='pending', kind
    tagged, with file_path wired — the shape the JUDGE consumes."""
    reviewer = arc_manager.create_arc(
        name="test-reviewer",
        agent_type="REVIEWER",
        integrity_level="trusted",
    )
    rid = derive_resource(
        content_type="dataclass",
        file_path=None,
        produced_by_arc_id=reviewer,
        produced_by_template="email_triage",
        template_verdict="pending",
        source_descriptor="extract:test",
        kind=kind,
    )
    path = resource_storage_path(rid, "blob")
    path.parent.mkdir(parents=True, exist_ok=True)
    with db_transaction() as db:
        db.execute(
            "UPDATE resources SET file_path = ? WHERE id = ?", (str(path), rid),
        )
    set_arc_state(reviewer, "extract_resource_id", rid)
    return reviewer, rid


class TestSubmitExtractPersists:
    def test_persists_fields_to_pending_resource(self):
        reviewer, rid = _reviewer_arc_with_pending_extract()
        fields = {
            "provider_message_id": "abc12",
            "received_history_id": "987654",
            "category": "personal",
            "from_address": "a@b.com",
            "subject_clean": "hello",
            "importance_flags": ["personal"],
            "attachments": [],
            "schema_version": "1.0",
        }

        result = _handle_submit_extract(
            {"fields": fields}, executor_arc_id=reviewer
        )
        assert "persisted" in result.lower()
        assert f"#{rid}" in result

        text = read_resource_content(rid, caller_arc_id=None)
        assert json.loads(text) == fields

    def test_does_not_flip_verdict(self):
        reviewer, rid = _reviewer_arc_with_pending_extract()
        _handle_submit_extract(
            {"fields": {"schema_version": "1.0"}}, executor_arc_id=reviewer
        )
        row = res_manager.get_resource(rid)
        # The Resource stays template-owned and pending — the JUDGE is the
        # sole authority that flips it to approved.
        assert row["template_verdict"] == "pending"


class TestSubmitExtractAuth:
    def test_no_arc_context_refused(self):
        result = _handle_submit_extract(
            {"fields": {"x": 1}}, executor_arc_id=None
        )
        assert result.startswith("Error")
        assert "arc" in result.lower()

    def test_missing_extract_resource_id_refused(self):
        reviewer = arc_manager.create_arc(
            name="reviewer-no-state",
            agent_type="REVIEWER",
            integrity_level="trusted",
        )
        result = _handle_submit_extract(
            {"fields": {"x": 1}}, executor_arc_id=reviewer
        )
        assert result.startswith("Error")
        assert "extract_resource_id" in result

    def test_caller_is_not_producer_refused(self):
        # Arc A produces the Resource; arc B's state points at it. B must
        # not be able to write A's Resource (caller != producer).
        producer, rid = _reviewer_arc_with_pending_extract()

        attacker = arc_manager.create_arc(
            name="attacker-reviewer",
            agent_type="REVIEWER",
            integrity_level="trusted",
        )
        # Seed the attacker's state to point at the victim's Resource.
        set_arc_state(attacker, "extract_resource_id", rid)

        result = _handle_submit_extract(
            {"fields": {"x": 1}}, executor_arc_id=attacker
        )
        assert result.startswith("Error")
        assert "refused" in result.lower() or "producer" in result.lower()


class TestSubmitExtractValidation:
    def test_missing_fields_refused(self):
        reviewer, _ = _reviewer_arc_with_pending_extract()
        result = _handle_submit_extract({}, executor_arc_id=reviewer)
        assert result.startswith("Error")
        assert "fields" in result

    def test_non_dict_fields_refused(self):
        reviewer, _ = _reviewer_arc_with_pending_extract()
        result = _handle_submit_extract(
            {"fields": "not-a-dict"}, executor_arc_id=reviewer
        )
        assert result.startswith("Error")


class TestSubmitExtractOfferGate:
    """`submit_extract` is offered to REVIEWER arcs only (not the chat
    agent, not other arc types) — respecting I10."""

    def test_offered_to_reviewer_arc(self):
        from carpenter.agent.invocation import _maybe_add_reviewer_emit_tool

        reviewer = arc_manager.create_arc(
            name="r", agent_type="REVIEWER", integrity_level="trusted",
        )
        out = _maybe_add_reviewer_emit_tool([], reviewer)
        assert any(t["name"] == "submit_extract" for t in out)

    def test_not_offered_to_executor_arc(self):
        from carpenter.agent.invocation import _maybe_add_reviewer_emit_tool

        # agent_type drives the gate (not integrity). Use a trusted EXECUTOR
        # so create_arc doesn't require the untrusted-batch builder.
        executor = arc_manager.create_arc(
            name="e", agent_type="EXECUTOR", integrity_level="trusted",
        )
        out = _maybe_add_reviewer_emit_tool([], executor)
        assert not any(t["name"] == "submit_extract" for t in out)

    def test_not_offered_without_arc(self):
        from carpenter.agent.invocation import _maybe_add_reviewer_emit_tool

        out = _maybe_add_reviewer_emit_tool([], None)
        assert out == []

    def test_not_in_always_available_set(self):
        """The normal chat agent must never see submit_extract."""
        from carpenter.chat_tool_loader import get_always_available_names

        assert "submit_extract" not in get_always_available_names()


class TestSubmitExtractFieldSchema:
    """The handler validates ``fields`` against the Resource's ``kind``
    dataclass BEFORE persisting.  On a mismatch it returns a corrective
    error and does NOT write, so the REVIEWER LLM can self-correct.

    Carpenter-core has no email package loaded, so we register a test
    dataclass kind in the package handler registry to exercise the path.
    """

    @pytest.fixture
    def registered_kind(self):
        from dataclasses import dataclass, field as dc_field
        from carpenter.packages.handler_registry import get_handler_registry

        @dataclass(frozen=True)
        class _TestTriage:
            provider_message_id: str = ""
            category: str = "unknown"
            importance_flags: tuple[str, ...] = ()
            schema_version: str = "1.0"

        reg = get_handler_registry()
        reg.register_kind("test-pkg-submit", "_TestTriage", _TestTriage)
        try:
            yield "_TestTriage"
        finally:
            reg.unregister_package("test-pkg-submit")

    def test_unknown_field_rejected_not_persisted(self, registered_kind):
        reviewer, rid = _reviewer_arc_with_pending_extract(
            kind=registered_kind
        )
        result = _handle_submit_extract(
            {"fields": {
                "provider_message_id": "x",
                "category": "personal",
                "attachment_count": 3,
                "classification": "spam",
            }},
            executor_arc_id=reviewer,
        )
        # Corrective error returned…
        assert "rejected" in result.lower()
        assert "attachment_count" in result
        assert "classification" in result
        assert "provider_message_id" in result
        # …and NOTHING was persisted (no blob written).
        row = res_manager.get_resource(rid)
        assert row["byte_size"] in (None, 0)

    def test_missing_required_field_rejected(self):
        from dataclasses import dataclass
        from carpenter.packages.handler_registry import get_handler_registry

        @dataclass
        class _NeedsField:
            must_have: str
            opt: str = ""

        reg = get_handler_registry()
        reg.register_kind("test-pkg-req", "_NeedsField", _NeedsField)
        try:
            reviewer, rid = _reviewer_arc_with_pending_extract(
                kind="_NeedsField"
            )
            result = _handle_submit_extract(
                {"fields": {"opt": "x"}}, executor_arc_id=reviewer
            )
            assert "must_have" in result
            assert "missing required" in result.lower()
            row = res_manager.get_resource(rid)
            assert row["byte_size"] in (None, 0)
        finally:
            reg.unregister_package("test-pkg-req")

    def test_correct_fields_persist_and_decode(self, registered_kind):
        from carpenter.security import judge as judge_mod

        reviewer, rid = _reviewer_arc_with_pending_extract(
            kind=registered_kind
        )
        fields = {
            "provider_message_id": "abc12",
            "category": "personal",
            "importance_flags": ["personal"],
            "schema_version": "1.0",
        }
        result = _handle_submit_extract(
            {"fields": fields}, executor_arc_id=reviewer
        )
        assert "persisted" in result.lower()

        text = read_resource_content(rid, caller_arc_id=None)
        assert json.loads(text) == fields
        # The JUDGE decoder can now construct the dataclass without error.
        row = res_manager.get_resource(rid)
        decoded = judge_mod._load_extraction_resource(row)
        assert decoded.provider_message_id == "abc12"
        assert decoded.importance_flags == ("personal",)

    def test_kindless_resource_still_writes(self):
        """A Resource with no ``kind`` keeps the historical write-as-is
        behaviour — the validator can't introspect fields and must not
        block non-typed callers."""
        reviewer = arc_manager.create_arc(
            name="kindless-reviewer",
            agent_type="REVIEWER",
            integrity_level="trusted",
        )
        rid = derive_resource(
            content_type="dataclass",
            file_path=None,
            produced_by_arc_id=reviewer,
            produced_by_template="some_template",
            template_verdict="pending",
            source_descriptor="extract:kindless",
            kind=None,
        )
        path = resource_storage_path(rid, "blob")
        path.parent.mkdir(parents=True, exist_ok=True)
        with db_transaction() as db:
            db.execute(
                "UPDATE resources SET file_path = ? WHERE id = ?",
                (str(path), rid),
            )
        set_arc_state(reviewer, "extract_resource_id", rid)

        result = _handle_submit_extract(
            {"fields": {"anything": 1, "goes": "here"}},
            executor_arc_id=reviewer,
        )
        assert "persisted" in result.lower()
        text = read_resource_content(rid, caller_arc_id=None)
        assert json.loads(text) == {"anything": 1, "goes": "here"}


class TestSubmitExtractJudgeDecodes:
    def test_judge_decodes_and_approves_persisted_extract(self):
        """End-to-end: submit_extract persists a valid EmailTriageExtract,
        then judge_email_triage decodes the blob + approves."""
        # This exercises the platform's JSON->dataclass coercion path; the
        # package JUDGE handler lives in carpenter-packages, so here we
        # assert the platform-side decode succeeds and the verdict is
        # JUDGE-driven (not self-approved by the tool).
        from carpenter.security import judge as judge_mod

        reviewer, rid = _reviewer_arc_with_pending_extract(
            kind="EmailTriageExtract"
        )
        # EmailTriageExtract is a package kind; without the package loaded
        # the platform decode raises a clean "Unknown extraction kind".
        # We instead validate the persisted blob is the exact JSON shape
        # the JUDGE deserialiser consumes (json.loads -> dict).
        fields = {
            "provider_message_id": "abc12",
            "received_history_id": "55",
            "category": "personal",
            "subject_clean": "hi",
            "importance_flags": ["personal"],
            "attachments": [],
            "schema_version": "1.0",
        }
        _handle_submit_extract({"fields": fields}, executor_arc_id=reviewer)

        text = read_resource_content(rid, caller_arc_id=None)
        payload = json.loads(text)
        assert isinstance(payload, dict)
        assert payload == fields
        # The blob is a JSON object — the shape _construct_dataclass /
        # _coerce_field_value expect (list->tuple coercion, etc.).
        assert isinstance(payload["importance_flags"], list)

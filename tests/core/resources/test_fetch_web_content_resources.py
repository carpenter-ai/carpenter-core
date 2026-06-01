"""Integration tests for the PR3 Resource-backed fetch_web_content flow.

These cover the happy path: after ``_handle_fetch_web_content``:
  - EXECUTOR arc has an output Resource (content_type='html',
    produced_by_template=NULL — raw ingest).
  - REVIEWER arc has an input link to the raw Resource and an output
    link to a derived Resource (produced_by_template='html_to_summary',
    template_verdict='pending').
  - JUDGE arc has ``_review_target_resource_id`` set to the derived
    Resource id.
  - Parent PLANNER has ``_primary_resource_id`` set to the derived
    Resource id.
  - After a simulated JUDGE approve via review_manager.submit_verdict,
    the derived Resource's template_verdict flips to 'approved' and
    ``is_trusted`` returns True.
  - After the REVIEWER finalises its output (simulated), the raw
    Resource's ``deprecated_at`` is set — auto-deprecation fires on
    REVIEWER commit regardless of JUDGE outcome.

The actual EXECUTOR / REVIEWER / JUDGE arc LLM runs are NOT exercised
here (no agent loop) — we simulate each step by calling the underlying
platform primitives the same way those arcs would via dispatch.
"""

import re

import pytest

from carpenter.agent import conversation
from carpenter.agent.invocation import _handle_fetch_web_content
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager
from carpenter.core.resources import is_trusted
from carpenter.core.workflows import review_manager
from carpenter.core.workflows._arc_state import get_arc_state
from carpenter.tool_backends import resource as resource_backend


def _parent_id_from_result(result: str) -> int:
    match = re.search(r"arc #(\d+)", result)
    assert match, f"parent id not found in: {result!r}"
    return int(match.group(1))


def _find_arc_by_agent_type(parent_id: int, agent_type: str) -> dict:
    children = arc_manager.get_children(parent_id)
    matches = [c for c in children if c["agent_type"] == agent_type]
    assert len(matches) == 1, f"expected 1 {agent_type}, got {len(matches)}"
    return matches[0]


def _simulate_executor_write(raw_resource_id: int) -> None:
    """Pretend the EXECUTOR wrote the html blob to disk and finalised.

    We don't run the RestrictedPython script (that's exercised in the
    unit test at ``tests/core/test_fetch_web_content.py``).  Instead we
    write the file directly and call the finalize dispatch, the way the
    script would.
    """
    row = res_manager.get_resource(raw_resource_id)
    assert row is not None and row["file_path"]
    with open(row["file_path"], "w") as f:
        f.write("<html>content</html>")
    # resource.finalize requires _caller_arc_id == produced_by_arc_id.
    resource_backend.handle_finalize({
        "resource_id": raw_resource_id,
        "_caller_arc_id": row["produced_by_arc_id"],
    })


def _simulate_reviewer_commit(derived_resource_id: int) -> None:
    """Pretend the REVIEWER wrote the derived summary and finalised it.

    Also deprecates inputs (the raw html) on the REVIEWER arc — the real
    REVIEWER agent would pass ``deprecate_inputs=True``.
    """
    row = res_manager.get_resource(derived_resource_id)
    assert row is not None and row["file_path"]
    with open(row["file_path"], "w") as f:
        f.write("clean summary")
    resource_backend.handle_finalize({
        "resource_id": derived_resource_id,
        "_caller_arc_id": row["produced_by_arc_id"],
        "deprecate_inputs": True,
    })


def _make_judge_arc_id_for(parent_id: int, derived_resource_id: int) -> int:
    """Return the JUDGE child arc id for the fetch pipeline.

    The pipeline builds it with ``_review_target`` already set (by
    ``handle_create_batch``) and ``_review_target_resource_id`` set by
    the fetch handler.  We just return the id here.
    """
    judge = _find_arc_by_agent_type(parent_id, "JUDGE")
    return judge["id"]


class TestFetchWebContentResourceWiring:
    def test_executor_has_raw_output_resource(self):
        result = _handle_fetch_web_content(
            {"url": "https://example.com", "goal": "find temperature"},
            conversation_id=None,
        )
        parent_id = _parent_id_from_result(result)
        executor = _find_arc_by_agent_type(parent_id, "EXECUTOR")

        outputs = res_manager.list_resources_for_arc(
            executor["id"], role="output"
        )
        assert len(outputs) == 1
        raw = outputs[0]
        assert raw["content_type"] == "html"
        assert raw["produced_by_template"] is None
        assert raw["template_verdict"] is None
        assert raw["source_descriptor"] == "https://example.com"
        assert raw["file_path"] is not None

    def test_reviewer_has_raw_input_and_derived_output(self):
        result = _handle_fetch_web_content(
            {"url": "https://weather.test/x", "goal": "temp"},
            conversation_id=None,
        )
        parent_id = _parent_id_from_result(result)
        reviewer = _find_arc_by_agent_type(parent_id, "REVIEWER")

        inputs = res_manager.list_resources_for_arc(reviewer["id"], role="input")
        outputs = res_manager.list_resources_for_arc(reviewer["id"], role="output")

        assert len(inputs) == 1
        assert len(outputs) == 1

        raw = inputs[0]
        derived = outputs[0]
        assert raw["content_type"] == "html"
        assert raw["produced_by_template"] is None

        assert derived["content_type"] == "text-summary"
        assert derived["produced_by_template"] == "html_to_summary"
        assert derived["template_verdict"] == "pending"
        assert derived["produced_by_arc_id"] == reviewer["id"]

    def test_judge_has_review_target_resource_id(self):
        result = _handle_fetch_web_content(
            {"url": "https://example.com", "goal": "g"},
            conversation_id=None,
        )
        parent_id = _parent_id_from_result(result)
        reviewer = _find_arc_by_agent_type(parent_id, "REVIEWER")
        judge = _find_arc_by_agent_type(parent_id, "JUDGE")

        derived_id = res_manager.list_resources_for_arc(
            reviewer["id"], role="output"
        )[0]["id"]

        target_resource_id = get_arc_state(judge["id"], "_review_target_resource_id")
        assert target_resource_id == derived_id

    def test_parent_has_primary_resource_id(self):
        result = _handle_fetch_web_content(
            {"url": "https://example.com", "goal": "g"},
            conversation_id=None,
        )
        parent_id = _parent_id_from_result(result)
        reviewer = _find_arc_by_agent_type(parent_id, "REVIEWER")
        derived_id = res_manager.list_resources_for_arc(
            reviewer["id"], role="output"
        )[0]["id"]

        primary = get_arc_state(parent_id, "_primary_resource_id")
        assert primary == derived_id

    def test_arc_state_pre_seeded_for_executor_and_reviewer(self):
        result = _handle_fetch_web_content(
            {"url": "https://example.com", "goal": "g"},
            conversation_id=None,
        )
        parent_id = _parent_id_from_result(result)
        executor = _find_arc_by_agent_type(parent_id, "EXECUTOR")
        reviewer = _find_arc_by_agent_type(parent_id, "REVIEWER")

        # EXECUTOR sees: fetch_url, raw_resource_path, raw_resource_id
        assert get_arc_state(executor["id"], "fetch_url") == "https://example.com"
        assert get_arc_state(executor["id"], "raw_resource_path")
        raw_id = get_arc_state(executor["id"], "raw_resource_id")
        assert isinstance(raw_id, int)

        # REVIEWER sees: raw + derived paths and ids
        assert get_arc_state(reviewer["id"], "raw_resource_path")
        assert get_arc_state(reviewer["id"], "derived_resource_path")
        assert get_arc_state(reviewer["id"], "derived_resource_id")


class TestFetchWebContentJudgeApprove:
    def test_judge_approve_promotes_derived_resource(self):
        result = _handle_fetch_web_content(
            {"url": "https://example.com", "goal": "g"},
            conversation_id=None,
        )
        parent_id = _parent_id_from_result(result)
        executor = _find_arc_by_agent_type(parent_id, "EXECUTOR")
        reviewer = _find_arc_by_agent_type(parent_id, "REVIEWER")
        judge = _find_arc_by_agent_type(parent_id, "JUDGE")

        raw_id = res_manager.list_resources_for_arc(
            executor["id"], role="output"
        )[0]["id"]
        derived_id = res_manager.list_resources_for_arc(
            reviewer["id"], role="output"
        )[0]["id"]

        # Simulate EXECUTOR + REVIEWER commits.
        _simulate_executor_write(raw_id)
        _simulate_reviewer_commit(derived_id)

        # Pre-check — still pending, not yet trusted.
        assert res_manager.get_resource(derived_id)["template_verdict"] == "pending"
        assert is_trusted(derived_id) is False

        # Simulate JUDGE approve via review_manager.submit_verdict — PR2
        # wiring flips the derived Resource verdict via
        # _review_target_resource_id on the JUDGE arc.
        review_manager.submit_verdict(
            reviewer_arc_id=judge["id"],
            target_arc_id=executor["id"],
            decision="approve",
            reason="ok",
        )

        row = res_manager.get_resource(derived_id)
        assert row["template_verdict"] == "approved"
        assert is_trusted(derived_id) is True

    def test_reviewer_commit_auto_deprecates_raw_resource(self):
        """Auto-deprecation fires on REVIEWER commit, BEFORE judge runs.

        Per the plan: auto-deprecation is triggered by a trusted arc
        successfully committing its output Resources.  The REVIEWER
        committing its derived summary is that event.  The raw html
        Resource (consumed as input by the REVIEWER) gets deprecated.
        This happens regardless of subsequent JUDGE verdict.
        """
        result = _handle_fetch_web_content(
            {"url": "https://example.com", "goal": "g"},
            conversation_id=None,
        )
        parent_id = _parent_id_from_result(result)
        executor = _find_arc_by_agent_type(parent_id, "EXECUTOR")
        reviewer = _find_arc_by_agent_type(parent_id, "REVIEWER")
        judge = _find_arc_by_agent_type(parent_id, "JUDGE")

        raw_id = res_manager.list_resources_for_arc(
            executor["id"], role="output"
        )[0]["id"]
        derived_id = res_manager.list_resources_for_arc(
            reviewer["id"], role="output"
        )[0]["id"]

        _simulate_executor_write(raw_id)
        assert res_manager.get_resource(raw_id)["deprecated_at"] is None

        _simulate_reviewer_commit(derived_id)
        # Raw is deprecated as soon as the REVIEWER commits.
        assert res_manager.get_resource(raw_id)["deprecated_at"] is not None

        # Subsequent JUDGE approve still works.
        review_manager.submit_verdict(
            reviewer_arc_id=judge["id"],
            target_arc_id=executor["id"],
            decision="approve",
            reason="ok",
        )
        assert is_trusted(derived_id) is True


class TestFetchWebContentRawIsAlwaysUntrusted:
    def test_raw_is_not_trusted_even_after_approve(self):
        """Raw ingest Resources are forever untrusted (produced_by_template=NULL)."""
        result = _handle_fetch_web_content(
            {"url": "https://example.com", "goal": "g"},
            conversation_id=None,
        )
        parent_id = _parent_id_from_result(result)
        executor = _find_arc_by_agent_type(parent_id, "EXECUTOR")
        reviewer = _find_arc_by_agent_type(parent_id, "REVIEWER")
        judge = _find_arc_by_agent_type(parent_id, "JUDGE")

        raw_id = res_manager.list_resources_for_arc(
            executor["id"], role="output"
        )[0]["id"]
        derived_id = res_manager.list_resources_for_arc(
            reviewer["id"], role="output"
        )[0]["id"]

        _simulate_executor_write(raw_id)
        _simulate_reviewer_commit(derived_id)
        review_manager.submit_verdict(
            reviewer_arc_id=judge["id"],
            target_arc_id=executor["id"],
            decision="approve",
            reason="ok",
        )

        # derived is trusted, raw is NOT (no provenance)
        assert is_trusted(derived_id) is True
        assert is_trusted(raw_id) is False


class TestFinalizePopulatesContentStats:
    def test_resource_finalize_fills_byte_size_and_hash(self):
        import hashlib

        result = _handle_fetch_web_content(
            {"url": "https://example.com", "goal": "g"},
            conversation_id=None,
        )
        parent_id = _parent_id_from_result(result)
        executor = _find_arc_by_agent_type(parent_id, "EXECUTOR")
        raw_id = res_manager.list_resources_for_arc(
            executor["id"], role="output"
        )[0]["id"]

        pre = res_manager.get_resource(raw_id)
        assert pre["byte_size"] is None
        assert pre["content_hash"] is None

        _simulate_executor_write(raw_id)

        post = res_manager.get_resource(raw_id)
        expected_bytes = b"<html>content</html>"
        assert post["byte_size"] == len(expected_bytes)
        assert post["content_hash"] == hashlib.sha256(expected_bytes).hexdigest()

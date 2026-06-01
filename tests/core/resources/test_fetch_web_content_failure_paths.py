"""Failure-path tests for the PR3 Resource-backed fetch_web_content flow.

Covers:
  - JUDGE rejects: derived Resource verdict -> 'rejected',
    is_trusted(derived) -> False.
  - Auto-deprecation of the raw Resource ALREADY happened at REVIEWER
    commit time (before the judge ran).  Per user decision, deprecation
    is tied to "trusted arc successfully commits its output Resources",
    which is the REVIEWER commit event.  JUDGE rejection does NOT undo
    that deprecation.
  - resource.finalize rejects callers that aren't the Resource's producer.
"""

import re

import pytest

from carpenter.agent.invocation import _handle_fetch_web_content
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import is_trusted
from carpenter.core.resources import manager as res_manager
from carpenter.core.workflows import review_manager
from carpenter.tool_backends import resource as resource_backend


def _parent_id_from_result(result: str) -> int:
    match = re.search(r"arc #(\d+)", result)
    assert match
    return int(match.group(1))


def _find_arc_by_agent_type(parent_id: int, agent_type: str) -> dict:
    children = arc_manager.get_children(parent_id)
    matches = [c for c in children if c["agent_type"] == agent_type]
    assert len(matches) == 1
    return matches[0]


def _simulate_executor_write(raw_id: int) -> None:
    row = res_manager.get_resource(raw_id)
    with open(row["file_path"], "w") as f:
        f.write("<html>bad</html>")
    resource_backend.handle_finalize({
        "resource_id": raw_id,
        "_caller_arc_id": row["produced_by_arc_id"],
    })


def _simulate_reviewer_commit(derived_id: int) -> None:
    row = res_manager.get_resource(derived_id)
    with open(row["file_path"], "w") as f:
        f.write("summary")
    resource_backend.handle_finalize({
        "resource_id": derived_id,
        "_caller_arc_id": row["produced_by_arc_id"],
        "deprecate_inputs": True,
    })


class TestJudgeReject:
    def test_judge_reject_leaves_derived_untrusted(self):
        result = _handle_fetch_web_content(
            {"url": "https://example.com", "goal": "g"},
            conversation_id=None,
        )
        parent_id = _parent_id_from_result(result)
        executor = _find_arc_by_agent_type(parent_id, "EXECUTOR")
        reviewer = _find_arc_by_agent_type(parent_id, "REVIEWER")
        judge = _find_arc_by_agent_type(parent_id, "JUDGE")

        raw_id = res_manager.list_resources_for_arc(executor["id"], role="output")[0]["id"]
        derived_id = res_manager.list_resources_for_arc(reviewer["id"], role="output")[0]["id"]

        _simulate_executor_write(raw_id)
        _simulate_reviewer_commit(derived_id)

        review_manager.submit_verdict(
            reviewer_arc_id=judge["id"],
            target_arc_id=executor["id"],
            decision="reject",
            reason="unsafe",
        )

        row = res_manager.get_resource(derived_id)
        assert row["template_verdict"] == "rejected"
        assert is_trusted(derived_id) is False

    def test_judge_reject_does_not_undo_raw_deprecation(self):
        """Per plan: auto-deprecation fires on REVIEWER commit, not on
        JUDGE verdict.  A subsequent JUDGE reject does NOT restore the
        raw Resource — it stays deprecated.
        """
        result = _handle_fetch_web_content(
            {"url": "https://example.com", "goal": "g"},
            conversation_id=None,
        )
        parent_id = _parent_id_from_result(result)
        executor = _find_arc_by_agent_type(parent_id, "EXECUTOR")
        reviewer = _find_arc_by_agent_type(parent_id, "REVIEWER")
        judge = _find_arc_by_agent_type(parent_id, "JUDGE")

        raw_id = res_manager.list_resources_for_arc(executor["id"], role="output")[0]["id"]
        derived_id = res_manager.list_resources_for_arc(reviewer["id"], role="output")[0]["id"]

        _simulate_executor_write(raw_id)
        _simulate_reviewer_commit(derived_id)

        # Deprecated at commit time, pre-judge.
        assert res_manager.get_resource(raw_id)["deprecated_at"] is not None

        review_manager.submit_verdict(
            reviewer_arc_id=judge["id"],
            target_arc_id=executor["id"],
            decision="reject",
            reason="nope",
        )

        # Still deprecated.
        assert res_manager.get_resource(raw_id)["deprecated_at"] is not None


class TestFinalizeAuth:
    def test_finalize_rejects_non_producer_arc(self):
        """resource.finalize enforces _caller_arc_id == produced_by_arc_id."""
        result = _handle_fetch_web_content(
            {"url": "https://example.com", "goal": "g"},
            conversation_id=None,
        )
        parent_id = _parent_id_from_result(result)
        executor = _find_arc_by_agent_type(parent_id, "EXECUTOR")
        reviewer = _find_arc_by_agent_type(parent_id, "REVIEWER")

        raw_id = res_manager.list_resources_for_arc(executor["id"], role="output")[0]["id"]
        row = res_manager.get_resource(raw_id)
        with open(row["file_path"], "w") as f:
            f.write("<html>x</html>")

        # REVIEWER tries to finalise the raw Resource (which was
        # produced by the EXECUTOR).  Must refuse.
        with pytest.raises(PermissionError):
            resource_backend.handle_finalize({
                "resource_id": raw_id,
                "_caller_arc_id": reviewer["id"],
            })

    def test_finalize_requires_file_path_set(self):
        """A Resource with no file_path cannot be finalised."""
        arc = arc_manager.create_arc(name="x")
        rid = res_manager.create_resource(
            content_type="html",
            file_path=None,
            produced_by_arc_id=arc,
        )
        with pytest.raises(ValueError, match="no file_path"):
            resource_backend.handle_finalize({
                "resource_id": rid,
                "_caller_arc_id": arc,
            })

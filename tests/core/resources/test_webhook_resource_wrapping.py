"""Tests for the Phase B PR B2 webhook -> Resource wrap pipeline.

These tests cover the happy path: a webhook subscription whose
``resource_content_type`` is set wraps the incoming payload as a raw-
ingest Resource, spawns REVIEWER (+JUDGE) arcs from the matching
template, links the Resources, and seeds the right arc state.
"""

from __future__ import annotations

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager
from carpenter.core.resources import is_trusted
from carpenter.core.workflows import review_manager
from carpenter.core.workflows import webhook_dispatch_handler as handler
from carpenter.core.workflows._arc_state import get_arc_state


def _find_arc_by_agent_type(parent_id: int, agent_type: str) -> dict:
    children = arc_manager.get_children(parent_id) or []
    matches = [c for c in children if c["agent_type"] == agent_type]
    assert len(matches) == 1, (
        f"expected 1 {agent_type} arc under parent {parent_id}, "
        f"got {len(matches)}"
    )
    return matches[0]


def _find_arcs_by_agent_type(parent_id: int, agent_type: str) -> list[dict]:
    children = arc_manager.get_children(parent_id) or []
    return [c for c in children if c["agent_type"] == agent_type]


def _get_parent_ids_for_reviewer(reviewer_arc_id: int) -> int:
    """Return the parent_id of a reviewer arc."""
    arc = arc_manager.get_arc(reviewer_arc_id)
    assert arc is not None
    return arc["parent_id"]


# ---------------------------------------------------------------------------
# Legacy path (resource_content_type NULL) is unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_path_no_resource_created():
    """Subscription without resource_content_type keeps legacy behaviour.

    The handler enqueues a work item (or creates an arc per action_type)
    but does NOT create any Resource rows.
    """
    handler.create_subscription(
        webhook_id="legacy-hook",
        source_type="generic",
        action_type="enqueue_work",
        action_config={
            "event_type": "custom.action",
            "payload": {"hello": "world"},
        },
    )

    # Snapshot resource count before.
    from carpenter.db import db_connection
    with db_connection() as db:
        before = db.execute("SELECT COUNT(*) FROM resources").fetchone()[0]

    await handler.handle_webhook_received(1, {
        "webhook_id": "legacy-hook",
        "data": {"payload": "data"},
    })

    with db_connection() as db:
        after = db.execute("SELECT COUNT(*) FROM resources").fetchone()[0]

    assert before == after, "legacy webhook path must not create Resources"


# ---------------------------------------------------------------------------
# Resource wrap path (resource_content_type set)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrap_creates_raw_resource_with_payload():
    """The raw Resource row holds the webhook payload bytes on disk."""
    handler.create_subscription(
        webhook_id="wrap-hook-1",
        source_type="generic",
        action_type="enqueue_work",
        resource_content_type="json-webhook",
    )

    await handler.handle_webhook_received(1, {
        "webhook_id": "wrap-hook-1",
        "data": {"event": "test", "value": 42},
    })

    # Locate the reviewer arc by agent_type and its parent PLANNER.
    from carpenter.db import db_connection
    with db_connection() as db:
        reviewers = db.execute(
            "SELECT id, parent_id FROM arcs WHERE agent_type = 'REVIEWER' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchall()
    assert len(reviewers) == 1
    reviewer_id = reviewers[0]["id"]

    outputs = res_manager.list_resources_for_arc(reviewer_id, role="output")
    inputs = res_manager.list_resources_for_arc(reviewer_id, role="input")

    assert len(inputs) == 1
    assert len(outputs) == 1

    raw = inputs[0]
    derived = outputs[0]

    assert raw["content_type"] == "json-webhook"
    # Raw-ingest: produced_by_template NULL, template_verdict NULL.
    assert raw["produced_by_template"] is None
    assert raw["template_verdict"] is None
    # Per the B2 design the REVIEWER arc owns the raw Resource.
    assert raw["produced_by_arc_id"] == reviewer_id

    # The payload was written to disk.
    assert raw["file_path"]
    with open(raw["file_path"], "rb") as f:
        contents = f.read()
    import json as _json
    assert _json.loads(contents) == {"event": "test", "value": 42}
    assert raw["byte_size"] == len(contents)

    # Derived Resource: pending from the json_webhook_to_summary template.
    assert derived["content_type"] == "json-summary"
    assert derived["produced_by_template"] == "json_webhook_to_summary"
    assert derived["template_verdict"] == "pending"
    assert derived["produced_by_arc_id"] == reviewer_id


@pytest.mark.asyncio
async def test_wrap_spawns_reviewer_and_judge_arcs():
    """Default (auto_approve_verdict=0) spawns both REVIEWER and JUDGE."""
    handler.create_subscription(
        webhook_id="wrap-hook-rj",
        source_type="generic",
        action_type="enqueue_work",
        resource_content_type="json-webhook",
    )

    await handler.handle_webhook_received(1, {
        "webhook_id": "wrap-hook-rj",
        "data": {"payload": "x"},
    })

    from carpenter.db import db_connection
    with db_connection() as db:
        reviewer = db.execute(
            "SELECT id, parent_id FROM arcs WHERE agent_type = 'REVIEWER' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert reviewer is not None
    parent_id = reviewer["parent_id"]

    reviewers = _find_arcs_by_agent_type(parent_id, "REVIEWER")
    judges = _find_arcs_by_agent_type(parent_id, "JUDGE")

    assert len(reviewers) == 1
    assert len(judges) == 1


@pytest.mark.asyncio
async def test_wrap_seeds_parent_primary_resource_id():
    """Parent PLANNER gets _primary_resource_id = derived Resource id."""
    handler.create_subscription(
        webhook_id="wrap-hook-primary",
        source_type="generic",
        action_type="enqueue_work",
        resource_content_type="json-webhook",
    )

    await handler.handle_webhook_received(1, {
        "webhook_id": "wrap-hook-primary",
        "data": {"x": 1},
    })

    from carpenter.db import db_connection
    with db_connection() as db:
        reviewer = db.execute(
            "SELECT id, parent_id FROM arcs WHERE agent_type = 'REVIEWER' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    parent_id = reviewer["parent_id"]

    derived_id = res_manager.list_resources_for_arc(
        reviewer["id"], role="output"
    )[0]["id"]

    primary = get_arc_state(parent_id, "_primary_resource_id")
    assert primary == derived_id


@pytest.mark.asyncio
async def test_wrap_seeds_judge_review_target_resource_id():
    """JUDGE arc has _review_target_resource_id = derived id."""
    handler.create_subscription(
        webhook_id="wrap-hook-judge",
        source_type="generic",
        action_type="enqueue_work",
        resource_content_type="json-webhook",
    )

    await handler.handle_webhook_received(1, {
        "webhook_id": "wrap-hook-judge",
        "data": {"x": 1},
    })

    from carpenter.db import db_connection
    with db_connection() as db:
        reviewer = db.execute(
            "SELECT id, parent_id FROM arcs WHERE agent_type = 'REVIEWER' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    parent_id = reviewer["parent_id"]

    judge = _find_arc_by_agent_type(parent_id, "JUDGE")
    derived_id = res_manager.list_resources_for_arc(
        reviewer["id"], role="output"
    )[0]["id"]

    assert get_arc_state(judge["id"], "_review_target_resource_id") == derived_id


@pytest.mark.asyncio
async def test_judge_approve_promotes_derived_resource_trust():
    """After JUDGE approve via review_manager, derived Resource is trusted."""
    handler.create_subscription(
        webhook_id="wrap-hook-approve",
        source_type="generic",
        action_type="enqueue_work",
        resource_content_type="json-webhook",
    )

    await handler.handle_webhook_received(1, {
        "webhook_id": "wrap-hook-approve",
        "data": {"x": 1},
    })

    from carpenter.db import db_connection
    with db_connection() as db:
        reviewer = db.execute(
            "SELECT id, parent_id FROM arcs WHERE agent_type = 'REVIEWER' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    parent_id = reviewer["parent_id"]
    judge = _find_arc_by_agent_type(parent_id, "JUDGE")

    derived_id = res_manager.list_resources_for_arc(
        reviewer["id"], role="output"
    )[0]["id"]

    # Pre-check: pending, not trusted.
    assert res_manager.get_resource(derived_id)["template_verdict"] == "pending"
    assert is_trusted(derived_id) is False

    # Simulate JUDGE approve.
    review_manager.submit_verdict(
        reviewer_arc_id=judge["id"],
        target_arc_id=reviewer["id"],
        decision="approve",
        reason="ok",
    )

    assert res_manager.get_resource(derived_id)["template_verdict"] == "approved"
    assert is_trusted(derived_id) is True


@pytest.mark.asyncio
async def test_judge_reject_marks_derived_resource_rejected():
    """JUDGE reject flips derived template_verdict to 'rejected'."""
    handler.create_subscription(
        webhook_id="wrap-hook-reject",
        source_type="generic",
        action_type="enqueue_work",
        resource_content_type="json-webhook",
    )

    await handler.handle_webhook_received(1, {
        "webhook_id": "wrap-hook-reject",
        "data": {"x": 1},
    })

    from carpenter.db import db_connection
    with db_connection() as db:
        reviewer = db.execute(
            "SELECT id, parent_id FROM arcs WHERE agent_type = 'REVIEWER' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    parent_id = reviewer["parent_id"]
    judge = _find_arc_by_agent_type(parent_id, "JUDGE")

    derived_id = res_manager.list_resources_for_arc(
        reviewer["id"], role="output"
    )[0]["id"]

    review_manager.submit_verdict(
        reviewer_arc_id=judge["id"],
        target_arc_id=reviewer["id"],
        decision="reject",
        reason="bad",
    )

    assert res_manager.get_resource(derived_id)["template_verdict"] == "rejected"
    assert is_trusted(derived_id) is False


@pytest.mark.asyncio
async def test_wrap_arc_state_carries_webhook_context():
    """Reviewer arc state has webhook_id, parsed_event, and resource paths."""
    handler.create_subscription(
        webhook_id="wrap-hook-state",
        source_type="generic",
        action_type="enqueue_work",
        resource_content_type="json-webhook",
    )

    await handler.handle_webhook_received(1, {
        "webhook_id": "wrap-hook-state",
        "data": {"key": "value"},
    })

    from carpenter.db import db_connection
    with db_connection() as db:
        reviewer = db.execute(
            "SELECT id FROM arcs WHERE agent_type = 'REVIEWER' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert get_arc_state(reviewer["id"], "webhook_id") == "wrap-hook-state"
    assert get_arc_state(reviewer["id"], "raw_resource_path")
    assert get_arc_state(reviewer["id"], "derived_resource_path")
    assert get_arc_state(reviewer["id"], "raw_resource_id")
    assert get_arc_state(reviewer["id"], "derived_resource_id")
    parsed = get_arc_state(reviewer["id"], "parsed_event")
    assert parsed is not None
    assert parsed["source_type"] == "generic"

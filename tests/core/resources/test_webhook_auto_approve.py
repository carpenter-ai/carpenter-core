"""Tests for the ``auto_approve_verdict`` webhook subscription override.

When a webhook subscription has ``auto_approve_verdict=1``:
  - No JUDGE arc is spawned; only a REVIEWER.
  - When the REVIEWER transitions to ``completed``, the derived
    Resource's ``template_verdict`` is auto-marked 'approved' via the
    ``webhook_resource_wrap.apply_auto_approve_on_completion`` hook
    wired into ``arcs.manager.update_status``.

When ``auto_approve_verdict=0`` (the default, honouring "nothing starts
trusted"):
  - A JUDGE arc is spawned.
  - Marking the REVIEWER complete does NOT promote the derived
    Resource's trust.
"""

from __future__ import annotations

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager
from carpenter.core.resources import is_trusted
from carpenter.core.workflows import webhook_dispatch_handler as handler


def _find_arcs_by_agent_type(parent_id: int, agent_type: str) -> list[dict]:
    children = arc_manager.get_children(parent_id) or []
    return [c for c in children if c["agent_type"] == agent_type]


@pytest.mark.asyncio
async def test_auto_approve_true_skips_judge():
    """With auto_approve_verdict=1, no JUDGE arc is spawned."""
    handler.create_subscription(
        webhook_id="auto-approve-hook",
        source_type="generic",
        action_type="enqueue_work",
        resource_content_type="json-webhook",
        auto_approve_verdict=True,
    )

    await handler.handle_webhook_received(1, {
        "webhook_id": "auto-approve-hook",
        "data": {"x": 1},
    })

    from carpenter.db import db_connection
    with db_connection() as db:
        reviewer = db.execute(
            "SELECT id, parent_id FROM arcs WHERE agent_type = 'REVIEWER' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    parent_id = reviewer["parent_id"]

    reviewers = _find_arcs_by_agent_type(parent_id, "REVIEWER")
    judges = _find_arcs_by_agent_type(parent_id, "JUDGE")

    assert len(reviewers) == 1
    assert len(judges) == 0


@pytest.mark.asyncio
async def test_auto_approve_completes_reviewer_marks_derived_approved():
    """REVIEWER 'completed' transition flips derived verdict to 'approved'."""
    handler.create_subscription(
        webhook_id="auto-approve-trigger",
        source_type="generic",
        action_type="enqueue_work",
        resource_content_type="json-webhook",
        auto_approve_verdict=True,
    )

    await handler.handle_webhook_received(1, {
        "webhook_id": "auto-approve-trigger",
        "data": {"x": 1},
    })

    from carpenter.db import db_connection
    with db_connection() as db:
        reviewer = db.execute(
            "SELECT id FROM arcs WHERE agent_type = 'REVIEWER' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

    derived_id = res_manager.list_resources_for_arc(
        reviewer["id"], role="output"
    )[0]["id"]

    # Pre-check: pending.
    assert res_manager.get_resource(derived_id)["template_verdict"] == "pending"
    assert is_trusted(derived_id) is False

    # Transition REVIEWER to completed (pending -> active -> completed).
    arc_manager.update_status(reviewer["id"], "active")
    arc_manager.update_status(reviewer["id"], "completed")

    # The post-transition hook should have flipped verdict to 'approved'.
    row = res_manager.get_resource(derived_id)
    assert row["template_verdict"] == "approved"
    assert is_trusted(derived_id) is True


@pytest.mark.asyncio
async def test_no_auto_approve_default_still_spawns_judge():
    """Default (auto_approve_verdict=False) spawns JUDGE; REVIEWER completion does NOT promote trust."""
    handler.create_subscription(
        webhook_id="no-auto-approve",
        source_type="generic",
        action_type="enqueue_work",
        resource_content_type="json-webhook",
        # auto_approve_verdict left default (False)
    )

    await handler.handle_webhook_received(1, {
        "webhook_id": "no-auto-approve",
        "data": {"x": 1},
    })

    from carpenter.db import db_connection
    with db_connection() as db:
        reviewer = db.execute(
            "SELECT id, parent_id FROM arcs WHERE agent_type = 'REVIEWER' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    parent_id = reviewer["parent_id"]

    # Sanity: JUDGE is present in the default path.
    judges = _find_arcs_by_agent_type(parent_id, "JUDGE")
    assert len(judges) == 1

    derived_id = res_manager.list_resources_for_arc(
        reviewer["id"], role="output"
    )[0]["id"]

    # Transition REVIEWER to completed — without auto-approve keys, no
    # verdict flip.
    arc_manager.update_status(reviewer["id"], "active")
    arc_manager.update_status(reviewer["id"], "completed")

    row = res_manager.get_resource(derived_id)
    # Still pending: only a JUDGE verdict promotes.
    assert row["template_verdict"] == "pending"
    assert is_trusted(derived_id) is False


@pytest.mark.asyncio
async def test_auto_approve_requires_resource_wrap():
    """auto_approve_verdict alone (without resource_content_type) is a no-op.

    Subscription stores the flag but the legacy path fires — no
    Resource is created and no auto-approve state is set.
    """
    handler.create_subscription(
        webhook_id="auto-approve-legacy",
        source_type="generic",
        action_type="enqueue_work",
        auto_approve_verdict=True,
        # resource_content_type left NULL -> legacy path
    )

    from carpenter.db import db_connection
    with db_connection() as db:
        before = db.execute("SELECT COUNT(*) FROM resources").fetchone()[0]

    await handler.handle_webhook_received(1, {
        "webhook_id": "auto-approve-legacy",
        "data": {"x": 1},
    })

    with db_connection() as db:
        after = db.execute("SELECT COUNT(*) FROM resources").fetchone()[0]

    assert before == after

"""Tests for template-aware `create_arc` subscription action (P1)."""

import json

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import event_bus, subscriptions, template_manager
from carpenter.core.workflows._arc_state import get_arc_state
from carpenter.db import get_db


SAMPLE_YAML = """\
name: demo-workflow
description: A demo template for subscription testing
steps:
  - name: fetch
    description: Fetch inputs
    order: 0
  - name: process
    description: Process data
    order: 1
"""


@pytest.fixture(autouse=True)
def _reset_subscriptions():
    subscriptions.reset()
    yield
    subscriptions.reset()


@pytest.fixture
def template_id(tmp_path):
    yaml_file = tmp_path / "demo.yaml"
    yaml_file.write_text(SAMPLE_YAML)
    return template_manager.load_template(str(yaml_file))


# ── _action_create_arc: template resolution ────────────────────────


def test_create_arc_action_with_template_name(template_id):
    subscriptions.load_subscriptions([{
        "name": "tmpl-sub",
        "on": "demo.event",
        "action": {
            "type": "create_arc",
            "template_name": "demo-workflow",
            "arc_name": "demo-run",
        },
    }])
    event_bus.record_event("demo.event", {"k": "v"})
    assert subscriptions.process_subscriptions() == 1

    db = get_db()
    try:
        rows = db.execute(
            "SELECT payload_json FROM work_queue WHERE event_type = 'subscription.create_arc'"
        ).fetchall()
    finally:
        db.close()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["template_id"] == template_id
    assert payload["template_name"] == "demo-workflow"
    assert payload["arc_name"] == "demo-run"


def test_create_arc_action_with_template_id(template_id):
    subscriptions.load_subscriptions([{
        "name": "tmpl-sub-id",
        "on": "demo.event",
        "action": {
            "type": "create_arc",
            "template_id": template_id,
        },
    }])
    event_bus.record_event("demo.event", {})
    assert subscriptions.process_subscriptions() == 1

    db = get_db()
    try:
        row = db.execute(
            "SELECT payload_json FROM work_queue WHERE event_type = 'subscription.create_arc'"
        ).fetchone()
    finally:
        db.close()
    payload = json.loads(row["payload_json"])
    assert payload["template_id"] == template_id
    assert payload["template_name"] == "demo-workflow"


def test_create_arc_action_missing_template_is_skipped():
    subscriptions.load_subscriptions([{
        "name": "bad-tmpl-sub",
        "on": "demo.event",
        "action": {
            "type": "create_arc",
            "template_name": "does-not-exist",
        },
    }])
    event_bus.record_event("demo.event", {})
    assert subscriptions.process_subscriptions() == 0

    db = get_db()
    try:
        rows = db.execute(
            "SELECT 1 FROM work_queue WHERE event_type = 'subscription.create_arc'"
        ).fetchall()
    finally:
        db.close()
    assert rows == []


def test_create_arc_action_id_name_mismatch_is_skipped(template_id):
    subscriptions.load_subscriptions([{
        "name": "mismatch-sub",
        "on": "demo.event",
        "action": {
            "type": "create_arc",
            "template_id": template_id,
            "template_name": "not-the-same-name",
        },
    }])
    event_bus.record_event("demo.event", {})
    assert subscriptions.process_subscriptions() == 0


def test_create_arc_action_without_template_preserves_legacy_behaviour():
    """No template → action still enqueues a work item (existing semantics)."""
    subscriptions.load_subscriptions([{
        "name": "legacy-sub",
        "on": "demo.event",
        "action": {
            "type": "create_arc",
            "name": "anything",
        },
    }])
    event_bus.record_event("demo.event", {})
    assert subscriptions.process_subscriptions() == 1


# ── handle_subscription_create_arc: work handler ───────────────────


def test_handle_create_arc_with_template_instantiates_steps(template_id):
    payload = {
        "template_id": template_id,
        "template_name": "demo-workflow",
        "arc_name": "demo-root",
        "arc_goal": "do the demo",
        "_subscription": "tmpl-sub",
        "_event_payload": {"pr_number": 42},
    }
    parent_id = subscriptions.handle_subscription_create_arc(payload)
    assert isinstance(parent_id, int)

    parent = arc_manager.get_arc(parent_id)
    assert parent["name"] == "demo-root"
    assert parent["goal"] == "do the demo"
    assert parent["template_id"] == template_id
    assert parent["parent_id"] is None

    # Event payload is persisted on the parent.
    assert get_arc_state(parent_id, "event_payload") == {"pr_number": 42}
    assert get_arc_state(parent_id, "subscription_name") == "tmpl-sub"

    # Template's two steps instantiated as children.
    db = get_db()
    try:
        children = db.execute(
            "SELECT name, step_order, from_template, template_id FROM arcs "
            "WHERE parent_id = ? ORDER BY step_order",
            (parent_id,),
        ).fetchall()
    finally:
        db.close()
    assert [c["name"] for c in children] == ["fetch", "process"]
    assert all(c["from_template"] for c in children)
    assert all(c["template_id"] == template_id for c in children)


def test_handle_create_arc_without_template_just_makes_parent():
    payload = {
        "arc_name": "bare",
        "_subscription": "bare-sub",
        "_event_payload": {},
    }
    parent_id = subscriptions.handle_subscription_create_arc(payload)
    parent = arc_manager.get_arc(parent_id)
    assert parent["name"] == "bare"
    assert parent["template_id"] is None

    db = get_db()
    try:
        kids = db.execute(
            "SELECT COUNT(*) AS n FROM arcs WHERE parent_id = ?",
            (parent_id,),
        ).fetchone()
    finally:
        db.close()
    assert kids["n"] == 0


def test_handle_create_arc_default_name_from_template(template_id):
    """When no arc_name is set, the template name is used."""
    parent_id = subscriptions.handle_subscription_create_arc({
        "template_id": template_id,
        "template_name": "demo-workflow",
    })
    parent = arc_manager.get_arc(parent_id)
    assert parent["name"] == "demo-workflow"

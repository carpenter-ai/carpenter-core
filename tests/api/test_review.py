"""Tests for carpenter.api.review."""
import pytest
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.testclient import TestClient

from carpenter.api import review
from carpenter.api.http import http_exception_handler
from carpenter.db import get_db
from carpenter.core.arcs import manager as arc_manager


@pytest.fixture
def client():
    app = Starlette(routes=review.routes)
    app.add_exception_handler(HTTPException, http_exception_handler)
    review.clear_reviews()
    return TestClient(app)


def _create_code_file(tmp_path):
    """Insert a code file row and write file to disk."""
    code = 'print("hello review")\n'
    code_path = str(tmp_path / "review_test.py")
    with open(code_path, "w") as f:
        f.write(code)

    db = get_db()
    try:
        cursor = db.execute(
            "INSERT INTO code_files (file_path, source) VALUES (?, ?)",
            (code_path, "test"),
        )
        code_file_id = cursor.lastrowid
        db.commit()
    finally:
        db.close()
    return code_file_id


def test_create_review_link(client, tmp_path):
    """Creating a review link returns UUID and URL."""
    code_file_id = _create_code_file(tmp_path)

    response = client.post(
        "/api/review/create",
        json={"code_file_id": code_file_id},
    )
    assert response.status_code == 200
    data = response.json()
    assert "review_id" in data
    assert data["url"].startswith("/api/review/")


def test_view_review_html(client, tmp_path):
    """Viewing a review link returns HTML with code."""
    code_file_id = _create_code_file(tmp_path)

    create_resp = client.post(
        "/api/review/create",
        json={"code_file_id": code_file_id},
    )
    review_id = create_resp.json()["review_id"]

    view_resp = client.get(f"/api/review/{review_id}")
    assert view_resp.status_code == 200
    assert "text/html" in view_resp.headers["content-type"]
    assert "hello review" in view_resp.text
    assert "Approve" in view_resp.text
    assert "Reject" in view_resp.text


def test_submit_approve_decision(client, tmp_path):
    """Approving a review updates code file and returns success."""
    code_file_id = _create_code_file(tmp_path)

    create_resp = client.post(
        "/api/review/create",
        json={"code_file_id": code_file_id},
    )
    review_id = create_resp.json()["review_id"]

    decide_resp = client.post(
        f"/api/review/{review_id}/decide",
        json={"decision": "approved", "comment": "Looks good"},
    )
    assert decide_resp.status_code == 200
    assert decide_resp.json()["decision"] == "approved"

    # Verify code file was updated
    db = get_db()
    try:
        row = db.execute(
            "SELECT review_status FROM code_files WHERE id = ?",
            (code_file_id,),
        ).fetchone()
        assert row["review_status"] == "approved"
    finally:
        db.close()


def test_review_one_time_use(client, tmp_path):
    """Review decision can only be submitted once."""
    code_file_id = _create_code_file(tmp_path)

    create_resp = client.post(
        "/api/review/create",
        json={"code_file_id": code_file_id},
    )
    review_id = create_resp.json()["review_id"]

    # First decision
    client.post(
        f"/api/review/{review_id}/decide",
        json={"decision": "approved"},
    )

    # Second decision should fail
    resp2 = client.post(
        f"/api/review/{review_id}/decide",
        json={"decision": "rejected"},
    )
    assert resp2.status_code == 410


def test_review_not_found(client):
    """Nonexistent review returns 404."""
    resp = client.get("/api/review/nonexistent-uuid")
    assert resp.status_code == 404


def test_review_with_arc_logs_history(client, tmp_path):
    """Review with arc_id logs decision to arc history."""
    code_file_id = _create_code_file(tmp_path)
    arc_id = arc_manager.create_arc("review-test")

    create_resp = client.post(
        "/api/review/create",
        json={"code_file_id": code_file_id, "arc_id": arc_id},
    )
    review_id = create_resp.json()["review_id"]

    client.post(
        f"/api/review/{review_id}/decide",
        json={"decision": "rejected", "comment": "Needs work"},
    )

    history = arc_manager.get_history(arc_id)
    review_entries = [h for h in history if h["entry_type"] == "review_decision"]
    assert len(review_entries) == 1


def test_create_review_nonexistent_code_file(client):
    """Creating a review for a code_file_id that doesn't exist returns 404."""
    response = client.post(
        "/api/review/create",
        json={"code_file_id": 999999},
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_decide_nonexistent_review(client):
    """Submitting a decision for a nonexistent review_id returns 404."""
    resp = client.post(
        "/api/review/nonexistent-uuid/decide",
        json={"decision": "approved"},
    )
    assert resp.status_code == 404


def test_view_already_used_review_returns_410(client, tmp_path):
    """Viewing a review after it has been decided returns 410."""
    code_file_id = _create_code_file(tmp_path)

    create_resp = client.post(
        "/api/review/create",
        json={"code_file_id": code_file_id},
    )
    review_id = create_resp.json()["review_id"]

    # Submit decision to mark as used
    client.post(
        f"/api/review/{review_id}/decide",
        json={"decision": "approved"},
    )

    # Now trying to view should return 410
    view_resp = client.get(f"/api/review/{review_id}")
    assert view_resp.status_code == 410


def test_submit_reject_decision(client, tmp_path):
    """Rejecting a review updates code file and returns the decision."""
    code_file_id = _create_code_file(tmp_path)

    create_resp = client.post(
        "/api/review/create",
        json={"code_file_id": code_file_id},
    )
    review_id = create_resp.json()["review_id"]

    decide_resp = client.post(
        f"/api/review/{review_id}/decide",
        json={"decision": "rejected", "comment": "Needs major rework"},
    )
    assert decide_resp.status_code == 200
    assert decide_resp.json()["decision"] == "rejected"

    # Verify code file review_status updated
    db = get_db()
    try:
        row = db.execute(
            "SELECT review_status FROM code_files WHERE id = ?",
            (code_file_id,),
        ).fetchone()
        assert row["review_status"] == "rejected"
    finally:
        db.close()


def test_create_diff_review_endpoint(client):
    """Creating a diff review via API returns review_id and url."""
    resp = client.post(
        "/api/review/create-diff",
        json={
            "diff_content": "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n",
            "title": "Test diff review",
            "changed_files": ["foo.py"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "review_id" in data
    assert data["url"].startswith("/api/review/")

    # Verify we can view the diff review
    view_resp = client.get(data["url"])
    assert view_resp.status_code == 200
    assert "text/html" in view_resp.headers["content-type"]
    assert "Test diff review" in view_resp.text


def test_diff_review_followup_creates_planner_sibling(client):
    """Approving a diff review with a followup_goal creates a sibling PLANNER arc.

    The new arc shares the current arc's parent_id (sibling), is created at
    trusted integrity, has agent_type=PLANNER, and the response includes the
    new arc id.  An arc_history entry of type 'followup_triggered' is recorded
    on the original arc.
    """
    parent_id = arc_manager.create_arc("review-followup-parent")
    current_id = arc_manager.add_child(parent_id, "current-coding-change")

    create_resp = client.post(
        "/api/review/create-diff",
        json={
            "diff_content": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
            "title": "Followup test",
            "arc_id": current_id,
            "changed_files": ["x"],
        },
    )
    review_id = create_resp.json()["review_id"]

    decide_resp = client.post(
        f"/api/review/{review_id}/decide",
        json={
            "decision": "approve",
            "comment": "looks good",
            "followup_goal": "Add a regression test for the new behavior.",
        },
    )
    assert decide_resp.status_code == 200
    body = decide_resp.json()
    assert "followup_arc_id" in body
    new_arc_id = body["followup_arc_id"]

    # New arc is a sibling: same parent_id as the current arc
    new_arc = arc_manager.get_arc(new_arc_id)
    assert new_arc is not None
    assert new_arc["parent_id"] == parent_id
    assert new_arc["agent_type"] == "PLANNER"
    assert new_arc["integrity_level"] == "trusted"
    assert new_arc["goal"] == "Add a regression test for the new behavior."
    assert new_arc["name"] == "followup-from-review"

    # arc_history of original arc records the followup
    history = arc_manager.get_history(current_id)
    followup_entries = [h for h in history if h["entry_type"] == "followup_triggered"]
    assert len(followup_entries) == 1


def test_diff_review_followup_root_arc(client):
    """If current arc is a root, follow-up arc is created as a new root."""
    current_id = arc_manager.create_arc("root-coding-change")

    create_resp = client.post(
        "/api/review/create-diff",
        json={
            "diff_content": "--- a/y\n+++ b/y\n@@ -1 +1 @@\n-a\n+b\n",
            "arc_id": current_id,
        },
    )
    review_id = create_resp.json()["review_id"]

    decide_resp = client.post(
        f"/api/review/{review_id}/decide",
        json={
            "decision": "approve",
            "followup_goal": "Document the change in the changelog.",
        },
    )
    assert decide_resp.status_code == 200
    new_arc_id = decide_resp.json()["followup_arc_id"]
    new_arc = arc_manager.get_arc(new_arc_id)
    assert new_arc["parent_id"] is None
    assert new_arc["agent_type"] == "PLANNER"


def test_diff_review_no_followup_when_goal_empty(client):
    """An approve decision without followup_goal does not create a new arc."""
    current_id = arc_manager.create_arc("plain-approve")

    create_resp = client.post(
        "/api/review/create-diff",
        json={
            "diff_content": "--- a/z\n+++ b/z\n@@ -1 +1 @@\n-a\n+b\n",
            "arc_id": current_id,
        },
    )
    review_id = create_resp.json()["review_id"]

    decide_resp = client.post(
        f"/api/review/{review_id}/decide",
        json={"decision": "approve", "comment": "ok"},
    )
    assert decide_resp.status_code == 200
    assert "followup_arc_id" not in decide_resp.json()


def test_diff_review_followup_skipped_on_reject(client):
    """A reject decision with followup_goal does NOT create a follow-up arc."""
    current_id = arc_manager.create_arc("reject-with-goal")

    create_resp = client.post(
        "/api/review/create-diff",
        json={
            "diff_content": "--- a/q\n+++ b/q\n@@ -1 +1 @@\n-a\n+b\n",
            "arc_id": current_id,
        },
    )
    review_id = create_resp.json()["review_id"]

    decide_resp = client.post(
        f"/api/review/{review_id}/decide",
        json={
            "decision": "reject",
            "comment": "no",
            "followup_goal": "should be ignored",
        },
    )
    assert decide_resp.status_code == 200
    assert "followup_arc_id" not in decide_resp.json()


def test_create_review_missing_required_field():
    """Sending JSON without code_file_id raises a structuring error."""
    from cattrs.errors import ClassValidationError

    app = Starlette(routes=review.routes)
    app.add_exception_handler(HTTPException, http_exception_handler)
    review.clear_reviews()
    # Use raise_server_exceptions=False so the 500 comes through as a response
    non_raising_client = TestClient(app, raise_server_exceptions=False)

    response = non_raising_client.post(
        "/api/review/create",
        json={"reviewer": "someone"},
    )
    assert response.status_code == 500


# ── Arc-approval reviews ────────────────────────────────────────────


def test_create_arc_approval_review_returns_url():
    """create_arc_approval_review returns review_id + URL and stores the
    target/gate/proposing arc ids."""
    review.clear_reviews()
    target_id = arc_manager.create_arc("target-action")
    gate_id = arc_manager.add_child(target_id, "await-approval")
    proposing_id = arc_manager.create_arc("reflection-root")

    out = review.create_arc_approval_review(
        target_arc_id=target_id,
        gate_arc_id=gate_id,
        title="Test approval",
        action_description="Do the thing",
        proposing_arc_id=proposing_id,
    )

    assert out["url"].startswith("/api/review/")
    stored = review.get_review(out["review_id"])
    assert stored["review_type"] == "arc-approval"
    assert stored["target_arc_id"] == target_id
    assert stored["gate_arc_id"] == gate_id
    assert stored["proposing_arc_id"] == proposing_id
    assert stored["action_description"] == "Do the thing"


def test_view_arc_approval_renders_html(client):
    """The arc-approval page renders title, description, Approve, Reject."""
    target_id = arc_manager.create_arc("target-action")
    gate_id = arc_manager.add_child(target_id, "await-approval")
    proposing_id = arc_manager.create_arc("reflection-root")

    out = review.create_arc_approval_review(
        target_arc_id=target_id,
        gate_arc_id=gate_id,
        title="Approve this reflection action",
        action_description="Add a config knob for foo",
        proposing_arc_id=proposing_id,
    )

    resp = client.get(out["url"])
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Approve this reflection action" in resp.text
    assert "Add a config knob for foo" in resp.text
    assert ">Approve<" in resp.text
    assert ">Reject<" in resp.text
    # No "Revise" button — arc-approval is binary.
    assert ">Revise<" not in resp.text


def test_arc_approval_approve_emits_manual_trigger(client):
    """Approving an arc-approval review records an arc.manual_trigger
    event whose payload references the gate arc id."""
    import json as _json

    target_id = arc_manager.create_arc("target-action")
    gate_id = arc_manager.add_child(target_id, "await-approval")

    out = review.create_arc_approval_review(
        target_arc_id=target_id,
        gate_arc_id=gate_id,
        title="t",
        action_description="d",
    )
    review_id = out["review_id"]

    decide_resp = client.post(
        f"/api/review/{review_id}/decide",
        json={"decision": "approve"},
    )
    assert decide_resp.status_code == 200

    db = get_db()
    try:
        rows = db.execute(
            "SELECT payload_json FROM events WHERE event_type = ?",
            ("arc.manual_trigger",),
        ).fetchall()
    finally:
        db.close()
    assert len(rows) == 1
    payload = _json.loads(rows[0]["payload_json"])
    assert payload["arc_id"] == gate_id
    assert payload["target_arc_id"] == target_id


def test_arc_approval_reject_cancels_target_arc(client):
    """Rejecting an arc-approval review cancels the target action arc."""
    target_id = arc_manager.create_arc("target-action")
    arc_manager.add_child(target_id, "await-approval")

    out = review.create_arc_approval_review(
        target_arc_id=target_id,
        gate_arc_id=999999,  # irrelevant for reject path
        title="t",
        action_description="d",
    )
    review_id = out["review_id"]

    decide_resp = client.post(
        f"/api/review/{review_id}/decide",
        json={"decision": "reject"},
    )
    assert decide_resp.status_code == 200

    target = arc_manager.get_arc(target_id)
    assert target["status"] == "cancelled"


def test_arc_approval_review_one_shot(client):
    """A second decision on the same arc-approval review returns 410."""
    target_id = arc_manager.create_arc("target-action")
    gate_id = arc_manager.add_child(target_id, "await-approval")

    out = review.create_arc_approval_review(
        target_arc_id=target_id,
        gate_arc_id=gate_id,
        title="t",
        action_description="d",
    )
    review_id = out["review_id"]

    client.post(f"/api/review/{review_id}/decide", json={"decision": "approve"})
    resp2 = client.post(
        f"/api/review/{review_id}/decide", json={"decision": "reject"}
    )
    assert resp2.status_code == 410


def test_arc_approval_approve_unblocks_gate_check_activation(client):
    """End-to-end: approve → recorded arc.manual_trigger event → mark
    processed → gate's check_activation returns True."""
    from carpenter.db import db_transaction

    target_id = arc_manager.create_arc("target-action")
    gate_id = arc_manager.add_child(target_id, "await-approval")

    # Register the gate's activation requirement on arc.manual_trigger,
    # mirroring what the gated reflection template instantiation does.
    db = get_db()
    try:
        db.execute(
            "INSERT INTO arc_activations (arc_id, event_type) VALUES (?, ?)",
            (gate_id, "arc.manual_trigger"),
        )
        db.commit()
    finally:
        db.close()

    out = review.create_arc_approval_review(
        target_arc_id=target_id,
        gate_arc_id=gate_id,
        title="t",
        action_description="d",
    )

    # Gate is blocked before approval.
    assert arc_manager.check_activation(gate_id) is False

    client.post(f"/api/review/{out['review_id']}/decide", json={"decision": "approve"})

    # The event was recorded; mark it processed so check_activation sees it.
    with db_transaction() as db:
        db.execute(
            "UPDATE events SET processed = TRUE WHERE event_type = ?",
            ("arc.manual_trigger",),
        )

    assert arc_manager.check_activation(gate_id) is True


def test_create_arc_approval_review_persists_recovery_data():
    """create_arc_approval_review writes review_arc_approval_data + review_id
    + review_url to the target arc's state."""
    from carpenter.core.workflows._arc_state import get_arc_state

    review.clear_reviews()
    target_id = arc_manager.create_arc("target-action")
    gate_id = arc_manager.add_child(target_id, "await-approval")

    out = review.create_arc_approval_review(
        target_arc_id=target_id,
        gate_arc_id=gate_id,
        title="Persisted",
        action_description="Persist me",
    )

    assert get_arc_state(target_id, "review_id") == out["review_id"]
    assert get_arc_state(target_id, "review_url") == out["url"]
    blob = get_arc_state(target_id, "review_arc_approval_data")
    assert blob["review_id"] == out["review_id"]
    assert blob["target_arc_id"] == target_id
    assert blob["gate_arc_id"] == gate_id
    assert blob["title"] == "Persisted"
    assert blob["action_description"] == "Persist me"
    assert "created_at" in blob


def test_recover_review_links_recovers_pending_arcs(client):
    """db.startup_recovery() resets active/waiting arcs to pending before
    recover_review_links runs, so pending arcs with arc-approval data
    must also be recovered."""
    target_id = arc_manager.create_arc("pending-target")
    gate_id = arc_manager.add_child(target_id, "await-approval")
    out = review.create_arc_approval_review(
        target_arc_id=target_id,
        gate_arc_id=gate_id,
        title="Pending",
        action_description="Pending action",
    )
    review_id = out["review_id"]

    review.clear_reviews()
    assert review.get_review(review_id) is None

    review.recover_review_links()

    recovered = review.get_review(review_id)
    assert recovered is not None
    assert recovered["review_type"] == "arc-approval"


def test_recover_review_links_round_trip_arc_approval(client):
    """Round-trip: create arc-approval review, blow away in-memory store,
    call recover_review_links, confirm submit_decision still works."""
    target_id = arc_manager.create_arc("target-action")
    gate_id = arc_manager.add_child(target_id, "await-approval")
    arc_manager.update_status(target_id, "active")
    arc_manager.update_status(target_id, "waiting")

    out = review.create_arc_approval_review(
        target_arc_id=target_id,
        gate_arc_id=gate_id,
        title="Recover me",
        action_description="recovered action",
    )
    review_id = out["review_id"]

    # Simulate daemon restart.
    review.clear_reviews()
    assert review.get_review(review_id) is None

    review.recover_review_links()

    recovered = review.get_review(review_id)
    assert recovered is not None
    assert recovered["review_type"] == "arc-approval"
    assert recovered["target_arc_id"] == target_id
    assert recovered["gate_arc_id"] == gate_id
    assert recovered["used"] is False

    # submit_decision works against the recovered entry.
    resp = client.post(
        f"/api/review/{review_id}/decide", json={"decision": "approve"}
    )
    assert resp.status_code == 200


def test_backfill_arc_approval_reviews_picks_up_stranded_arc():
    """backfill_arc_approval_reviews mints a review for an arc with
    _review_mode='human', non-terminal, no review_arc_approval_data."""
    from carpenter.core.workflows._arc_state import (
        get_arc_state,
        set_arc_state,
    )

    review.clear_reviews()
    target_id = arc_manager.create_arc("target-action")
    arc_manager.add_child(target_id, "await-approval")
    arc_manager.update_status(target_id, "active")
    arc_manager.update_status(target_id, "waiting")
    set_arc_state(target_id, "_review_mode", "human")
    set_arc_state(target_id, "action_description", "stranded action")
    set_arc_state(target_id, "action_type", "kb")
    # Stale state from a botched out-of-process backfill.
    set_arc_state(target_id, "review_id", "stale-id")
    set_arc_state(target_id, "review_url", "/api/review/stale-id")

    count = review.backfill_arc_approval_reviews()
    assert count == 1

    new_review_id = get_arc_state(target_id, "review_id")
    assert new_review_id != "stale-id"
    assert review.get_review(new_review_id) is not None
    blob = get_arc_state(target_id, "review_arc_approval_data")
    assert blob["review_id"] == new_review_id
    assert blob["action_description"] == "stranded action"


def test_backfill_arc_approval_reviews_idempotent():
    """Second backfill run is a no-op once the blob is persisted."""
    from carpenter.core.workflows._arc_state import set_arc_state

    review.clear_reviews()
    target_id = arc_manager.create_arc("target-action")
    arc_manager.add_child(target_id, "await-approval")
    arc_manager.update_status(target_id, "active")
    arc_manager.update_status(target_id, "waiting")
    set_arc_state(target_id, "_review_mode", "human")
    set_arc_state(target_id, "action_description", "d")

    first = review.backfill_arc_approval_reviews()
    assert first == 1
    second = review.backfill_arc_approval_reviews()
    assert second == 0


def test_backfill_arc_approval_reviews_skips_already_backfilled():
    """An arc with review_arc_approval_data is skipped even if it also
    has _review_mode='human' and is non-terminal."""
    from carpenter.core.workflows._arc_state import set_arc_state

    review.clear_reviews()
    target_id = arc_manager.create_arc("target-action")
    gate_id = arc_manager.add_child(target_id, "await-approval")
    arc_manager.update_status(target_id, "active")
    arc_manager.update_status(target_id, "waiting")
    set_arc_state(target_id, "_review_mode", "human")
    # create_arc_approval_review writes review_arc_approval_data itself.
    review.create_arc_approval_review(
        target_arc_id=target_id,
        gate_arc_id=gate_id,
        title="t",
        action_description="d",
    )

    count = review.backfill_arc_approval_reviews()
    assert count == 0


def test_backfill_arc_approval_reviews_skips_terminal_arcs():
    """A cancelled / completed arc isn't backfilled."""
    from carpenter.core.workflows._arc_state import set_arc_state

    review.clear_reviews()
    target_id = arc_manager.create_arc("target-action")
    arc_manager.add_child(target_id, "await-approval")
    set_arc_state(target_id, "_review_mode", "human")
    set_arc_state(target_id, "action_description", "d")
    arc_manager.update_status(target_id, "cancelled")

    count = review.backfill_arc_approval_reviews()
    assert count == 0


# ── Absolute review URLs ────────────────────────────────────────────


def test_absolutize_review_url_passes_through_absolute():
    assert review.absolutize_review_url("https://x/api/review/abc") == \
        "https://x/api/review/abc"
    assert review.absolutize_review_url("http://x/api/review/abc") == \
        "http://x/api/review/abc"


def test_absolutize_review_url_passes_through_when_unset():
    from carpenter import config as cfg
    saved = cfg.CONFIG.get("public_base_url", "")
    cfg.CONFIG["public_base_url"] = ""
    try:
        assert review.absolutize_review_url("/api/review/abc") == "/api/review/abc"
    finally:
        cfg.CONFIG["public_base_url"] = saved


def test_absolutize_review_url_prefixes_relative():
    from carpenter import config as cfg
    saved = cfg.CONFIG.get("public_base_url", "")
    cfg.CONFIG["public_base_url"] = "https://example.test/"
    try:
        assert review.absolutize_review_url("/api/review/abc") == \
            "https://example.test/api/review/abc"
    finally:
        cfg.CONFIG["public_base_url"] = saved


def test_create_diff_review_returns_absolute_when_public_base_url_set():
    from carpenter import config as cfg

    review.clear_reviews()
    saved = cfg.CONFIG.get("public_base_url", "")
    cfg.CONFIG["public_base_url"] = "https://example.test"
    try:
        out = review.create_diff_review(
            diff_content="--- a\n+++ b\n",
            title="t",
        )
    finally:
        cfg.CONFIG["public_base_url"] = saved

    assert out["url"].startswith("https://example.test/api/review/")
    assert out["url"].endswith(out["review_id"])


def test_create_diff_review_returns_relative_when_unset():
    from carpenter import config as cfg

    review.clear_reviews()
    saved = cfg.CONFIG.get("public_base_url", "")
    cfg.CONFIG["public_base_url"] = ""
    try:
        out = review.create_diff_review(
            diff_content="--- a\n+++ b\n",
            title="t",
        )
    finally:
        cfg.CONFIG["public_base_url"] = saved

    assert out["url"].startswith("/api/review/")


def test_create_arc_approval_review_returns_absolute_when_public_base_url_set():
    from carpenter import config as cfg
    from carpenter.core.workflows._arc_state import get_arc_state

    review.clear_reviews()
    target_id = arc_manager.create_arc("target-action")
    gate_id = arc_manager.add_child(target_id, "await-approval")

    saved = cfg.CONFIG.get("public_base_url", "")
    cfg.CONFIG["public_base_url"] = "https://example.test"
    try:
        out = review.create_arc_approval_review(
            target_arc_id=target_id,
            gate_arc_id=gate_id,
            title="t",
            action_description="d",
        )
    finally:
        cfg.CONFIG["public_base_url"] = saved

    assert out["url"].startswith("https://example.test/api/review/")
    # Persisted review_url should also be absolute.
    assert get_arc_state(target_id, "review_url") == out["url"]


def test_create_arc_approval_review_returns_relative_when_unset():
    from carpenter import config as cfg

    review.clear_reviews()
    target_id = arc_manager.create_arc("target-action")
    gate_id = arc_manager.add_child(target_id, "await-approval")

    saved = cfg.CONFIG.get("public_base_url", "")
    cfg.CONFIG["public_base_url"] = ""
    try:
        out = review.create_arc_approval_review(
            target_arc_id=target_id,
            gate_arc_id=gate_id,
            title="t",
            action_description="d",
        )
    finally:
        cfg.CONFIG["public_base_url"] = saved

    assert out["url"].startswith("/api/review/")


def test_migrate_review_urls_to_absolute_rewrites_relative():
    from carpenter import config as cfg
    from carpenter.core.workflows._arc_state import (
        get_arc_state, set_arc_state,
    )

    arc_id = arc_manager.create_arc("migrate-target")
    set_arc_state(arc_id, "review_url", "/api/review/abc-123")

    saved = cfg.CONFIG.get("public_base_url", "")
    cfg.CONFIG["public_base_url"] = "https://example.test"
    try:
        n = review.migrate_review_urls_to_absolute()
    finally:
        cfg.CONFIG["public_base_url"] = saved

    assert n >= 1
    assert get_arc_state(arc_id, "review_url") == \
        "https://example.test/api/review/abc-123"


def test_migrate_review_urls_to_absolute_is_idempotent():
    from carpenter import config as cfg
    from carpenter.core.workflows._arc_state import (
        get_arc_state, set_arc_state,
    )

    arc_id = arc_manager.create_arc("migrate-already-absolute")
    set_arc_state(
        arc_id, "review_url", "https://example.test/api/review/already",
    )

    saved = cfg.CONFIG.get("public_base_url", "")
    cfg.CONFIG["public_base_url"] = "https://example.test"
    try:
        review.migrate_review_urls_to_absolute()
        n_second = review.migrate_review_urls_to_absolute()
    finally:
        cfg.CONFIG["public_base_url"] = saved

    # The already-absolute arc contributes 0 to either run; second run
    # should be a no-op for the whole table.
    assert n_second == 0
    assert get_arc_state(arc_id, "review_url") == \
        "https://example.test/api/review/already"


def test_migrate_review_urls_to_absolute_rewrites_nested_blob():
    from carpenter import config as cfg
    from carpenter.core.workflows._arc_state import (
        get_arc_state, set_arc_state,
    )

    arc_id = arc_manager.create_arc("migrate-blob")
    set_arc_state(arc_id, "review_arc_approval_data", {
        "review_id": "rid",
        "target_arc_id": arc_id,
        "gate_arc_id": 0,
        "review_url": "/api/review/rid",
        "title": "t",
        "action_description": "d",
        "created_at": "x",
    })

    saved = cfg.CONFIG.get("public_base_url", "")
    cfg.CONFIG["public_base_url"] = "https://example.test"
    try:
        review.migrate_review_urls_to_absolute()
    finally:
        cfg.CONFIG["public_base_url"] = saved

    blob = get_arc_state(arc_id, "review_arc_approval_data")
    assert blob["review_url"] == "https://example.test/api/review/rid"


def test_migrate_review_urls_to_absolute_noop_when_unset():
    from carpenter import config as cfg
    from carpenter.core.workflows._arc_state import (
        get_arc_state, set_arc_state,
    )

    arc_id = arc_manager.create_arc("migrate-noop")
    set_arc_state(arc_id, "review_url", "/api/review/keep-relative")

    saved = cfg.CONFIG.get("public_base_url", "")
    cfg.CONFIG["public_base_url"] = ""
    try:
        review.migrate_review_urls_to_absolute()
    finally:
        cfg.CONFIG["public_base_url"] = saved

    # absolutize_review_url is a no-op when unset; value unchanged.
    assert get_arc_state(arc_id, "review_url") == "/api/review/keep-relative"

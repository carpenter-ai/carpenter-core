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

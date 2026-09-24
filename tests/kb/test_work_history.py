"""Tests for carpenter.kb.work_history — work history summaries."""

import os

import httpx
import pytest

from carpenter.db import get_db
from carpenter.kb.store import KBStore
from carpenter.kb.work_history import (
    should_summarize,
    generate_work_summary,
    create_work_entry,
    _sanitize_name,
)


def _create_arc(db, name, goal="", parent_id=None, status="completed"):
    """Helper to create an arc for testing."""
    cursor = db.execute(
        "INSERT INTO arcs (name, goal, parent_id, status) VALUES (?, ?, ?, ?)",
        (name, goal, parent_id, status),
    )
    db.commit()
    return cursor.lastrowid


class TestShouldSummarize:
    def test_sentinel_excluded(self):
        assert should_summarize(0) is False

    def test_child_arc_excluded(self):
        db = get_db()
        try:
            parent_id = _create_arc(db, "parent")
            child_id = _create_arc(db, "child", parent_id=parent_id)
            _create_arc(db, "grandchild", parent_id=child_id)
        finally:
            db.close()
        assert should_summarize(child_id) is False

    def test_root_without_children_excluded(self):
        db = get_db()
        try:
            arc_id = _create_arc(db, "solo-root")
        finally:
            db.close()
        assert should_summarize(arc_id) is False

    def test_underscore_name_excluded(self):
        db = get_db()
        try:
            parent_id = _create_arc(db, "_internal")
            _create_arc(db, "child", parent_id=parent_id)
        finally:
            db.close()
        assert should_summarize(parent_id) is False

    def test_valid_root_with_children(self):
        db = get_db()
        try:
            parent_id = _create_arc(db, "My Workflow")
            _create_arc(db, "Step 1", parent_id=parent_id)
        finally:
            db.close()
        assert should_summarize(parent_id) is True

    def test_nonexistent_arc(self):
        assert should_summarize(999999) is False

    def test_disabled_by_config(self, monkeypatch):
        import carpenter.config
        current = dict(carpenter.config.CONFIG)
        current["kb"] = {"work_history_enabled": False}
        monkeypatch.setattr("carpenter.config.CONFIG", current)

        db = get_db()
        try:
            parent_id = _create_arc(db, "Workflow")
            _create_arc(db, "Step", parent_id=parent_id)
        finally:
            db.close()
        assert should_summarize(parent_id) is False


class TestSanitizeName:
    def test_basic(self):
        assert _sanitize_name("My Workflow") == "my-workflow"

    def test_special_chars(self):
        assert _sanitize_name("Send email (urgent!)") == "send-email-urgent"

    def test_truncation(self):
        long_name = "a" * 100
        result = _sanitize_name(long_name)
        assert len(result) <= 50

    def test_empty(self):
        assert _sanitize_name("") == "unnamed"
        assert _sanitize_name("!!!") == "unnamed"


class TestGenerateWorkSummary:
    def test_returns_none_for_missing_arc(self):
        result = generate_work_summary(999999)
        assert result is None

    def test_calls_ai_model(self, monkeypatch):
        """Mock the AI call and verify it returns a summary."""
        db = get_db()
        try:
            parent_id = _create_arc(db, "Build Feature")
            _create_arc(db, "Write code", goal="Write the code", parent_id=parent_id)
            _create_arc(db, "Run tests", goal="Run test suite", parent_id=parent_id)
        finally:
            db.close()

        # Mock model_resolver (imported inside generate_work_summary)
        class MockClient:
            def call(self, system, messages, model=None, max_tokens=None, temperature=None):
                return {"content": [{"type": "text", "text": "Built the feature and ran tests."}]}

            def extract_text(self, resp):
                return resp["content"][0]["text"]

        mock_client = MockClient()
        monkeypatch.setattr(
            "carpenter.agent.model_resolver.get_model_for_role",
            lambda role: "anthropic:claude-test",
        )
        monkeypatch.setattr(
            "carpenter.agent.model_resolver.create_client_for_model",
            lambda model_str: mock_client,
        )
        monkeypatch.setattr(
            "carpenter.agent.model_resolver.parse_model_string",
            lambda model_str: ("anthropic", "claude-test"),
        )

        result = generate_work_summary(parent_id)
        assert result == "Built the feature and ran tests."


class TestCreateWorkEntry:
    def test_creates_kb_entry(self, tmp_path, monkeypatch):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)

        db = get_db()
        try:
            parent_id = _create_arc(db, "Deploy App")
            _create_arc(db, "Build", parent_id=parent_id)
        finally:
            db.close()

        # Mock the AI summary
        monkeypatch.setattr(
            "carpenter.kb.work_history.generate_work_summary",
            lambda arc_id: "Deployed the application successfully.",
        )

        path = create_work_entry(parent_id, store)
        assert path is not None
        assert path.startswith("work/")
        assert "deploy-app" in path

        # Verify entry exists
        entry = store.get_entry(path)
        assert entry is not None
        assert "Deployed the application" in entry["content"]

    def test_returns_none_on_summary_failure(self, tmp_path, monkeypatch):
        kb_dir = str(tmp_path / "kb")
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)

        db = get_db()
        try:
            parent_id = _create_arc(db, "Broken")
            _create_arc(db, "Step", parent_id=parent_id)
        finally:
            db.close()

        monkeypatch.setattr(
            "carpenter.kb.work_history.generate_work_summary",
            lambda arc_id: None,
        )

        path = create_work_entry(parent_id, store)
        assert path is None


class TestGenerateWorkSummaryApiFailure:
    """A failing AI call must degrade to None, not escape the function.

    Regression for dead-lettered ``kb.work_summary`` work items. An
    Anthropic spend cap returns a **400** (not a 429), so the provider layer
    correctly declined to retry and re-raised ``httpx.HTTPStatusError``.
    That class was absent from this function's ``except`` tuple, so it
    escaped to the work handler, which retried three times and then
    dead-lettered — despite the docstring promising "None on failure".
    """

    def _patch_resolver(self, monkeypatch, client):
        # Patch the resolver, not the provider module: generate_work_summary
        # imports model_resolver inside the function body, so these three
        # names are the only interception point.
        monkeypatch.setattr(
            "carpenter.agent.model_resolver.get_model_for_role",
            lambda role: "anthropic:claude-test",
        )
        monkeypatch.setattr(
            "carpenter.agent.model_resolver.create_client_for_model",
            lambda model_str: client,
        )
        monkeypatch.setattr(
            "carpenter.agent.model_resolver.parse_model_string",
            lambda model_str: ("anthropic", "claude-test"),
        )

    def _arc(self):
        db = get_db()
        try:
            parent_id = _create_arc(db, "Build Feature")
            _create_arc(db, "Write code", goal="Write it", parent_id=parent_id)
        finally:
            db.close()
        return parent_id

    def test_spend_cap_400_returns_none(self, monkeypatch):
        """The exact shape that caused the outage: a non-retryable 400."""
        parent_id = self._arc()

        class CapReachedClient:
            def call(self, system, messages, **kw):
                request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
                response = httpx.Response(400, request=request, json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "You have reached your specified API usage limits.",
                    },
                })
                raise httpx.HTTPStatusError(
                    "Client error '400 Bad Request'",
                    request=request, response=response,
                )

            def extract_text(self, resp):  # pragma: no cover - never reached
                raise AssertionError("extract_text must not run after a failed call")

        self._patch_resolver(monkeypatch, CapReachedClient())
        assert generate_work_summary(parent_id) is None

    @pytest.mark.parametrize("exc", [
        httpx.ConnectError("connection refused"),
        httpx.TimeoutException("timed out"),
    ])
    def test_transport_failures_return_none(self, monkeypatch, exc):
        """Any httpx.HTTPError subclass, not just status errors."""
        parent_id = self._arc()

        class FailingClient:
            def call(self, system, messages, **kw):
                raise exc

            def extract_text(self, resp):  # pragma: no cover
                raise AssertionError("unreachable")

        self._patch_resolver(monkeypatch, FailingClient())
        assert generate_work_summary(parent_id) is None

    def test_create_work_entry_writes_nothing_on_api_failure(self, tmp_path, monkeypatch):
        """The caller must no-op too — no KB entry, no exception."""
        parent_id = self._arc()

        class FailingClient:
            def call(self, system, messages, **kw):
                raise httpx.ConnectError("connection refused")

            def extract_text(self, resp):  # pragma: no cover
                raise AssertionError("unreachable")

        self._patch_resolver(monkeypatch, FailingClient())
        store = KBStore(str(tmp_path / "kb"))
        assert create_work_entry(parent_id, store) is None

    def test_programming_errors_still_surface(self, monkeypatch):
        """Don't over-widen: a genuine bug must not be silently swallowed."""
        parent_id = self._arc()

        class BuggyClient:
            def call(self, system, messages, **kw):
                raise AttributeError("genuine bug in the client")

            def extract_text(self, resp):  # pragma: no cover
                raise AssertionError("unreachable")

        self._patch_resolver(monkeypatch, BuggyClient())
        with pytest.raises(AttributeError):
            generate_work_summary(parent_id)

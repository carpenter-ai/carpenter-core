"""Tests for carpenter.kb.work_history — work history summaries."""

import os

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

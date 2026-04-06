"""Tests for reflection auto-action processing."""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

from carpenter.agent import reflection, reflection_action, conversation
from carpenter.db import get_db
from carpenter import config


# ── Helpers ──────────────────────────────────────────────────────────

def _create_reflection(proposed_actions=None, cadence="daily"):
    """Create a reflection record and return its ID."""
    return reflection.save_reflection(
        cadence,
        "2026-03-15T00:00:00",
        "2026-03-16T00:00:00",
        "Test reflection content",
        proposed_actions=proposed_actions,
        model="test",
    )


def _enable_auto_action(monkeypatch):
    """Enable auto_action in the reflection config."""
    refl_config = dict(config.CONFIG["reflection"])
    refl_config["auto_action"] = True
    monkeypatch.setitem(config.CONFIG, "reflection", refl_config)


# ── Action classification ────────────────────────────────────────────

class TestClassifyAction:
    """Test action classification heuristics."""

    def test_kb_action(self):
        assert reflection_action.classify_action("Create a new debugging skill") == "kb"

    def test_kb_action_update(self):
        assert reflection_action.classify_action("Update kb entry for code review") == "kb"

    def test_code_action(self):
        assert reflection_action.classify_action("Implement a new logging function") == "code"

    def test_code_action_fix(self):
        assert reflection_action.classify_action("Fix bug in arc creation") == "code"

    def test_config_action(self):
        assert reflection_action.classify_action("Change configuration for reflection threshold") == "config"

    def test_config_setting(self):
        assert reflection_action.classify_action("Adjust setting for rate limits") == "config"

    def test_other_action(self):
        assert reflection_action.classify_action("Discuss with user about priorities") == "other"


# ── Parsing proposed actions ─────────────────────────────────────────

class TestParseProposedActions:
    """Test parsing of proposed_actions from reflections."""

    def test_json_list(self):
        actions = json.dumps(["Create skill A", "Fix bug B"])
        result = reflection_action._parse_proposed_actions(actions)
        assert result == ["Create skill A", "Fix bug B"]

    def test_json_string(self):
        actions = json.dumps("Single action")
        result = reflection_action._parse_proposed_actions(actions)
        assert result == ["Single action"]

    def test_line_separated(self):
        actions = "Create skill A\nFix bug B\nUpdate config C"
        result = reflection_action._parse_proposed_actions(actions)
        assert len(result) == 3
        assert result[0] == "Create skill A"

    def test_markdown_list(self):
        actions = "- Create skill A\n- Fix bug B\n- Update config C"
        result = reflection_action._parse_proposed_actions(actions)
        assert len(result) == 3
        assert result[0] == "Create skill A"

    def test_numbered_list(self):
        actions = "1. Create skill A\n2. Fix bug B\n3. Update config C"
        result = reflection_action._parse_proposed_actions(actions)
        assert len(result) == 3
        assert result[0] == "Create skill A"

    def test_empty(self):
        assert reflection_action._parse_proposed_actions(None) == []
        assert reflection_action._parse_proposed_actions("") == []
        assert reflection_action._parse_proposed_actions("  ") == []

    def test_mixed_format(self):
        actions = "- Create a new skill for debugging\n* Implement code fix\n3. Adjust config"
        result = reflection_action._parse_proposed_actions(actions)
        assert len(result) == 3


# ── Process reflection actions ───────────────────────────────────────

class TestProcessReflectionActions:
    """Test the main process_reflection_actions function."""

    def test_disabled_by_default(self, test_db):
        """Auto-action does nothing when config is false."""
        rid = _create_reflection(proposed_actions="- Create skill X")
        result = reflection_action.process_reflection_actions(rid)
        assert result["submitted"] == 0
        assert result["skipped"] == 0

    def test_creates_records_for_each_action(self, test_db, monkeypatch):
        """Should create reflection_actions records for each action."""
        _enable_auto_action(monkeypatch)

        # Mock invoke_for_chat to avoid actual AI calls
        monkeypatch.setattr(
            "carpenter.agent.invocation.invoke_for_chat",
            lambda *a, **kw: {"response_text": "Done", "conversation_id": 1},
        )

        rid = _create_reflection(
            proposed_actions="- Create a new skill for testing\n- Implement code fix for logging",
        )
        result = reflection_action.process_reflection_actions(rid)

        assert result["submitted"] == 2
        actions = reflection_action.get_reflection_actions(reflection_id=rid)
        assert len(actions) == 2

    def test_skips_config_and_other_actions(self, test_db, monkeypatch):
        """Config and other actions should be recorded as skipped."""
        _enable_auto_action(monkeypatch)

        rid = _create_reflection(
            proposed_actions="- Adjust configuration threshold\n- Discuss priorities with user",
        )
        result = reflection_action.process_reflection_actions(rid)

        assert result["skipped"] == 2
        assert result["submitted"] == 0

        actions = reflection_action.get_reflection_actions(reflection_id=rid)
        assert len(actions) == 2
        assert all(a["status"] == "skipped" for a in actions)

    def test_skipped_actions_record_suggestion(self, test_db, monkeypatch):
        """Skipped actions should have the suggestion text in outcome."""
        _enable_auto_action(monkeypatch)

        rid = _create_reflection(
            proposed_actions="- Adjust configuration threshold",
        )
        reflection_action.process_reflection_actions(rid)

        actions = reflection_action.get_reflection_actions(reflection_id=rid)
        assert len(actions) == 1
        assert "Adjust configuration threshold" in actions[0]["outcome"]

    def test_skill_action_submitted(self, test_db, monkeypatch):
        """Skill actions should invoke invoke_for_chat."""
        _enable_auto_action(monkeypatch)

        invoke_calls = []

        def mock_invoke(*args, **kwargs):
            invoke_calls.append((args, kwargs))
            return {"response_text": "Skill created", "conversation_id": 1}

        monkeypatch.setattr(
            "carpenter.agent.invocation.invoke_for_chat",
            mock_invoke,
        )

        rid = _create_reflection(proposed_actions="- Create a new skill for testing")
        result = reflection_action.process_reflection_actions(rid)

        assert result["submitted"] == 1
        assert result["approved"] == 1
        assert len(invoke_calls) == 1

    def test_code_action_submitted(self, test_db, monkeypatch):
        """Code actions should invoke invoke_for_chat."""
        _enable_auto_action(monkeypatch)

        invoke_calls = []

        def mock_invoke(*args, **kwargs):
            invoke_calls.append((args, kwargs))
            return {"response_text": "Code implemented", "conversation_id": 1}

        monkeypatch.setattr(
            "carpenter.agent.invocation.invoke_for_chat",
            mock_invoke,
        )

        rid = _create_reflection(proposed_actions="- Implement a fix for the logging bug")
        result = reflection_action.process_reflection_actions(rid)

        assert result["submitted"] == 1
        assert result["approved"] == 1

    def test_action_failure_recorded(self, test_db, monkeypatch):
        """Failed actions should be recorded as rejected with error detail."""
        _enable_auto_action(monkeypatch)

        def mock_invoke(*args, **kwargs):
            raise RuntimeError("AI provider error")

        monkeypatch.setattr(
            "carpenter.agent.invocation.invoke_for_chat",
            mock_invoke,
        )

        rid = _create_reflection(proposed_actions="- Create a new kb entry for testing")
        result = reflection_action.process_reflection_actions(rid)

        # The exception is caught inside _submit_kb_action and returns
        # success=False, so it's counted as rejected (not errors)
        assert result["rejected"] == 1
        actions = reflection_action.get_reflection_actions(reflection_id=rid)
        assert len(actions) == 1
        assert actions[0]["status"] == "rejected"
        assert "AI provider error" in actions[0]["outcome"]

    def test_no_proposed_actions(self, test_db, monkeypatch):
        """Reflection with no proposed_actions should return early."""
        _enable_auto_action(monkeypatch)

        rid = _create_reflection(proposed_actions=None)
        result = reflection_action.process_reflection_actions(rid)

        assert result["submitted"] == 0
        assert result["skipped"] == 0

    def test_nonexistent_reflection(self, test_db, monkeypatch):
        """Nonexistent reflection ID should return early."""
        _enable_auto_action(monkeypatch)

        result = reflection_action.process_reflection_actions(99999)
        assert result["submitted"] == 0


# ── Rate limiting ────────────────────────────────────────────────────

class TestRateLimiting:
    """Test per-reflection and per-day rate limits."""

    def test_per_reflection_cap(self, test_db, monkeypatch):
        """Should only process up to max_actions_per_reflection actions."""
        refl_config = dict(config.CONFIG["reflection"])
        refl_config["auto_action"] = True
        refl_config["max_actions_per_reflection"] = 2
        monkeypatch.setitem(config.CONFIG, "reflection", refl_config)

        monkeypatch.setattr(
            "carpenter.agent.invocation.invoke_for_chat",
            lambda *a, **kw: {"response_text": "Done", "conversation_id": 1},
        )

        actions = "\n".join([
            "- Create skill A",
            "- Create skill B",
            "- Create skill C",
            "- Create skill D",
        ])
        rid = _create_reflection(proposed_actions=actions)
        result = reflection_action.process_reflection_actions(rid)

        # Should only process 2 actions (the cap)
        total_processed = result["submitted"] + result["skipped"]
        assert total_processed == 2

    def test_per_day_cap(self, test_db, monkeypatch):
        """Should respect the daily action limit."""
        refl_config = dict(config.CONFIG["reflection"])
        refl_config["auto_action"] = True
        refl_config["max_actions_per_day"] = 3
        monkeypatch.setitem(config.CONFIG, "reflection", refl_config)

        monkeypatch.setattr(
            "carpenter.agent.invocation.invoke_for_chat",
            lambda *a, **kw: {"response_text": "Done", "conversation_id": 1},
        )

        # First reflection: 2 actions
        rid1 = _create_reflection(proposed_actions="- Create skill A\n- Create skill B")
        result1 = reflection_action.process_reflection_actions(rid1)
        assert result1["submitted"] == 2

        # Second reflection: should only be able to do 1 more (3 - 2 = 1)
        rid2 = _create_reflection(proposed_actions="- Create skill C\n- Create skill D")
        result2 = reflection_action.process_reflection_actions(rid2)
        assert result2["submitted"] + result2["skipped"] <= 1

    def test_daily_limit_reached_skips_entirely(self, test_db, monkeypatch):
        """When daily limit is already reached, skip all actions."""
        refl_config = dict(config.CONFIG["reflection"])
        refl_config["auto_action"] = True
        refl_config["max_actions_per_day"] = 0  # Already at limit
        monkeypatch.setitem(config.CONFIG, "reflection", refl_config)

        rid = _create_reflection(proposed_actions="- Create skill X")
        result = reflection_action.process_reflection_actions(rid)

        assert result["submitted"] == 0
        assert result["skipped"] == 0


# ── Taint detection ─────────────────────────────────────────────────

class TestTaintDetection:
    """Test taint-aware review mode selection."""

    def test_clean_reflection_uses_standard_review_mode(self, test_db, monkeypatch):
        """Clean reflection should use the standard review_mode from config."""
        refl_config = dict(config.CONFIG["reflection"])
        refl_config["auto_action"] = True
        refl_config["review_mode"] = "auto"
        refl_config["tainted_review_mode"] = "human"
        monkeypatch.setitem(config.CONFIG, "reflection", refl_config)

        monkeypatch.setattr(
            "carpenter.agent.invocation.invoke_for_chat",
            lambda *a, **kw: {"response_text": "Done", "conversation_id": 1},
        )

        rid = _create_reflection(proposed_actions="- Create a new skill for testing")
        reflection_action.process_reflection_actions(rid)

        actions = reflection_action.get_reflection_actions(reflection_id=rid)
        assert len(actions) == 1
        assert actions[0]["review_mode"] == "auto"

    def test_tainted_reflection_uses_tainted_review_mode(self, test_db, monkeypatch):
        """Tainted reflection should use tainted_review_mode from config."""
        refl_config = dict(config.CONFIG["reflection"])
        refl_config["auto_action"] = True
        refl_config["review_mode"] = "auto"
        refl_config["tainted_review_mode"] = "human"
        monkeypatch.setitem(config.CONFIG, "reflection", refl_config)

        monkeypatch.setattr(
            "carpenter.agent.invocation.invoke_for_chat",
            lambda *a, **kw: {"response_text": "Done", "conversation_id": 1},
        )

        # Create a reflection with a tainted conversation
        rid = _create_reflection(proposed_actions="- Create skill for web scraping")

        # Create a conversation that looks like a reflection conversation
        conv_id = conversation.create_conversation()
        conversation.set_conversation_title(conv_id, "[Daily Reflection] 2026-03-16")

        # Taint the conversation
        db = get_db()
        try:
            db.execute(
                "INSERT INTO conversation_taint (conversation_id, source_tool) "
                "VALUES (?, ?)",
                (conv_id, "carpenter_tools.act.web"),
            )
            db.commit()
        finally:
            db.close()

        reflection_action.process_reflection_actions(rid)

        actions = reflection_action.get_reflection_actions(reflection_id=rid)
        assert len(actions) == 1
        assert actions[0]["review_mode"] == "human"


# ── Batch notification ───────────────────────────────────────────────

class TestBatchNotification:
    """Test that a batch notification is sent after processing."""

    def test_notification_sent(self, test_db, monkeypatch):
        """A notification should be sent after processing actions."""
        _enable_auto_action(monkeypatch)

        monkeypatch.setattr(
            "carpenter.agent.invocation.invoke_for_chat",
            lambda *a, **kw: {"response_text": "Done", "conversation_id": 1},
        )

        notify_calls = []
        monkeypatch.setattr(
            "carpenter.core.notifications.notify",
            lambda msg, priority=None, category=None: notify_calls.append(
                {"message": msg, "priority": priority, "category": category}
            ),
        )

        rid = _create_reflection(proposed_actions="- Create a new skill for testing")
        reflection_action.process_reflection_actions(rid)

        assert len(notify_calls) == 1
        assert "reflection_actions" == notify_calls[0]["category"]
        assert "low" == notify_calls[0]["priority"]
        assert "Reflection auto-actions processed" in notify_calls[0]["message"]

    def test_no_notification_when_no_actions(self, test_db, monkeypatch):
        """No notification should be sent when there are no actions."""
        _enable_auto_action(monkeypatch)

        notify_calls = []
        monkeypatch.setattr(
            "carpenter.core.notifications.notify",
            lambda msg, priority=None, category=None: notify_calls.append(msg),
        )

        rid = _create_reflection(proposed_actions=None)
        reflection_action.process_reflection_actions(rid)

        assert len(notify_calls) == 0

    def test_notification_includes_taint_note(self, test_db, monkeypatch):
        """Notification should mention tainted reflection."""
        _enable_auto_action(monkeypatch)

        monkeypatch.setattr(
            "carpenter.agent.invocation.invoke_for_chat",
            lambda *a, **kw: {"response_text": "Done", "conversation_id": 1},
        )

        notify_calls = []
        monkeypatch.setattr(
            "carpenter.core.notifications.notify",
            lambda msg, priority=None, category=None: notify_calls.append(msg),
        )

        rid = _create_reflection(proposed_actions="- Create skill for web scraping")

        # Create tainted reflection conversation
        conv_id = conversation.create_conversation()
        conversation.set_conversation_title(conv_id, "[Daily Reflection] 2026-03-16")
        db = get_db()
        try:
            db.execute(
                "INSERT INTO conversation_taint (conversation_id, source_tool) "
                "VALUES (?, ?)",
                (conv_id, "carpenter_tools.act.web"),
            )
            db.commit()
        finally:
            db.close()

        reflection_action.process_reflection_actions(rid)

        assert len(notify_calls) == 1
        assert "tainted" in notify_calls[0].lower()


# ── get_reflection_actions ───────────────────────────────────────────

class TestGetReflectionActions:
    """Test the query function for reflection actions."""

    def test_returns_actions(self, test_db, monkeypatch):
        _enable_auto_action(monkeypatch)

        monkeypatch.setattr(
            "carpenter.agent.invocation.invoke_for_chat",
            lambda *a, **kw: {"response_text": "Done", "conversation_id": 1},
        )

        rid = _create_reflection(proposed_actions="- Create skill A\n- Create skill B")
        reflection_action.process_reflection_actions(rid)

        actions = reflection_action.get_reflection_actions(reflection_id=rid)
        assert len(actions) == 2

    def test_filter_by_status(self, test_db, monkeypatch):
        _enable_auto_action(monkeypatch)

        monkeypatch.setattr(
            "carpenter.agent.invocation.invoke_for_chat",
            lambda *a, **kw: {"response_text": "Done", "conversation_id": 1},
        )

        rid = _create_reflection(
            proposed_actions="- Create skill A\n- Adjust config B",
        )
        reflection_action.process_reflection_actions(rid)

        approved = reflection_action.get_reflection_actions(status="approved")
        skipped = reflection_action.get_reflection_actions(status="skipped")

        assert len(approved) >= 1
        assert len(skipped) >= 1

    def test_limit(self, test_db, monkeypatch):
        _enable_auto_action(monkeypatch)

        monkeypatch.setattr(
            "carpenter.agent.invocation.invoke_for_chat",
            lambda *a, **kw: {"response_text": "Done", "conversation_id": 1},
        )

        rid = _create_reflection(
            proposed_actions="- Create skill A\n- Create skill B\n- Create skill C",
        )
        reflection_action.process_reflection_actions(rid)

        actions = reflection_action.get_reflection_actions(limit=2)
        assert len(actions) <= 2

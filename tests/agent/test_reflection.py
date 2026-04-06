"""Tests for reflective meta-skills: data gathering, handler, storage."""

from datetime import datetime, timezone, timedelta

import pytest

from carpenter.agent import reflection, conversation
from carpenter.db import get_db
from carpenter import config


class TestShouldReflect:
    """Test activity threshold checking."""

    def test_no_activity_returns_false(self, test_db):
        assert reflection.should_reflect("daily") is False

    def test_with_activity_returns_true(self, test_db):
        # Create a conversation
        conv_id = conversation.create_conversation()
        conversation.add_message(conv_id, "user", "Hello")
        assert reflection.should_reflect("daily") is True

    def test_weekly_needs_more_activity(self, test_db, monkeypatch):
        monkeypatch.setitem(config.CONFIG, "reflection", {"min_daily_conversations": 2})
        # Weekly threshold = min_daily * 7 = 14
        # Create 10 conversations (not enough)
        for _ in range(10):
            conversation.create_conversation()
        assert reflection.should_reflect("weekly") is False

        # Create 4 more (total 14, enough)
        for _ in range(4):
            conversation.create_conversation()
        assert reflection.should_reflect("weekly") is True

    def test_unknown_cadence_returns_false(self, test_db):
        assert reflection.should_reflect("hourly") is False


class TestSaveAndGetReflections:
    """Test reflection storage round-trip."""

    def test_save_and_get(self, test_db):
        rid = reflection.save_reflection(
            "daily", "2026-03-12T00:00:00", "2026-03-13T00:00:00",
            "Today's reflection content",
            proposed_actions="Update skill X",
            model="haiku",
            input_tokens=100,
            output_tokens=50,
        )
        assert rid > 0

        results = reflection.get_reflections("daily", limit=1)
        assert len(results) == 1
        r = results[0]
        assert r["cadence"] == "daily"
        assert r["content"] == "Today's reflection content"
        assert r["proposed_actions"] == "Update skill X"
        assert r["model"] == "haiku"
        assert r["input_tokens"] == 100
        assert r["output_tokens"] == 50

    def test_get_ordered_by_period_end_desc(self, test_db):
        reflection.save_reflection("daily", "2026-03-10", "2026-03-11", "Day 1")
        reflection.save_reflection("daily", "2026-03-11", "2026-03-12", "Day 2")
        reflection.save_reflection("daily", "2026-03-12", "2026-03-13", "Day 3")

        results = reflection.get_reflections("daily", limit=3)
        assert results[0]["content"] == "Day 3"
        assert results[1]["content"] == "Day 2"
        assert results[2]["content"] == "Day 1"

    def test_get_filters_by_cadence(self, test_db):
        reflection.save_reflection("daily", "2026-03-12", "2026-03-13", "Daily")
        reflection.save_reflection("weekly", "2026-03-06", "2026-03-13", "Weekly")

        daily = reflection.get_reflections("daily")
        weekly = reflection.get_reflections("weekly")
        assert len(daily) == 1
        assert daily[0]["content"] == "Daily"
        assert len(weekly) == 1
        assert weekly[0]["content"] == "Weekly"

    def test_get_respects_limit(self, test_db):
        for i in range(10):
            reflection.save_reflection("daily", f"2026-03-{i+1:02d}", f"2026-03-{i+2:02d}", f"Day {i}")
        results = reflection.get_reflections("daily", limit=3)
        assert len(results) == 3


class TestGatherDailyData:
    """Test daily data gathering."""

    def test_gathers_conversations(self, test_db):
        conv_id = conversation.create_conversation()
        conversation.set_conversation_title(conv_id, "Test conversation")
        conversation.add_message(conv_id, "user", "Hello")

        data = reflection.gather_daily_data()
        assert "Test conversation" in data
        assert "Daily Reflection Data" in data

    def test_includes_conversation_summaries(self, test_db):
        conv_id = conversation.create_conversation()
        conversation.set_conversation_summary(conv_id, "Discussed X and Y")

        data = reflection.gather_daily_data()
        assert "Discussed X and Y" in data

    def test_empty_day(self, test_db):
        data = reflection.gather_daily_data()
        assert "No conversations in the last 24 hours" in data


class TestGatherWeeklyData:
    """Test weekly data gathering."""

    def test_includes_daily_reflections(self, test_db):
        reflection.save_reflection("daily", "2026-03-12", "2026-03-13", "Daily observation")

        data = reflection.gather_weekly_data()
        assert "Weekly Reflection Data" in data
        assert "Daily observation" in data

    def test_no_daily_reflections(self, test_db):
        data = reflection.gather_weekly_data()
        assert "No daily reflections available" in data


class TestGatherMonthlyData:
    """Test monthly data gathering."""

    def test_includes_weekly_reflections(self, test_db):
        reflection.save_reflection("weekly", "2026-03-06", "2026-03-13", "Weekly pattern")

        data = reflection.gather_monthly_data()
        assert "Monthly Reflection Data" in data
        assert "Weekly pattern" in data

    def test_includes_skill_knowledge_entries(self, test_db, tmp_path):
        """Monthly data should include KB skill entries."""
        from carpenter.kb.store import KBStore
        import carpenter.config

        kb_dir = str(tmp_path / "kb")
        import os
        os.makedirs(kb_dir, exist_ok=True)
        store = KBStore(kb_dir=kb_dir)
        store.write_entry(
            "skills/test-skill", "# Test Skill\n\nA test skill.",
            description="A test skill", validate_links=False,
        )

        # Monkeypatch get_store to return our test store
        import carpenter.kb
        original = carpenter.kb.get_store
        carpenter.kb.get_store = lambda: store
        try:
            data = reflection.gather_monthly_data()
            assert "test-skill" in data
        finally:
            carpenter.kb.get_store = original


class TestGatherPeriodStats:
    """Test the common period stats gathering."""

    def test_counts_conversations(self, test_db):
        conversation.create_conversation()
        conversation.create_conversation()

        stats = reflection._gather_period_stats(1)
        assert "Conversations: 2" in stats

    def test_counts_tools(self, test_db):
        # Insert some tool call records
        conv_id = conversation.create_conversation()
        msg_id = conversation.add_message(conv_id, "assistant", "test")
        db = get_db()
        try:
            db.execute(
                "INSERT INTO tool_calls (conversation_id, message_id, tool_use_id, tool_name, input_json, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (conv_id, msg_id, "t1", "read_file", "{}", 50),
            )
            db.execute(
                "INSERT INTO tool_calls (conversation_id, message_id, tool_use_id, tool_name, input_json, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (conv_id, msg_id, "t2", "read_file", "{}", 30),
            )
            db.commit()
        finally:
            db.close()

        stats = reflection._gather_period_stats(1)
        assert "read_file" in stats
        assert "2 calls" in stats


class TestCronRegistration:
    """Test idempotent cron registration for reflections."""

    def test_cron_registration_idempotent(self, test_db, monkeypatch):
        """Registering cron twice should not error (UNIQUE name constraint caught)."""
        monkeypatch.setitem(config.CONFIG, "reflection", {"enabled": True})

        from carpenter.core.engine import trigger_manager

        # First registration
        trigger_manager.add_cron(
            "daily-reflection", "0 23 * * *", "reflection.trigger",
            {"cadence": "daily"},
        )

        # Second registration should not raise (caught by try/except in http.py pattern)
        try:
            trigger_manager.add_cron(
                "daily-reflection", "0 23 * * *", "reflection.trigger",
                {"cadence": "daily"},
            )
        except Exception:
            pass  # Expected — UNIQUE constraint

        # Should still have exactly one entry
        entries = trigger_manager.list_cron()
        daily_entries = [e for e in entries if e["name"] == "daily-reflection"]
        assert len(daily_entries) == 1

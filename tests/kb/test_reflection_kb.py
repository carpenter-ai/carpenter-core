"""Tests for reflection → KB entry creation."""

from carpenter.db import get_db
from carpenter.kb import get_store
from carpenter.kb.reflection_kb import (
    backfill_reflections,
    create_reflection_entry,
)


def _create_reflection(cadence, period_start, period_end, content, proposed_actions=None):
    """Insert a test reflection and return its ID."""
    db = get_db()
    try:
        cursor = db.execute(
            "INSERT INTO reflections (cadence, period_start, period_end, content, proposed_actions) "
            "VALUES (?, ?, ?, ?, ?)",
            (cadence, period_start, period_end, content, proposed_actions),
        )
        db.commit()
        return cursor.lastrowid
    finally:
        db.close()


class TestCreateReflectionEntry:
    def test_creates_entry(self):
        refl_id = _create_reflection(
            "daily", "2025-01-01", "2025-01-01",
            "Observed high tool usage patterns today.",
        )
        store = get_store()
        path = create_reflection_entry(refl_id, store)
        assert path == "reflections/daily/2025-01-01"

        entry = store.get_entry(path)
        assert entry is not None
        assert "tool usage" in entry["content"].lower()
        assert entry["entry_type"] == "reflection"

    def test_returns_none_without_content(self):
        refl_id = _create_reflection("daily", "2025-01-02", "2025-01-02", "")
        store = get_store()
        path = create_reflection_entry(refl_id, store)
        assert path is None

    def test_includes_proposed_actions(self):
        refl_id = _create_reflection(
            "weekly", "2025-01-01", "2025-01-07",
            "Weekly review of activity.",
            proposed_actions="Reduce API calls by caching responses.",
        )
        store = get_store()
        path = create_reflection_entry(refl_id, store)
        assert path is not None

        entry = store.get_entry(path)
        assert "caching responses" in entry["content"].lower()

    def test_backfills_existing(self):
        _create_reflection("daily", "2025-02-01", "2025-02-01", "Day one.")
        _create_reflection("daily", "2025-02-02", "2025-02-02", "Day two.")
        _create_reflection("daily", "2025-02-03", "2025-02-03", "")  # empty

        store = get_store()
        count = backfill_reflections(store)
        assert count == 2

    def test_high_water_mark_skips_already_backfilled(self):
        _create_reflection("daily", "2025-03-01", "2025-03-01", "March first.")
        _create_reflection("daily", "2025-03-02", "2025-03-02", "March second.")

        store = get_store()
        count1 = backfill_reflections(store)
        assert count1 == 2

        # Second backfill should find nothing new
        count2 = backfill_reflections(store)
        assert count2 == 0

        # Add a new reflection — only it should be backfilled
        _create_reflection("weekly", "2025-03-01", "2025-03-07", "Week one.")
        count3 = backfill_reflections(store)
        assert count3 == 1

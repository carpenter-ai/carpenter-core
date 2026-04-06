"""Tests for carpenter.core.models.speed_tracker."""

import sqlite3
import textwrap
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from carpenter.core.models.speed_tracker import compute_measured_speeds, update_registry_speeds
from carpenter.core.models.registry import ModelEntry


@pytest.fixture
def speed_db(tmp_path, monkeypatch):
    """Create a test database with api_calls table and sample data."""
    db_path = str(tmp_path / "test_speed.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_calls (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER,
            model TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_creation_input_tokens INTEGER DEFAULT 0,
            cache_read_input_tokens INTEGER DEFAULT 0,
            stop_reason TEXT,
            latency_ms INTEGER,
            arc_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

    # Monkeypatch get_db to return our test DB
    def mock_get_db():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr("carpenter.db.get_db", mock_get_db)

    return conn


def _insert_call(conn, model, latency_ms, output_tokens, hours_ago=0):
    """Insert a test api_call row."""
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    conn.execute(
        "INSERT INTO api_calls (model, latency_ms, output_tokens, created_at) "
        "VALUES (?, ?, ?, ?)",
        (model, latency_ms, output_tokens, ts),
    )
    conn.commit()


class TestComputeMeasuredSpeeds:
    def test_empty_table(self, speed_db):
        assert compute_measured_speeds() == {}

    def test_single_call(self, speed_db):
        _insert_call(speed_db, "claude-sonnet-4-5-20250929", 2000, 500)
        # 2s / 0.5ktok = 4.0 s/ktok
        speeds = compute_measured_speeds()
        assert "claude-sonnet-4-5-20250929" in speeds
        assert speeds["claude-sonnet-4-5-20250929"] == 4.0

    def test_median_odd(self, speed_db):
        """Median of 3 values = middle value."""
        _insert_call(speed_db, "test-model", 1000, 1000)  # 1s/ktok
        _insert_call(speed_db, "test-model", 3000, 1000)  # 3s/ktok
        _insert_call(speed_db, "test-model", 5000, 1000)  # 5s/ktok
        speeds = compute_measured_speeds()
        assert speeds["test-model"] == 3.0

    def test_median_even(self, speed_db):
        """Median of 4 values = average of middle two."""
        _insert_call(speed_db, "test-model", 1000, 1000)  # 1s/ktok
        _insert_call(speed_db, "test-model", 2000, 1000)  # 2s/ktok
        _insert_call(speed_db, "test-model", 3000, 1000)  # 3s/ktok
        _insert_call(speed_db, "test-model", 4000, 1000)  # 4s/ktok
        speeds = compute_measured_speeds()
        assert speeds["test-model"] == 2.5

    def test_multiple_models(self, speed_db):
        _insert_call(speed_db, "model-a", 1000, 1000)
        _insert_call(speed_db, "model-b", 5000, 1000)
        speeds = compute_measured_speeds()
        assert "model-a" in speeds
        assert "model-b" in speeds
        assert speeds["model-a"] < speeds["model-b"]

    def test_skips_null_latency(self, speed_db):
        speed_db.execute(
            "INSERT INTO api_calls (model, latency_ms, output_tokens) VALUES (?, NULL, ?)",
            ("test-model", 1000),
        )
        speed_db.commit()
        assert compute_measured_speeds() == {}

    def test_skips_zero_output(self, speed_db):
        _insert_call(speed_db, "test-model", 1000, 0)
        assert compute_measured_speeds() == {}

    def test_respects_days_cutoff(self, speed_db):
        # Recent call
        _insert_call(speed_db, "recent-model", 1000, 1000, hours_ago=1)
        # Old call (10 days ago)
        _insert_call(speed_db, "old-model", 1000, 1000, hours_ago=240)
        speeds = compute_measured_speeds(days=7)
        assert "recent-model" in speeds
        assert "old-model" not in speeds


class TestUpdateRegistrySpeeds:
    def test_updates_registry(self, speed_db, monkeypatch):
        _insert_call(speed_db, "claude-sonnet-4-5-20250929", 2000, 500)

        updated_keys = {}

        def mock_update(key, speed):
            updated_keys[key] = speed

        registry = {
            "sonnet": ModelEntry(
                key="sonnet", provider="anthropic",
                model_id="claude-sonnet-4-5-20250929",
                quality_tier=4, cost_per_mtok_in=3.0,
                cost_per_mtok_out=15.0, cached_cost_per_mtok_in=0.3,
                context_window=200000, capabilities=[],
            ),
        }

        monkeypatch.setattr(
            "carpenter.core.models.registry.get_registry",
            lambda: registry,
        )
        monkeypatch.setattr(
            "carpenter.core.models.registry.get_entry_by_model_id",
            lambda model_id: registry.get("sonnet") if "sonnet" in model_id else None,
        )
        monkeypatch.setattr(
            "carpenter.core.models.registry.update_measured_speed",
            mock_update,
        )

        count = update_registry_speeds()
        assert count == 1
        assert "sonnet" in updated_keys

    def test_returns_zero_when_empty(self, speed_db, monkeypatch):
        monkeypatch.setattr(
            "carpenter.core.models.registry.get_registry",
            lambda: {},
        )
        assert update_registry_speeds() == 0

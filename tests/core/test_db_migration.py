"""Tests for database migrations in carpenter.db."""

import sqlite3

import pytest

from carpenter.db import get_db, init_db


class TestModelCallsProviderMigration:
    def test_model_calls_provider_column_added(self):
        """After init_db, model_calls should have a provider column."""
        db = get_db()
        try:
            cols = {row[1] for row in db.execute("PRAGMA table_info(model_calls)").fetchall()}
            assert "provider" in cols
        finally:
            db.close()

    def test_model_calls_provider_backfill(self):
        """Provider column is backfilled from model_id for existing rows."""
        db = get_db()
        try:
            # Insert rows without provider (simulating pre-migration data)
            db.execute(
                "INSERT INTO model_calls (model_id, success, called_at, provider) "
                "VALUES ('anthropic:claude-sonnet', 1, '2026-01-01T00:00:00Z', 'anthropic')"
            )
            db.execute(
                "INSERT INTO model_calls (model_id, success, called_at, provider) "
                "VALUES ('claude-haiku', 1, '2026-01-01T00:00:00Z', 'anthropic')"
            )
            db.commit()

            # Verify provider values
            rows = db.execute(
                "SELECT model_id, provider FROM model_calls ORDER BY model_id"
            ).fetchall()

            by_model = {row["model_id"]: row["provider"] for row in rows}
            assert by_model["anthropic:claude-sonnet"] == "anthropic"
            assert by_model["claude-haiku"] == "anthropic"
        finally:
            db.close()

    def test_model_calls_provider_index_exists(self):
        """Provider index should exist after migration."""
        db = get_db()
        try:
            indexes = {row[1] for row in db.execute("PRAGMA index_list(model_calls)").fetchall()}
            assert "idx_model_calls_provider" in indexes
        finally:
            db.close()

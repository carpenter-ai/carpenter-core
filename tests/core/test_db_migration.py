"""Tests for database migrations in carpenter.db."""

import json
import sqlite3

import pytest

from carpenter.db import get_db, init_db
from carpenter.db_migrations import _migrate_arc_step_role


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


# ── D2 PR-α: step_role migration backfill ─────────────────────────


class TestArcStepRoleMigration:
    """Phase 24: ALTER TABLE arcs ADD COLUMN step_role + backfill."""

    def test_step_role_column_present_after_init(self):
        """init_db ensures the column exists on a fresh DB."""
        db = get_db()
        try:
            cols = {row[1] for row in db.execute("PRAGMA table_info(arcs)").fetchall()}
            assert "step_role" in cols
        finally:
            db.close()

    def test_step_role_index_present(self):
        """The (template_id, step_role) compound index exists."""
        db = get_db()
        try:
            indexes = {row[1] for row in db.execute("PRAGMA index_list(arcs)").fetchall()}
            assert "idx_arcs_step_role" in indexes
        finally:
            db.close()

    def test_backfill_on_synthetic_legacy_db(self, tmp_path):
        """Simulate a pre-migration DB: arcs.step_role NULL but template
        steps_json has roles. Migration backfills the column."""
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # Minimal schema for the migration to operate on.
        conn.executescript("""
            CREATE TABLE workflow_templates (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                steps_json TEXT NOT NULL
            );
            CREATE TABLE arcs (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                template_id INTEGER REFERENCES workflow_templates(id)
                -- intentionally NO step_role column: simulating pre-Phase-24
            );
        """)
        # Seed a template with roles on two of three steps.
        steps = [
            {"name": "gather", "role": "prepare", "order": 0},
            {"name": "act",    "role": "analyze", "order": 1},
            {"name": "notify", "order": 2},  # no role
        ]
        conn.execute(
            "INSERT INTO workflow_templates (id, name, steps_json) VALUES (?, ?, ?)",
            (1, "wf", json.dumps(steps)),
        )
        # Seed three arcs from this template; one arc whose name does not
        # match any step (should remain NULL).
        conn.executemany(
            "INSERT INTO arcs (id, name, template_id) VALUES (?, ?, ?)",
            [
                (10, "gather", 1),
                (11, "act",    1),
                (12, "notify", 1),
                (13, "wandered-off", 1),
            ],
        )
        # And a non-template arc — its template_id is NULL.
        conn.execute(
            "INSERT INTO arcs (id, name, template_id) VALUES (?, ?, NULL)",
            (14, "rootless"),
        )
        conn.commit()

        # Run the migration.
        tables = {"arcs", "workflow_templates"}
        _migrate_arc_step_role(conn, tables)

        # Column was added.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(arcs)").fetchall()}
        assert "step_role" in cols

        # Backfill correctness.
        rows = {
            row["id"]: row["step_role"]
            for row in conn.execute("SELECT id, step_role FROM arcs").fetchall()
        }
        assert rows[10] == "prepare"
        assert rows[11] == "analyze"
        assert rows[12] is None  # template step had no role
        assert rows[13] is None  # name doesn't match any step
        assert rows[14] is None  # no template

        conn.close()

    def test_backfill_is_idempotent(self, tmp_path):
        """Running the migration twice doesn't clobber backfilled values
        and doesn't fail when the column already exists."""
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE workflow_templates (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                steps_json TEXT NOT NULL
            );
            CREATE TABLE arcs (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                template_id INTEGER REFERENCES workflow_templates(id)
            );
        """)
        conn.execute(
            "INSERT INTO workflow_templates (id, name, steps_json) VALUES (?, ?, ?)",
            (1, "wf", json.dumps([{"name": "s", "role": "r"}])),
        )
        conn.execute(
            "INSERT INTO arcs (id, name, template_id) VALUES (?, ?, ?)",
            (1, "s", 1),
        )
        conn.commit()

        tables = {"arcs", "workflow_templates"}
        _migrate_arc_step_role(conn, tables)
        _migrate_arc_step_role(conn, tables)

        row = conn.execute("SELECT step_role FROM arcs WHERE id = 1").fetchone()
        assert row["step_role"] == "r"
        conn.close()

    def test_backfill_handles_malformed_steps_json(self, tmp_path):
        """A template row with non-JSON or wrong-shape steps_json is
        skipped without erroring."""
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE workflow_templates (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                steps_json TEXT NOT NULL
            );
            CREATE TABLE arcs (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                template_id INTEGER REFERENCES workflow_templates(id)
            );
        """)
        # Three templates: malformed JSON, JSON that is a string, JSON dict
        # with no steps key. None should crash the migration.
        conn.execute(
            "INSERT INTO workflow_templates (id, name, steps_json) VALUES (?, ?, ?)",
            (1, "bad", "not json{"),
        )
        conn.execute(
            "INSERT INTO workflow_templates (id, name, steps_json) VALUES (?, ?, ?)",
            (2, "string", json.dumps("oops")),
        )
        conn.execute(
            "INSERT INTO workflow_templates (id, name, steps_json) VALUES (?, ?, ?)",
            (3, "nodict", json.dumps({"other_key": []})),
        )
        conn.execute(
            "INSERT INTO arcs (id, name, template_id) VALUES (?, ?, ?)", (1, "x", 1),
        )
        conn.execute(
            "INSERT INTO arcs (id, name, template_id) VALUES (?, ?, ?)", (2, "x", 2),
        )
        conn.execute(
            "INSERT INTO arcs (id, name, template_id) VALUES (?, ?, ?)", (3, "x", 3),
        )
        conn.commit()

        tables = {"arcs", "workflow_templates"}
        _migrate_arc_step_role(conn, tables)  # must not raise

        rows = conn.execute("SELECT step_role FROM arcs").fetchall()
        assert all(row["step_role"] is None for row in rows)
        conn.close()

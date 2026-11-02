"""Tests for database migrations in carpenter.db."""

import json
import sqlite3

import pytest

from carpenter.db import get_db, init_db
from carpenter.db_migrations import (
    _migrate_arc_step_role,
    _migrate_dedupe_model_policy_presets,
)


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


# ── model_policies preset de-duplication ──────────────────────────


def _legacy_policy_db(tmp_path, filename="legacy_policies.db"):
    """model_policies as it was before the partial unique index existed."""
    conn = sqlite3.connect(str(tmp_path / filename))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE model_policies (
            id INTEGER PRIMARY KEY,
            name TEXT,
            model TEXT,
            agent_role TEXT,
            temperature REAL,
            max_tokens INTEGER,
            policy_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE arcs (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            model_policy_id INTEGER
        );
    """)
    return conn


def _body(name):
    return json.dumps({"constraints": {"min_quality": 2}, "preference": [0.3, 0.4, 0.3], "n": name})


def _reseed(conn, names, times):
    """Simulate `times` platform starts seeding `names` with no index present."""
    for _ in range(times):
        for name in names:
            conn.execute(
                "INSERT OR IGNORE INTO model_policies (name, policy_json) VALUES (?, ?)",
                (name, _body(name)),
            )
    conn.commit()


class TestModelPolicyPresetDedupe:
    """Preset rows accumulated one copy per start before the index existed."""

    def test_preset_index_present_after_init(self):
        """init_db leaves the partial unique index on model_policies."""
        db = get_db()
        try:
            indexes = {
                row[1] for row in db.execute("PRAGMA index_list('model_policies')").fetchall()
            }
        finally:
            db.close()
        assert "idx_model_policies_preset" in indexes

    def test_reseeding_without_the_index_duplicates(self, tmp_path):
        """The bug itself: OR IGNORE has nothing to conflict on."""
        conn = _legacy_policy_db(tmp_path)
        _reseed(conn, ["fast-chat", "caretaker"], 5)
        assert conn.execute("SELECT COUNT(*) FROM model_policies").fetchone()[0] == 10
        conn.close()

    def test_collapses_duplicates_to_the_lowest_id(self, tmp_path):
        """One row survives per preset, and it is the lowest id."""
        conn = _legacy_policy_db(tmp_path)
        _reseed(conn, ["fast-chat", "caretaker"], 5)
        lowest = {
            row["name"]: row["keep"]
            for row in conn.execute(
                "SELECT name, MIN(id) AS keep FROM model_policies GROUP BY name"
            ).fetchall()
        }

        _migrate_dedupe_model_policy_presets(conn, {"model_policies", "arcs"})

        rows = conn.execute("SELECT id, name FROM model_policies ORDER BY id").fetchall()
        assert len(rows) == 2
        assert {row["name"]: row["id"] for row in rows} == lowest
        conn.close()

    def test_repoints_arcs_at_the_surviving_row(self, tmp_path):
        """An arc pointing at a duplicate is moved, not orphaned.

        model_policy_id is not a declared foreign key, so nothing in the
        database would have stopped the delete from orphaning it.
        """
        conn = _legacy_policy_db(tmp_path)
        _reseed(conn, ["fast-chat"], 4)
        ids = [row["id"] for row in conn.execute(
            "SELECT id FROM model_policies ORDER BY id"
        ).fetchall()]
        conn.execute(
            "INSERT INTO arcs (id, name, model_policy_id) VALUES (1, 'a', ?)", (ids[-1],)
        )
        conn.execute(
            "INSERT INTO arcs (id, name, model_policy_id) VALUES (2, 'b', ?)", (ids[0],)
        )
        conn.execute("INSERT INTO arcs (id, name, model_policy_id) VALUES (3, 'c', NULL)")
        conn.commit()

        _migrate_dedupe_model_policy_presets(conn, {"model_policies", "arcs"})

        pointers = [row["model_policy_id"] for row in conn.execute(
            "SELECT model_policy_id FROM arcs ORDER BY id"
        ).fetchall()]
        assert pointers == [ids[0], ids[0], None]
        conn.close()

    def test_safe_to_run_twice(self, tmp_path):
        """Second run is a no-op and does not raise."""
        conn = _legacy_policy_db(tmp_path)
        _reseed(conn, ["fast-chat", "caretaker"], 3)

        _migrate_dedupe_model_policy_presets(conn, {"model_policies", "arcs"})
        after_first = conn.execute(
            "SELECT id, name, policy_json FROM model_policies ORDER BY id"
        ).fetchall()
        _migrate_dedupe_model_policy_presets(conn, {"model_policies", "arcs"})
        after_second = conn.execute(
            "SELECT id, name, policy_json FROM model_policies ORDER BY id"
        ).fetchall()

        assert [tuple(row) for row in after_first] == [tuple(row) for row in after_second]
        conn.close()

    def test_database_without_duplicates_is_untouched(self, tmp_path):
        """A clean database keeps every row and still gains the index."""
        conn = _legacy_policy_db(tmp_path)
        _reseed(conn, ["fast-chat", "caretaker", "careful-coding"], 1)
        before = [tuple(row) for row in conn.execute(
            "SELECT id, name, policy_json FROM model_policies ORDER BY id"
        ).fetchall()]

        _migrate_dedupe_model_policy_presets(conn, {"model_policies", "arcs"})

        after = [tuple(row) for row in conn.execute(
            "SELECT id, name, policy_json FROM model_policies ORDER BY id"
        ).fetchall()]
        assert before == after
        indexes = {row[1] for row in conn.execute(
            "PRAGMA index_list('model_policies')"
        ).fetchall()}
        assert "idx_model_policies_preset" in indexes
        conn.close()

    def test_index_blocks_a_later_reseed(self, tmp_path):
        """After the migration, OR IGNORE has a constraint to conflict on."""
        conn = _legacy_policy_db(tmp_path)
        _reseed(conn, ["fast-chat"], 6)
        _migrate_dedupe_model_policy_presets(conn, {"model_policies", "arcs"})

        _reseed(conn, ["fast-chat"], 10)

        assert conn.execute("SELECT COUNT(*) FROM model_policies").fetchone()[0] == 1
        conn.close()

    def test_distinct_policy_bodies_survive(self, tmp_path):
        """Same name, different body: not a re-seed, so both rows stay."""
        conn = _legacy_policy_db(tmp_path)
        conn.execute(
            "INSERT INTO model_policies (name, policy_json) VALUES ('fast-chat', ?)",
            (json.dumps({"preference": [0.3, 0.4, 0.3]}),),
        )
        conn.execute(
            "INSERT INTO model_policies (name, policy_json) VALUES ('fast-chat', ?)",
            (json.dumps({"preference": [0.9, 0.1, 0.0]}),),
        )
        conn.commit()

        _migrate_dedupe_model_policy_presets(conn, {"model_policies", "arcs"})

        assert conn.execute("SELECT COUNT(*) FROM model_policies").fetchone()[0] == 2
        conn.close()

    def test_pinned_and_overriding_rows_survive(self, tmp_path):
        """Rows outside the preset shape are never collapsed."""
        conn = _legacy_policy_db(tmp_path)
        body = _body("fast-chat")
        # Hard-pinned rows: policy_json NULL, name repeated.
        for model in ("prov:a", "prov:b"):
            conn.execute(
                "INSERT INTO model_policies (name, model) VALUES ('fast-chat', ?)", (model,)
            )
        # Named selector policies carrying a per-call override.
        conn.execute(
            "INSERT INTO model_policies (name, model, policy_json) "
            "VALUES ('fast-chat', 'prov:a', ?)", (body,)
        )
        conn.execute(
            "INSERT INTO model_policies (name, agent_role, policy_json) "
            "VALUES ('fast-chat', 'coder', ?)", (body,)
        )
        conn.execute(
            "INSERT INTO model_policies (name, temperature, policy_json) "
            "VALUES ('fast-chat', 0.2, ?)", (body,)
        )
        conn.execute(
            "INSERT INTO model_policies (name, max_tokens, policy_json) "
            "VALUES ('fast-chat', 4096, ?)", (body,)
        )
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM model_policies").fetchone()[0]

        _migrate_dedupe_model_policy_presets(conn, {"model_policies", "arcs"})

        assert conn.execute("SELECT COUNT(*) FROM model_policies").fetchone()[0] == before
        conn.close()

    def test_noop_when_table_absent(self, tmp_path):
        """A database with no model_policies table is left alone."""
        conn = sqlite3.connect(str(tmp_path / "empty.db"))
        conn.row_factory = sqlite3.Row
        _migrate_dedupe_model_policy_presets(conn, set())  # must not raise
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0] == 0
        conn.close()

    def test_dedupe_without_an_arcs_table(self, tmp_path):
        """De-duplication still runs when there are no arcs to repoint."""
        conn = sqlite3.connect(str(tmp_path / "no_arcs.db"))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE model_policies (
                id INTEGER PRIMARY KEY,
                name TEXT,
                model TEXT,
                agent_role TEXT,
                temperature REAL,
                max_tokens INTEGER,
                policy_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        _reseed(conn, ["fast-chat"], 3)

        _migrate_dedupe_model_policy_presets(conn, {"model_policies"})

        assert conn.execute("SELECT COUNT(*) FROM model_policies").fetchone()[0] == 1
        conn.close()

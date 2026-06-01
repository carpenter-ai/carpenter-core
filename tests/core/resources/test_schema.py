"""Schema-level tests for the Resource tables."""

from carpenter.db import get_db


class TestResourcesTable:
    def test_table_exists(self):
        db = get_db()
        try:
            row = db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='resources'"
            ).fetchone()
            assert row is not None
        finally:
            db.close()

    def test_expected_columns(self):
        db = get_db()
        try:
            cols = {row[1]: row for row in db.execute(
                "PRAGMA table_info(resources)"
            ).fetchall()}
        finally:
            db.close()

        expected = {
            "id", "content_type", "file_path", "byte_size", "content_hash",
            "produced_by_arc_id", "produced_by_template", "template_verdict",
            "source_descriptor", "pinned", "retain_until",
            "created_at", "deprecated_at", "deleted_at",
        }
        assert expected.issubset(cols.keys())

        # content_type is NOT NULL
        assert cols["content_type"][3] == 1
        # pinned is NOT NULL with default 0
        assert cols["pinned"][3] == 1

    def test_indexes_exist(self):
        db = get_db()
        try:
            indexes = {row[1] for row in db.execute(
                "PRAGMA index_list(resources)"
            ).fetchall()}
        finally:
            db.close()

        for name in (
            "idx_resources_arc",
            "idx_resources_sweep",
            "idx_resources_content",
        ):
            assert name in indexes, f"missing index {name}"

    def test_template_verdict_check_constraint(self):
        """CHECK (template_verdict IN (...)) rejects bogus values."""
        db = get_db()
        try:
            # NULL is allowed
            db.execute(
                "INSERT INTO resources (content_type, template_verdict) "
                "VALUES ('text/plain', NULL)"
            )
            # Valid values allowed
            for v in ("pending", "approved", "rejected"):
                db.execute(
                    "INSERT INTO resources (content_type, template_verdict) "
                    "VALUES ('text/plain', ?)",
                    (v,),
                )
            db.commit()

            # Invalid rejected
            try:
                db.execute(
                    "INSERT INTO resources (content_type, template_verdict) "
                    "VALUES ('text/plain', 'maybe')"
                )
                db.commit()
                raise AssertionError("CHECK constraint should have rejected 'maybe'")
            except Exception as exc:
                assert "CHECK" in str(exc) or "constraint" in str(exc).lower()
        finally:
            db.close()


class TestArcResourcesTable:
    def test_table_exists(self):
        db = get_db()
        try:
            row = db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='arc_resources'"
            ).fetchone()
            assert row is not None
        finally:
            db.close()

    def test_expected_columns(self):
        db = get_db()
        try:
            cols = {row[1]: row for row in db.execute(
                "PRAGMA table_info(arc_resources)"
            ).fetchall()}
        finally:
            db.close()

        expected = {"id", "arc_id", "resource_id", "role", "created_at"}
        assert expected.issubset(cols.keys())
        assert cols["arc_id"][3] == 1
        assert cols["resource_id"][3] == 1
        assert cols["role"][3] == 1

    def test_indexes_exist(self):
        db = get_db()
        try:
            indexes = {row[1] for row in db.execute(
                "PRAGMA index_list(arc_resources)"
            ).fetchall()}
        finally:
            db.close()

        assert "idx_arc_resources_arc" in indexes
        assert "idx_arc_resources_res" in indexes

    def test_role_check_constraint(self):
        db = get_db()
        try:
            # Need a real arc and resource to satisfy FKs (FKs ON).
            arc_id = db.execute(
                "INSERT INTO arcs (name, status) VALUES ('a', 'pending')"
            ).lastrowid
            res_id = db.execute(
                "INSERT INTO resources (content_type) VALUES ('text/plain')"
            ).lastrowid
            db.commit()

            # Valid roles work
            db.execute(
                "INSERT INTO arc_resources (arc_id, resource_id, role) "
                "VALUES (?, ?, 'input')",
                (arc_id, res_id),
            )
            db.commit()

            # Invalid role rejected
            try:
                db.execute(
                    "INSERT INTO arc_resources (arc_id, resource_id, role) "
                    "VALUES (?, ?, 'bogus')",
                    (arc_id, res_id),
                )
                db.commit()
                raise AssertionError("CHECK role constraint should have fired")
            except Exception as exc:
                assert "CHECK" in str(exc) or "constraint" in str(exc).lower()
        finally:
            db.close()

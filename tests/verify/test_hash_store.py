"""Tests for hash registry (verify/hash_store.py)."""

import pytest
import json

from carpenter.verify.hash_store import (
    compute_code_hash,
    check_verified_hash,
    add_verified_hash,
    invalidate_stale_hashes,
)
from carpenter.db import get_db


class TestComputeHash:
    def test_deterministic(self):
        h1 = compute_code_hash("x = 1")
        h2 = compute_code_hash("x = 1")
        assert h1 == h2

    def test_different_code_different_hash(self):
        h1 = compute_code_hash("x = 1")
        h2 = compute_code_hash("x = 2")
        assert h1 != h2

    def test_sha256_length(self):
        h = compute_code_hash("test")
        assert len(h) == 64  # SHA-256 hex digest


class TestAddAndCheck:
    def test_roundtrip(self):
        h = compute_code_hash("x = 1")
        add_verified_hash(h, "[]", 0)
        result = check_verified_hash(h)
        assert result is not None
        assert result["code_hash"] == h

    def test_missing_hash(self):
        result = check_verified_hash("0" * 64)
        assert result is None

    def test_stale_hash_returns_none(self):
        """Hash with old policy version returns None."""
        h = compute_code_hash("x = 1")
        add_verified_hash(h, "[]", 0)

        # Increment policy version
        db = get_db()
        try:
            db.execute(
                "INSERT OR REPLACE INTO arc_state (arc_id, key, value_json) "
                "VALUES (0, '_policy_version', ?)",
                (json.dumps(5),),
            )
            db.commit()
        finally:
            db.close()

        result = check_verified_hash(h)
        assert result is None


class TestInvalidation:
    def test_invalidate_removes_old(self):
        h1 = compute_code_hash("x = 1")
        h2 = compute_code_hash("x = 2")
        add_verified_hash(h1, "[]", 0)
        add_verified_hash(h2, "[]", 0)

        # Set current version to 5
        db = get_db()
        try:
            db.execute(
                "INSERT OR REPLACE INTO arc_state (arc_id, key, value_json) "
                "VALUES (0, '_policy_version', ?)",
                (json.dumps(5),),
            )
            db.commit()
        finally:
            db.close()

        count = invalidate_stale_hashes()
        assert count == 2

    def test_invalidate_keeps_current(self):
        # Set version to 3
        db = get_db()
        try:
            db.execute(
                "INSERT OR REPLACE INTO arc_state (arc_id, key, value_json) "
                "VALUES (0, '_policy_version', ?)",
                (json.dumps(3),),
            )
            db.commit()
        finally:
            db.close()

        h = compute_code_hash("x = 1")
        add_verified_hash(h, "[]", 3)

        count = invalidate_stale_hashes()
        assert count == 0

        result = check_verified_hash(h)
        assert result is not None

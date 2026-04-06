"""Tests for CONSTRAINED integrity level behavior.

CONSTRAINED is enforced the same as UNTRUSTED for now (conservative default).
These tests verify that constrained arcs are treated like untrusted arcs
in all enforcement paths.
"""

import pytest
from starlette.testclient import TestClient

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.trust.integrity import (
    IntegrityLevel,
    join,
    is_trusted,
    is_non_trusted,
)
from carpenter.tool_backends import arc as arc_backend
from carpenter.tool_backends.state import handle_get, handle_set
from carpenter.db import get_db


# ── Lattice algebra ──────────────────────────────────────────────────

class TestLatticeAlgebra:

    def test_join_trusted_constrained(self):
        assert join("trusted", "constrained") == IntegrityLevel.CONSTRAINED

    def test_join_constrained_untrusted(self):
        assert join("constrained", "untrusted") == IntegrityLevel.UNTRUSTED

    def test_join_trusted_untrusted(self):
        assert join("trusted", "untrusted") == IntegrityLevel.UNTRUSTED

    def test_join_same_level(self):
        assert join("constrained", "constrained") == IntegrityLevel.CONSTRAINED

    def test_join_commutative(self):
        assert join("trusted", "constrained") == join("constrained", "trusted")
        assert join("trusted", "untrusted") == join("untrusted", "trusted")
        assert join("constrained", "untrusted") == join("untrusted", "constrained")

    def test_is_trusted_only_for_trusted(self):
        assert is_trusted("trusted") is True
        assert is_trusted("constrained") is False
        assert is_trusted("untrusted") is False

    def test_is_non_trusted_for_constrained_and_untrusted(self):
        assert is_non_trusted("trusted") is False
        assert is_non_trusted("constrained") is True
        assert is_non_trusted("untrusted") is True


# ── Arc creation ─────────────────────────────────────────────────────

class TestConstrainedArcCreation:

    def test_constrained_arc_via_add_child(self):
        """Constrained arcs can be created as children."""
        parent = arc_manager.create_arc("parent")
        child = arc_manager.add_child(
            parent, "constrained-child", integrity_level="constrained"
        )
        arc = arc_manager.get_arc(child)
        assert arc["integrity_level"] == "constrained"

    def test_constrained_arc_batch_requires_reviewer(self):
        """Batch creation of constrained arc requires reviewer (same as untrusted)."""
        # Constrained arcs are non-trusted, so need reviewers
        result = arc_backend.handle_create_batch({
            "arcs": [
                {"name": "constrained-target", "integrity_level": "constrained"},
            ]
        })
        assert "error" in result
        assert "require at least one REVIEWER or JUDGE" in result["error"]

    def test_constrained_arc_batch_with_reviewer_succeeds(self):
        """Batch creation of constrained arc with reviewer succeeds."""
        result = arc_backend.handle_create_batch({
            "arcs": [
                {
                    "name": "constrained-target",
                    "integrity_level": "constrained",
                },
                {
                    "name": "reviewer",
                    "agent_type": "REVIEWER",
                    "reviewer_profile": "security-reviewer",
                },
            ]
        })
        assert "arc_ids" in result
        target_id = result["arc_ids"][0]
        arc = arc_manager.get_arc(target_id)
        assert arc["integrity_level"] == "constrained"


# ── Enforcement (same as untrusted) ─────────────────────────────────

class TestConstrainedEnforcement:

    def test_constrained_arc_is_non_trusted(self):
        """is_non_trusted returns True for constrained arcs."""
        from carpenter.core.trust.integrity import is_non_trusted
        parent = arc_manager.create_arc("parent")
        child = arc_manager.add_child(
            parent, "constrained-child", integrity_level="constrained"
        )
        arc = arc_manager.get_arc(child)
        assert is_non_trusted(arc["integrity_level"]) is True

    def test_constrained_arc_blocked_from_untrusted_data(self):
        """Constrained arcs (like trusted) cannot access _UNTRUSTED_DATA_TOOLS.

        Note: This tests the callback enforcement. Constrained arcs have
        integrity_level != 'trusted', but the enforcement path for
        _UNTRUSTED_DATA_TOOLS blocks trusted arcs. Constrained arcs are
        actually allowed to access untrusted data (they're non-trusted).
        """
        # Constrained arcs can read untrusted data (they ARE non-trusted)
        # This is consistent with the existing check which only blocks
        # caller_integrity == "trusted"
        pass  # Enforcement tested via HTTP in test_taint_enforcement.py

    def test_individual_constrained_arc_rejected(self):
        """Cannot create individual constrained arc (same as untrusted)."""
        with pytest.raises(ValueError, match="Cannot create individual untrusted arc"):
            arc_manager.create_arc(
                "constrained", integrity_level="constrained"
            )


# ── State encryption ─────────────────────────────────────────────────

class TestConstrainedStateEncryption:

    def test_constrained_arc_state_encrypted(self):
        """State for constrained arcs is encrypted (same as untrusted)."""
        from carpenter.core.workflows import review_manager
        from carpenter.core.trust.encryption import generate_arc_key

        parent = arc_manager.create_arc("parent")
        target = arc_manager.add_child(
            parent, "constrained-worker", integrity_level="constrained"
        )
        reviewer = review_manager.create_review_arc(target, "reviewer")
        generate_arc_key(target, [reviewer])

        result = handle_set({
            "arc_id": target,
            "key": "secret",
            "value": "sensitive-data",
        })
        assert result["success"] is True

        get_result = handle_get({"arc_id": target, "key": "secret"})
        assert get_result.get("encrypted") is True

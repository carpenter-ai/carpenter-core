"""Tests for parent-child cross-arc state reads."""

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.api.callbacks import _is_descendant_of
from carpenter.tool_backends.state import handle_get, handle_set


class TestParentChildStateReads:

    def test_parent_reads_child_state(self, create_arc):
        """Parent arc reads from child via state.get with target arc_id."""
        parent = create_arc("parent")
        child = arc_manager.add_child(parent, "child")

        # Set state on child
        handle_set({"arc_id": child, "key": "result", "value": "success"})

        # Verify descendant relationship
        assert _is_descendant_of(child, parent)

        # Parent reads child state directly via handle_get
        result = handle_get({"arc_id": child, "key": "result"})
        assert result["value"] == "success"

    def test_parent_reads_grandchild_state(self, create_arc):
        """Parent reads from grandchild (descendant)."""
        root = create_arc("root")
        child = arc_manager.add_child(root, "child")
        arc_manager.update_status(child, "active")
        grandchild = arc_manager.add_child(child, "grandchild")

        handle_set({"arc_id": grandchild, "key": "deep_data", "value": 42})

        # Verify descendant chain
        assert _is_descendant_of(grandchild, root)
        assert _is_descendant_of(grandchild, child)

        # Root can read grandchild state
        result = handle_get({"arc_id": grandchild, "key": "deep_data"})
        assert result["value"] == 42

    def test_non_parent_is_not_ancestor(self, create_arc):
        """Sibling arc is not an ancestor of another sibling's child."""
        root = create_arc("root")
        sibling_a = arc_manager.add_child(root, "sibling-a")
        sibling_b = arc_manager.add_child(root, "sibling-b")
        arc_manager.update_status(sibling_a, "active")
        child_of_a = arc_manager.add_child(sibling_a, "child-of-a")

        # sibling_b is NOT an ancestor of child_of_a
        assert not _is_descendant_of(child_of_a, sibling_b)

    def test_cannot_read_non_trusted_child_state_via_descendant_check(self, create_arc):
        """Non-trusted child's integrity_level blocks reads through the callback handler."""
        from carpenter.db import get_db

        root = create_arc("root")
        # Create an untrusted child directly via the unchecked _insert_arc
        # (the public create_arc rejects untrusted creation outside a
        # batch-builder; we want a bare untrusted arc for this guard test).
        child = arc_manager._insert_arc(
            name="untrusted-child",
            parent_id=root,
            integrity_level="untrusted",
        )

        # Verify it IS a descendant
        assert _is_descendant_of(child, root)

        # But confirm the arc is untrusted
        db = get_db()
        try:
            row = db.execute(
                "SELECT integrity_level FROM arcs WHERE id = ?", (child,)
            ).fetchone()
            assert row["integrity_level"] == "untrusted"
        finally:
            db.close()

    def test_backward_compatible_without_target_arc_id(self, create_arc):
        """state.get(key) still reads from current arc (no _target_arc_id)."""
        arc = create_arc("regular-arc")
        handle_set({"arc_id": arc, "key": "data", "value": "hello"})

        result = handle_get({"arc_id": arc, "key": "data"})
        assert result["value"] == "hello"

    def test_cross_arc_state_read_with_grant(self, create_arc):
        """Sibling with a read grant can read state; one without cannot."""
        root = create_arc("root")
        sibling_a = arc_manager.add_child(root, "sibling-a")
        sibling_b = arc_manager.add_child(root, "sibling-b")

        # Set state on sibling_a
        handle_set({"arc_id": sibling_a, "key": "data", "value": "secret"})

        # Without grant, sibling_b cannot see sibling_a (not descendant)
        assert not _is_descendant_of(sibling_a, sibling_b)
        assert not arc_manager.has_read_grant(sibling_b, sibling_a)

        # Grant read access
        arc_manager.grant_read_access(sibling_b, sibling_a, depth="subtree", reason="test")

        # Now sibling_b has a read grant
        assert arc_manager.has_read_grant(sibling_b, sibling_a)

        # And can read the state directly
        result = handle_get({"arc_id": sibling_a, "key": "data"})
        assert result["value"] == "secret"

    def test_nonexistent_target_not_descendant(self, create_arc):
        """Reading from nonexistent arc_id is not a descendant."""
        root = create_arc("root")
        assert not _is_descendant_of(99999, root)

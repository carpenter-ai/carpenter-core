"""Tests for arc read grant system."""

import pytest

from carpenter.core.arcs import manager as arc_manager


def _create_arc(name, parent_id=None, **kwargs):
    return arc_manager.create_arc(name=name, parent_id=parent_id, **kwargs)


class TestGrantReadAccess:

    def test_grant_and_has_grant_direct(self):
        """Direct grant: reader can read target."""
        a = _create_arc("arc-a")
        b = _create_arc("arc-b")

        arc_manager.grant_read_access(a, b, depth="subtree", reason="test")
        assert arc_manager.has_read_grant(a, b)

    def test_has_grant_subtree_covers_descendants(self):
        """Subtree grant on parent covers child and grandchild."""
        reader = _create_arc("reader")
        target_root = _create_arc("target-root")
        target_child = arc_manager.add_child(target_root, "target-child")
        arc_manager.update_status(target_child, "active")
        target_grandchild = arc_manager.add_child(target_child, "target-grandchild")

        arc_manager.grant_read_access(reader, target_root, depth="subtree")

        assert arc_manager.has_read_grant(reader, target_root)
        assert arc_manager.has_read_grant(reader, target_child)
        assert arc_manager.has_read_grant(reader, target_grandchild)

    def test_has_grant_self_does_not_cover_children(self):
        """Self grant only covers exact target, not descendants."""
        reader = _create_arc("reader")
        target = _create_arc("target")
        child = arc_manager.add_child(target, "child-of-target")

        arc_manager.grant_read_access(reader, target, depth="self")

        assert arc_manager.has_read_grant(reader, target)
        assert not arc_manager.has_read_grant(reader, child)

    def test_no_grant_returns_false(self):
        """Siblings without grants cannot read each other."""
        root = _create_arc("root")
        a = arc_manager.add_child(root, "sibling-a")
        b = arc_manager.add_child(root, "sibling-b")

        assert not arc_manager.has_read_grant(a, b)
        assert not arc_manager.has_read_grant(b, a)

    def test_list_read_grants(self):
        """list_read_grants returns correct grants for a reader."""
        reader = _create_arc("reader")
        t1 = _create_arc("target-1")
        t2 = _create_arc("target-2")

        arc_manager.grant_read_access(reader, t1, depth="subtree", reason="r1")
        arc_manager.grant_read_access(reader, t2, depth="self", reason="r2")

        grants = arc_manager.list_read_grants(reader)
        assert len(grants) == 2
        target_ids = {g["target_arc_id"] for g in grants}
        assert target_ids == {t1, t2}

    def test_invalid_depth_raises(self):
        """ValueError for bad depth."""
        a = _create_arc("a")
        b = _create_arc("b")

        with pytest.raises(ValueError, match="Invalid depth"):
            arc_manager.grant_read_access(a, b, depth="all")

    def test_nonexistent_arc_raises(self):
        """ValueError for missing arcs."""
        a = _create_arc("exists")

        with pytest.raises(ValueError, match="not found"):
            arc_manager.grant_read_access(a, 99999)

        with pytest.raises(ValueError, match="not found"):
            arc_manager.grant_read_access(99999, a)

    def test_grant_or_replace(self):
        """Re-granting same pair updates depth/reason."""
        a = _create_arc("a")
        b = _create_arc("b")

        arc_manager.grant_read_access(a, b, depth="self", reason="first")
        grants = arc_manager.list_read_grants(a)
        assert len(grants) == 1
        assert grants[0]["depth"] == "self"

        arc_manager.grant_read_access(a, b, depth="subtree", reason="updated")
        grants = arc_manager.list_read_grants(a)
        assert len(grants) == 1
        assert grants[0]["depth"] == "subtree"
        assert grants[0]["reason"] == "updated"

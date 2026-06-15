"""Tests for the three-way package-upgrade reconciliation core.

Covers every branch of the ``classify`` matrix with synthetic
``path -> content`` mappings, the ``conflicts()`` / ``auto_apply()``
partition, and the ``unified_diff`` / ``is_binary`` helpers.
"""

from __future__ import annotations

import pytest

from carpenter.packages.reconcile import (
    FileDelta,
    FileStatus,
    ReconcilePlan,
    classify,
    content_hash,
    is_binary,
    unified_diff,
)


def _status(plan: ReconcilePlan, path: str) -> FileStatus:
    for d in plan.deltas:
        if d.path == path:
            return d.status
    raise KeyError(path)


def _delta(plan: ReconcilePlan, path: str) -> FileDelta:
    for d in plan.deltas:
        if d.path == path:
            return d
    raise KeyError(path)


class TestPresentInAllThree:
    def test_unchanged(self):
        plan = classify({"a": "x"}, {"a": "x"}, {"a": "x"})
        assert _status(plan, "a") is FileStatus.UNCHANGED

    def test_upstream_only(self):
        # old == cur, new differs -> adopt new
        plan = classify({"a": "x"}, {"a": "y"}, {"a": "x"})
        assert _status(plan, "a") is FileStatus.UPSTREAM_ONLY

    def test_user_only(self):
        # old == new, cur differs -> keep user's
        plan = classify({"a": "x"}, {"a": "x"}, {"a": "z"})
        assert _status(plan, "a") is FileStatus.USER_ONLY

    def test_converged(self):
        # new == cur, both differ from old -> no conflict
        plan = classify({"a": "x"}, {"a": "y"}, {"a": "y"})
        assert _status(plan, "a") is FileStatus.CONVERGED

    def test_three_way_conflict(self):
        plan = classify({"a": "x"}, {"a": "y"}, {"a": "z"})
        assert _status(plan, "a") is FileStatus.CONFLICT


class TestAddsAndRemoves:
    def test_added_upstream(self):
        plan = classify({}, {"a": "x"}, {})
        assert _status(plan, "a") is FileStatus.ADDED_UPSTREAM

    def test_added_user(self):
        plan = classify({}, {}, {"a": "x"})
        assert _status(plan, "a") is FileStatus.ADDED_USER

    def test_converged_add(self):
        # new + cur, not old, identical content
        plan = classify({}, {"a": "x"}, {"a": "x"})
        assert _status(plan, "a") is FileStatus.CONVERGED

    def test_conflicting_add(self):
        # new + cur, not old, different content
        plan = classify({}, {"a": "x"}, {"a": "y"})
        assert _status(plan, "a") is FileStatus.CONFLICT

    def test_removed_upstream(self):
        # old + cur, not new, user untouched -> remove
        plan = classify({"a": "x"}, {}, {"a": "x"})
        assert _status(plan, "a") is FileStatus.REMOVED_UPSTREAM

    def test_removed_upstream_conflict(self):
        # old + cur, not new, user modified -> conflict
        plan = classify({"a": "x"}, {}, {"a": "z"})
        assert _status(plan, "a") is FileStatus.REMOVED_UPSTREAM_CONFLICT


class TestUserDeletion:
    def test_user_deleted_unchanged_upstream(self):
        # old + new (==), not cur: honour the user's deletion as USER_ONLY
        plan = classify({"a": "x"}, {"a": "x"}, {})
        assert _status(plan, "a") is FileStatus.USER_ONLY

    def test_user_deleted_vs_upstream_changed(self):
        # old + new (!=), not cur: competing intents -> conflict
        plan = classify({"a": "x"}, {"a": "y"}, {})
        assert _status(plan, "a") is FileStatus.CONFLICT

    def test_removed_by_both(self):
        # in old only, gone from new and cur -> converged on absence
        plan = classify({"a": "x"}, {}, {})
        assert _status(plan, "a") is FileStatus.REMOVED_UPSTREAM


class TestPlanPartition:
    def test_conflicts_and_auto_apply_partition(self):
        old = {"keep": "1", "up": "1", "user": "1", "conf": "1", "rm": "1", "rmc": "1"}
        new = {"keep": "1", "up": "2", "user": "1", "conf": "2", "addu": "n"}
        current = {
            "keep": "1",
            "up": "1",
            "user": "9",
            "conf": "3",
            "rm": "1",
            "rmc": "9",
            "addc": "c",
        }
        plan = classify(old, new, current)

        conflicts = plan.conflicts()
        auto = plan.auto_apply()

        # No overlap, full cover.
        conflict_paths = {d.path for d in conflicts}
        auto_paths = {d.path for d in auto}
        assert conflict_paths.isdisjoint(auto_paths)
        assert conflict_paths | auto_paths == {d.path for d in plan.deltas}

        # Expected conflicts: three-way conflict + removed-upstream-conflict.
        assert conflict_paths == {"conf", "rmc"}
        assert plan.has_conflicts is True
        for d in conflicts:
            assert d.is_conflict
        for d in auto:
            assert not d.is_conflict

    def test_no_conflicts_flag(self):
        plan = classify({"a": "x"}, {"a": "x"}, {"a": "x"})
        assert plan.has_conflicts is False
        assert plan.conflicts() == ()
        assert len(plan.auto_apply()) == 1


class TestDeterminismAndHashes:
    def test_deltas_sorted_by_path(self):
        plan = classify({"c": "1"}, {"a": "1"}, {"b": "1"})
        assert [d.path for d in plan.deltas] == ["a", "b", "c"]

    def test_hashes_recorded(self):
        plan = classify({"a": "old"}, {"a": "new"}, {"a": "old"})
        d = _delta(plan, "a")
        assert d.old_hash == content_hash("old")
        assert d.new_hash == content_hash("new")
        assert d.current_hash == content_hash("old")

    def test_absent_hash_is_none(self):
        plan = classify({}, {"a": "x"}, {})
        d = _delta(plan, "a")
        assert d.old_hash is None
        assert d.current_hash is None
        assert d.new_hash == content_hash("x")

    def test_str_and_bytes_equal(self):
        # Equivalent str and bytes content classified as identical.
        plan = classify({"a": "x"}, {"a": b"x"}, {"a": "x"})
        assert _status(plan, "a") is FileStatus.UNCHANGED

    def test_classify_is_pure(self):
        old, new, cur = {"a": "1"}, {"a": "2"}, {"a": "1"}
        p1 = classify(old, new, cur)
        p2 = classify(old, new, cur)
        assert p1 == p2
        # Inputs untouched.
        assert (old, new, cur) == ({"a": "1"}, {"a": "2"}, {"a": "1"})


class TestDiffHelper:
    def test_text_diff(self):
        out = unified_diff("f.txt", "line1\nline2\n", "line1\nCHANGED\n")
        assert "old/f.txt" in out
        assert "new/f.txt" in out
        assert "-line2" in out
        assert "+CHANGED" in out

    def test_text_diff_custom_labels(self):
        out = unified_diff("f", "a\n", "b\n", a_label="L", b_label="R")
        assert "L/f" in out
        assert "R/f" in out

    def test_identical_text_no_diff(self):
        assert unified_diff("f", "same\n", "same\n") == ""

    def test_binary_differ(self):
        out = unified_diff("img.png", b"\x00\x01", b"\x00\x02")
        assert out == "Binary files img.png differ\n"

    def test_binary_match(self):
        out = unified_diff("img.png", b"\x00\x01", b"\x00\x01")
        assert out == "Binary files img.png match\n"


class TestIsBinary:
    def test_str_is_text(self):
        assert is_binary("hello") is False

    def test_utf8_bytes_is_text(self):
        assert is_binary("héllo".encode("utf-8")) is False

    def test_nul_byte_is_binary(self):
        assert is_binary(b"a\x00b") is True

    def test_invalid_utf8_is_binary(self):
        assert is_binary(b"\xff\xfe\xfd") is True


class TestStatusEnum:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (FileStatus.CONFLICT, True),
            (FileStatus.REMOVED_UPSTREAM_CONFLICT, True),
            (FileStatus.UNCHANGED, False),
            (FileStatus.UPSTREAM_ONLY, False),
            (FileStatus.USER_ONLY, False),
            (FileStatus.CONVERGED, False),
            (FileStatus.ADDED_UPSTREAM, False),
            (FileStatus.ADDED_USER, False),
            (FileStatus.REMOVED_UPSTREAM, False),
        ],
    )
    def test_is_conflict(self, status, expected):
        assert status.is_conflict is expected

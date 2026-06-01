"""Tests for platform-level subscription filter operators.

``filter_matches`` is shared by event-bus matchers and subscription
filters. ``$ne`` semantics are covered in
``test_reflection_per_arc_trigger.py``. This module covers the two
operators added in Phase E4 to let the skill-kb-review template
subscribe to a generic ``kb.entry_written`` event and filter on its
payload without platform-side coupling to the ``skills/`` prefix.
"""

from __future__ import annotations

from carpenter.core.engine._utils import filter_matches


class TestStartsWith:
    def test_matches_prefix(self):
        assert filter_matches({"path": {"$starts_with": "skills/"}},
                              {"path": "skills/fibonacci"})

    def test_rejects_non_prefix(self):
        assert not filter_matches({"path": {"$starts_with": "skills/"}},
                                  {"path": "prompts/foo"})

    def test_rejects_missing_key(self):
        assert not filter_matches({"path": {"$starts_with": "skills/"}}, {})

    def test_rejects_non_string_value(self):
        assert not filter_matches({"path": {"$starts_with": "skills/"}},
                                  {"path": 42})

    def test_matches_full_prefix(self):
        # Exact-equal-to-prefix should still match — starts_with includes
        # the empty-suffix case.
        assert filter_matches({"path": {"$starts_with": "skills/"}},
                              {"path": "skills/"})

    def test_combined_with_equality(self):
        flt = {"event_type": "write", "path": {"$starts_with": "skills/"}}
        assert filter_matches(flt, {"event_type": "write", "path": "skills/x"})
        assert not filter_matches(flt, {"event_type": "read", "path": "skills/x"})
        assert not filter_matches(flt, {"event_type": "write", "path": "x"})


class TestIsNull:
    def test_true_matches_absent_key(self):
        assert filter_matches({"auto_source": {"$is_null": True}}, {})

    def test_true_matches_none_value(self):
        assert filter_matches({"auto_source": {"$is_null": True}},
                              {"auto_source": None})

    def test_true_rejects_present_value(self):
        assert not filter_matches({"auto_source": {"$is_null": True}},
                                  {"auto_source": "sync"})

    def test_false_matches_present_value(self):
        assert filter_matches({"auto_source": {"$is_null": False}},
                              {"auto_source": "sync"})

    def test_false_rejects_absent_key(self):
        assert not filter_matches({"auto_source": {"$is_null": False}}, {})

    def test_false_rejects_explicit_none(self):
        assert not filter_matches({"auto_source": {"$is_null": False}},
                                  {"auto_source": None})


class TestOperatorFallthrough:
    def test_unknown_operator_falls_back_to_equality(self):
        # Documented behaviour: unknown operators are treated as a
        # literal equality check so that stored filters from an older
        # codebase do not break subscriptions silently.
        flt = {"x": {"$madeup": "y"}}
        assert not filter_matches(flt, {"x": "anything"})
        assert filter_matches(flt, {"x": {"$madeup": "y"}})

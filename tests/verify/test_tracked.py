"""Tests for tracked value wrappers (verify/tracked.py)."""

import pytest

from carpenter.verify.tracked import (
    Tracked, TrackedStr, TrackedList, TrackedDict, wrap_value,
    _T, _C, _U,
)
from carpenter.security.exceptions import ConstrainedControlFlowError
from carpenter_tools.policy.types import EmailPolicy, Domain, IntRange, Enum as PolicyEnum


class TestTrackedBool:
    """__bool__ gate: T passes, C/U raises."""

    def test_trusted_bool_true(self):
        assert bool(Tracked(True, _T)) is True

    def test_trusted_bool_false(self):
        assert bool(Tracked(False, _T)) is False

    def test_constrained_bool_raises(self):
        with pytest.raises(ConstrainedControlFlowError):
            bool(Tracked(True, _C))

    def test_untrusted_bool_raises(self):
        with pytest.raises(ConstrainedControlFlowError):
            bool(Tracked(True, _U))

    def test_if_trusted_works(self):
        x = Tracked(42, _T)
        result = "yes" if x else "no"
        assert result == "yes"

    def test_if_constrained_raises(self):
        x = Tracked(42, _C)
        with pytest.raises(ConstrainedControlFlowError):
            "yes" if x else "no"


class TestTrackedComparison:
    """Comparison operators with decomposition rule."""

    def test_t_eq_t(self):
        result = Tracked(1, _T) == Tracked(1, _T)
        assert isinstance(result, Tracked)
        assert result.value is True
        assert result.label == _T

    def test_c_eq_c(self):
        result = Tracked(1, _C) == Tracked(1, _C)
        assert result.label == _C

    def test_c_eq_t_policy_decomposition(self):
        """C == T(PolicyLiteral) -> T (decomposition rule)."""
        email = EmailPolicy("test@example.com")
        result = Tracked("test@example.com", _C) == email
        assert isinstance(result, Tracked)
        assert result.value is True
        assert result.label == _T

    def test_c_eq_t_policy_no_match(self):
        """C == T(PolicyLiteral) with different value still gets T label."""
        email = EmailPolicy("other@example.com")
        result = Tracked("test@example.com", _C) == email
        assert result.value is False
        assert result.label == _T

    def test_c_ne_policy(self):
        email = EmailPolicy("test@example.com")
        result = Tracked("test@example.com", _C) != email
        assert result.value is False
        assert result.label == _T

    def test_c_lt_intrange(self):
        r = IntRange(0, 100)
        result = Tracked(50, _C) == r
        assert result.label == _T

    def test_ne(self):
        result = Tracked(1, _T) != Tracked(2, _T)
        assert result.value is True

    def test_lt(self):
        result = Tracked(1, _T) < Tracked(2, _T)
        assert result.value is True
        assert result.label == _T

    def test_gt(self):
        result = Tracked(2, _T) > Tracked(1, _T)
        assert result.value is True

    def test_le(self):
        result = Tracked(1, _T) <= Tracked(1, _T)
        assert result.value is True

    def test_ge(self):
        result = Tracked(2, _T) >= Tracked(2, _T)
        assert result.value is True

    def test_c_eq_bare_value(self):
        """C compared against non-policy T value: join(C, T) = C."""
        result = Tracked(1, _C) == Tracked(1, _T)
        assert result.label == _C  # No decomposition without PolicyLiteral


class TestTrackedArithmetic:
    def test_add_t_t(self):
        result = Tracked(1, _T) + Tracked(2, _T)
        assert result.value == 3
        assert result.label == _T

    def test_add_t_c(self):
        result = Tracked(1, _T) + Tracked(2, _C)
        assert result.value == 3
        assert result.label == _C

    def test_sub(self):
        result = Tracked(5, _T) - Tracked(3, _C)
        assert result.value == 2
        assert result.label == _C

    def test_mul(self):
        result = Tracked(3, _T) * Tracked(4, _T)
        assert result.value == 12
        assert result.label == _T

    def test_radd(self):
        result = 1 + Tracked(2, _C)
        assert result.value == 3
        assert result.label == _C


class TestTrackedIdentity:
    def test_hash(self):
        assert hash(Tracked(42, _T)) == hash(42)

    def test_str(self):
        assert str(Tracked("hello", _T)) == "hello"

    def test_repr(self):
        r = repr(Tracked(42, _T))
        assert "42" in r
        assert "trusted" in r

    def test_format(self):
        t = Tracked(3.14, _T)
        assert f"{t:.1f}" == "3.1"

    def test_int(self):
        assert int(Tracked(42, _T)) == 42

    def test_float(self):
        assert float(Tracked(3, _T)) == 3.0


class TestTrackedStr:
    def test_contains_t_t(self):
        s = TrackedStr("hello world", _T)
        assert ("hello" in s) is True

    def test_contains_c_raises(self):
        s = TrackedStr("hello", _C)
        with pytest.raises(ConstrainedControlFlowError):
            "world" in s

    def test_lower(self):
        s = TrackedStr("HELLO", _C)
        result = s.lower()
        assert result.value == "hello"
        assert result.label == _C

    def test_upper(self):
        s = TrackedStr("hello", _T)
        assert s.upper().value == "HELLO"

    def test_strip(self):
        s = TrackedStr("  hi  ", _C)
        assert s.strip().value == "hi"
        assert s.strip().label == _C

    def test_startswith_policy(self):
        s = TrackedStr("https://example.com/path", _C)
        from carpenter_tools.policy.types import Url
        prefix = Url("https://example.com")
        result = s.startswith(prefix)
        assert result.value is True
        assert result.label == _T  # decomposition

    def test_endswith(self):
        s = TrackedStr("test@example.com", _C)
        from carpenter_tools.policy.types import Domain
        suffix = Domain("example.com")
        result = s.endswith(suffix)
        assert result.label == _T  # decomposition

    def test_add(self):
        s = TrackedStr("hello", _T) + TrackedStr(" world", _C)
        assert s.value == "hello world"
        assert s.label == _C


class TestTrackedList:
    def test_contains_all_t_items_c_query(self):
        """If all list items are T and query is C -> T (membership check)."""
        items = [Tracked("a", _T), Tracked("b", _T), Tracked("c", _T)]
        lst = TrackedList(items, _T)
        # Result is T because all items are T and C is being checked
        assert (Tracked("b", _C) in lst) is True

    def test_contains_not_found(self):
        items = [Tracked("a", _T), Tracked("b", _T)]
        lst = TrackedList(items, _T)
        assert (Tracked("z", _C) in lst) is False

    def test_contains_c_items_raises(self):
        """C items in list with C query -> C label -> raises."""
        items = [Tracked("a", _C), Tracked("b", _C)]
        lst = TrackedList(items, _C)
        with pytest.raises(ConstrainedControlFlowError):
            Tracked("a", _C) in lst

    def test_iter(self):
        items = [Tracked(1, _T), Tracked(2, _T)]
        lst = TrackedList(items, _T)
        collected = list(lst)
        assert len(collected) == 2

    def test_len(self):
        items = [Tracked(1, _T), Tracked(2, _T), Tracked(3, _T)]
        lst = TrackedList(items, _T)
        assert len(lst) == 3

    def test_getitem(self):
        items = [Tracked(10, _T), Tracked(20, _C)]
        lst = TrackedList(items, _T)
        assert lst[0].value == 10

    def test_bool_trusted(self):
        lst = TrackedList([Tracked(1, _T)], _T)
        assert bool(lst) is True

    def test_bool_constrained_raises(self):
        lst = TrackedList([Tracked(1, _C)], _C)
        with pytest.raises(ConstrainedControlFlowError):
            bool(lst)


class TestTrackedDict:
    def test_getitem(self):
        d = TrackedDict({"key": Tracked(42, _T)}, _T)
        result = d["key"]
        assert isinstance(result, Tracked)
        assert result.value == 42

    def test_contains_c_raises(self):
        """C key in T dict -> C label -> raises."""
        d = TrackedDict({"a": 1, "b": 2}, _T)
        with pytest.raises(ConstrainedControlFlowError):
            Tracked("a", _C) in d

    def test_contains_t_t(self):
        d = TrackedDict({"a": 1, "b": 2}, _T)
        assert ("a" in d) is True

    def test_get_found(self):
        d = TrackedDict({"x": Tracked(99, _T)}, _T)
        result = d.get("x")
        assert result.value == 99

    def test_get_default(self):
        d = TrackedDict({}, _T)
        result = d.get("missing", "default")
        assert result == "default"

    def test_bool_trusted(self):
        d = TrackedDict({"a": 1}, _T)
        assert bool(d) is True

    def test_bool_constrained_raises(self):
        d = TrackedDict({"a": 1}, _C)
        with pytest.raises(ConstrainedControlFlowError):
            bool(d)


class TestWrapValue:
    def test_wrap_str(self):
        result = wrap_value("hello", _C)
        assert isinstance(result, TrackedStr)
        assert result.label == _C

    def test_wrap_int(self):
        result = wrap_value(42, _T)
        assert isinstance(result, Tracked)
        assert result.value == 42

    def test_wrap_list(self):
        result = wrap_value([1, 2, 3], _C)
        assert isinstance(result, TrackedList)
        assert len(result) == 3

    def test_wrap_dict(self):
        result = wrap_value({"a": 1}, _T)
        assert isinstance(result, TrackedDict)

    def test_wrap_bool(self):
        result = wrap_value(True, _C)
        assert isinstance(result, Tracked)
        assert result.value is True
        assert result.label == _C


class TestDecompositionEndToEnd:
    """End-to-end: C == Email("x") passes __bool__, bare C doesn't."""

    def test_policy_comparison_allows_branching(self):
        """if Tracked(x, 'C') == Email('y'): — should succeed."""
        sender = Tracked("alice@example.com", _C)
        allowed = EmailPolicy("alice@example.com")
        check = (sender == allowed)
        assert check.label == _T
        assert bool(check) is True  # __bool__ succeeds because label is T

    def test_bare_constrained_blocks_branching(self):
        """if Tracked(x, 'C'): — should raise."""
        sender = Tracked("alice@example.com", _C)
        with pytest.raises(ConstrainedControlFlowError):
            if sender:
                pass

    def test_policy_mismatch_allows_false_branch(self):
        """if C == Email("other"): — result is False but label is T."""
        sender = Tracked("alice@example.com", _C)
        check = (sender == EmailPolicy("bob@example.com"))
        assert check.label == _T
        assert bool(check) is False

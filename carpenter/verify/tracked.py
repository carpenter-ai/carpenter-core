"""Tracked value wrappers for verified flow analysis.

Each wrapper carries a taint label ('T', 'C', 'U') through Python's native
evaluation via dunder protocol. When __bool__() is called on a non-TRUSTED
value, ConstrainedControlFlowError is raised — catching every control-flow
use of CONSTRAINED data.

The decomposition rule: C == T(typed constructor) -> T. This allows safe
branching on CONSTRAINED data when compared against policy-typed or
declaration-typed references.
"""

from __future__ import annotations

from typing import Any

from carpenter.core.trust.integrity import join as integrity_join, IntegrityLevel
from carpenter.security.exceptions import ConstrainedControlFlowError
from carpenter_tools.policy.types import PolicyLiteral
from carpenter_tools.declarations import SecurityType


# Short label aliases
_T = IntegrityLevel.TRUSTED.value      # "trusted"
_C = IntegrityLevel.CONSTRAINED.value   # "constrained"
_U = IntegrityLevel.UNTRUSTED.value     # "untrusted"


def _label_of(obj: Any) -> str:
    """Extract the label from a value. Non-tracked values are TRUSTED."""
    if isinstance(obj, (Tracked, TrackedList, TrackedDict, TrackedStr)):
        return obj.label
    return _T


def _value_of(obj: Any) -> Any:
    """Extract the raw value, unwrapping Tracked if needed."""
    if isinstance(obj, Tracked):
        return obj.value
    if isinstance(obj, PolicyLiteral):
        return obj.value
    return obj


def _join_labels(a: str, b: str) -> str:
    """Join two labels returning the string value."""
    return integrity_join(a, b).value


def _is_typed_reference(obj: Any) -> bool:
    """True if obj is a PolicyLiteral or SecurityType instance."""
    raw = _value_of(obj)
    return (
        isinstance(raw, PolicyLiteral)
        or isinstance(obj, PolicyLiteral)
        or isinstance(raw, SecurityType)
        or isinstance(obj, SecurityType)
    )


def _is_policy_check(self_label: str, other: Any) -> bool:
    """True if this is a C vs T(typed reference) comparison."""
    other_label = _label_of(other)
    return (
        (self_label == _C and other_label == _T and _is_typed_reference(other))
        or (self_label == _T and _label_of(other) == _C)
    )


class Tracked:
    """Value wrapper carrying a taint label through operations.

    The label propagates via integrity_join on binary operations.
    __bool__() raises ConstrainedControlFlowError if label != T.
    """

    __slots__ = ("_value", "_label")

    def __init__(self, value: Any, label: str):
        self._value = value
        self._label = label

    @property
    def value(self) -> Any:
        return self._value

    @property
    def label(self) -> str:
        return self._label

    # ---- Control flow gate ----

    def __bool__(self) -> bool:
        if self._label != _T:
            raise ConstrainedControlFlowError(self._label, "bool")
        return bool(self._value)

    # ---- Comparison operators ----

    def _compare(self, other: Any, op: str) -> Tracked:
        other_label = _label_of(other)
        other_val = _value_of(other)
        if isinstance(other, Tracked):
            other_val = other.value

        ops = {
            "eq": lambda a, b: a == b,
            "ne": lambda a, b: a != b,
            "lt": lambda a, b: a < b,
            "gt": lambda a, b: a > b,
            "le": lambda a, b: a <= b,
            "ge": lambda a, b: a >= b,
        }
        result = ops[op](self._value, other_val)

        # Decomposition rule: C checked against T(typed reference) -> T
        if self._label == _C and other_label == _T and _is_typed_reference(other):
            return Tracked(result, _T)
        is_self_typed = isinstance(self._value, (PolicyLiteral, SecurityType))
        if self._label == _T and other_label == _C and is_self_typed:
            return Tracked(result, _T)

        return Tracked(result, _join_labels(self._label, other_label))

    def __eq__(self, other: Any) -> Tracked:  # type: ignore[override]
        return self._compare(other, "eq")

    def __ne__(self, other: Any) -> Tracked:  # type: ignore[override]
        return self._compare(other, "ne")

    def __lt__(self, other: Any) -> Tracked:
        return self._compare(other, "lt")

    def __gt__(self, other: Any) -> Tracked:
        return self._compare(other, "gt")

    def __le__(self, other: Any) -> Tracked:
        return self._compare(other, "le")

    def __ge__(self, other: Any) -> Tracked:
        return self._compare(other, "ge")

    # ---- Arithmetic ----

    def __add__(self, other: Any) -> Tracked:
        return Tracked(self._value + _value_of(other), _join_labels(self._label, _label_of(other)))

    def __radd__(self, other: Any) -> Tracked:
        return Tracked(_value_of(other) + self._value, _join_labels(_label_of(other), self._label))

    def __sub__(self, other: Any) -> Tracked:
        return Tracked(self._value - _value_of(other), _join_labels(self._label, _label_of(other)))

    def __rsub__(self, other: Any) -> Tracked:
        return Tracked(_value_of(other) - self._value, _join_labels(_label_of(other), self._label))

    def __mul__(self, other: Any) -> Tracked:
        return Tracked(self._value * _value_of(other), _join_labels(self._label, _label_of(other)))

    def __rmul__(self, other: Any) -> Tracked:
        return Tracked(_value_of(other) * self._value, _join_labels(_label_of(other), self._label))

    # ---- Identity ----

    def __hash__(self) -> int:
        return hash(self._value)

    def __str__(self) -> str:
        return str(self._value)

    def __repr__(self) -> str:
        return f"Tracked({self._value!r}, {self._label!r})"

    def __format__(self, format_spec: str) -> str:
        return format(self._value, format_spec)

    def __int__(self) -> int:
        return int(self._value)

    def __float__(self) -> float:
        return float(self._value)


class TrackedStr(Tracked):
    """String wrapper that preserves labels through string operations."""

    __slots__ = ()

    def __init__(self, value: str, label: str):
        super().__init__(value, label)

    def __contains__(self, item: Any) -> bool:
        """Python calls bool() on __contains__ result — raise if label is not T."""
        item_val = _value_of(item)
        item_label = _label_of(item)
        result_val = item_val in self._value
        result_label = _join_labels(self._label, item_label)
        if result_label != _T:
            raise ConstrainedControlFlowError(result_label, "contains")
        return result_val

    def lower(self) -> TrackedStr:
        return TrackedStr(self._value.lower(), self._label)

    def upper(self) -> TrackedStr:
        return TrackedStr(self._value.upper(), self._label)

    def strip(self, chars: str | None = None) -> TrackedStr:
        return TrackedStr(self._value.strip(chars), self._label)

    def startswith(self, prefix: Any) -> Tracked:
        prefix_val = _value_of(prefix)
        prefix_label = _label_of(prefix)
        result = self._value.startswith(prefix_val)
        # Decomposition: C.startswith(T(typed reference)) -> T
        if self._label == _C and prefix_label == _T and _is_typed_reference(prefix):
            return Tracked(result, _T)
        return Tracked(result, _join_labels(self._label, prefix_label))

    def endswith(self, suffix: Any) -> Tracked:
        suffix_val = _value_of(suffix)
        suffix_label = _label_of(suffix)
        result = self._value.endswith(suffix_val)
        if self._label == _C and suffix_label == _T and _is_typed_reference(suffix):
            return Tracked(result, _T)
        return Tracked(result, _join_labels(self._label, suffix_label))

    def __add__(self, other: Any) -> TrackedStr:
        return TrackedStr(self._value + str(_value_of(other)), _join_labels(self._label, _label_of(other)))

    def __radd__(self, other: Any) -> TrackedStr:
        return TrackedStr(str(_value_of(other)) + self._value, _join_labels(_label_of(other), self._label))


class TrackedList:
    """List wrapper that yields tracked elements and tracks membership."""

    __slots__ = ("_items", "_label")

    def __init__(self, items: list, label: str):
        self._items = items  # list of Tracked values
        self._label = label

    @property
    def label(self) -> str:
        return self._label

    @property
    def items(self) -> list:
        return self._items

    def __contains__(self, item: Any) -> bool:
        """Python calls bool() on __contains__ result — raise if label is not T."""
        item_val = _value_of(item)
        item_label = _label_of(item)
        result_val = any(
            _value_of(i) == item_val for i in self._items
        )
        # If all list items are T: membership check = policy check -> T
        if all(_label_of(i) == _T for i in self._items) and item_label == _C:
            return result_val  # label is T, safe to return bool
        result_label = _join_labels(self._label, item_label)
        if result_label != _T:
            raise ConstrainedControlFlowError(result_label, "contains")
        return result_val

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, index: Any) -> Any:
        idx = _value_of(index)
        item = self._items[idx]
        idx_label = _label_of(index)
        if idx_label != _T:
            # Subscript with non-T index taints the result
            item_label = _label_of(item)
            return Tracked(_value_of(item), _join_labels(item_label, idx_label))
        return item

    def __bool__(self) -> bool:
        if self._label != _T:
            raise ConstrainedControlFlowError(self._label, "bool")
        return bool(self._items)


class TrackedDict:
    """Dict wrapper that preserves labels through access."""

    __slots__ = ("_data", "_label")

    def __init__(self, data: dict, label: str):
        self._data = data
        self._label = label

    @property
    def label(self) -> str:
        return self._label

    def __getitem__(self, key: Any) -> Any:
        key_val = _value_of(key)
        key_label = _label_of(key)
        item = self._data[key_val]
        item_label = _label_of(item)
        return Tracked(_value_of(item), _join_labels(self._label, _join_labels(item_label, key_label)))

    def __contains__(self, key: Any) -> bool:
        """Python calls bool() on __contains__ result — raise if label is not T."""
        key_val = _value_of(key)
        key_label = _label_of(key)
        result_val = key_val in self._data
        result_label = _join_labels(self._label, key_label)
        if result_label != _T:
            raise ConstrainedControlFlowError(result_label, "contains")
        return result_val

    def get(self, key: Any, default: Any = None) -> Any:
        key_val = _value_of(key)
        key_label = _label_of(key)
        if key_val in self._data:
            item = self._data[key_val]
            item_label = _label_of(item)
            return Tracked(_value_of(item), _join_labels(self._label, _join_labels(item_label, key_label)))
        return default

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def __bool__(self) -> bool:
        if self._label != _T:
            raise ConstrainedControlFlowError(self._label, "bool")
        return bool(self._data)


def wrap_value(value: Any, label: str) -> Any:
    """Factory: wrap a value in the appropriate tracked type."""
    if isinstance(value, str):
        return TrackedStr(value, label)
    if isinstance(value, list):
        wrapped_items = [wrap_value(item, label) for item in value]
        return TrackedList(wrapped_items, label)
    if isinstance(value, dict):
        wrapped_data = {k: wrap_value(v, label) for k, v in value.items()}
        return TrackedDict(wrapped_data, label)
    return Tracked(value, label)

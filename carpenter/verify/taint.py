"""Static taint propagation for verified flow analysis.

Walks the AST and propagates integrity labels through all expressions.
Identifies conditions that depend on CONSTRAINED data and lists all
constrained inputs needed for dry-run enumeration.
"""

from __future__ import annotations

import ast
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from carpenter.core.trust.integrity import join as integrity_join, IntegrityLevel

logger = logging.getLogger(__name__)

_T = IntegrityLevel.TRUSTED.value
_C = IntegrityLevel.CONSTRAINED.value
_U = IntegrityLevel.UNTRUSTED.value

# Known carpenter_tools modules that return trusted output
_TRUSTED_TOOL_MODULES = frozenset({
    "carpenter_tools.act.arc",
    "carpenter_tools.act.messaging",
    "carpenter_tools.act.scheduling",
    "carpenter_tools.act.state",
    "carpenter_tools.read.arc",
    "carpenter_tools.read.state",
    "carpenter_tools.read.files",
    "carpenter_tools.read.messaging",
})

# Policy literal class names
_POLICY_LITERAL_NAMES = frozenset({
    "EmailPolicy", "Domain", "Url", "FilePath", "Command",
    "IntRange", "Enum", "Bool", "Pattern",
})

# Declaration type class names (SecurityType subclasses)
_DECLARATION_TYPE_NAMES = frozenset({
    "Label", "Email", "URL", "WorkspacePath", "SQL", "JSON", "UnstructuredText",
})

# All typed constructor names (union of both sets)
_TYPED_CONSTRUCTOR_NAMES = _POLICY_LITERAL_NAMES | _DECLARATION_TYPE_NAMES

# Map class name -> type string for detected_type recording
_POLICY_CLASS_TO_TYPE: dict[str, str] = {
    # PolicyLiteral types
    "EmailPolicy": "email",
    "Domain": "domain",
    "Url": "url",
    "FilePath": "filepath",
    "Command": "command",
    "IntRange": "int_range",
    "Enum": "enum",
    "Bool": "bool",
    "Pattern": "pattern",
    # Declaration types
    "Label": "label",
    "Email": "email",
    "URL": "url_decl",
    "WorkspacePath": "workspace_path",
    "SQL": "sql",
    "JSON": "json_decl",
    "UnstructuredText": "unstructured_text",
}


@dataclass
class InputSpec:
    """A constrained input that needs enumeration during dry-run."""

    key: str
    arc_id: int | None
    integrity_level: str
    detected_type: str | None = None  # policy type from comparison context
    is_iterated: bool = False  # True if used as for-loop iterable
    has_accumulator: bool = False  # True if loop body accumulates across iterations


@dataclass
class ConditionInfo:
    """An if/elif condition that depends on constrained data."""

    lineno: int
    label: str
    description: str


@dataclass
class TaintAnalysis:
    """Result of static taint analysis."""

    all_trusted: bool  # True if no C data in any condition
    conditions_with_c: list[ConditionInfo] = field(default_factory=list)
    constrained_inputs: list[InputSpec] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


def analyze_taint(
    tree: ast.Module,
    arc_id: int | None = None,
    _get_arc: Any = None,
) -> TaintAnalysis:
    """Analyze taint flow through code AST.

    Args:
        tree: Parsed AST module.
        arc_id: The arc submitting this code (for context).
        _get_arc: Optional override for arc lookup (for testing).

    Returns:
        TaintAnalysis with condition labels and constrained input list.
    """
    visitor = _TaintVisitor(arc_id=arc_id, get_arc_fn=_get_arc)
    visitor.visit(tree)

    all_trusted = len(visitor.conditions_with_c) == 0 and len(visitor.violations) == 0

    return TaintAnalysis(
        all_trusted=all_trusted,
        conditions_with_c=visitor.conditions_with_c,
        constrained_inputs=visitor.constrained_inputs,
        violations=visitor.violations,
    )


class _TaintVisitor(ast.NodeVisitor):
    """AST visitor that propagates taint labels through expressions."""

    def __init__(self, arc_id: int | None = None, get_arc_fn: Any = None):
        self._label_env: dict[str, str] = {}  # variable name -> label
        self._import_map: dict[str, str] = {}  # alias -> module path
        self._arc_id = arc_id
        self._get_arc_fn = get_arc_fn
        self.conditions_with_c: list[ConditionInfo] = []
        self.constrained_inputs: list[InputSpec] = []
        self.violations: list[str] = []
        self._seen_inputs: set[tuple[str, int | None]] = set()
        self._var_to_input: dict[str, InputSpec] = {}  # variable -> InputSpec
        self._last_created_input: InputSpec | None = None

    def _get_arc(self, target_arc_id: int) -> dict | None:
        """Look up an arc by ID."""
        if self._get_arc_fn is not None:
            return self._get_arc_fn(target_arc_id)
        try:
            from carpenter.core.arcs.manager import get_arc
            return get_arc(target_arc_id)
        except (ImportError, sqlite3.Error, KeyError, ValueError) as _exc:
            return None

    def _join(self, a: str, b: str) -> str:
        return integrity_join(a, b).value

    def _expr_label(self, node: ast.expr) -> str:
        """Compute the taint label of an expression node."""
        if isinstance(node, ast.Constant):
            return _T

        if isinstance(node, ast.Name):
            return self._label_env.get(node.id, _T)

        if isinstance(node, ast.BinOp):
            left = self._expr_label(node.left)
            right = self._expr_label(node.right)
            return self._join(left, right)

        if isinstance(node, ast.UnaryOp):
            return self._expr_label(node.operand)

        if isinstance(node, ast.BoolOp):
            labels = [self._expr_label(v) for v in node.values]
            result = labels[0]
            for lbl in labels[1:]:
                result = self._join(result, lbl)
            return result

        if isinstance(node, ast.Compare):
            return self._compare_label(node)

        if isinstance(node, ast.Call):
            return self._call_label(node)

        if isinstance(node, ast.Attribute):
            return self._expr_label(node.value)

        if isinstance(node, ast.Subscript):
            val_label = self._expr_label(node.value)
            slice_label = self._expr_label(node.slice)
            return self._join(val_label, slice_label)

        if isinstance(node, ast.JoinedStr):
            labels = []
            for v in node.values:
                if isinstance(v, ast.FormattedValue):
                    labels.append(self._expr_label(v.value))
                elif isinstance(v, ast.Constant):
                    labels.append(_T)
            if not labels:
                return _T
            result = labels[0]
            for lbl in labels[1:]:
                result = self._join(result, lbl)
            return result

        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            if not node.elts:
                return _T
            labels = [self._expr_label(e) for e in node.elts]
            result = labels[0]
            for lbl in labels[1:]:
                result = self._join(result, lbl)
            return result

        if isinstance(node, ast.Dict):
            labels = []
            for k in node.keys:
                if k is not None:
                    labels.append(self._expr_label(k))
            for v in node.values:
                labels.append(self._expr_label(v))
            if not labels:
                return _T
            result = labels[0]
            for lbl in labels[1:]:
                result = self._join(result, lbl)
            return result

        if isinstance(node, ast.IfExp):
            test_label = self._expr_label(node.test)
            body_label = self._expr_label(node.body)
            else_label = self._expr_label(node.orelse)
            return self._join(test_label, self._join(body_label, else_label))

        if isinstance(node, ast.ListComp):
            # Approximate: join of element and iterator labels
            elt_label = self._expr_label(node.elt)
            for gen in node.generators:
                iter_label = self._expr_label(gen.iter)
                elt_label = self._join(elt_label, iter_label)
            return elt_label

        if isinstance(node, ast.Starred):
            return self._expr_label(node.value)

        # Default: trusted
        return _T

    def _compare_label(self, node: ast.Compare) -> str:
        """Compute label for a comparison, applying decomposition rule."""
        left_label = self._expr_label(node.left)
        result_label = left_label

        for comparator in node.comparators:
            comp_label = self._expr_label(comparator)

            # Decomposition: C == T(typed constructor) -> T
            if self._is_typed_constructor_node(comparator):
                if left_label == _C and comp_label == _T:
                    self._record_detected_type(node.left, comparator)
                    result_label = _T
                    continue
            if self._is_typed_constructor_node(node.left):
                if comp_label == _C and left_label == _T:
                    self._record_detected_type(comparator, node.left)
                    result_label = _T
                    continue

            # C == bare_literal (no policy type) -> flag violation
            if (left_label == _C or comp_label == _C):
                if isinstance(comparator, ast.Constant) and not self._is_typed_constructor_node(comparator):
                    line = getattr(node, "lineno", "?")
                    self.violations.append(
                        f"Line {line}: comparing CONSTRAINED data against untyped "
                        f"literal {comparator.value!r} — use a policy-typed literal instead"
                    )

            result_label = self._join(result_label, comp_label)

        return result_label

    def _is_typed_constructor_node(self, node: ast.expr) -> bool:
        """Check if node is a typed constructor call (PolicyLiteral or SecurityType)."""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id in _TYPED_CONSTRUCTOR_NAMES
        return False

    def _record_detected_type(self, constrained_node: ast.expr, policy_node: ast.expr) -> None:
        """Record the policy type on the InputSpec for the constrained variable.

        Called when a decomposition rule fires: C == T(typed constructor).
        """
        # Find the variable name of the constrained side
        var_name = None
        if isinstance(constrained_node, ast.Name):
            var_name = constrained_node.id
        if var_name is None:
            return
        input_spec = self._var_to_input.get(var_name)
        if input_spec is None:
            return
        # Extract type from the constructor name
        if isinstance(policy_node, ast.Call) and isinstance(policy_node.func, ast.Name):
            policy_type = _POLICY_CLASS_TO_TYPE.get(policy_node.func.id)
            if policy_type and input_spec.detected_type is None:
                input_spec.detected_type = policy_type

    def _call_label(self, node: ast.Call) -> str:
        """Compute label for a function call."""
        func = node.func

        # Typed constructors (PolicyLiteral + SecurityType): always T
        if isinstance(func, ast.Name) and func.id in _TYPED_CONSTRUCTOR_NAMES:
            return _T

        # state.get(key, arc_id=X) — label from arc X
        if isinstance(func, ast.Attribute) and func.attr == "get":
            if isinstance(func.value, ast.Name) and func.value.id == "state":
                return self._state_get_label(node)

        # Platform tool calls: T
        if isinstance(func, ast.Attribute):
            obj_name = self._resolve_name(func.value)
            if obj_name and any(
                obj_name == mod.split(".")[-1]
                for mod in _TRUSTED_TOOL_MODULES
            ):
                return _T

        # Builtins: propagate arg labels
        if isinstance(func, ast.Name):
            arg_labels = [self._expr_label(a) for a in node.args]
            if not arg_labels:
                return _T
            result = arg_labels[0]
            for lbl in arg_labels[1:]:
                result = self._join(result, lbl)
            return result

        # Default: join of all argument labels
        labels = [self._expr_label(a) for a in node.args]
        for kw in node.keywords:
            labels.append(self._expr_label(kw.value))
        if not labels:
            return _T
        result = labels[0]
        for lbl in labels[1:]:
            result = self._join(result, lbl)
        return result

    def _state_get_label(self, node: ast.Call) -> str:
        """Determine label for state.get(key, arc_id=X) calls."""
        # Extract arc_id keyword
        target_arc_id = None
        key_value = None
        if node.args:
            if isinstance(node.args[0], ast.Constant):
                key_value = node.args[0].value

        for kw in node.keywords:
            if kw.arg == "arc_id" and isinstance(kw.value, ast.Constant):
                target_arc_id = kw.value.value

        if target_arc_id is not None:
            arc = self._get_arc(target_arc_id)
            if arc is not None:
                level = arc.get("integrity_level", _T)
                if level != _T:
                    input_key = (key_value or "<unknown>", target_arc_id)
                    if input_key not in self._seen_inputs:
                        self._seen_inputs.add(input_key)
                        spec = InputSpec(
                            key=key_value or "<unknown>",
                            arc_id=target_arc_id,
                            integrity_level=level,
                        )
                        self.constrained_inputs.append(spec)
                        self._last_created_input = spec
                    else:
                        # Find existing spec for var tracking
                        for existing in self.constrained_inputs:
                            if existing.key == (key_value or "<unknown>") and existing.arc_id == target_arc_id:
                                self._last_created_input = existing
                                break
                return level
            # Arc not found — conservative: treat as C
            return _C

        # No arc_id — reading own state (trusted if own arc is trusted)
        return _T

    def _resolve_name(self, node: ast.expr) -> str | None:
        """Resolve a simple name or attribute chain."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = self._resolve_name(node.value)
            if base:
                return f"{base}.{node.attr}"
        return None

    # ---- Visitors ----

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Record import aliases for later resolution."""
        module = node.module or ""
        for alias in node.names:
            name = alias.asname or alias.name
            self._import_map[name] = f"{module}.{alias.name}" if module else alias.name
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Track label for assigned variables."""
        self._last_created_input = None
        label = self._expr_label(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._label_env[target.id] = label
                if self._last_created_input is not None:
                    self._var_to_input[target.id] = self._last_created_input
            elif isinstance(target, ast.Tuple):
                for elt in target.elts:
                    if isinstance(elt, ast.Name):
                        self._label_env[elt.id] = label
                        if self._last_created_input is not None:
                            self._var_to_input[elt.id] = self._last_created_input
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        """Track label for augmented assignments."""
        val_label = self._expr_label(node.value)
        if isinstance(node.target, ast.Name):
            old_label = self._label_env.get(node.target.id, _T)
            self._label_env[node.target.id] = self._join(old_label, val_label)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Track label for annotated assignments."""
        if node.value is not None:
            label = self._expr_label(node.value)
            if isinstance(node.target, ast.Name):
                self._label_env[node.target.id] = label
        self.generic_visit(node)

    def _references_any(self, node: ast.expr, names: set[str]) -> bool:
        """Check if *node* references any of the given variable *names*."""
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and child.id in names:
                return True
        return False

    def visit_For(self, node: ast.For) -> None:
        """Track label for for-loop target variable.

        If the iterable is a variable mapped to an InputSpec, mark
        is_iterated=True and propagate the InputSpec to the loop target
        so comparisons inside the body detect the type correctly.

        Also detects accumulator patterns (``total += item``) — an
        AugAssign whose target is NOT a loop variable but whose value
        references a loop variable.
        """
        iter_label = self._expr_label(node.iter)

        # Mark iterable InputSpec as iterated
        iterable_spec = None
        if isinstance(node.iter, ast.Name):
            iterable_spec = self._var_to_input.get(node.iter.id)
            if iterable_spec is not None:
                iterable_spec.is_iterated = True

        # Collect loop target names
        loop_target_names: set[str] = set()
        if isinstance(node.target, ast.Name):
            loop_target_names.add(node.target.id)
            self._label_env[node.target.id] = iter_label
            # Propagate InputSpec to loop target for type detection
            if iterable_spec is not None:
                self._var_to_input[node.target.id] = iterable_spec
        elif isinstance(node.target, ast.Tuple):
            for elt in node.target.elts:
                if isinstance(elt, ast.Name):
                    loop_target_names.add(elt.id)
                    self._label_env[elt.id] = iter_label
                    if iterable_spec is not None:
                        self._var_to_input[elt.id] = iterable_spec

        # Visit body
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

        # Detect accumulator patterns after visiting body
        if iterable_spec is not None and loop_target_names:
            for stmt in node.body:
                if (isinstance(stmt, ast.AugAssign)
                        and isinstance(stmt.target, ast.Name)
                        and stmt.target.id not in loop_target_names
                        and self._references_any(stmt.value, loop_target_names)):
                    iterable_spec.has_accumulator = True
                    break

    def visit_If(self, node: ast.If) -> None:
        """Check if condition depends on constrained data."""
        cond_label = self._expr_label(node.test)
        if cond_label != _T:
            self.conditions_with_c.append(ConditionInfo(
                lineno=node.lineno,
                label=cond_label,
                description=ast.dump(node.test),
            ))
        # Visit all branches
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Try(self, node: ast.Try) -> None:
        """Visit all branches of try/except."""
        for stmt in node.body:
            self.visit(stmt)
        for handler in node.handlers:
            if handler.name:
                self._label_env[handler.name] = _T
            for stmt in handler.body:
                self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)
        for stmt in node.finalbody:
            self.visit(stmt)

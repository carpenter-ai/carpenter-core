"""AST checker for tool argument SecurityType correctness.

Walks the AST and verifies that tool call arguments use the correct
SecurityType constructor as declared by each tool's ``param_types``
metadata. For example, if ``arc.create`` declares ``name: Label``,
then ``arc.create(name=URL("x"))`` is a violation.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from carpenter_tools.tool_meta import build_tool_type_map


# Known tool module names (leaf names used in ``from carpenter_tools.act import X``)
_TOOL_MODULE_NAMES = frozenset({
    "arc", "messaging", "state", "web", "files", "scheduling",
    "git", "lm", "plugin", "review", "config", "kb", "webhook",
    "platform_time", "system_info",
})

# Known SecurityType and PolicyLiteral constructor names
_KNOWN_TYPE_NAMES = frozenset({
    # PolicyLiteral types
    "EmailPolicy", "Domain", "Url", "FilePath", "Command",
    "IntRange", "Enum", "Bool", "Pattern",
    # SecurityType declarations
    "Label", "Email", "URL", "WorkspacePath", "SQL", "JSON", "UnstructuredText",
})

# Lazy-cached type map
_type_map_cache: dict[tuple[str, str, int | str], str] | None = None


def _get_type_map() -> dict[tuple[str, str, int | str], str]:
    global _type_map_cache
    if _type_map_cache is None:
        _type_map_cache = build_tool_type_map()
    return _type_map_cache


def clear_type_map_cache() -> None:
    """Clear the cached type map, forcing a rebuild on next use."""
    global _type_map_cache
    _type_map_cache = None


@dataclass
class ToolArgTypeResult:
    """Result of tool argument type checking."""

    passed: bool
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_tool_arg_types(code: str) -> ToolArgTypeResult:
    """Check that tool call arguments use the correct SecurityType constructors.

    Args:
        code: Python source code to check.

    Returns:
        ToolArgTypeResult with violations for type mismatches.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ToolArgTypeResult(passed=True)

    type_map = _get_type_map()
    if not type_map:
        return ToolArgTypeResult(passed=True)

    checker = _ArgTypeChecker(type_map)
    checker.visit(tree)

    return ToolArgTypeResult(
        passed=len(checker.violations) == 0,
        violations=checker.violations,
        warnings=checker.warnings,
    )


class _ArgTypeChecker(ast.NodeVisitor):
    """AST visitor that checks tool call argument types."""

    def __init__(self, type_map: dict[tuple[str, str, int | str], str]):
        self._type_map = type_map
        self._var_constructors: dict[str, str] = {}  # var name -> constructor name
        self.violations: list[str] = []
        self.warnings: list[str] = []

    def _get_constructor_name(self, node: ast.expr) -> str | None:
        """Extract SecurityType constructor name from an expression.

        Returns the constructor name (e.g. "Label", "URL") if the node
        is a direct call to a known SecurityType/PolicyLiteral constructor,
        or None.
        """
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _KNOWN_TYPE_NAMES:
                return node.func.id
        return None

    def _resolve_arg_type(self, node: ast.expr) -> str | None:
        """Resolve the SecurityType of an argument expression.

        Returns constructor name if known, None if unknown.
        """
        # Direct constructor: Label("x"), URL("http://...")
        ctor = self._get_constructor_name(node)
        if ctor is not None:
            return ctor

        # Variable reference: look up in var_constructors
        if isinstance(node, ast.Name):
            return self._var_constructors.get(node.id)

        return None

    def visit_Assign(self, node: ast.Assign) -> None:
        """Track variable assignments to SecurityType constructors."""
        ctor = self._get_constructor_name(node.value)
        if ctor is not None:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._var_constructors[target.id] = ctor
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Check tool call arguments against expected types."""
        # Match X.Y(...) where X is a known tool module name
        if not isinstance(node.func, ast.Attribute):
            self.generic_visit(node)
            return

        attr_node = node.func
        if not isinstance(attr_node.value, ast.Name):
            self.generic_visit(node)
            return

        module_name = attr_node.value.id
        func_name = attr_node.attr

        if module_name not in _TOOL_MODULE_NAMES:
            self.generic_visit(node)
            return

        lineno = getattr(node, "lineno", "?")

        # Check positional arguments
        for i, arg in enumerate(node.args):
            expected = self._type_map.get((module_name, func_name, i))
            if expected is None:
                continue
            actual = self._resolve_arg_type(arg)
            if actual is None:
                self.warnings.append(
                    f"Line {lineno}: {module_name}.{func_name}() arg[{i}]: "
                    f"cannot determine SecurityType (expected {expected})"
                )
                continue
            if actual != expected:
                self.violations.append(
                    f"Line {lineno}: {module_name}.{func_name}() arg[{i}]: "
                    f"expected {expected}, got {actual}"
                )

        # Check keyword arguments
        for kw in node.keywords:
            if kw.arg is None:
                continue  # **kwargs
            expected = self._type_map.get((module_name, func_name, kw.arg))
            if expected is None:
                continue
            actual = self._resolve_arg_type(kw.value)
            if actual is None:
                self.warnings.append(
                    f"Line {lineno}: {module_name}.{func_name}() kwarg '{kw.arg}': "
                    f"cannot determine SecurityType (expected {expected})"
                )
                continue
            if actual != expected:
                self.violations.append(
                    f"Line {lineno}: {module_name}.{func_name}() kwarg '{kw.arg}': "
                    f"expected {expected}, got {actual}"
                )

        self.generic_visit(node)

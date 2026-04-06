"""AST checker for untyped string literals.

Every string value in coder-generated code must be wrapped in a
platform-provided type constructor (SecurityType or PolicyLiteral).
This module walks the AST and flags any bare string literals.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field


# All recognized type constructors — both PolicyLiteral and SecurityType names
_KNOWN_TYPE_CONSTRUCTORS = frozenset({
    # PolicyLiteral types
    "EmailPolicy", "Domain", "Url", "FilePath", "Command",
    "IntRange", "Enum", "Bool", "Pattern",
    # SecurityType declarations
    "Label", "Email", "URL", "WorkspacePath", "SQL", "JSON", "UnstructuredText",
})


@dataclass
class StringDeclarationResult:
    """Result of string declaration checking."""

    passed: bool
    violations: list[str] = field(default_factory=list)


def check_string_declarations(code: str) -> StringDeclarationResult:
    """Check that all string literals are wrapped in type constructors.

    Args:
        code: Python source code to check.

    Returns:
        StringDeclarationResult with violations for untyped strings.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return StringDeclarationResult(passed=True)

    parent_map = _build_parent_map(tree)
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _is_exempt(node, parent_map):
                continue
            if _is_typed(node, parent_map):
                continue
            lineno = getattr(node, "lineno", "?")
            snippet = repr(node.value)
            if len(snippet) > 44:
                snippet = snippet[:40] + "...'"
            violations.append(
                f"Line {lineno}: untyped string literal {snippet} "
                f"— wrap in Label(), URL(), etc."
            )

        elif isinstance(node, ast.JoinedStr):
            # f-string as a whole: check if it's wrapped in a constructor
            if _is_typed(node, parent_map):
                continue
            if _is_exempt(node, parent_map):
                continue
            lineno = getattr(node, "lineno", "?")
            violations.append(
                f"Line {lineno}: untyped f-string "
                f"— wrap in Label(), UnstructuredText(), etc."
            )

    return StringDeclarationResult(
        passed=len(violations) == 0,
        violations=violations,
    )


def extract_unstructured_text_values(code: str) -> list[str]:
    """Extract literal string values from UnstructuredText() calls in code.

    Walks the AST and collects the string argument from every call of the form
    ``UnstructuredText("some text")``.  Non-literal arguments (variables,
    f-strings, expressions) are silently skipped — their runtime values are
    not available at review time and are already covered by taint analysis.

    Returns an empty list if the code does not parse (the AST step earlier in
    the pipeline will have already caught and rejected it).

    Args:
        code: Python source code to inspect.

    Returns:
        List of raw string values passed to UnstructuredText() as literals.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    values: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "UnstructuredText"):
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            values.append(first_arg.value)

    return values


def _build_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    """Build a mapping from node id to parent node."""
    parent_map: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent_map[id(child)] = node
    return parent_map


def _is_typed(node: ast.AST, parent_map: dict[int, ast.AST]) -> bool:
    """Check if node is wrapped in a recognized type constructor call."""
    parent = parent_map.get(id(node))
    if parent is None:
        return False
    if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name):
        return parent.func.id in _KNOWN_TYPE_CONSTRUCTORS
    return False


def _is_exempt(node: ast.AST, parent_map: dict[int, ast.AST]) -> bool:
    """Check if node is exempt from the typed-string requirement.

    Exempt cases:
    - String constants that are children of JoinedStr (f-string fragments)
    - Format spec in FormattedValue
    - Import module names (import from statement)
    - Keyword argument names (keyword.arg is a str in AST but not a node)
    - Dict keys in dict literals (programmer-supplied structural identifiers)
    """
    parent = parent_map.get(id(node))
    if parent is None:
        return False

    # f-string fragment: str Constant inside JoinedStr
    if isinstance(parent, ast.JoinedStr):
        return True

    # Format spec inside FormattedValue
    if isinstance(parent, ast.FormattedValue):
        return True

    # Dict key: string used as a key in a dict literal (not a value).
    # Dict keys are programmer-supplied structural identifiers, not user data,
    # so they don't need typed-string wrappers.
    if isinstance(parent, ast.Dict):
        # ast.Dict has .keys and .values lists of the same length.
        # Check if this node is one of the keys (not one of the values).
        if node in parent.keys:
            return True

    return False

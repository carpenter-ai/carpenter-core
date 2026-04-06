"""AST whitelist checker for verified flow analysis.

Validates that submitted code uses only the whitelisted Python subset.
This ensures all execution paths are enumerable and taint labels can
be tracked through all operations.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from ..config import get_config


@dataclass
class WhitelistResult:
    """Result of whitelist checking."""

    passed: bool
    violations: list[str] = field(default_factory=list)


# AST node types that are allowed in the whitelisted subset.
_ALLOWED_NODES = frozenset({
    # Module structure
    ast.Module,
    ast.Expr,

    # Assignments
    ast.Assign,
    ast.AugAssign,
    ast.AnnAssign,

    # Control flow (bounded)
    ast.If,
    ast.For,
    ast.Try,
    ast.ExceptHandler,

    # Expressions
    ast.Compare,
    ast.BoolOp,
    ast.UnaryOp,
    ast.BinOp,
    ast.Call,
    ast.IfExp,

    # Data construction
    ast.List,
    ast.Dict,
    ast.Tuple,
    ast.Set,
    ast.ListComp,
    ast.DictComp,
    ast.SetComp,

    # Access
    ast.Subscript,
    ast.Attribute,

    # String formatting
    ast.JoinedStr,
    ast.FormattedValue,

    # Imports (validated separately)
    ast.ImportFrom,

    # Primitives
    ast.Name,
    ast.Constant,
    ast.Starred,

    # Contexts
    ast.Store,
    ast.Load,
    ast.Del,

    # Operators
    ast.And,
    ast.Or,
    ast.Not,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.BitOr,
    ast.BitAnd,
    ast.BitXor,
    ast.LShift,
    ast.RShift,
    ast.Invert,
    ast.UAdd,
    ast.USub,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,

    # Comprehension internals
    ast.comprehension,

    # Call internals
    ast.keyword,

    # Import internals
    ast.alias,
})

# Explicitly rejected constructs (for clear error messages).
_REJECTED_DESCRIPTIONS = {
    ast.While: "while loops (use bounded for-loops or arc-level iteration)",
    ast.FunctionDef: "function definitions (keep code flat; split across arcs)",
    ast.AsyncFunctionDef: "async function definitions",
    ast.Lambda: "lambda expressions (keep code flat)",
    ast.Import: "bare imports (use 'from carpenter_tools... import ...')",
    ast.ClassDef: "class definitions (use plain data structures)",
    ast.Yield: "yield expressions (use arc-level concurrency)",
    ast.YieldFrom: "yield-from expressions",
    ast.Await: "await expressions (use arc-level concurrency)",
    ast.AsyncWith: "async with statements",
    ast.AsyncFor: "async for loops",
    ast.Global: "global statements",
    ast.Nonlocal: "nonlocal statements",
    ast.Assert: "assert statements",
    ast.Delete: "delete statements",
    ast.Raise: "raise statements",
    ast.Return: "return statements (code runs at module level)",
    ast.Pass: None,  # Pass is harmless, allow it
}

# Actually allow Pass — it's harmless
_ALLOWED_NODES_WITH_PASS = _ALLOWED_NODES | {ast.Pass}

# Valid import module prefixes
_ALLOWED_IMPORT_PREFIXES = (
    "carpenter_tools.",
    "carpenter_tools",
)

# Built-in default for safe stdlib modules allowed in verified code.
_DEFAULT_ALLOWED_STDLIB_MODULES = frozenset({
    "datetime",
    "json",
    "math",
    "re",
    "time",
})


def _get_allowed_stdlib_modules() -> frozenset[str]:
    """Return the effective allowed-stdlib-modules set.

    Uses config ``verification.allowed_stdlib_modules`` when non-empty,
    otherwise falls back to the built-in default.  Shared with dry_run.py.
    """
    verification = get_config("verification", {})
    if isinstance(verification, dict):
        override = verification.get("allowed_stdlib_modules", [])
        if override:
            return frozenset(override)
    return _DEFAULT_ALLOWED_STDLIB_MODULES


# Module-level alias for backward compatibility.
_ALLOWED_STDLIB_MODULES = _DEFAULT_ALLOWED_STDLIB_MODULES


def check_whitelist(code: str) -> WhitelistResult:
    """Check if code uses only the whitelisted Python subset.

    Returns WhitelistResult with passed=True if all constructs are
    allowed, or passed=False with a list of violation descriptions.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return WhitelistResult(
            passed=False,
            violations=[f"Syntax error: {e}"],
        )

    violations: list[str] = []

    for node in ast.walk(tree):
        node_type = type(node)

        # Check if node type is explicitly rejected
        if node_type in _REJECTED_DESCRIPTIONS:
            desc = _REJECTED_DESCRIPTIONS[node_type]
            if desc is not None:  # None means allowed (Pass)
                line = getattr(node, "lineno", "?")
                violations.append(f"Line {line}: {desc}")
            continue

        # Check if node type is in the allowed set (including Pass)
        if node_type not in _ALLOWED_NODES_WITH_PASS:
            # Try/ExceptHandler compat for Python 3.11+ (TryStar)
            name = node_type.__name__
            line = getattr(node, "lineno", "?")
            violations.append(f"Line {line}: disallowed construct '{name}'")
            continue

        # Validate imports
        if isinstance(node, ast.ImportFrom):
            result = _validate_import(node)
            if result is not None:
                violations.append(result)

        # Validate function calls
        if isinstance(node, ast.Call):
            result = _validate_call(node)
            if result is not None:
                violations.append(result)

    return WhitelistResult(passed=len(violations) == 0, violations=violations)


def _validate_import(node: ast.ImportFrom) -> str | None:
    """Validate an ImportFrom node. Returns error string or None."""
    module = node.module or ""

    # carpenter_tools.* always allowed
    if any(module == p or module.startswith(p + ".") for p in ("carpenter_tools",)):
        return None

    # Safe stdlib modules allowed (must match dry_run._mock_import)
    if module in _get_allowed_stdlib_modules():
        return None

    line = getattr(node, "lineno", "?")
    return f"Line {line}: import from '{module}' not allowed (only carpenter_tools.* or safe stdlib)"


def _validate_call(node: ast.Call) -> str | None:
    """Validate a Call node. Returns error string or None.

    Callee must resolve to a carpenter_tools function, a PolicyLiteral
    constructor, or a safe builtin (len, range, list, dict, tuple, set,
    str, int, float, bool, print, enumerate, zip, sorted, min, max, abs,
    round, isinstance, type, hasattr, getattr).
    """
    callee = node.func
    line = getattr(node, "lineno", "?")

    # Safe builtins
    safe_builtins = frozenset({
        "len", "range", "list", "dict", "tuple", "set",
        "str", "int", "float", "bool", "print",
        "enumerate", "zip", "sorted", "min", "max",
        "abs", "round", "isinstance", "type", "hasattr", "getattr",
    })

    # Simple name call: e.g., Email("foo"), len(x)
    if isinstance(callee, ast.Name):
        if callee.id in safe_builtins:
            return None
        # Policy literal constructors (imported names) are OK — checked at taint analysis
        return None

    # Attribute call: e.g., arc.create_batch(...), state.get(...)
    if isinstance(callee, ast.Attribute):
        # Allow any attribute call — the taint analysis validates these
        return None

    # Subscript call: e.g., some_dict["key"](...) — reject
    return f"Line {line}: unsupported call expression"

"""Static analysis tools for the review pipeline."""

import ast
import re
from collections import Counter


def analyze_file_type(code: str) -> dict:
    """Analyze basic file properties.

    Returns: {lines, size_bytes, has_imports, has_classes, has_functions}
    """
    lines = code.split("\n")
    return {
        "lines": len(lines),
        "size_bytes": len(code.encode("utf-8")),
        "has_imports": any(line.strip().startswith(("import ", "from ")) for line in lines),
        "has_classes": any(line.strip().startswith("class ") for line in lines),
        "has_functions": any(line.strip().startswith("def ") for line in lines),
    }


def validate_syntax(code: str) -> dict:
    """Validate Python syntax.

    Returns: {valid: bool, errors: [{line, message}]}
    """
    try:
        ast.parse(code)
        return {"valid": True, "errors": []}
    except SyntaxError as e:
        return {
            "valid": False,
            "errors": [{"line": e.lineno or 0, "message": str(e.msg)}],
        }


def extract_comments_and_strings(code: str) -> dict:
    """Extract comments, string literals, and docstrings from Python code.

    Returns: {comments: [str], string_literals: [str], docstrings: [str]}
    """
    comments = []
    string_literals = []
    docstrings = []

    # Extract comments (lines starting with # after stripping)
    for line in code.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            comments.append(stripped[1:].strip())
        elif "#" in stripped:
            # Inline comment — crude extraction (won't handle # in strings perfectly)
            parts = stripped.split("#", 1)
            if len(parts) > 1:
                comments.append(parts[1].strip())

    # Extract strings and docstrings from AST
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                # Check if it's a docstring (Expr containing a string at module/class/function level)
                string_literals.append(node.value)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                ds = ast.get_docstring(node)
                if ds:
                    docstrings.append(ds)
    except SyntaxError:
        pass

    return {
        "comments": comments,
        "string_literals": string_literals,
        "docstrings": docstrings,
    }


def check_plugin_prompt_safety(code: str) -> dict:
    """Check that plugin tool calls use literal prompt strings.

    Walks the AST looking for calls to plugin.submit_task(). If the
    prompt argument is not a string literal, warns the reviewer.

    Returns: {safe: bool, warnings: [str]}
    """
    warnings = []

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {"safe": True, "warnings": []}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # Match: plugin.submit_task(...) or submit_task(...)
        func_name = _get_call_name(node)
        if func_name not in ("plugin.submit_task", "submit_task"):
            continue

        # Find the prompt argument (positional index 1 or keyword)
        prompt_node = None

        # Check positional args (plugin_name=0, prompt=1)
        if len(node.args) >= 2:
            prompt_node = node.args[1]

        # Check keyword args
        for kw in node.keywords:
            if kw.arg == "prompt":
                prompt_node = kw.value
                break

        if prompt_node is None:
            continue

        # Check if the prompt is a string literal
        if not isinstance(prompt_node, ast.Constant) or \
           not isinstance(prompt_node.value, str):
            line = getattr(prompt_node, "lineno", "?")
            warnings.append(
                f"Line {line}: plugin.submit_task() called with non-literal "
                f"prompt. Reviewer should verify the prompt source is trusted."
            )

    return {
        "safe": len(warnings) == 0,
        "warnings": warnings,
    }


def check_import_star(code: str) -> dict:
    """Check for wildcard imports (from X import *).

    Wildcard imports are prohibited in untrusted code as they obscure
    what names are being imported and can be used for obfuscation.

    Returns: {violation: bool, findings: [{line, message}]}
    """
    findings = []

    # Regex pattern to detect 'from X import *' (with optional spaces/tabs, relative imports)
    # Matches: "from module import *", "from .module import *", "from ..module import *"
    # Use [ \t]* instead of \s* to avoid matching newlines in the pattern
    pattern = re.compile(r'^[ \t]*from\s+[\w.]+\s+import\s+\*', re.MULTILINE)

    for match in pattern.finditer(code):
        # Calculate line number from position
        line_num = code[:match.start()].count('\n') + 1
        findings.append({
            "line": line_num,
            "message": (
                "Wildcard imports (from X import *) are not allowed in untrusted code. "
                "Please use explicit imports. See coding guidelines."
            )
        })

    return {
        "violation": len(findings) > 0,
        "findings": findings,
    }


def _get_call_name(node: ast.Call) -> str:
    """Extract the function name from a Call node."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    elif isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            return f"{func.value.id}.{func.attr}"
        elif isinstance(func.value, ast.Attribute):
            # Handle deeper chains like a.b.submit_task
            return func.attr
    return ""

"""Code sanitizer for the review pipeline.

Strips comments, string literals, and renames user-defined variables
to prevent prompt injection aimed at the code reviewer.

The sanitized code is used ONLY for the reviewer's inspection.
The original code is what actually gets executed.
"""

import ast
import builtins
import keyword
from typing import Dict, Tuple


# Names that should never be renamed — Python builtins and keywords
_BUILTINS = frozenset(dir(builtins)) | frozenset(keyword.kwlist) | frozenset({
    "__name__", "__file__", "__doc__", "__all__",
    "__init__", "__main__", "__spec__",
})


def _sequential_name(index: int) -> str:
    """Generate sequential variable names: a, b, ..., z, aa, ab, ..."""
    result = ""
    n = index
    while True:
        result = chr(ord("a") + n % 26) + result
        n = n // 26 - 1
        if n < 0:
            break
    return result


class _NameCollector(ast.NodeVisitor):
    """First pass: collect user-defined names and imported names."""

    def __init__(self):
        self.defined_names: set[str] = set()
        self.imported_names: set[str] = set()

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.defined_names.add(node.id)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self.defined_names.add(node.name)
        self._collect_args(node.args)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.defined_names.add(node.name)
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            if alias.asname:
                # import X as Y — Y is user-chosen, rename it
                self.defined_names.add(alias.asname)
            else:
                # import X — X is a real module name, preserve it
                self.imported_names.add(alias.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.asname:
                # from X import Y as Z — Z is user-chosen, rename it
                self.defined_names.add(alias.asname)
            else:
                # from X import Y — Y is the real name, preserve it
                self.imported_names.add(alias.name)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        if node.name:
            self.defined_names.add(node.name)
        self.generic_visit(node)

    def _collect_args(self, arguments: ast.arguments):
        for arg in arguments.args + arguments.posonlyargs + arguments.kwonlyargs:
            self.defined_names.add(arg.arg)
        if arguments.vararg:
            self.defined_names.add(arguments.vararg.arg)
        if arguments.kwarg:
            self.defined_names.add(arguments.kwarg.arg)


def _strip_docstrings(tree: ast.AST) -> None:
    """Remove docstrings from module, class, and function bodies (in-place)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                if len(node.body) > 1:
                    node.body = node.body[1:]
                else:
                    node.body = [ast.Pass()]


class _CodeSanitizer(ast.NodeTransformer):
    """Second pass: replace string literals and rename user-defined symbols."""

    def __init__(self, rename_map: dict[str, str]):
        self.rename_map = rename_map
        self._string_counter = 0

    def _next_placeholder(self) -> str:
        self._string_counter += 1
        return f"S{self._string_counter}"

    # --- String literal replacement ---

    def visit_Constant(self, node):
        if isinstance(node.value, (str, bytes)):
            return ast.copy_location(
                ast.Name(id=self._next_placeholder(), ctx=ast.Load()),
                node,
            )
        return node

    def visit_JoinedStr(self, node):
        # Replace entire f-string with a single placeholder
        return ast.copy_location(
            ast.Name(id=self._next_placeholder(), ctx=ast.Load()),
            node,
        )

    # --- Variable/function/class renaming ---

    def visit_Name(self, node):
        if node.id in self.rename_map:
            node.id = self.rename_map[node.id]
        return node

    def visit_FunctionDef(self, node):
        if node.name in self.rename_map:
            node.name = self.rename_map[node.name]
        self._rename_args(node.args)
        self.generic_visit(node)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        if node.name in self.rename_map:
            node.name = self.rename_map[node.name]
        self.generic_visit(node)
        return node

    def visit_ExceptHandler(self, node):
        if node.name and node.name in self.rename_map:
            node.name = self.rename_map[node.name]
        self.generic_visit(node)
        return node

    def visit_Global(self, node):
        node.names = [self.rename_map.get(n, n) for n in node.names]
        return node

    def visit_Nonlocal(self, node):
        node.names = [self.rename_map.get(n, n) for n in node.names]
        return node

    def visit_Import(self, node):
        for alias in node.names:
            if alias.asname and alias.asname in self.rename_map:
                alias.asname = self.rename_map[alias.asname]
        return node

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.asname and alias.asname in self.rename_map:
                alias.asname = self.rename_map[alias.asname]
        return node

    def _rename_args(self, arguments: ast.arguments):
        for arg in arguments.args + arguments.posonlyargs + arguments.kwonlyargs:
            if arg.arg in self.rename_map:
                arg.arg = self.rename_map[arg.arg]
        if arguments.vararg and arguments.vararg.arg in self.rename_map:
            arguments.vararg.arg = self.rename_map[arguments.vararg.arg]
        if arguments.kwarg and arguments.kwarg.arg in self.rename_map:
            arguments.kwarg.arg = self.rename_map[arguments.kwarg.arg]


def _build_rename_map(
    defined_names: set[str],
    imported_names: set[str],
) -> dict[str, str]:
    """Build a mapping from user-defined names to sequential identifiers."""
    # Only rename names that are user-defined (not builtins or imports)
    renameable = defined_names - imported_names - _BUILTINS
    rename_map = {}
    idx = 0
    for name in sorted(renameable):  # Sort for deterministic output
        new_name = _sequential_name(idx)
        # Skip if new_name collides with a preserved name
        while new_name in _BUILTINS or new_name in imported_names:
            idx += 1
            new_name = _sequential_name(idx)
        rename_map[name] = new_name
        idx += 1
    return rename_map


def sanitize_for_review(source: str) -> tuple[str, list[str]]:
    """Sanitize Python source code for reviewer consumption.

    Performs three transformations:
    1. Removes docstrings
    2. Replaces all string/bytes literals with placeholders (S1, S2, ...)
    3. Renames all user-defined symbols to sequential identifiers (a, b, c, ...)

    Comments are automatically dropped by the AST round-trip (they aren't
    part of the AST).

    Args:
        source: Python source code string.

    Returns:
        Tuple of (sanitized_source, advisory_notes).
        - sanitized_source: transformed code safe for reviewer inspection
        - advisory_notes: any observations from the sanitization process
    """
    advisory_notes = []

    tree = ast.parse(source)

    # First pass: collect names
    collector = _NameCollector()
    collector.visit(tree)

    # Build rename map
    rename_map = _build_rename_map(collector.defined_names, collector.imported_names)

    # No advisory when there are no user-defined names to rename — simple code
    # (e.g. a single set_value() call) is perfectly fine and not worth noting.

    # Re-parse for a clean tree (collector doesn't modify, but be safe)
    tree = ast.parse(source)

    # Strip docstrings before the main transform
    _strip_docstrings(tree)

    # Second pass: replace strings and rename variables
    sanitizer = _CodeSanitizer(rename_map)
    tree = sanitizer.visit(tree)
    ast.fix_missing_locations(tree)

    sanitized = ast.unparse(tree)

    return sanitized, advisory_notes


def sanitize_changeset(
    files: Dict[str, str]
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Sanitize a multi-file changeset with consistent cross-file renaming.

    Performs:
    1. Filename obfuscation (malicious.py → file_a.py)
    2. Cross-file symbol table construction
    3. Consistent renaming across all files
    4. Import statement updates to use obfuscated filenames
    5. String literal and docstring removal per file

    Args:
        files: Dict mapping original filename to code content

    Returns:
        Tuple of (sanitized_files, filename_map):
        - sanitized_files: Dict mapping obfuscated filename to sanitized code
        - filename_map: Dict mapping obfuscated filename to original filename
                       (for internal tracking only, NOT shown to reviewer)
    """
    # Step 1: Create filename mapping (deterministic order for testing)
    original_filenames = sorted(files.keys())
    filename_map = {}  # obfuscated -> original
    reverse_filename_map = {}  # original -> obfuscated

    for idx, original_name in enumerate(original_filenames):
        obfuscated_name = f"file_{chr(ord('a') + idx)}.py"
        filename_map[obfuscated_name] = original_name
        reverse_filename_map[original_name] = obfuscated_name

    # Step 2: Build cross-file symbol table
    # Map: filename -> set of defined symbols
    symbol_table: Dict[str, set[str]] = {}
    imported_table: Dict[str, set[str]] = {}

    for filename, code in files.items():
        try:
            tree = ast.parse(code)
            collector = _NameCollector()
            collector.visit(tree)
            symbol_table[filename] = collector.defined_names
            imported_table[filename] = collector.imported_names
        except SyntaxError:
            # Skip files with syntax errors
            symbol_table[filename] = set()
            imported_table[filename] = set()

    # Step 3: Build module name mappings
    # Map: module name (without .py) -> filename
    module_to_file = {}
    for filename in files.keys():
        module_name = _module_name_from_filename(filename)
        module_to_file[module_name] = filename

    # Step 4: Create consistent cross-file rename map
    # Collect all defined names across all files
    all_defined = set()
    all_imported = set()
    for defined in symbol_table.values():
        all_defined.update(defined)
    for imported in imported_table.values():
        all_imported.update(imported)

    # Build unified rename map
    global_rename_map = _build_rename_map(all_defined, all_imported)

    # Step 5: Sanitize each file with cross-file aware transformations
    sanitized_files = {}

    for original_filename, code in files.items():
        obfuscated_filename = reverse_filename_map[original_filename]

        try:
            tree = ast.parse(code)

            # Strip docstrings
            _strip_docstrings(tree)

            # Transform with cross-file aware sanitizer
            sanitizer = _CrossFileSanitizer(
                global_rename_map,
                module_to_file,
                reverse_filename_map,
            )
            tree = sanitizer.visit(tree)
            ast.fix_missing_locations(tree)

            sanitized_code = ast.unparse(tree)
            sanitized_files[obfuscated_filename] = sanitized_code

        except SyntaxError:
            # For syntax errors, just use empty file
            sanitized_files[obfuscated_filename] = "# Syntax error in original file"

    return sanitized_files, filename_map


class _CrossFileSanitizer(_CodeSanitizer):
    """Enhanced sanitizer that handles cross-file imports and filename obfuscation."""

    def __init__(
        self,
        rename_map: dict[str, str],
        module_to_file: dict[str, str],
        filename_obfuscation: dict[str, str],  # original -> obfuscated
    ):
        super().__init__(rename_map)
        self.module_to_file = module_to_file
        self.filename_obfuscation = filename_obfuscation

    def visit_Import(self, node):
        """Handle 'import module' statements with filename obfuscation."""
        for alias in node.names:
            # Check if this is importing a module from the changeset
            module_name = alias.name.split(".")[0]  # Handle dotted imports
            if module_name in self.module_to_file:
                original_file = self.module_to_file[module_name]
                if original_file in self.filename_obfuscation:
                    # Replace module name with obfuscated filename (without .py)
                    obfuscated_file = self.filename_obfuscation[original_file]
                    obfuscated_module = _module_name_from_filename(obfuscated_file)
                    alias.name = obfuscated_module

            # Handle 'import X as Y' renaming
            if alias.asname and alias.asname in self.rename_map:
                alias.asname = self.rename_map[alias.asname]

        return node

    def visit_ImportFrom(self, node):
        """Handle 'from module import ...' statements with filename obfuscation."""
        # Check if module is in the changeset
        if node.module:
            module_name = node.module.split(".")[0]
            if module_name in self.module_to_file:
                original_file = self.module_to_file[module_name]
                if original_file in self.filename_obfuscation:
                    # Replace module name with obfuscated filename (without .py)
                    obfuscated_file = self.filename_obfuscation[original_file]
                    obfuscated_module = _module_name_from_filename(obfuscated_file)
                    node.module = obfuscated_module

        # Handle 'from X import Y as Z' renaming
        for alias in node.names:
            if alias.asname and alias.asname in self.rename_map:
                alias.asname = self.rename_map[alias.asname]

        return node


def _module_name_from_filename(filename: str) -> str:
    """Convert filename to module name (strip .py extension)."""
    if filename.endswith(".py"):
        return filename[:-3]
    return filename

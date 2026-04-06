"""Tests for code_sanitizer — string stripping, comment removal, variable renaming."""

import ast

from carpenter.review.code_sanitizer import (
    sanitize_for_review,
    sanitize_changeset,
    _sequential_name,
)


# --- Helper ---


def _parse_ok(code: str) -> bool:
    """Check that sanitized code is valid Python."""
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


# --- _sequential_name ---


class TestSequentialName:
    def test_first_26(self):
        assert _sequential_name(0) == "a"
        assert _sequential_name(25) == "z"

    def test_wraps_to_aa(self):
        assert _sequential_name(26) == "aa"
        assert _sequential_name(27) == "ab"

    def test_triple_letter(self):
        # 26 + 26*26 = 702 -> "aaa"
        assert _sequential_name(702) == "aaa"


# --- String stripping ---


class TestStringStripping:
    def test_single_quoted(self):
        code = "x = 'hello world'\n"
        result, _ = sanitize_for_review(code)
        assert "hello" not in result
        assert "S1" in result

    def test_double_quoted(self):
        code = 'x = "hello world"\n'
        result, _ = sanitize_for_review(code)
        assert "hello" not in result

    def test_triple_quoted(self):
        code = 'x = """multi\nline\nstring"""\n'
        result, _ = sanitize_for_review(code)
        assert "multi" not in result
        assert "line" not in result

    def test_byte_string(self):
        code = "x = b'binary data'\n"
        result, _ = sanitize_for_review(code)
        assert "binary" not in result

    def test_fstring(self):
        code = 'x = 1\ny = f"value is {x}"\n'
        result, _ = sanitize_for_review(code)
        assert "value is" not in result
        # The f-string becomes a placeholder
        assert _parse_ok(result)

    def test_multiple_strings_get_distinct_placeholders(self):
        code = 'a = "first"\nb = "second"\n'
        result, _ = sanitize_for_review(code)
        assert "S1" in result
        assert "S2" in result
        assert "first" not in result
        assert "second" not in result


# --- Comment stripping ---


class TestCommentStripping:
    def test_inline_comment_removed(self):
        code = "x = 1  # this is a comment\n"
        result, _ = sanitize_for_review(code)
        assert "comment" not in result

    def test_line_comment_removed(self):
        code = "# full line comment\nx = 1\n"
        result, _ = sanitize_for_review(code)
        assert "full line" not in result

    def test_code_structure_preserved(self):
        code = "# comment\nx = 1\ny = 2\n"
        result, _ = sanitize_for_review(code)
        # Variables renamed but assignments preserved
        assert "=" in result
        assert _parse_ok(result)


# --- Docstring removal ---


class TestDocstringRemoval:
    def test_module_docstring(self):
        code = '"""This module does stuff."""\nx = 1\n'
        result, _ = sanitize_for_review(code)
        assert "module" not in result
        assert "stuff" not in result

    def test_function_docstring(self):
        code = 'def foo():\n    """Docstring here."""\n    return 1\n'
        result, _ = sanitize_for_review(code)
        assert "Docstring" not in result
        assert _parse_ok(result)

    def test_class_docstring(self):
        code = 'class Foo:\n    """Class doc."""\n    pass\n'
        result, _ = sanitize_for_review(code)
        assert "Class doc" not in result

    def test_function_with_only_docstring(self):
        code = 'def foo():\n    """Only a docstring."""\n'
        result, _ = sanitize_for_review(code)
        # Should have pass instead of empty body
        assert "pass" in result
        assert _parse_ok(result)


# --- Variable renaming ---


class TestVariableRenaming:
    def test_simple_assignment(self):
        code = "my_var = 42\nprint(my_var)\n"
        result, _ = sanitize_for_review(code)
        assert "my_var" not in result
        # print is a builtin, should be preserved
        assert "print" in result

    def test_function_def_renamed(self):
        code = "def my_function(x):\n    return x + 1\n"
        result, _ = sanitize_for_review(code)
        assert "my_function" not in result
        assert "def " in result
        assert _parse_ok(result)

    def test_class_def_renamed(self):
        code = "class MyClass:\n    pass\n"
        result, _ = sanitize_for_review(code)
        assert "MyClass" not in result
        assert "class " in result

    def test_function_args_renamed(self):
        code = "def foo(bar, baz):\n    return bar + baz\n"
        result, _ = sanitize_for_review(code)
        assert "bar" not in result
        assert "baz" not in result

    def test_for_loop_var(self):
        code = "for item in range(10):\n    print(item)\n"
        result, _ = sanitize_for_review(code)
        assert "item" not in result
        assert "range" in result  # builtin preserved
        assert "print" in result  # builtin preserved

    def test_with_as_var(self):
        code = 'with open("f") as handle:\n    data = handle.read()\n'
        result, _ = sanitize_for_review(code)
        assert "handle" not in result
        assert "open" in result  # builtin preserved
        assert _parse_ok(result)

    def test_except_handler_var(self):
        code = "try:\n    pass\nexcept Exception as err:\n    print(err)\n"
        result, _ = sanitize_for_review(code)
        assert "err" not in result
        assert "Exception" in result  # builtin preserved

    def test_starred_assignment(self):
        code = "first, *rest = [1, 2, 3]\n"
        result, _ = sanitize_for_review(code)
        assert "first" not in result
        assert "rest" not in result
        assert _parse_ok(result)

    def test_varargs_kwargs(self):
        code = "def foo(*args, **kwargs):\n    return args, kwargs\n"
        result, _ = sanitize_for_review(code)
        assert "args" not in result
        assert "kwargs" not in result

    def test_global_statement(self):
        code = "counter = 0\ndef inc():\n    global counter\n    counter += 1\n"
        result, _ = sanitize_for_review(code)
        assert "counter" not in result
        assert "global" in result
        assert _parse_ok(result)

    def test_comprehension_var(self):
        code = "squares = [x * x for x in range(10)]\n"
        result, _ = sanitize_for_review(code)
        assert "squares" not in result
        assert "range" in result

    def test_walrus_operator(self):
        code = "if (n := 10) > 5:\n    print(n)\n"
        result, _ = sanitize_for_review(code)
        assert _parse_ok(result)


# --- Preservation of external names ---


class TestPreservation:
    def test_builtins_preserved(self):
        code = "x = len([1, 2, 3])\ny = isinstance(x, int)\n"
        result, _ = sanitize_for_review(code)
        assert "len" in result
        assert "isinstance" in result
        assert "int" in result

    def test_bare_import_preserved(self):
        code = "import os\nresult = os.path.join('a', 'b')\n"
        result, _ = sanitize_for_review(code)
        assert "os" in result
        assert "os.path.join" in result

    def test_from_import_preserved(self):
        code = "from pathlib import Path\np = Path('.')\n"
        result, _ = sanitize_for_review(code)
        assert "Path" in result

    def test_import_alias_renamed(self):
        code = "import numpy as np\nresult = np.array([1, 2])\n"
        result, _ = sanitize_for_review(code)
        assert "numpy" in result  # real module name preserved
        assert "np" not in result.replace("numpy", "")  # alias renamed

    def test_from_import_alias_renamed(self):
        code = "from os.path import join as j\nresult = j('a', 'b')\n"
        result, _ = sanitize_for_review(code)
        assert "join" in result  # real name preserved in import

    def test_carpenter_tools_preserved(self):
        code = (
            "from carpenter_tools.act import files\n"
            "data = files.write_file('out.txt', 'content')\n"
        )
        result, _ = sanitize_for_review(code)
        assert "files" in result  # imported name preserved
        assert "write_file" in result  # attribute preserved
        assert "carpenter_tools" in result

    def test_attribute_names_preserved(self):
        code = "import os\nresult = os.environ.get('HOME')\n"
        result, _ = sanitize_for_review(code)
        # Attribute access (.environ, .get) is preserved because
        # attributes are strings in the AST, not Name nodes
        assert ".environ" in result
        assert ".get" in result


# --- Edge cases ---


class TestEdgeCases:
    def test_empty_code(self):
        result, notes = sanitize_for_review("")
        assert _parse_ok(result)
        assert notes == []  # no advisory — simple code with nothing to rename is fine

    def test_no_user_defined_names(self):
        code = "print(42)\n"
        result, notes = sanitize_for_review(code)
        assert "print" in result
        assert "42" in result
        assert notes == []  # no advisory — nothing to rename is not a concern

    def test_deterministic_output(self):
        code = "x = 1\ny = 2\nz = 3\n"
        r1, _ = sanitize_for_review(code)
        r2, _ = sanitize_for_review(code)
        assert r1 == r2

    def test_nested_functions(self):
        code = (
            "def outer(x):\n"
            "    def inner(y):\n"
            "        return x + y\n"
            "    return inner\n"
        )
        result, _ = sanitize_for_review(code)
        assert "outer" not in result
        assert "inner" not in result
        assert _parse_ok(result)

    def test_complex_code_round_trips(self):
        code = (
            "import json\n"
            "from pathlib import Path\n"
            "\n"
            "def process_files(directory, pattern='*.txt'):\n"
            '    """Process all matching files."""\n'
            "    results = []\n"
            "    for path in Path(directory).glob(pattern):\n"
            "        with open(path) as f:\n"
            "            data = json.load(f)\n"
            "            results.append(data)\n"
            "    return results\n"
        )
        result, _ = sanitize_for_review(code)
        assert _parse_ok(result)
        # Module names preserved
        assert "json" in result
        assert "Path" in result
        # User names renamed
        assert "process_files" not in result
        assert "directory" not in result
        assert "results" not in result
        # Docstring removed
        assert "Process all" not in result


# --- Multi-file changeset sanitization ---


class TestChangesetSanitization:
    def test_filename_obfuscation(self):
        """Test that filenames are obfuscated to file_a.py, file_b.py, etc."""
        files = {
            "malicious_payload.py": "def bad(): pass",
            "helper_script.py": "def good(): pass",
        }
        sanitized, filename_map = sanitize_changeset(files)

        # Should have obfuscated filenames
        assert "file_a.py" in sanitized
        assert "file_b.py" in sanitized
        # Original filenames should not appear as keys
        assert "malicious_payload.py" not in sanitized
        assert "helper_script.py" not in sanitized

        # Filename map should map obfuscated -> original
        assert "malicious_payload.py" in filename_map.values()
        assert "helper_script.py" in filename_map.values()

    def test_import_update_simple(self):
        """Test that imports are updated to use obfuscated filenames."""
        files = {
            "main.py": "import helpers",
            "helpers.py": "def func(): pass",
        }
        sanitized, filename_map = sanitize_changeset(files)

        # Find which file is main (should be file_a since 'main.py' < 'helpers.py')
        # Actually 'helpers.py' < 'main.py' alphabetically
        obfuscated_helpers = None
        obfuscated_main = None
        for obf, orig in filename_map.items():
            if orig == "helpers.py":
                obfuscated_helpers = obf
            elif orig == "main.py":
                obfuscated_main = obf

        # main.py should import the obfuscated helper module name
        main_code = sanitized[obfuscated_main]
        expected_module = obfuscated_helpers.replace(".py", "")
        assert f"import {expected_module}" in main_code
        # Should NOT contain original module name
        assert "import helpers" not in main_code

    def test_import_from_update(self):
        """Test that 'from module import ...' is updated."""
        files = {
            "main.py": "from utils import process",
            "utils.py": "def process(): pass",
        }
        sanitized, filename_map = sanitize_changeset(files)

        # Find obfuscated filenames
        obfuscated_main = [k for k, v in filename_map.items() if v == "main.py"][0]

        main_code = sanitized[obfuscated_main]
        # Should have obfuscated module name
        assert "from utils import" not in main_code
        # Should import from file_a or file_b (whichever is utils)
        assert "from file_" in main_code

    def test_consistent_symbol_renaming(self):
        """Test that symbols are renamed consistently across files."""
        files = {
            "file_a.py": """
def shared_func():
    pass
""",
            "file_b.py": """
from file_a import shared_func

def caller():
    shared_func()
""",
        }
        sanitized, _ = sanitize_changeset(files)

        # Both files should use the same renamed symbol
        # (hard to test exact name, but should be consistent)
        # Just verify both files sanitized successfully
        assert len(sanitized) == 2
        for code in sanitized.values():
            assert _parse_ok(code)

    def test_cross_file_no_collision(self):
        """Test that renamed symbols don't collide across files."""
        files = {
            "a.py": "def func1(): pass",
            "b.py": "def func2(): pass",
        }
        sanitized, _ = sanitize_changeset(files)

        # All files should be valid Python
        for code in sanitized.values():
            assert _parse_ok(code)

    def test_external_imports_preserved(self):
        """Test that external imports (stdlib, etc.) are not obfuscated."""
        files = {
            "main.py": "import os\nimport sys\nimport helpers",
            "helpers.py": "def func(): pass",
        }
        sanitized, filename_map = sanitize_changeset(files)

        obfuscated_main = [k for k, v in filename_map.items() if v == "main.py"][0]
        main_code = sanitized[obfuscated_main]

        # External imports should remain unchanged
        assert "import os" in main_code
        assert "import sys" in main_code
        # Internal import should be obfuscated
        assert "import helpers" not in main_code

    def test_syntax_error_handling(self):
        """Test that syntax errors in one file don't crash the sanitizer."""
        files = {
            "good.py": "def func(): pass",
            "bad.py": "def :",  # Syntax error
        }
        sanitized, _ = sanitize_changeset(files)

        # Should still sanitize the good file
        assert len(sanitized) == 2
        # Bad file should have placeholder
        bad_key = [k for k, v in sanitized.items() if "Syntax error" in v][0]
        assert "Syntax error" in sanitized[bad_key]

    def test_deterministic_ordering(self):
        """Test that filename obfuscation is deterministic (alphabetical)."""
        files = {
            "zebra.py": "pass",
            "alpha.py": "pass",
            "middle.py": "pass",
        }
        sanitized, filename_map = sanitize_changeset(files)

        # Alpha should be file_a, middle should be file_b, zebra should be file_c
        assert filename_map["file_a.py"] == "alpha.py"
        assert filename_map["file_b.py"] == "middle.py"
        assert filename_map["file_c.py"] == "zebra.py"

    def test_string_literals_removed(self):
        """Test that string literals are removed in multi-file context."""
        files = {
            "main.py": 'msg = "secret message"',
        }
        sanitized, _ = sanitize_changeset(files)

        code = list(sanitized.values())[0]
        assert "secret message" not in code
        assert "S1" in code

    def test_relative_imports(self):
        """Test that relative imports are handled (basic support)."""
        files = {
            "main.py": "from . import helpers",
            "helpers.py": "def func(): pass",
        }
        # Should not crash (though relative import handling is limited)
        sanitized, _ = sanitize_changeset(files)
        assert len(sanitized) == 2

    def test_empty_changeset(self):
        """Test handling of empty changeset."""
        files = {}
        sanitized, filename_map = sanitize_changeset(files)
        assert sanitized == {}
        assert filename_map == {}

    def test_single_file_changeset(self):
        """Test that single-file changeset works."""
        files = {
            "main.py": "def func(): pass",
        }
        sanitized, filename_map = sanitize_changeset(files)
        assert len(sanitized) == 1
        assert "file_a.py" in sanitized
        assert filename_map["file_a.py"] == "main.py"

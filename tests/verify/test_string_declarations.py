"""Tests for string declaration AST checker (verify/string_declarations.py)."""

import pytest

from carpenter.verify.string_declarations import (
    check_string_declarations,
    extract_unstructured_text_values,
)


class TestTypedStringsPassing:
    """Typed strings should pass the check."""

    def test_label_passes(self):
        code = 'x = Label("status")'
        result = check_string_declarations(code)
        assert result.passed

    def test_url_passes(self):
        code = 'x = URL("https://example.com")'
        result = check_string_declarations(code)
        assert result.passed

    def test_email_passes(self):
        code = 'x = Email("a@b.com")'
        result = check_string_declarations(code)
        assert result.passed

    def test_workspace_path_passes(self):
        code = 'x = WorkspacePath("results.json")'
        result = check_string_declarations(code)
        assert result.passed

    def test_sql_passes(self):
        code = 'x = SQL("SELECT 1")'
        result = check_string_declarations(code)
        assert result.passed

    def test_json_passes(self):
        code = 'x = JSON("{}")'
        result = check_string_declarations(code)
        assert result.passed

    def test_unstructured_text_passes(self):
        code = 'x = UnstructuredText("hello")'
        result = check_string_declarations(code)
        assert result.passed


class TestPolicyLiteralsPassing:
    """PolicyLiteral constructors also count as typed."""

    def test_policy_email_passes(self):
        code = 'x = Email("test@example.com")'
        result = check_string_declarations(code)
        assert result.passed

    def test_enum_passes(self):
        code = 'x = Enum("active")'
        result = check_string_declarations(code)
        assert result.passed

    def test_domain_passes(self):
        code = 'x = Domain("example.com")'
        result = check_string_declarations(code)
        assert result.passed

    def test_url_policy_passes(self):
        code = 'x = Url("https://api.com")'
        result = check_string_declarations(code)
        assert result.passed

    def test_filepath_passes(self):
        code = 'x = FilePath("/safe/dir")'
        result = check_string_declarations(code)
        assert result.passed

    def test_command_passes(self):
        code = 'x = Command("echo hello")'
        result = check_string_declarations(code)
        assert result.passed

    def test_pattern_passes(self):
        code = r'x = Pattern("\\d+")'
        result = check_string_declarations(code)
        assert result.passed


class TestUntypedStringsRejected:
    """Bare string literals should be flagged as violations."""

    def test_bare_string_rejected(self):
        code = 'x = "hello"'
        result = check_string_declarations(code)
        assert not result.passed
        assert len(result.violations) == 1
        assert "untyped string literal" in result.violations[0]

    def test_bare_fstring_rejected(self):
        code = 'name = Label("x")\ny = f"Hello {name}"'
        result = check_string_declarations(code)
        assert not result.passed
        assert any("untyped f-string" in v for v in result.violations)

    def test_multiple_bare_strings(self):
        code = 'x = "a"\ny = "b"\nz = "c"'
        result = check_string_declarations(code)
        assert not result.passed
        assert len(result.violations) == 3


class TestFstringFragmentsExempt:
    """F-string fragments inside a typed f-string are exempt."""

    def test_typed_fstring_passes(self):
        code = 'name = Label("x")\ny = UnstructuredText(f"Hello {name}")'
        result = check_string_declarations(code)
        assert result.passed

    def test_typed_fstring_with_literal_fragment(self):
        # "Hello " inside f-string is exempt since parent is JoinedStr
        code = 'x = Label("val")\ny = Label(f"prefix-{x}")'
        result = check_string_declarations(code)
        assert result.passed


class TestMixedTypedUntyped:
    """Mix of typed and untyped strings — violations only for untyped."""

    def test_some_typed_some_not(self):
        code = 'x = Label("ok")\ny = "bare"\nz = Email("a@b.com")'
        result = check_string_declarations(code)
        assert not result.passed
        assert len(result.violations) == 1
        assert "'bare'" in result.violations[0]


class TestEdgeCases:
    def test_no_strings(self):
        code = "x = 1\ny = x + 2"
        result = check_string_declarations(code)
        assert result.passed

    def test_empty_code(self):
        result = check_string_declarations("")
        assert result.passed

    def test_syntax_error_passes(self):
        result = check_string_declarations("def ")
        assert result.passed

    def test_empty_typed_string(self):
        code = 'x = Label("")'
        result = check_string_declarations(code)
        assert result.passed

    def test_numeric_code_only(self):
        code = "x = 42\ny = 3.14"
        result = check_string_declarations(code)
        assert result.passed

    def test_all_typed_complex(self):
        code = """
x = Label("status")
y = URL("https://example.com")
z = Email("user@example.com")
w = WorkspacePath("output/result.json")
q = SQL("SELECT * FROM users WHERE id = ?")
j = JSON('{"key": "value"}')
t = UnstructuredText("Free form text here")
"""
        result = check_string_declarations(code)
        assert result.passed


# ---------------------------------------------------------------------------
# extract_unstructured_text_values
# ---------------------------------------------------------------------------

class TestExtractUnstructuredTextValues:
    """Tests for the AST extractor that collects UnstructuredText literal values."""

    def test_single_literal(self):
        code = 'msg = UnstructuredText("hello world")'
        assert extract_unstructured_text_values(code) == ["hello world"]

    def test_multiple_calls(self):
        code = (
            'a = UnstructuredText("first message")\n'
            'b = UnstructuredText("second message")\n'
        )
        result = extract_unstructured_text_values(code)
        assert result == ["first message", "second message"]

    def test_skips_non_literal_argument(self):
        # Variable argument — value not known at review time
        code = "msg = UnstructuredText(user_input)"
        assert extract_unstructured_text_values(code) == []

    def test_skips_other_type_constructors(self):
        code = 'x = URL("https://example.com")\ny = Label("ok")\n'
        assert extract_unstructured_text_values(code) == []

    def test_mixed_typed_strings(self):
        code = (
            'a = URL("https://example.com")\n'
            'b = UnstructuredText("free text here")\n'
            'c = Label("done")\n'
        )
        assert extract_unstructured_text_values(code) == ["free text here"]

    def test_unparseable_code_returns_empty(self):
        assert extract_unstructured_text_values("def :") == []

    def test_empty_code_returns_empty(self):
        assert extract_unstructured_text_values("") == []

    def test_no_unstructured_text_calls(self):
        code = "x = 1\ny = x + 2\n"
        assert extract_unstructured_text_values(code) == []

    def test_empty_string_argument(self):
        code = 'msg = UnstructuredText("")'
        assert extract_unstructured_text_values(code) == [""]

    def test_multiline_string(self):
        code = 'msg = UnstructuredText("line one\\nline two")'
        result = extract_unstructured_text_values(code)
        assert len(result) == 1
        assert "line one" in result[0]

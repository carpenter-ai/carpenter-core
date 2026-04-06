"""Tests for SecurityType declarations (carpenter_tools/declarations.py)."""

import pytest

from carpenter_tools.declarations import (
    SecurityType, Label, Email, URL, WorkspacePath, SQL, JSON, UnstructuredText,
)


class TestSecurityTypeSubclassesStr:
    """SecurityType subclasses str for seamless compatibility."""

    def test_label_isinstance_str(self):
        assert isinstance(Label("status"), str)

    def test_email_isinstance_str(self):
        assert isinstance(Email("a@b.com"), str)

    def test_url_isinstance_str(self):
        assert isinstance(URL("https://example.com"), str)

    def test_workspace_path_isinstance_str(self):
        assert isinstance(WorkspacePath("results.json"), str)

    def test_sql_isinstance_str(self):
        assert isinstance(SQL("SELECT 1"), str)

    def test_json_isinstance_str(self):
        assert isinstance(JSON('{"key": "value"}'), str)

    def test_unstructured_text_isinstance_str(self):
        assert isinstance(UnstructuredText("hello world"), str)

    def test_isinstance_security_type(self):
        assert isinstance(Label("x"), SecurityType)


class TestStringOperations:
    """SecurityType values work like strings."""

    def test_equality_with_str(self):
        assert Label("hello") == "hello"

    def test_str_value(self):
        assert str(Label("hello")) == "hello"

    def test_concatenation(self):
        result = Label("hello") + " world"
        assert result == "hello world"

    def test_dict_key_usage(self):
        d = {Label("key"): "value"}
        assert d["key"] == "value"
        assert d[Label("key")] == "value"

    def test_in_check(self):
        assert "ell" in Label("hello")

    def test_len(self):
        assert len(Label("abc")) == 3

    def test_upper(self):
        assert Label("hello").upper() == "HELLO"

    def test_startswith(self):
        assert Label("hello").startswith("hel")


class TestRepr:
    """__repr__ shows the type name."""

    def test_label_repr(self):
        assert repr(Label("x")) == "Label('x')"

    def test_email_repr(self):
        assert repr(Email("a@b.com")) == "Email('a@b.com')"

    def test_url_repr(self):
        assert repr(URL("https://x.com")) == "URL('https://x.com')"

    def test_workspace_path_repr(self):
        assert repr(WorkspacePath("f.txt")) == "WorkspacePath('f.txt')"

    def test_sql_repr(self):
        assert repr(SQL("SELECT 1")) == "SQL('SELECT 1')"

    def test_json_repr(self):
        assert repr(JSON("{}")) == "JSON('{}')"

    def test_unstructured_text_repr(self):
        assert repr(UnstructuredText("hi")) == "UnstructuredText('hi')"


class TestConstructorWrapping:
    """All 7 types can be constructed."""

    def test_label(self):
        v = Label("my-key")
        assert v == "my-key"

    def test_email(self):
        v = Email("user@example.com")
        assert v == "user@example.com"

    def test_url(self):
        v = URL("https://example.com/path")
        assert v == "https://example.com/path"

    def test_workspace_path(self):
        v = WorkspacePath("subdir/file.txt")
        assert v == "subdir/file.txt"

    def test_sql(self):
        v = SQL("SELECT * FROM users WHERE id = ?")
        assert v == "SELECT * FROM users WHERE id = ?"

    def test_json(self):
        v = JSON('{"key": "value"}')
        assert v == '{"key": "value"}'

    def test_unstructured_text(self):
        v = UnstructuredText("Free-form text with special chars!@#$")
        assert v == "Free-form text with special chars!@#$"


class TestTypeName:
    """Each type has the correct _type_name for validator dispatch."""

    def test_label_type_name(self):
        assert Label._type_name == "label"

    def test_email_type_name(self):
        assert Email._type_name == "email"

    def test_url_type_name(self):
        assert URL._type_name == "url"

    def test_workspace_path_type_name(self):
        assert WorkspacePath._type_name == "workspace_path"

    def test_sql_type_name(self):
        assert SQL._type_name == "sql"

    def test_json_type_name(self):
        assert JSON._type_name == "json"

    def test_unstructured_text_type_name(self):
        assert UnstructuredText._type_name == "unstructured_text"

"""Tests for static analyzer."""
from carpenter.review.static_analyzer import (
    analyze_file_type,
    validate_syntax,
    extract_comments_and_strings,
    check_import_star,
)


def test_analyze_file_type_basic():
    code = "import os\n\ndef hello():\n    pass\n"
    result = analyze_file_type(code)
    assert result["lines"] == 5
    assert result["has_imports"] is True
    assert result["has_functions"] is True
    assert result["has_classes"] is False

def test_analyze_file_type_class():
    code = "class Foo:\n    pass\n"
    result = analyze_file_type(code)
    assert result["has_classes"] is True

def test_validate_syntax_valid():
    result = validate_syntax("x = 1")
    assert result["valid"] is True
    assert result["errors"] == []

def test_validate_syntax_invalid():
    result = validate_syntax("def :")
    assert result["valid"] is False
    assert len(result["errors"]) > 0

def test_extract_comments():
    code = "# This is a comment\nx = 1  # inline\n"
    result = extract_comments_and_strings(code)
    assert "This is a comment" in result["comments"]

def test_extract_docstrings():
    code = '\"\"\"Module docstring.\"\"\"\ndef foo():\n    \"\"\"Func doc.\"\"\"\n    pass\n'
    result = extract_comments_and_strings(code)
    assert len(result["docstrings"]) >= 1


def test_check_import_star_basic_violation():
    """Test basic wildcard import detection."""
    code = "from os import *"
    result = check_import_star(code)
    assert result["violation"] is True
    assert len(result["findings"]) == 1
    assert result["findings"][0]["line"] == 1
    assert "wildcard" in result["findings"][0]["message"].lower()


def test_check_import_star_relative_imports():
    """Test detection of relative wildcard imports."""
    code = """
from .module import *
from ..parent import *
from ...grandparent import *
"""
    result = check_import_star(code)
    assert result["violation"] is True
    assert len(result["findings"]) == 3
    # Line numbers should be 2, 3, 4 (after blank line)
    lines = [f["line"] for f in result["findings"]]
    assert 2 in lines
    assert 3 in lines
    assert 4 in lines


def test_check_import_star_multiple_violations():
    """Test multiple wildcard imports in same file."""
    code = """
from sys import *
from os import *
import json
from pathlib import *
"""
    result = check_import_star(code)
    assert result["violation"] is True
    assert len(result["findings"]) == 3
    # Should detect lines 2, 3, and 5


def test_check_import_star_no_violation():
    """Test that explicit imports are allowed."""
    code = """
import os
from sys import argv, exit
from pathlib import Path
from .module import specific_function
"""
    result = check_import_star(code)
    assert result["violation"] is False
    assert len(result["findings"]) == 0


def test_check_import_star_in_string():
    """Test that import * in strings is not flagged."""
    code = """
comment = "This code used to do: from os import *"
x = 'from sys import *'
"""
    result = check_import_star(code)
    # These are in strings, not actual imports - should not be flagged
    # The regex checks for actual import statements
    assert result["violation"] is False


def test_check_import_star_with_whitespace():
    """Test detection with various whitespace patterns."""
    code = """
from   os   import   *
  from sys import *
	from pathlib import *
"""
    result = check_import_star(code)
    assert result["violation"] is True
    assert len(result["findings"]) == 3



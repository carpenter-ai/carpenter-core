"""Tests for carpenter.core.code_manager -- save_code and ast_check only.

The execute() function uses the RestrictedPython executor exclusively.
Tests for the old subprocess executor path have been removed.
"""

import os
import re

import pytest

from carpenter.core import code_manager
from carpenter.db import get_db
from carpenter import config


def test_save_code_creates_file():
    """save_code writes the code to disk and the file exists."""
    result = code_manager.save_code("print('hello')\n", source="agent")
    assert os.path.isfile(result["file_path"])
    with open(result["file_path"]) as f:
        assert f.read() == "print('hello')\n"


def test_save_code_tracks_in_db():
    """save_code inserts a row into code_files with correct fields."""
    result = code_manager.save_code("x = 1\n", source="user", arc_id=None, name="demo")
    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM code_files WHERE id = ?", (result["code_file_id"],)
        ).fetchone()
        assert row is not None
        assert row["file_path"] == result["file_path"]
        assert row["source"] == "user"
        assert row["arc_id"] is None
        assert "demo" in row["file_path"]
    finally:
        db.close()


def test_save_code_date_partitioned():
    """save_code uses YYYY/MM/DD directory structure."""
    result = code_manager.save_code("pass\n", source="agent")
    path = result["file_path"]
    # Path should contain a date-partitioned segment like /2026/03/09/
    assert re.search(r"/\d{4}/\d{2}/\d{2}/", path), (
        f"Expected date-partitioned path, got: {path}"
    )


def test_save_code_sequential_numbering():
    """Saving two files produces incrementing sequence numbers in filenames."""
    r1 = code_manager.save_code("a = 1\n", source="agent", name="first")
    r2 = code_manager.save_code("b = 2\n", source="agent", name="second")

    base1 = os.path.basename(r1["file_path"])
    base2 = os.path.basename(r2["file_path"])

    seq1 = int(base1.split("_")[0])
    seq2 = int(base2.split("_")[0])
    assert seq2 > seq1


def test_ast_check_clean_code():
    """Clean code produces no findings."""
    findings = code_manager.ast_check("x = 1\nprint(x)\n")
    assert findings == []


def test_ast_check_os_system():
    """os.system() call is detected."""
    code = "import os\nos.system('ls')\n"
    findings = code_manager.ast_check(code)
    assert len(findings) == 1
    assert findings[0]["level"] == "warning"
    assert "os.system()" in findings[0]["description"]
    assert findings[0]["line"] == 2


def test_ast_check_eval_exec():
    """eval() and exec() calls are both detected."""
    code = "eval('1+1')\nexec('x=1')\n"
    findings = code_manager.ast_check(code)
    assert len(findings) == 2
    descriptions = {f["description"] for f in findings}
    assert "eval() call" in descriptions
    assert "exec() call" in descriptions
    assert all(f["level"] == "flag" for f in findings)


def test_ast_check_subprocess_shell():
    """subprocess.run/call/Popen with shell=True are detected."""
    code = (
        "import subprocess\n"
        "subprocess.run('ls', shell=True)\n"
        "subprocess.call('ls', shell=True)\n"
        "subprocess.Popen('ls', shell=True)\n"
    )
    findings = code_manager.ast_check(code)
    assert len(findings) == 3
    assert all(f["level"] == "warning" for f in findings)
    descs = [f["description"] for f in findings]
    assert "subprocess.run() with shell=True" in descs
    assert "subprocess.call() with shell=True" in descs
    assert "subprocess.Popen() with shell=True" in descs


def test_ast_check_import_call():
    """__import__() call is detected."""
    code = "__import__('os')\n"
    findings = code_manager.ast_check(code)
    assert len(findings) == 1
    assert findings[0]["level"] == "flag"
    assert "__import__()" in findings[0]["description"]


def test_ast_check_syntax_error():
    """Unparseable code returns a single error-level finding."""
    code = "def f(\n"
    findings = code_manager.ast_check(code)
    assert len(findings) == 1
    assert findings[0]["level"] == "error"
    assert "Syntax error" in findings[0]["description"]


# --- _sanitize_filename tests ---


class TestSanitizeFilename:
    """Test centralized filename sanitization in code_manager."""

    def test_path_traversal_blocked(self):
        assert code_manager._sanitize_filename("../../etc/passwd") == "etc_passwd"

    def test_slash_replaced(self):
        assert "/" not in code_manager._sanitize_filename("foo/bar/baz")

    def test_backslash_replaced(self):
        assert "\\" not in code_manager._sanitize_filename("foo\\bar\\baz")

    def test_dotdot_stripped(self):
        result = code_manager._sanitize_filename("..hidden..file")
        assert ".." not in result

    def test_leading_dot_stripped(self):
        result = code_manager._sanitize_filename(".secret")
        assert not result.startswith(".")

    def test_special_chars_replaced(self):
        result = code_manager._sanitize_filename("hello world!@#$%")
        assert all(c.isalnum() or c in ("_", "-") for c in result)

    def test_empty_fallback(self):
        assert code_manager._sanitize_filename("") == "script"
        assert code_manager._sanitize_filename("///") == "script"
        assert code_manager._sanitize_filename("..") == "script"

    def test_truncation(self):
        long_name = "a" * 100
        assert len(code_manager._sanitize_filename(long_name)) <= 50

    def test_normal_name_unchanged(self):
        assert code_manager._sanitize_filename("my_script") == "my_script"

    def test_collapsed_underscores(self):
        result = code_manager._sanitize_filename("foo   bar   baz")
        assert "__" not in result

    def test_save_code_sanitizes_name(self):
        """save_code applies filename sanitization to the name parameter."""
        result = code_manager.save_code(
            "x = 1\n", source="agent", name="../../etc/passwd"
        )
        basename = os.path.basename(result["file_path"])
        assert ".." not in basename
        assert "/" not in basename


def test_execute_creates_session():
    """execute() creates an execution session in the database."""
    result = code_manager.save_code("x = 1\n", source="agent")

    # Mark as approved
    db = get_db()
    try:
        db.execute(
            "UPDATE code_files SET review_status = 'approved' WHERE id = ?",
            (result["code_file_id"],),
        )
        db.commit()
    finally:
        db.close()

    exec_result = code_manager.execute(result["code_file_id"])
    assert exec_result["execution_id"] is not None
    assert exec_result["execution_status"] in ("success", "failed")

    # Verify an execution session was created
    db = get_db()
    try:
        row = db.execute(
            "SELECT session_id, reviewed FROM execution_sessions "
            "WHERE execution_id = ?",
            (exec_result["execution_id"],),
        ).fetchone()
        assert row is not None
        assert row["session_id"] is not None
        assert row["reviewed"] == 1  # approved code -> reviewed=True
    finally:
        db.close()


def test_execute_uses_restricted_executor_type():
    """execute() always records executor_type='restricted'."""
    result = code_manager.save_code("x = 1\n", source="agent")

    db = get_db()
    try:
        db.execute(
            "UPDATE code_files SET review_status = 'approved' WHERE id = ?",
            (result["code_file_id"],),
        )
        db.commit()
    finally:
        db.close()

    exec_result = code_manager.execute(result["code_file_id"])

    db = get_db()
    try:
        row = db.execute(
            "SELECT executor_type FROM code_executions WHERE id = ?",
            (exec_result["execution_id"],),
        ).fetchone()
        assert row["executor_type"] == "restricted"
    finally:
        db.close()

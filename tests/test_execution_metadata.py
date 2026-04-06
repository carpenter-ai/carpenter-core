"""Tests for execution metadata: process-ID recording and executor selection.

Replaces the old early-PID tests that were specific to subprocess executors
and the tiny test_executor_registration.py module.
"""

import os

import pytest

import carpenter.executor as executor_mod


# -- Process-ID recording ---------------------------------------------------


def test_execution_records_process_id(test_db):
    """execute() records a process_id in the code_executions table."""
    from carpenter.db import get_db
    from carpenter.core.code_manager import save_code, execute

    # Save a simple script
    code = "x = 1"
    result = save_code(code, source="test", name="process_id_test")
    code_file_id = result["code_file_id"]

    # Mark it as approved
    db = get_db()
    db.execute("UPDATE code_files SET review_status='approved' WHERE id=?", (code_file_id,))
    db.commit()
    db.close()

    # Execute it
    exec_result = execute(code_file_id)
    assert exec_result["execution_status"] == "success"

    # Verify process_id and executor_type are in the DB
    db = get_db()
    row = db.execute(
        "SELECT pid_or_container, executor_type "
        "FROM code_executions WHERE id=?",
        (exec_result["execution_id"],),
    ).fetchone()
    db.close()

    assert row["pid_or_container"] is not None
    assert row["pid_or_container"] != ""
    assert "restricted-thread" in row["pid_or_container"]
    assert row["executor_type"] == "restricted"


# -- Executor registration / selection --------------------------------------


class TestExecutorRegistration:
    """Tests for executor module (get_executor)."""

    def test_get_executor_returns_restricted(self):
        """get_executor() returns a RestrictedExecutor."""
        result = executor_mod.get_executor("restricted")
        assert result.name == "restricted"

    def test_get_executor_default_is_restricted(self):
        """get_executor(None) returns a RestrictedExecutor."""
        result = executor_mod.get_executor()
        assert result.name == "restricted"

    def test_get_executor_unknown_type_raises(self):
        """get_executor('docker') raises ValueError since it was removed."""
        with pytest.raises(ValueError, match="Unknown executor_type"):
            executor_mod.get_executor("docker")

        with pytest.raises(ValueError, match="Unknown executor_type"):
            executor_mod.get_executor("subprocess_basic")

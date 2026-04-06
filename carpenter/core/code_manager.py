"""Code manager for Carpenter.

Manages saving, AST-checking, and executing agent-generated Python code.
Code files are saved to date-partitioned directories and tracked in the database.
AST analysis flags suspicious patterns before execution.
"""

import ast
import logging
import os
import re as _re
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta

from .. import config
from ..db import get_db, db_transaction

logger = logging.getLogger(__name__)


def _sanitize_filename(name: str) -> str:
    """Sanitize a string for safe use as part of a filename.

    Strips path separators, dotdot sequences, leading dots, and
    non-alphanumeric characters.  Collapses runs of underscores and
    truncates to 50 characters.  Returns ``"script"`` for empty input.
    """
    name = name.replace("/", "_").replace("\\", "_")
    name = name.replace("..", "").lstrip(".")
    name = _re.sub(r"[^a-zA-Z0-9_\-]", "_", name)
    name = _re.sub(r"_+", "_", name).strip("_")
    return name[:50] or "script"


def save_code(
    code: str,
    source: str,
    arc_id: int | None = None,
    name: str = "script",
) -> dict:
    """Save Python code to a date-partitioned file and track it in the database.

    Args:
        code: The Python source code to save.
        source: Origin of the code (e.g. "agent", "user", "template").
        arc_id: Optional arc ID to associate with the code file.
        name: Base name for the file (default "script").

    Returns:
        Dict with ``code_file_id`` and ``file_path``.
    """
    # Sanitize name and source to prevent path traversal / invalid filenames
    name = _sanitize_filename(name)
    source = _sanitize_filename(source)

    now = datetime.now(timezone.utc)
    date_dir = os.path.join(
        config.CONFIG["code_dir"],
        now.strftime("%Y"),
        now.strftime("%m"),
        now.strftime("%d"),
    )
    os.makedirs(date_dir, exist_ok=True)

    # Insert the database record first to get the id for the sequence number.
    # Use a placeholder path; we will update it after we know the id.
    with db_transaction() as db:
        cursor = db.execute(
            "INSERT INTO code_files (file_path, source, arc_id) VALUES (?, ?, ?)",
            ("__pending__", source, arc_id),
        )
        code_file_id = cursor.lastrowid

        sequence = f"{code_file_id:06d}"
        filename = f"{sequence}_{source}_{name}.py"
        file_path = os.path.join(date_dir, filename)

        # Update the record with the real path.
        db.execute(
            "UPDATE code_files SET file_path = ? WHERE id = ?",
            (file_path, code_file_id),
        )

    # Write the file to disk.
    with open(file_path, "w") as f:
        f.write(code)

    return {"code_file_id": code_file_id, "file_path": file_path}


def ast_check(code: str) -> list[dict]:
    """Perform AST analysis to flag suspicious patterns in Python code.

    Args:
        code: Python source code to analyse.

    Returns:
        List of finding dicts, each with ``level``, ``line``, and ``description``.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [
            {
                "level": "error",
                "line": exc.lineno or 0,
                "description": f"Syntax error: {exc.msg}",
            }
        ]

    findings: list[dict] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func

        # Detect bare calls: eval(), exec(), __import__()
        if isinstance(func, ast.Name):
            if func.id in ("eval", "exec"):
                findings.append({
                    "level": "flag",
                    "line": node.lineno,
                    "description": f"{func.id}() call",
                })
            elif func.id == "__import__":
                findings.append({
                    "level": "flag",
                    "line": node.lineno,
                    "description": "__import__() call",
                })

        # Detect attribute calls: os.system(), subprocess.*()
        if isinstance(func, ast.Attribute):
            # os.system()
            if (
                isinstance(func.value, ast.Name)
                and func.value.id == "os"
                and func.attr == "system"
            ):
                findings.append({
                    "level": "warning",
                    "line": node.lineno,
                    "description": "os.system() call",
                })

            # subprocess.call/run/Popen with shell=True
            if (
                isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
                and func.attr in ("call", "run", "Popen")
            ):
                for keyword in node.keywords:
                    if keyword.arg == "shell":
                        # Check if the value is True
                        if isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                            findings.append({
                                "level": "warning",
                                "line": node.lineno,
                                "description": f"subprocess.{func.attr}() with shell=True",
                            })

    return findings


def _execute_restricted(
    file_path: str,
    log_file: str,
    *,
    session_id: str,
    conversation_id: int | None,
    arc_id: int | None,
    execution_context: str,
    execution_id: int,
) -> dict:
    """Execute code using the in-process RestrictedPython executor.

    Reads the source file, builds a dispatch bridge with the execution
    context, and runs the code in a restricted sandbox with threading.

    Returns:
        Dict matching the ExecutorResult shape: exit_code, status,
        process_id, log_file.
    """
    from ..executor.restricted import RestrictedExecutor
    from ..executor.dispatch_bridge import make_tool_handler

    # Read source code from file
    try:
        with open(file_path) as f:
            source_code = f.read()
    except OSError as exc:
        return {
            "exit_code": -1,
            "status": "error",
            "process_id": "",
            "log_file": log_file,
        }

    # Build tool handler with execution context
    tool_handler = make_tool_handler(
        session_id=session_id,
        conversation_id=conversation_id,
        arc_id=arc_id,
        execution_context=execution_context,
    )

    executor = RestrictedExecutor(tool_handler=tool_handler)
    timeout = config.CONFIG.get("execution_timeout", 300)
    result = executor.execute(source_code, timeout=float(timeout))

    # Write output to log file
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    try:
        with open(log_file, "w") as f:
            if result.output:
                f.write(result.output)
            if result.error:
                f.write(f"\n[ERROR]\n{result.error}\n")
            if result.timed_out:
                f.write(f"\n[TIMEOUT] Execution terminated after {timeout}s\n")
    except OSError:
        logger.debug("Failed to write execution log file", exc_info=True)

    # Map RestrictedExecutor result to ExecutorResult shape
    if result.timed_out:
        status = "timed_out"
    elif result.exit_code != 0:
        status = "failed"
    else:
        status = "success"

    return {
        "exit_code": result.exit_code,
        "status": status,
        "process_id": f"restricted-thread-{execution_id}",
        "log_file": log_file,
    }


def execute(code_file_id: int, *,
            conversation_id: int | None = None,
            arc_id: int | None = None,
            execution_context: str = "reviewed") -> dict:
    """Execute a saved code file.

    Looks up the code file in the database, dispatches to the configured
    executor, and records the execution.

    Args:
        code_file_id: ID of the code file to execute.
        conversation_id: Conversation context for dispatch bridge.
        arc_id: Arc context for dispatch bridge.
        execution_context: Distinguishes the execution origin. ``"reviewed"``
            for chat agent submit_code (allowed to call messaging tools),
            ``"arc-step"`` for arc dispatch (messaging blocked).

    Returns:
        Dict with ``execution_id``, ``exit_code``, ``execution_status``,
        and ``log_file``.

    Raises:
        ValueError: If the code_file_id is not found.
    """
    # Generate execution session ID
    session_id = str(uuid.uuid4())

    with db_transaction() as db:
        # Look up code file and its review status
        row = db.execute(
            "SELECT file_path, review_status FROM code_files WHERE id = ?",
            (code_file_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Code file {code_file_id} not found")

        file_path = row["file_path"]
        review_status = row["review_status"]
        is_reviewed = review_status == "approved"

        # Create execution record before launching subprocess
        cursor = db.execute(
            "INSERT INTO code_executions "
            "(code_file_id, execution_status, started_at) "
            "VALUES (?, 'running', ?)",
            (code_file_id, datetime.now(timezone.utc).isoformat())
        )
        execution_id = cursor.lastrowid

        # Register execution session for callback authentication
        expiry_hours = config.CONFIG.get("execution_session_expiry_hours", 1)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)
        db.execute(
            "INSERT INTO execution_sessions "
            "(session_id, code_file_id, execution_id, reviewed, conversation_id, "
            "execution_context, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, code_file_id, execution_id, is_reviewed,
             conversation_id, execution_context, expires_at.isoformat())
        )

    # Compute log file path (mirrors code file path but in log_dir)
    log_dir = config.CONFIG["log_dir"]
    code_dir = config.CONFIG["code_dir"]
    relative = os.path.relpath(file_path, code_dir)
    log_file = os.path.join(log_dir, os.path.splitext(relative)[0] + ".log")

    # Execute using the in-process RestrictedPython executor
    exec_result = _execute_restricted(
        file_path, log_file,
        session_id=session_id,
        conversation_id=conversation_id,
        arc_id=arc_id,
        execution_context=execution_context,
        execution_id=execution_id,
    )

    # Map executor result to execution record
    execution_status = exec_result["status"]  # success, failed, timed_out, error

    # Update the execution record with completion details
    with db_transaction() as db:
        db.execute(
            "UPDATE code_executions SET "
            "execution_status = ?, exit_code = ?, executor_type = ?, "
            "pid_or_container = ?, log_file = ?, completed_at = ? "
            "WHERE id = ?",
            (
                execution_status,
                exec_result["exit_code"],
                "restricted",
                exec_result.get("process_id", ""),
                exec_result.get("log_file"),
                datetime.now(timezone.utc).isoformat(),
                execution_id,
            ),
        )

    # Update ancestor performance counters (descendant_executions)
    if arc_id is not None:
        try:
            from .arcs import manager as _am
            _am.increment_ancestor_executions(arc_id)
        except (ImportError, sqlite3.Error, ValueError) as _exc:
            logger.debug("Failed to update ancestor execution counters for arc %s", arc_id, exc_info=True)

    return {
        "execution_id": execution_id,
        "exit_code": exec_result["exit_code"],
        "execution_status": execution_status,
        "log_file": exec_result.get("log_file"),
    }

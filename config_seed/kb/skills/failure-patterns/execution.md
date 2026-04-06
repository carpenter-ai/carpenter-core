# Execution Failures

Failures that occur when submitted code is executed by the code manager.

---

## Execution Timeout

**Symptoms**: Execution status is `timeout`. No output or partial output in log file. The `code_executions` record shows `execution_status = 'timeout'`.

**Root cause**: The subprocess executor enforces a timeout (default varies by coding agent profile, typically 300s for builtin). Long-running operations -- large file I/O, expensive computations, blocking network calls without timeouts, infinite loops -- exceed the limit.

**Escape**:
1. Check the log file for partial output to identify where execution stalled.
2. Break the work into smaller steps. Submit code that does one operation at a time rather than a monolithic script.
3. For network operations, always set explicit timeouts on requests (e.g., `requests.get(url, timeout=30)`).
4. For file operations on large datasets, process in chunks and report progress via state callbacks.
5. If the task genuinely requires more time, inform the user and suggest they increase the timeout in the coding agent profile configuration.

**Prevention**: Always include timeouts on blocking I/O. Prefer streaming/chunked processing for large data. Avoid unbounded loops.

**Escalation**: If the task fundamentally cannot complete within the timeout and the user cannot change the configuration, explain the limitation and propose an alternative approach (e.g., breaking into multiple sequential arcs).

---

## Out of Memory (OOM)

**Symptoms**: Execution status is `failed`. Log file may show `MemoryError` or the process may be killed by the OS with no Python traceback (exit code 137 on Linux = SIGKILL from OOM killer).

**Root cause**: The submitted code allocates more memory than the system can provide. Common triggers: loading entire large files into memory, creating very large data structures, unbounded list accumulation in loops.

**Escape**:
1. If exit code is 137 with no traceback, this is almost certainly OOM.
2. Rewrite the code to process data in streaming fashion: read line-by-line, use generators, process in batches.
3. For file operations, use `with open(...) as f: for line in f:` instead of `f.read()` or `f.readlines()`.
4. If building a large result, write intermediate results to disk or state rather than accumulating in memory.

**Prevention**: Never call `.read()` on files of unknown size. Use generators and iterators. Set explicit limits on collection sizes.

**Escalation**: If the data volume genuinely requires more memory than available, inform the user. The task may need to be split into smaller pieces or run on a system with more resources.

---

## Permission Denied

**Symptoms**: Execution log shows `PermissionError` or `[Errno 13] Permission denied`. Code cannot read, write, or execute a file or directory.

**Root cause**: The executor subprocess runs with the platform's user permissions. It cannot access files owned by other users, write to read-only directories, or execute files without execute permission. The sandbox configuration may further restrict accessible paths.

**Escape**:
1. Check the exact path in the error message.
2. For reads: verify the file exists and the platform user has read access. Use `read_file` tool first to confirm readability.
3. For writes: verify the target directory exists and is writable. The safe directories are those under `base_dir` (data, logs, code, workspaces). Writing outside these directories will usually fail.
4. For the sandbox specifically: check `config.CONFIG["sandbox"]["allowed_write_dirs"]`. If empty, only config-derived paths are writable.
5. If the user is asking you to modify files in the platform source directory, use the coding-change workflow (workspace + diff review) instead of direct file writes.

**Prevention**: Always write to paths under the configured data directories. Do not attempt to write to `/tmp`, the home directory root, or system paths unless explicitly configured.

**Escalation**: If the user needs access to paths outside the platform's configured directories, they need to adjust the sandbox configuration or file permissions.

---

## Import Error

**Symptoms**: Execution log shows `ModuleNotFoundError: No module named 'xxx'` or `ImportError: cannot import name 'xxx'`.

**Root cause**: The submitted code imports a Python package that is not installed in the platform's Python environment. The executor runs in the same Python environment as the platform.

**Escape**:
1. Check which module is missing from the error message.
2. If it is a standard library module, verify the Python version supports it.
3. If it is a third-party package, inform the user that it needs to be installed (`pip install xxx`). Do NOT submit code that runs `pip install` -- package installation is a human decision.
4. If the import is from `carpenter_tools`, verify you are using the correct submodule path: `carpenter_tools.read.*` for read-only operations, `carpenter_tools.act.*` for actions.
5. Common mistake: importing `carpenter_tools.act.web` when `requests` or `httpx` is not installed. The web tool module itself may import fine, but the underlying HTTP library may be missing.

**Prevention**: Only import modules you know are available. When uncertain, wrap the import in a try/except and report the missing dependency clearly. Prefer standard library modules when possible.

**Escalation**: If the task requires a third-party package that is not installed, tell the user which package is needed and why.

---

## Callback Unreachable

**Symptoms**: Execution log shows `ConnectionRefusedError`, `ConnectionError`, or `httpx.ConnectError` when the executed code tries to call back to the platform (e.g., via `carpenter_tools.act.state.set_state()`).

**Root cause**: The callback mechanism requires the platform HTTP server to be reachable at `http://localhost:{port}` (default 7842). If the server is not running, restarting, or the port has changed, callbacks fail. The `CALLBACK_URL` and `CALLBACK_TOKEN` environment variables are injected by the code manager; if they are missing or wrong, callbacks fail.

**Escape**:
1. This usually indicates a transient server issue. Retry the execution.
2. If the error persists, check whether the platform server is running (`GET /` should respond).
3. Verify the port in `config.CONFIG["port"]` matches the running server.
4. If the code does not actually need callbacks (read-only operations), rewrite it to use direct Python instead of callback tools.
5. Read-only callbacks (`carpenter_tools.read.*`) do not require an execution session. Action callbacks (`carpenter_tools.act.*`) require a valid `CARPENTER_EXECUTION_SESSION`.

**Prevention**: For operations that do not modify platform state, prefer read-only approaches (direct file reading, in-process computation) over callback-based tools.

**Escalation**: If the callback URL is fundamentally unreachable (e.g., platform running in a different network namespace), the execution architecture may need reconfiguration by the user.

---

## Execution Session Expired or Invalid

**Symptoms**: Callback returns HTTP 403 with a message about invalid or expired execution session. The executed code's action callbacks are rejected.

**Root cause**: Execution sessions are created by the code manager with a limited lifetime (default: 1 hour, configurable via `execution_session_expiry_hours`). If execution takes longer than this, or if the session ID is somehow corrupted, action callbacks are rejected. Read-only callbacks are not affected.

**Escape**:
1. If the execution was long-running, the session may have expired. Re-execute the code (a new session will be created).
2. Check the `execution_sessions` table: `SELECT * FROM execution_sessions WHERE session_id = ?` to verify the session exists and has not expired.
3. If the session exists but `reviewed = 0`, the code was not approved by the review pipeline, and action callbacks are blocked by design.

**Prevention**: Keep execution times well under the session expiry limit. For long-running tasks, break into smaller steps.

**Escalation**: If tasks genuinely need longer sessions, the user can increase `execution_session_expiry_hours` in the configuration.

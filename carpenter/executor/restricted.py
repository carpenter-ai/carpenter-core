"""Restricted executor -- runs code in-process using RestrictedPython + threading.

Replaces the subprocess/HTTP callback architecture with:
- RestrictedPython for code sandboxing (no imports, no open, no eval/exec)
- Threading for universal platform support (Linux + Android)
- JSON-serialized queue boundary for tool dispatch (no object reference leakage)
- PyThreadState_SetAsyncExc for cooperative timeout

The executed code receives a single injected function ``dispatch(tool_name, params)``
which communicates with the main thread over a queue pair.  All data crossing the
boundary is JSON-serialized to prevent object reference leakage.
"""

import ctypes
import json
import logging
import queue
import threading
import traceback
from typing import Any, Callable

from RestrictedPython import compile_restricted, safe_builtins, PrintCollector
from RestrictedPython.Guards import (
    full_write_guard,
    guarded_unpack_sequence,
    safer_getattr,
)
from RestrictedPython.Eval import default_guarded_getitem, default_guarded_getiter

logger = logging.getLogger(__name__)

# Sentinel used to signal the dispatch loop to shut down.
_DISPATCH_SHUTDOWN = "__shutdown__"

# Extra builtins beyond RestrictedPython's safe_builtins.
# These are read-only functions that don't provide escape paths.
_EXTRA_BUILTINS = {
    "all": all,
    "any": any,
    "dict": dict,
    "enumerate": enumerate,
    "filter": filter,
    "frozenset": frozenset,
    "iter": iter,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "next": next,
    "reversed": reversed,
    "set": set,
    "sum": sum,
    "type": type,
    "vars": None,  # blocked explicitly
}


def _inplacevar_(op, x, y):
    """Handle augmented assignment operators (+=, -=, *=, etc.).

    RestrictedPython transforms ``x += y`` into ``x = _inplacevar_('+=', x, y)``
    so that write guards can be applied.
    """
    if op == "+=":
        return x + y
    elif op == "-=":
        return x - y
    elif op == "*=":
        return x * y
    elif op == "/=":
        return x / y
    elif op == "//=":
        return x // y
    elif op == "%=":
        return x % y
    elif op == "**=":
        return x ** y
    elif op == "|=":
        return x | y
    elif op == "&=":
        return x & y
    elif op == "^=":
        return x ^ y
    elif op == "<<=":
        return x << y
    elif op == ">>=":
        return x >> y
    raise NotImplementedError(f"Unsupported in-place operator: {op}")


class ExecutionResult:
    """Result of a restricted code execution."""

    __slots__ = ("output", "error", "dispatch_log", "timed_out", "exit_code")

    def __init__(
        self,
        *,
        output: str = "",
        error: str = "",
        dispatch_log: list[dict] | None = None,
        timed_out: bool = False,
        exit_code: int = 0,
    ):
        self.output = output
        self.error = error
        self.dispatch_log = dispatch_log or []
        self.timed_out = timed_out
        self.exit_code = exit_code


def _make_dispatch_fn(
    request_queue: queue.Queue,
    response_queue: queue.Queue,
    allowed_tools: frozenset[str] | None,
    dispatch_log: list[dict],
) -> Callable:
    """Build the ``dispatch(tool_name, params)`` closure injected into user code.

    The function JSON-serializes params onto *request_queue*, blocks on
    *response_queue* for the result, and JSON-deserializes it back.
    This ensures no live Python objects leak across the boundary.

    Args:
        request_queue: Queue for sending requests to the dispatcher.
        response_queue: Queue for receiving results from the dispatcher.
        allowed_tools: If not None, restrict dispatch to these tool names.
        dispatch_log: Mutable list where each dispatch call is logged.
    """

    def dispatch(tool_name: str, params: dict | None = None) -> Any:
        if params is None:
            params = {}
        if not isinstance(tool_name, str):
            raise TypeError(f"tool_name must be a string, got {type(tool_name).__name__}")
        if not isinstance(params, dict):
            raise TypeError(f"params must be a dict, got {type(params).__name__}")

        # Block shutdown sentinel from user code
        if tool_name == _DISPATCH_SHUTDOWN:
            raise PermissionError(
                f"Tool name '{_DISPATCH_SHUTDOWN}' is reserved for internal use"
            )

        # Validate against allowed tools if a whitelist is set
        if allowed_tools is not None and tool_name not in allowed_tools:
            raise PermissionError(
                f"Tool '{tool_name}' is not in the allowed tools list"
            )

        # Serialize to JSON to prevent object reference leakage
        try:
            request_json = json.dumps({
                "tool_name": tool_name,
                "params": params,
            })
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"dispatch params must be JSON-serializable: {exc}"
            ) from exc

        log_entry = {"tool_name": tool_name, "params": params}

        # Send request and wait for response
        request_queue.put(request_json)
        response_json = response_queue.get()

        # Deserialize response
        response = json.loads(response_json)

        if "error" in response:
            log_entry["error"] = response["error"]
            dispatch_log.append(log_entry)
            raise RuntimeError(f"dispatch({tool_name}) failed: {response['error']}")

        result = response.get("result")
        log_entry["result"] = result
        dispatch_log.append(log_entry)
        return result

    return dispatch


def _build_namespace(
    dispatch_fn: Callable,
) -> dict:
    """Build the restricted namespace for code execution.

    Includes safe builtins, RestrictedPython guards, the dispatch function,
    carpenter_tools compatibility shim, and PrintCollector for capturing
    print output.

    RestrictedPython transforms ``print(...)`` into ``_print_(...)`` calls.
    After execution, collected output is retrieved via ``namespace['_print']()``.
    """
    from ._compat import build_compat_namespace

    builtins = dict(safe_builtins)

    # Add useful builtins that safe_builtins omits
    for name, obj in _EXTRA_BUILTINS.items():
        if obj is not None:
            builtins[name] = obj
        else:
            builtins.pop(name, None)

    # Build carpenter_tools compatibility namespace
    compat = build_compat_namespace(dispatch_fn)

    # Provide a controlled __import__ that resolves carpenter_tools
    # sub-modules from the compatibility namespace.  This allows code
    # written for the subprocess executor (``from carpenter_tools.act
    # import arc``) to work unmodified in the restricted sandbox.
    def _restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
        # Only allow importing from the carpenter_tools namespace
        if name == "carpenter_tools" or name.startswith("carpenter_tools."):
            parts = name.split(".")
            obj = compat.get("carpenter_tools")
            if obj is None:
                raise ImportError(f"No module named {name!r}")
            for part in parts[1:]:
                obj = getattr(obj, part, None)
                if obj is None:
                    raise ImportError(f"No module named {name!r}")
            # Handle ``from carpenter_tools.act import arc, messaging``
            if fromlist:
                return obj
            # Handle ``import carpenter_tools`` (return top-level)
            return compat["carpenter_tools"]
        raise ImportError(
            f"Imports are not allowed in the restricted executor. "
            f"Use dispatch() or the pre-imported carpenter_tools modules instead."
        )

    builtins["__import__"] = _restricted_import

    namespace = {
        "__builtins__": builtins,
        "_getattr_": safer_getattr,
        "_getitem_": default_guarded_getitem,
        "_getiter_": default_guarded_getiter,
        "_write_": full_write_guard,
        "_iter_unpack_sequence_": guarded_unpack_sequence,
        "_inplacevar_": _inplacevar_,
        "_print_": PrintCollector,
        "dispatch": dispatch_fn,
    }

    # Also inject pre-imported tool modules directly into the namespace
    # so code can use ``arc.create(...)`` without any import statement.
    namespace.update(compat)

    return namespace


def _terminate_thread(thread: threading.Thread) -> bool:
    """Raise SystemExit in the target thread via PyThreadState_SetAsyncExc.

    Returns True if the exception was set, False if the thread was not found.
    This is cooperative -- the exception fires at the next Python bytecode
    boundary.  Under RestrictedPython with no C extensions, this is prompt.
    """
    if thread.ident is None:
        return False
    tid = ctypes.c_ulong(thread.ident)
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        tid, ctypes.py_object(SystemExit)
    )
    return res == 1


ToolHandler = Callable[[str, dict], Any]
"""Signature for the tool dispatch handler: (tool_name, params) -> result."""


class RestrictedExecutor:
    """Execute code in a RestrictedPython sandbox with threaded dispatch.

    Usage::

        def my_tool_handler(tool_name, params):
            return some_backend.handle(tool_name, params)

        executor = RestrictedExecutor(tool_handler=my_tool_handler)
        result = executor.execute(
            code="x = dispatch('state.get', {'key': 'foo'})",
            timeout=30.0,
        )
    """

    name = "restricted"

    def __init__(
        self,
        *,
        tool_handler: ToolHandler | None = None,
        default_timeout: float = 300.0,
    ):
        """Initialize the restricted executor.

        Args:
            tool_handler: Function called to dispatch tool requests.
                Signature: ``(tool_name: str, params: dict) -> Any``.
                The return value must be JSON-serializable.
                If None, all dispatch calls will fail with an error.
            default_timeout: Default execution timeout in seconds.
        """
        self._tool_handler = tool_handler
        self._default_timeout = default_timeout

    def execute(
        self,
        code: str,
        *,
        allowed_tools: frozenset[str] | list[str] | None = None,
        timeout: float | None = None,
        extra_namespace: dict | None = None,
    ) -> ExecutionResult:
        """Execute restricted Python code in a worker thread.

        Args:
            code: Python source code to execute.
            allowed_tools: If set, restrict dispatch() to these tool names.
                If None, all tools recognized by tool_handler are allowed.
            timeout: Execution timeout in seconds.  None uses default_timeout.
            extra_namespace: Additional names to inject into the namespace.
                These are added after the standard namespace is built.

        Returns:
            ExecutionResult with output, errors, dispatch log, and exit code.
        """
        if timeout is None:
            timeout = self._default_timeout

        if isinstance(allowed_tools, list):
            allowed_tools = frozenset(allowed_tools)

        # Step 1: Compile the code with RestrictedPython
        try:
            compiled = compile_restricted(code, "<user_code>", "exec")
        except SyntaxError as exc:
            return ExecutionResult(
                error=f"SyntaxError: {exc}",
                exit_code=1,
            )

        # Check for compilation errors from RestrictedPython
        # compile_restricted returns a code object or None on error
        if compiled is None:
            return ExecutionResult(
                error="RestrictedPython compilation failed (restricted syntax detected)",
                exit_code=1,
            )

        # Step 2: Set up queues and namespace
        request_queue: queue.Queue = queue.Queue()
        response_queue: queue.Queue = queue.Queue()
        dispatch_log: list[dict] = []

        dispatch_fn = _make_dispatch_fn(
            request_queue, response_queue, allowed_tools, dispatch_log
        )
        namespace = _build_namespace(dispatch_fn)

        if extra_namespace:
            # Apply extra_namespace but protect guard functions from override
            _guard_keys = {
                "_getattr_", "_getitem_", "_getiter_", "_write_",
                "_inplacevar_", "_iter_unpack_sequence_", "__builtins__"
            }
            for key in extra_namespace:
                if key in _guard_keys:
                    raise ValueError(
                        f"extra_namespace cannot override guard function '{key}'"
                    )
            namespace.update(extra_namespace)

        # Step 3: Run code in a worker thread
        exec_error: list[str] = []  # mutable container for thread result
        exec_done = threading.Event()

        def _worker():
            try:
                exec(compiled, namespace)
            except SystemExit:
                exec_error.append("[TIMEOUT] Execution terminated after timeout")
            except Exception:
                exec_error.append(traceback.format_exc())
            finally:
                # Signal the dispatch loop to stop
                request_queue.put(json.dumps({"tool_name": _DISPATCH_SHUTDOWN}))
                exec_done.set()

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()

        # Step 4: Run dispatch loop on the current thread
        timed_out = self._dispatch_loop(
            worker, request_queue, response_queue, timeout, exec_done,
        )

        # Step 5: Wait for worker to finish (with a short grace period)
        worker.join(timeout=2.0)

        # Step 6: Build result
        # RestrictedPython's PrintCollector stores output in _print
        # (created by _print_ = PrintCollector during exec).
        _print_obj = namespace.get("_print")
        if callable(_print_obj):
            try:
                output = _print_obj()
            except Exception:
                output = ""
        else:
            output = ""
        error = exec_error[0] if exec_error else ""
        exit_code = 0
        if timed_out:
            exit_code = -1
        elif error:
            exit_code = 1

        return ExecutionResult(
            output=output,
            error=error,
            dispatch_log=dispatch_log,
            timed_out=timed_out,
            exit_code=exit_code,
        )

    def _dispatch_loop(
        self,
        worker: threading.Thread,
        request_queue: queue.Queue,
        response_queue: queue.Queue,
        timeout: float,
        exec_done: threading.Event,
    ) -> bool:
        """Run the dispatch loop, servicing tool requests from the worker.

        Returns True if the execution timed out.
        """
        import time

        deadline = time.monotonic() + timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Timeout: kill the worker thread
                logger.warning("Restricted execution timed out after %.1fs", timeout)
                _terminate_thread(worker)
                return True

            try:
                request_json = request_queue.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                # Check if the worker finished without a shutdown signal
                if exec_done.is_set():
                    return False
                continue

            request = json.loads(request_json)
            tool_name = request["tool_name"]

            # Check for shutdown sentinel
            if tool_name == _DISPATCH_SHUTDOWN:
                return False

            params = request.get("params", {})

            # Dispatch to the tool handler
            try:
                if self._tool_handler is None:
                    raise RuntimeError("No tool handler configured")
                result = self._tool_handler(tool_name, params)
                # Ensure result is JSON-serializable
                response_json = json.dumps({"result": result})
            except Exception as exc:
                logger.warning(
                    "Dispatch error for tool %s: %s", tool_name, exc,
                )
                response_json = json.dumps({"error": str(exc)})

            response_queue.put(response_json)

        return False

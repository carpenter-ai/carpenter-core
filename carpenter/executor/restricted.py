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

import builtins as _builtins_module
import ctypes
import json
import logging
import queue
import threading
import traceback
import types
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


# TRIPWIRE: This is a FAIL-CLOSED allowlist of vetted PURE-DATA stdlib packages
# that EXECUTOR code may import. Everything not on this set is denied by default
# (see the final `raise ImportError` in `_restricted_import`). The point of the
# executor restriction is to block DIRECT network/filesystem/syscall access and
# sandbox-escape primitives — NOT to forbid pure-data Python like `json`/`re`.
#
# Rules for adding to this set:
#   1. The module must NOT provide I/O (network, filesystem, subprocess) or any
#      sandbox-escape capability (code compilation/execution, introspection,
#      object-graph walking, raw memory, deserialization of arbitrary objects).
#   2. Re-exporting a dangerous module as a plain attribute (e.g. `uuid.os`,
#      `datetime.sys`) is OK *only because* the `_module_blocking_getattr` guard
#      below denies reaching ANY module object via attribute access. Do NOT rely
#      on a module being "clean" — rely on that generic guard.
#   3. A module exposing a dangerous NON-module callable as a plain attribute
#      (e.g. a bare `system`/`popen`/`open`) is NOT covered by the guard and MUST
#      be dropped. Vet each entry. (Current set: `re.compile` compiles regexes,
#      not code; `operator.call` is just `f(*args)` — neither is an escape.)
#
# DELIBERATELY EXCLUDED (do not add): os, io, sys, socket, subprocess, urllib,
# http, requests, ssl, asyncio, threading, multiprocessing, pathlib, shutil,
# tempfile, glob, signal, platform, resource, pwd, grp, inspect, gc, importlib,
# builtins, ctypes, pickle, marshal, ast, code, types, gzip, bz2, lzma
# (gzip/bz2/lzma import os/io and expose them as attributes; the rest are direct
# I/O / introspection / escape vectors).
_IMPORT_ALLOWLIST = frozenset({
    "json", "re", "math", "datetime", "base64", "binascii", "hashlib", "hmac",
    "uuid", "decimal", "fractions", "statistics", "random", "secrets", "string",
    "textwrap", "unicodedata", "difflib", "itertools", "functools", "operator",
    "heapq", "bisect", "array", "collections", "enum", "dataclasses", "typing",
    "copy", "numbers", "struct", "html", "csv", "zlib",
})


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


def _module_blocking_getattr(object, name, default=None, getattr=getattr):
    """``_getattr_`` guard: ``safer_getattr`` plus a module re-export block.

    ``safer_getattr`` already blocks underscore/private/inspect attribute names.
    But allowlisted pure-data modules frequently ``import os`` / ``import sys``
    (etc.) at top level and expose them as NON-underscore attributes
    (``uuid.os``, ``datetime.sys``, ``dataclasses.inspect`` …). Reaching any of
    those would be an immediate sandbox escape.

    TRIPWIRE: This guard MUST deny resolving an attribute whose value is a
    ``types.ModuleType`` instance. That is the generic defense that lets the
    import allowlist safely contain modules which re-export dangerous modules:
    you may ``import uuid``, but ``uuid.os`` is unreachable. Do NOT remove the
    ModuleType check, and do NOT special-case any module name as "trusted".
    The carpenter_tools proxies are ``_ToolModule``/``_CarpenterToolsRoot``/etc.
    (custom objects, NOT ModuleType), so they are unaffected.
    """
    # First apply all of safer_getattr's checks (underscore, format, inspect
    # attrs, default handling). This resolves and returns the attribute value.
    value = safer_getattr(object, name, default, getattr)
    # Then deny if the resolved value is a real module object. This blocks the
    # `import uuid; uuid.os.system(...)` style re-export escape generically,
    # regardless of which allowlisted module re-exported it.
    if isinstance(value, types.ModuleType):
        raise AttributeError(
            f'"{name}" resolves to a module object, which is forbidden to '
            f"access in the restricted executor."
        )
    return value


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
        dispatch_log: Mutable list where each dispatch call is recorded.
            Entries contain ``tool_name`` and byte-size counts only;
            ``params`` and ``result`` are intentionally NOT recorded so
            that the log can never leak plaintext untrusted arc state.
            See docs/trust-invariants.md I7.
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

        # TRIPWIRE: every value crossing the dispatch boundary MUST be JSON-serialized
        # (both request and response, both directions). Reason: passing live Python
        # objects across the queue would let user code reach back into platform state
        # via attribute access on a returned object. The JSON round-trip is the only
        # thing guaranteeing the boundary is a value boundary, not a reference one.
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

        # Audit-only entry: record that this tool was dispatched, but NEVER
        # record `params` or `result`. The executor may be running code
        # produced by an untrusted arc, in which case the params include
        # plaintext untrusted state (e.g. ``state.set(arc_id=<untrusted>,
        # value=<plaintext>)``). Storing those values in
        # ``ExecutionResult.dispatch_log`` would put plaintext on a
        # return-value field that callers could trivially persist,
        # undermining the I7 at-rest encryption guarantee for non-trusted
        # arcs. See docs/trust-invariants.md I7.
        #
        # The log keeps ``tool_name`` (already public — it's the name of
        # the dispatched tool) plus byte-size counts so callers can detect
        # "user code dispatched a 5 MB state.set" without seeing the
        # contents. Tool-name-only is sufficient for the current consumer
        # (a single test); the size fields are cheap defense-in-depth for
        # any future debugging tool.
        # TRIPWIRE: dispatch_log entries MUST contain only tool_name + sizes (and
        # a redacted error boolean below). Never add `params`, `result`, or error
        # text. Reason: user code may be running on behalf of an untrusted arc, in
        # which case params/results carry plaintext untrusted state. Persisting them
        # via ExecutionResult.dispatch_log defeats the I7 at-rest encryption
        # guarantee. Related: trust-invariants I7.
        log_entry = {
            "tool_name": tool_name,
            "params_size": len(request_json),
        }

        # Send request and wait for response
        request_queue.put(request_json)
        response_json = response_queue.get()

        # Deserialize response
        response = json.loads(response_json)

        if "error" in response:
            # Error strings can also reflect data the tool tried to act on
            # (e.g. "no such key: <secret-derived-name>"). Record a
            # redacted boolean instead of the error text; the full error
            # is still raised so user code sees the actual failure.
            log_entry["error"] = True
            dispatch_log.append(log_entry)
            raise RuntimeError(f"dispatch({tool_name}) failed: {response['error']}")

        log_entry["result_size"] = len(response_json)
        dispatch_log.append(log_entry)
        return response.get("result")

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
    # TRIPWIRE: _restricted_import is FAIL-CLOSED. It allows ONLY (a) the
    # `carpenter_tools` namespace and (b) absolute imports whose top-level
    # package is in `_IMPORT_ALLOWLIST` (vetted pure-data stdlib modules). It
    # raises ImportError for everything else. Reason: any non-allowlisted module
    # is a potential sandbox escape (os, subprocess, ctypes, builtins, pickle…).
    # The final `raise ImportError` branch below is load-bearing — do not add
    # an "else: return ..." fallback for unknown names, and do not widen the
    # allowlist without vetting per the rules above `_IMPORT_ALLOWLIST`.
    # Related: coding-invariants I5 (executor attests to nothing — platform-level
    # whitelisting is authoritative).
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

        # Allow vetted pure-data stdlib modules. Relative imports (level > 0)
        # are not meaningful for stdlib here and are denied. The TOP-LEVEL
        # package name must be on the allowlist; submodules of an allowed
        # package (e.g. ``collections.abc``) are permitted. The actual import is
        # delegated to the REAL builtin __import__ so that import X / import X
        # as y / from X import a / dotted-submodule semantics are all correct.
        # The platform process importing the real module is fine; the executor
        # restriction is on WHICH modules are reachable, and the
        # `_module_blocking_getattr` guard prevents reaching re-exported modules
        # (e.g. `uuid.os`) via attribute access.
        if level == 0 and isinstance(name, str) and name:
            top = name.split(".")[0]
            if top in _IMPORT_ALLOWLIST:
                return _builtins_module.__import__(
                    name, globals, locals, fromlist or (), level
                )

        raise ImportError(
            f"Imports are not allowed in the restricted executor "
            f"except for carpenter_tools and a vetted set of pure-data stdlib "
            f"modules. Module {name!r} is not permitted. Use dispatch() or the "
            f"pre-imported carpenter_tools modules instead."
        )

    builtins["__import__"] = _restricted_import

    namespace = {
        "__builtins__": builtins,
        "_getattr_": _module_blocking_getattr,
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
                # Intentionally suppress: PrintCollector failure shouldn't fail
                # the whole execution; just lose captured stdout.
                logger.info("PrintCollector _print() failed", exc_info=True)
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
                logger.exception(
                    "Dispatch error for tool %s", tool_name,
                )
                response_json = json.dumps({"error": str(exc)})

            response_queue.put(response_json)

        return False

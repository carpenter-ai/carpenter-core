"""Dry-run executor for verified flow analysis.

Executes code with tracked value wrappers injected for CONSTRAINED
inputs. Mock tool modules record calls instead of executing them.
If ConstrainedControlFlowError is raised, the code is rejected.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from .tracked import Tracked, TrackedList, TrackedDict, TrackedStr, wrap_value
from carpenter.core.trust.integrity import IntegrityLevel
from carpenter.security.exceptions import ConstrainedControlFlowError

logger = logging.getLogger(__name__)

_T = IntegrityLevel.TRUSTED.value
_C = IntegrityLevel.CONSTRAINED.value
_U = IntegrityLevel.UNTRUSTED.value


# Lazy-cached tool policy map built from @tool(param_policies=...) metadata.
_tool_policy_map_cache: dict[tuple[str, str, int | str], str] | None = None

# Lazy-cached return type map built from @tool(return_types=...) metadata.
_tool_return_type_map_cache: dict[tuple[str, str], str | dict[str, str]] | None = None


def _get_tool_policy_map() -> dict[tuple[str, str, int | str], str]:
    """Return the tool policy map, building it on first use."""
    global _tool_policy_map_cache
    if _tool_policy_map_cache is None:
        from carpenter_tools.tool_meta import build_tool_policy_map
        _tool_policy_map_cache = build_tool_policy_map()
    return _tool_policy_map_cache


def _get_tool_return_type_map() -> dict[tuple[str, str], str | dict[str, str]]:
    """Return the tool return type map, building it on first use."""
    global _tool_return_type_map_cache
    if _tool_return_type_map_cache is None:
        from carpenter_tools.tool_meta import build_tool_return_type_map
        _tool_return_type_map_cache = build_tool_return_type_map()
    return _tool_return_type_map_cache


def clear_tool_policy_cache() -> None:
    """Clear the cached tool policy map, forcing a rebuild on next use.

    Called after ``carpenter_tools`` modules are reloaded (e.g. after
    platform self-modification) so new ``param_policies`` take effect.
    """
    global _tool_policy_map_cache, _tool_return_type_map_cache
    _tool_policy_map_cache = None
    _tool_return_type_map_cache = None


@dataclass
class ToolCallRecord:
    """Record of a tool call made during dry-run."""

    module: str
    function: str
    args: tuple
    kwargs: dict


@dataclass
class DryRunResult:
    """Result of dry-run verification."""

    passed: bool
    reason: str = ""
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    input_combinations: int = 0
    error_detail: str = ""


def _get_default_threshold() -> int:
    """Return the default dry-run threshold from config."""
    from carpenter import config as config_mod
    verification_cfg = config_mod.CONFIG.get("verification", {})
    return verification_cfg.get("threshold", 150)


def run_dry_run(
    code: str,
    constrained_inputs: list[dict],
    threshold: int | None = None,
) -> DryRunResult:
    """Execute code with tracked wrappers for all input combinations.

    Args:
        code: Python source code to verify.
        constrained_inputs: List of dicts with 'key', 'arc_id',
            'integrity_level'. Each generates enumerated test values.
        threshold: Max input combinations before rejecting.  Defaults to
            the ``verification.threshold`` config value (150 if unset).
            Keep low — code needing more combinations should validate
            inputs via policy-typed literals first to decompose them to
            TRUSTED.

    Returns:
        DryRunResult with passed=True if all combinations succeed.
    """
    if threshold is None:
        threshold = _get_default_threshold()

    # Generate input value sets for each constrained input
    input_sets = _enumerate_inputs(constrained_inputs)

    # Compute total combinations
    if not input_sets:
        # No constrained inputs — run once with no tracked wrappers
        return _run_single(code, {})

    total_combos = 1
    contributors = []
    for key, values in input_sets.items():
        contributors.append((key, len(values)))
        total_combos *= len(values)
        if total_combos > threshold:
            breakdown = " × ".join(
                f"'{k}' ({n} values)" for k, n in contributors
            )
            return DryRunResult(
                passed=False,
                reason=f"Input space ({total_combos}+) exceeds threshold ({threshold}): "
                       f"{breakdown}. Validate each constrained variable independently "
                       f"via policy-typed literals (EmailPolicy, Domain, etc.) before combining "
                       f"them — decomposed values become TRUSTED and drop out of "
                       f"enumeration.",
                input_combinations=total_combos,
            )

    # Generate cross-product of input combinations
    all_tool_calls: list[ToolCallRecord] = []
    keys = list(input_sets.keys())
    combo_count = 0

    for combo_values in _cross_product([input_sets[k] for k in keys]):
        combo_count += 1
        input_mapping = {}
        for i, key in enumerate(keys):
            input_mapping[key] = combo_values[i]

        result = _run_single(code, input_mapping)
        if not result.passed:
            return DryRunResult(
                passed=False,
                reason=result.reason,
                tool_calls=all_tool_calls + result.tool_calls,
                input_combinations=combo_count,
                error_detail=result.error_detail,
            )
        all_tool_calls.extend(result.tool_calls)

    return DryRunResult(
        passed=True,
        tool_calls=all_tool_calls,
        input_combinations=combo_count,
    )


class _SafeOsModule:
    """Proxy for ``os`` that blocks side-effect functions during dry-run.

    Read-only operations (``os.path.*``, ``os.getcwd()``, ``os.sep``, etc.)
    are forwarded.  Mutating or process-spawning functions raise
    ``PermissionError`` so verified code cannot accidentally delete files,
    execute commands, or change the working directory.
    """

    # Functions that are safe (read-only) and explicitly allowed
    _ALLOWED = frozenset({
        "getcwd", "getenv", "sep", "pathsep", "linesep", "name", "curdir",
        "pardir", "extsep", "altsep", "devnull", "fspath",
    })
    # Functions that are dangerous and explicitly blocked
    _BLOCKED = frozenset({
        "system", "popen", "remove", "unlink", "rmdir", "removedirs",
        "rename", "renames", "replace", "mkdir", "makedirs", "chdir",
        "fchdir", "chroot", "chmod", "chown", "lchmod", "lchown",
        "link", "symlink", "truncate", "execl", "execle", "execlp",
        "execlpe", "execv", "execve", "execvp", "execvpe", "fork",
        "kill", "killpg", "abort", "_exit",
    })

    def __init__(self):
        import os as _real_os
        self._real_os = _real_os
        # Expose os.path as-is (read-only operations)
        self.path = _real_os.path
        # Expose safe constants
        for attr in ("sep", "pathsep", "linesep", "name", "curdir",
                      "pardir", "extsep", "altsep", "devnull"):
            setattr(self, attr, getattr(_real_os, attr, None))

    def __getattr__(self, name):
        if name in self._BLOCKED:
            raise PermissionError(
                f"os.{name}() is not allowed during dry-run verification"
            )
        if name in self._ALLOWED:
            return getattr(self._real_os, name)
        # For anything else not explicitly listed, check if it exists and
        # allow read-only attribute access (e.g. os.environ for reads)
        val = getattr(self._real_os, name, None)
        if val is None:
            raise AttributeError(f"module 'os' has no attribute '{name}'")
        return val


class _SafeTimeModule:
    """Proxy for ``time`` that replaces ``sleep()`` with a no-op.

    Prevents verified code from blocking the dry-run verifier.
    All other ``time`` functions are forwarded to the real module.
    """

    def __init__(self):
        import time as _real_time
        self._real_time = _real_time

    def sleep(self, *args, **kwargs):
        """No-op: sleep is skipped during dry-run."""
        pass

    def __getattr__(self, name):
        return getattr(self._real_time, name)


def _run_single(code: str, input_mapping: dict[str, Any]) -> DryRunResult:
    """Run code once with the given input values wrapped as Tracked."""
    tool_calls: list[ToolCallRecord] = []

    # ── Mock state module ────────────────────────────────────────
    class MockState:
        def get(self, key, arc_id=None, **kwargs):
            lookup_key = f"{key}:{arc_id}" if arc_id is not None else key
            if lookup_key in input_mapping:
                return input_mapping[lookup_key]
            return Tracked(None, _T)

        def set(self, key, value, **kwargs):
            tool_calls.append(ToolCallRecord("state", "set", (key, value), kwargs))
            return Tracked(True, _T)

        def get_typed(self, key, model_class=None, **kwargs):
            return Tracked(None, _T)

        def set_typed(self, key, model, **kwargs):
            tool_calls.append(ToolCallRecord("state", "set_typed", (key, model), kwargs))
            return Tracked(True, _T)

        def delete(self, key, **kwargs):
            tool_calls.append(ToolCallRecord("state", "delete", (key,), kwargs))
            return Tracked(True, _T)

        def list_keys(self, **kwargs):
            return wrap_value([], _T)

    # ── Mock arc modules (split act vs read) ─────────────────────
    class MockActArc:
        def create(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("arc", "create", args, kwargs))
            return Tracked(1, _T)

        def add_child(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("arc", "add_child", args, kwargs))
            return Tracked(1, _T)

        def create_batch(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("arc", "create_batch", args, kwargs))
            return Tracked({"arc_ids": [1]}, _T)

        def freeze(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("arc", "freeze", args, kwargs))
            return Tracked(True, _T)

        def cancel(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("arc", "cancel", args, kwargs))
            return Tracked(0, _T)

        def update_status(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("arc", "update_status", args, kwargs))
            return Tracked(None, _T)

        def invoke_coding_change(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("arc", "invoke_coding_change", args, kwargs))
            return Tracked(1, _T)

        def request_ai_review(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("arc", "request_ai_review", args, kwargs))
            return Tracked(1, _T)

        def grant_read_access(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("arc", "grant_read_access", args, kwargs))
            return wrap_value({"granted": True}, _T)

    class MockReadArc:
        def get(self, *args, **kwargs):
            return wrap_value(None, _T)

        def get_children(self, *args, **kwargs):
            return wrap_value([], _T)

        def get_history(self, *args, **kwargs):
            return wrap_value([], _T)

        def get_plan(self, *args, **kwargs):
            return wrap_value(None, _T)

        def get_children_plan(self, *args, **kwargs):
            return wrap_value([], _T)

        def read_output_UNTRUSTED(self, *args, **kwargs):
            return Tracked("", _C)

        def read_state_UNTRUSTED(self, *args, **kwargs):
            return Tracked("", _C)

    # ── Mock messaging modules (split act vs read) ───────────────
    class MockActMessaging:
        def send(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("messaging", "send", args, kwargs))
            return Tracked(True, _T)

    class MockReadMessaging:
        def ask(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("messaging", "ask", args, kwargs))
            return wrap_value({"response": ""}, _T)

    class MockWeb:
        def get(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("web", "get", args, kwargs))
            return TrackedDict({
                "status_code": Tracked(200, _T),
                "text": TrackedStr("", _C),
            }, _T)

        def post(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("web", "post", args, kwargs))
            return TrackedDict({
                "status_code": Tracked(200, _T),
                "text": TrackedStr("", _C),
            }, _T)

    class MockFiles:
        def write(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("files", "write", args, kwargs))
            return Tracked(True, _T)

        def read(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("files", "read", args, kwargs))
            return wrap_value("", _C)

        def list_dir(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("files", "list_dir", args, kwargs))
            return wrap_value([], _T)

    class MockScheduling:
        def add_cron(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("scheduling", "add_cron", args, kwargs))
            return Tracked(1, _T)

        def add_once(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("scheduling", "add_once", args, kwargs))
            return Tracked(1, _T)

        def remove_cron(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("scheduling", "remove_cron", args, kwargs))
            return Tracked(True, _T)

        def list_cron(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("scheduling", "list_cron", args, kwargs))
            return wrap_value([], _T)

        def enable_cron(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("scheduling", "enable_cron", args, kwargs))
            return Tracked(True, _T)

    # ── Mock git modules (split act vs read) ─────────────────────
    class MockActGit:
        def setup_repo(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("git", "setup_repo", args, kwargs))
            return Tracked(True, _T)

        def create_branch(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("git", "create_branch", args, kwargs))
            return Tracked(True, _T)

        def commit_and_push(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("git", "commit_and_push", args, kwargs))
            return Tracked(True, _T)

        def create_pr(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("git", "create_pr", args, kwargs))
            return Tracked(True, _T)

        def list_prs(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("git", "list_prs", args, kwargs))
            return wrap_value([], _T)

        def merge_pr(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("git", "merge_pr", args, kwargs))
            return Tracked(True, _T)

        def close_pr(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("git", "close_pr", args, kwargs))
            return Tracked(True, _T)

        def post_pr_review(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("git", "post_pr_review", args, kwargs))
            return Tracked(True, _T)

        def create_repo_webhook(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("git", "create_repo_webhook", args, kwargs))
            return Tracked(True, _T)

        def delete_repo_webhook(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("git", "delete_repo_webhook", args, kwargs))
            return Tracked(True, _T)

    class MockReadGit:
        def get_pr(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("git", "get_pr", args, kwargs))
            return wrap_value({}, _T)

        def get_pr_diff(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("git", "get_pr_diff", args, kwargs))
            return wrap_value("", _C)

    class MockLm:
        def call(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("lm", "call", args, kwargs))
            return TrackedDict({
                "content": TrackedStr("", _C),
                "model": TrackedStr("mock", _T),
                "usage": TrackedDict({}, _T),
                "role": TrackedStr("assistant", _T),
            }, _T)

    # ── Mock plugin modules (split act vs read) ──────────────────
    class MockActPlugin:
        def submit_task(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("plugin", "submit_task", args, kwargs))
            return wrap_value({"status": "completed", "output": ""}, _T)

        def submit_task_async(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("plugin", "submit_task_async", args, kwargs))
            return wrap_value({"task_id": "mock-task-id", "plugin_name": "mock"}, _T)

    class MockReadPlugin:
        def list_plugins(self, *args, **kwargs):
            return wrap_value([], _T)

        def get_task_status(self, *args, **kwargs):
            return wrap_value({"completed": False}, _T)

        def read_workspace_file(self, *args, **kwargs):
            return wrap_value("", _C)

        def check_health(self, *args, **kwargs):
            return wrap_value({"healthy": True}, _T)

    class MockReview:
        def submit_verdict(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("review", "submit_verdict", args, kwargs))
            return Tracked(True, _T)

    class MockPlatformTime:
        def current_time(self, *args, **kwargs):
            return wrap_value({"timestamp": "2026-01-01T00:00:00Z", "platform": "Carpenter"}, _T)

    class MockSystemInfo:
        def system_info(self, *args, **kwargs):
            return wrap_value({"hostname": "mock", "python_version": "3.11"}, _T)

    # ── Mock modules for previously missing submodules ────────────
    class MockConfig:
        """Mock for carpenter_tools.act.config."""
        def set_value(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("config", "set_value", args, kwargs))
            return wrap_value({"status": "ok"}, _T)

        def reload(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("config", "reload", args, kwargs))
            return wrap_value({"status": "ok", "reloaded": True}, _T)

    class MockReadConfig:
        """Mock for carpenter_tools.read.config."""
        def get_value(self, *args, **kwargs):
            return wrap_value({"key": "", "value": None}, _T)

        def list_keys(self, *args, **kwargs):
            return wrap_value({"keys": []}, _T)

        def models(self, *args, **kwargs):
            return wrap_value({"models": {}}, _T)

    class MockConversation:
        """Mock for carpenter_tools.act.conversation."""
        def rename(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("conversation", "rename", args, kwargs))
            return wrap_value({"status": "ok"}, _T)

        def archive(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("conversation", "archive", args, kwargs))
            return wrap_value({"status": "ok"}, _T)

    class MockCredentials:
        """Mock for carpenter_tools.act.credentials."""
        def request(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("credentials", "request", args, kwargs))
            return wrap_value({"request_id": "mock", "url": "https://mock"}, _T)

        def verify(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("credentials", "verify", args, kwargs))
            return wrap_value({"valid": True}, _T)

        def import_file(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("credentials", "import_file", args, kwargs))
            return wrap_value({"status": "ok"}, _T)

    class MockKb:
        """Mock for carpenter_tools.act.kb."""
        def edit(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("kb", "edit", args, kwargs))
            return wrap_value({"status": "ok"}, _T)

        def add(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("kb", "add", args, kwargs))
            return wrap_value({"status": "ok"}, _T)

        def delete(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("kb", "delete", args, kwargs))
            return wrap_value({"status": "ok"}, _T)

    class MockPlatform:
        """Mock for carpenter_tools.act.platform."""
        def request_restart(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("platform", "request_restart", args, kwargs))
            return wrap_value({"status": "ok"}, _T)

    class MockActWebhook:
        """Mock for carpenter_tools.act.webhook."""
        def subscribe(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("webhook", "subscribe", args, kwargs))
            return wrap_value({"webhook_id": "mock", "subscription_id": 1}, _T)

        def delete(self, *args, **kwargs):
            tool_calls.append(ToolCallRecord("webhook", "delete", args, kwargs))
            return wrap_value({"deleted": True}, _T)

    class MockReadWebhook:
        """Mock for carpenter_tools.read.webhook."""
        def list_subscriptions(self, *args, **kwargs):
            return wrap_value([], _T)

    # Build mock act/read aggregate modules
    class MockActModule:
        arc = MockActArc()
        messaging = MockActMessaging()
        state = MockState()
        web = MockWeb()
        files = MockFiles()
        scheduling = MockScheduling()
        git = MockActGit()
        lm = MockLm()
        plugin = MockActPlugin()
        review = MockReview()
        config = MockConfig()
        conversation = MockConversation()
        credentials = MockCredentials()
        kb = MockKb()
        platform = MockPlatform()
        webhook = MockActWebhook()

    class MockReadModule:
        arc = MockReadArc()
        state = MockState()
        messaging = MockReadMessaging()
        files = MockFiles()
        platform_time = MockPlatformTime()
        system_info = MockSystemInfo()
        plugin = MockReadPlugin()
        config = MockReadConfig()
        git = MockReadGit()
        webhook = MockReadWebhook()

    class MockPolicyModule:
        """Pass-through to real policy types for verification."""
        pass

    # Import real policy types for the mock
    from carpenter_tools.policy.types import (
        EmailPolicy, Domain, Url, FilePath, Command, IntRange, Enum, Bool, Pattern,
        PolicyLiteral,
    )
    mock_policy = MockPolicyModule()
    for cls in (EmailPolicy, Domain, Url, FilePath, Command, IntRange, Enum, Bool, Pattern, PolicyLiteral):
        setattr(mock_policy, cls.__name__, cls)

    # Import real declaration types for the mock
    from carpenter_tools.declarations import (
        SecurityType, Label, URL as DeclURL, WorkspacePath, SQL,
        JSON as DeclJSON, UnstructuredText,
        Email as DeclEmail,
    )

    class MockDeclarationsModule:
        """Pass-through to real SecurityType classes for verification."""
        pass

    mock_declarations = MockDeclarationsModule()
    for cls in (SecurityType, Label, DeclURL, WorkspacePath, SQL, DeclJSON, UnstructuredText, DeclEmail):
        setattr(mock_declarations, cls.__name__, cls)

    # Build mock modules map for __import__
    mock_modules = {
        "carpenter_tools": type("Module", (), {
            "act": MockActModule(),
            "read": MockReadModule(),
            "policy": mock_policy,
            "declarations": mock_declarations,
        })(),
        # act submodules
        "carpenter_tools.act": MockActModule(),
        "carpenter_tools.act.arc": MockActArc(),
        "carpenter_tools.act.messaging": MockActMessaging(),
        "carpenter_tools.act.state": MockState(),
        "carpenter_tools.act.web": MockWeb(),
        "carpenter_tools.act.files": MockFiles(),
        "carpenter_tools.act.scheduling": MockScheduling(),
        "carpenter_tools.act.git": MockActGit(),
        "carpenter_tools.act.lm": MockLm(),
        "carpenter_tools.act.plugin": MockActPlugin(),
        "carpenter_tools.act.review": MockReview(),
        "carpenter_tools.act.config": MockConfig(),
        "carpenter_tools.act.conversation": MockConversation(),
        "carpenter_tools.act.credentials": MockCredentials(),
        "carpenter_tools.act.kb": MockKb(),
        "carpenter_tools.act.platform": MockPlatform(),
        "carpenter_tools.act.webhook": MockActWebhook(),
        # read submodules
        "carpenter_tools.read": MockReadModule(),
        "carpenter_tools.read.arc": MockReadArc(),
        "carpenter_tools.read.state": MockState(),
        "carpenter_tools.read.messaging": MockReadMessaging(),
        "carpenter_tools.read.files": MockFiles(),
        "carpenter_tools.read.platform_time": MockPlatformTime(),
        "carpenter_tools.read.system_info": MockSystemInfo(),
        "carpenter_tools.read.plugin": MockReadPlugin(),
        "carpenter_tools.read.config": MockReadConfig(),
        "carpenter_tools.read.git": MockReadGit(),
        "carpenter_tools.read.webhook": MockReadWebhook(),
        # policy & declarations
        "carpenter_tools.policy": mock_policy,
        "carpenter_tools.policy.types": mock_policy,
        "carpenter_tools.declarations": mock_declarations,
    }

    # ── Safe module proxies ──────────────────────────────────────
    # Block side-effect functions in os while allowing read-only access
    safe_os = _SafeOsModule()
    # Block time.sleep to prevent blocking during dry-run
    safe_time = _SafeTimeModule()

    def _mock_import(name, globals=None, locals=None, fromlist=(), level=0):
        """Restricted import that only allows carpenter_tools."""
        if name in mock_modules:
            return mock_modules[name]
        # Allow os with side-effect functions blocked
        if name in ("os", "os.path"):
            return safe_os
        # Allow standard library modules commonly used in submitted code
        if name == "time":
            return safe_time
        from .whitelist import _get_allowed_stdlib_modules
        if name in _get_allowed_stdlib_modules():
            import importlib
            return importlib.import_module(name)
        raise ImportError(f"Import of '{name}' not allowed in verified code")

    # Build restricted globals
    safe_builtins = {
        "True": True, "False": False, "None": None,
        "len": len, "range": range, "list": list, "dict": dict,
        "tuple": tuple, "set": set, "str": str, "int": int,
        "float": float, "bool": _safe_bool, "print": _noop_print,
        "enumerate": enumerate, "zip": zip, "sorted": sorted,
        "min": min, "max": max, "abs": abs, "round": round,
        "isinstance": isinstance, "type": type,
        "hasattr": hasattr, "getattr": getattr,
        "__import__": _mock_import,
    }

    exec_globals = {
        "__builtins__": safe_builtins,
        "state": MockState(),
        "arc": MockActArc(),
        "messaging": MockActMessaging(),
    }

    # Set verification mode and patch policy validation to use in-memory policies
    import os
    old_env = os.environ.get("CARPENTER_VERIFICATION_MODE")
    os.environ["CARPENTER_VERIFICATION_MODE"] = "1"

    # Patch the policy validator to use in-memory policies directly
    # instead of HTTP callbacks (we're running in the platform process)
    import carpenter_tools.policy._validate as _validate_mod
    _original_validate = _validate_mod.validate_policy_value

    def _local_validate(policy_type: str, value: str) -> bool:
        """Validate against in-memory policies without HTTP."""
        from carpenter.security.policies import get_policies
        try:
            return get_policies().validate(policy_type, value)
        except (KeyError, ValueError, TypeError) as _exc:
            from carpenter.security.exceptions import PolicyValidationError
            raise PolicyValidationError(policy_type, value)

    _validate_mod.validate_policy_value = _local_validate

    try:
        compiled = compile(code, "<verify>", "exec")
        exec(compiled, exec_globals)

        # Validate recorded tool calls against security policies
        policy_violations = _validate_tool_calls(tool_calls)
        if policy_violations:
            detail = "; ".join(policy_violations)
            return DryRunResult(
                passed=False,
                reason=f"Tool call policy violation: {detail}",
                tool_calls=tool_calls,
                error_detail=detail,
            )

        return DryRunResult(passed=True, tool_calls=tool_calls)
    except ConstrainedControlFlowError as e:
        return DryRunResult(
            passed=False,
            reason=f"CONSTRAINED data reached control flow: {e}",
            tool_calls=tool_calls,
            error_detail=str(e),
        )
    except Exception as e:  # broad catch: dry run involves complex tool dispatch
        return DryRunResult(
            passed=False,
            reason=f"Execution error during dry-run: {type(e).__name__}: {e}",
            tool_calls=tool_calls,
            error_detail=str(e),
        )
    finally:
        _validate_mod.validate_policy_value = _original_validate
        if old_env is None:
            os.environ.pop("CARPENTER_VERIFICATION_MODE", None)
        else:
            os.environ["CARPENTER_VERIFICATION_MODE"] = old_env


def _extract_tracked_values(value: Any) -> list[tuple[Any, str]]:
    """Recursively extract (raw_value, label) pairs from tracked values.

    Only returns pairs where the label is non-trusted (C or U).
    """
    results: list[tuple[Any, str]] = []
    if isinstance(value, Tracked):
        if value.label != _T:
            results.append((value.value, value.label))
    elif isinstance(value, TrackedStr):
        if value.label != _T:
            results.append((value.value, value.label))
    elif isinstance(value, TrackedList):
        for item in value.items:
            results.extend(_extract_tracked_values(item))
    elif isinstance(value, TrackedDict):
        for v in value.values():
            results.extend(_extract_tracked_values(v))
    return results


def _validate_tool_calls(tool_calls: list[ToolCallRecord]) -> list[str]:
    """Validate recorded tool calls against security policies.

    Returns a list of violation descriptions. Empty means all passed.
    """
    from carpenter.security.policies import get_policies
    policies = get_policies()
    policy_map = _get_tool_policy_map()
    violations: list[str] = []

    for tc in tool_calls:
        # Check positional args
        for i, arg in enumerate(tc.args):
            policy_type = policy_map.get((tc.module, tc.function, i))
            if policy_type is None:
                continue
            tracked_values = _extract_tracked_values(arg)
            for raw_val, label in tracked_values:
                if not policies.is_allowed(policy_type, str(raw_val)):
                    violations.append(
                        f"{tc.module}.{tc.function}() arg[{i}]: "
                        f"value {raw_val!r} (label={label}) not in "
                        f"{policy_type} allowlist"
                    )

        # Check keyword args
        for kw_name, kw_val in tc.kwargs.items():
            policy_type = policy_map.get((tc.module, tc.function, kw_name))
            if policy_type is None:
                continue
            tracked_values = _extract_tracked_values(kw_val)
            for raw_val, label in tracked_values:
                if not policies.is_allowed(policy_type, str(raw_val)):
                    violations.append(
                        f"{tc.module}.{tc.function}() kwarg '{kw_name}': "
                        f"value {raw_val!r} (label={label}) not in "
                        f"{policy_type} allowlist"
                    )

    return violations


def _enumerate_inputs(constrained_inputs: list[dict]) -> dict[str, list[Any]]:
    """Generate enumerated test values for constrained inputs.

    Uses detected_type from taint analysis to choose enumeration strategy:
    - "bool" or None -> [True, False]
    - "email"/"domain"/"url"/"filepath"/"command"/"enum" -> allowlist values
    - "int_range" -> enumerate integers from ranges (cap at 100 per range)
    - "pattern" -> [True, False] fallback (can't enumerate regex matches)
    - is_iterated=True -> wrap each value in a single-element list
    """
    from carpenter.security.policies import get_policies

    result: dict[str, list[Any]] = {}
    policies = get_policies()

    for inp in constrained_inputs:
        key = inp.get("key", "<unknown>")
        arc_id = inp.get("arc_id")
        lookup_key = f"{key}:{arc_id}" if arc_id is not None else key
        detected_type = inp.get("detected_type")
        is_iterated = inp.get("is_iterated", False)
        has_accumulator = inp.get("has_accumulator", False)

        raw_values = _enumerate_for_type(detected_type, policies)

        # Wrap values as tracked
        if is_iterated:
            if has_accumulator:
                # Single multi-element list — tests full accumulation
                values = [wrap_value(raw_values, _C)]
            else:
                # N single-element lists — tests each value independently
                values = [wrap_value([rv], _C) for rv in raw_values]
        else:
            values = [wrap_value(rv, _C) for rv in raw_values]

        result[lookup_key] = values

    return result


_INT_RANGE_CAP = 100  # max integers per range entry


def _enumerate_for_type(detected_type: str | None, policies: Any) -> list[Any]:
    """Return raw values to enumerate for a detected policy type."""
    if detected_type is None or detected_type == "bool" or detected_type == "pattern":
        return [True, False]

    if detected_type in ("email", "domain", "url", "filepath", "command", "enum"):
        allowlist = policies.get_allowlist(detected_type)
        if not allowlist:
            return ["__no_allowlist_value__"]
        return sorted(allowlist)

    if detected_type == "int_range":
        allowlist = policies.get_allowlist("int_range")
        if not allowlist:
            return ["__no_allowlist_value__"]
        values: list[Any] = []
        for range_str in sorted(allowlist):
            parts = range_str.split(":")
            if len(parts) == 2:
                try:
                    lo, hi = int(parts[0]), int(parts[1])
                    count = min(hi - lo + 1, _INT_RANGE_CAP)
                    values.extend(range(lo, lo + count))
                except ValueError:
                    continue
        return values if values else ["__no_allowlist_value__"]

    # Unknown type — fall back to bool
    return [True, False]


def _cross_product(lists: list[list]) -> list[tuple]:
    """Compute cross-product of lists of values."""
    if not lists:
        return [()]
    result = [()]
    for lst in lists:
        result = [prev + (val,) for prev in result for val in lst]
    return result


def _safe_bool(value):
    """Safe bool that works with Tracked values."""
    if isinstance(value, Tracked):
        return bool(value)  # triggers __bool__ check
    return bool(value)


def _noop_print(*args, **kwargs):
    """Print that does nothing during dry-run."""
    pass

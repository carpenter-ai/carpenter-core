"""Operator-only chat tools (chat_tool_registry.OPERATOR_ONLY_TOOLS).

Core, not the user-owned tool module, decides that these need a human:
an arc agent never runs them, and the chat agent runs them only after a
confirmation handler approves the call.
"""

import json

import pytest

from carpenter import config
from carpenter.agent import invocation
from carpenter.chat_tool_loader import set_confirmation_handler
from carpenter.chat_tool_registry import OPERATOR_ONLY_TOOLS, PLATFORM_TOOLS
from carpenter.core import budget

BUDGET_DEF = {
    "name": "budget_control",
    "description": "control the breaker",
    "input_schema": {
        "type": "object",
        "properties": {"action": {"type": "string"}},
        "required": ["action"],
    },
}


@pytest.fixture
def executed(monkeypatch):
    """Record calls that reach tool execution instead of running them."""
    calls = []

    def fake_execute(tool_name, tool_input, **kwargs):
        calls.append((tool_name, kwargs.get("executor_arc_id")))
        return "executed"

    monkeypatch.setattr(invocation, "_execute_chat_tool", fake_execute)
    yield calls
    set_confirmation_handler(None)


def _dispatch(executor_arc_id=None):
    return invocation._dispatch_chat_tool_call(
        "budget_control", {"action": "disable"}, [BUDGET_DEF],
        conv_id=1, executor_arc_id=executor_arc_id, executor_conv_id=None,
    )


def test_operator_only_tools_are_platform_tools():
    assert OPERATOR_ONLY_TOOLS <= PLATFORM_TOOLS


def test_refused_without_confirmation_handler(executed, monkeypatch):
    """No handler: a clear operator-only error, and the tool never runs.

    The loaded module is assumed stale (no requires_user_confirm), so the
    refusal must come from core.
    """
    monkeypatch.setattr(
        "carpenter.chat_tool_loader.get_loaded_tools", lambda: {},
    )
    set_confirmation_handler(None)
    out = _dispatch()
    assert executed == []
    assert "operator-only" in out
    assert "python3 -m carpenter budget" in out


def test_refused_for_arc_agent_even_when_confirmed(executed):
    set_confirmation_handler(lambda name, inp: True)
    out = _dispatch(executor_arc_id=42)
    assert executed == []
    assert "operator-only" in out and "arc agent" in out


def test_runs_after_human_confirms(executed):
    seen = []
    set_confirmation_handler(lambda name, inp: seen.append(name) or True)
    assert _dispatch() == "executed"
    assert seen == ["budget_control"]
    assert executed == [("budget_control", None)]


def test_declined_confirmation_does_not_run(executed):
    set_confirmation_handler(lambda name, inp: False)
    assert "declined" in _dispatch()
    assert executed == []


def test_arc_agents_are_not_offered_operator_only_tools():
    tools = [{"name": n} for n in sorted(OPERATOR_ONLY_TOOLS | {"read_file"})]
    assert invocation._drop_operator_only_tools(tools) == [{"name": "read_file"}]


def test_seed_budget_control_declares_confirmation():
    from pathlib import Path
    from carpenter.chat_tool_loader import load_chat_tools
    import shutil, tempfile

    seed = Path(invocation.__file__).resolve().parents[2] / "config_seed" / "chat_tools"
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(seed / "budget.py", d)
        tools = load_chat_tools(d)
    assert tools["budget_control"].requires_user_confirm is True
    assert tools["budget_status"].requires_user_confirm is False


# ── CLI: the operator's own path ───────────────────────────────────

@pytest.fixture
def kill_switch(monkeypatch):
    monkeypatch.setattr(budget, "_notify", lambda *a, **k: None)
    config.CONFIG["api_budget"] = {
        "enabled": True, "notify_human": False,
        "limits": [{"name": "kill", "metric": "calls", "window_seconds": 3600,
                    "threshold": 0, "action": "shutdown"}],
    }
    budget._cached_summary = None
    budget._last_eval_ts = 0.0
    budget._evaluate(force=True)
    assert (budget.status()["shutdown"] or {}).get("active") is True
    # Stop the condition from re-latching once cleared.
    config.CONFIG["api_budget"]["limits"][0]["threshold"] = 10_000_000
    budget._cached_summary = None
    budget._last_eval_ts = 0.0


def test_cli_resume_clears_latch(kill_switch, capsys):
    from carpenter.cli_budget import cmd_budget
    assert cmd_budget(["resume"]) == 0
    assert "Cleared latches" in capsys.readouterr().out
    budget._cached_summary = None
    assert not (budget.status()["shutdown"] or {}).get("active")


def test_cli_status_disable_enable(capsys):
    from carpenter.cli_budget import cmd_budget
    assert cmd_budget(["disable"]) == 0
    assert budget._enabled() is False
    assert cmd_budget(["enable"]) == 0
    assert budget._enabled() is True
    capsys.readouterr()
    assert cmd_budget(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is True


def test_cli_set_threshold(capsys):
    from carpenter.cli_budget import cmd_budget
    assert cmd_budget(["set-threshold", "kill", "5", "--ttl", "60"]) == 0
    overrides = budget.status()["threshold_overrides"]
    assert overrides["kill"]["threshold"] == 5.0
    assert "expires_at" in overrides["kill"]


def test_cli_refuses_missing_database(tmp_path, monkeypatch, capsys):
    """Wrong user or config: refuse, and never create an empty database."""
    from carpenter.cli_budget import cmd_budget
    missing = tmp_path / "nope" / "platform.db"
    monkeypatch.setitem(config.CONFIG, "database_path", str(missing))
    assert cmd_budget(["disable"]) == 1
    assert "no Carpenter database" in capsys.readouterr().err
    assert not missing.exists()


def test_cli_refuses_uninitialised_database(tmp_path, monkeypatch, capsys):
    import sqlite3
    from carpenter.cli_budget import cmd_budget
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    monkeypatch.setitem(config.CONFIG, "database_path", str(empty))
    assert cmd_budget(["resume"]) == 1
    assert "budget_state" in capsys.readouterr().err


def test_cli_rejects_unknown_command(capsys):
    from carpenter.cli_budget import cmd_budget
    assert cmd_budget(["bogus"]) == 2

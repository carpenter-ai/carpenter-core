"""Chat-only chat tools (chat_tool_registry.CHAT_ONLY_TOOLS).

The mirror image of the operator-only set: core refuses these to an ARC
agent and allows them to the chat agent, whatever the user-owned tool
module declares.  An arc runs unattended and may be acting on content a
third party wrote, so a tool whose safety argument is "a human is
reading this conversation" must never be reachable from one.
"""

import pytest

from carpenter.agent import invocation
from carpenter.chat_tool_loader import set_confirmation_handler
from carpenter.chat_tool_registry import (
    CHAT_ONLY_TOOLS,
    OPERATOR_ONLY_TOOLS,
)

TOOL = sorted(CHAT_ONLY_TOOLS)[0]

TOOL_DEF = {
    "name": TOOL,
    "description": "ask the operator to approve something",
    "input_schema": {
        "type": "object",
        "properties": {"sources": {"type": "array",
                                   "items": {"type": "string"}}},
        "required": ["sources"],
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
        TOOL, {"sources": ["git+https://forge.invalid/o/r.git"]}, [TOOL_DEF],
        conv_id=1, executor_arc_id=executor_arc_id, executor_conv_id=None,
    )


# ── the sets ────────────────────────────────────────────────────────

def test_chat_only_and_operator_only_are_disjoint():
    """A tool in both would be unreachable by every agent.

    ``_select_chat_tools`` drops operator-only tools for EVERYONE when no
    confirmation handler is registered, and none is registered on this
    platform.
    """
    assert CHAT_ONLY_TOOLS & OPERATOR_ONLY_TOOLS == frozenset()


def test_the_set_is_not_empty():
    assert CHAT_ONLY_TOOLS


# ── dispatch ────────────────────────────────────────────────────────

def test_refused_for_an_arc_agent(executed, monkeypatch):
    """Refused by core, with the module assumed stale (no flags at all)."""
    monkeypatch.setattr(
        "carpenter.chat_tool_loader.get_loaded_tools", lambda: {},
    )
    out = _dispatch(executor_arc_id=7)
    assert executed == []
    assert "only be called from a chat conversation" in out
    assert "arc agent" in out


def test_refused_for_an_arc_even_with_a_confirmation_handler(executed):
    """A registered handler is not consent from the person in the chat."""
    set_confirmation_handler(lambda name, inp: True)
    out = _dispatch(executor_arc_id=7)
    assert executed == []
    assert "arc agent" in out


def test_the_chat_agent_reaches_execution(executed, monkeypatch):
    monkeypatch.setattr(
        "carpenter.chat_tool_loader.get_loaded_tools", lambda: {},
    )
    assert _dispatch() == "executed"
    assert executed == [(TOOL, None)]


# ── the offered tool set ────────────────────────────────────────────

def test_arc_agents_are_not_offered_chat_only_tools():
    tools = [{"name": n} for n in sorted(CHAT_ONLY_TOOLS | {"read_file"})]
    assert invocation._drop_chat_only_tools(tools) == [{"name": "read_file"}]


def test_the_operator_only_drop_leaves_chat_only_tools_alone():
    """The two drops are separate on purpose; one must not imply the other."""
    tools = [{"name": n} for n in sorted(CHAT_ONLY_TOOLS | {"read_file"})]
    assert invocation._drop_operator_only_tools(tools) == tools


def test_still_offered_to_the_chat_agent_without_a_confirmation_handler(
    monkeypatch,
):
    """The regression that makes the whole design work.

    No confirmation handler is registered on this platform, which is why
    these tools are not in ``OPERATOR_ONLY_TOOLS``: that set is dropped
    for every agent in that case.
    """
    from carpenter import chat_tool_loader
    defs = [
        dict(TOOL_DEF),
        {"name": "read_file", "description": "r",
         "input_schema": {"type": "object"}},
    ]
    monkeypatch.setattr(
        chat_tool_loader, "get_tool_defs_for_api", lambda: defs,
    )
    set_confirmation_handler(None)
    try:
        names = {t["name"] for t in invocation._select_chat_tools(200_000)}
    finally:
        set_confirmation_handler(None)
    assert TOOL in names and "read_file" in names

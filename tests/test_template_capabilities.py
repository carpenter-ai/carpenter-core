"""Tests for template capability grants.

Covers:
- Capability registry definitions
- YAML parsing with capabilities at template and step level
- Capability storage in arc_state during instantiation
- Template-level + step-level capability merging
- Tool whitelist augmentation via capabilities
- Scope bypass for system.read capability
- Negative cases (arc without capability is still blocked)
"""

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta

import pytest
import yaml

from carpenter.executor.dispatch_bridge import validate_and_dispatch, DispatchError
from carpenter.core.engine import template_manager
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.trust.capabilities import (
    CAPABILITY_TOOL_GRANTS,
    SCOPE_BYPASS_CAPABILITIES,
    get_arc_capabilities,
    resolve_capability_tools,
)
from carpenter.db import get_db


# ── Registry unit tests ─────────────────────────────────────────────

def test_capability_tool_grants_structure():
    """All capability grants map to non-empty frozensets of tool names."""
    assert len(CAPABILITY_TOOL_GRANTS) > 0
    for cap, tools in CAPABILITY_TOOL_GRANTS.items():
        assert isinstance(cap, str), f"Key {cap!r} should be a string"
        assert isinstance(tools, frozenset), f"Value for {cap!r} should be frozenset"
        assert len(tools) > 0, f"Capability {cap!r} should grant at least one tool"


def test_scope_bypass_capabilities_structure():
    assert isinstance(SCOPE_BYPASS_CAPABILITIES, frozenset)
    assert "system.read" in SCOPE_BYPASS_CAPABILITIES


def test_resolve_capability_tools_empty():
    assert resolve_capability_tools(set()) == frozenset()


def test_resolve_capability_tools_single():
    result = resolve_capability_tools({"kb.write"})
    assert result == CAPABILITY_TOOL_GRANTS["kb.write"]


def test_resolve_capability_tools_union():
    """Multiple capabilities produce the union of all granted tools."""
    result = resolve_capability_tools({"kb.write", "kb.read"})
    expected = CAPABILITY_TOOL_GRANTS["kb.write"] | CAPABILITY_TOOL_GRANTS["kb.read"]
    assert result == expected


def test_resolve_capability_tools_unknown_cap():
    """Unknown capability names are silently ignored."""
    result = resolve_capability_tools({"nonexistent.cap"})
    assert result == frozenset()


# ── arc_state read/write ────────────────────────────────────────────

def test_get_arc_capabilities_empty():
    """Arc without _capabilities returns empty set."""
    arc_id = arc_manager.create_arc("test-no-caps", goal="No caps")
    assert get_arc_capabilities(arc_id) == set()


def test_get_arc_capabilities_stored():
    """Capabilities stored in arc_state are retrieved correctly."""
    arc_id = arc_manager.create_arc("test-with-caps", goal="Has caps")
    db = get_db()
    try:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
            (arc_id, "_capabilities", json.dumps(["kb.write", "system.read"])),
        )
        db.commit()
    finally:
        db.close()

    caps = get_arc_capabilities(arc_id)
    assert caps == {"kb.write", "system.read"}


def test_get_arc_capabilities_invalid_json():
    """Invalid JSON in arc_state returns empty set (doesn't crash)."""
    arc_id = arc_manager.create_arc("test-bad-json", goal="Bad json")
    db = get_db()
    try:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
            (arc_id, "_capabilities", "not valid json"),
        )
        db.commit()
    finally:
        db.close()

    caps = get_arc_capabilities(arc_id)
    assert caps == set()


# ── YAML parsing and template instantiation ─────────────────────────

def _write_template_yaml(tmp_path, data):
    """Write a YAML template file and return its path."""
    path = os.path.join(str(tmp_path), "test-template.yaml")
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


def test_load_template_with_capabilities(tmp_path):
    """Template-level capabilities are preserved in steps_json."""
    yaml_path = _write_template_yaml(tmp_path, {
        "name": "caps-test",
        "description": "Test template with capabilities",
        "capabilities": ["system.read"],
        "steps": [
            {"name": "step-1", "description": "First step", "order": 0},
        ],
    })

    tid = template_manager.load_template(yaml_path)
    template = template_manager.get_template(tid)

    assert template["capabilities"] == ["system.read"]
    assert len(template["steps"]) == 1


def test_load_template_without_capabilities(tmp_path):
    """Templates without capabilities default to empty list."""
    yaml_path = _write_template_yaml(tmp_path, {
        "name": "no-caps-test",
        "steps": [
            {"name": "step-1", "order": 0},
        ],
    })

    tid = template_manager.load_template(yaml_path)
    template = template_manager.get_template(tid)

    assert template["capabilities"] == []


def test_instantiate_template_stores_capabilities(tmp_path):
    """Instantiation stores merged capabilities in arc_state._capabilities."""
    yaml_path = _write_template_yaml(tmp_path, {
        "name": "caps-instantiate",
        "capabilities": ["system.read"],
        "steps": [
            {
                "name": "gather",
                "description": "Gather data",
                "order": 0,
                "capabilities": ["kb.write"],
                "agent_type": "EXECUTOR",
            },
        ],
    })

    tid = template_manager.load_template(yaml_path)
    parent_id = arc_manager.create_arc("parent", goal="Test")
    arc_ids = template_manager.instantiate_template(tid, parent_id)
    assert len(arc_ids) == 1

    caps = get_arc_capabilities(arc_ids[0])
    # Should have both template-level and step-level capabilities
    assert caps == {"system.read", "kb.write"}


def test_instantiate_template_only_template_caps(tmp_path):
    """Step without its own capabilities inherits template-level only."""
    yaml_path = _write_template_yaml(tmp_path, {
        "name": "template-only-caps",
        "capabilities": ["system.read"],
        "steps": [
            {"name": "step-1", "order": 0, "agent_type": "PLANNER"},
        ],
    })

    tid = template_manager.load_template(yaml_path)
    parent_id = arc_manager.create_arc("parent", goal="Test")
    arc_ids = template_manager.instantiate_template(tid, parent_id)

    caps = get_arc_capabilities(arc_ids[0])
    assert caps == {"system.read"}


def test_instantiate_template_only_step_caps(tmp_path):
    """Template without capabilities, step with capabilities."""
    yaml_path = _write_template_yaml(tmp_path, {
        "name": "step-only-caps",
        "steps": [
            {
                "name": "writer",
                "order": 0,
                "capabilities": ["kb.write"],
                "agent_type": "PLANNER",
            },
        ],
    })

    tid = template_manager.load_template(yaml_path)
    parent_id = arc_manager.create_arc("parent", goal="Test")
    arc_ids = template_manager.instantiate_template(tid, parent_id)

    caps = get_arc_capabilities(arc_ids[0])
    assert caps == {"kb.write"}


def test_instantiate_template_no_caps_no_state(tmp_path):
    """Template and step without capabilities — no _capabilities in arc_state."""
    yaml_path = _write_template_yaml(tmp_path, {
        "name": "no-caps-at-all",
        "steps": [
            {"name": "plain-step", "order": 0},
        ],
    })

    tid = template_manager.load_template(yaml_path)
    parent_id = arc_manager.create_arc("parent", goal="Test")
    arc_ids = template_manager.instantiate_template(tid, parent_id)

    caps = get_arc_capabilities(arc_ids[0])
    assert caps == set()


def test_instantiate_deduplicates_capabilities(tmp_path):
    """Duplicate capabilities between template and step are deduplicated."""
    yaml_path = _write_template_yaml(tmp_path, {
        "name": "dedup-caps",
        "capabilities": ["system.read", "kb.write"],
        "steps": [
            {
                "name": "both",
                "order": 0,
                "capabilities": ["kb.write", "kb.read"],
            },
        ],
    })

    tid = template_manager.load_template(yaml_path)
    parent_id = arc_manager.create_arc("parent", goal="Test")
    arc_ids = template_manager.instantiate_template(tid, parent_id)

    caps = get_arc_capabilities(arc_ids[0])
    assert caps == {"system.read", "kb.write", "kb.read"}


def test_instantiate_multi_step_different_caps(tmp_path):
    """Each step gets its own merged capabilities."""
    yaml_path = _write_template_yaml(tmp_path, {
        "name": "multi-step-caps",
        "capabilities": ["system.read"],
        "steps": [
            {"name": "reader", "order": 0, "agent_type": "PLANNER"},
            {
                "name": "writer",
                "order": 1,
                "capabilities": ["kb.write"],
                "agent_type": "PLANNER",
            },
        ],
    })

    tid = template_manager.load_template(yaml_path)
    parent_id = arc_manager.create_arc("parent", goal="Test")
    arc_ids = template_manager.instantiate_template(tid, parent_id)
    assert len(arc_ids) == 2

    # First step: only template-level
    caps0 = get_arc_capabilities(arc_ids[0])
    assert caps0 == {"system.read"}

    # Second step: template + step
    caps1 = get_arc_capabilities(arc_ids[1])
    assert caps1 == {"system.read", "kb.write"}


# ── Dispatch enforcement: tool whitelist augmentation ────────────────


def _create_planner_arc(arc_id=None, capabilities=None):
    """Create a PLANNER arc and optionally store capabilities."""
    aid = arc_manager.create_arc("planner-test", goal="Test", agent_type="PLANNER")
    if capabilities:
        db = get_db()
        try:
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
                (aid, "_capabilities", json.dumps(capabilities)),
            )
            db.commit()
        finally:
            db.close()
    return aid


def test_planner_blocked_without_capability():
    """PLANNER arc without kb.write capability cannot call kb.add."""
    arc_id = _create_planner_arc()
    _create_reviewed_session("planner-test-session")

    with pytest.raises(DispatchError, match="(?i)planner"):
        validate_and_dispatch(
            "kb.add",
            {"_caller_arc_id": arc_id, "arc_id": arc_id,
             "path": "test/entry", "content": "hello"},
            session_id="planner-test-session",
        )


def test_planner_allowed_with_kb_write_capability():
    """PLANNER arc with kb.write capability can call kb.add."""
    arc_id = _create_planner_arc(capabilities=["kb.write"])
    _create_reviewed_session("planner-kb-session")

    # Should pass the agent-type check (may fail in the KB handler itself,
    # but that's OK — we're testing the whitelist augmentation, not the handler)
    try:
        result = validate_and_dispatch(
            "kb.add",
            {"_caller_arc_id": arc_id, "arc_id": arc_id,
             "path": "test/entry", "content": "hello"},
            session_id="planner-kb-session",
        )
    except DispatchError as e:
        # Should NOT be a PLANNER restriction error
        assert "PLANNER" not in str(e)


def test_planner_still_blocked_for_ungranted_tools():
    """PLANNER arc with kb.write cannot call web.get (not in granted tools)."""
    arc_id = _create_planner_arc(capabilities=["kb.write"])
    _create_reviewed_session("planner-web-session")

    with pytest.raises(DispatchError):
        validate_and_dispatch(
            "web.get",
            {"_caller_arc_id": arc_id, "arc_id": arc_id,
             "url": "http://example.com"},
            session_id="planner-web-session",
        )


# ── Dispatch enforcement: scope bypass ───────────────────────────────

def _create_reviewed_session(session_id):
    """Create a valid reviewed execution session."""
    db = get_db()
    try:
        cursor = db.execute(
            "INSERT INTO code_files (file_path, source, review_status) VALUES (?, ?, ?)",
            ("/tmp/test.py", "test", "approved"),
        )
        code_file_id = cursor.lastrowid
        cursor = db.execute(
            "INSERT INTO code_executions (code_file_id, execution_status, started_at) "
            "VALUES (?, 'running', ?)",
            (code_file_id, datetime.now(timezone.utc).isoformat()),
        )
        execution_id = cursor.lastrowid
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        db.execute(
            "INSERT INTO execution_sessions "
            "(session_id, code_file_id, execution_id, reviewed, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, code_file_id, execution_id, True, expires_at.isoformat()),
        )
        db.commit()
    finally:
        db.close()
    return session_id


def test_cross_arc_read_blocked_without_system_read():
    """Arc without system.read cannot read state from non-descendant arc."""
    # Create two unrelated arcs (neither is ancestor of the other)
    caller_id = arc_manager.create_arc("caller", goal="Caller")
    target_id = arc_manager.create_arc("target", goal="Target")

    # Set some state on target
    db = get_db()
    try:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
            (target_id, "secret", json.dumps("data")),
        )
        db.commit()
    finally:
        db.close()

    with pytest.raises(DispatchError, match="descendant"):
        validate_and_dispatch(
            "state.get",
            {
                "arc_id": caller_id,
                "_caller_arc_id": caller_id,
                "_target_arc_id": target_id,
                "key": "secret",
            },
        )


def test_cross_arc_read_allowed_with_system_read():
    """Arc with system.read capability can read state from any trusted arc."""
    # Create two unrelated arcs
    caller_id = arc_manager.create_arc("caller-sysread", goal="Caller")
    target_id = arc_manager.create_arc("target-sysread", goal="Target")

    # Give caller system.read capability
    db = get_db()
    try:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
            (caller_id, "_capabilities", json.dumps(["system.read"])),
        )
        # Set state on target
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?)",
            (target_id, "readable", json.dumps("hello")),
        )
        db.commit()
    finally:
        db.close()

    result = validate_and_dispatch(
        "state.get",
        {
            "arc_id": caller_id,
            "_caller_arc_id": caller_id,
            "_target_arc_id": target_id,
            "key": "readable",
        },
    )
    assert result["value"] == "hello"


# ── Backward compatibility ──────────────────────────────────────────

def test_legacy_steps_json_format():
    """Templates stored in old plain-list format still parse correctly."""
    # Simulate legacy format by inserting directly into DB
    db = get_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cursor = db.execute(
            "INSERT INTO workflow_templates "
            "(name, description, yaml_path, required_for_json, steps_json, version, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-template", "Old format", "/tmp/legacy.yaml",
                json.dumps([]), json.dumps([{"name": "old-step", "order": 0}]),
                1, now,
            ),
        )
        tid = cursor.lastrowid
        db.commit()
    finally:
        db.close()

    template = template_manager.get_template(tid)
    assert template["steps"] == [{"name": "old-step", "order": 0}]
    assert template["capabilities"] == []


def test_get_template_by_name_includes_capabilities(tmp_path):
    """get_template_by_name also returns capabilities."""
    yaml_path = _write_template_yaml(tmp_path, {
        "name": "named-caps",
        "capabilities": ["kb.read"],
        "steps": [{"name": "s1", "order": 0}],
    })

    template_manager.load_template(yaml_path)
    template = template_manager.get_template_by_name("named-caps")

    assert template is not None
    assert template["capabilities"] == ["kb.read"]


def test_list_templates_includes_capabilities(tmp_path):
    """list_templates also returns capabilities for each template."""
    yaml_path = _write_template_yaml(tmp_path, {
        "name": "listed-caps",
        "capabilities": ["system.read"],
        "steps": [{"name": "s1", "order": 0}],
    })

    template_manager.load_template(yaml_path)
    templates = template_manager.list_templates()

    found = [t for t in templates if t["name"] == "listed-caps"]
    assert len(found) == 1
    assert found[0]["capabilities"] == ["system.read"]

"""Per-key human-gate predicate in ``config_tool.handle_set_value``.

Covers the gate added by PR 4 of the platform-integrity rollout.  The
gate refuses ``set_value`` for keys matching
``_HUMAN_GATED_KEYS`` even if they appear in ``_MUTABLE_KEYS`` — a
forward-looking invariant so a future PR can't quietly add a
``platform_integrity.*`` key to the allowlist and bypass human review.

The mutable allowlist and the gated glob list are deliberately disjoint
today; the monkeypatched-allowlist tests below exercise the cross.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from carpenter import config as carpenter_config
from carpenter.core.trust.audit import get_trust_events
from carpenter.tool_backends import config_tool


def _audit_events_for(event_type: str):
    return get_trust_events(event_type=f"integrity.{event_type}", limit=100)


def _seed_yaml(tmp_path: Path, monkeypatch) -> Path:
    """Point config_module at a writeable per-test yaml file."""
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("base_dir: " + str(tmp_path) + "\n")
    # config_module.handle_set_value falls back to _loaded_yaml_path
    # when it's set; set it so we don't write into the real config.
    monkeypatch.setattr(
        carpenter_config, "_loaded_yaml_path", str(yaml_path), raising=False,
    )
    return yaml_path


# ── Forward-looking gate predicate ──────────────────────────────────────


def test_set_value_on_normal_mutable_key_still_works(tmp_path, monkeypatch):
    """A regular mutable key (chat_language) is unaffected by the gate."""
    yaml_path = _seed_yaml(tmp_path, monkeypatch)

    result = config_tool.handle_set_value({"key": "chat_language", "value": "de"})
    assert result["status"] == "ok"
    assert result["key"] == "chat_language"
    assert result["value"] == "de"

    # YAML actually written.
    text = yaml_path.read_text()
    assert "chat_language" in text and "de" in text


def test_set_value_on_non_mutable_key_still_fails(tmp_path, monkeypatch):
    """``review_auto_approve_threshold`` is not in _MUTABLE_KEYS — the
    pre-existing allowlist check still rejects it (not the new gate)."""
    _seed_yaml(tmp_path, monkeypatch)

    with pytest.raises(ValueError) as exc:
        config_tool.handle_set_value({
            "key": "review_auto_approve_threshold", "value": 1.0,
        })
    # Pre-existing message: "not in the mutable-key allowlist".
    assert "mutable-key allowlist" in str(exc.value)


def test_set_value_on_platform_integrity_key_is_gated(tmp_path, monkeypatch):
    """Monkeypatched _MUTABLE_KEYS containing a platform_integrity.* key:
    the gate raises a ValueError citing the human-gated message — NOT
    the allowlist message."""
    _seed_yaml(tmp_path, monkeypatch)

    extended = set(config_tool._MUTABLE_KEYS) | {"platform_integrity.path_overrides"}
    monkeypatch.setattr(config_tool, "_MUTABLE_KEYS", frozenset(extended))

    with pytest.raises(ValueError) as exc:
        config_tool.handle_set_value({
            "key": "platform_integrity.path_overrides", "value": [],
        })
    assert "human-gated" in str(exc.value)
    assert "platform-integrity workflow" in str(exc.value)

    rows = _audit_events_for("config_set_refused")
    assert any(
        (r.get("details") or {}).get("key") == "platform_integrity.path_overrides"
        for r in rows
    ), rows


def test_set_value_on_capability_matrix_key_is_gated(tmp_path, monkeypatch):
    """Monkeypatched _MUTABLE_KEYS containing capability_matrix.foo:
    the gate raises ValueError citing the human-gated message."""
    _seed_yaml(tmp_path, monkeypatch)

    extended = set(config_tool._MUTABLE_KEYS) | {"capability_matrix.foo"}
    monkeypatch.setattr(config_tool, "_MUTABLE_KEYS", frozenset(extended))

    with pytest.raises(ValueError) as exc:
        config_tool.handle_set_value({"key": "capability_matrix.foo", "value": True})
    assert "human-gated" in str(exc.value)

    rows = _audit_events_for("config_set_refused")
    assert any(
        (r.get("details") or {}).get("key") == "capability_matrix.foo"
        for r in rows
    ), rows


def test_human_gated_globs_do_not_overlap_mutable_keys():
    """Pin the invariant: no current _MUTABLE_KEYS entry matches a
    human-gated glob.  If a future PR adds e.g. ``review.something`` to
    the allowlist, this test fires before the gate refuses it at runtime
    — surfacing the conflict in CI rather than at first invocation."""
    overlaps = [
        key for key in config_tool._MUTABLE_KEYS
        if config_tool._is_human_gated(key)
    ]
    assert overlaps == [], (
        f"_MUTABLE_KEYS entries match human-gated globs: {overlaps} — "
        "either remove them from _MUTABLE_KEYS or explicitly carve them "
        "out of _HUMAN_GATED_KEYS"
    )

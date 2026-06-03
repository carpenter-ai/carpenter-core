"""Unit tests for the structured-action parser (PR 7 close-out).

Pins the new contract: ``parse_proposed_actions`` returns a list of
dicts shaped ``{"description": str, "target_path": str | None}``.

The parser package lives under ``config_seed/templates/reflection/``;
import is via the synthetic ``carpenter_template_packages.reflection``
namespace registered by the template loader.  These tests load the
package once per test via a small helper.
"""
from __future__ import annotations

import importlib
import os
import shutil

import pytest

from carpenter.core.engine import template_manager


TEMPLATES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "config_seed", "templates",
)


def _copy_seed(tmp_path):
    dest = str(tmp_path / "templates")
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(TEMPLATES_DIR):
        src = os.path.join(TEMPLATES_DIR, f)
        if os.path.isfile(src) and f.endswith((".yaml", ".yml")):
            shutil.copy(src, dest)
        elif os.path.isdir(src) and not f.startswith((".", "_")):
            shutil.copytree(src, os.path.join(dest, f))
    return dest


def _load_parser(tmp_path):
    dest = _copy_seed(tmp_path)
    template_manager.load_templates_from_dir(dest)
    parser = importlib.import_module(
        "carpenter_template_packages.reflection.proposed_action_parser",
    )
    importlib.reload(parser)
    return parser


# ── Shape contract ───────────────────────────────────────────────────────


def test_returns_list_of_dicts(tmp_path):
    parser = _load_parser(tmp_path)
    result = parser.parse_proposed_actions("- write docs\n- fix tests\n")
    assert isinstance(result, list)
    assert all(isinstance(a, dict) for a in result)
    assert all("description" in a and "target_path" in a for a in result)


def test_empty_input_returns_empty_list(tmp_path):
    parser = _load_parser(tmp_path)
    assert parser.parse_proposed_actions(None) == []
    assert parser.parse_proposed_actions("") == []
    assert parser.parse_proposed_actions("   ") == []


# ── Target-path extraction ───────────────────────────────────────────────


def test_extracts_relative_kb_path_from_backticks(tmp_path):
    parser = _load_parser(tmp_path)
    actions = parser.parse_proposed_actions(
        "- Update `kb/error-budgets.md` with the new policy\n"
    )
    assert len(actions) == 1
    assert actions[0]["description"].startswith("Update")
    assert actions[0]["target_path"] == "kb/error-budgets.md"


def test_extracts_absolute_python_path_from_backticks(tmp_path):
    parser = _load_parser(tmp_path)
    actions = parser.parse_proposed_actions(
        "- Refactor `/repo/carpenter/security/judge.py` for clarity\n"
    )
    assert actions[0]["target_path"] == "/repo/carpenter/security/judge.py"


def test_no_backtick_path_yields_none(tmp_path):
    parser = _load_parser(tmp_path)
    actions = parser.parse_proposed_actions(
        "- Think harder about the algorithm\n"
    )
    assert actions[0]["target_path"] is None


def test_backticks_without_extension_yield_none(tmp_path):
    parser = _load_parser(tmp_path)
    # Backticked literal but not path-like (no extension dot)
    actions = parser.parse_proposed_actions(
        "- Update the `LoginManager` class\n"
    )
    assert actions[0]["target_path"] is None


def test_first_backticked_path_wins(tmp_path):
    parser = _load_parser(tmp_path)
    actions = parser.parse_proposed_actions(
        "- Move `src/a.py` content into `src/b.py`\n"
    )
    assert actions[0]["target_path"] == "src/a.py"


def test_mixed_actions_per_item_independence(tmp_path):
    parser = _load_parser(tmp_path)
    text = (
        "- Update `kb/foo.md` with X\n"
        "- Generic improvement to the system\n"
        "- Touch `path/to/bar.yaml`\n"
    )
    actions = parser.parse_proposed_actions(text)
    assert len(actions) == 3
    assert actions[0]["target_path"] == "kb/foo.md"
    assert actions[1]["target_path"] is None
    assert actions[2]["target_path"] == "path/to/bar.yaml"


# ── Bullet-prefix stripping preserved ────────────────────────────────────


def test_strips_bullet_prefixes(tmp_path):
    parser = _load_parser(tmp_path)
    text = (
        "- bullet a\n"
        "* asterisk b\n"
        "1. numbered c\n"
        "10. multi-digit d\n"
    )
    actions = parser.parse_proposed_actions(text)
    descs = [a["description"] for a in actions]
    assert descs == ["bullet a", "asterisk b", "numbered c", "multi-digit d"]


# ── JSON inputs ──────────────────────────────────────────────────────────


def test_json_list_of_strings(tmp_path):
    parser = _load_parser(tmp_path)
    import json
    text = json.dumps([
        "Update `kb/x.md`",
        "Implement `feature/y.py`",
    ])
    actions = parser.parse_proposed_actions(text)
    assert len(actions) == 2
    assert actions[0]["target_path"] == "kb/x.md"
    assert actions[1]["target_path"] == "feature/y.py"


def test_json_list_of_objects_passthrough(tmp_path):
    parser = _load_parser(tmp_path)
    import json
    text = json.dumps([
        {"description": "Edit it", "target_path": "/abs/path/file.yaml"},
        {"description": "Just think"},
    ])
    actions = parser.parse_proposed_actions(text)
    assert actions[0]["target_path"] == "/abs/path/file.yaml"
    assert actions[1]["target_path"] is None


def test_json_object_with_missing_target_falls_back_to_extraction(tmp_path):
    parser = _load_parser(tmp_path)
    import json
    text = json.dumps([
        {"description": "Touch `dir/a.md`"},
    ])
    actions = parser.parse_proposed_actions(text)
    assert actions[0]["target_path"] == "dir/a.md"


def test_json_skips_empty_descriptions(tmp_path):
    parser = _load_parser(tmp_path)
    import json
    text = json.dumps([
        {"description": ""},
        "real action `f.py`",
    ])
    actions = parser.parse_proposed_actions(text)
    assert len(actions) == 1
    assert actions[0]["target_path"] == "f.py"

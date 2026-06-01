"""Tests for the Resources nudge appended to ``read_arc_result`` output (PR4)."""

import importlib.util
import sys
from pathlib import Path

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager
from carpenter.core.workflows._arc_state import set_arc_state


_SEED_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "config_seed" / "chat_tools" / "arcs.py"
)


def _load_read_arc_result():
    spec = importlib.util.spec_from_file_location(
        "config_seed_chat_tools_arcs_pr4test", str(_SEED_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.read_arc_result


read_arc_result = _load_read_arc_result()


def _completed_arc(name="a", response="done") -> int:
    arc_id = arc_manager.create_arc(name=name, integrity_level="trusted")
    arc_manager.update_status(arc_id, "active")
    set_arc_state(arc_id, "_agent_response", response)
    arc_manager.update_status(arc_id, "completed")
    return arc_id


def _link_trusted_resource(arc_id, tmp_path, body="trusted body") -> int:
    fp = tmp_path / f"tr-{arc_id}.txt"
    fp.write_text(body, encoding="utf-8")
    rid = res_manager.derive_resource(
        content_type="text-summary",
        file_path=str(fp),
        produced_by_arc_id=arc_id,
        produced_by_template="html_to_summary",
        template_verdict="approved",
        byte_size=len(body.encode("utf-8")),
    )
    res_manager.link_arc_resource(
        arc_id=arc_id, resource_id=rid, role="output"
    )
    return rid


def _link_untrusted_resource(arc_id, tmp_path, body="raw") -> int:
    fp = tmp_path / f"un-{arc_id}.html"
    fp.write_text(body, encoding="utf-8")
    rid = res_manager.create_resource(
        content_type="html",
        file_path=str(fp),
        produced_by_arc_id=arc_id,
    )
    res_manager.link_arc_resource(
        arc_id=arc_id, resource_id=rid, role="output"
    )
    return rid


def test_arc_with_trusted_resource_gets_nudge(tmp_path):
    arc_id = _completed_arc(response="ok")
    rid = _link_trusted_resource(arc_id, tmp_path)
    out = read_arc_result({"arc_id": arc_id})
    assert "ok" in out  # body preserved
    assert f"Resources associated with arc #{arc_id}" in out
    assert f"Resource #{rid}" in out
    assert "trusted" in out
    assert f"read_resource({rid})" in out


def test_arc_without_resources_has_no_nudge():
    arc_id = _completed_arc(response="plain body")
    out = read_arc_result({"arc_id": arc_id})
    assert out == "plain body"  # byte-for-byte preserved
    assert "Resources associated" not in out


def test_mix_trusted_and_untrusted(tmp_path):
    arc_id = _completed_arc(response="mixed")
    trusted_id = _link_trusted_resource(arc_id, tmp_path, body="t-body")
    untrusted_id = _link_untrusted_resource(arc_id, tmp_path, body="u-body")

    out = read_arc_result({"arc_id": arc_id})

    # Both resources are listed
    assert f"Resource #{trusted_id}" in out
    assert f"Resource #{untrusted_id}" in out

    # Trusted gets actionable nudge; untrusted is marked not readable
    trusted_line = next(
        ln for ln in out.splitlines()
        if f"Resource #{trusted_id}" in ln
    )
    assert "trusted" in trusted_line
    assert f"read_resource({trusted_id})" in trusted_line

    untrusted_line = next(
        ln for ln in out.splitlines()
        if f"Resource #{untrusted_id}" in ln
    )
    assert "untrusted" in untrusted_line
    assert "not readable from chat" in untrusted_line


def test_nudge_preserves_pagination_header(tmp_path):
    # Build a long response that triggers pagination.
    body = "X" * 10_000
    arc_id = _completed_arc(response=body)
    _link_trusted_resource(arc_id, tmp_path, body="summary")

    out = read_arc_result({"arc_id": arc_id, "offset": 0, "limit": 100})
    # Pagination header survives
    assert out.startswith("[Showing characters 0-100 of 10000")
    # And the Resources nudge is appended at the end
    assert "Resources associated" in out

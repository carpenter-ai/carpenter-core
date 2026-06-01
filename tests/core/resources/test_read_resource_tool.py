"""Tests for the ``read_resource`` chat tool (PR4).

The tool lives at ``config_seed/chat_tools/resources.py``.  Because the
chat-tool loader imports that file dynamically from a user config
directory, we import the handler function directly via
``importlib`` on the source file rather than through the loader.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager
from carpenter.db import db_transaction


_SEED_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "config_seed" / "chat_tools" / "resources.py"
)


def _load_read_resource():
    spec = importlib.util.spec_from_file_location(
        "config_seed_chat_tools_resources_pr4test", str(_SEED_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.read_resource


read_resource = _load_read_resource()


def _write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


def _trusted_arc(name="arc") -> int:
    return arc_manager.create_arc(name=name, integrity_level="trusted")


def _make_trusted_resource(tmp_path, body="hello world") -> int:
    arc = _trusted_arc()
    fp = _write(tmp_path, "t.txt", body)
    rid = res_manager.derive_resource(
        content_type="text-summary",
        file_path=fp,
        produced_by_arc_id=arc,
        produced_by_template="html_to_summary",
        template_verdict="pending",
        byte_size=len(body.encode("utf-8")),
    )
    res_manager.mark_template_verdict(rid, "approved")
    return rid


def test_happy_path_trusted_resource_returns_content(tmp_path):
    body = "the quick brown fox"
    rid = _make_trusted_resource(tmp_path, body)
    out = read_resource({"resource_id": rid})
    assert body in out
    assert f"Resource #{rid}" in out
    assert "content_type=text-summary" in out
    assert f"byte_size={len(body.encode('utf-8'))}" in out
    assert "offset=0" in out


def test_untrusted_raw_resource_refused(tmp_path):
    fp = _write(tmp_path, "raw.html", "<html>secret</html>")
    rid = res_manager.create_resource(
        content_type="html",
        file_path=fp,
        produced_by_arc_id=None,
    )
    out = read_resource({"resource_id": rid})
    assert "untrusted" in out.lower()
    assert "<html>" not in out  # content must not leak
    assert "secret" not in out
    assert f"Resource #{rid}" in out


def test_pending_verdict_resource_refused(tmp_path):
    arc = _trusted_arc()
    fp = _write(tmp_path, "p.txt", "pending data")
    rid = res_manager.derive_resource(
        content_type="text-summary",
        file_path=fp,
        produced_by_arc_id=arc,
        produced_by_template="html_to_summary",
        template_verdict="pending",
    )
    out = read_resource({"resource_id": rid})
    assert "untrusted" in out.lower()
    assert "pending data" not in out
    assert "template_verdict=pending" in out


def test_rejected_resource_refused(tmp_path):
    arc = _trusted_arc()
    fp = _write(tmp_path, "r.txt", "rejected body")
    rid = res_manager.derive_resource(
        content_type="text-summary",
        file_path=fp,
        produced_by_arc_id=arc,
        produced_by_template="html_to_summary",
        template_verdict="rejected",
    )
    out = read_resource({"resource_id": rid})
    assert "untrusted" in out.lower()
    assert "rejected body" not in out
    assert "template_verdict=rejected" in out


def test_deleted_resource_returns_cleanup_message(tmp_path):
    rid = _make_trusted_resource(tmp_path, "gone")
    with db_transaction() as db:
        db.execute(
            "UPDATE resources SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
            (rid,),
        )
    out = read_resource({"resource_id": rid})
    assert "cleaned up" in out.lower() or "deleted" in out.lower()
    assert f"Resource #{rid}" in out
    assert "gone" not in out  # no content leak


def test_nonexistent_id_clean_error():
    out = read_resource({"resource_id": 999_999_999})
    assert "not found" in out.lower()


def test_pagination_via_offset_limit(tmp_path):
    body = "abcdefghijklmnop"  # 16 chars
    rid = _make_trusted_resource(tmp_path, body)
    out = read_resource({"resource_id": rid, "offset": 4, "limit": 5})
    # Content slice should appear
    assert "efghi" in out
    assert "offset=4" in out
    assert "limit=5" in out
    # byte_size==16, offset+limit==9, so 7 bytes remain -> nudge
    assert "more" in out.lower()


def test_no_more_nudge_when_read_fits(tmp_path):
    body = "short"
    rid = _make_trusted_resource(tmp_path, body)
    out = read_resource({"resource_id": rid, "offset": 0, "limit": 1000})
    assert "short" in out
    # No "more=" remaining when the whole blob fit
    assert "more=" not in out


def test_default_offset_and_limit(tmp_path):
    body = "default-path-test"
    rid = _make_trusted_resource(tmp_path, body)
    # Only resource_id provided — offset/limit defaults should apply.
    out = read_resource({"resource_id": rid})
    assert body in out
    assert "offset=0" in out
    assert "limit=50000" in out

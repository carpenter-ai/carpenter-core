"""Tests for ``read_resource_content``.

``caller_arc_id`` is mandatory and keyword-only.  Tests that don't
exercise the trust gate pass ``caller_arc_id=None`` (chat / platform /
test context — no arc-dispatch gate fires).  Tests that do exercise
the gate pass an explicit arc id; a defence-in-depth gate rejects
trusted arcs trying to read untrusted Resources.
"""

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager
from carpenter.db import db_transaction


def _make_file(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_reads_full_content(tmp_path):
    file_path = _make_file(tmp_path, "a.html", "hello world")
    rid = res_manager.create_resource(
        content_type="text/html", file_path=file_path, produced_by_arc_id=None,
    )
    assert res_manager.read_resource_content(rid, caller_arc_id=None) == "hello world"


def test_reads_slice(tmp_path):
    file_path = _make_file(tmp_path, "a.txt", "abcdefghij")
    rid = res_manager.create_resource(
        content_type="text/plain", file_path=file_path, produced_by_arc_id=None,
    )
    assert (
        res_manager.read_resource_content(rid, offset=3, limit=4, caller_arc_id=None)
        == "defg"
    )


def test_raises_when_missing_resource():
    with pytest.raises(FileNotFoundError):
        res_manager.read_resource_content(999_999, caller_arc_id=None)


def test_raises_when_file_path_null():
    rid = res_manager.create_resource(
        content_type="text/plain", file_path=None, produced_by_arc_id=None,
    )
    with pytest.raises(FileNotFoundError, match="file_path"):
        res_manager.read_resource_content(rid, caller_arc_id=None)


def test_raises_after_deleted_at_set(tmp_path):
    file_path = _make_file(tmp_path, "b.html", "body")
    rid = res_manager.create_resource(
        content_type="text/html", file_path=file_path, produced_by_arc_id=None,
    )
    # Directly set deleted_at — PR5's sweep job will wire a higher-level
    # function; for PR1 we just verify the read check.
    with db_transaction() as db:
        db.execute(
            "UPDATE resources SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
            (rid,),
        )
    with pytest.raises(FileNotFoundError, match="deleted"):
        res_manager.read_resource_content(rid, caller_arc_id=None)


def test_raises_when_file_missing_on_disk(tmp_path):
    missing = tmp_path / "gone.html"
    rid = res_manager.create_resource(
        content_type="text/html", file_path=str(missing), produced_by_arc_id=None,
    )
    with pytest.raises(FileNotFoundError, match="missing on disk"):
        res_manager.read_resource_content(rid, caller_arc_id=None)


def test_does_not_enforce_trust(tmp_path):
    """Raw (untrusted) resource still reads — trust is a caller concern."""
    file_path = _make_file(tmp_path, "untrusted.html", "content")
    rid = res_manager.create_resource(
        content_type="text/html", file_path=file_path, produced_by_arc_id=None,
    )
    from carpenter.core.resources.trust import is_trusted
    assert is_trusted(rid) is False
    # But read still works.
    assert res_manager.read_resource_content(rid, caller_arc_id=None) == "content"


def test_negative_offset_raises(tmp_path):
    file_path = _make_file(tmp_path, "x.txt", "abc")
    rid = res_manager.create_resource(
        content_type="text/plain", file_path=file_path, produced_by_arc_id=None,
    )
    with pytest.raises(ValueError, match="offset"):
        res_manager.read_resource_content(rid, offset=-1, caller_arc_id=None)


def test_negative_limit_raises(tmp_path):
    file_path = _make_file(tmp_path, "x.txt", "abc")
    rid = res_manager.create_resource(
        content_type="text/plain", file_path=file_path, produced_by_arc_id=None,
    )
    with pytest.raises(ValueError, match="limit"):
        res_manager.read_resource_content(rid, limit=-1, caller_arc_id=None)


# ---------------------------------------------------------------------------
# Defence-in-depth trust gate (caller_arc_id)
# ---------------------------------------------------------------------------


def test_trusted_caller_rejected_reading_untrusted_resource(tmp_path):
    """Trusted arc passing caller_arc_id cannot read a raw (untrusted) Resource."""
    arc_id = arc_manager.create_arc(name="trusted-caller", integrity_level="trusted")
    file_path = _make_file(tmp_path, "raw.html", "untrusted bytes")
    rid = res_manager.create_resource(
        content_type="text/html", file_path=file_path, produced_by_arc_id=None,
    )
    # Raw ingest → derived trust is 'untrusted'.
    with pytest.raises(PermissionError, match="Trusted arc"):
        res_manager.read_resource_content(rid, caller_arc_id=arc_id)


def test_trusted_caller_allowed_reading_trusted_resource(tmp_path):
    """Trusted arc can read an approved-template (trusted) Resource."""
    producer = arc_manager.create_arc(name="producer", integrity_level="trusted")
    reader = arc_manager.create_arc(name="reader", integrity_level="trusted")
    file_path = _make_file(tmp_path, "t.html", "trusted bytes")
    rid = res_manager.derive_resource(
        content_type="text/html",
        file_path=file_path,
        produced_by_arc_id=producer,
        produced_by_template="tmpl.v1",
        template_verdict="approved",
    )
    assert (
        res_manager.read_resource_content(rid, caller_arc_id=reader)
        == "trusted bytes"
    )


def test_untrusted_caller_can_read_untrusted_resource(tmp_path):
    """Untrusted arc (e.g. sandboxed EXECUTOR) may read raw Resources."""
    parent = arc_manager.create_arc(name="parent", integrity_level="trusted")
    child = arc_manager.add_child(parent, "worker", integrity_level="untrusted")
    file_path = _make_file(tmp_path, "raw.txt", "payload")
    rid = res_manager.create_resource(
        content_type="text/plain", file_path=file_path, produced_by_arc_id=None,
    )
    assert (
        res_manager.read_resource_content(rid, caller_arc_id=child) == "payload"
    )


def test_trusted_caller_rejected_reading_pending_derived_resource(tmp_path):
    """Derived but unapproved (pending) Resources are still untrusted."""
    arc_id = arc_manager.create_arc(name="caller", integrity_level="trusted")
    producer = arc_manager.create_arc(name="prod", integrity_level="trusted")
    file_path = _make_file(tmp_path, "pending.html", "awaiting judge")
    rid = res_manager.derive_resource(
        content_type="text/html",
        file_path=file_path,
        produced_by_arc_id=producer,
        produced_by_template="tmpl.v1",
        template_verdict="pending",
    )
    with pytest.raises(PermissionError, match="Trusted arc"):
        res_manager.read_resource_content(rid, caller_arc_id=arc_id)


def test_missing_caller_arc_raises(tmp_path):
    """Passing caller_arc_id for a non-existent arc is a PermissionError."""
    file_path = _make_file(tmp_path, "x.txt", "bytes")
    rid = res_manager.create_resource(
        content_type="text/plain", file_path=file_path, produced_by_arc_id=None,
    )
    with pytest.raises(PermissionError, match="caller arc"):
        res_manager.read_resource_content(rid, caller_arc_id=999_999)


def test_caller_arc_id_none_skips_trust_check(tmp_path):
    """``caller_arc_id=None`` is the explicit chat/platform/test path."""
    file_path = _make_file(tmp_path, "raw.html", "no check")
    rid = res_manager.create_resource(
        content_type="text/html", file_path=file_path, produced_by_arc_id=None,
    )
    # Untrusted Resource, caller_arc_id=None → read succeeds (chat surface
    # / platform code is responsible for its own gate).
    assert res_manager.read_resource_content(rid, caller_arc_id=None) == "no check"


def test_caller_arc_id_is_required():
    """Omitting ``caller_arc_id`` is a TypeError — the gate cannot be
    silently bypassed by forgetting the parameter."""
    with pytest.raises(TypeError):
        res_manager.read_resource_content(1)  # type: ignore[call-arg]

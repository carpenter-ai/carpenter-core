"""Crash-safety tests for the Resource sweep.

The sweep commits the DB state change BEFORE unlinking the file.  A crash
between commit and unlink therefore leaves an orphan blob on disk but a
consistent DB row (``deleted_at`` set, ``file_path`` NULL).  A subsequent
sweep must NOT re-visit that row (the select predicate filters on
``file_path IS NOT NULL``), and the orphan blob is discoverable because
the parent directory still exists.

The inverse ordering — unlink first — would leave a window where the row
claims ``file_path=<path>`` but the blob is gone.  Consumers would hit
``FileNotFoundError`` on read.  We deliberately prefer disk-orphan over
DB-lie.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from carpenter.core.resources import manager as res_manager
from carpenter.core.resources.sweep import run_sweep
from carpenter.db import db_transaction


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _make_old_deprecated_resource(tmp_path) -> tuple[int, str]:
    rid = res_manager.create_resource(
        content_type="text/plain",
        file_path=None,
        produced_by_arc_id=None,
    )
    d = tmp_path / "resources" / str(rid)
    d.mkdir(parents=True, exist_ok=True)
    blob = d / "blob"
    blob.write_bytes(b"data")
    deprecated_at_iso = _utc_iso(
        datetime.now(timezone.utc) - timedelta(days=30)
    )
    with db_transaction() as db:
        db.execute(
            "UPDATE resources "
            "SET file_path = ?, deprecated_at = ? "
            "WHERE id = ?",
            (str(blob), deprecated_at_iso, rid),
        )
    return rid, str(blob)


def test_unlink_failure_leaves_db_committed_and_file_on_disk(
    tmp_path, monkeypatch,
):
    """A simulated unlink crash must still commit the DB state flip.

    After the crash:
    - The row has ``deleted_at`` set and ``file_path`` NULL.
    - The blob still exists on disk (the orphan we accept).
    - A subsequent sweep finds 0 candidates (filter excludes
      file_path IS NULL rows).
    """
    rid, path = _make_old_deprecated_resource(tmp_path)

    import os as _os

    real_unlink = _os.unlink

    def boom(target):  # pragma: no cover — exercised by monkeypatch
        if str(target) == path:
            raise OSError("simulated crash between commit and unlink")
        return real_unlink(target)

    monkeypatch.setattr("carpenter.core.resources.sweep.os.unlink", boom)

    result = run_sweep()

    # DB side effect committed.
    row = res_manager.get_resource(rid)
    assert row["deleted_at"] is not None
    assert row["file_path"] is None

    # File is still on disk (the orphan).
    assert Path(path).exists()

    # The per-row result reports the OSError, not an ENOENT.
    assert result["candidates"] == 1
    assert result["files_deleted"] == 0
    assert len(result["file_errors"]) == 1
    err_rid, err_str = result["file_errors"][0]
    assert err_rid == rid
    assert err_str.startswith("unlink:")
    assert "simulated crash" in err_str

    # Second sweep finds nothing — the row is no longer a candidate.
    result2 = run_sweep()
    assert result2["candidates"] == 0
    assert result2["files_deleted"] == 0

    # Orphan blob is still discoverable: parent dir exists, blob exists.
    assert Path(path).exists()
    assert Path(path).parent.exists()

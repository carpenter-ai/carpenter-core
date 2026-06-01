"""Tests for the weekly Resource sweep job.

Covers the core invariants laid out in
``carpenter/core/resources/sweep.py``:

- Candidate selection: deprecated + old + unpinned + unretained only.
- Pin / ``retain_until`` act as shields.
- Already-deleted rows are no-ops (idempotency).
- Multiple resources in one pass are each handled independently.
- Missing-on-disk blobs (ENOENT) count as a successful unlink.
- The per-resource directory is pruned after the blob vanishes.
- Return dict shape matches the spec.
"""

from datetime import datetime, timedelta, timezone

import pytest

from carpenter.core.resources import manager as res_manager
from carpenter.core.resources import sweep as res_sweep
from carpenter.core.resources.sweep import run_sweep
from carpenter.db import db_connection, db_transaction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _make_blob(tmp_path, resource_id: int, content: bytes = b"blob") -> str:
    """Create a per-resource directory + blob matching the storage layout."""
    # The sweep only cares about (path, path.parent) — we don't need to go
    # through resource_storage_dir() as long as both the file and the
    # enclosing directory exist.
    d = tmp_path / "resources" / str(resource_id)
    d.mkdir(parents=True, exist_ok=True)
    blob = d / "blob"
    blob.write_bytes(content)
    return str(blob)


def _create_sweepable_row(
    tmp_path,
    *,
    deprecated_days_ago: float = 30.0,
    pinned: bool = False,
    retain_until: datetime | None = None,
    with_file: bool = True,
) -> tuple[int, str | None]:
    """Insert a resource row with full control over the sweep-relevant fields.

    Returns ``(resource_id, file_path_or_None)``.
    """
    rid = res_manager.create_resource(
        content_type="text/plain",
        file_path=None,  # we'll set it explicitly below
        produced_by_arc_id=None,
        pinned=pinned,
    )

    file_path: str | None = None
    if with_file:
        file_path = _make_blob(tmp_path, rid)

    deprecated_at_iso: str | None = None
    if deprecated_days_ago is not None:
        deprecated_at_iso = _utc_iso(
            datetime.now(timezone.utc) - timedelta(days=deprecated_days_ago)
        )

    retain_iso = _utc_iso(retain_until) if retain_until else None

    with db_transaction() as db:
        db.execute(
            "UPDATE resources "
            "SET file_path = ?, deprecated_at = ?, retain_until = ? "
            "WHERE id = ?",
            (file_path, deprecated_at_iso, retain_iso, rid),
        )
    return rid, file_path


def _row(rid: int) -> dict:
    return res_manager.get_resource(rid)


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------


def test_old_deprecated_unpinned_unretained_is_swept(tmp_path):
    rid, path = _create_sweepable_row(tmp_path, deprecated_days_ago=30)
    assert path is not None
    from pathlib import Path
    assert Path(path).exists()

    result = run_sweep()

    assert result["candidates"] == 1
    assert result["files_deleted"] == 1
    assert result["file_errors"] == []

    row = _row(rid)
    assert row is not None, "row is kept as a tombstone"
    assert row["deleted_at"] is not None
    assert row["file_path"] is None
    assert not Path(path).exists()


def test_not_old_enough_is_untouched(tmp_path):
    rid, path = _create_sweepable_row(tmp_path, deprecated_days_ago=1)

    result = run_sweep(age_days=7)

    assert result["candidates"] == 0
    assert result["files_deleted"] == 0

    row = _row(rid)
    assert row["deleted_at"] is None
    assert row["file_path"] == path


def test_pinned_is_untouched(tmp_path):
    rid, path = _create_sweepable_row(
        tmp_path, deprecated_days_ago=30, pinned=True,
    )

    result = run_sweep()

    assert result["candidates"] == 0
    row = _row(rid)
    assert row["deleted_at"] is None
    assert row["file_path"] == path
    assert row["pinned"] == 1


def test_future_retain_until_is_untouched(tmp_path):
    future = datetime.now(timezone.utc) + timedelta(days=30)
    rid, path = _create_sweepable_row(
        tmp_path, deprecated_days_ago=30, retain_until=future,
    )

    result = run_sweep()

    assert result["candidates"] == 0
    row = _row(rid)
    assert row["deleted_at"] is None
    assert row["file_path"] == path


def test_past_retain_until_with_old_deprecation_is_swept(tmp_path):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    rid, path = _create_sweepable_row(
        tmp_path, deprecated_days_ago=30, retain_until=past,
    )

    result = run_sweep()

    assert result["candidates"] == 1
    assert result["files_deleted"] == 1
    row = _row(rid)
    assert row["deleted_at"] is not None
    assert row["file_path"] is None


def test_never_deprecated_is_untouched(tmp_path):
    rid, path = _create_sweepable_row(tmp_path, deprecated_days_ago=None)

    result = run_sweep()

    assert result["candidates"] == 0
    row = _row(rid)
    assert row["deleted_at"] is None
    assert row["file_path"] == path
    assert row["deprecated_at"] is None


def test_already_deleted_is_noop(tmp_path):
    rid, path = _create_sweepable_row(tmp_path, deprecated_days_ago=30)

    # First pass sweeps it.
    first = run_sweep()
    assert first["candidates"] == 1
    assert first["files_deleted"] == 1
    row_after_first = _row(rid)
    deleted_at_first = row_after_first["deleted_at"]

    # Second pass should find nothing (file_path is NULL now).
    second = run_sweep()
    assert second["candidates"] == 0
    assert second["files_deleted"] == 0

    row_after_second = _row(rid)
    # Tombstone timestamp does not shift.
    assert row_after_second["deleted_at"] == deleted_at_first
    assert row_after_second["file_path"] is None


def test_multiple_resources_in_one_sweep(tmp_path):
    sweepable = []
    for _ in range(3):
        rid, path = _create_sweepable_row(tmp_path, deprecated_days_ago=30)
        sweepable.append((rid, path))

    # One extra row that should NOT be swept (pinned).
    safe_rid, safe_path = _create_sweepable_row(
        tmp_path, deprecated_days_ago=30, pinned=True,
    )

    result = run_sweep()

    assert result["candidates"] == 3
    assert result["files_deleted"] == 3
    assert result["file_errors"] == []

    from pathlib import Path
    for rid, path in sweepable:
        row = _row(rid)
        assert row["deleted_at"] is not None
        assert row["file_path"] is None
        assert not Path(path).exists()

    safe_row = _row(safe_rid)
    assert safe_row["deleted_at"] is None
    assert safe_row["file_path"] == safe_path
    assert Path(safe_path).exists()


def test_missing_file_on_disk_is_not_an_error(tmp_path):
    """If the blob is already gone, the sweep still finishes cleanly."""
    import os as _os
    from pathlib import Path

    rid, path = _create_sweepable_row(tmp_path, deprecated_days_ago=30)
    _os.unlink(path)
    assert not Path(path).exists()

    result = run_sweep()

    assert result["candidates"] == 1
    # ENOENT counts as "file is gone, which is what we wanted".
    assert result["files_deleted"] == 1
    assert result["file_errors"] == []

    row = _row(rid)
    assert row["deleted_at"] is not None
    assert row["file_path"] is None


def test_empty_per_resource_dir_is_removed_after_sweep(tmp_path):
    from pathlib import Path

    rid, path = _create_sweepable_row(tmp_path, deprecated_days_ago=30)
    parent = Path(path).parent

    run_sweep()

    assert not Path(path).exists()
    assert not parent.exists(), f"per-resource dir should be rmdir'd: {parent}"


def test_non_empty_per_resource_dir_is_left_alone(tmp_path):
    """An unexpected sibling file should not block the sweep nor be removed."""
    from pathlib import Path

    rid, path = _create_sweepable_row(tmp_path, deprecated_days_ago=30)
    parent = Path(path).parent
    # Stray sibling — not something the sweep owns.
    stray = parent / "stray.txt"
    stray.write_text("surprise")

    result = run_sweep()

    assert result["candidates"] == 1
    assert result["files_deleted"] == 1
    assert not Path(path).exists()
    # The directory should still exist because it is not empty.
    assert parent.exists()
    assert stray.exists()


def test_return_shape_and_types(tmp_path):
    _create_sweepable_row(tmp_path, deprecated_days_ago=30)

    result = run_sweep()

    assert set(result.keys()) == {"candidates", "files_deleted", "file_errors"}
    assert isinstance(result["candidates"], int)
    assert isinstance(result["files_deleted"], int)
    assert isinstance(result["file_errors"], list)


def test_age_days_override_to_zero_sweeps_freshly_deprecated(tmp_path):
    """age_days=0 is the ``sweep everything deprecated`` mode."""
    rid, path = _create_sweepable_row(tmp_path, deprecated_days_ago=0.001)

    result = run_sweep(age_days=0)

    assert result["candidates"] == 1
    assert result["files_deleted"] == 1
    row = _row(rid)
    assert row["deleted_at"] is not None


def test_age_days_negative_raises(tmp_path):
    with pytest.raises(ValueError):
        run_sweep(age_days=-1)


def test_config_default_is_honored(tmp_path, monkeypatch):
    """When age_days is None, the config value is read."""
    # 3 days deprecated; default of 7 would skip, a config of 2 would sweep.
    rid, path = _create_sweepable_row(tmp_path, deprecated_days_ago=3)

    import carpenter.config as _cfg
    monkeypatch.setitem(_cfg.CONFIG, "resource_sweep_age_days", 2)

    result = run_sweep()

    assert result["candidates"] == 1
    assert result["files_deleted"] == 1
    row = _row(rid)
    assert row["deleted_at"] is not None


def test_no_file_path_row_is_skipped(tmp_path):
    """A deprecated row that was never backed by a file is not a candidate."""
    rid, _ = _create_sweepable_row(
        tmp_path, deprecated_days_ago=30, with_file=False,
    )

    result = run_sweep()

    assert result["candidates"] == 0
    row = _row(rid)
    assert row["deleted_at"] is None
    assert row["file_path"] is None

"""Shared atomic, locked, chmod-600, fsync'd writer for ``.env`` files.

This is the single implementation used by every code path that mutates a
``.env`` file holding long-lived secrets (OAuth refresh tokens, API
keys, forge tokens).  Centralising it ensures the security and
durability properties stay in sync across call sites:

* **Atomic** — writes go to a unique temp file in the same directory
  and are then ``os.rename``'d into place.  Concurrent readers always
  see either the old complete file or the new complete file, never a
  partial write.
* **Locked** — concurrent writers (e.g. an operator running
  ``setup-credential`` while an OAuth callback is being processed) are
  serialised via ``flock`` on a sidecar lock file.  We lock the
  sidecar rather than the secrets file itself so the secrets file
  isn't opened just to acquire the lock.
* **chmod 0o600** — the temp file is chmod'd to owner-only *before*
  the rename, so the destination is never briefly world-readable.
* **fsync durable** — the temp file's data is fsync'd to disk before
  rename, and the parent directory is fsync'd after rename, so a
  power loss after ``update_dot_env`` returns will not silently
  drop the new credential.
* **Non-POSIX** — on platforms without ``fcntl`` (Windows) the lock
  degrades to a no-op and directory fsync is skipped; the rest of
  the contract still holds.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


@contextmanager
def _dot_env_lock(dot_env_path: Path) -> Iterator[None]:
    """Serialize concurrent writers to ``.env`` via ``flock`` on a sidecar.

    The lock file is ``<dot_env_path>.lock`` rather than the ``.env`` file
    itself so we don't open the secrets file just to acquire the lock.
    On non-POSIX platforms (no ``fcntl``), this degrades to a no-op — the
    rest of the write is still atomic via ``os.rename``.
    """
    try:
        import fcntl  # POSIX only
    except ImportError:  # pragma: no cover — non-POSIX path
        yield
        return

    lock_path = dot_env_path.with_suffix(dot_env_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Open in append mode so the lock file is created if missing without
    # truncating it.  We never write to it.
    with open(lock_path, "a") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _fsync_directory(dir_path: Path) -> None:
    """fsync the directory entry so ``rename`` survives a crash.

    On POSIX, ``os.rename`` only guarantees atomicity, not durability:
    after a power loss the directory entry may still point at the old
    inode unless the directory itself has been fsync'd.  We open the
    directory read-only and fsync the resulting fd.

    Best-effort: on platforms (or filesystems) where directories cannot
    be opened/fsync'd we log and continue.  The atomic rename property
    is preserved either way.
    """
    try:
        dir_fd = os.open(str(dir_path), os.O_RDONLY)
    except OSError as exc:  # pragma: no cover — exotic FS / Windows
        logger.debug("could not open %s for fsync: %s", dir_path, exc)
        return
    try:
        os.fsync(dir_fd)
    except OSError as exc:  # pragma: no cover — fs without dir fsync
        logger.debug("fsync on directory %s failed: %s", dir_path, exc)
    finally:
        os.close(dir_fd)


def update_dot_env(dot_env_path: Path, key: str, value: str) -> bool:
    """Write or update ``KEY=VALUE`` in a ``.env`` file.

    The write is atomic, serialized across processes, chmod 0o600, and
    fsync'd for durability.  See module docstring for the full
    contract.

    Args:
        dot_env_path: Path to the ``.env`` file.  Parent directories
            are created as needed.
        key: Env-var name (e.g. ``FORGEJO_TOKEN``).
        value: Env-var value.

    Returns:
        ``True`` if the key was updated in place; ``False`` if the key
        was newly added.
    """
    dot_env_path = Path(dot_env_path)
    dot_env_path.parent.mkdir(parents=True, exist_ok=True)

    with _dot_env_lock(dot_env_path):
        existing_lines: list[str] = []
        if dot_env_path.is_file():
            existing_lines = dot_env_path.read_text().splitlines()

        new_lines: list[str] = []
        updated = False
        for line in existing_lines:
            if re.match(rf'^{re.escape(key)}\s*=', line.strip()):
                new_lines.append(f"{key}={value}")
                updated = True
            else:
                new_lines.append(line)

        if not updated:
            if new_lines and new_lines[-1].strip():
                new_lines.append("")  # blank separator
            new_lines.append(f"{key}={value}")

        new_content = "\n".join(new_lines) + "\n"

        # Atomic write: write to a unique temp file in the same dir, then
        # rename over the destination.  ``os.rename`` is atomic on POSIX,
        # so concurrent readers always see either the old or the new
        # complete file — never a partial write.
        tmp_path = dot_env_path.with_name(
            f"{dot_env_path.name}.tmp.{os.getpid()}",
        )
        try:
            # Open with low-level ``os.open`` so we can fsync the fd
            # before closing.  ``Path.write_text`` would close the fd
            # internally, defeating the durability story.
            fd = os.open(
                str(tmp_path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            try:
                data = new_content.encode("utf-8")
                # ``os.write`` may short-write; loop until done.
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                # fsync BEFORE close+rename: guarantees the file's
                # contents have hit stable storage so a crash after
                # rename can't yield an empty file under the .env
                # name.
                os.fsync(fd)
            finally:
                os.close(fd)
            # chmod 0o600 BEFORE rename so the destination is never
            # briefly world-readable.  ``os.open`` already created the
            # file with mode 0o600, but an existing temp file (e.g.
            # from a previous crashed run) might have looser perms,
            # so chmod again to be safe.  The file holds OAuth
            # refresh tokens; owner-only is the right default.
            os.chmod(tmp_path, 0o600)
            os.rename(tmp_path, dot_env_path)
            # fsync the parent directory so the rename's directory
            # entry survives a power loss.  Without this, the file
            # data is durable but the directory entry may still
            # reference the old inode after a crash.
            _fsync_directory(dot_env_path.parent)
        except Exception:
            # Best-effort cleanup; if rename succeeded the temp file is
            # already gone and unlink will raise FileNotFoundError.
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            raise

    return updated

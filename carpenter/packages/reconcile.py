"""Three-way package-upgrade reconciliation (pure logic core).

When a capability package is upgraded, the files it ships may have changed
upstream AND the user may have locally modified some of them.  Reconciling
this is the dpkg/ucf problem: we compare three trees and classify every
path.

* ``old``     — the tree the *previously installed* package version shipped.
* ``new``     — the tree the *new* package version ships.
* ``current`` — what is actually on disk now (user's possibly-edited copy).

This module is the *pure* logic core only: dataclasses, an enum, the
``classify`` three-way matrix, and a stdlib-only text-diff helper.  It does
**no** archive handling, no network, no caching, no apply step, and no CLI.
Those layers are separate future work.  Inputs are simple
``path -> content`` mappings (bytes or str) so the classifier is trivially
testable and fully deterministic.

Status matrix (per path)
------------------------
Membership is denoted ``(O, N, C)`` — present in old / new / current.

Present in all three (O, N, C):
  * old == new == cur                 -> :data:`FileStatus.UNCHANGED`
  * old == cur,  new != old           -> :data:`FileStatus.UPSTREAM_ONLY`
  * old == new,  cur != old           -> :data:`FileStatus.USER_ONLY`
  * new == cur,  both != old          -> :data:`FileStatus.CONVERGED`
  * all three pairwise distinct       -> :data:`FileStatus.CONFLICT`

Present in ``new`` only (-, N, -):
  * always                            -> :data:`FileStatus.ADDED_UPSTREAM`

Present in ``current`` only (-, -, C):
  * always                            -> :data:`FileStatus.ADDED_USER`

Present in new + current, not old (-, N, C):
  * new == cur                        -> :data:`FileStatus.CONVERGED`
  * new != cur                        -> :data:`FileStatus.CONFLICT`

Present in old + current, not new (O, -, C)  — upstream removed the file:
  * cur == old (user untouched)       -> :data:`FileStatus.REMOVED_UPSTREAM`
  * cur != old (user modified it)     -> :data:`FileStatus.REMOVED_UPSTREAM_CONFLICT`

Present in old + new, not current (O, N, -)  — user deleted the file:
  * old == new (user removed a file upstream left unchanged)
        -> :data:`FileStatus.USER_ONLY`
           Rationale: the user made a deliberate local change (deletion) and
           upstream shipped nothing new for this path, so we honour the
           user's edit exactly as we honour a user content edit (USER_ONLY).
           ``auto_apply`` keeps the user's state (the deletion) with no
           prompt.
  * old != new (user deleted a file upstream *changed*)
        -> :data:`FileStatus.CONFLICT`
           Rationale: two competing intents (user wants it gone, upstream
           shipped a new version), so the user must decide.

Present in none of the three:
  * impossible — such a path never appears in the union of keys.

A status is "auto-applicable" iff it needs no user decision.  The two
conflict statuses (:data:`FileStatus.CONFLICT`,
:data:`FileStatus.REMOVED_UPSTREAM_CONFLICT`) require a decision; everything
else is auto-applicable.
"""

from __future__ import annotations

import difflib
import enum
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "FileStatus",
    "FileDelta",
    "ReconcilePlan",
    "classify",
    "content_hash",
    "is_binary",
    "unified_diff",
]


class FileStatus(enum.Enum):
    """Classification of a single path across the three trees."""

    #: Present in all three, identical everywhere.  No action.
    UNCHANGED = "unchanged"
    #: Present in all three; only upstream changed (user untouched).  Adopt new.
    UPSTREAM_ONLY = "upstream_only"
    #: Present in all three; only the user changed it.  Keep user's content.
    USER_ONLY = "user_only"
    #: Three-way divergence; user must decide.
    CONFLICT = "conflict"
    #: New file shipped by the new version only.  Adopt.
    ADDED_UPSTREAM = "added_upstream"
    #: File the user created (absent from old & new).  Keep.
    ADDED_USER = "added_user"
    #: In old, gone in new, user untouched.  Remove.
    REMOVED_UPSTREAM = "removed_upstream"
    #: In old, gone in new, but user modified it.  User must decide.
    REMOVED_UPSTREAM_CONFLICT = "removed_upstream_conflict"
    #: User and upstream independently reached identical content.  No conflict.
    CONVERGED = "converged"

    @property
    def is_conflict(self) -> bool:
        """True if this status needs an explicit user decision."""
        return self in _CONFLICT_STATUSES


_CONFLICT_STATUSES: frozenset[FileStatus] = frozenset(
    {FileStatus.CONFLICT, FileStatus.REMOVED_UPSTREAM_CONFLICT}
)


@dataclass(frozen=True)
class FileDelta:
    """The classification of one path and the hashes that produced it.

    Hashes are hex SHA-256 of the content, or ``None`` when the path is
    absent from that tree.  Hashes (not raw content) are stored so a plan is
    cheap to keep around and safe to log.
    """

    path: str
    status: FileStatus
    old_hash: str | None
    new_hash: str | None
    current_hash: str | None

    @property
    def is_conflict(self) -> bool:
        return self.status.is_conflict


@dataclass(frozen=True)
class ReconcilePlan:
    """The full classification of a three-way reconciliation.

    ``deltas`` is ordered deterministically by path.
    """

    deltas: tuple[FileDelta, ...]

    def conflicts(self) -> tuple[FileDelta, ...]:
        """Subset of deltas that require an explicit user decision."""
        return tuple(d for d in self.deltas if d.is_conflict)

    def auto_apply(self) -> tuple[FileDelta, ...]:
        """Subset of deltas that can be applied with no user input."""
        return tuple(d for d in self.deltas if not d.is_conflict)

    @property
    def has_conflicts(self) -> bool:
        """True if any delta needs a user decision."""
        return any(d.is_conflict for d in self.deltas)


# ── hashing / content helpers ────────────────────────────────────────


def _as_bytes(content: bytes | str) -> bytes:
    """Normalise content to bytes (UTF-8 for str)."""
    if isinstance(content, bytes):
        return content
    return content.encode("utf-8")


def content_hash(content: bytes | str) -> str:
    """Hex-encoded SHA-256 of ``content`` (str encoded as UTF-8)."""
    return hashlib.sha256(_as_bytes(content)).hexdigest()


def _opt_hash(content: bytes | str | None) -> str | None:
    return None if content is None else content_hash(content)


# ── core classifier ──────────────────────────────────────────────────


def _classify_one(
    old: bytes | str | None,
    new: bytes | str | None,
    cur: bytes | str | None,
) -> FileStatus:
    """Apply the three-way matrix to one path's three optional contents.

    Equality is compared on normalised bytes so a ``str`` and the equivalent
    ``bytes`` are treated as identical content.
    """
    o = None if old is None else _as_bytes(old)
    n = None if new is None else _as_bytes(new)
    c = None if cur is None else _as_bytes(cur)

    has_o, has_n, has_c = o is not None, n is not None, c is not None

    # Present in all three.
    if has_o and has_n and has_c:
        if o == n == c:
            return FileStatus.UNCHANGED
        if o == c:  # only upstream moved
            return FileStatus.UPSTREAM_ONLY
        if o == n:  # only user moved
            return FileStatus.USER_ONLY
        if n == c:  # user & upstream landed on the same content
            return FileStatus.CONVERGED
        return FileStatus.CONFLICT  # all three distinct

    # New only.
    if not has_o and has_n and not has_c:
        return FileStatus.ADDED_UPSTREAM

    # Current only.
    if not has_o and not has_n and has_c:
        return FileStatus.ADDED_USER

    # New + current, not old (both added the path).
    if not has_o and has_n and has_c:
        return FileStatus.CONVERGED if n == c else FileStatus.CONFLICT

    # Old + current, not new (upstream removed the file).
    if has_o and not has_n and has_c:
        if o == c:
            return FileStatus.REMOVED_UPSTREAM
        return FileStatus.REMOVED_UPSTREAM_CONFLICT

    # Old + new, not current (user deleted the file).
    if has_o and has_n and not has_c:
        # User left nothing; honour the deletion if upstream didn't change
        # the file, else it's a competing intent the user must resolve.
        return FileStatus.USER_ONLY if o == n else FileStatus.CONFLICT

    # Old only (in old, gone from both new and current): both upstream and
    # the user removed it — converged on absence, no action needed.
    if has_o and not has_n and not has_c:
        return FileStatus.REMOVED_UPSTREAM

    # Unreachable: a path in the key union is present in at least one tree.
    raise AssertionError("path absent from all three trees")  # pragma: no cover


def classify(
    old: Mapping[str, bytes | str],
    new: Mapping[str, bytes | str],
    current: Mapping[str, bytes | str],
) -> ReconcilePlan:
    """Classify every path across the ``old``/``new``/``current`` trees.

    Pure and deterministic.  Deltas are ordered by path.  See the module
    docstring for the full status matrix and the user-deletion rationale.
    """
    paths = sorted(set(old) | set(new) | set(current))
    deltas: list[FileDelta] = []
    for path in paths:
        o = old.get(path)
        n = new.get(path)
        c = current.get(path)
        status = _classify_one(o, n, c)
        deltas.append(
            FileDelta(
                path=path,
                status=status,
                old_hash=_opt_hash(o),
                new_hash=_opt_hash(n),
                current_hash=_opt_hash(c),
            )
        )
    return ReconcilePlan(deltas=tuple(deltas))


# ── diff helper (for the future CLI) ──────────────────────────────────


def is_binary(content: bytes | str) -> bool:
    """Heuristic binary detection.

    ``str`` is always text.  ``bytes`` is treated as binary if it contains a
    NUL byte or fails to decode as UTF-8 — the same cheap heuristic git uses.
    """
    if isinstance(content, str):
        return False
    if b"\x00" in content:
        return True
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _as_text_lines(content: bytes | str) -> list[str]:
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    return content.splitlines(keepends=True)


def unified_diff(
    path: str,
    a: bytes | str,
    b: bytes | str,
    *,
    a_label: str = "old",
    b_label: str = "new",
) -> str:
    """Return a unified text diff of ``a`` vs ``b`` for ``path``.

    For binary content (either side) no diff is produced; a single marker
    line ``"Binary files <path> differ"`` (or "match") is returned instead.
    Stdlib-only (``difflib``).
    """
    if is_binary(a) or is_binary(b):
        same = _as_bytes(a) == _as_bytes(b)
        verb = "match" if same else "differ"
        return f"Binary files {path} {verb}\n"

    diff = difflib.unified_diff(
        _as_text_lines(a),
        _as_text_lines(b),
        fromfile=f"{a_label}/{path}",
        tofile=f"{b_label}/{path}",
    )
    return "".join(diff)

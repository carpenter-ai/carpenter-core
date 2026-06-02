"""Platform path tier and change-category classifier.

Deterministic (no LLM) classifier used by the platform-integrity system to
decide how to handle file changes proposed by agents.  Three concepts:

1. **Path tier** — T0 invisible (credentials/secrets), T1 platform (carpenter
   repo source and security tests; protected by humans), T2 user-home (the
   user's writable area).  Most-specific (longest realpath-prefix) rule wins,
   ties resolve to the most-restrictive tier (T0 > T1 > T2).
2. **Change category** — "python", "yaml", "kb", or "unknown", driven by
   extension and location.
3. **Workflow selection** — given a set of paths, pick which change-workflow
   template to use and whether to force human review.

Hardcoded T0/T1 rules are a FLOOR — user config (``platform_integrity.
path_overrides``) can *add* paths to a more restrictive tier, but cannot
*remove* anything from the floor.

Fail-closed: any exception in tier classification logs a WARNING and returns
T1 (platform-protected) rather than silently classifying as T2.

This module is purely additive in this PR — no enforcement wiring yet.  Later
PRs consume :func:`path_tier`, :func:`change_category`, :func:`is_invisible`,
and :func:`select_workflow_for_paths`.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from typing import Optional

from .. import config as _config_module

logger = logging.getLogger(__name__)


# ── Public tier constants ────────────────────────────────────────────────
PATH_TIER_T0 = "T0"
PATH_TIER_T1 = "T1"
PATH_TIER_T2 = "T2"


# ── Hardcoded T0 invisible patterns (matched via fnmatch on realpath) ────
# These are the FLOOR; user config can add more T0 patterns but cannot
# remove these.  Anything matching here is invisible to chat / planner / etc.
_HARDCODED_T0_PATTERNS: tuple[str, ...] = (
    "*/.env",
    "*/.env.*",
    "*/platform.db",
    "*/platform.db-wal",
    "*/platform.db-shm",
    "*/credentials/*",
    "*/secrets/*",
    "*.key",
    "*.pem",
    "*_token",
    "*/review_keys/*",
    "/opt/credentials/*",
)


# ── Hardcoded T1 platform prefixes (relative to the resolved repo root) ──
# These are appended to the repo root realpath.  Anything matching is
# platform-protected — changes here always require human review.
_HARDCODED_T1_REL_PREFIXES: tuple[str, ...] = (
    "carpenter/",
    "carpenter_tools/",
    "config_seed/",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    ".github/",
    ".forgejo/",
    "tests/security/",
    "tests/test_taint_invariants.py",
    "docs/coding-invariants.md",
    "docs/trust-invariants.md",
    "docs/security-model.md",
    "schema.sql",
    "db_migrations.py",
)


# ── Change-workflow defaults (consumed when CONFIG block missing) ────────
_DEFAULT_CHANGE_WORKFLOWS: dict[str, str] = {
    "python": "coding-change",
    "yaml": "yaml-change",
    "kb": "kb-change",
    "unknown": "coding-change",
}


# ── Repo-root resolution ─────────────────────────────────────────────────

def _resolve_repo_root() -> Optional[str]:
    """Resolve the carpenter-core repo root via CONFIG or import fallback.

    Returns ``None`` if both strategies fail (caller treats unclassified
    paths as T1 — fail-closed).
    """
    # 1) Explicit config.  Most-trusted source.
    try:
        cfg_repo = _config_module.CONFIG.get("repo_dir")  # type: ignore[attr-defined]
        if cfg_repo:
            return os.path.realpath(os.path.expanduser(str(cfg_repo)))
    except Exception:  # noqa: BLE001 — defensive
        pass

    # 2) Fall back to the importing package's location.
    try:
        import carpenter  # local import to avoid bootstrap cycles
        pkg_file = getattr(carpenter, "__file__", None)
        if pkg_file:
            return os.path.realpath(os.path.join(os.path.dirname(pkg_file), ".."))
    except Exception:  # noqa: BLE001
        pass

    return None


def _resolve_carpenter_home() -> Optional[str]:
    """Resolve the user's carpenter-home dir for T2 classification.

    Falls back through ``carpenter_home`` then ``base_dir`` (the historical
    name for the same concept).  Returns ``None`` if neither is set.
    """
    cfg = _config_module.CONFIG
    for key in ("carpenter_home", "base_dir"):
        try:
            val = cfg.get(key)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            val = None
        if val:
            try:
                return os.path.realpath(os.path.expanduser(str(val)))
            except Exception:  # noqa: BLE001
                continue
    return None


# ── Path-override config plumbing ────────────────────────────────────────

def _load_path_overrides() -> list[dict]:
    """Return the user's `platform_integrity.path_overrides` list (live).

    Always re-read at call time so :func:`reload_config` is honored.  Bad
    entries (missing keys, non-string prefix, unknown tier) are silently
    dropped — fail-closed semantics live in :func:`path_tier`.
    """
    try:
        block = _config_module.CONFIG.get("platform_integrity") or {}  # type: ignore[attr-defined]
        overrides = block.get("path_overrides") if isinstance(block, dict) else None
        if not isinstance(overrides, list):
            return []
        clean: list[dict] = []
        for entry in overrides:
            if not isinstance(entry, dict):
                continue
            prefix = entry.get("prefix")
            tier = entry.get("tier")
            if not isinstance(prefix, str) or tier not in (
                PATH_TIER_T0, PATH_TIER_T1, PATH_TIER_T2,
            ):
                continue
            clean.append({"prefix": prefix, "tier": tier})
        return clean
    except Exception:  # noqa: BLE001
        return []


# ── Core API ─────────────────────────────────────────────────────────────

# Tier severity for tie-breaking (higher = more restrictive)
_TIER_RANK: dict[str, int] = {
    PATH_TIER_T2: 0,
    PATH_TIER_T1: 1,
    PATH_TIER_T0: 2,
}


def _hardcoded_t1_prefixes(repo_root: str) -> list[str]:
    """Build the absolute T1 prefix list for the resolved repo root."""
    out: list[str] = []
    for rel in _HARDCODED_T1_REL_PREFIXES:
        out.append(os.path.join(repo_root, rel))
    return out


def _match_t0(real: str) -> bool:
    """Return True if *real* matches any hardcoded T0 pattern."""
    for pattern in _HARDCODED_T0_PATTERNS:
        if fnmatch.fnmatch(real, pattern):
            return True
    return False


def _best_prefix_match(real: str, prefixes: list[tuple[str, str]]) -> Optional[tuple[str, str]]:
    """Return the (prefix, tier) with longest realpath match, or None.

    A prefix matches if either the path equals the prefix exactly or starts
    with ``prefix + os.sep`` (or the prefix already ends with ``/``).  Ties
    on length are broken by tier severity (more restrictive wins).
    """
    best: Optional[tuple[str, str]] = None
    best_len = -1
    best_rank = -1
    for prefix, tier in prefixes:
        norm = prefix.rstrip(os.sep)
        if real == norm or real.startswith(norm + os.sep):
            plen = len(norm)
            rank = _TIER_RANK.get(tier, 0)
            if plen > best_len or (plen == best_len and rank > best_rank):
                best = (prefix, tier)
                best_len = plen
                best_rank = rank
    return best


def path_tier(path: str) -> str:
    """Classify *path* into ``T0``, ``T1``, or ``T2``.

    Resolves symlinks first (``os.path.realpath``) so a T2 symlink that
    points inside the platform repo is correctly classified T1.  Most-
    specific prefix (longest realpath match) wins; ties resolve to the
    most-restrictive tier (T0 > T1 > T2).

    Any exception logs at WARNING and returns ``T1`` (platform-protected).
    """
    try:
        real = os.path.realpath(os.path.expanduser(str(path)))
    except Exception:  # noqa: BLE001
        logger.warning("path_tier: realpath() failed for %r; defaulting to T1", path)
        return PATH_TIER_T1

    try:
        # Hardcoded T0 always wins (the FLOOR for invisibility).
        if _match_t0(real):
            return PATH_TIER_T0

        # User overrides (config) — union with hardcoded.  These can ADD T0
        # or T1 classifications; they cannot demote hardcoded T1 to T2.
        override_prefixes: list[tuple[str, str]] = []
        for entry in _load_path_overrides():
            try:
                p = os.path.realpath(os.path.expanduser(entry["prefix"]))
            except Exception:  # noqa: BLE001
                continue
            override_prefixes.append((p, entry["tier"]))

        # T0 overrides take priority over everything below.
        t0_override = _best_prefix_match(
            real, [pt for pt in override_prefixes if pt[1] == PATH_TIER_T0],
        )
        if t0_override is not None:
            return PATH_TIER_T0

        # Hardcoded T1 prefixes (repo source tree).
        repo_root = _resolve_repo_root()
        t1_prefixes: list[tuple[str, str]] = []
        if repo_root:
            for p in _hardcoded_t1_prefixes(repo_root):
                t1_prefixes.append((p, PATH_TIER_T1))

        # Combine hardcoded T1 with user T1 overrides for most-specific match.
        combined = list(t1_prefixes) + [
            pt for pt in override_prefixes if pt[1] == PATH_TIER_T1
        ]
        best = _best_prefix_match(real, combined)
        if best is not None:
            return PATH_TIER_T1

        # User T2 overrides are advisory only — they cannot demote a path
        # that was already classified as T0 or T1 above.  At this point
        # nothing more restrictive matched, so accept the override if any.
        t2_override = _best_prefix_match(
            real, [pt for pt in override_prefixes if pt[1] == PATH_TIER_T2],
        )
        if t2_override is not None:
            return PATH_TIER_T2

        # Default: T2 if under carpenter_home, else also T2.  We can't
        # safely classify "unknown location" paths as anything safer than
        # T1, but the task spec says "Anything else not matching T0 or T1"
        # is T2.  If the repo root couldn't be resolved (no carpenter
        # available), we fall through to T1 (fail-closed).
        if repo_root is None:
            logger.warning(
                "path_tier: repo root unresolved; defaulting %r to T1", path,
            )
            return PATH_TIER_T1
        return PATH_TIER_T2
    except Exception:  # noqa: BLE001 — last-resort fail-closed
        logger.warning("path_tier: classification error for %r; defaulting to T1", path, exc_info=True)
        return PATH_TIER_T1


def change_category(path: str) -> str:
    """Return the change-category for *path*.

    Returns one of ``"python"``, ``"yaml"``, ``"kb"``, ``"unknown"``.

    Rules:
    - ``.py`` → ``python``
    - ``.yaml`` / ``.yml`` → ``yaml``
    - Path contains ``/kb/`` segment AND ends with ``.md`` → ``kb``
    - Inside ``<repo>/docs/`` AND ends with ``.md`` → ``kb``
    - Otherwise → ``unknown``

    On any error, returns ``"unknown"`` (no fail-closed escalation here —
    category is advisory; the tier classifier is the security boundary).
    """
    try:
        norm = os.path.expanduser(str(path))
        lower = norm.lower()
        if lower.endswith(".py"):
            return "python"
        if lower.endswith(".yaml") or lower.endswith(".yml"):
            return "yaml"
        if lower.endswith(".md"):
            # `/kb/` segment match — exact path-segment containment
            # (works on both raw and realpath; we don't strictly need
            # realpath here since extensions and segments are advisory).
            if os.sep + "kb" + os.sep in norm or "/kb/" in norm:
                return "kb"
            # docs/ under repo root
            try:
                real = os.path.realpath(norm)
                repo_root = _resolve_repo_root()
                if repo_root:
                    docs_root = os.path.join(repo_root, "docs")
                    if real == docs_root or real.startswith(docs_root + os.sep):
                        return "kb"
            except Exception:  # noqa: BLE001
                pass
        return "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def is_invisible(path: str) -> bool:
    """Return True if *path* is classified as T0 (invisible)."""
    return path_tier(path) == PATH_TIER_T0


# ── Workflow selection ───────────────────────────────────────────────────

# Most-restrictive category order (first present wins).
_CATEGORY_ORDER: tuple[str, ...] = ("unknown", "python", "yaml", "kb")


def _change_workflow_for(category: str) -> str:
    """Look up the template name for *category* in CONFIG (live).

    Falls back to :data:`_DEFAULT_CHANGE_WORKFLOWS`, and ultimately to
    ``"coding-change"`` if nothing else resolves.
    """
    try:
        block = _config_module.CONFIG.get("platform_integrity") or {}  # type: ignore[attr-defined]
        wf = block.get("change_workflows") if isinstance(block, dict) else None
        if isinstance(wf, dict):
            name = wf.get(category)
            if isinstance(name, str) and name:
                return name
    except Exception:  # noqa: BLE001
        pass
    return _DEFAULT_CHANGE_WORKFLOWS.get(category, "coding-change")


def select_workflow_for_paths(paths: list[str]) -> tuple[str, bool]:
    """Choose change-workflow template and human-review forcing for *paths*.

    Returns ``(template_name, force_human)``.  Logic:

    1. Classify every path's ``(tier, category)``.
    2. If any path is T1, set ``force_human=True``.  (T0 paths shouldn't
       reach here in normal flow, but if they do they also force human.)
    3. Pick category by most-restrictive rule: ``unknown > python > yaml > kb``.
       The first category in that order which is present in the inputs wins.
    4. Resolve the template via CONFIG ``platform_integrity.change_workflows``,
       falling back to defaults.
    """
    categories: set[str] = set()
    force_human = False
    for p in paths or []:
        tier = path_tier(p)
        if tier in (PATH_TIER_T0, PATH_TIER_T1):
            force_human = True
        categories.add(change_category(p))

    chosen: Optional[str] = None
    for cat in _CATEGORY_ORDER:
        if cat in categories:
            chosen = cat
            break
    if chosen is None:
        chosen = "unknown"

    template = _change_workflow_for(chosen)
    return template, force_human


# ── Audit wrapper ────────────────────────────────────────────────────────

_AUDIT_NAMESPACE = "integrity."


def audit_path_decision(
    arc_id: int | None,
    event_type: str,
    path: str,
    details: dict,
) -> None:
    """Record a path-classification decision to the trust audit log.

    Thin wrapper over :func:`carpenter.core.trust.audit.log_trust_event`.
    Adds the stable ``"integrity."`` namespace prefix.  Never raises —
    swallows audit-import errors with a WARNING so classification logic is
    not blocked by audit-layer issues.
    """
    full_event = event_type if event_type.startswith(_AUDIT_NAMESPACE) else _AUDIT_NAMESPACE + event_type
    enriched = dict(details) if details else {}
    enriched.setdefault("path", path)
    try:
        # Late import — keeps this module importable without a DB / trust
        # subsystem present (e.g. during unit tests that don't need audit).
        from ..core.trust.audit import log_trust_event  # type: ignore
        log_trust_event(arc_id, full_event, enriched)
    except Exception:  # noqa: BLE001
        logger.warning(
            "audit_path_decision: failed to record event %r for %r; continuing",
            full_event, path, exc_info=True,
        )


__all__ = [
    "PATH_TIER_T0",
    "PATH_TIER_T1",
    "PATH_TIER_T2",
    "path_tier",
    "change_category",
    "is_invisible",
    "select_workflow_for_paths",
    "audit_path_decision",
]

"""Unified seed installer for Carpenter config_seed/ content.

Provides a single entry point (``install_config_seed``) that iterates a
declarative manifest mapping subdirectories under ``config_seed/`` to their
on-disk install targets relative to a ``base_dir``.

This replaces the previously duplicated "copytree-if-missing" pattern that
lived in :mod:`carpenter.prompts`, :mod:`carpenter.kb`, and :mod:`carpenter.db`.
The legacy functions still exist as thin wrappers that delegate here.

The manifest is the SINGLE source of truth for seed-subdir -> install-target.
Do NOT add new manifest entries unless there is a corresponding existing
installer or an explicit decision to start seeding that content — several
directories under ``config_seed/`` (``chat_tools/``, ``templates/``, ...) are
sourced at runtime rather than copied on first install.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def _config_seed_root() -> Path:
    """Return the absolute path to the repo-root ``config_seed/`` directory."""
    return Path(__file__).resolve().parent.parent / "config_seed"


@dataclass(frozen=True)
class SeedTarget:
    """One entry in the seed install manifest.

    Attributes:
        name: Logical name used as the key in the result dict and for overrides.
        seed_subdir: Path under ``config_seed/`` holding the seed content.
        default_rel_target: Default install target relative to ``base_dir``.
        count_glob: Glob pattern used (with ``rglob``) to count installed files
            for logging / return value. Matches the legacy installers' counters.
        label: Human-readable label for log messages.
    """

    name: str
    seed_subdir: str
    default_rel_target: str
    count_glob: str
    label: str


# Single source of truth: config_seed/<seed_subdir> -> <base_dir>/<default_rel_target>.
# Covers exactly the four install sites that previously had their own function.
SEED_MANIFEST: tuple[SeedTarget, ...] = (
    SeedTarget(
        name="prompts",
        seed_subdir="prompts",
        default_rel_target="config/prompts",
        count_glob="*.md",
        label="Prompt",
    ),
    SeedTarget(
        name="coding-prompts",
        seed_subdir="coding-prompts",
        default_rel_target="config/coding-prompts",
        count_glob="*.md",
        label="Coding prompt",
    ),
    SeedTarget(
        name="kb",
        seed_subdir="kb",
        default_rel_target="config/kb",
        count_glob="*.md",
        label="KB seed",
    ),
    SeedTarget(
        name="data_models",
        seed_subdir="data_models",
        default_rel_target="data_models",
        count_glob="*.py",
        label="Data model",
    ),
)


def _install_one(seed_dir: Path, target_dir: Path, label: str, count_glob: str) -> dict:
    """Copy ``seed_dir`` to ``target_dir`` iff the target doesn't already exist.

    Returns a status dict:
        {"status": "installed" | "exists" | "no_defaults" | "error", "copied": int}
    """
    if target_dir.is_dir():
        return {"status": "exists", "copied": 0}

    if not seed_dir.is_dir():
        logger.warning("%s seed directory not found: %s", label, seed_dir)
        return {"status": "no_defaults", "copied": 0}

    try:
        shutil.copytree(str(seed_dir), str(target_dir))
        count = sum(1 for _ in target_dir.rglob(count_glob))
        logger.info("Installed %s defaults: %d files to %s", label, count, target_dir)
        return {"status": "installed", "copied": count}
    except OSError as exc:
        logger.error("Failed to install %s defaults: %s", label, exc)
        return {"status": "error", "error": str(exc), "copied": 0}


def install_config_seed(
    base_dir: str | os.PathLike | None,
    *,
    overrides: dict[str, Path] | None = None,
    manifest: tuple[SeedTarget, ...] = SEED_MANIFEST,
) -> dict:
    """Install every seed target in the manifest.

    Args:
        base_dir: Base directory; targets resolve as ``base_dir/<rel_target>``
            unless overridden. May be ``None`` only if every target has an
            override; otherwise a ``ValueError`` is raised.
        overrides: Map of logical name -> absolute target path, used to
            override per-target install locations (tests, atypical layouts).
        manifest: Manifest to iterate; exposed for tests.

    Returns:
        Dict keyed by logical name with the per-target status dict
        (same shape as the legacy installers).
    """
    overrides = overrides or {}
    seed_root = _config_seed_root()
    base_path = Path(base_dir) if base_dir else None

    results: dict[str, dict] = {}
    for entry in manifest:
        if entry.name in overrides:
            target = Path(overrides[entry.name])
        else:
            if base_path is None:
                raise ValueError(
                    f"install_config_seed: base_dir is required when no "
                    f"override is provided for target {entry.name!r}"
                )
            target = base_path / entry.default_rel_target

        seed_dir = seed_root / entry.seed_subdir
        results[entry.name] = _install_one(
            seed_dir, target, entry.label, entry.count_glob,
        )

    return results


def install_single_target(
    name: str,
    target_dir: str | os.PathLike,
    *,
    manifest: tuple[SeedTarget, ...] = SEED_MANIFEST,
) -> dict:
    """Install a single named seed target at ``target_dir``.

    Convenience wrapper used by the legacy per-target installer functions.
    """
    entry = next((e for e in manifest if e.name == name), None)
    if entry is None:
        raise KeyError(f"Unknown seed target: {name!r}")
    seed_dir = _config_seed_root() / entry.seed_subdir
    return _install_one(seed_dir, Path(target_dir), entry.label, entry.count_glob)

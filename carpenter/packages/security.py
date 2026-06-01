"""Capability-package security guards (Phase A).

These guards run at package-load time, BEFORE any of the package's code
is imported.  They enforce a small set of trust-model invariants that
the package framework would otherwise let a third-party package smuggle
past:

* **I10 (chat-tool trust boundaries)** — Packages must not declare
  chat tools at ``trust_boundary='platform'``.  ``PLATFORM_TOOLS`` is a
  hardcoded frozenset in :mod:`carpenter.chat_tool_registry`; user
  config and packages alike are confined to the chat boundary.  This
  guard is *defense in depth* — :func:`carpenter.chat_tool_registry.validate_tool_defs`
  also rejects platform-boundary chat tools at registration time, but
  catching the violation at manifest-load time means a bad package
  fails before any of its code runs.
* **I3 (only JUDGE promotes U→T)** — Packages may not ship JUDGE code.
  JUDGE arcs run deterministic platform-policy checks
  (``security/judge.py``); allowing packages to override that surface
  would let a malicious package promote arbitrary untrusted state.
* **I9 (default-deny policy literals)** — Packages may not pre-populate
  ``Email`` / ``Domain`` / etc. policy allowlists.  Allowlists are
  user-controlled state.  A package may ship KB articles suggesting
  "you may want to add your own email", but it must not auto-add.
* **KB scoping** — A package's KB articles must live under its declared
  ``kb_namespace`` (defaults to its name).  Phase A doesn't yet load
  KB articles from packages, but this guard is wired up so that the
  Phase B email package cannot accidentally seed ``kb/web/*`` and
  prompt-poison adjacent capabilities.
* **No bundled .env** — Packages must not ship credential bytes.  Per
  ``docs/2026-04-30_d8-capability-package-phase-a-plan.md`` §5.5 item 6,
  credentials are user-input at install time only.

The guards run against the parsed :class:`~carpenter.packages.manifest.PackageManifest`
plus a directory walk of the package source.  No package code is
imported during validation.

If any check fails, :class:`PackageSecurityError` is raised with an
explanation.  Callers (the registry) are expected to skip the package
entirely on failure — partial loading is never safe.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .manifest import PackageManifest

logger = logging.getLogger(__name__)


# Manifest fields whose mere PRESENCE indicates a trust-boundary
# violation.  PRIMARY GATE: the packages.manifest loader's strict
# ``_ALLOWED_FIELDS`` allowlist already rejects ANY unknown manifest
# key.  This frozenset exists so that violations of these specific
# trust-relevant keys produce a clear, security-flavored error message
# (instead of a generic "unknown field" complaint) and so that the
# raw-YAML check fires BEFORE the typed loader silently strips the
# field if the loader's allowlist is ever widened by accident.
#
# We only enumerate keys whose MEANING is explicitly trust-violating
# (JUDGE code, policy seeds, credentials, trust-boundary overrides).
# Cosmetic allowlist names (``egress_allowlist``, ``domain_allowlist``,
# ``command_allowlist``) are NOT in this set: the manifest's
# ``_ALLOWED_FIELDS`` already rejects them as unknown fields, and
# adding them here would just duplicate that gate without changing
# behavior.
# NOTE: ``judge_handlers`` is allowed as a manifest field as of D24
# stage 3a — packages may now declare deterministic JUDGE handlers
# (the package's JUDGE *is* the JUDGE for its template; D24 SD7 / Q2).
# The trust-boundary contract is preserved: package JUDGE handlers run
# only against the package's own templates, never replacing the
# platform JUDGE for platform-shipped templates.  The ``judge`` and
# ``judge_handler`` (singular) keys remain forbidden so that older /
# looser spellings still surface a security-flavored error.
_FORBIDDEN_RAW_KEYS = frozenset({
    # Pre-populating policy allowlists violates I9.
    "policy_seed",
    "policy_allowlist",
    # Older / singular JUDGE-shipping spellings — still forbidden.
    "judge",
    "judge_handler",
    # Trust-boundary overrides at the manifest level violate I10.
    "trust_boundary",
    "platform_tools",
    # Bundled credentials forbidden — must be user input at install time.
    "env_file",
    "credentials",
    "secrets",
})


class PackageSecurityError(Exception):
    """Raised when a package fails Phase A trust-boundary validation."""


def _check_no_forbidden_raw_keys(
    raw_manifest: dict,
    *,
    manifest_path: Path,
) -> None:
    """Reject manifests that mention any forbidden top-level key.

    Operates on the raw YAML dict (NOT the parsed PackageManifest), so
    that a malicious or accidental manifest cannot smuggle a JUDGE
    declaration past the typed loader.

    The manifest loader itself already rejects unknown fields; this
    exists to produce a clearer, security-flavored error message and
    to fail closed if the loader's allowlist is ever widened without
    re-thinking the trust model.
    """
    found = sorted(set(raw_manifest.keys()) & _FORBIDDEN_RAW_KEYS)
    if found:
        raise PackageSecurityError(
            f"Manifest {manifest_path} contains forbidden field(s) "
            f"{found}.  Capability packages may not declare JUDGE code, "
            f"trust-boundary overrides, pre-populated policy allowlists, "
            f"or bundled credentials.  See docs/trust-invariants.md "
            f"(I3, I9, I10) and docs/2026-04-30_d8-capability-package-"
            f"phase-a-plan.md §5.5.",
        )


def _check_no_env_files(package_root: Path) -> None:
    """Reject packages that ship any ``.env`` files.

    Credentials must be user input at install time, never package-bundled.
    Walks the package directory recursively so that ``.env`` hidden
    inside a subdir still trips the check.

    ``.envrc`` (direnv) is rejected by virtue of not matching the
    example/template/sample suffix allowlist below — it falls through
    to the raise.  A separate explicit branch for it would be
    redundant.
    """
    for env_file in package_root.rglob(".env*"):
        # Skip explicit example / template files — these are docs, not
        # credential bytes — but ONLY when they have a clearly-marked
        # extension.  ``.env.example`` documents what env vars to set.
        if env_file.suffix in {".example", ".template", ".sample"}:
            continue
        raise PackageSecurityError(
            f"Package at {package_root} ships {env_file.name!r} "
            f"({env_file}).  Capability packages may not bundle "
            f"credential files; credentials must come from user input "
            f"at install time (Phase A spec §5.5 item 6).",
        )


def _check_kb_scoping(
    manifest: PackageManifest, *, package_root: Path,
) -> None:
    """Restrict any KB content to the package's declared namespace.

    Phase A does not yet load KB articles from packages, but if a
    package ships a ``kb/`` directory, every entry under it MUST live
    inside ``kb/<kb_namespace>/`` so that Phase B+ KB seeding cannot
    accidentally cross-pollinate other namespaces.
    """
    kb_root = package_root / "kb"
    if not kb_root.is_dir():
        return  # No KB content to scope — nothing to enforce.

    expected_prefix = kb_root / manifest.kb_namespace
    for entry in kb_root.rglob("*"):
        if not entry.is_file():
            continue
        try:
            entry.relative_to(expected_prefix)
        except ValueError:
            raise PackageSecurityError(
                f"Package {manifest.name!r} ships KB entry {entry} "
                f"outside its declared namespace "
                f"{manifest.kb_namespace!r}.  Packages may only seed "
                f"KB under kb/{manifest.kb_namespace}/ (defends "
                f"against skill-knowledge cross-pollination).",
            ) from None


def _check_chat_tool_paths(
    manifest: PackageManifest, *, package_root: Path,
) -> None:
    """Each declared chat-tool path must be a real file under the package root.

    Defends against path traversal (``../foo.py``) and dangling
    references.  We don't import the module here — that happens later
    via the existing ``chat_tool_loader`` machinery, which performs
    @chat_tool decorator validation (capabilities, trust_boundary).
    """
    for rel in manifest.chat_tools:
        rel_path = Path(rel)
        if rel_path.is_absolute():
            raise PackageSecurityError(
                f"Package {manifest.name!r} declares chat_tool "
                f"{rel!r} as an absolute path.  Chat-tool paths must "
                f"be relative to the package root.",
            )
        # Resolve relative to package root and confirm it stays inside.
        candidate = (package_root / rel_path).resolve()
        try:
            candidate.relative_to(package_root.resolve())
        except ValueError:
            raise PackageSecurityError(
                f"Package {manifest.name!r}: chat_tool path {rel!r} "
                f"escapes package root {package_root}",
            ) from None
        if not candidate.is_file():
            raise PackageSecurityError(
                f"Package {manifest.name!r}: chat_tool {rel!r} not "
                f"found at {candidate}",
            )
        if candidate.suffix != ".py":
            raise PackageSecurityError(
                f"Package {manifest.name!r}: chat_tool {rel!r} must "
                f"be a .py file, got {candidate.suffix!r}",
            )


def validate_manifest_security(
    manifest: PackageManifest,
    *,
    raw_manifest: dict,
    manifest_path: Path,
) -> None:
    """Run all Phase A security guards on a parsed manifest.

    Args:
        manifest: The shape-validated manifest.
        raw_manifest: The raw YAML dict (pre-parse).  Used for the
            forbidden-keys check so that smuggled fields are caught
            before the typed loader silently strips them.
        manifest_path: Path to the manifest file (for error messages).

    Raises:
        PackageSecurityError: If any guard rejects the package.  The
            registry MUST treat this as a hard fail — partial loading
            of a security-rejected package is never safe.
    """
    package_root = manifest.source_path

    _check_no_forbidden_raw_keys(raw_manifest, manifest_path=manifest_path)
    _check_no_env_files(package_root)
    _check_kb_scoping(manifest, package_root=package_root)
    _check_chat_tool_paths(manifest, package_root=package_root)

    logger.debug(
        "Package %r passed Phase A security guards (%s)",
        manifest.name, manifest_path,
    )

"""Live load of newly installed capability packages (no restart).

Installing a package copies files and writes an ``installed_packages``
row, but the components the package contributes — chat tools, arc
templates, JUDGE handlers, data models, step handlers, granted platform
capability verbs, in-process triggers — are registered by the *startup*
discovery pass (``registry.discover_and_register``, called once from
:class:`carpenter.coordinator.Coordinator`).  Until now, "install X then
use X" therefore ended with "restart the server".

:func:`reload_packages` closes that gap for the case where it is sound:

* A package installed for the **first time** in this process has no
  entries in ``sys.modules`` under ``_carpenter_pkg_.<name>.`` — nothing
  stale can be served — so its components can be registered live, and
  they are registered by calling the very same registry methods startup
  calls (:meth:`PackageRegistry.prepare_manifest`,
  :meth:`PackageRegistry.register_package`,
  :meth:`PackageRegistry.cross_validate_tools`).
* A package whose modules are **already cached** cannot be re-loaded.
  ``loaders._import_package_module`` caches every module it imports
  under ``_carpenter_pkg_.<pkg>.<dotted>`` and returns the cache on
  re-entry, so re-registering a re-installed package would run the OLD
  module bodies while the operator believes the new ones are live.
  :func:`reload_packages` refuses, with ``needs_restart=True``.

**Why we do not purge ``sys.modules`` to force a re-import.**  Deleting
the ``_carpenter_pkg_.<pkg>.*`` entries would make the next import read
the new files, but it would NOT retract anything built from the old
ones, and the platform holds many such references: ``LoadedTool``
objects in ``chat_tool_loader._loaded_tools`` / ``_extension_tools``
(bound function objects), JUDGE handlers and data-model *classes* in
``PackageHandlerRegistry`` (class identity is compared on
deserialisation), capability handlers plus the T1 trust classification
of their files, running ``Trigger`` instances, step handlers held by the
engine registry, and any in-flight arc holding an instance of a
package-shipped dataclass.  A purge would leave two generations of each
class alive, which is exactly the mismatched-identity failure the long
comment at ``loaders.py:133`` records.  Doing it safely needs a
retract-then-rebuild protocol: unregister every component of that one
package from each registry (the uninstall path already does part of
this), quiesce or reject work that holds its objects, purge the
namespace, re-import, re-register, and fail closed by keeping the old
generation if any step fails.  That is a work package of its own; a
restart achieves the same thing for free.  See the WP8 report.

Deliberately NOT done here: no restart, no process exit, no
restart-pending flag, no config write, no chat tool and no HTTP route.
This is a library function; a sibling work package exposes it.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

# ``_PKG_NAMESPACE`` ("_carpenter_pkg_") is imported from the loader
# rather than re-spelled here, so the cache check and the cache itself
# can never disagree about the prefix.
from .loaders import _PKG_NAMESPACE
from .manifest import PackageManifest
from .registry import (
    PackageRegistry,
    RegisteredPackage,
    configured_search_paths,
    get_registry,
)

logger = logging.getLogger(__name__)

_EMPTY: Mapping[str, int] = MappingProxyType({})

# One reload at a time per process.  A reload mutates process-wide
# registries (chat tools, handlers, capabilities, triggers); two
# concurrent reloads could interleave a package's registrations.
_RELOAD_LOCK = threading.Lock()


@dataclass(frozen=True)
class PackageLoadOutcome:
    """What :func:`reload_packages` did about one package.

    Attributes:
        name: Package name (the manifest's, or the directory's when the
            manifest could not be parsed).
        loaded: True when this call registered the package's components
            into the live process.
        needs_restart: True when the package cannot be loaded (or
            re-loaded) without restarting the platform — i.e. its
            modules are already cached in ``sys.modules``.
        reason: Why a restart is needed, or what was registered.
        components: ``{component_type: count}`` for what this call
            registered (or, for an already-registered package, what it
            had registered).  Component types are ``chat_tools`` plus
            the loader keys (``arc_templates``, ``judge_handlers``,
            ``data_models``, ``step_handlers``,
            ``platform_capabilities``, ``triggers``) and
            ``trigger_subscriptions``.
    """

    name: str
    loaded: bool
    needs_restart: bool
    reason: str
    components: Mapping[str, int] = field(default_factory=lambda: _EMPTY)


def cached_module_names(package_name: str) -> tuple[str, ...]:
    """Return this package's module names currently in ``sys.modules``.

    Only genuine submodules count (``_carpenter_pkg_.<name>.``).  The
    bare ``_carpenter_pkg_.<name>`` namespace module is a synthetic
    container ``_import_package_module`` creates before it even checks
    that the target file exists (``loaders.py:93``), so it can be left
    behind by a failed import and carries no package code.
    """
    prefix = f"{_PKG_NAMESPACE}.{package_name}."
    return tuple(
        sorted(m for m in list(sys.modules) if m.startswith(prefix))
    )


def _components_of(entry: RegisteredPackage) -> Mapping[str, int]:
    """Summarise a registered package's components as counts."""
    out: dict[str, int] = {}
    if entry.chat_tool_names:
        out["chat_tools"] = len(entry.chat_tool_names)
    for key, count in (entry.artifact_counts or {}).items():
        if count:
            out[str(key)] = int(count)
    return MappingProxyType(out)


def _register_subscriptions(manifest: PackageManifest) -> int:
    """Re-register the package's trigger subscriptions from its record.

    The same path startup uses: read the install dir's
    ``_subscriptions.json`` (the restart-surviving source of truth) and
    hand it to ``subscriptions.load_package_subscriptions``, which drops
    this package's prior registrations before re-adding them.  Scoped to
    one package — it never touches another package's subscriptions.
    """
    from ..core.engine import subscriptions as _subs
    from .installer import _subscriptions_record_path

    record = _subscriptions_record_path(Path(manifest.source_path))
    if not record.is_file():
        return 0
    try:
        entries = json.loads(record.read_text())
    except (ValueError, OSError):
        logger.warning(
            "Could not read package subscriptions for %r at %s",
            manifest.name, record,
        )
        return 0
    if not isinstance(entries, list) or not entries:
        return 0
    return _subs.load_package_subscriptions([(manifest.name, entries)])


def _describe_registration(
    entry: RegisteredPackage, components: Mapping[str, int],
) -> str:
    """Build the human-readable ``reason`` for a successful load."""
    summary = ", ".join(f"{n} {k}" for k, n in components.items())
    reason = f"registered {summary}" if summary else (
        "registered (the package declares no loadable components)"
    )
    if entry.gated_chat_tool_names:
        reason += (
            f"; {len(entry.gated_chat_tool_names)} write-capable chat "
            f"tool(s) gated (operator opt-in required)"
        )
    if entry.load_errors:
        reason += (
            f"; {len(entry.load_errors)} non-fatal load error(s): "
            + " | ".join(entry.load_errors)
        )
    return reason


def _outcome_for(
    registry: PackageRegistry,
    name: str,
    manifest: PackageManifest | None,
    skip_reason: str | None,
    *,
    conn,
) -> PackageLoadOutcome:
    """Apply (or refuse) the live registration of one prepared package."""
    # 1. Startup's fatal-per-package skips: a rejected manifest, a
    #    failed security guard, or — the one that matters most — a
    #    package whose on-disk hash does not match its
    #    ``installed_packages`` row.  ``prepare_manifest`` has already
    #    logged it at ERROR exactly as a cold start would; we report it
    #    and move on to the next package, never load it.
    if manifest is None or skip_reason is not None:
        return PackageLoadOutcome(
            name=name,
            loaded=False,
            needs_restart=False,
            reason=skip_reason or "skipped",
            components=_EMPTY,
        )

    cached = cached_module_names(name)
    already = registry.get(name)

    # 2. Already registered in this process (startup, or an earlier
    #    reload).  Nothing to do — re-registering would duplicate
    #    templates, judges and trigger subscriptions.
    if already is not None:
        if cached:
            return PackageLoadOutcome(
                name=name,
                loaded=False,
                needs_restart=True,
                reason=(
                    f"already loaded in this process and {len(cached)} of "
                    f"its module(s) are cached in sys.modules under "
                    f"'{_PKG_NAMESPACE}.{name}.' ({cached[0]}); a "
                    f"re-import would return the old module bodies, so "
                    f"changed code needs a platform restart"
                ),
                components=_components_of(already),
            )
        return PackageLoadOutcome(
            name=name,
            loaded=False,
            needs_restart=False,
            reason="already registered in this process",
            components=_components_of(already),
        )

    # 3. Not registered, but its modules are already cached.  This is
    #    the honest refusal: the bytes on disk are not what an import
    #    would return.  It happens on a re-install of a package loaded
    #    at startup, and also when something imported the package's
    #    python *before* installing it — notably
    #    ``installer.classify_package_chat_tools``, the import-based
    #    install preview, which caches the SOURCE tree's modules under
    #    this same package namespace.
    if cached:
        return PackageLoadOutcome(
            name=name,
            loaded=False,
            needs_restart=True,
            reason=(
                f"{len(cached)} module(s) are already cached in "
                f"sys.modules under '{_PKG_NAMESPACE}.{name}.' "
                f"({cached[0]}); loaders._import_package_module returns "
                f"the cache, so registering now would run the old module "
                f"bodies — a platform restart is required"
            ),
            components=_EMPTY,
        )

    # 4. Fresh: register through the same code path startup uses.
    try:
        entry = registry.register_package(manifest, db_conn=conn)
    except Exception as exc:  # noqa: BLE001 — one bad package only
        # Startup is "log and continue" per package for every
        # *expected* failure (manifest, security, hash pin, and each
        # individual loader).  An unexpected raise from the registry
        # loop body, though, propagates out of discover_and_register
        # and the coordinator swallows it for the WHOLE discovery pass,
        # losing every package after the bad one.  A live reload must
        # not do that: it reports per package, so it keeps going.
        logger.exception(
            "Live reload: registering package %r raised; skipping it "
            "and continuing with the others", name,
        )
        return PackageLoadOutcome(
            name=name,
            loaded=False,
            needs_restart=False,
            reason=f"registration failed: {type(exc).__name__}: {exc}",
            components=_EMPTY,
        )

    components = dict(_components_of(entry))
    try:
        n_subs = _register_subscriptions(manifest)
    except Exception:  # noqa: BLE001 — best-effort, as at startup
        logger.exception(
            "Live reload: re-registering subscriptions for %r failed", name,
        )
        n_subs = 0
    if n_subs:
        components["trigger_subscriptions"] = n_subs

    frozen = MappingProxyType(components)
    return PackageLoadOutcome(
        name=name,
        loaded=True,
        needs_restart=False,
        reason=_describe_registration(entry, frozen),
        components=frozen,
    )


def _reload(conn, requested: tuple[str, ...] | None) -> tuple[PackageLoadOutcome, ...]:
    registry = get_registry()
    search_paths, install_paths = registry.resolve_search_paths(
        configured_search_paths(),
    )
    manifest_pairs = registry._discover_manifests(
        search_paths, install_paths=install_paths,
    )

    outcomes: list[PackageLoadOutcome] = []
    seen: set[str] = set()
    loaded_any = False

    for manifest_path, is_install in manifest_pairs:
        manifest, skip_reason = registry.prepare_manifest(
            manifest_path, is_install=is_install, db_conn=conn,
        )
        # Name from the manifest, falling back to the directory name so
        # an unparseable manifest can still be reported (and filtered).
        name = (
            manifest.name if manifest is not None
            else manifest_path.parent.name
        )
        if requested is not None and name not in requested:
            continue
        seen.add(name)
        outcome = _outcome_for(
            registry, name, manifest, skip_reason, conn=conn,
        )
        loaded_any = loaded_any or outcome.loaded
        outcomes.append(outcome)

    # Cross-package chat-tool validation over the merged set, the same
    # pass startup runs after its discovery loop.  Only needed when
    # something new was registered.
    if loaded_any:
        registry.cross_validate_tools()

    # A name the caller asked for that discovery never saw is worth
    # reporting: the usual cause is an install written to a directory
    # the registry does not search (the chat tool and the CLI can
    # disagree about the install root).
    if requested is not None:
        for name in requested:
            if name in seen:
                continue
            outcomes.append(
                PackageLoadOutcome(
                    name=name,
                    loaded=False,
                    needs_restart=False,
                    reason=(
                        "not found: no installed package by that name on "
                        "the capability-package search paths ("
                        + ", ".join(str(p) for p in search_paths)
                        + ")"
                    ),
                    components=_EMPTY,
                )
            )

    if outcomes:
        logger.info(
            "Live package reload: %d loaded, %d need a restart, "
            "%d skipped",
            sum(1 for o in outcomes if o.loaded),
            sum(1 for o in outcomes if o.needs_restart),
            sum(1 for o in outcomes if not o.loaded and not o.needs_restart),
        )
    return tuple(outcomes)


def reload_packages(
    *,
    only: Sequence[str] | None = None,
    conn=None,
) -> tuple[PackageLoadOutcome, ...]:
    """Register newly installed capability packages in the live process.

    Safe to call while the platform is serving: it registers, never
    tears down.  In particular it does not call
    ``chat_tool_loader.load_chat_tools`` (which clears
    ``_extension_tools`` and would drop every package chat tool already
    registered), and it registers each package exactly once.

    Discovery, validation and the install hash-pin gate are the startup
    ones; a package whose on-disk bytes do not match its
    ``installed_packages`` hash is logged at ERROR and skipped, never
    loaded.

    Idempotent: a package already registered in this process is
    reported, not re-registered.

    Args:
        only: Register only these package names.  A requested name that
            discovery does not find still gets an outcome, so a caller
            can tell "installed somewhere we do not look" from
            "loaded".  ``None`` (default) considers every discovered
            package.
        conn: Open SQLite connection to use.  Follow the installer's
            convention and pass the connection of the transaction you
            are already in — the registration path writes (arc
            templates land in ``workflow_templates``) and reads the
            install records that prove operator consent.  The caller
            owns the commit.  When omitted,
            a ``db_transaction()`` is opened for the call; that is
            refused with ``RuntimeError`` if this thread already holds
            one, because a second connection would deadlock on SQLite's
            WAL writer lock (``carpenter.db.get_db``).

    Returns:
        One :class:`PackageLoadOutcome` per package considered, in
        discovery order, with requested-but-missing names last.
    """
    requested: tuple[str, ...] | None = None
    if only is not None:
        requested = tuple(dict.fromkeys(str(n) for n in only))
        if not requested:
            return ()

    if conn is not None:
        with _RELOAD_LOCK:
            return _reload(conn, requested)

    # Respect db.py's per-thread transaction guard: inside an active
    # transaction the caller must thread its connection through.
    from ..db import _transaction_guard, db_transaction

    if getattr(_transaction_guard, "active", False):
        raise RuntimeError(
            "reload_packages() was called while a db_transaction() is "
            "active on this thread.  Pass that transaction's connection "
            "as conn=; opening a second connection inside it would "
            "deadlock on SQLite's WAL writer lock (see "
            "carpenter.db.get_db)."
        )
    with _RELOAD_LOCK:
        with db_transaction() as db:
            return _reload(db, requested)

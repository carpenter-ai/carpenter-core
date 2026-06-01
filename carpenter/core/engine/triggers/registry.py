"""Trigger registry — manages trigger type registration and instance lifecycle.

Trigger types are registered by class; instances are created from config.
The registry provides access to pollable and endpoint triggers for the
main loop and HTTP server respectively.

D24 Phase 3a (PR-B) — package-scoped registration
-------------------------------------------------
Capability-package-shipped trigger *types* are registered under a
``source_package`` tag so that uninstalling the package cleanly drops
both the type registration and any live instances.  See
:func:`unregister_for_package` for the inverse op called from the
installer's uninstall path.

Loading a manifest's ``triggers:`` block goes through
:func:`load_package_triggers`, which is a thin wrapper over
:func:`load_triggers` that threads ``source_package`` + a
``PackageStateHandle`` to each instance's constructor.
"""

import importlib
import importlib.util
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .base import Trigger, PollableTrigger, EndpointTrigger

if TYPE_CHECKING:
    from ....packages.state import PackageStateHandle
    from ....packages.vectors import PackageVectorStore

logger = logging.getLogger(__name__)

# Type registry: trigger_type string → Trigger subclass
_trigger_types: dict[str, type[Trigger]] = {}

# Mapping from registered trigger_type name → source_package name (or
# ``None`` for platform-builtin / user-plugin types).  Used by
# :func:`unregister_for_package` to drop only the types a package added.
_type_sources: dict[str, str | None] = {}

# Active trigger instances
_instances: list[Trigger] = []


def register_trigger_type(
    cls: type[Trigger], *, source_package: str | None = None,
) -> None:
    """Register a trigger class by its trigger_type() string.

    Args:
        cls: Trigger subclass to register.
        source_package: Optional capability package name that contributed
            this trigger type.  ``None`` for platform-builtin and
            user-plugin types.  Used by :func:`unregister_for_package`
            to drop only the types a package added when it's uninstalled.

    Raises:
        TypeError: If cls is not a Trigger subclass.
        ValueError: If trigger_type() is already registered.
    """
    if not (isinstance(cls, type) and issubclass(cls, Trigger)):
        raise TypeError(f"{cls} is not a Trigger subclass")

    type_name = cls.trigger_type()
    if type_name in _trigger_types:
        existing = _trigger_types[type_name]
        if existing is cls:
            # Idempotent for same class; keep the existing source tag.
            return
        raise ValueError(
            f"Trigger type {type_name!r} already registered by {existing.__name__}"
        )

    _trigger_types[type_name] = cls
    _type_sources[type_name] = source_package
    logger.debug(
        "Registered trigger type: %s (%s, source_package=%s)",
        type_name, cls.__name__, source_package,
    )


def get_trigger_type(type_name: str) -> type[Trigger] | None:
    """Look up a registered trigger class by type name."""
    return _trigger_types.get(type_name)


def load_triggers(
    trigger_configs: list[dict],
    *,
    source_package: str | None = None,
    package_state: "PackageStateHandle | None" = None,
    package_vectors: "PackageVectorStore | None" = None,
) -> list[Trigger]:
    """Instantiate triggers from config dicts.

    Each config dict must have:
        - name: unique trigger name
        - type: registered trigger type string
        - enabled: bool (default True)
        - ... additional type-specific config

    Args:
        trigger_configs: List of per-trigger config dicts.
        source_package: Optional capability-package name to thread into
            each instance's ``source_package`` kwarg.  ``None`` for
            platform / config-defined triggers (back-compat default).
        package_state: Optional :class:`PackageStateHandle` to thread
            into each instance's ``package_state`` kwarg.
        package_vectors: Optional :class:`PackageVectorStore` to thread
            into each instance's ``package_vectors`` kwarg (Phase 2
            PR-2 / D10).  Same signature-introspection rule as
            ``package_state``: only forwarded if the trigger class
            accepts it, so platform-builtin and legacy subclasses keep
            working unchanged.

    Returns list of instantiated triggers (only enabled ones).
    Appends to the global instance list.
    """
    # Constructing the kwargs we pass through to ``cls(...)``.  We
    # always thread ``source_package``, ``package_state`` and
    # ``package_vectors`` — even when they're ``None`` — so subclasses
    # that have learned about them see the consistent signature.  The
    # base Trigger.__init__ defaults to ``None`` for all three, so older
    # subclasses that don't yet accept the kwargs would break here; we
    # therefore inspect the constructor signature and only pass them
    # when accepted.
    import inspect

    triggers = []
    for cfg in trigger_configs:
        name = cfg.get("name")
        type_name = cfg.get("type")
        enabled = cfg.get("enabled", True)

        if not enabled:
            logger.debug("Skipping disabled trigger: %s", name)
            continue

        if not name or not type_name:
            logger.warning("Trigger config missing name or type: %s", cfg)
            continue

        cls = _trigger_types.get(type_name)
        if cls is None:
            logger.warning(
                "Unknown trigger type %r for trigger %r (registered: %s)",
                type_name, name, sorted(_trigger_types.keys()),
            )
            continue

        try:
            # Detect whether the subclass override of ``__init__`` (if
            # any) accepts each kwarg.  Anything inheriting the base
            # ``Trigger.__init__`` will, since we added them there.
            try:
                sig = inspect.signature(cls.__init__)
                params = sig.parameters
                # ``**kwargs`` covers anything; otherwise we need
                # explicit named parameters.
                accepts_var_kw = any(
                    p.kind is inspect.Parameter.VAR_KEYWORD
                    for p in params.values()
                )
                accepts_pkg_kwargs = accepts_var_kw or (
                    "source_package" in params and "package_state" in params
                )
                accepts_vectors_kwarg = accepts_var_kw or (
                    "package_vectors" in params
                )
            except (TypeError, ValueError):
                # Builtin / C-coded init we can't introspect; assume the
                # safe path.
                accepts_pkg_kwargs = False
                accepts_vectors_kwarg = False
            if accepts_pkg_kwargs:
                kwargs = {
                    "name": name,
                    "config": cfg,
                    "source_package": source_package,
                    "package_state": package_state,
                }
                if accepts_vectors_kwarg:
                    kwargs["package_vectors"] = package_vectors
                else:
                    # Legacy subclass that learned about source_package
                    # / package_state in Phase 3a but hasn't grown
                    # ``package_vectors`` yet.  Warn only when the
                    # caller actually has a vector handle to thread.
                    if package_vectors is not None:
                        logger.warning(
                            "Trigger type %r (%s) does not accept "
                            "package_vectors kwarg; package %r triggers "
                            "cannot use per-package vectors until the "
                            "trigger class is updated",
                            type_name, cls.__name__, source_package,
                        )
                instance = cls(**kwargs)
            else:
                # Legacy subclass that overrides __init__ with the old
                # 2-arg signature.  Fall back to the back-compat call,
                # and warn loudly if a package-scoped install asked for
                # state / vectors — that combination cannot work safely.
                if (
                    source_package is not None
                    or package_state is not None
                    or package_vectors is not None
                ):
                    logger.warning(
                        "Trigger type %r (%s) does not accept "
                        "source_package/package_state/package_vectors "
                        "kwargs; package %r triggers cannot use "
                        "per-package state or vectors until the trigger "
                        "class is updated",
                        type_name, cls.__name__, source_package,
                    )
                instance = cls(name=name, config=cfg)
            triggers.append(instance)
            _instances.append(instance)
            logger.info("Loaded trigger: %s (type=%s)", name, type_name)
        except Exception:
            logger.exception("Failed to instantiate trigger %s (type=%s)", name, type_name)

    return triggers


def load_package_triggers(
    trigger_configs: list[dict],
    *,
    source_package: str,
    package_state: "PackageStateHandle | None" = None,
    package_vectors: "PackageVectorStore | None" = None,
) -> list[Trigger]:
    """Convenience wrapper: load triggers for a specific capability package.

    Equivalent to :func:`load_triggers` with ``source_package`` set;
    refuses an empty / whitespace package name to make the intent
    explicit at the call site.  Used by the installer's
    ``_install_triggers`` helper.
    """
    if not source_package or not source_package.strip():
        raise ValueError("source_package must be a non-empty string")
    return load_triggers(
        trigger_configs,
        source_package=source_package,
        package_state=package_state,
        package_vectors=package_vectors,
    )


def load_user_triggers(directory: str) -> int:
    """Scan a directory for Python files defining custom trigger subclasses.

    Each .py file is imported. Any Trigger subclass with a trigger_type()
    method is automatically registered.

    Args:
        directory: Path to scan for trigger plugin files.

    Returns:
        Number of trigger types registered.
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        logger.debug("Trigger plugins directory does not exist: %s", directory)
        return 0

    registered = 0
    for py_file in sorted(dir_path.glob("*.py")):
        if py_file.name.startswith("_"):
            continue

        module_name = f"carpenter_trigger_plugin_{py_file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, str(py_file))
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Find and register Trigger subclasses
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, Trigger)
                    and attr not in (Trigger, PollableTrigger, EndpointTrigger)
                    and hasattr(attr, "trigger_type")
                    and not getattr(attr.trigger_type, "__isabstractmethod__", False)
                ):
                    try:
                        register_trigger_type(attr)
                        registered += 1
                    except (TypeError, ValueError) as exc:
                        logger.warning("Could not register %s from %s: %s", attr_name, py_file, exc)

        except Exception:
            logger.exception("Failed to load trigger plugin: %s", py_file)

    if registered:
        logger.info("Loaded %d user trigger type(s) from %s", registered, directory)
    return registered


def get_trigger_instances() -> list[Trigger]:
    """Return all active trigger instances."""
    return list(_instances)


def get_pollable_triggers() -> list[PollableTrigger]:
    """Return all PollableTrigger instances."""
    return [t for t in _instances if isinstance(t, PollableTrigger)]


def get_endpoint_triggers() -> list[EndpointTrigger]:
    """Return all EndpointTrigger instances."""
    return [t for t in _instances if isinstance(t, EndpointTrigger)]


def start_all() -> None:
    """Call start() on all trigger instances."""
    for trigger in _instances:
        try:
            trigger.start()
        except Exception:
            logger.exception("Failed to start trigger: %s", trigger.name)


def stop_all() -> None:
    """Call stop() on all trigger instances."""
    for trigger in _instances:
        try:
            trigger.stop()
        except Exception:
            logger.exception("Failed to stop trigger: %s", trigger.name)


def check_pollable_triggers() -> int:
    """Call check() on all PollableTrigger instances.

    Returns the number of triggers checked.
    """
    checked = 0
    for trigger in get_pollable_triggers():
        try:
            trigger.check()
            checked += 1
        except Exception:
            logger.exception("Error in pollable trigger check: %s", trigger.name)
    return checked


def instances_for_package(package_name: str) -> list[Trigger]:
    """Return all live trigger instances whose ``source_package`` matches.

    Useful for tests and for the installer's uninstall path so we can
    call ``stop()`` on each instance before dropping the type
    registration.
    """
    if not package_name:
        return []
    return [t for t in _instances if t.source_package == package_name]


def unregister_for_package(package_name: str) -> int:
    """Drop all triggers + trigger types contributed by ``package_name``.

    Called from :func:`carpenter.packages.installer.uninstall_package`.
    Idempotent.  Each removed instance has its ``stop()`` lifecycle hook
    invoked (best-effort — exceptions are logged but don't block other
    instances from being torn down).

    Returns the total number of (instance + type) registrations removed.
    """
    if not package_name:
        return 0

    # First, stop and drop instances tagged with this package.
    removed_instances = 0
    survivors: list[Trigger] = []
    for inst in _instances:
        if inst.source_package == package_name:
            try:
                inst.stop()
            except Exception:
                logger.exception(
                    "Error stopping trigger %s on package %r uninstall",
                    inst.name, package_name,
                )
            removed_instances += 1
        else:
            survivors.append(inst)
    _instances[:] = survivors

    # Then drop type registrations contributed by this package.
    removed_types = 0
    for type_name in [
        t for t, src in _type_sources.items() if src == package_name
    ]:
        _trigger_types.pop(type_name, None)
        _type_sources.pop(type_name, None)
        removed_types += 1

    if removed_instances or removed_types:
        logger.info(
            "Removed %d trigger instance(s) and %d trigger type(s) for "
            "package %r",
            removed_instances, removed_types, package_name,
        )
    return removed_instances + removed_types


def load_package_trigger_module(
    py_file: Path | str, *, source_package: str,
) -> int:
    """Import a Python file from a capability package and register any
    :class:`Trigger` subclasses it defines, tagged with ``source_package``.

    The file is imported once per call (no module caching) and any
    subclasses of :class:`Trigger` declared at module scope that have a
    concrete ``trigger_type()`` are registered.  Already-registered
    types are skipped (idempotent).

    Returns the number of new trigger types registered.
    """
    if not source_package or not source_package.strip():
        raise ValueError("source_package must be a non-empty string")
    path = Path(py_file)
    if not path.is_file():
        logger.warning(
            "Package trigger module not found: %s (package=%r)",
            path, source_package,
        )
        return 0
    module_name = f"carpenter_pkg_trigger_{source_package}_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            return 0
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        logger.exception(
            "Failed to import package trigger module %s (package=%r)",
            path, source_package,
        )
        return 0

    registered = 0
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (
            isinstance(attr, type)
            and issubclass(attr, Trigger)
            and attr not in (Trigger, PollableTrigger, EndpointTrigger)
            and hasattr(attr, "trigger_type")
            and not getattr(attr.trigger_type, "__isabstractmethod__", False)
        ):
            try:
                register_trigger_type(attr, source_package=source_package)
                registered += 1
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "Could not register %s from %s for package %r: %s",
                    attr_name, path, source_package, exc,
                )
    return registered


def reset() -> None:
    """Clear all registrations and instances. For testing only."""
    _trigger_types.clear()
    _type_sources.clear()
    _instances.clear()

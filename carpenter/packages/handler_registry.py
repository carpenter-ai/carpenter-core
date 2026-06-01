"""Process-wide registry for package-shipped runtime artifacts.

D24 stage 3b wires arc-templates, JUDGE handlers, and data-model
dataclasses from installed capability packages into the running
platform.  Stage 3a loaded the manifest declarations; this module
holds the *resolved* references — actual Python objects looked up by
name — so the platform's dispatch paths (``security/judge.py`` and
``core/engine/handler_registry.py``) can find them at runtime.

Three registries live here:

* **JUDGE handlers** — ``dict[template_name, callable]``.  Keyed by
  the template's flat unprefixed name (SD7).  The dispatch wrapper
  in :mod:`carpenter.security.judge` consults this registry first;
  a hit means the package's JUDGE *is* the JUDGE for that template
  (Q2).  If no package registers a handler for a template, the
  platform's default ``run_policy_checks`` runs as before.

* **Data-model classes** — ``dict[kind_name, type]``.  Keyed by the
  unprefixed dataclass name (SD7).  ``security/judge.py``
  ``_load_extraction_resource`` consults this registry as a fallback
  when ``_PLATFORM_KINDS`` doesn't recognise the kind on a Resource
  row.  Cross-package collisions are load errors.

* **Step handlers** — registered against the engine's step-handler
  registry (``core.engine.handler_registry``) since dispatch looks
  it up there.  We don't keep a separate copy here; we just track
  which (template, role) pairs each package contributed so that
  uninstall can revert them cleanly.

Trust-model invariants enforced HERE (in addition to the manifest +
security-guard checks at install time):

* **I3 (only JUDGE promotes U->T)** — package-shipped JUDGE handlers
  receive a *deserialised dataclass* from the dispatch wrapper, never
  raw bytes and never an arc-state dict.  The wrapper validates
  policy-typed fields against ``SecurityPolicies`` *in-process*
  before invoking the handler, so package code only does the
  structural / cross-field checks the type system can't express.
* **I7 / I10 (boundaries)** — packages cannot register handlers for
  templates they do not own.  The loader (``packages.loaders``) only
  registers the JUDGE handler keys declared in the package's own
  manifest, so a package cannot shadow a platform-shipped template's
  JUDGE.  The platform's handler set is exposed as
  ``_PLATFORM_TEMPLATES``, derived at startup from the
  ``config_seed/templates/`` directory listing (see
  ``_compute_platform_templates`` below); collisions with that set
  are load errors.
* **Collisions across packages** — when two packages declare the
  same template name (or two packages declare the same data-model
  class name), the second registration is rejected with a load
  error.  The first registration wins; the second package's
  ``RegisteredPackage.load_errors`` records the collision.

Everything in this module is process-local; restarts re-register
from ``installed_packages``.
"""

from __future__ import annotations

import functools
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


# B-full NIT #5: derive the platform-template set from the filesystem
# (``config_seed/templates/``) at startup so a new platform template
# can't drift out of sync with this gate.  Two-step approach:
#
# 1. The canonical source is the directory listing — every subdir of
#    ``config_seed/templates/`` is a platform template name (the
#    flat-file ``*.yaml`` form is the legacy layout; modern templates
#    live in folders with ``__init__.py`` + ``template.yaml``).
# 2. The result is cached via ``functools.lru_cache`` so repeated
#    lookups are free and the set is genuinely immutable per process.
#
# A sanity test (see ``tests/packages/test_handler_registry.py``)
# asserts the derived set contains a small list of known platform
# names so a misconfigured ``config_seed/templates/`` would fail
# tests rather than silently widen what packages can claim.

# Derived from the filesystem.  Tests can ``_compute_platform_templates.cache_clear()``
# if they need to re-scan a fixture directory.
_DEFAULT_TEMPLATES_DIR = (
    Path(__file__).resolve().parents[2] / "config_seed" / "templates"
)


@functools.lru_cache(maxsize=1)
def _compute_platform_templates() -> frozenset[str]:
    """Return platform-shipped template names derived from the filesystem.

    Walks ``config_seed/templates/`` and returns a frozenset of every
    subdir name AND every ``*.yaml`` file's stem.  Both layouts coexist
    today (some templates are single YAML files, others are folders);
    listing both keeps the gate aligned with whichever shape the
    platform uses for any given template.
    """
    templates_dir = _DEFAULT_TEMPLATES_DIR
    if not templates_dir.is_dir():
        # In stripped builds / installed wheels, ``config_seed`` may
        # not be next to the package.  Fail-soft: return an empty set
        # so package registration still works (the caller's collision
        # check just becomes a no-op for platform names).  The CI
        # sanity test will catch a real misconfiguration.
        logger.warning(
            "_compute_platform_templates: %s not found; returning empty "
            "platform-template set",
            templates_dir,
        )
        return frozenset()

    names: set[str] = set()
    for entry in templates_dir.iterdir():
        if entry.name.startswith(("_", ".")):
            continue
        if entry.is_dir():
            names.add(entry.name)
        elif entry.is_file() and entry.suffix == ".yaml":
            # Strip the .yaml suffix so e.g. ``coding-change.yaml`` and
            # the folder ``coding-change/`` both surface the same
            # platform-reserved name.
            names.add(entry.stem)
    return frozenset(names)


# Module-level constant.  Computed once via the cached function above;
# accessing ``_PLATFORM_TEMPLATES`` directly always sees the same
# frozenset (tests can replace it via ``monkeypatch.setattr`` if they
# need to inject a fixture).  Implemented as a module-level
# ``__getattr__`` so the lazy import order (registry → handler_registry)
# doesn't pin a stale value when test fixtures reset the cache.
def __getattr__(name: str):
    if name == "_PLATFORM_TEMPLATES":
        return _compute_platform_templates()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Type aliases: a JUDGE handler takes a dataclass instance and returns
# a JudgeResult.  The deserialisation + policy validation happens in
# the dispatch wrapper before the handler is called (security/judge.py
# §3.6 step 3-4); the handler does only structural/cross-field checks.
JudgeHandler = Callable[..., "JudgeResultLike"]


@dataclass(frozen=True)
class JudgeResultLike:
    """Duck-typed result handlers can return.

    Real handlers should return :class:`carpenter.security.judge.JudgeResult`;
    we keep the type bound loose here to avoid a hard import cycle
    (``security.judge`` imports from us via dispatch).
    """

    approved: bool
    reason: str = ""


@dataclass
class _PackageEntry:
    """What a single package contributed to the registries."""

    name: str
    judges: dict[str, JudgeHandler] = field(default_factory=dict)
    kinds: dict[str, type] = field(default_factory=dict)
    step_handlers: list[tuple[str, str]] = field(default_factory=list)


class PackageHandlerRegistry:
    """Process-wide map of package-shipped handlers and kinds."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # template_name -> (package_name, handler)
        self._judges: dict[str, tuple[str, JudgeHandler]] = {}
        # kind_name -> (package_name, dataclass)
        self._kinds: dict[str, tuple[str, type]] = {}
        # package_name -> entry (for unregistration)
        self._by_package: dict[str, _PackageEntry] = {}

    # ── JUDGE handlers ──────────────────────────────────────────────

    def register_judge(
        self, package_name: str, template_name: str, handler: JudgeHandler,
    ) -> None:
        """Register a JUDGE handler for a package's template.

        Raises:
            ValueError: collision with platform template, with another
                package's same-named template, or invalid input.
        """
        if not callable(handler):
            raise ValueError(
                f"register_judge({template_name!r}): handler must be callable",
            )
        if not template_name:
            raise ValueError("register_judge: template_name must be non-empty")
        if template_name in _compute_platform_templates():
            raise ValueError(
                f"register_judge({template_name!r}): template is "
                f"platform-reserved; packages cannot override platform JUDGEs",
            )
        with self._lock:
            existing = self._judges.get(template_name)
            if existing is not None and existing[0] != package_name:
                raise ValueError(
                    f"register_judge({template_name!r}): already "
                    f"registered by package {existing[0]!r}; cannot "
                    f"shadow",
                )
            self._judges[template_name] = (package_name, handler)
            entry = self._by_package.setdefault(
                package_name, _PackageEntry(name=package_name),
            )
            entry.judges[template_name] = handler

    def lookup_judge(self, template_name: str) -> JudgeHandler | None:
        """Return the registered package JUDGE for a template, or None."""
        with self._lock:
            entry = self._judges.get(template_name)
            return entry[1] if entry else None

    def list_judges(self) -> list[tuple[str, str]]:
        """Return ``[(template_name, package_name), ...]``."""
        with self._lock:
            return [(t, pkg) for t, (pkg, _) in sorted(self._judges.items())]

    # ── Data-model kinds ────────────────────────────────────────────

    def register_kind(
        self, package_name: str, kind_name: str, cls: type,
    ) -> None:
        """Register a dataclass kind shipped by a package.

        Raises:
            ValueError: collision with the platform's kind registry,
                with another package's same-named kind, or invalid
                input.
        """
        # Platform kinds win — defense in depth against package
        # smuggling a redefined ``PolicyCheckList`` past the loader.
        from ..security.judge import _PLATFORM_KINDS  # noqa: WPS433
        if kind_name in _PLATFORM_KINDS:
            raise ValueError(
                f"register_kind({kind_name!r}): kind is platform-reserved",
            )
        if not isinstance(cls, type):
            raise ValueError(
                f"register_kind({kind_name!r}): expected a class, got "
                f"{type(cls).__name__}",
            )
        with self._lock:
            existing = self._kinds.get(kind_name)
            if existing is not None and existing[0] != package_name:
                raise ValueError(
                    f"register_kind({kind_name!r}): already "
                    f"registered by package {existing[0]!r}; cannot "
                    f"shadow",
                )
            self._kinds[kind_name] = (package_name, cls)
            entry = self._by_package.setdefault(
                package_name, _PackageEntry(name=package_name),
            )
            entry.kinds[kind_name] = cls

    def lookup_kind(self, kind_name: str) -> type | None:
        """Return the dataclass for a kind, or None if not registered."""
        with self._lock:
            entry = self._kinds.get(kind_name)
            return entry[1] if entry else None

    def list_kinds(self) -> list[tuple[str, str]]:
        """Return ``[(kind_name, package_name), ...]``."""
        with self._lock:
            return [(k, pkg) for k, (pkg, _) in sorted(self._kinds.items())]

    # ── Step handlers (tracked for unregistration only) ─────────────

    def track_step_handler(
        self, package_name: str, template_name: str, role: str,
    ) -> None:
        """Record that a package registered a step handler.

        The actual registration happens against the engine's
        ``handler_registry`` (the dispatch path looks it up there); this
        registry only tracks the (template, role) pairs so that
        :func:`unregister_package` can roll them back on uninstall.
        """
        with self._lock:
            entry = self._by_package.setdefault(
                package_name, _PackageEntry(name=package_name),
            )
            pair = (template_name, role)
            # Dedupe: a package may call ``track_step_handler`` more than
            # once for the same (template, role) when re-loaded; we want
            # ``unregister_package`` to call ``unregister_step_handler``
            # exactly once (PR #306 followup NIT #6).
            if pair not in entry.step_handlers:
                entry.step_handlers.append(pair)

    # ── Unregistration / reset ──────────────────────────────────────

    def unregister_package(self, package_name: str) -> None:
        """Drop everything a package registered.

        Called from :func:`carpenter.packages.installer.uninstall_package`
        when a clean uninstall succeeds, and from tests.  Idempotent.
        """
        with self._lock:
            entry = self._by_package.pop(package_name, None)
            if entry is None:
                return
            for tname in list(entry.judges):
                cur = self._judges.get(tname)
                if cur is not None and cur[0] == package_name:
                    self._judges.pop(tname, None)
            for kname in list(entry.kinds):
                cur_k = self._kinds.get(kname)
                if cur_k is not None and cur_k[0] == package_name:
                    self._kinds.pop(kname, None)

        # Step handlers live in the engine's registry; drop them too.
        # ``ImportError`` is the only legitimate soft-fallback (stripped
        # build without the engine).  Any other exception from
        # ``unregister_step_handler`` is a real bug — let it propagate
        # so the uninstall caller can surface it (PR #306 followup).
        try:
            from ..core.engine import handler_registry as _h
        except ImportError:
            logger.warning(
                "unregister_package(%r): engine.handler_registry "
                "unavailable; skipping step-handler cleanup",
                package_name,
            )
            return
        for tname, role in entry.step_handlers:
            _h.unregister_step_handler(tname, role)

    def reset(self) -> None:
        """Drop EVERY registration.  Tests only."""
        with self._lock:
            self._judges.clear()
            self._kinds.clear()
            self._by_package.clear()


# ── Module-level singleton ─────────────────────────────────────────

_REGISTRY: PackageHandlerRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_handler_registry() -> PackageHandlerRegistry:
    """Return the process-wide :class:`PackageHandlerRegistry`."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = PackageHandlerRegistry()
        return _REGISTRY

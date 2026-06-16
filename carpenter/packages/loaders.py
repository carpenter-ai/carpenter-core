"""Loaders: wire installed-package artifacts into platform runtime registries.

D24 stage 3b connects the manifest declarations (validated in stage 3a)
to the platform's actual runtime dispatch paths:

* **Arc templates** → ``core.engine.template_manager.load_template``.
* **Step handlers** → ``core.engine.handler_registry.register_step_handler``.
* **JUDGE handlers** → :func:`carpenter.packages.handler_registry.get_handler_registry`.
* **Data models** → ``carpenter.packages.handler_registry`` kinds map +
  the dataclasses are also dropped into ``sys.modules`` under
  ``_carpenter_pkg_.<package>.data_models`` so package judges can
  ``from ..data_models import ...`` cleanly.

Each loader returns a ``(registered_count, errors)`` tuple.  Errors do
NOT abort the package load: stage-3a's pattern is preserved (one bad
artifact does not strand the rest of the package).  Errors are recorded
on the package's ``RegisteredPackage.load_errors``.

The module is deliberately narrow.  It does NOT touch policy
contributions, KB articles, or trigger subscriptions — those are
B-full deferrals (per the D24 plan §9.2).  The manifest fields are
parsed and recorded by stage 3a; B-full will add loaders here.

Trust-model invariants enforced by these loaders (also covered by
the package handler registry's class-level checks):

* JUDGE handler signatures must accept exactly one positional
  parameter — the deserialised dataclass.  No raw-bytes or
  raw-arc-state path is ever exposed to a package handler.
* Step handlers must be coroutines (the engine awaits them).
* Template names that collide with platform-shipped templates or
  with already-loaded package templates are load errors.
* Data-model classes that collide with platform kinds or with
  already-loaded package kinds are load errors.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import sys
import types
from dataclasses import is_dataclass
from pathlib import Path

from .handler_registry import get_handler_registry
from .manifest import PackageManifest

logger = logging.getLogger(__name__)


# Package-namespace prefix for dynamically-imported modules.  Mirrors
# Phase A's chat-tool import pattern (``_carpenter_pkg_.<pkg>.<mod>``)
# so dataclasses imported via this prefix can be cleanly distinguished
# from platform code in tracebacks.
_PKG_NAMESPACE = "_carpenter_pkg_"


def _ensure_namespace_package(package_name: str, pkg_root: Path) -> None:
    """Make ``_carpenter_pkg_.<package_name>`` a real package in sys.modules.

    Required so that ``from ..data_models import EmailReviewExtract``
    inside a package-shipped JUDGE handler resolves cleanly.  The
    parent ``_carpenter_pkg_`` namespace is created lazily on first use.
    """
    parent_name = _PKG_NAMESPACE
    if parent_name not in sys.modules:
        parent = types.ModuleType(parent_name)
        parent.__path__ = []
        sys.modules[parent_name] = parent

    full = f"{parent_name}.{package_name}"
    if full not in sys.modules:
        mod = types.ModuleType(full)
        mod.__path__ = [str(pkg_root)]
        sys.modules[full] = mod


def _import_package_module(
    package_name: str, dotted: str, pkg_root: Path,
) -> types.ModuleType:
    """Import ``<pkg_root>/<dotted_with_slashes>.py`` under the package namespace.

    ``dotted`` is the manifest-declared module path (``judges.email_review``);
    we map it to ``<pkg_root>/judges/email_review.py`` and load it under
    ``_carpenter_pkg_.<package_name>.<dotted>``.

    The function fails closed: a malformed dotted path that escapes the
    package root raises ImportError, never returns silently.
    """
    _ensure_namespace_package(package_name, pkg_root)

    parts = dotted.split(".")
    if any(p in ("", "..", ".") for p in parts):
        raise ImportError(
            f"Invalid module path {dotted!r} for package {package_name!r}",
        )

    rel = Path(*parts).with_suffix(".py")
    candidate = (pkg_root / rel).resolve()
    try:
        candidate.relative_to(pkg_root.resolve())
    except ValueError as exc:
        raise ImportError(
            f"Module path {dotted!r} escapes package root {pkg_root}",
        ) from exc
    if not candidate.is_file():
        raise ImportError(
            f"Module {dotted!r} not found at {candidate}",
        )

    full_name = f"{_PKG_NAMESPACE}.{package_name}.{dotted}"
    if full_name in sys.modules:
        return sys.modules[full_name]

    # Make sure intermediate parent packages exist as namespace
    # modules so relative imports inside the loaded module work.
    for i in range(1, len(parts)):
        parent_dotted = ".".join(parts[:i])
        parent_full = f"{_PKG_NAMESPACE}.{package_name}.{parent_dotted}"
        if parent_full in sys.modules:
            continue
        parent_dir = (pkg_root / Path(*parts[:i])).resolve()
        if parent_dir.is_dir():
            parent_mod = types.ModuleType(parent_full)
            parent_mod.__path__ = [str(parent_dir)]
            sys.modules[parent_full] = parent_mod

    # Only mark this module as a sub-package (settable
    # ``submodule_search_locations``) if the candidate is itself an
    # ``__init__.py``.  For ordinary modules like ``judges.py`` we must
    # leave the search locations as ``None`` so that intra-package
    # relative imports (``from .data_models import X``) resolve via the
    # already-cached parent package rather than being re-imported as a
    # nested attribute of the current module.  The parent namespace
    # package set up above carries the correct ``__path__`` for that
    # resolution.  The previous unconditional setting caused
    # ``data_models`` to be loaded twice (once as
    # ``_carpenter_pkg_.<pkg>.data_models`` and once as
    # ``_carpenter_pkg_.<pkg>.judges.data_models``), giving JUDGE
    # handlers a class identity that didn't match registry entries and
    # forcing ``type().__name__`` workarounds at the package layer.
    is_pkg_init = candidate.name == "__init__.py"
    spec = importlib.util.spec_from_file_location(
        full_name, str(candidate),
        submodule_search_locations=(
            [str(candidate.parent)]
            if is_pkg_init and candidate.parent.is_dir()
            else None
        ),
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not build spec for {candidate}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(full_name, None)
        raise
    return module


# ── Data models ─────────────────────────────────────────────────────


def load_data_models(
    manifest: PackageManifest,
) -> tuple[int, list[str]]:
    """Import the package's ``data_models`` module and register kinds.

    Each name in ``manifest.data_models`` must resolve to a class on
    the imported module; non-classes / missing names are reported as
    errors and the rest still register.  The imported dataclasses are
    registered with :func:`PackageHandlerRegistry.register_kind` so the
    JUDGE-dispatch deserialiser can find them by ``kind`` string (SD12).

    Returns ``(registered_count, errors)``.
    """
    if not manifest.data_models:
        return 0, []
    pkg_root = manifest.source_path
    try:
        module = _import_package_module(
            manifest.name, "data_models", pkg_root,
        )
    except ImportError as exc:
        return 0, [
            f"Failed to import data_models module: {exc}",
        ]

    registry = get_handler_registry()
    registered = 0
    errors: list[str] = []
    for name in manifest.data_models:
        cls = getattr(module, name, None)
        if cls is None:
            errors.append(
                f"data_models: {name!r} not found in data_models.py",
            )
            continue
        if not isinstance(cls, type):
            errors.append(
                f"data_models: {name!r} is not a class "
                f"(got {type(cls).__name__})",
            )
            continue
        if not is_dataclass(cls):
            errors.append(
                f"data_models: {name!r} must be a @dataclass "
                f"(stdlib dataclasses)",
            )
            continue
        try:
            registry.register_kind(manifest.name, name, cls)
        except ValueError as exc:
            errors.append(f"data_models: {exc}")
            continue
        registered += 1
    return registered, errors


# ── Arc templates ───────────────────────────────────────────────────


def load_arc_templates(
    manifest: PackageManifest,
    *,
    db_conn=None,
) -> tuple[int, list[str], list[str]]:
    """Load each declared arc template into the platform's template DB.

    Args:
        manifest: The package manifest declaring the templates.
        db_conn: Optional active DB connection.  The daemon's startup
            discovery wraps ``discover_and_register`` in a
            ``db_transaction()``; when set we thread the connection into
            :func:`template_manager.load_template` so it reuses the
            transaction instead of opening a nested one (which trips the
            same-thread deadlock guard and stranded every package
            template at startup).

    Returns ``(registered_count, errors, registered_names)``.  Each
    template is loaded by :func:`carpenter.core.engine.template_manager.load_template`,
    which inserts/updates a ``workflow_templates`` row keyed by the
    template's own ``name:`` field.  Per SD7, we DO NOT prefix the
    template name; collisions with platform / other-package templates
    surface as engine-level "duplicate template" updates which the
    operator must resolve by uninstalling one of them.

    The function does NOT enforce the "if EXECUTOR untrusted then
    REVIEWER+JUDGE" structural check from §5.5 step 2 of the design
    doc — that rule is enforced by the runtime arc dispatcher
    (``tool_backends/arc.py``), and adding a redundant gate at load
    time would block tests that ship synthetic templates.  B-full
    can promote it.
    """
    if not manifest.arc_templates:
        return 0, [], []
    from ..core.engine import template_manager
    from .handler_registry import _PLATFORM_TEMPLATES

    pkg_root = manifest.source_path
    registered = 0
    errors: list[str] = []
    registered_names: list[str] = []

    for tref in manifest.arc_templates:
        if tref.name in _PLATFORM_TEMPLATES:
            errors.append(
                f"arc_templates: template name {tref.name!r} is "
                f"platform-reserved; rename it",
            )
            continue
        yaml_path = (pkg_root / tref.path).resolve()
        try:
            yaml_path.relative_to(pkg_root.resolve())
        except ValueError:
            errors.append(
                f"arc_templates: {tref.name!r}: path {tref.path!r} "
                f"escapes package root",
            )
            continue
        if not yaml_path.is_file():
            errors.append(
                f"arc_templates: {tref.name!r}: YAML not found at "
                f"{yaml_path}",
            )
            continue
        try:
            # Record the owning package on the template so instantiation
            # can stamp the package's per-arc grant (``pkg.<name>``) onto
            # every step arc — this is what lets the package's EXECUTOR
            # arc invoke the package's registered trusted capability verbs
            # through the per-package dispatch gate. Scoped to this
            # package's own templates; platform/other-package arcs never
            # receive the grant.
            template_manager.load_template(
                str(yaml_path),
                owner_package=manifest.name,
                db_conn=db_conn,
            )
        except Exception as exc:  # noqa: BLE001 — surface to load_errors
            errors.append(
                f"arc_templates: {tref.name!r}: load_template raised: {exc}",
            )
            continue
        registered += 1
        registered_names.append(tref.name)

    return registered, errors, registered_names


# ── JUDGE handlers ──────────────────────────────────────────────────


def load_judge_handlers(
    manifest: PackageManifest,
) -> tuple[int, list[str]]:
    """Import the package's JUDGE handler modules and register them.

    A package's JUDGE handler is referenced by:

    * ``arc_templates[*].judge_handler`` — the ``module:func`` ref.
    * ``judge_handlers[*]`` — the bare module declaration (kept so that
      the static AST lint can see the file).

    The wiring goes through ``arc_templates``: a template's
    ``judge_handler`` field tells us which function to register against
    that template.  ``judge_handlers`` declarations without an
    associated template are imported (so AST lint had something to
    walk) but not registered.

    Each registered handler must accept exactly one positional argument:
    the deserialised extract dataclass.  Handlers that take more
    parameters are rejected at registration time so that the dispatch
    wrapper's contract ("hand the handler the typed dataclass, nothing
    else") is enforced before any package code runs.

    Returns ``(registered_count, errors)``.
    """
    if not manifest.arc_templates and not manifest.judge_handlers:
        return 0, []
    pkg_root = manifest.source_path
    registry = get_handler_registry()
    registered = 0
    errors: list[str] = []

    # Pre-import every judge_handlers entry so static-lint targets exist
    # even if no template references them.  Errors here are surfaced
    # but do not block template-driven registration below.
    for href in manifest.judge_handlers:
        try:
            _import_package_module(manifest.name, href.module, pkg_root)
        except ImportError as exc:
            errors.append(
                f"judge_handlers: failed to import {href.module!r}: {exc}",
            )

    # Register the actual JUDGE-for-template wires.
    for tref in manifest.arc_templates:
        if not tref.judge_handler:
            continue
        module_part, _, func_part = tref.judge_handler.partition(":")
        try:
            module = _import_package_module(
                manifest.name, module_part, pkg_root,
            )
        except ImportError as exc:
            errors.append(
                f"judge_handlers: template {tref.name!r}: "
                f"could not import {module_part!r}: {exc}",
            )
            continue

        handler = getattr(module, func_part, None)
        if handler is None or not callable(handler):
            errors.append(
                f"judge_handlers: template {tref.name!r}: "
                f"{tref.judge_handler!r} is not callable",
            )
            continue

        # I3 invariant: handler signature must take exactly one
        # positional dataclass argument.  Reject handlers that try to
        # accept arc state, raw bytes, DB handles, etc.  inspect.signature
        # is best-effort for C-implemented callables, but every package
        # JUDGE we expect is pure Python.
        try:
            sig = inspect.signature(handler)
        except (TypeError, ValueError):
            errors.append(
                f"judge_handlers: template {tref.name!r}: "
                f"could not introspect handler signature",
            )
            continue
        positional = [
            p for p in sig.parameters.values()
            if p.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.POSITIONAL_ONLY,
            )
        ]
        if len(positional) != 1:
            errors.append(
                f"judge_handlers: template {tref.name!r}: handler "
                f"must take exactly one positional argument "
                f"(the deserialised extract); got {len(positional)}",
            )
            continue

        try:
            registry.register_judge(manifest.name, tref.name, handler)
        except ValueError as exc:
            errors.append(f"judge_handlers: {exc}")
            continue
        registered += 1
    return registered, errors


# ── Platform capabilities (TRUSTED dispatch verbs) ─────────────────


def load_platform_capabilities(
    manifest: PackageManifest,
    *,
    granted_verbs: frozenset[str],
) -> tuple[int, list[str]]:
    """Register the package's GRANTED trusted capability verbs.

    Mirrors :func:`load_judge_handlers`: at startup, AFTER SD6 verify,
    for each ``platform_capabilities`` entry whose ``verb`` is in
    ``granted_verbs`` (i.e. the operator confirmed it at install time and
    the install record recorded the grant), import the handler module via
    :func:`_import_package_module` (hash-pinned tree, synthetic
    ``_carpenter_pkg_`` namespace) and register the verb on the process-
    wide capability registry as a wrapped ``handler(params, ctx)``.

    Capabilities NOT in ``granted_verbs`` are deliberately skipped — a
    declared-but-not-confirmed capability is never registered, so its verb
    is not dispatchable.  This is the trust boundary: registration is
    gated on the recorded grant, not merely on declaration.

    The egress host is resolved PLATFORM-SIDE here (from the package's
    declared credential, ``grant.host_from``) and baked into the
    registration so the handler's :class:`CapabilityContext` carries the
    confirmed host; the untrusted executor can never influence it.

    Returns ``(registered_count, errors)``.  Errors do not abort the rest
    of the package load (stage-3a's pattern).
    """
    if not manifest.platform_capabilities:
        return 0, []
    from .capabilities import (
        CapabilityError,
        get_capability_registry,
        resolve_package_secret,
    )

    pkg_root = manifest.source_path
    registry = get_capability_registry()
    registered = 0
    errors: list[str] = []

    for cap in manifest.platform_capabilities:
        if cap.verb not in granted_verbs:
            # Declared but not granted/confirmed — never register.
            logger.info(
                "platform_capabilities: verb %r (package %r) declared but "
                "not granted; skipping registration",
                cap.verb, manifest.name,
            )
            continue
        # Resolve the confirmed egress host platform-side from the
        # package's credential.  The full env var is
        # f"{credential_ref}_{host_from}".  Resolve via the package-aware
        # resolver (keyed on ``manifest.name``) so a host that lives ONLY
        # in the package's per-package ``.env`` is found — matching how
        # ``CapabilityContext.secret`` resolves the rest of the package's
        # credentials at dispatch time.
        host_key = f"{cap.grant.credential_ref}_{cap.grant.host_from}"
        host = resolve_package_secret(manifest.name, host_key)
        if not host:
            errors.append(
                f"platform_capabilities: verb {cap.verb!r}: egress host "
                f"credential {host_key!r} is not set platform-side; "
                f"capability not registered (provide the credential and "
                f"restart)",
            )
            continue
        try:
            module = _import_package_module(
                manifest.name, cap.module, pkg_root,
            )
        except ImportError as exc:
            errors.append(
                f"platform_capabilities: verb {cap.verb!r}: could not "
                f"import {cap.module!r}: {exc}",
            )
            continue
        handler = getattr(module, cap.handler, None)
        if handler is None or not callable(handler):
            errors.append(
                f"platform_capabilities: verb {cap.verb!r}: handler "
                f"{cap.module}:{cap.handler} is not callable",
            )
            continue
        try:
            registry.register(
                package_name=manifest.name,
                verb=cap.verb,
                kind=cap.kind,
                handler=handler,
                grant=cap.grant,
                host=host,
            )
        except CapabilityError as exc:
            errors.append(f"platform_capabilities: {exc}")
            continue
        # Platform-integrity tier: classify the handler module path as T1
        # (trusted/platform-protected) so edits to it get careful-review
        # treatment, even though it lives under the (otherwise T2) package
        # install dir.  Best-effort — a failure here must not strand the
        # verb registration.
        try:
            from ..security.platform_paths import (
                register_trusted_capability_path,
            )
            rel = Path(*cap.module.split("."))
            module_path = (pkg_root / rel).with_suffix(".py")
            register_trusted_capability_path(str(module_path))
        except Exception:  # noqa: BLE001 — defensive
            logger.warning(
                "Could not classify capability handler %r as T1; edits to "
                "it may not get platform-review treatment",
                cap.module, exc_info=True,
            )
        registered += 1
    return registered, errors


# ── Step handlers ───────────────────────────────────────────────────


def load_step_handlers(
    manifest: PackageManifest,
) -> tuple[int, list[str]]:
    """Register Python step handlers for the package's templates.

    Stage 3b does NOT pull step-handler declarations out of the
    manifest as a separate field (that's a B-full extension per the
    design doc §4 "step_handlers" entry).  Instead, packages whose
    templates ship a ``register_handlers(registry)`` entrypoint inside
    a sibling ``__init__.py`` get that hook called once the templates
    are loaded — same pattern as platform-shipped template packages
    (``config_seed/templates/<name>/__init__.py``).

    A package can therefore opt into Python step handlers by laying out
    its template directory like::

        templates/email-triage/template.yaml
        templates/email-triage/__init__.py   # register_handlers(reg)

    This loader does the importlib dance and tracks the registered
    (template, role) pairs on the handler registry so uninstall can
    revert them.  It does NOT consume any new manifest field — that's
    a deliberate choice to keep B-min minimal.

    Returns ``(registered_count, errors)``.
    """
    if not manifest.arc_templates:
        return 0, []
    from ..core.engine import handler_registry as engine_reg

    pkg_root = manifest.source_path
    registry = get_handler_registry()
    registered = 0
    errors: list[str] = []

    for tref in manifest.arc_templates:
        yaml_path = (pkg_root / tref.path).resolve()
        init_path = yaml_path.parent / "__init__.py"
        if not init_path.is_file():
            continue
        # Compute dotted path from package root to the template dir.
        try:
            rel_parts = yaml_path.parent.relative_to(pkg_root.resolve()).parts
        except ValueError:
            errors.append(
                f"step_handlers: {tref.name!r}: template dir escapes "
                f"package root",
            )
            continue
        dotted = ".".join(rel_parts)
        if not dotted:
            errors.append(
                f"step_handlers: {tref.name!r}: template directory must "
                f"be nested under the package root",
            )
            continue

        try:
            module = _import_package_module(manifest.name, dotted, pkg_root)
        except ImportError as exc:
            errors.append(
                f"step_handlers: {tref.name!r}: import failed: {exc}",
            )
            continue

        register_fn = getattr(module, "register_handlers", None)
        if register_fn is None:
            continue

        # The package's register_handlers receives the engine's
        # handler_registry module.  We track registrations after the
        # fact by snapshotting the registry's keys.
        before = set(engine_reg.registered_handlers())
        try:
            register_fn(engine_reg)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                f"step_handlers: {tref.name!r}: register_handlers raised: "
                f"{exc}",
            )
            continue
        after = set(engine_reg.registered_handlers())
        for tname, role in (after - before):
            registry.track_step_handler(manifest.name, tname, role)
            registered += 1

    return registered, errors


# ── Combined entrypoint used by registry.py ────────────────────────


def load_package_artifacts(
    manifest: PackageManifest,
    *,
    db_conn=None,
) -> tuple[dict[str, int], list[str], list[str]]:
    """Run every B-min loader and aggregate results.

    Args:
        manifest: The package manifest whose artifacts to load.
        db_conn: Optional active DB connection, threaded into
            :func:`load_arc_templates` so template loading reuses the
            caller's transaction (see that function's docstring).

    Returns ``(counts, errors, template_names)``:

    * ``counts`` — ``{"data_models": N, "arc_templates": N,
      "judge_handlers": N, "step_handlers": N}``.
    * ``errors`` — flat list of human-readable error strings (each
      stays on the registered package's ``load_errors``).
    * ``template_names`` — the unprefixed names of templates this
      package successfully registered.  Used for collision detection
      across packages.
    """
    counts = {
        "data_models": 0,
        "arc_templates": 0,
        "judge_handlers": 0,
        "step_handlers": 0,
    }
    errors: list[str] = []

    # Order matters: data_models must be importable before judges
    # (which import them via relative imports) and arc_templates
    # before step_handlers (which sit alongside the YAML).
    n, errs = load_data_models(manifest)
    counts["data_models"] = n
    errors.extend(errs)

    n, errs, tnames = load_arc_templates(manifest, db_conn=db_conn)
    counts["arc_templates"] = n
    errors.extend(errs)

    n, errs = load_judge_handlers(manifest)
    counts["judge_handlers"] = n
    errors.extend(errs)

    n, errs = load_step_handlers(manifest)
    counts["step_handlers"] = n
    errors.extend(errs)

    return counts, errors, tnames

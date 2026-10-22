"""Capability-package registry: discovery, validation, registration.

The registry is the single platform-side coordinator for capability
packages in Phase A.  It:

1. Walks each configured search path looking for ``packages/<name>/manifest.yaml``.
2. Loads + shape-validates each manifest (:mod:`carpenter.packages.manifest`).
3. Runs trust-model security guards (:mod:`carpenter.packages.security`).
4. Imports each declared chat-tool module and registers its
   ``@chat_tool``-decorated functions via the existing
   :func:`carpenter.chat_tool_loader.register_extension_tool`
   mechanism — which itself enforces I10 a second time (defense in
   depth: package validation rejects platform-boundary tools at load
   time, and ``register_extension_tool`` rejects them again at
   registration time).
5. Records the loaded packages so that the read-only
   ``list_packages`` chat tool can introspect them.

Per leadership decision **D22** (capability packages must install
trivially), :func:`default_search_paths` returns paths that are
populated automatically when the user clones ``carpenter-packages``
alongside ``carpenter-core``.  No user config edit is required for the
``hello`` reference package to be discovered; configuring additional
search paths is opt-in via the ``capability_packages.search_paths``
config key.

A package whose manifest fails to load, or whose security guards fail,
is logged at ERROR level and SKIPPED.  One bad package never prevents
others from loading — this matches the "loaded packages" table model
from ``~/notes/capability-packages.md``.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import PackageManifest, ManifestError, load_manifest
from .security import PackageSecurityError, validate_manifest_security

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegisteredPackage:
    """A successfully-loaded capability package.

    Attributes:
        manifest: Parsed manifest.
        chat_tool_names: Tool names registered from this package.
        template_names: Arc-template names this package contributed
            (D24 stage 3b).
        artifact_counts: ``{"data_models": N, "arc_templates": N,
            "judge_handlers": N, "step_handlers": N}`` summary of the
            B-min loader registrations (D24 stage 3b).  Empty dict when
            the package shipped no D24 artifacts.
        load_errors: Non-fatal errors encountered (warnings).
    """

    manifest: PackageManifest
    chat_tool_names: tuple[str, ...] = ()
    template_names: tuple[str, ...] = ()
    artifact_counts: dict = field(default_factory=dict)
    load_errors: tuple[str, ...] = ()
    # Write-capable chat tools that were SKIPPED because the operator did
    # not opt this package in (``write_chat_tools_allowed=False``).  These
    # are surfaced for observability — NOT errors.  Empty when the package
    # ships no write chat tools or the operator opted in (then they appear
    # in ``chat_tool_names`` instead).
    gated_chat_tool_names: tuple[str, ...] = ()


def default_install_paths() -> list[Path]:
    """Return the install destination(s) — primary D24 stage 3a path.

    ``~/carpenter/packages/`` is the install destination per SD2.
    Returned paths directly contain ``<name>/manifest.yaml`` children
    (no ``packages/`` subdir).
    """
    out: list[Path] = []
    try:
        from .. import config as _config_mod
        base_dir = (
            _config_mod.CONFIG.get("base_dir", "")
            if hasattr(_config_mod, "CONFIG") else ""
        )
    except Exception:  # pragma: no cover — defensive
        base_dir = ""
    if base_dir:
        # ``base_dir`` is the repo dir; the data dir is conventionally
        # its sibling.  Try both data-sibling and same-dir layouts.
        base_dir_path = Path(base_dir)
        out.append(base_dir_path.parent / "data" / "packages")
        out.append(base_dir_path / "packages")
    out.append(Path(os.path.expanduser("~/carpenter/packages")))
    return out


def back_compat_source_paths() -> list[Path]:
    """Removed in D24 stage 3b: back-compat source-repo shim is gone.

    Stage 3a kept a transitional shim that scanned the
    ``~/repos/carpenter-packages`` source tree so unmigrated packages
    (notably ``hello``) kept loading without operator action.  Stage 3b
    migrates ``hello`` to the install model and removes the shim — the
    registry now ONLY loads packages from the install destination
    (``~/carpenter/packages/``).

    The function is retained as an empty-list stub for backwards
    compatibility with any callers that imported it during the 3a
    window.  It will be deleted in B-full.
    """
    return []


def _env_var_paths() -> list[Path]:
    """``CARPENTER_PACKAGES_PATH`` env-var override paths.

    Treated as install paths (highest priority) so explicit overrides
    aren't downgraded to back-compat shim semantics.
    """
    paths: list[Path] = []
    env = os.environ.get("CARPENTER_PACKAGES_PATH", "")
    for raw in env.split(os.pathsep):
        raw = raw.strip()
        if raw:
            paths.append(Path(os.path.expanduser(raw)))
    return paths


def default_search_paths() -> list[Path]:
    """Return the default search paths used by ``discover_and_register``.

    Order (D24 stage 3b — back-compat shim removed):

    1. ``CARPENTER_PACKAGES_PATH`` env-var entries (explicit override).
    2. **Install destination(s)** — ``~/carpenter/packages/`` (the
       D24 SD2 install target) and base_dir-derived equivalents.

    Nonexistent paths are silently skipped.
    """
    out = _env_var_paths()
    out.extend(default_install_paths())
    seen: set[Path] = set()
    deduped: list[Path] = []
    for p in out:
        rp = p.resolve() if p.exists() else p
        if rp in seen:
            continue
        seen.add(rp)
        deduped.append(p)
    return deduped


def configured_search_paths() -> list[Path] | None:
    """Return the operator-configured search paths, or ``None``.

    Reads ``config["capability_packages"]["search_paths"]``.  ``None``
    means "use :func:`default_search_paths`" (per D22 — zero-action
    discovery).  A malformed value is logged and treated as unset.

    Shared by the startup path (:class:`carpenter.coordinator.Coordinator`)
    and the live reload path
    (:func:`carpenter.packages.reload.reload_packages`) so the two can
    never look in different places.
    """
    try:
        from .. import config as _config_mod
        cfg = _config_mod.CONFIG.get("capability_packages", {}) or {}
    except Exception:  # pragma: no cover — defensive
        return None
    configured = cfg.get("search_paths")
    if not configured:
        return None
    if not isinstance(configured, list):
        logger.error(
            "config['capability_packages']['search_paths'] must be "
            "a list; falling back to defaults",
        )
        return None
    return [Path(os.path.expanduser(str(p))) for p in configured]


def _read_raw_yaml(path: Path) -> dict:
    """Read the raw YAML dict for a manifest, for security validation."""
    import yaml
    with open(path, encoding="utf-8") as fp:
        data = yaml.safe_load(fp)
    if not isinstance(data, dict):
        raise ManifestError(
            f"Manifest at {path} must be a YAML mapping",
        )
    return data


class PackageRegistry:
    """Process-wide registry of loaded capability packages."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._packages: dict[str, RegisteredPackage] = {}
        self._search_paths: list[Path] = []

    def list_packages(self) -> list[RegisteredPackage]:
        with self._lock:
            return list(self._packages.values())

    def get(self, name: str) -> RegisteredPackage | None:
        with self._lock:
            return self._packages.get(name)

    def reset(self) -> None:
        """Clear registry state.  Test-only — production restarts the process."""
        with self._lock:
            self._packages.clear()
            self._search_paths.clear()
        # D24 stage 3b: also drop runtime registrations so artifact
        # state doesn't leak between tests.
        try:
            from .handler_registry import get_handler_registry
            get_handler_registry().reset()
        except Exception:  # pragma: no cover — defensive
            logger.exception("PackageRegistry.reset: handler reset failed")
        # Package-capability framework: drop registered trusted verbs too.
        try:
            from .capabilities import get_capability_registry
            get_capability_registry().reset()
        except Exception:  # pragma: no cover — defensive
            logger.exception("PackageRegistry.reset: capability reset failed")

    def _discover_manifests(
        self,
        search_paths: list[Path],
        *,
        install_paths: set[Path] | None = None,
    ) -> list[tuple[Path, bool]]:
        """Find every ``manifest.yaml`` under each search path.

        Returns a list of ``(manifest_path, is_installed_path)``
        tuples.  ``is_installed_path`` is True when the search root
        was contributed by :func:`default_install_paths` (or the
        provided ``install_paths`` set), False when it came from the
        back-compat shim or test override.  Stage 3a uses the flag
        to (a) verify install hashes only on installed packages and
        (b) emit a "back-compat shim active" warning on shim loads.
        """
        if install_paths is None:
            install_paths = {p.resolve() for p in default_install_paths()}
        found: list[tuple[Path, bool]] = []
        for root in search_paths:
            if not root.is_dir():
                continue
            is_install = root.resolve() in install_paths
            packages_dir = root / "packages"
            if not packages_dir.is_dir():
                # Allow the search path itself to BE the packages dir,
                # for flexibility in tests and one-off installs.
                packages_dir = root
            for child in sorted(packages_dir.iterdir()):
                if not child.is_dir():
                    continue
                if child.name.startswith(".") or child.name.startswith("_"):
                    continue
                manifest_path = child / "manifest.yaml"
                if manifest_path.is_file():
                    found.append((manifest_path, is_install))
        return found

    def _register_chat_tools(
        self,
        manifest: PackageManifest,
        *,
        write_chat_tools_allowed: bool = False,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Import the package's chat-tool modules and register decorated funcs.

        Returns ``(registered_names, gated_names, errors)``.  ``errors`` is
        non-fatal — the package still counts as loaded (so ``list_packages``
        shows partial load), but missing tools are logged.  ``gated_names``
        are write-capable chat tools that were SKIPPED because the operator
        did not opt the package in (``write_chat_tools_allowed=False``); they
        are surfaced in the package's load summary, NOT as fatal errors.

        Args:
            write_chat_tools_allowed: When ``True`` (operator opted this
                package in at install time), the package's write-capable
                chat-boundary tools ARE registered.  When ``False`` (the
                default — chat agent is read-only), those tools are
                gracefully SKIPPED (logged + surfaced as gated), while the
                package's read-only chat tools still register normally.
                Platform-boundary tools are hard-refused regardless.

        Trust-model invariants enforced here (in addition to the
        manifest-level checks done in ``security.py``):

        * **I10 (platform boundary)**: tools declaring
          ``trust_boundary='platform'`` from the package side are
          rejected.  ``register_extension_tool`` would also coerce to
          ``'chat'``, but rejecting here gives a clearer error.
        * **always_available / requires_user_confirm are platform
          decisions**: a package's ``@chat_tool`` decorator can set
          these meta entries, but the platform must NOT honor them.
          ``always_available=True`` would force the tool into every
          agent type's tool list (bypassing platform discoverability
          choices).  ``requires_user_confirm=False`` would let a
          package opt OUT of confirmation prompts.  Both are forced to
          their safe defaults on the platform side, regardless of what
          the package decorator declared.
        * **Platform-tool name collision**: if a package's tool name
          matches a hardcoded ``PLATFORM_TOOLS`` member, we record a
          load_errors entry.  ``register_extension_tool`` already
          silently skips on collision (the platform tool wins because
          it's loaded first), but observability is the issue here:
          without surfacing the collision, a package could try to
          shadow ``escalate`` and the operator would never know.
        """
        from ..chat_tool_loader import register_extension_tool, _loaded_tools
        from ..chat_tool_registry import PLATFORM_TOOLS, WRITE_CAPABILITIES
        from .loaders import _import_package_module

        registered: list[str] = []
        gated: list[str] = []
        errors: list[str] = []

        for rel in manifest.chat_tools:
            # Map the manifest's relative ``*.py`` path (e.g. ``tools.py``
            # or ``chat/tools.py``) to the dotted module path
            # ``_import_package_module`` expects (``tools`` /
            # ``chat.tools``).  Loading PACKAGE-aware — i.e. under the
            # synthetic ``_carpenter_pkg_.<name>`` parent package, with
            # the package dir on its ``__path__`` — is what lets the
            # module use intra-package relative imports such as
            # ``from .arc_builders import ...`` or ``from .scripts import
            # ...``.  This mirrors how ``loaders.py`` loads judge/data-
            # model/capability modules; the prior bare
            # ``spec_from_file_location`` load left ``__package__`` unset
            # and raised ImportError on any relative import.
            rel_path = Path(rel)
            dotted = ".".join(rel_path.with_suffix("").parts)
            try:
                module = _import_package_module(
                    manifest.name, dotted, manifest.source_path,
                )
            except Exception as exc:
                errors.append(
                    f"Failed to import chat tool module {rel!r}: {exc}",
                )
                logger.exception(
                    "Package %r: failed to import chat tool module %s",
                    manifest.name, rel,
                )
                continue

            # Collect @chat_tool-decorated callables.
            for attr_name in dir(module):
                obj = getattr(module, attr_name)
                if not callable(obj):
                    continue
                meta = getattr(obj, "_chat_tool_meta", None)
                if meta is None:
                    continue

                tool_name = meta["name"]

                # Defense in depth: even though manifest validation
                # rejected platform-boundary declarations at the
                # manifest level, we re-check here in case a package
                # tries to set trust_boundary='platform' via the
                # @chat_tool decorator directly.  register_extension_tool
                # itself coerces trust_boundary to "chat" and validates,
                # so this is a triple-check (decorator -> manifest ->
                # registration).
                if meta.get("trust_boundary") == "platform":
                    errors.append(
                        f"Chat tool {tool_name!r} declares "
                        f"trust_boundary='platform'; rejected.  "
                        f"Capability packages may not ship platform-"
                        f"boundary tools (I10).",
                    )
                    logger.error(
                        "Package %r: tool %r tried to declare platform "
                        "trust_boundary; refusing to register",
                        manifest.name, tool_name,
                    )
                    continue

                # Platform-tool shadowing: surface as load_errors entry
                # for observability.  The actual collision-skip in
                # ``register_extension_tool`` already prevents the
                # shadow (platform tools are loaded first), but if we
                # don't record it here a malicious package could try
                # to displace ``escalate`` and the operator would
                # never see the attempt.
                if tool_name in PLATFORM_TOOLS:
                    errors.append(
                        f"Chat tool {tool_name!r} collides with a "
                        f"hardcoded platform tool name; rejected.  "
                        f"Capability packages may not shadow platform "
                        f"tools (I10).",
                    )
                    logger.error(
                        "Package %r: tool %r collides with PLATFORM_TOOLS; "
                        "refusing to register",
                        manifest.name, tool_name,
                    )
                    continue

                # Cross-package tool-name collision: if a previously-
                # loaded extension tool already owns this name, surface
                # a load_errors entry.  ``register_extension_tool`` will
                # silently skip on collision, but observability matters
                # — without this, a malicious local clone discovered
                # first could silently shadow a sibling-repo package's
                # tool.
                if tool_name in _loaded_tools:
                    errors.append(
                        f"Chat tool {tool_name!r} collides with an "
                        f"already-registered tool; rejected.  Two "
                        f"packages cannot define the same tool name.",
                    )
                    logger.error(
                        "Package %r: tool %r collides with an already-"
                        "registered tool; refusing to register",
                        manifest.name, tool_name,
                    )
                    continue

                # Operator-gated WRITE chat tools (I10 relaxation): the
                # chat agent is read-only BY DEFAULT.  A package's chat-
                # boundary tool that declares write capabilities is only
                # registered when the operator explicitly opted this
                # package in at install time.  Otherwise it is GRACEFULLY
                # SKIPPED (logged + surfaced as gated in the load summary),
                # NOT a fatal error that floods load_errors.
                tool_write_caps = [
                    c for c in meta["capabilities"]
                    if c in WRITE_CAPABILITIES
                ]
                if tool_write_caps and not write_chat_tools_allowed:
                    gated.append(tool_name)
                    logger.info(
                        "Package %r: gated chat tool %r (write caps: %s) "
                        "not registered; operator did not opt in "
                        "(write_chat_tools_allowed=false)",
                        manifest.name, tool_name,
                        ", ".join(sorted(tool_write_caps)),
                    )
                    continue

                # IMPORTANT: ``always_available`` and ``requires_user_confirm``
                # are platform-side decisions.  We deliberately ignore
                # whatever the package's @chat_tool decorator declared
                # for these and force safe defaults.  See class docstring.
                try:
                    register_extension_tool(
                        name=tool_name,
                        description=meta["description"],
                        input_schema=meta["input_schema"],
                        handler=obj,
                        capabilities=meta["capabilities"],
                        always_available=False,
                        requires_user_confirm=False,
                        allow_write_caps=write_chat_tools_allowed,
                    )
                except ValueError as exc:
                    errors.append(
                        f"register_extension_tool refused {tool_name!r}: "
                        f"{exc}",
                    )
                    logger.error(
                        "Package %r: register_extension_tool refused %r: %s",
                        manifest.name, tool_name, exc,
                    )
                    continue

                registered.append(tool_name)

        return tuple(registered), tuple(gated), tuple(errors)

    # ── Internal seam shared by startup discovery and live reload ────
    #
    # ``discover_and_register`` is the startup path.  The helpers it is
    # built from — :meth:`resolve_search_paths`,
    # :meth:`prepare_manifest`, :meth:`register_package` and
    # :meth:`cross_validate_tools` — are also called by
    # :func:`carpenter.packages.reload.reload_packages`, so a live
    # reload registers a newly installed package's components through
    # EXACTLY the same code a cold start uses instead of a second,
    # drifting implementation.

    def resolve_search_paths(
        self, search_paths: list[Path] | None = None,
    ) -> tuple[list[Path], set[Path]]:
        """Return ``(search_paths, install_paths)`` for a discovery run.

        ``install_paths`` is the subset of roots whose packages get the
        hash-pin check (D24 SD6).  When the caller passes custom
        search paths (tests, or an explicit config override), all of
        them count as install paths so a fixture is verified exactly
        as a real install would be.
        """
        if search_paths is None:
            search_paths = default_search_paths()
        if search_paths == default_search_paths():
            install_paths = {p.resolve() for p in default_install_paths()}
            # Env-var override paths are explicit overrides — treat
            # them as install paths so they don't trigger shim-mode.
            install_paths |= {p.resolve() for p in _env_var_paths()}
        else:
            install_paths = {p.resolve() for p in search_paths}
        return list(search_paths), install_paths

    def prepare_manifest(
        self, manifest_path: Path, *, is_install: bool, db_conn=None,
    ) -> tuple[PackageManifest | None, str | None]:
        """Parse, security-check and hash-pin-gate one discovered manifest.

        Returns ``(manifest, None)`` when the package may be
        registered, or ``(manifest_or_None, reason)`` when it must be
        skipped.  Every skip is logged here exactly as it is at
        startup; the returned reason exists so a caller that reports
        per-package outcomes (the live reload path) can repeat it
        without re-deriving the decision.
        """
        try:
            raw = _read_raw_yaml(manifest_path)
            manifest = load_manifest(manifest_path)
            validate_manifest_security(
                manifest,
                raw_manifest=raw,
                manifest_path=manifest_path,
            )
        except (ManifestError, PackageSecurityError, FileNotFoundError) as exc:
            logger.error(
                "Skipping capability package at %s: %s",
                manifest_path, exc,
            )
            return None, f"manifest rejected: {exc}"
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception(
                "Unexpected error loading capability package %s: %s",
                manifest_path, exc,
            )
            return None, (
                f"manifest load failed: {type(exc).__name__}: {exc}"
            )

        # SD6: hash-verify installed packages on every load.
        if is_install and db_conn is not None:
            from .installer import verify_install
            try:
                vr = verify_install(manifest.name, conn=db_conn)
            except Exception as exc:  # pragma: no cover — defensive
                logger.exception(
                    "verify_install raised for %r at %s: %s",
                    manifest.name, manifest_path, exc,
                )
                return manifest, (
                    f"install verification raised: "
                    f"{type(exc).__name__}: {exc}"
                )
            if not vr.ok:
                # SD6 / spec §5.5: refuse to load on hash mismatch
                # OR missing install record.  An install-path dir
                # with no ``installed_packages`` row is a sign of
                # tampering or a partial install (the install
                # transaction either commits both or neither);
                # loading without verification would give an
                # attacker who can drop files into the install root
                # the ability to bypass hash checks.  Tightened
                # from the prior soft-warn (PR #306 followup
                # NIT #7).
                missing_row = (
                    " (no installed_packages row — re-run "
                    "install_package)" if vr.expected_hash is None else ""
                )
                logger.error(
                    "Refusing to load capability package %r from "
                    "%s: %s%s",
                    manifest.name, manifest_path, vr.message, missing_row,
                )
                return manifest, (
                    f"refused by the install hash pin: "
                    f"{vr.message}{missing_row}"
                )
        return manifest, None

    def register_package(
        self, manifest: PackageManifest, *, db_conn=None,
    ) -> RegisteredPackage:
        """Register one package's components and record it as loaded.

        The single registration path for capability-package components:
        chat tools, data models, arc templates, JUDGE handlers, step
        handlers, granted platform capabilities and in-process
        triggers.  Startup discovery and the live reload both call it.

        The caller is responsible for having run
        :meth:`prepare_manifest` first (manifest validated, security
        guards passed, install hash pin verified) and for the package
        not already being loaded.
        """
        # Per-package operator opt-in for WRITE chat tools.  Read the
        # install record's ``write_chat_tools_allowed`` flag — the
        # authoritative record of operator consent.  Without a DB
        # connection we cannot prove consent, so we fail-closed
        # (treat as not-opted-in; write chat tools are gated off).
        write_allowed = False
        if db_conn is not None:
            try:
                from .installer import (
                    write_chat_tools_allowed_for_package,
                )
                write_allowed = write_chat_tools_allowed_for_package(
                    db_conn, manifest.name,
                )
            except Exception:  # pragma: no cover — defensive
                logger.exception(
                    "Package %r: could not read write_chat_tools_allowed "
                    "flag; defaulting to gated-off (read-only)",
                    manifest.name,
                )
                write_allowed = False

        registered_names, gated_names, errors = self._register_chat_tools(
            manifest, write_chat_tools_allowed=write_allowed,
        )

        # D24 stage 3b: load arc templates, JUDGE handlers, data
        # models, and step handlers from the package.  Each loader
        # returns its own error list which we fold into the
        # package's load_errors so list_packages stays observable.
        artifact_counts: dict[str, int] = {}
        template_names: tuple[str, ...] = ()
        try:
            from .loaders import load_package_artifacts
            counts, art_errors, tnames = load_package_artifacts(
                manifest, db_conn=db_conn,
            )
            artifact_counts = counts
            errors = errors + tuple(art_errors)
            template_names = tuple(tnames)
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception(
                "Package %r: artifact loader raised: %s",
                manifest.name, exc,
            )
            errors = errors + (
                f"artifact loader raised: {type(exc).__name__}: {exc}",
            )

        # Package-capability framework: register the package's GRANTED
        # trusted dispatch verbs.  Only verbs recorded as granted in
        # the install record (operator-confirmed at install time) are
        # registered; declared-but-not-confirmed verbs are skipped.
        # Requires the DB to read the grant record — without it we
        # cannot prove the operator consented, so we register nothing
        # (fail-closed).
        if manifest.platform_capabilities:
            if db_conn is None:
                logger.warning(
                    "Package %r declares platform capabilities but no "
                    "DB connection was provided; cannot verify grants, "
                    "registering none",
                    manifest.name,
                )
            else:
                try:
                    from .installer import granted_verbs_for_package
                    from .loaders import load_platform_capabilities
                    granted = granted_verbs_for_package(
                        db_conn, manifest.name,
                    )
                    cap_n, cap_errs = load_platform_capabilities(
                        manifest, granted_verbs=granted,
                    )
                    errors = errors + tuple(cap_errs)
                    if cap_n:
                        artifact_counts["platform_capabilities"] = cap_n
                except Exception as exc:  # pragma: no cover — defensive
                    logger.exception(
                        "Package %r: capability loader raised: %s",
                        manifest.name, exc,
                    )
                    errors = errors + (
                        f"capability loader raised: "
                        f"{type(exc).__name__}: {exc}",
                    )

        # Phase 3a PR-B follow-up: instantiate + start the package's
        # in-process Trigger instances at STARTUP.  install_package
        # does this once at install time, but a daemon restart never
        # re-ran it, so a package's poll triggers silently vanished
        # after a restart (they were never re-added to the pollable-
        # trigger registry and never ticked).  We reuse
        # ``_install_triggers`` — it is load-not-copy (imports the
        # trigger modules, instantiates via ``load_package_triggers``,
        # and calls ``start()``); it does NOT copy files or write the
        # install record, so it is safe to call on a load.  It is
        # idempotent: it drops any prior registrations for the package
        # via ``unregister_for_package`` before re-loading, so a second
        # ``discover_and_register`` in the same process replaces rather
        # than duplicates the package's trigger instances.  The active
        # ``db_conn`` is threaded so a trigger's start-time
        # package_state reads/writes do not open a nested transaction
        # (mirrors #47).  Trigger-load failures are surfaced as
        # non-fatal load_errors rather than crashing startup.
        try:
            from .installer import _install_triggers
            triggers_n = _install_triggers(
                manifest, Path(manifest.source_path), conn=db_conn,
            )
            if triggers_n:
                artifact_counts["triggers"] = triggers_n
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception(
                "Package %r: trigger loader raised: %s",
                manifest.name, exc,
            )
            errors = errors + (
                f"trigger loader raised: {type(exc).__name__}: {exc}",
            )

        entry = RegisteredPackage(
            manifest=manifest,
            chat_tool_names=registered_names,
            template_names=template_names,
            artifact_counts=artifact_counts,
            load_errors=errors,
            gated_chat_tool_names=gated_names,
        )
        with self._lock:
            self._packages[manifest.name] = entry
        artifact_summary = (
            ", ".join(
                f"{n} {k}" for k, n in artifact_counts.items() if n
            )
            or "no D24 artifacts"
        )
        if gated_names:
            logger.warning(
                "Package %r: %d write-capable chat tool(s) GATED "
                "(requires operator opt-in; not registered): %s",
                manifest.name, len(gated_names),
                ", ".join(gated_names),
            )
        # Surface each non-fatal load error individually at WARNING.
        # Previously only the COUNT was logged in the summary below,
        # which made the "N non-fatal error(s)" message opaque -- an
        # operator could see "7 errors" with no way to tell what they
        # were without instrumenting the code.  Logging them here keeps
        # them observable in the daemon journal.
        for _err in errors:
            logger.warning(
                "Package %r: non-fatal load error: %s",
                manifest.name, _err,
            )
        logger.info(
            "Loaded capability package %r v%s from %s "
            "(%d chat tool(s)%s; %s%s)",
            manifest.name,
            manifest.version,
            manifest.source_path,
            len(registered_names),
            f", {len(gated_names)} gated (operator opt-in required)"
            if gated_names else "",
            artifact_summary,
            f"; {len(errors)} non-fatal error(s)" if errors else "",
        )
        return entry

    def cross_validate_tools(self) -> None:
        """Validate the merged chat-tool set across every loaded package.

        ``register_extension_tool`` already validates each tool
        individually, but running ``validate_tool_defs`` over the
        whole loaded set catches any remaining inconsistencies that
        only emerge across packages — including collisions that
        somehow slipped past the per-tool collision check.  Errors
        here are logged and recorded on the offending package's
        ``load_errors``; the validator does not mutate tool state, and
        an error already recorded is not appended twice (so repeated
        calls — e.g. a live reload after startup — stay idempotent).
        """
        try:
            from ..chat_tool_loader import _loaded_tools
            from ..chat_tool_registry import validate_tool_defs

            all_tools = list(_loaded_tools.values())
            # The per-package write-gate was already enforced at
            # registration time (write-cap chat tools only reach
            # ``_loaded_tools`` for an opted-in package; gated ones are
            # skipped).  This cross-package pass exists to catch
            # collisions / duplicates / platform-boundary / unknown-cap
            # issues across the merged set, NOT to re-litigate the write
            # gate — so we pass ``write_chat_tools_allowed=True`` to avoid
            # falsely flagging a legitimately opted-in package's write
            # chat tools.
            validation_errors = validate_tool_defs(
                all_tools, write_chat_tools_allowed=True,
            )
            if not validation_errors:
                return
            for verr in validation_errors:
                logger.error(
                    "Cross-package tool validation: %s", verr,
                )
            # Annotate every package whose tool name appears in an
            # error string.  This is best-effort: validate_tool_defs
            # error strings include the offending tool name in
            # quotes, so we match by substring.
            with self._lock:
                for pkg_name, pkg in list(self._packages.items()):
                    relevant = tuple(
                        ve for ve in validation_errors
                        if any(
                            f"{tn!r}" in ve
                            for tn in pkg.chat_tool_names
                        )
                        and ve not in pkg.load_errors
                    )
                    if relevant:
                        self._packages[pkg_name] = RegisteredPackage(
                            manifest=pkg.manifest,
                            chat_tool_names=pkg.chat_tool_names,
                            template_names=pkg.template_names,
                            artifact_counts=pkg.artifact_counts,
                            load_errors=pkg.load_errors + relevant,
                            gated_chat_tool_names=pkg.gated_chat_tool_names,
                        )
        except Exception:  # pragma: no cover — defensive
            logger.exception(
                "Cross-package validate_tool_defs raised; ignoring",
            )

    def note_duplicate(
        self, manifest: PackageManifest, manifest_path: Path,
    ) -> str:
        """Record a second package claiming an already-loaded name.

        Annotates the ALREADY-loaded package with a ``load_errors``
        entry so the collision is observable via ``list_packages``,
        and returns the message.  Without this, a malicious local
        clone discovered AFTER a legitimate package would be silently
        dropped.  We don't refuse to start; we log + record.
        """
        err_msg = (
            f"Duplicate package name {manifest.name!r}: a "
            f"package with this name was already loaded from "
            f"a different path.  This second occurrence at "
            f"{manifest_path} was skipped."
        )
        logger.error(
            "Capability package %r at %s collides with an "
            "already-loaded package; skipping",
            manifest.name, manifest_path,
        )
        with self._lock:
            existing = self._packages.get(manifest.name)
            if existing is not None and err_msg not in existing.load_errors:
                self._packages[manifest.name] = RegisteredPackage(
                    manifest=existing.manifest,
                    chat_tool_names=existing.chat_tool_names,
                    template_names=existing.template_names,
                    artifact_counts=existing.artifact_counts,
                    load_errors=existing.load_errors + (err_msg,),
                    gated_chat_tool_names=existing.gated_chat_tool_names,
                )
        return err_msg

    def loaded_from_same_path(
        self, manifest: PackageManifest,
    ) -> bool:
        """True if this exact package directory is already registered.

        Re-discovering the directory a loaded package came from is a
        no-op re-scan (startup running twice, or a live reload), NOT a
        duplicate-name collision — only a *different* directory
        claiming a loaded name is.
        """
        with self._lock:
            existing = self._packages.get(manifest.name)
        if existing is None:
            return False
        try:
            return (
                Path(existing.manifest.source_path).resolve()
                == Path(manifest.source_path).resolve()
            )
        except OSError:  # pragma: no cover — defensive
            return False

    def discover_and_register(
        self,
        search_paths: list[Path] | None = None,
        *,
        db_conn=None,
    ) -> list[RegisteredPackage]:
        """Discover packages on the search paths and register their artifacts.

        Idempotent in the sense that calling twice produces no
        duplicate registrations: a package whose name is already loaded
        from the same directory is skipped (with a debug log).  This is
        the COLD-START path; to pick up a package installed into a
        live process see
        :func:`carpenter.packages.reload.reload_packages`, and note
        that a package already imported in this process cannot be
        re-imported without a restart (``sys.modules`` caching).

        Args:
            search_paths: Override the default search paths.  ``None``
                uses :func:`default_search_paths`.
            db_conn: Optional SQLite connection for hash-verification
                of installed packages (D24 SD6).  When provided, every
                package discovered under an install path has its
                recorded hash compared to a fresh recomputation; on
                mismatch the package is logged-and-skipped.  When
                None, hash verification is skipped (e.g. tests that
                don't initialise the DB).

        Returns:
            List of :class:`RegisteredPackage` actually loaded by this
            call (excludes packages already loaded from a prior call
            and excludes packages that failed validation).
        """
        search_paths, install_paths = self.resolve_search_paths(search_paths)
        with self._lock:
            self._search_paths = list(search_paths)

        manifest_pairs = self._discover_manifests(
            search_paths, install_paths=install_paths,
        )
        if not manifest_pairs:
            logger.info(
                "No capability packages discovered on search paths: %s",
                [str(p) for p in search_paths],
            )
            return []

        loaded: list[RegisteredPackage] = []

        for manifest_path, is_install in manifest_pairs:
            manifest, skip_reason = self.prepare_manifest(
                manifest_path, is_install=is_install, db_conn=db_conn,
            )
            if manifest is None or skip_reason is not None:
                continue

            # D24 stage 3b: back-compat shim removed.  Every loaded
            # package must come from an install path; non-install paths
            # may still be passed in by tests for fixture flexibility,
            # but a non-install path is no longer special-cased.
            if self.get(manifest.name) is not None:
                if not self.loaded_from_same_path(manifest):
                    self.note_duplicate(manifest, manifest_path)
                else:
                    logger.debug(
                        "Capability package %r already loaded from %s; "
                        "skipping re-registration",
                        manifest.name, manifest_path,
                    )
                continue

            loaded.append(self.register_package(manifest, db_conn=db_conn))

        self.cross_validate_tools()

        if loaded:
            logger.info(
                "Capability package framework: %d package(s) loaded",
                len(loaded),
            )
        return loaded


# ── Module-level singleton (matches chat_tool_loader's pattern) ─────

_REGISTRY: PackageRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_registry() -> PackageRegistry:
    """Return the process-wide :class:`PackageRegistry` singleton."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = PackageRegistry()
        return _REGISTRY


def discover_and_register(
    search_paths: list[Path] | None = None,
    *,
    db_conn=None,
) -> list[RegisteredPackage]:
    """Convenience wrapper: discover + register on the singleton registry.

    Called once at server startup from :class:`carpenter.coordinator.Coordinator`.
    """
    return get_registry().discover_and_register(
        search_paths=search_paths, db_conn=db_conn,
    )

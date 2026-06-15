"""Carpenter capability package framework (Phase A + D24 stage 3a).

Capability packages are sibling repos (e.g. ``carpenter-packages``) that
ship reusable bundles of chat tools, KB articles, and (eventually)
triggers / arc templates / data models for Carpenter.

Phase A scope (per ``docs/2026-04-30_d8-capability-package-phase-a-plan.md``
and leadership decisions D20/D21/D22):

* Manifest format (``manifest.py``) — YAML descriptor + validator.
* Registry (``registry.py``) — discovery + chat-tool registration.
* Security guards (``security.py``) — I10 / I3 / I9 enforcement at load
  time: packages cannot ship platform-boundary tools, JUDGE code, or
  pre-populate policy allowlists; KB seeding is restricted to a
  per-package namespace; bundled ``.env`` files are forbidden.

D24 stage 3a additions (this PR):

* Manifest schema extended with declaration-only fields for
  ``arc_templates``, ``judge_handlers``, ``data_models``,
  ``kb_articles``, ``trigger_subscriptions`` (validation only —
  loading / wiring is stage 3b).
* ``installer.py`` provides copy-on-install + deterministic hashing
  + atomic-swap materialization, plus ``verify_install`` for
  startup-time integrity checks (SD3 / SD6).
* ``installed_packages`` + ``installed_packages_templates`` SQL
  tables record installs and their templates (D24 §5.1).
* New chat tools ``install_package`` / ``uninstall_package``
  (config_seed/chat_tools/packages.py) drive the install lifecycle
  through the standard chat-tool human-confirmation pattern (SD1 /
  SD4).

D24 stage 3b additions (this PR):

* ``loaders.py`` and ``handler_registry.py`` connect the manifest
  declarations to the platform's runtime dispatch:

  - Arc templates load through
    ``carpenter.core.engine.template_manager.load_template`` under
    their declared (flat, unprefixed) names (SD7).
  - JUDGE handlers register against
    :class:`PackageHandlerRegistry`; the dispatch wrapper in
    ``carpenter.security.judge`` consults the package map before
    falling back to the platform's default ``run_policy_checks``.
  - Data-model dataclasses load into a per-package isolated
    namespace (``_carpenter_pkg_.<package>.data_models``) and
    register against the handler registry's kind map; the JUDGE
    deserialiser consults the map for non-platform kinds.
  - Step handlers shipped beside a template's YAML
    (``templates/<name>/__init__.py`` with a
    ``register_handlers(registry)`` entrypoint) are imported and
    their registrations tracked so uninstall can revert them.

* The Phase A back-compat shim that scanned
  ``~/repos/carpenter-packages/packages/`` is **removed**.  The
  registry now ONLY loads packages from the install destination
  (``~/carpenter/packages/``).  The reference ``hello`` package is
  migrated to the install model; existing operators run
  ``install_package hello`` once at deploy time.

Trust-model invariants enforced at load and dispatch time:

* JUDGE handlers from packages run as deterministic Python on a
  *typed dataclass*.  The dispatch wrapper validates policy-typed
  fields against ``SecurityPolicies`` *before* invoking the handler,
  so the handler only does structural / cross-field checks the
  dataclass type system can't express.  No raw bytes, no DB handle,
  no arc state ever reaches package code.  (I3.)
* Package-shipped JUDGE handlers cannot register against
  platform-reserved template names; the
  :data:`carpenter.packages.handler_registry._PLATFORM_TEMPLATES`
  set is checked at registration time.  Cross-package collisions
  on template names, kind names, or chat-tool names are load
  errors.  (I7 / I10.)
* JUDGE handler signatures must accept exactly one positional
  argument (the deserialised dataclass).  Handlers that try to take
  more parameters are rejected at registration time.
"""

from .manifest import (
    ArcTemplateRef,
    EnvCredentialRef,
    JudgeHandlerRef,
    KbArticleRef,
    OAuthCredentialRef,
    PackageManifest,
    ManifestError,
    SubscriptionRef,
    load_manifest,
)
from .registry import (
    PackageRegistry,
    RegisteredPackage,
    get_registry,
    discover_and_register,
    default_search_paths,
)
from .security import (
    PackageSecurityError,
    validate_manifest_security,
)
from .installer import (
    InstallError,
    InstallResult,
    UninstallResult,
    VerifyResult,
    compute_package_hash,
    ensure_installer_tables,
    get_install_record,
    install_package,
    list_install_records,
    list_blocking_arcs,
    uninstall_package,
    verify_install,
)
from .handler_registry import (
    PackageHandlerRegistry,
    get_handler_registry,
)

__all__ = [
    "ArcTemplateRef",
    "EnvCredentialRef",
    "JudgeHandlerRef",
    "KbArticleRef",
    "OAuthCredentialRef",
    "PackageManifest",
    "ManifestError",
    "SubscriptionRef",
    "load_manifest",
    "PackageRegistry",
    "RegisteredPackage",
    "get_registry",
    "discover_and_register",
    "default_search_paths",
    "PackageSecurityError",
    "validate_manifest_security",
    "InstallError",
    "InstallResult",
    "UninstallResult",
    "VerifyResult",
    "compute_package_hash",
    "ensure_installer_tables",
    "get_install_record",
    "install_package",
    "list_install_records",
    "list_blocking_arcs",
    "uninstall_package",
    "verify_install",
    "PackageHandlerRegistry",
    "get_handler_registry",
]

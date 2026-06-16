"""Package-contributed TRUSTED platform-side dispatch verbs.

This module is the framework for the ``platform_capabilities`` manifest
section.  A capability package may contribute TRUSTED dispatch-verb
handlers (e.g. ``imap.fetch``) that run parent-side with egress and
credentials — a *different trust class* than the package's untrusted
EXECUTOR scripts.  Trusted code cannot be sandboxed; the framework's job
is **declaration + consent + scoping + registration + integrity-gating**,
not sandboxing the handler.

Pieces that live here:

* :class:`CapabilityContext` — the small, typed object handed to every
  trusted handler.  It exposes :meth:`CapabilityContext.secret` (resolve
  the package's declared credential value *platform-side* — from the
  process env, the package's OWN per-package ``.env``, or platform config,
  never from the executor env) plus the confirmed grant fields
  (host/port/protocol).  It
  binds the handler to its GRANTED scope: host and credentials come from
  here, never from handler ``params`` / the untrusted script.
* :class:`CapabilityRegistry` — process-wide map of registered verbs
  (``verb -> _RegisteredVerb``: package, grant, handler, host).  Mirrors
  :class:`carpenter.packages.handler_registry.PackageHandlerRegistry`'s
  singleton style.  Knows which package owns each verb so the dispatch
  gate can permit a verb only for that package's own arcs.
* :func:`capability_grant_for_package` — the per-package capability name
  (``pkg.<name>``) whose presence in an arc's ``_capabilities`` permits
  that package's verbs.  Reuses the existing per-arc capability mechanism
  (``carpenter.core.trust.capabilities``) rather than inventing a new
  permission model.

The actual wiring into the executor dispatch table and the allow-list
gate lives in :mod:`carpenter.api.callbacks` /
:mod:`carpenter.executor.dispatch_bridge`; this module is import-light so
it can be used from tests without the full HTTP/trust stack.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .manifest import EgressGrant

logger = logging.getLogger(__name__)

# UPPER_SNAKE credential-suffix shape — same convention as a kind:env
# credential's ``required_keys`` (carpenter.packages.manifest._ENV_SUFFIX_RE).
_SECRET_SUFFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# A package name is used as a single path component to locate that
# package's per-package ``.env``; it must be a safe path component (no
# separators, no traversal, not a dot-dir).  This mirrors the package-name
# shape enforced by the manifest loader, but we re-validate here because
# the resolved path holds secrets and must never escape the per-package
# directory.
_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# A trusted capability handler is invoked as ``handler(params, ctx)`` and
# must return a JSON-serialisable dict (the value crosses the JSON-only
# dispatch boundary back to the untrusted executor).
CapabilityHandler = Callable[[dict, "CapabilityContext"], dict]


class CapabilityError(Exception):
    """Raised when capability resolution / registration / dispatch fails."""


def capability_grant_for_package(package_name: str) -> str:
    """Return the per-arc capability name that permits a package's verbs.

    A package's registered capability verbs are permitted ONLY for arcs
    that carry this capability in their ``_capabilities`` arc_state
    (the existing per-arc grant mechanism — see
    :mod:`carpenter.core.trust.capabilities`).  This is namespaced per
    package so one package's grant never permits another's verbs.
    """
    return f"pkg.{package_name}"


@dataclass(frozen=True)
class CapabilityContext:
    """Scoped context handed to a trusted capability handler.

    The context is the ONLY channel through which a handler obtains its
    egress target and credentials; the handler must NOT read host/port or
    secrets from its ``params`` (which originate in the untrusted
    executor).  This binds the trusted handler to the scope the operator
    confirmed at install time.

    Attributes:
        package_name: The package that owns this capability.
        verb: The dispatch verb being served.
        kind: Capability kind (``"egress"`` today).
        protocol: Confirmed egress protocol (e.g. ``"imap"``).
        host: Confirmed egress host, resolved platform-side from the
            package's credential (``grant.host_from``).  Never taken from
            handler params.
        port: Confirmed egress port.
        credential_ref: The ``env_key_prefix`` of the package's declared
            ``kind: env`` credential.  :meth:`secret` resolves values
            under this prefix.

    Design note: ``db``-kind capabilities will add a ``ctx.db(namespace)``
    accessor here later; the shape (typed, scope-bound, no raw handles)
    is deliberately small so that extension is additive.
    """

    package_name: str
    verb: str
    kind: str
    protocol: str
    host: str
    port: int
    credential_ref: str

    def secret(self, ref: str) -> str:
        """Resolve a credential value PLATFORM-SIDE.

        ``ref`` is an ``UPPER_SNAKE`` suffix of the package's declared
        ``kind: env`` credential (e.g. ``"PASSWORD"``); the full env var
        is ``f"{self.credential_ref}_{ref}"``.  The value is resolved,
        in order, from: the live platform process environment; the
        package's OWN per-package ``.env`` at
        ``{base_dir}/config/packages/{package_name}/.env`` (where
        env-credentialed package secrets actually live, chmod 600); then
        the loaded platform config (which layers in the MAIN
        ``{base_dir}/.env`` for known credential keys).  Values are NEVER
        read from the untrusted executor's environment.

        The per-package ``.env`` is keyed strictly on ``self.package_name``
        (set by the trusted loader from the manifest, never the executor),
        so a handler can only ever read ITS OWN package's credentials.

        Raises:
            CapabilityError: if ``ref`` is malformed or the credential is
                not set platform-side.
        """
        if not isinstance(ref, str) or not _SECRET_SUFFIX_RE.match(ref):
            raise CapabilityError(
                f"secret(): ref must be an UPPER_SNAKE suffix, got {ref!r}",
            )
        key = f"{self.credential_ref}_{ref}"
        value = _resolve_platform_secret(key, package_name=self.package_name)
        if value is None:
            raise CapabilityError(
                f"secret({ref!r}): credential {key!r} is not set "
                f"platform-side (package {self.package_name!r})",
            )
        return value


def _package_env_path(package_name: str) -> Path | None:
    """Return the per-package ``.env`` path, or None if unavailable.

    The path is ``{base_dir}/config/packages/{package_name}/.env`` where
    ``base_dir`` comes from :data:`carpenter.config.CONFIG`.  ``package_name``
    is validated as a single safe path component (no separators, no ``..``
    traversal) BEFORE it is joined onto the base path, so a malformed or
    hostile package name can never escape the per-package directory.
    """
    if not package_name or not _PACKAGE_NAME_RE.match(package_name):
        # Reject names that are not a single safe path component
        # (covers ``..``, ``/``, ``\``, leading dots, empty, etc.).
        return None
    try:
        from .. import config
    except ImportError:  # pragma: no cover — config always present in prod
        return None
    base_dir = config.CONFIG.get("base_dir")
    if not base_dir:
        return None
    return Path(base_dir) / "config" / "packages" / package_name / ".env"


def _read_package_env_value(package_name: str, key: str) -> str | None:
    """Read ``key`` from a package's own per-package ``.env``, or None.

    Parses the file as simple ``KEY=VALUE`` lines (strip whitespace, skip
    blank lines and ``#`` comments, split on the first ``=``).  Never logs
    secret values; on any read error returns None (the caller treats a
    missing value as "not set platform-side").
    """
    env_path = _package_env_path(package_name)
    if env_path is None:
        return None
    try:
        if not env_path.is_file():
            return None
        text = env_path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover — permissions / IO edge
        logger.warning(
            "could not read per-package .env for package %r: %s",
            package_name, exc,
        )
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip()
    return None


def _resolve_platform_secret(
    key: str, *, package_name: str | None = None,
) -> str | None:
    """Resolve ``key`` from the platform env / per-package ``.env`` / config.

    Resolution order (highest precedence first):

    1. The live platform process environment (the daemon mirrors ``.env``
       writes into ``os.environ``; preserves the existing behavior).
    2. The package's OWN per-package ``.env`` at
       ``{base_dir}/config/packages/{package_name}/.env`` — this is where
       env-credentialed package secrets actually live, and is loaded by
       neither ``os.environ`` nor ``config.CONFIG``.  Keyed strictly on
       ``package_name`` so a handler can only read its own package's file.
    3. The loaded platform config (which layers the MAIN ``{base_dir}/.env``
       for known credential keys).

    This is deliberately the PLATFORM environment, not anything the
    untrusted executor can influence.
    """
    val = os.environ.get(key)
    if val:
        return val
    if package_name:
        pkg_val = _read_package_env_value(package_name, key)
        if pkg_val:
            return pkg_val
    try:
        from .. import config
    except ImportError:  # pragma: no cover — config always present in prod
        return None
    cfg = config.CONFIG
    return cfg.get(key) or cfg.get(key.lower()) or None


@dataclass
class _RegisteredVerb:
    """A registered trusted capability verb."""

    package_name: str
    verb: str
    kind: str
    handler: CapabilityHandler
    grant: EgressGrant
    host: str


class CapabilityRegistry:
    """Process-wide registry of package-contributed trusted dispatch verbs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # verb -> registered verb
        self._verbs: dict[str, _RegisteredVerb] = {}
        # package_name -> list of verbs (for unregistration)
        self._by_package: dict[str, list[str]] = {}

    def register(
        self,
        *,
        package_name: str,
        verb: str,
        kind: str,
        handler: CapabilityHandler,
        grant: EgressGrant,
        host: str,
    ) -> None:
        """Register a trusted capability verb.

        Raises:
            CapabilityError: collision with a built-in platform tool, with
                a verb already registered by a DIFFERENT package, or
                invalid input.
        """
        if not callable(handler):
            raise CapabilityError(
                f"register({verb!r}): handler must be callable",
            )
        # Never allow a capability verb to shadow a built-in dispatch verb.
        try:
            from ..api.callbacks import _DISPATCH
            builtin = verb in _DISPATCH
        except ImportError:  # pragma: no cover — defensive
            builtin = False
        if builtin:
            raise CapabilityError(
                f"register({verb!r}): collides with a built-in dispatch "
                f"verb; capability verbs may not shadow platform tools",
            )
        with self._lock:
            existing = self._verbs.get(verb)
            if existing is not None and existing.package_name != package_name:
                raise CapabilityError(
                    f"register({verb!r}): already registered by package "
                    f"{existing.package_name!r}; cannot shadow",
                )
            self._verbs[verb] = _RegisteredVerb(
                package_name=package_name,
                verb=verb,
                kind=kind,
                handler=handler,
                grant=grant,
                host=host,
            )
            verbs = self._by_package.setdefault(package_name, [])
            if verb not in verbs:
                verbs.append(verb)
        logger.info(
            "Registered trusted capability verb %r for package %r "
            "(kind=%s, egress=%s://%s:%d)",
            verb, package_name, kind, grant.protocol, host, grant.port,
        )

    def lookup(self, verb: str) -> _RegisteredVerb | None:
        with self._lock:
            return self._verbs.get(verb)

    def is_capability_verb(self, verb: str) -> bool:
        with self._lock:
            return verb in self._verbs

    def package_for_verb(self, verb: str) -> str | None:
        with self._lock:
            entry = self._verbs.get(verb)
            return entry.package_name if entry else None

    def verbs_for_package(self, package_name: str) -> frozenset[str]:
        with self._lock:
            return frozenset(self._by_package.get(package_name, ()))

    def list_verbs(self) -> list[tuple[str, str]]:
        """Return ``[(verb, package_name), ...]`` sorted by verb."""
        with self._lock:
            return [
                (v, e.package_name) for v, e in sorted(self._verbs.items())
            ]

    def context_for(self, verb: str) -> CapabilityContext | None:
        """Build the :class:`CapabilityContext` for a registered verb."""
        entry = self.lookup(verb)
        if entry is None:
            return None
        return CapabilityContext(
            package_name=entry.package_name,
            verb=entry.verb,
            kind=entry.kind,
            protocol=entry.grant.protocol,
            host=entry.host,
            port=entry.grant.port,
            credential_ref=entry.grant.credential_ref,
        )

    def dispatch(self, verb: str, params: dict) -> dict:
        """Invoke a registered verb's handler with its scoped context.

        This is the callable the dispatch table routes to.  It builds the
        :class:`CapabilityContext` (host/creds come from the confirmed
        grant, never from ``params``) and calls
        ``handler(params, ctx)``.

        Raises:
            CapabilityError: if the verb is not registered.
        """
        entry = self.lookup(verb)
        if entry is None:
            raise CapabilityError(f"verb {verb!r} is not a registered capability")
        ctx = self.context_for(verb)
        return entry.handler(params, ctx)

    def unregister_package(self, package_name: str) -> None:
        """Drop every verb a package registered.  Idempotent."""
        with self._lock:
            verbs = self._by_package.pop(package_name, [])
            for verb in verbs:
                entry = self._verbs.get(verb)
                if entry is not None and entry.package_name == package_name:
                    self._verbs.pop(verb, None)
        if verbs:
            logger.info(
                "Unregistered %d capability verb(s) for package %r",
                len(verbs), package_name,
            )

    def reset(self) -> None:
        """Drop EVERY registration.  Tests only."""
        with self._lock:
            self._verbs.clear()
            self._by_package.clear()


# ── Module-level singleton ─────────────────────────────────────────

_REGISTRY: CapabilityRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_capability_registry() -> CapabilityRegistry:
    """Return the process-wide :class:`CapabilityRegistry`."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = CapabilityRegistry()
        return _REGISTRY

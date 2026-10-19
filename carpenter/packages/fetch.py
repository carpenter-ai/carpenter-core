"""Fetching a capability package's tree from its source.

:mod:`carpenter.packages.sources` answers *where* a package comes from;
this module answers *how* those bytes become a local directory the
installer can hash and copy.  It defines the contract and dispatches on
:attr:`~carpenter.packages.sources.SourceSpec.kind`; the only fetcher
implemented here is the local-directory one, which is also the only kind
that needs no transport.  Git and archive fetchers are separate pieces
of work and plug in through :func:`register_fetcher`.

The shape of the contract is deliberate:

* **Quarantine first.** A remote fetcher writes into a fresh,
  per-call directory under :func:`staging_root_dir` and returns it as
  :attr:`FetchResult.staging`.  Attacker-supplied bytes therefore land
  somewhere the package registry does not look (the staging root is not
  on any search path) and somewhere the caller can delete wholesale with
  :func:`cleanup` whatever happens next.  Nothing is installed from the
  quarantine until the manifest and security guards have passed.
* **Limits are the fetcher's business, not the caller's.** Every fetcher
  takes the same :class:`FetchLimits` and is expected to abort — and
  leave nothing behind — when a source exceeds them.  A remote source is
  untrusted input, so an unbounded download, a file-count explosion or a
  hung connection has to fail rather than fill the disk.
* **``resolved`` pins what was actually fetched.** The commit sha40 for
  git, the archive's sha256 for an archive, the empty string for a local
  directory (there is nothing to pin: the operator's own tree is the
  anchor).  It is what the install record stores so a later run can tell
  whether it would get the same bytes.
* **The egress gate belongs to the dispatcher; redirects belong to the
  fetcher.** :func:`fetch_source` runs every non-local spec's URL past
  :func:`carpenter.security.egress.check_url` before it dispatches, so
  the check cannot be forgotten by a fetcher and every future fetcher
  inherits it.  That gate sees one URL.  **Any fetcher that follows HTTP
  redirects must re-check every hop** with
  :func:`carpenter.security.egress.check_redirect`, exactly as
  ``carpenter/tool_backends/web.py`` does (it follows redirects by hand
  for this reason: see ``_MAX_REDIRECTS`` there and the ``check_url`` /
  ``check_redirect`` pair around its request).  Without that, a 302 from
  an allowed host to a loopback, link-local or metadata address walks
  straight through the gate.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable
from urllib.parse import urlsplit

from ..security import egress
from .sources import SourceSpec

logger = logging.getLogger(__name__)

__all__ = [
    "FetchError",
    "FetchLimits",
    "FetchResult",
    "Fetcher",
    "LocalDirectoryFetcher",
    "cleanup",
    "fetch_source",
    "register_fetcher",
    "staging_root_dir",
]

#: Name of the quarantine root under ``{base_dir}/cache/``.  Sibling of
#: ``package-archives`` (the reconcile cache) and, like it, off the root
#: filesystem.
STAGING_DIR_NAME = "package-staging"

#: Prefix for per-call quarantine directories.  It starts with ``_`` so
#: that even a misconfigured search path pointed at the staging root
#: would skip these directories (package discovery ignores children
#: starting with ``.`` or ``_``).
_QUARANTINE_PREFIX = "_fetch-"


class FetchError(Exception):
    """Raised when a package source cannot be fetched.

    Also raised when a source's kind has no registered fetcher, so a
    caller never has to distinguish "could not fetch" from "cannot
    fetch this sort of source" by type alone — the message says which.
    """


@dataclass(frozen=True)
class FetchLimits:
    """Resource ceilings a fetcher must enforce on untrusted input.

    Attributes:
        max_bytes: Total bytes a fetch may transfer or expand to.
        max_files: Total regular files a fetched tree may contain.
        max_file_bytes: Largest single file a fetched tree may contain.
        timeout_s: Wall-clock budget for the whole fetch.
    """

    max_bytes: int = 64 * 1024 * 1024
    max_files: int = 5000
    max_file_bytes: int = 16 * 1024 * 1024
    timeout_s: float = 60.0


@dataclass(frozen=True)
class FetchResult:
    """What a fetch produced.

    Attributes:
        tree: The materialised package root — the directory that holds
            ``manifest.yaml``.  This is what the installer is handed.
        staging: The quarantine directory to remove when done (``None``
            for a local source, whose tree belongs to the operator and
            must never be deleted).  Pass the whole result to
            :func:`cleanup` rather than deleting this by hand.
        resolved: Git commit sha40, archive sha256, or ``""`` for local.
        bytes_fetched: Bytes transferred.  ``0`` for a local source,
            which is read in place rather than copied.
        files_fetched: Regular files written into the quarantine.  ``0``
            for a local source, for the same reason.
    """

    tree: Path
    staging: Path | None
    resolved: str
    bytes_fetched: int
    files_fetched: int


@runtime_checkable
class Fetcher(Protocol):
    """Materialises one or more source kinds into a local directory.

    Attributes:
        kinds: The :attr:`SourceSpec.kind` values this fetcher handles.
    """

    kinds: ClassVar[frozenset[str]]

    def fetch(
        self, spec: SourceSpec, dest: Path, *, limits: FetchLimits,
    ) -> FetchResult:
        """Materialise ``spec`` into ``dest`` and describe the result.

        ``dest`` is a freshly created, empty, per-call quarantine
        directory owned by the caller — except for a local source, where
        it is the operator's own source directory and must be treated as
        read-only.  Implementations enforce ``limits``, and on failure
        raise :class:`FetchError` rather than returning a partial tree.
        """
        ...


# ── the registry ────────────────────────────────────────────────────

_FETCHERS: dict[str, Fetcher] = {}


def register_fetcher(f: Fetcher) -> None:
    """Register ``f`` for every kind it declares.

    Idempotent per kind: a later registration for the same kind replaces
    the earlier one, so a platform package may substitute its own
    implementation.
    """
    kinds = getattr(f, "kinds", None)
    if not kinds or not all(isinstance(k, str) and k for k in kinds):
        raise FetchError(
            f"{f!r} declares no 'kinds'; a fetcher must name the source "
            f"kinds it handles",
        )
    if not callable(getattr(f, "fetch", None)):
        raise FetchError(f"{f!r} has no callable 'fetch'")
    for kind in kinds:
        _FETCHERS[kind] = f
    logger.debug("Registered package fetcher %s for %s", type(f).__name__,
                 ", ".join(sorted(kinds)))


# ── quarantine ──────────────────────────────────────────────────────


def staging_root_dir() -> Path:
    """Return the quarantine root, creating it ``0700`` if missing.

    ``{base_dir}/cache/package-staging``.  Mode ``0700`` because a
    partially fetched, not-yet-validated package tree is untrusted input
    that no other local user has any business reading or, worse,
    swapping out from under the installer.

    This path is deliberately NOT one of the registry's search paths, so
    a tree that never passes validation can never be discovered and
    loaded.
    """
    from .. import config

    base_dir = config.CONFIG.get("base_dir")
    if not base_dir:
        raise FetchError("staging_root_dir: config 'base_dir' is not set")
    root = Path(base_dir) / "cache" / STAGING_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:  # pragma: no cover — best effort on odd filesystems
        logger.warning("Could not set 0700 on package staging root %s", root)
    return root


def cleanup(result: FetchResult) -> None:
    """Remove the quarantine directory a fetch left behind.

    A no-op when :attr:`FetchResult.staging` is ``None`` — a local
    source's tree is the operator's own directory and is never removed.
    Safe to call more than once, and safe to call on a partial result in
    an ``except`` / ``finally`` block.
    """
    staging = result.staging
    if staging is None:
        return
    shutil.rmtree(staging, ignore_errors=True)


# ── the local fetcher ───────────────────────────────────────────────


class LocalDirectoryFetcher:
    """Uses a local package directory in place, copying nothing.

    The operator's own directory is the trust anchor for a local
    install, so there is nothing to transfer, nothing to quarantine and
    nothing to pin: the installer's atomic copy into the install
    destination is the only copy that happens.
    """

    kinds: ClassVar[frozenset[str]] = frozenset({"local"})

    def fetch(
        self, spec: SourceSpec, dest: Path, *, limits: FetchLimits,
    ) -> FetchResult:
        """Return ``spec``'s directory (plus any subdir) as the tree."""
        if spec.path is None:
            raise FetchError(
                f"local source {spec.raw!r} has no resolved path",
            )
        tree = spec.path / spec.subdir if spec.subdir else spec.path
        if not tree.is_dir():
            raise FetchError(
                f"local source directory {tree} does not exist",
            )
        return FetchResult(
            tree=tree, staging=None, resolved="",
            bytes_fetched=0, files_fetched=0,
        )


register_fetcher(LocalDirectoryFetcher())


# ── the egress gate ─────────────────────────────────────────────────


def _check_egress(spec: SourceSpec) -> None:
    """Refuse a remote source the platform's egress guard denies.

    One gate for every remote kind, in the dispatcher rather than in
    each fetcher: a package source is reached from the platform process,
    which can reach loopback services, the local network and cloud
    metadata endpoints that trust network location instead of
    credentials.  The allowlist is the platform's existing
    ``web_egress_allowlist`` — there is no second allowlist concept for
    packages — so an operator naming a forge on a private network adds
    its host there, once, and the message says so.

    Raises:
        FetchError: ``spec`` has no URL to check, or the guard refused
            it.  The guard's own ``EgressDenied`` is chained as the
            cause, and its message (which may name the resolved address)
            is quoted once; nothing further about the destination is
            added here.
    """
    url = spec.url or ""
    if not url:
        raise FetchError(
            f"{spec.kind} source {spec.raw!r} has no URL, so its "
            f"destination cannot be checked",
        )
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:  # pragma: no cover — defensive
        host = ""
    try:
        egress.check_url(url)
    except egress.EgressDenied as exc:
        where = f"host {host!r}" if host else f"source {spec.raw!r}"
        raise FetchError(
            f"refusing to fetch package source from {where}: the platform "
            f"will not fetch from it ({exc}). An operator must add the host "
            f"to the web_egress_allowlist config key to allow it.",
        ) from exc


# ── the dispatcher ──────────────────────────────────────────────────


def fetch_source(
    spec: SourceSpec,
    *,
    limits: FetchLimits | None = None,
    staging_root: Path | None = None,
) -> FetchResult:
    """Materialise ``spec`` into a local package tree.

    Dispatches on ``spec.kind`` to the registered fetcher.  A non-local
    spec's URL is first put past the platform's egress guard
    (:func:`carpenter.security.egress.check_url`), so no fetcher owns
    that check; a fetcher that then follows redirects must check each hop
    itself (see the module docstring).  For a remote
    kind a fresh per-call quarantine directory is created under
    ``staging_root`` (default :func:`staging_root_dir`) and handed to the
    fetcher as its destination; it is removed again if the fetch fails.
    A local source is used in place: the result carries
    ``staging=None`` and ``resolved=""``.

    The caller owns the returned quarantine and must pass the result to
    :func:`cleanup` once the tree has been installed (or abandoned).

    Args:
        spec: A parsed source spec.
        limits: Resource ceilings for the fetch.  Defaults to
            :class:`FetchLimits`'s own defaults.
        staging_root: Override for the quarantine root (tests).

    Returns:
        The :class:`FetchResult` the fetcher produced, after checking it
        really is a package tree in the place it claimed.

    Raises:
        FetchError: no fetcher is registered for the kind, the egress
            guard refused the destination, the fetch failed, or the
            fetcher returned a tree that is not a package directory
            inside its quarantine.
    """
    fetcher = _FETCHERS.get(spec.kind)
    if fetcher is None:
        known = ", ".join(sorted(_FETCHERS)) or "none"
        raise FetchError(
            f"no fetcher is registered for source kind {spec.kind!r} "
            f"(registered kinds: {known})",
        )
    limits = limits or FetchLimits()

    if spec.kind == "local":
        # Nothing to quarantine and nothing to reach: the directory is the
        # operator's own, so the egress guard is not consulted at all.
        result = fetcher.fetch(spec, spec.path or Path(), limits=limits)
        _validate_result(spec, result, quarantine=None)
        return result

    # Every remote kind passes the egress gate before any fetcher runs.
    _check_egress(spec)

    root = staging_root if staging_root is not None else staging_root_dir()
    root.mkdir(parents=True, exist_ok=True)
    quarantine = Path(tempfile.mkdtemp(prefix=_QUARANTINE_PREFIX, dir=root))
    try:
        result = fetcher.fetch(spec, quarantine, limits=limits)
        _validate_result(spec, result, quarantine=quarantine)
    except Exception:
        shutil.rmtree(quarantine, ignore_errors=True)
        raise
    return result


def _validate_result(
    spec: SourceSpec, result: FetchResult, *, quarantine: Path | None,
) -> None:
    """Check a fetcher honoured the contract before we trust its result."""
    if not isinstance(result, FetchResult):
        raise FetchError(
            f"fetcher for {spec.kind!r} returned {type(result).__name__}, "
            f"not a FetchResult",
        )
    tree = Path(result.tree)
    if quarantine is not None:
        # A remote fetcher must stay inside the quarantine it was given,
        # so the caller's cleanup really does remove everything it wrote.
        try:
            tree.resolve().relative_to(quarantine.resolve())
        except ValueError:
            raise FetchError(
                f"fetcher for {spec.kind!r} returned a tree outside its "
                f"staging directory",
            ) from None
    if not tree.is_dir():
        raise FetchError(
            f"fetched tree {tree} for {spec.raw!r} is not a directory",
        )
    if not (tree / "manifest.yaml").is_file():
        raise FetchError(
            f"fetched tree {tree} for {spec.raw!r} has no manifest.yaml",
        )

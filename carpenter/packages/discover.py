"""Listing what a configured package source offers, without installing it.

:mod:`carpenter.packages.sources` answers *where* a package comes from
and :mod:`carpenter.packages.fetch` answers *how* its bytes become a
local tree.  This module answers the question a person actually asks
first: **"what can I install from here?"** — so that "install the email
package" is a sentence someone can say in chat without first finding a
URL to paste.

Three properties make this safe to expose to an agent:

* **It is metadata only.**  A listing reads each candidate's
  ``manifest.yaml`` and nothing else.  No package module is imported, no
  package code is executed, and :mod:`carpenter.packages.static_preview`
  is deliberately NOT run either — a preview is what the operator reads
  on an approval page, after a specific install has been requested, and
  building one here would mean doing per-package work (and producing
  per-package security findings) for packages nobody asked for.
* **It fetches through one path.**  Everything goes through
  :func:`carpenter.packages.fetch.fetch_source`, which owns the egress
  gate (``web_egress_allowlist``), the quarantine, the resource caps, the
  credential injection and the tree hardening.  There is no second fetch
  path in this module, and the quarantine is always removed again: a
  listing must not leave a fetched tree behind.
* **It cannot be pointed anywhere.**  The only input is the *alias* of a
  source the operator already configured in ``package_sources``.  An
  agent cannot ask this module to reach a URL of its own choosing, which
  is what keeps it out of SSRF territory, and the listing cache's TTL
  bounds how often a caller can make the platform talk to a remote at
  all.

The listing's shape comes from the source's ``subdir_template``:

======================================  ==================================
``subdir_template``                     what is listed
======================================  ==================================
absent                                  the fetched tree root is ONE
                                        package
no ``{name}`` (e.g. ``pkg/core``)       that one fixed subdirectory is
                                        ONE package
contains ``{name}`` (``packages/
{name}``)                               the children of the directory
                                        ABOVE the ``{name}`` placeholder
                                        (here ``packages/``) are the
                                        candidates
======================================  ==================================

That directory above the placeholder is the *index root*, and it is the
only thing a listing has to materialise.  **A monorepo index root is not
itself a capability package**, and both the fetch dispatcher
(``fetch._validate_result``) and the git/archive fetchers currently
refuse to return a tree without a ``manifest.yaml`` in it.  So an index
listing needs an additive seam on the fetch side — a listing mode that
skips that one check — and until it exists
:func:`list_remote_packages` refuses an index-shaped source with a
message saying exactly that.  Everything above and below that one call
is shape-agnostic: :func:`listing_from_tree` builds the listing from a
materialised tree whichever shape it is, which is also how the index
shape is tested.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .fetch import FetchError, FetchLimits, cleanup, fetch_source
from .manifest import ManifestError, load_manifest
from .sources import (
    ConfiguredSource,
    SourceSpec,
    SourceSpecError,
    configured_sources,
    parse_source,
    redact,
    source_id,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DiscoveryError",
    "RemotePackage",
    "SourceListing",
    "SourceShape",
    "cached_listing",
    "listing_cache_path",
    "listing_from_tree",
    "listing_ttl_hours",
    "list_remote_packages",
    "list_source_aliases",
    "shape_for",
    "source_for_alias",
]

#: Name of the listing cache directory under ``{base_dir}/cache/``.
#: Sibling of ``package-staging`` (the fetch quarantine) and
#: ``package-archives`` (the reconcile cache).
LISTING_DIR_NAME = "package-listings"

#: Config key holding how long a cached listing is served for.
TTL_CONFIG_KEY = "package_sources_listing_ttl_hours"

#: Default TTL in hours.  Short enough that a package published this
#: morning is visible this afternoon, long enough that an agent asking
#: repeatedly does not turn into a clone loop against a remote.
DEFAULT_TTL_HOURS = 6

#: On-disk format version of a cache file.  A file written by a
#: different version is ignored rather than migrated — it is a cache.
CACHE_FORMAT = 1

#: A candidate directory name must look like this to be considered.  The
#: manifest inside it is still the authority on the package's NAME; this
#: only keeps us from treating odd filesystem entries as candidates.
_CANDIDATE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: 40-hex git commit / 64-hex sha256, used to decide how a ``resolved``
#: value pins an emitted spec string.
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_MANIFEST = "manifest.yaml"


class DiscoveryError(Exception):
    """Raised when a source cannot be listed.

    One bad *package* never raises: it lands in
    :attr:`SourceListing.errors`.  This is for the listing as a whole —
    an unknown alias, a malformed registry, a refused fetch, a missing
    index root.
    """


# ── what a listing is ───────────────────────────────────────────────


@dataclass(frozen=True)
class RemotePackage:
    """One package a configured source offers.

    Attributes:
        name: The package name from its manifest (the authority — not
            the directory name, which only locates it).
        version: Manifest version string.
        description: First line of the manifest description, so a
            listing stays one line per package.
        subdir: Where the package lives inside the source tree, as a
            POSIX relative path; ``None`` when the tree root IS the
            package.
        spec: The install spec string to pass straight to
            ``request_package_install``.  It names this exact subdir and
            pins the revision the listing was read from, and it parses
            back through :func:`carpenter.packages.sources.parse_source`.
    """

    name: str
    version: str
    description: str
    subdir: str | None
    spec: str


@dataclass(frozen=True)
class SourceListing:
    """What one configured source offered at one revision.

    Attributes:
        source: The configured source's alias.
        url: The source URL, redacted (never carries a credential).
        resolved: Commit sha / archive digest the listing was read from,
            so a reader can say which bytes they were looking at.
        packages: The packages found, in name order.
        fetched_at: ISO-8601 UTC timestamp of the fetch this listing
            came from (NOT of the cache read).
        errors: Per-candidate problems that did not stop the listing —
            one entry per unreadable manifest.  A listing with errors is
            still a listing.
    """

    source: str
    url: str
    resolved: str
    packages: tuple[RemotePackage, ...]
    fetched_at: str
    errors: tuple[str, ...]


@dataclass(frozen=True)
class SourceShape:
    """How a configured source lays its packages out.

    Attributes:
        is_index: ``True`` when the source holds many packages under an
            index root (``subdir_template`` contains ``{name}``).
        index_root: POSIX path of the directory whose children are the
            candidates; ``""`` means the fetched tree root.  Index only.
        prefix: Text before ``{name}`` within the placeholder path
            segment (``pkg-{name}`` → ``"pkg-"``).  Index only.
        suffix: Text after ``{name}`` within that segment.  Index only.
        template: The source's ``subdir_template``, kept so a
            candidate's subdir is built by substitution rather than by
            string surgery — which keeps an emitted spec identical to
            what a bare-name install through this source would resolve
            to.
        fixed_subdir: The single package's subdir for a non-index
            source; ``""`` means the tree root.
    """

    is_index: bool
    index_root: str = ""
    prefix: str = ""
    suffix: str = ""
    template: str | None = None
    fixed_subdir: str = ""

    @property
    def fetch_subdir(self) -> str | None:
        """The subdir a listing fetch has to materialise.

        For an index source that is the index root (the children of
        which are the candidates); for a single-package source it is the
        package's own subdir.  ``None`` means "the whole tree root".
        """
        root = self.index_root if self.is_index else self.fixed_subdir
        return root or None


# ── the configured source ───────────────────────────────────────────


def list_source_aliases() -> tuple[str, ...]:
    """Return the aliases of every configured package source, in order.

    Declaration order is the search order for a bare package name, so it
    is the order worth showing a reader.

    Raises:
        DiscoveryError: ``package_sources`` is malformed.  A typo is
            refused rather than skipped, exactly as
            :func:`carpenter.packages.sources.configured_sources` does
            it — a listing that silently omitted a source would be read
            as "that source has nothing".
    """
    try:
        return tuple(s.name for s in configured_sources())
    except SourceSpecError as exc:
        raise DiscoveryError(f"the package source registry is invalid: {exc}") from exc


def source_for_alias(alias: str) -> ConfiguredSource:
    """Return the configured source called ``alias``.

    Raises:
        DiscoveryError: no such alias, which is by far the most likely
            caller mistake — so the message lists the ones that do
            exist, or says plainly that none are configured and names
            the config key to put one in.
    """
    if not isinstance(alias, str) or not alias.strip():
        raise DiscoveryError("a source alias is required")
    wanted = alias.strip()
    try:
        sources = configured_sources()
    except SourceSpecError as exc:
        raise DiscoveryError(f"the package source registry is invalid: {exc}") from exc
    for source in sources:
        if source.name == wanted:
            return source
    if not sources:
        raise DiscoveryError(
            f"no package source is configured, so there is nothing to "
            f"list (asked for {wanted!r}). An operator adds one to the "
            f"'package_sources' config key, and adds its host to "
            f"'web_egress_allowlist' when it is not on the public "
            f"internet.",
        )
    known = ", ".join(s.name for s in sources)
    raise DiscoveryError(
        f"no configured package source is called {wanted!r}; the "
        f"configured sources are: {known}",
    )


def shape_for(source: ConfiguredSource) -> SourceShape:
    """Derive how ``source`` lays its packages out.

    See the module docstring's table.  The index root is the path ABOVE
    the segment holding ``{name}``: ``packages/{name}`` indexes
    ``packages/``, a bare ``{name}`` indexes the tree root, and
    ``pkgs/{name}/src`` still indexes ``pkgs/`` (the ``/src`` tail is
    part of each candidate's own path, not of the index).

    Raises:
        DiscoveryError: the template uses ``{name}`` in a way a listing
            cannot invert (in more than one path segment, or twice
            within one segment).  Installing through such a template
            still works; only listing it does not.
    """
    template = source.subdir_template
    if not template:
        return SourceShape(is_index=False, fixed_subdir="")
    if "{name}" not in template:
        return SourceShape(is_index=False, fixed_subdir=template, template=template)
    segments = template.split("/")
    holders = [i for i, seg in enumerate(segments) if "{name}" in seg]
    if len(holders) > 1:
        raise DiscoveryError(
            f"source {source.name!r} has subdir_template {template!r}, "
            f"which uses '{{name}}' in {len(holders)} path segments; a "
            f"listing cannot work out which directory to enumerate. "
            f"Installing a package by name through this source still "
            f"works.",
        )
    holder = segments[holders[0]]
    parts = holder.split("{name}")
    if len(parts) != 2:
        raise DiscoveryError(
            f"source {source.name!r} has subdir_template {template!r}, "
            f"which repeats '{{name}}' inside one path segment; a "
            f"listing cannot work out which directories are packages.",
        )
    return SourceShape(
        is_index=True,
        index_root="/".join(segments[: holders[0]]),
        prefix=parts[0],
        suffix=parts[1],
        template=template,
    )


# ── spec strings an operator can paste back ─────────────────────────


def _spec_string(base: SourceSpec, *, subdir: str | None, resolved: str) -> str:
    """Build the install spec for one package of a listing.

    Pins what was actually listed: the resolved commit for a git source,
    the archive's sha256 for an archive.  A caller passing this to
    ``request_package_install`` therefore gets the bytes the listing
    described, not whatever the branch moved on to — and the pin is
    visible to the human who approves it.

    The result round-trips through
    :func:`carpenter.packages.sources.parse_source`; a test asserts that
    for every spec a listing emits.
    """
    if base.kind == "git":
        url = f"git+{base.url}"
        ref = resolved if _SHA1_RE.match(resolved or "") else base.ref
        if ref:
            url = f"{url}@{ref}"
        return f"{url}#subdir={subdir}" if subdir else url
    if base.kind == "archive":
        bits: list[str] = []
        digest = resolved if _SHA256_RE.match(resolved or "") else base.digest
        if digest:
            bits.append(f"sha256={digest}")
        if subdir:
            bits.append(f"subdir={subdir}")
        return f"{base.url}#{'&'.join(bits)}" if bits else str(base.url)
    raise DiscoveryError(
        f"cannot build an install spec for a {base.kind!r} source",
    )


# ── reading a materialised tree ─────────────────────────────────────


def _scrub(text: str, tree: Path) -> str:
    """Replace the quarantine path in a message with a stable label.

    A manifest error quotes the file it failed on, which during a
    listing is a temporary directory under the staging root.  That path
    is noise to a reader, it changes on every fetch (so it would churn
    the cache file), and it is local filesystem detail that has no
    business in a chat reply.
    """
    return str(text).replace(str(tree), "<source>")


def _first_line(text: str) -> str:
    """Return the first non-empty line of a description, or ``""``."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _candidate_dirs(tree: Path, shape: SourceShape) -> list[tuple[str, Path]]:
    """Return ``(candidate name, package directory)`` pairs to read.

    For a single-package source that is one pair.  For an index source
    it is one pair per child of the index root whose name matches the
    template's placeholder segment; a child that does not match, or that
    holds no manifest, is skipped silently — it is not claiming to be a
    package (a monorepo legitimately holds a README, a tools directory
    and so on).  Children starting ``.`` or ``_`` are skipped for the
    same reason the package registry skips them.
    """
    if not shape.is_index:
        return [("", tree)]
    out: list[tuple[str, Path]] = []
    for child in sorted(tree.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        name = _candidate_name(child.name, shape)
        if name is None:
            continue
        # Built by substitution, so the subdir is identical to what a
        # bare-name install through this source resolves to.
        relative = (shape.template or "{name}").format(name=name)
        pkg_dir = tree / _relative_to_index(relative, shape)
        if (pkg_dir / _MANIFEST).is_file():
            out.append((name, pkg_dir))
    return out


def _candidate_name(dir_name: str, shape: SourceShape) -> str | None:
    """Extract the ``{name}`` part of an index child, or ``None``."""
    if not dir_name.startswith(shape.prefix) or not dir_name.endswith(shape.suffix):
        return None
    end = len(dir_name) - len(shape.suffix)
    if end <= len(shape.prefix):
        return None
    name = dir_name[len(shape.prefix): end]
    return name if _CANDIDATE_NAME_RE.match(name) else None


def _relative_to_index(relative: str, shape: SourceShape) -> str:
    """Strip the index root from a substituted template path.

    The fetched tree IS the index root (that is what a listing fetch
    materialises), so ``packages/{name}`` → ``<name>`` and
    ``pkgs/{name}/src`` → ``<name>/src``.
    """
    if not shape.index_root:
        return relative
    prefix = shape.index_root + "/"
    return relative[len(prefix):] if relative.startswith(prefix) else relative


def listing_from_tree(
    source: ConfiguredSource,
    tree: Path,
    *,
    resolved: str,
    fetched_at: str | None = None,
) -> SourceListing:
    """Build a listing by reading manifests in an already-fetched tree.

    ``tree`` is what a listing fetch materialises: the index root for a
    monorepo source, the package root for a single-package source.
    Nothing here touches the network, and nothing here imports or
    executes any of the tree's contents — it reads ``manifest.yaml``
    files and that is all.

    A candidate whose manifest will not parse is recorded in
    :attr:`SourceListing.errors` and the rest of the listing continues:
    one broken package in a monorepo must never make the other twenty
    invisible.

    Raises:
        DiscoveryError: ``tree`` is not a directory, the source's
            template cannot be inverted, or a single-package source's
            tree holds no manifest at all (there is then nothing to
            list, which is a source-level problem, not a per-package
            one).
    """
    tree = Path(tree)
    if not tree.is_dir():
        raise DiscoveryError(
            f"source {source.name!r}: the fetched tree is not a directory",
        )
    shape = shape_for(source)
    base = _base_spec(source)
    packages: list[RemotePackage] = []
    errors: list[str] = []

    candidates = _candidate_dirs(tree, shape)
    if not shape.is_index and not (tree / _MANIFEST).is_file():
        raise DiscoveryError(
            f"source {source.name!r} is configured as a single package "
            f"but its tree holds no {_MANIFEST}",
        )
    for candidate, pkg_dir in candidates:
        label = candidate or source.name
        try:
            manifest = load_manifest(pkg_dir / _MANIFEST)
        except (ManifestError, FileNotFoundError, OSError) as exc:
            errors.append(f"{label}: {_scrub(str(exc), tree)}")
            continue
        subdir = _subdir_for(shape, manifest_name=candidate)
        packages.append(
            RemotePackage(
                name=manifest.name,
                version=manifest.version,
                description=_first_line(manifest.description),
                subdir=subdir,
                spec=_spec_string(base, subdir=subdir, resolved=resolved),
            ),
        )
        if candidate and manifest.name != candidate:
            # Worth saying out loud: a bare-name install resolves through
            # the template on the MANIFEST name, so a directory that
            # disagrees with its manifest would not be found that way.
            # The spec above names the directory, so it still installs.
            errors.append(
                f"{label}: directory name does not match the manifest "
                f"name {manifest.name!r}; use the spec above rather than "
                f"the bare name",
            )
    packages.sort(key=lambda p: p.name)
    return SourceListing(
        source=source.name,
        url=redact(source.url),
        resolved=resolved,
        packages=tuple(packages),
        fetched_at=fetched_at or _utc_now_iso(),
        errors=tuple(errors),
    )


def _subdir_for(shape: SourceShape, *, manifest_name: str) -> str | None:
    """Return the package's subdir inside the SOURCE tree."""
    if not shape.is_index:
        return shape.fixed_subdir or None
    return (shape.template or "{name}").format(name=manifest_name)


def _base_spec(source: ConfiguredSource) -> SourceSpec:
    """Parse the configured source's URL into a spec, without a subdir."""
    try:
        return parse_source(source.url)
    except SourceSpecError as exc:
        raise DiscoveryError(
            f"source {source.name!r} has an invalid url: {exc}",
        ) from exc


# ── the cache ───────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    """Return the current UTC time, ISO-8601, seconds resolution."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def listing_ttl_hours() -> float:
    """Return how long a cached listing is served for, in hours.

    Reads ``package_sources_listing_ttl_hours``.  An unusable value
    falls back to :data:`DEFAULT_TTL_HOURS` with a warning rather than
    disabling the cache or making it eternal: a malformed config must
    never mean "never refresh".  ``0`` is honoured, and means "always
    refetch".
    """
    from .. import config

    raw = config.CONFIG.get(TTL_CONFIG_KEY, DEFAULT_TTL_HOURS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Config %s is %r, which is not a number; using %s hours",
            TTL_CONFIG_KEY, raw, DEFAULT_TTL_HOURS,
        )
        return float(DEFAULT_TTL_HOURS)
    if value < 0:
        logger.warning(
            "Config %s is %r, which is negative; using %s hours",
            TTL_CONFIG_KEY, raw, DEFAULT_TTL_HOURS,
        )
        return float(DEFAULT_TTL_HOURS)
    return value


def _cache_source_id(source: ConfiguredSource) -> str:
    """Identity of the thing that was listed, for the cache file name.

    :func:`carpenter.packages.sources.source_id` over the source URL and
    the directory a listing enumerates, so changing either the URL or
    the ``subdir_template`` yields a different file instead of serving a
    listing of somewhere else.
    """
    base = _base_spec(source)
    shape = shape_for(source)
    return source_id(replace(base, subdir=shape.fetch_subdir))


def listing_cache_path(source: ConfiguredSource) -> Path:
    """Return the cache file for ``source``'s listing.

    ``{base_dir}/cache/package-listings/<alias>-<source_id>.json``.  The
    alias is in the name so a reader can see whose listing it is; the
    source id is in it so a re-pointed alias cannot serve a stale one.
    """
    from .. import config

    base_dir = config.CONFIG.get("base_dir")
    if not base_dir:
        raise DiscoveryError("config 'base_dir' is not set")
    return (
        Path(base_dir) / "cache" / LISTING_DIR_NAME
        / f"{source.name}-{_cache_source_id(source)}.json"
    )


def _listing_to_json(listing: SourceListing, source: ConfiguredSource) -> dict:
    """Serialise a listing, with enough to validate it on the way back."""
    return {
        "format": CACHE_FORMAT,
        "source": listing.source,
        "source_id": _cache_source_id(source),
        "subdir_template": source.subdir_template,
        "url": listing.url,
        "resolved": listing.resolved,
        "fetched_at": listing.fetched_at,
        "packages": [
            {
                "name": p.name,
                "version": p.version,
                "description": p.description,
                "subdir": p.subdir,
                "spec": p.spec,
            }
            for p in listing.packages
        ],
        "errors": list(listing.errors),
    }


def _listing_from_json(data: dict, source: ConfiguredSource) -> SourceListing | None:
    """Rebuild a listing from a cache file, or ``None`` if unusable.

    Every mismatch is a miss rather than an error: this is a cache, and
    a refetch is always a correct answer.
    """
    if not isinstance(data, dict) or data.get("format") != CACHE_FORMAT:
        return None
    if data.get("source") != source.name:
        return None
    if data.get("source_id") != _cache_source_id(source):
        return None
    if data.get("subdir_template") != source.subdir_template:
        return None
    raw_packages = data.get("packages")
    if not isinstance(raw_packages, list):
        return None
    packages: list[RemotePackage] = []
    for entry in raw_packages:
        if not isinstance(entry, dict):
            return None
        try:
            packages.append(
                RemotePackage(
                    name=str(entry["name"]),
                    version=str(entry["version"]),
                    description=str(entry.get("description", "")),
                    subdir=entry["subdir"],
                    spec=str(entry["spec"]),
                ),
            )
        except KeyError:
            return None
    fetched_at = data.get("fetched_at")
    resolved = data.get("resolved")
    if not isinstance(fetched_at, str) or not isinstance(resolved, str):
        return None
    errors = data.get("errors")
    return SourceListing(
        source=source.name,
        url=str(data.get("url") or redact(source.url)),
        resolved=resolved,
        packages=tuple(packages),
        fetched_at=fetched_at,
        errors=tuple(str(e) for e in errors) if isinstance(errors, list) else (),
    )


def _is_fresh(listing: SourceListing, *, ttl_hours: float, now: datetime) -> bool:
    """Is a cached listing still inside its TTL?

    An unparseable timestamp counts as stale, so a hand-edited or
    truncated cache file causes a refetch rather than an eternal listing.
    """
    try:
        stamped = datetime.fromisoformat(listing.fetched_at)
    except (TypeError, ValueError):
        return False
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    return stamped + timedelta(hours=ttl_hours) > now


def cached_listing(alias: str) -> SourceListing | None:
    """Return ``alias``'s cached listing without fetching anything.

    Ignores the TTL: the caller is told when it was fetched and can
    decide.  This is what the read-only chat tool uses to summarise
    every configured source at once — listing all of them by fetching
    all of them would turn one question into one network round trip per
    source.

    Returns ``None`` when there is no usable cache file (absent,
    unreadable, written by another format version, or recorded against
    a different URL or ``subdir_template``).
    """
    source = source_for_alias(alias)
    return _read_cache(source)


def _read_cache(source: ConfiguredSource) -> SourceListing | None:
    """Load and validate ``source``'s cache file, or return ``None``."""
    try:
        path = listing_cache_path(source)
    except DiscoveryError:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    listing = _listing_from_json(data, source)
    if listing is None:
        logger.debug("Ignoring unusable package listing cache at %s", path)
    return listing


def _write_cache(listing: SourceListing, source: ConfiguredSource) -> None:
    """Write ``listing`` to its cache file atomically, best effort.

    A cache that cannot be written is logged and otherwise ignored: the
    listing in hand is still correct, and failing the call because a
    cache write failed would be the wrong trade.
    """
    try:
        path = listing_cache_path(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(_listing_to_json(listing, source), handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Could not cache the %s package listing: %s",
                       source.name, exc)


# ── the entry point ─────────────────────────────────────────────────


def list_remote_packages(
    alias: str,
    *,
    refresh: bool = False,
    limits: FetchLimits | None = None,
    conn=None,
) -> SourceListing:
    """Return what the configured source ``alias`` offers.

    Serves the cached listing when there is a fresh one, otherwise
    fetches the source once through
    :func:`carpenter.packages.fetch.fetch_source` — which applies the
    egress gate, the resource caps, the credential injection and the
    tree hardening — reads the candidates' manifests, removes the
    quarantine and caches the result.

    Nothing in this call imports or executes any fetched code.

    Args:
        alias: The alias of a source in the ``package_sources`` config
            list.
        refresh: Fetch even when a fresh cached listing exists.  Note
            that the read-only chat tool never passes this: the TTL is
            what stops an agent making the platform talk to a remote on
            every question.
        limits: Resource ceilings for the fetch.  Defaults to
            :class:`carpenter.packages.fetch.FetchLimits`'s own.
        conn: Accepted so callers can pass their database connection
            uniformly with the other package entry points.  Discovery
            reads no database and ignores it; a listing is a property of
            the remote, not of this install.

    Returns:
        A :class:`SourceListing`.  Its ``fetched_at`` is when the bytes
        were read, which for a cache hit is in the past — say so when
        showing it to a person.

    Raises:
        DiscoveryError: unknown alias, malformed registry, a fetch the
            platform refused (the egress message is quoted, and it names
            ``web_egress_allowlist`` and the host), a missing index root,
            or an index-shaped source, which the fetch layer cannot
            materialise yet (see the module docstring).
    """
    source = source_for_alias(alias)
    shape = shape_for(source)

    if not refresh:
        cached = _read_cache(source)
        if cached is not None and _is_fresh(
            cached,
            ttl_hours=listing_ttl_hours(),
            now=datetime.now(timezone.utc),
        ):
            logger.debug(
                "Serving cached package listing for %s (fetched %s)",
                source.name, cached.fetched_at,
            )
            return cached

    base = _base_spec(source)
    spec = replace(
        base, subdir=shape.fetch_subdir, alias=source.name, raw=source.name,
    )
    logger.info(
        "Listing packages offered by source %s (%s)",
        source.name, redact(source.url),
    )
    try:
        result = fetch_source(spec, limits=limits or FetchLimits())
    except FetchError as exc:
        raise DiscoveryError(_fetch_refusal(source, shape, exc)) from exc
    try:
        listing = listing_from_tree(
            source, result.tree,
            resolved=result.resolved, fetched_at=_utc_now_iso(),
        )
    finally:
        # A listing must never leave a fetched tree behind: it is
        # untrusted input that nothing is going to install.
        cleanup(result)
    _write_cache(listing, source)
    return listing


def _fetch_refusal(
    source: ConfiguredSource, shape: SourceShape, exc: FetchError,
) -> str:
    """Explain a refused listing fetch, including the known gap.

    An index root is not a capability package, and the fetch layer
    refuses to hand back a tree without a ``manifest.yaml`` in it
    (``fetch._validate_result``, and the same check inside the git and
    archive fetchers).  Listing a monorepo therefore needs an additive
    listing mode on the fetch side; say so rather than leaving a reader
    to guess that their source is broken.
    """
    message = f"could not list source {source.name!r}: {exc}"
    if shape.is_index and _MANIFEST in str(exc):
        root = shape.index_root or "the repository root"
        return (
            f"{message}. {source.name!r} is configured with "
            f"subdir_template {shape.template!r}, so listing it means "
            f"reading the directory {root!r}, which is an index of "
            f"packages and not a package itself. The platform's package "
            f"fetch currently returns a tree only when it holds a "
            f"{_MANIFEST}, so this source cannot be listed yet; install "
            f"from it by name or by an explicit spec in the meantime."
        )
    return message

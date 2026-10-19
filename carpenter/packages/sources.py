"""Source-spec parsing and canonicalisation for capability packages.

A *source spec* is the single string an operator supplies to say where a
capability package comes from.  Historically that was always a bare
package name resolved against one hardcoded local directory; this module
widens it to a small, explicit grammar covering local directories, git
repositories and release archives, while keeping the bare-name form
behaving exactly as it did.

Accepted forms (and nothing else):

* **bare name** — ``^[a-z0-9][a-z0-9._-]*$``.  Resolved through the
  operator's configured source registry (``packages.sources`` /
  ``packages.search_order``, see :func:`configured_sources`).  When no
  configured alias matches, it falls back to the legacy local layout
  ``~/repos/carpenter-packages/packages/<name>`` so existing installs
  keep working untouched.
* **local directory** — an absolute path (``~`` is expanded) or a
  ``file://`` URL pointing at the directory that holds ``manifest.yaml``.
* **git** — ``git+https://host/org/repo.git[@ref][#subdir=rel/path]``.
* **archive** — ``https://host/path/pkg.tar.gz[#sha256=<64 hex>]``
  (``.tgz`` and ``.tar`` are accepted too).

Plain ``http://`` is accepted **only** for a loopback literal host
(``127.0.0.1``, ``localhost``, ``[::1]``) so a test can run a throwaway
server without a TLS certificate.  ``git+ssh://``, ``ssh://`` and
``git://`` are never accepted: they carry no transport integrity the
platform can reason about, and the first two depend on an agent/key
setup outside the platform's control.

Design constraints this module holds to:

* **Pure.** Parsing performs no network I/O and never reads a package
  tree.  The only filesystem contact is path normalisation for the local
  form (``expanduser`` + ``resolve``), so a spec can be parsed, shown to
  an operator, and compared long before any bytes are fetched.
* **Credential-free canonical output.** :attr:`SourceSpec.url` never
  carries ``userinfo`` or a token query parameter, and
  :attr:`SourceSpec.raw` is the operator's own string with any credential
  replaced by ``***`` (see :func:`redact`).  A read token belongs in the
  platform environment variable named by a configured source's
  ``credential_ref``, never in a stored or logged URL.
* **Stable identity.** :func:`source_id` hashes the *logical* source
  (kind + canonical location + subdir) and deliberately ignores the ref
  and the digest, so the same source keeps one id across versions while
  two different remotes publishing the same package name never collide.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

__all__ = [
    "ConfiguredSource",
    "SourceSpec",
    "SourceSpecError",
    "configured_sources",
    "describe",
    "legacy_local_source",
    "parse_source",
    "redact",
    "resolve_bare_name",
    "source_id",
    "source_search_order",
]


# ── grammar constants ───────────────────────────────────────────────

#: A bare package name.  Deliberately wider than the manifest's own
#: ``name`` pattern (which forbids a leading digit and a ``.``) because
#: this matches what the operator *typed*; the manifest validator still
#: has the last word on the package's real name.
_BARE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

#: A POSIX environment-variable identifier (``credential_ref`` is the
#: NAME of a platform-provided variable, never a value).
_ENV_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")

#: Conservative git ref shape: a branch/tag name or a 40-hex commit.
#: Excludes the ref-name forms git itself rejects plus anything that
#: could be read as an option or a path traversal.
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")

_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar")

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: Query parameters that carry a read token on the forges we know of.
#: Stripped from every canonical URL and masked by :func:`redact`.
_CREDENTIAL_QUERY_KEYS = frozenset(
    {"token", "access_token", "private_token", "api_key"},
)

#: The pre-registry local layout a bare name falls back to.  Keeping it
#: is what makes this module behaviour-preserving for existing installs.
_LEGACY_SOURCE_ROOT = "~/repos/carpenter-packages/packages"

#: Appended to every refusal so the operator is told what *does* work
#: rather than only what did not.
_ACCEPTED_FORMS = (
    "accepted source forms: a bare package name (resolved through the "
    "configured packages.sources, else the local source tree); an "
    "absolute path or file:// URL to a package directory; "
    "git+https://host/org/repo.git[@ref][#subdir=rel/path]; "
    "https://host/path/pkg.tar.gz[#sha256=<64 lowercase hex>] (.tgz and "
    ".tar also accepted). Plain http:// is accepted only for a loopback "
    "host (127.0.0.1, localhost, [::1]); git+ssh://, ssh:// and git:// "
    "are never accepted."
)


class SourceSpecError(Exception):
    """Raised when a source spec (or the configured registry) is invalid."""


def _fail(problem: str) -> "SourceSpecError":
    """Build a refusal that always states the accepted grammar."""
    return SourceSpecError(f"{problem}; {_ACCEPTED_FORMS}")


# ── the spec ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceSpec:
    """A parsed, canonicalised package source.

    Attributes:
        kind: ``"local"``, ``"git"`` or ``"archive"``.
        raw: What the operator typed, with any credential redacted.
            Safe to log, display and store.
        url: Canonical, credential-free URL (git / archive only).  For
            git this is the clone URL *without* the ``git+`` prefix, the
            ``@ref`` suffix or the fragment.
        path: Resolved local directory holding ``manifest.yaml`` (local
            only).
        ref: Git branch, tag or 40-hex commit (git only).  ``None``
            means "the remote's default branch".
        subdir: POSIX relative path of the package root inside the
            fetched tree.  ``None`` means the tree root *is* the package.
        digest: Expected lowercase hex sha256 of the archive bytes
            (archive only).
        alias: The configured source alias a bare name resolved through,
            or ``None`` for a spec the operator wrote out in full.
    """

    kind: str
    raw: str
    url: str | None = None
    path: Path | None = None
    ref: str | None = None
    subdir: str | None = None
    digest: str | None = None
    alias: str | None = None


# ── redaction ───────────────────────────────────────────────────────


def redact(url: str) -> str:
    """Return ``url`` with any embedded credential replaced by ``***``.

    Strips ``userinfo`` (``https://user:token@host/…`` →
    ``https://***@host/…``) and masks the value of any ``token``,
    ``access_token``, ``private_token`` or ``api_key`` query parameter
    (case-insensitively).  Anything that is not a URL — a bare name, a
    local path — is returned unchanged.

    Use this everywhere a source URL is logged, previewed or persisted.
    """
    if not url:
        return url
    prefix = ""
    rest = url
    if rest.startswith("git+"):
        prefix, rest = "git+", rest[4:]
    try:
        parts = urlsplit(rest)
    except ValueError:  # pragma: no cover — defensive
        return url
    if not parts.scheme or not parts.netloc:
        return url
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "***@" + netloc.rsplit("@", 1)[1]
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if any(k.lower() in _CREDENTIAL_QUERY_KEYS for k, _ in pairs):
            query = urlencode(
                [
                    (k, "***" if k.lower() in _CREDENTIAL_QUERY_KEYS else v)
                    for k, v in pairs
                ],
                safe="*",
                quote_via=quote_plus,
            )
    return prefix + urlunsplit(
        (parts.scheme, netloc, parts.path, query, parts.fragment),
    )


# ── identity and description ────────────────────────────────────────


def source_id(spec: SourceSpec) -> str:
    """Return a stable 16-char lowercase hex id for a logical source.

    The id is ``sha256`` over the kind, the canonical location (URL for
    a remote, path for a local directory) and the subdir.  The ref and
    the digest are deliberately **excluded**: those pin a particular
    revision, while the id answers "which source is this?".  Two remotes
    publishing the same package name therefore get different ids, and
    the same remote keeps one id across version bumps.
    """
    location = spec.url if spec.url is not None else (
        str(spec.path) if spec.path is not None else ""
    )
    payload = "\0".join((spec.kind, location, spec.subdir or ""))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def describe(spec: SourceSpec) -> str:
    """Return a one-line human provenance string for operator previews.

    Never contains a credential: it is built from the canonical,
    credential-free fields.
    """
    bits: list[str] = []
    if spec.kind == "local":
        bits.append(f"local directory {spec.path}")
    elif spec.kind == "git":
        bits.append(f"git {spec.url}")
        bits.append(f"at {spec.ref}" if spec.ref else "at the default branch")
    elif spec.kind == "archive":
        bits.append(f"archive {spec.url}")
        if spec.digest:
            bits.append(f"sha256 {spec.digest[:12]}...")
        else:
            bits.append("no sha256 pin")
    else:  # pragma: no cover — defensive
        bits.append(f"{spec.kind} {spec.url or spec.path}")
    if spec.subdir:
        bits.append(f"subdir {spec.subdir}")
    if spec.alias:
        bits.append(f"via configured source {spec.alias!r}")
    return ", ".join(bits)


# ── fragment / component validation ─────────────────────────────────


def _parse_fragment(fragment: str, *, allow_digest: bool) -> tuple[str | None, str | None]:
    """Parse a ``#key=value[&key=value]`` fragment into (subdir, digest)."""
    subdir: str | None = None
    digest: str | None = None
    if not fragment:
        return None, None
    for item in fragment.split("&"):
        if not item:
            continue
        key, sep, value = item.partition("=")
        if not sep:
            raise _fail(
                f"source fragment {item!r} is not a key=value pair",
            )
        key = key.strip()
        if key == "subdir":
            subdir = _validate_subdir(value)
        elif key == "sha256":
            if not allow_digest:
                raise _fail(
                    "a #sha256= fragment only applies to an archive source",
                )
            digest = _validate_digest(value)
        else:
            raise _fail(
                f"unknown source fragment key {key!r} (expected 'subdir'"
                f"{' or sha256' if allow_digest else ''})",
            )
    return subdir, digest


def _validate_digest(value: str) -> str:
    """Validate a ``sha256=`` fragment value (64 lowercase hex chars)."""
    candidate = value.strip()
    if not _SHA256_RE.match(candidate):
        raise _fail(
            f"sha256 fragment {candidate!r} is not 64 lowercase hex "
            f"characters",
        )
    return candidate


def _validate_subdir(value: str) -> str:
    """Validate a package-root subdir: a relative POSIX path, no escapes."""
    candidate = value.strip()
    if not candidate:
        raise _fail("subdir is empty")
    if "\\" in candidate:
        raise _fail(
            f"subdir {candidate!r} must use POSIX '/' separators",
        )
    pure = PurePosixPath(candidate)
    if pure.is_absolute():
        raise _fail(f"subdir {candidate!r} must be relative, not absolute")
    parts = [p for p in candidate.split("/") if p not in ("", ".")]
    if not parts:
        raise _fail(f"subdir {candidate!r} does not name a directory")
    if any(p == ".." for p in parts):
        raise _fail(
            f"subdir {candidate!r} must not contain a '..' component "
            f"(it would escape the fetched tree)",
        )
    return "/".join(parts)


def _validate_ref(value: str) -> str:
    """Validate a git ref: a branch/tag name or a 40-hex commit."""
    candidate = value.strip()
    if not candidate:
        raise _fail("git ref after '@' is empty")
    if _SHA1_RE.match(candidate):
        return candidate
    if len(candidate) > 255:
        raise _fail("git ref is longer than 255 characters")
    if not _GIT_REF_RE.match(candidate):
        raise _fail(
            f"git ref {candidate!r} is not a branch, tag or 40-hex commit",
        )
    if (
        ".." in candidate
        or candidate.endswith((".lock", "/", "."))
        or "@{" in candidate
        or "//" in candidate
    ):
        raise _fail(f"git ref {candidate!r} is not a valid ref name")
    return candidate


def _canonical_remote_url(parts, *, scheme: str, path: str) -> str:
    """Rebuild a credential-free URL from a split result.

    ``userinfo`` is dropped and every credential-bearing query parameter
    is removed outright (not masked): this URL is stored and re-fetched,
    so a masked token would be a broken token.  A read token reaches the
    fetcher from the environment variable a configured source names in
    ``credential_ref``.
    """
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"          # IPv6 literal
    netloc = f"{host}:{parts.port}" if parts.port else host
    query = ""
    if parts.query:
        kept = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _CREDENTIAL_QUERY_KEYS
        ]
        query = urlencode(kept) if kept else ""
    return urlunsplit((scheme, netloc, path, query, ""))


def _check_scheme_and_host(parts, *, scheme: str, label: str) -> None:
    """Enforce https, or http only to a loopback literal."""
    if scheme not in ("https", "http"):
        raise _fail(f"{label} source scheme {scheme!r} is not supported")
    host = (parts.hostname or "").lower()
    if not host:
        raise _fail(f"{label} source URL has no host")
    if scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise _fail(
            f"plain http:// is refused for host {host!r} — use https:// "
            f"(http:// is accepted only for a loopback host so tests need "
            f"no TLS)",
        )


# ── remote parsing ──────────────────────────────────────────────────


def _parse_git(spec: str) -> SourceSpec:
    """Parse ``git+https://host/org/repo.git[@ref][#subdir=…]``."""
    rest = spec[len("git+"):]
    url_part, _, fragment = rest.partition("#")
    parts = urlsplit(url_part)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise _fail(
            f"git source scheme 'git+{scheme}' is not supported (only "
            f"git+https, and git+http to a loopback host)",
        )
    _check_scheme_and_host(parts, scheme=scheme, label="git")
    # The optional ``@ref`` lives in the PATH, so splitting on the last
    # '@' of the path leaves any userinfo in the netloc untouched.
    path = parts.path
    ref: str | None = None
    if "@" in path:
        path, _, ref_text = path.rpartition("@")
        ref = _validate_ref(ref_text)
    if not path or path == "/":
        raise _fail(
            f"git source {redact(url_part)!r} has no repository path",
        )
    subdir, _digest = _parse_fragment(fragment, allow_digest=False)
    return SourceSpec(
        kind="git",
        raw=redact(spec),
        url=_canonical_remote_url(parts, scheme=scheme, path=path),
        ref=ref,
        subdir=subdir,
    )


def _parse_archive(spec: str) -> SourceSpec:
    """Parse ``https://host/path/pkg.tar.gz[#sha256=…]``."""
    url_part, _, fragment = spec.partition("#")
    parts = urlsplit(url_part)
    scheme = parts.scheme.lower()
    _check_scheme_and_host(parts, scheme=scheme, label="archive")
    path = parts.path
    if not path.lower().endswith(_ARCHIVE_SUFFIXES):
        raise _fail(
            f"http(s) source {redact(url_part)!r} does not name an archive "
            f"(expected a path ending in {', '.join(_ARCHIVE_SUFFIXES)}); "
            f"prefix a git repository URL with 'git+'",
        )
    subdir, digest = _parse_fragment(fragment, allow_digest=True)
    return SourceSpec(
        kind="archive",
        raw=redact(spec),
        url=_canonical_remote_url(parts, scheme=scheme, path=path),
        subdir=subdir,
        digest=digest,
    )


def _parse_remote(spec: str) -> SourceSpec:
    """Parse a spec that must be a git or archive source."""
    lowered = spec.lower()
    if lowered.startswith("git+"):
        return _parse_git(spec)
    if lowered.startswith(("http://", "https://")):
        return _parse_archive(spec)
    raise _fail(
        f"source {redact(spec)!r} is not a git or archive source",
    )


def _parse_local(spec: str) -> SourceSpec:
    """Parse an absolute path or ``file://`` URL into a local spec."""
    if spec.lower().startswith("file://"):
        parts = urlsplit(spec)
        if parts.netloc and parts.netloc.lower() != "localhost":
            raise _fail(
                f"file:// source host {parts.netloc!r} is not supported "
                f"(use file:///absolute/path)",
            )
        if parts.query or parts.fragment:
            raise _fail(
                "a file:// source takes no query or fragment",
            )
        raw_path = parts.path
        if not raw_path:
            raise _fail("file:// source has no path")
    else:
        raw_path = spec
    resolved = Path(os.path.expanduser(raw_path))
    if not resolved.is_absolute():  # pragma: no cover — guarded by callers
        raise _fail(f"local source {raw_path!r} is not an absolute path")
    return SourceSpec(kind="local", raw=spec, path=resolved.resolve())


# ── the configured source registry ──────────────────────────────────


@dataclass(frozen=True)
class ConfiguredSource:
    """One entry of the operator's ``packages.sources`` list.

    Attributes:
        name: Alias the operator refers to this source by (and what
            ``packages.search_order`` lists).
        url: A ``git+https://…`` or ``https://…`` source, per the module
            grammar.  Must not contain a ``{name}`` placeholder — a
            monorepo is addressed with ``subdir_template`` instead.
        subdir_template: Optional path template for the package root
            inside the fetched tree, e.g. ``packages/{name}``.  ``None``
            means the tree root IS the package.
        credential_ref: Optional NAME of a platform-provided environment
            variable holding a read token.  A name, never a value; a
            package may not supply it.
    """

    name: str
    url: str
    subdir_template: str | None = None
    credential_ref: str | None = None


_SOURCE_KEYS = frozenset({"name", "url", "subdir_template", "credential_ref"})


def _packages_config(config: dict | None) -> dict:
    """Return the ``packages`` config mapping (never ``None``)."""
    if config is None:
        from ..config import get_config
        config = {"packages": get_config("packages", {}) or {}}
    packages_cfg = config.get("packages", {}) or {}
    if not isinstance(packages_cfg, dict):
        raise SourceSpecError(
            "config 'packages' must be a mapping of package names plus the "
            "'sources' / 'search_order' keys",
        )
    return packages_cfg


def configured_sources(config: dict | None = None) -> tuple[ConfiguredSource, ...]:
    """Parse and validate ``packages.sources``.

    Args:
        config: Optional config mapping to read instead of the live
            platform config (tests pass ``{"packages": {...}}``).

    Returns:
        The declared sources in declaration order.

    Raises:
        SourceSpecError: the list, an entry, or a field is malformed.
            Nothing is returned partially validated — a bad entry is
            refused rather than skipped, so a typo cannot silently
            change which remote a package is installed from.
    """
    raw = _packages_config(config).get("sources", []) or []
    if not isinstance(raw, list):
        raise SourceSpecError(
            "config 'packages.sources' must be a list of "
            "{name, url, subdir_template, credential_ref} entries",
        )
    out: list[ConfiguredSource] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        where = f"packages.sources[{index}]"
        if not isinstance(entry, dict):
            raise SourceSpecError(
                f"{where} must be a mapping, not {type(entry).__name__}",
            )
        unknown = sorted(set(entry) - _SOURCE_KEYS)
        if unknown:
            raise SourceSpecError(
                f"{where} has unknown key(s) {', '.join(unknown)}; "
                f"allowed keys are {', '.join(sorted(_SOURCE_KEYS))}",
            )
        name = entry.get("name")
        if not isinstance(name, str) or not _BARE_NAME_RE.match(name):
            raise SourceSpecError(
                f"{where}.name must be a lowercase alias matching "
                f"{_BARE_NAME_RE.pattern}, got {name!r}",
            )
        if name in seen:
            raise SourceSpecError(
                f"{where}.name {name!r} is declared more than once",
            )
        seen.add(name)
        url = entry.get("url")
        if not isinstance(url, str) or not url.strip():
            raise SourceSpecError(
                f"{where}.url must be a non-empty string",
            )
        url = url.strip()
        if "{" in url or "}" in url:
            raise SourceSpecError(
                f"{where}.url must not contain a placeholder such as "
                f"'{{name}}'; address a monorepo with subdir_template",
            )
        try:
            _parse_remote(url)
        except SourceSpecError as exc:
            raise SourceSpecError(f"{where}.url is invalid: {exc}") from exc
        template = entry.get("subdir_template")
        if template is not None:
            if not isinstance(template, str) or not template.strip():
                raise SourceSpecError(
                    f"{where}.subdir_template must be a non-empty string",
                )
            template = template.strip()
            _validate_subdir_template(template, where=where)
        credential_ref = entry.get("credential_ref")
        if credential_ref is not None:
            if (
                not isinstance(credential_ref, str)
                or not _ENV_IDENT_RE.match(credential_ref)
            ):
                raise SourceSpecError(
                    f"{where}.credential_ref must be the NAME of an "
                    f"environment variable (matching "
                    f"{_ENV_IDENT_RE.pattern}), not a value; got "
                    f"{credential_ref!r}",
                )
        out.append(
            ConfiguredSource(
                name=name, url=url,
                subdir_template=template,
                credential_ref=credential_ref,
            ),
        )
    return tuple(out)


def _validate_subdir_template(template: str, *, where: str) -> None:
    """Validate a ``subdir_template``: only ``{name}``, no escapes."""
    stripped = template.replace("{name}", "")
    if "{" in stripped or "}" in stripped:
        raise SourceSpecError(
            f"{where}.subdir_template may only use the '{{name}}' "
            f"placeholder, got {template!r}",
        )
    try:
        _validate_subdir(template.format(name="_probe_"))
    except SourceSpecError as exc:
        raise SourceSpecError(
            f"{where}.subdir_template is invalid: {exc}",
        ) from exc


def source_search_order(config: dict | None = None) -> tuple[str, ...]:
    """Parse and validate ``packages.search_order``.

    Returns the alias names to try, in order, when a bare package name
    is installed.  Empty means "no configured source is consulted" and a
    bare name keeps the legacy local behaviour.
    """
    packages_cfg = _packages_config(config)
    raw = packages_cfg.get("search_order", []) or []
    if not isinstance(raw, list):
        raise SourceSpecError(
            "config 'packages.search_order' must be a list of source "
            "alias names",
        )
    known = {s.name for s in configured_sources(config)}
    out: list[str] = []
    for index, item in enumerate(raw):
        where = f"packages.search_order[{index}]"
        if not isinstance(item, str) or not item.strip():
            raise SourceSpecError(
                f"{where} must be a non-empty source alias name, got "
                f"{item!r}",
            )
        alias = item.strip()
        if alias not in known:
            raise SourceSpecError(
                f"{where} names source {alias!r}, which is not declared "
                f"in packages.sources ("
                f"{', '.join(sorted(known)) if known else 'no sources declared'})",
            )
        out.append(alias)
    return tuple(out)


def legacy_local_source(name: str) -> SourceSpec:
    """Return the pre-registry local source spec for a bare name.

    ``~/repos/carpenter-packages/packages/<name>`` — the one layout the
    install path knew before a source registry existed.  A bare name
    that matches no configured source resolves here, which is what keeps
    this module behaviour-preserving.
    """
    return SourceSpec(
        kind="local",
        raw=name,
        path=Path(os.path.expanduser(f"{_LEGACY_SOURCE_ROOT}/{name}")),
    )


def resolve_bare_name(
    name: str, *, config: dict | None = None,
) -> tuple[SourceSpec, ...]:
    """Return the configured candidates for a bare package name, in order.

    One spec per alias in ``packages.search_order``, in that order.
    Parsing cannot tell whether a remote actually publishes the package
    (that needs I/O), so the candidates are returned as a tuple and a
    caller that can fetch may try each in turn; :func:`parse_source`
    takes the first.  An empty tuple means no configured source applies
    and the caller should use :func:`legacy_local_source`.
    """
    sources = {s.name: s for s in configured_sources(config)}
    out: list[SourceSpec] = []
    for alias in source_search_order(config):
        entry = sources[alias]
        base = _parse_remote(entry.url)
        subdir = base.subdir
        if entry.subdir_template:
            subdir = _validate_subdir(entry.subdir_template.format(name=name))
        out.append(replace(base, raw=name, subdir=subdir, alias=alias))
    return tuple(out)


# ── the entry point ─────────────────────────────────────────────────


def parse_source(
    spec: str, *, name: str | None = None, config: dict | None = None,
) -> SourceSpec:
    """Parse an operator-supplied source spec into a :class:`SourceSpec`.

    Args:
        spec: The source string: a bare package name, an absolute path
            or ``file://`` URL, a ``git+https://`` repository, or an
            ``https://`` archive.  See the module docstring for the full
            grammar.
        name: Package name to use for a bare-name resolution — it fills
            a configured source's ``{name}`` ``subdir_template`` and the
            legacy local fallback path.  Defaults to ``spec`` itself
            when ``spec`` is a bare name; ignored otherwise.
        config: Optional config mapping to read the source registry from
            instead of the live platform config.

    Returns:
        A canonicalised spec whose ``url`` carries no credential.

    Raises:
        SourceSpecError: the spec is not one of the accepted forms, or
            the configured source registry it resolves through is
            malformed.  The message always states the accepted forms.
    """
    if not isinstance(spec, str) or not spec.strip():
        raise _fail("source spec is empty")
    text = spec.strip()
    lowered = text.lower()

    # Refusals that deserve their own message rather than "not a known
    # form": a reader who typed one of these was close to something that
    # works and should be told which part is the problem.
    if lowered.startswith(("git+ssh://", "ssh://", "git://", "git+git://")):
        raise _fail(
            f"source scheme in {text.split('://')[0]!r}:// is refused — "
            f"ssh and the git protocol are not supported package transports",
        )

    if lowered.startswith("git+") or lowered.startswith(("http://", "https://")):
        return _parse_remote(text)

    if lowered.startswith("file://") or text.startswith(("/", "~")):
        return _parse_local(text)

    if _BARE_NAME_RE.match(text):
        pkg_name = name or text
        candidates = resolve_bare_name(pkg_name, config=config)
        if candidates:
            first = candidates[0]
            return first if first.raw == text else replace(first, raw=text)
        return legacy_local_source(pkg_name)

    raise _fail(f"source {redact(text)!r} is not a recognised source")

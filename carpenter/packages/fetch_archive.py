"""Fetching a capability package from a plain archive download URL.

This is the ``archive`` half of the fetch contract in
:mod:`carpenter.packages.fetch`: a release asset, or any ``.tar.gz`` /
``.tgz`` / ``.tar`` reachable over HTTP(S), streamed into the
dispatcher's quarantine directory and expanded with the hardened
extractor the reconcile cache already uses.  There is deliberately
nothing forge-specific here — a release asset *is* an archive URL, so a
self-hosted forge, an object store and a plain web server are all the
same code path.

What this module is careful about, and why:

* **Streamed, never slurped.** The body is read in chunks and written
  straight to disk while a running sha256 is updated, so an attacker
  cannot make the platform allocate the archive in memory.  The byte cap
  is checked on every chunk and the connection is dropped the moment it
  is passed — a 10 GiB body costs the cap plus one chunk, not 10 GiB.
* **Redirects are followed by hand.** The dispatcher runs
  :func:`carpenter.security.egress.check_url` on the *initial* URL only.
  A 302 from an allowed host to ``127.0.0.1``, ``169.254.169.254`` or a
  private range would otherwise walk straight through that gate, so each
  hop goes past :func:`carpenter.security.egress.check_redirect` before
  it is requested, exactly as ``carpenter/tool_backends/web.py`` does.
* **Integrity is reported whether or not it was requested.** The sha256
  of the downloaded bytes is always returned as
  :attr:`~carpenter.packages.fetch.FetchResult.resolved`; when the source
  carried no ``#sha256=`` pin, that digest is also logged at WARNING so
  an operator can paste it back into the source and pin it.
* **A small archive may declare enormous members — or enormous numbers
  of them.** The tar index is inspected *before* extraction: member
  count, each member's declared size and their total are checked against
  the limits, so a decompression bomb is refused without a byte of it
  reaching the disk.  The index is walked one member at a time and the
  first cap passed stops the walk, because reading the whole index is
  itself the bomb when an archive declares millions of members.  What
  actually landed is then re-measured, because the index is
  attacker-supplied too.
* **The ambient environment cannot redirect the transport.** The
  client runs with ``trust_env=False``, so a proxy variable in the
  platform process's environment cannot send the request somewhere the
  egress guard never resolved.  What the gate checked is what gets
  connected to.
* **Credentials go to one origin and are never recorded.** A source that
  resolved through a configured entry with a ``credential_ref`` names an
  environment *variable*; its value is sent as a bearer token to that
  origin and dropped on any redirect that leaves it.  The value never
  reaches a log line, an exception message or the install record.
* **Only tar is handled.** A zip is refused by name and by magic bytes
  with a message that says so, rather than half-handled.
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import ssl
import tarfile
import time
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

import httpx

from ..security import egress

# The hardened extractor and its refusal type.  Private by name because
# nothing outside the package tree should be extracting archives; this
# module is inside that tree, and a second extractor is exactly what the
# design forbids.
from .archive_cache import ArchiveVerificationError, _safe_extract
from .fetch import FetchError, FetchLimits, FetchResult, register_fetcher
from .sources import SourceSpec, SourceSpecError, configured_sources, redact

logger = logging.getLogger(__name__)

__all__ = ["ArchiveUrlFetcher"]

#: Redirect hop limit.  Matches ``tool_backends/web.py`` so the platform
#: has one answer to "how many hops is too many".
MAX_REDIRECTS = 10

#: Read size for the streamed body.  Also the granularity of the byte
#: cap: a fetch may overshoot ``max_bytes`` by at most one chunk before
#: the connection is dropped.
_CHUNK_BYTES = 64 * 1024

_MANIFEST = "manifest.yaml"
_DOWNLOAD_DIR = "_download"
_ARCHIVE_NAME = "archive"
_TREE_DIR = "_tree"
_ZIP_MAGIC = b"PK\x03\x04"
_DEFAULT_PORTS = {"http": 80, "https": 443}
_USER_AGENT = "Carpenter/1.0 (package fetcher)"
_ACCEPT = "application/gzip, application/x-gzip, application/octet-stream, */*"

#: How many names a "what I found instead" message lists before it
#: stops.  Enough to recognise a tree, short enough for one log line.
_LISTING_LIMIT = 20

_ZIP_REFUSAL = (
    "zip archives are not supported; publish the package as a .tar.gz "
    "(.tgz and .tar are also accepted)"
)


# ── small helpers ───────────────────────────────────────────────────


def _origin(url: str) -> tuple[str, str, int | None]:
    """Return ``(scheme, host, port)`` — the identity a credential is for.

    Comparing the whole origin rather than only the host is deliberate:
    a redirect that keeps the host but changes the scheme or the port is
    a different service, and an ``https`` → ``http`` hop would put the
    token on the wire in clear.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        port = parts.port
    except ValueError:  # pragma: no cover — defensive, httpx would refuse
        port = None
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    return (scheme, host, port)


def _elapsed_failure(limits: FetchLimits, started: float) -> FetchError:
    """The timeout refusal, naming the limit and what was observed."""
    return FetchError(
        f"fetching the package archive exceeded its wall-clock budget: "
        f"timeout_s={limits.timeout_s}, "
        f"elapsed={time.monotonic() - started:.1f}s",
    )


def _remaining(deadline: float, limits: FetchLimits, started: float) -> float:
    """Seconds of budget left, or raise if the budget is spent."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise _elapsed_failure(limits, started)
    return left


def _causes(exc: BaseException) -> list[BaseException]:
    """Return ``exc`` and its cause/context chain (bounded)."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < 10:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _listing(directory: Path) -> str:
    """Describe a directory's immediate children for an error message."""
    try:
        names = sorted(
            child.name + ("/" if child.is_dir() else "")
            for child in directory.iterdir()
        )
    except OSError:  # pragma: no cover — defensive
        return "(unreadable)"
    if not names:
        return "(nothing)"
    if len(names) > _LISTING_LIMIT:
        extra = len(names) - _LISTING_LIMIT
        return ", ".join(names[:_LISTING_LIMIT]) + f", … (+{extra} more)"
    return ", ".join(names)


def _is_within(base: Path, target: Path) -> bool:
    """True if ``target`` is ``base`` or lives under it."""
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


# ── credentials: a variable name in, a bearer token out ─────────────


def _credential_variable(spec: SourceSpec) -> str | None:
    """Return the env var name this source's token comes from, if any.

    A spec only carries the *alias* of the configured source it resolved
    through; the ``credential_ref`` lives on the operator's registry
    entry, which is read fresh here so a token is never cached on a spec
    that gets logged or stored.  A spec the operator wrote out in full
    has no alias and therefore no credential: a URL typed by hand is
    never silently authenticated.
    """
    if not spec.alias:
        return None
    try:
        sources = configured_sources()
    except SourceSpecError as exc:
        raise FetchError(
            f"cannot fetch package source {spec.raw!r}: the configured "
            f"package_sources registry it came from is invalid ({exc})",
        ) from exc
    for source in sources:
        if source.name == spec.alias:
            return source.credential_ref
    # The alias was valid when the spec was parsed and is not now.  Carry
    # on anonymously rather than guessing: an authenticated source will
    # answer 401 and that message says exactly what to do.
    logger.warning(
        "package source alias %r is no longer in package_sources; "
        "fetching %s without credentials",
        spec.alias, redact(spec.url or ""),
    )
    return None


def _bearer_token(variable: str) -> str:
    """Read ``variable`` from the environment, or refuse by name.

    The refusal names the *variable*, never its value — this is the one
    place a missing platform secret is diagnosed, so it has to be
    actionable without being a leak.
    """
    value = os.environ.get(variable) or ""
    if not value.strip():
        raise FetchError(
            f"package source credential environment variable {variable!r} "
            f"is unset or empty. The platform supplies this token, not the "
            f"package: set {variable} in the platform environment, or drop "
            f"credential_ref from the package_sources entry to fetch "
            f"anonymously.",
        )
    return value.strip()


# ── failure translation: one distinct message per cause ─────────────


def _looks_like_tls(chain: list[BaseException]) -> bool:
    """True if a connect failure is really a certificate/TLS failure."""
    if any(isinstance(exc, ssl.SSLError) for exc in chain):
        return True
    text = " ".join(str(exc).lower() for exc in chain)
    return any(
        token in text
        for token in ("certificate", "ssl", "tlsv1", "handshake")
    )


def _looks_like_dns(chain: list[BaseException]) -> bool:
    """True if a connect failure is really a name-resolution failure."""
    if any(isinstance(exc, socket.gaierror) for exc in chain):
        return True
    text = " ".join(str(exc).lower() for exc in chain)
    return any(
        token in text
        for token in (
            "name or service not known",
            "nodename nor servname",
            "temporary failure in name resolution",
            "getaddrinfo",
            "name resolution",
        )
    )


def _transport_failure(
    exc: Exception, url: str, limits: FetchLimits,
) -> FetchError:
    """Map an httpx failure to one distinct, operator-facing message.

    Every branch names the archive (redacted) and what the operator can
    do about it.  None of them interpolate a header, so a bearer token
    cannot reach an error message by this route.
    """
    where = redact(url)
    if isinstance(exc, httpx.TimeoutException):
        return FetchError(
            f"timed out fetching package archive {where}: the server did "
            f"not respond within the fetch budget (timeout_s="
            f"{limits.timeout_s}). Raise the timeout or check that the "
            f"host is reachable from the platform.",
        )
    if isinstance(exc, httpx.RemoteProtocolError):
        return FetchError(
            f"package archive download from {where} was truncated: the "
            f"server closed the connection before sending the whole body "
            f"({exc}). Retry; if it repeats, the asset on the server is "
            f"probably incomplete.",
        )
    if isinstance(exc, httpx.ConnectError):
        chain = _causes(exc)
        if _looks_like_tls(chain):
            return FetchError(
                f"TLS verification failed for package archive {where}: "
                f"{exc}. The platform will not fetch over an untrusted "
                f"connection — install the forge's CA certificate, or fix "
                f"the server's certificate chain.",
            )
        if _looks_like_dns(chain):
            return FetchError(
                f"could not resolve the host of package archive {where}: "
                f"{exc}. Check the host name in the source URL and the "
                f"platform's DNS.",
            )
        return FetchError(
            f"could not connect to the host serving package archive "
            f"{where}: {exc}. Check that the host is up and reachable "
            f"from the platform.",
        )
    if isinstance(exc, httpx.ReadError):
        return FetchError(
            f"package archive download from {where} failed mid-body "
            f"({exc}); the transfer was incomplete.",
        )
    return FetchError(
        f"could not fetch package archive {where}: {type(exc).__name__}: "
        f"{exc}",
    )


def _check_status(
    status: int, reason: str, url: str, variable: str | None,
) -> None:
    """Refuse a non-2xx response with a message particular to its code."""
    if status < 400:
        return
    where = redact(url)
    if status == 404:
        raise FetchError(
            f"package archive not found: the server returned 404 for "
            f"{where}. Check the file name and version in the source URL — "
            f"a release asset that was renamed or never published looks "
            f"exactly like this.",
        )
    if status in (401, 403):
        if variable:
            hint = (
                f"The token in {variable} was sent but rejected: check that "
                f"it is current and has read access to this asset."
            )
        else:
            hint = (
                "No credential was sent. If this source is private, add a "
                "package_sources entry for it with a credential_ref naming "
                "the environment variable that holds a read token."
            )
        raise FetchError(
            f"access to package archive {where} was refused (HTTP {status} "
            f"{reason}). {hint}",
        )
    if status == 429:
        raise FetchError(
            f"the host serving package archive {where} is rate-limiting "
            f"the platform (HTTP 429). Retry later.",
        )
    if status >= 500:
        raise FetchError(
            f"the host serving package archive {where} failed with HTTP "
            f"{status} {reason}; this is the server's fault, not the "
            f"source's. Retry later.",
        )
    raise FetchError(
        f"unexpected HTTP {status} {reason} fetching package archive "
        f"{where}.",
    )


# ── shape checks ────────────────────────────────────────────────────


def _refuse_zip_url(url: str) -> None:
    """Refuse a ``.zip`` source by name, before any request is made."""
    path = (urlsplit(url).path or "").lower()
    if path.endswith(".zip"):
        raise FetchError(
            f"package source {redact(url)} names a .zip archive: "
            f"{_ZIP_REFUSAL}",
        )


def _refuse_zip_bytes(archive_path: Path, url: str) -> None:
    """Refuse a body that is a zip whatever the URL claimed."""
    with open(archive_path, "rb") as handle:
        magic = handle.read(len(_ZIP_MAGIC))
    if magic == _ZIP_MAGIC:
        raise FetchError(
            f"the body served for package archive {redact(url)} is a zip "
            f"archive, whatever its file name says: {_ZIP_REFUSAL}",
        )


def _verify_digest(spec: SourceSpec, actual: str, url: str) -> None:
    """Check the download against ``spec.digest``, or warn it was unpinned."""
    where = redact(url)
    if spec.digest:
        if actual != spec.digest:
            raise FetchError(
                f"package archive {where} failed its sha256 check: "
                f"expected {spec.digest}, got {actual}. The bytes at that "
                f"URL are not the bytes that were pinned — refusing to "
                f"install them.",
            )
        return
    logger.warning(
        "package archive %s was fetched UNPINNED: the source carries no "
        "#sha256= digest, so nothing proves these are the intended bytes. "
        "Its sha256 is %s — append '#sha256=%s' to the source to pin it.",
        where, actual, actual,
    )


# ── caps over the tar index and over what landed ────────────────────


def _accepted_members(
    tar: tarfile.TarFile, limits: FetchLimits, url: str,
) -> list[tarfile.TarInfo]:
    """Walk the index one member at a time; return what may be extracted.

    This runs before a single member is written, which is the whole
    point: a few hundred kilobytes of gzip can declare gigabytes of
    content, and discovering that by filling the disk is not a defence.

    **The walk is lazy, and that is a limit of its own.**  The index is
    attacker-supplied in its *length* as much as its contents:
    ``getmembers()`` builds one ``TarInfo`` for every member before any
    cap is consulted, and at a high compression ratio an archive inside
    the download cap can declare tens of millions of them — enough
    resident memory to take the platform down on a single pre-consent
    request.  So each member is pulled individually and the first cap
    passed aborts the walk, which bounds it to ``max_files`` entries
    whatever the archive claims to hold.

    Passing here is necessary, not sufficient: the index can lie about
    sizes, so :func:`_measure_extracted` re-checks what actually landed.
    """
    where = redact(url)
    members: list[tarfile.TarInfo] = []
    declared_total = 0
    for member in tar:
        members.append(member)
        if len(members) > limits.max_files:
            raise FetchError(
                f"package archive {where} declares more than "
                f"{limits.max_files} entries, over the max_files limit of "
                f"{limits.max_files}; refusing to expand it",
            )
        if not member.isfile():
            continue
        size = int(member.size)
        if size > limits.max_file_bytes:
            raise FetchError(
                f"package archive {where} declares member {member.name!r} "
                f"as {size} bytes, over the max_file_bytes limit of "
                f"{limits.max_file_bytes}; refusing to expand it",
            )
        declared_total += size
        if declared_total > limits.max_bytes:
            raise FetchError(
                f"package archive {where} declares {declared_total} bytes of "
                f"file content, over the max_bytes limit of "
                f"{limits.max_bytes}; refusing to expand it",
            )
    return members


def _measure_extracted(
    root: Path, limits: FetchLimits, url: str,
) -> tuple[int, int]:
    """Count and size what actually landed, re-checking every limit.

    Returns ``(files, bytes)``.  :func:`_safe_extract` has already
    guaranteed there are no symlinks, hardlinks or special files in the
    tree, so ``stat`` here cannot be redirected anywhere.
    """
    where = redact(url)
    files = 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            files += 1
            if files > limits.max_files:
                raise FetchError(
                    f"package archive {where} expanded to more than "
                    f"{limits.max_files} files (max_files)",
                )
            size = path.stat().st_size
            if size > limits.max_file_bytes:
                rel = path.relative_to(root).as_posix()
                raise FetchError(
                    f"package archive {where} expanded {rel!r} to {size} "
                    f"bytes, over the max_file_bytes limit of "
                    f"{limits.max_file_bytes}",
                )
            total += size
            if total > limits.max_bytes:
                raise FetchError(
                    f"package archive {where} expanded to over "
                    f"{limits.max_bytes} bytes (max_bytes); {total} bytes "
                    f"written so far",
                )
    return files, total


# ── layout: which directory inside the archive is the package ───────


def _package_root(
    extract_root: Path,
    spec: SourceSpec,
    url: str,
    *,
    require_manifest: bool = True,
) -> Path:
    """Resolve the package root inside an expanded archive.

    Two layouts are accepted, because both are published in the wild:
    the archive root *is* the package (``manifest.yaml`` at the top
    level), or the archive holds exactly one top-level directory which
    is the package (what every forge's "download this tag" tarball
    looks like).  ``spec.subdir`` is then applied relative to whichever
    of those was found, so a monorepo published either way addresses its
    packages the same way.

    With ``require_manifest=False`` the same walk happens — including
    the containment check on ``spec.subdir`` and the "does that
    directory exist" check — and only the final "and it holds a
    manifest" verdict is dropped, so a listing can be handed a
    monorepo's index root.
    """
    where = redact(url)
    base = extract_root
    if not (extract_root / _MANIFEST).is_file():
        children = list(extract_root.iterdir())
        directories = [child for child in children if child.is_dir()]
        if len(children) == 1 and len(directories) == 1:
            base = directories[0]

    tree = base
    if spec.subdir:
        tree = (base / spec.subdir).resolve()
        if not _is_within(extract_root.resolve(), tree):
            raise FetchError(
                f"package archive {where}: subdir {spec.subdir!r} points "
                f"outside the expanded archive; refusing it",
            )
        if not tree.is_dir():
            raise FetchError(
                f"package archive {where} has no directory "
                f"{spec.subdir!r}; it contains: {_listing(base)}",
            )

    if (tree / _MANIFEST).is_file() or not require_manifest:
        return tree

    if spec.subdir:
        raise FetchError(
            f"package archive {where}: subdir {spec.subdir!r} holds no "
            f"{_MANIFEST}, so it is not a capability package; it contains: "
            f"{_listing(tree)}",
        )
    if base != extract_root:
        raise FetchError(
            f"package archive {where} is not a capability package: its "
            f"single top-level directory {base.name!r} holds no "
            f"{_MANIFEST}; it contains: {_listing(base)}",
        )
    raise FetchError(
        f"package archive {where} is not a capability package: no "
        f"{_MANIFEST} at the archive root, and the root is not a single "
        f"directory containing one. It holds: {_listing(extract_root)}",
    )


# ── the fetcher ─────────────────────────────────────────────────────


class ArchiveUrlFetcher:
    """Fetches an ``archive`` source: one HTTP(S) GET of a tar archive.

    Registered for the ``archive`` kind through
    :func:`~carpenter.packages.fetch.register_fetcher`, so
    :func:`~carpenter.packages.fetch.fetch_source` reaches it with the
    initial URL already past the egress gate and ``dest`` already a
    fresh ``0700`` quarantine.  Everything after that — redirects,
    caps, integrity, extraction and layout — is this class's business.
    """

    kinds: ClassVar[frozenset[str]] = frozenset({"archive"})

    def fetch(
        self,
        spec: SourceSpec,
        dest: Path,
        *,
        limits: FetchLimits,
        require_manifest: bool = True,
    ) -> FetchResult:
        """Download, verify and expand ``spec`` inside ``dest``.

        Args:
            spec: An ``archive`` source spec.  ``spec.digest``, when
                set, is the expected sha256 of the downloaded bytes;
                ``spec.subdir`` selects the package root inside the
                archive; ``spec.alias`` is how a configured source's
                ``credential_ref`` is found.
            dest: The quarantine directory.  The download lands in
                ``dest/_download`` (and is deleted once expanded) and
                the tree in ``dest/_tree``.
            limits: Byte, file-count, per-file and wall-clock ceilings,
                all enforced here.
            require_manifest: When ``False``, the resolved directory is
                returned even if it holds no ``manifest.yaml`` — a
                monorepo's index root, for
                :mod:`carpenter.packages.discover`.  Nothing else
                changes: the redirect re-checks, the digest
                verification, every cap and the member hardening all
                run exactly as they do for an install.

        Returns:
            A :class:`~carpenter.packages.fetch.FetchResult` whose
            ``resolved`` is the sha256 of the downloaded bytes, pinned
            or not.

        Raises:
            FetchError: for every failure — a refused destination, a
                breached limit, a digest mismatch, an unsupported
                archive format, an unsafe member, a missing manifest, or
                any HTTP/transport error.  The message names the
                problem and, where there is one, the operator action.
        """
        started = time.monotonic()
        deadline = started + max(float(limits.timeout_s), 0.0)

        if spec.kind != "archive":
            raise FetchError(
                f"{type(self).__name__} handles 'archive' sources, not "
                f"{spec.kind!r} ({spec.raw!r})",
            )
        url = spec.url or ""
        if not url:
            raise FetchError(
                f"archive source {spec.raw!r} has no URL to fetch",
            )
        _refuse_zip_url(url)

        dest = Path(dest)
        archive_path = dest / _DOWNLOAD_DIR / _ARCHIVE_NAME
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        extract_root = dest / _TREE_DIR
        extract_root.mkdir(parents=True, exist_ok=True)

        variable = _credential_variable(spec)
        digest, bytes_fetched = self._download(
            spec, archive_path,
            limits=limits, deadline=deadline, started=started,
            variable=variable,
        )
        _remaining(deadline, limits, started)
        _verify_digest(spec, digest, url)
        _refuse_zip_bytes(archive_path, url)

        self._expand(archive_path, extract_root, limits=limits, url=url)
        # One copy of the bytes is enough: the quarantine is what the
        # caller installs from, and the digest is already computed.
        archive_path.unlink(missing_ok=True)
        _remaining(deadline, limits, started)

        files_fetched, expanded = _measure_extracted(
            extract_root, limits, url,
        )
        tree = _package_root(
            extract_root, spec, url, require_manifest=require_manifest,
        )
        logger.info(
            "Fetched package archive %s: %d bytes transferred, %d files "
            "expanded (%d bytes), sha256 %s",
            redact(url), bytes_fetched, files_fetched, expanded, digest,
        )
        return FetchResult(
            tree=tree,
            staging=dest,
            resolved=digest,
            bytes_fetched=bytes_fetched,
            files_fetched=files_fetched,
        )

    # ── transport ────────────────────────────────────────────────────

    def _download(
        self,
        spec: SourceSpec,
        archive_path: Path,
        *,
        limits: FetchLimits,
        deadline: float,
        started: float,
        variable: str | None,
    ) -> tuple[str, int]:
        """Stream the archive to ``archive_path``, following redirects by hand.

        Each hop's response is inspected by
        :func:`carpenter.security.egress.check_redirect` *before* the
        next request is issued, so an allowed host cannot bounce the
        platform onto loopback, a link-local address or a cloud
        metadata endpoint.  The hop limit is that function's too.

        Returns:
            ``(sha256_hex, bytes_written)``.
        """
        url = spec.url or ""
        headers = {"User-Agent": _USER_AGENT, "Accept": _ACCEPT}
        credential_origin: tuple[str, str, int | None] | None = None
        if variable:
            headers["Authorization"] = f"Bearer {_bearer_token(variable)}"
            credential_origin = _origin(url)

        hops = 0
        while True:
            timeout = httpx.Timeout(_remaining(deadline, limits, started))
            try:
                with httpx.stream(
                    "GET", url,
                    headers=headers, follow_redirects=False, timeout=timeout,
                    # Not the ambient environment's business where a
                    # package comes from: ``trust_env`` would let
                    # ``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY`` send
                    # this request to a host the egress guard never
                    # resolved and never checked.
                    trust_env=False,
                ) as response:
                    try:
                        target = egress.check_redirect(
                            response, MAX_REDIRECTS, hops,
                        )
                    except egress.EgressDenied as exc:
                        raise FetchError(
                            f"refusing to continue the redirect chain for "
                            f"package archive {redact(spec.url or '')}: "
                            f"{exc}. A redirect may not take the platform "
                            f"to a destination the egress guard would not "
                            f"have accepted in the first place.",
                        ) from exc
                    if target is None:
                        _check_status(
                            response.status_code,
                            response.reason_phrase or "",
                            url, variable,
                        )
                        return self._consume(
                            response, archive_path,
                            limits=limits, deadline=deadline,
                            started=started, url=url,
                        )
            except httpx.HTTPError as exc:
                raise _transport_failure(exc, url, limits) from exc

            hops += 1
            if (
                credential_origin is not None
                and _origin(target) != credential_origin
            ):
                # The classic credential-leak bug: a 302 to another
                # origin must not carry the bearer token with it.  Once
                # dropped it is not restored, even by a hop back.
                headers.pop("Authorization", None)
                credential_origin = None
                logger.info(
                    "Dropped the Authorization header while following a "
                    "package archive redirect to a different origin",
                )
            url = target

    def _consume(
        self,
        response: httpx.Response,
        archive_path: Path,
        *,
        limits: FetchLimits,
        deadline: float,
        started: float,
        url: str,
    ) -> tuple[str, int]:
        """Write the streamed body to disk under the byte and time caps.

        The cap is checked per chunk and raises from inside the
        streaming context manager, so httpx closes the connection
        there and then — the rest of the body is never transferred.
        """
        where = redact(url)
        declared = self._declared_length(response)
        if declared is not None and declared > limits.max_bytes:
            raise FetchError(
                f"package archive {where} declares a Content-Length of "
                f"{declared} bytes, over the max_bytes limit of "
                f"{limits.max_bytes}; refusing to download it",
            )

        digest = hashlib.sha256()
        total = 0
        with open(archive_path, "wb") as handle:
            for chunk in response.iter_bytes(_CHUNK_BYTES):
                total += len(chunk)
                if total > limits.max_bytes:
                    raise FetchError(
                        f"package archive download from {where} exceeded "
                        f"the max_bytes limit of {limits.max_bytes}: "
                        f"aborted after {total} bytes",
                    )
                if time.monotonic() > deadline:
                    raise _elapsed_failure(limits, started)
                digest.update(chunk)
                handle.write(chunk)

        if declared is not None and declared != total:
            raise FetchError(
                f"package archive download from {where} was truncated: the "
                f"server declared {declared} bytes but sent {total}.",
            )
        if total == 0:
            raise FetchError(
                f"the host serving package archive {where} returned an "
                f"empty body; there is no archive at that URL.",
            )
        return digest.hexdigest(), total

    @staticmethod
    def _declared_length(response: httpx.Response) -> int | None:
        """The response's ``Content-Length``, if it stated a sane one."""
        raw = response.headers.get("content-length")
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    # ── extraction ───────────────────────────────────────────────────

    @staticmethod
    def _expand(
        archive_path: Path,
        extract_root: Path,
        *,
        limits: FetchLimits,
        url: str,
    ) -> None:
        """Vet the tar index, then extract with the hardened extractor.

        No second extractor: member safety is
        :func:`carpenter.packages.archive_cache._safe_extract`'s job and
        its refusal is re-raised as a :class:`FetchError` with the cause
        chained, so a caller can still see which member it was.
        """
        where = redact(url)
        try:
            with tarfile.open(str(archive_path), mode="r:*") as tar:
                members = _accepted_members(tar, limits, url)
                _safe_extract(tar, extract_root, members=members)
        except ArchiveVerificationError as exc:
            raise FetchError(
                f"package archive {where} was refused by the archive "
                f"safety check: {exc}",
            ) from exc
        except tarfile.TarError as exc:
            raise FetchError(
                f"package archive {where} could not be read as a tar "
                f"archive ({exc}); only .tar.gz, .tgz and .tar are "
                f"supported.",
            ) from exc
        except OSError as exc:
            raise FetchError(
                f"package archive {where} could not be expanded: {exc}",
            ) from exc


register_fetcher(ArchiveUrlFetcher())

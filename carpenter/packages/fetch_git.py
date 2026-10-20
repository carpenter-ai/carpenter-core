"""Fetching a capability package's tree from a git repository.

The fetcher registered here handles ``kind="git"`` source specs — the
``git+https://host/org/repo.git[@ref][#subdir=rel/path]`` form that
:mod:`carpenter.packages.sources` parses — and hands
:mod:`carpenter.packages.fetch`'s dispatcher a materialised package
directory inside the quarantine it was given.

**dulwich only: the git binary is never invoked.**  Nothing in this
repository shells out to ``git``, and for a package fetch that is a
security property rather than a style preference: ``git`` runs hooks and
honours in-repository configuration, so a clone of an attacker-controlled
repository can execute the attacker's code on the platform host before
anything has been reviewed.  dulwich is a pure-Python implementation that
runs no hooks and no filters, so fetching a package executes none of the
remote's code.  For the same reason the clone is given an **empty** git
configuration: the host user's ``~/.gitconfig`` must not be able to
rewrite the URL (``url.<base>.insteadOf``) after the dispatcher has
already put it past the egress gate, nor point the transport at a proxy.
The transport is built here rather than by dulwich's
``default_urllib3_manager`` for the same reason: that helper reads
``http_proxy``/``https_proxy``/``all_proxy`` out of the platform's
environment, and a proxy is a destination the gate never checked (see
:func:`_pool_manager`).

**Redirects are driven by hand, through the egress guard.**  urllib3
follows a redirect chain inside one ``request()`` call, so the gate the
dispatcher ran over the clone URL would otherwise see hop 0 only.  The
pool manager built by :func:`_gated_pool_manager_class` re-checks every
hop with :func:`carpenter.security.egress.check_url`, caps the chain at
:data:`MAX_REDIRECTS`, and refuses any hop that leaves the scheme, host
and port the request named — so the provenance recorded for the fetch is
the origin the bytes really came from.

What the fetch does, in order:

1. Resolve the credential, if the source was configured with one, from
   the environment variable the source names (never from the package,
   never from the URL).
2. Clone into ``<dest>/_clone`` with ``checkout=False`` and, where the
   ref allows it, ``depth=1``.  No working tree is written, so no file
   from the remote touches the disk outside the object store yet.
3. Resolve the ref to a concrete commit sha40, peeling an annotated tag.
4. Refuse submodules, then materialise *only* the package subtree into
   ``<dest>/tree/<subdir>``, checking every entry's mode and path and
   counting bytes and files against :class:`~.fetch.FetchLimits` as it
   writes.
5. Harden the materialised tree (symlink escapes, hard links, anything
   that is not a regular file or a directory), then delete the clone —
   ``.git`` included — so the installed tree is package bytes only and
   its hash depends on nothing but those bytes.

Anything that fails raises :class:`~.fetch.FetchError` and nothing
partial is returned; the dispatcher removes the quarantine.
"""

from __future__ import annotations

import io
import logging
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import ClassVar
from urllib.parse import urljoin, urlsplit

from ..security import egress
from .fetch import FetchError, FetchLimits, FetchResult, register_fetcher
from .sources import SourceSpec, SourceSpecError, configured_sources, redact

logger = logging.getLogger(__name__)

__all__ = ["GitFetcher"]

#: A fully qualified commit id as the grammar accepts it.
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")

#: Child of the quarantine that holds the bare-ish clone (object store
#: and refs, no working tree).  Removed before the fetch returns.
_CLONE_DIR_NAME = "_clone"

#: Child of the quarantine the package tree is materialised into.  The
#: returned tree is this directory plus ``spec.subdir``.
_TREE_DIR_NAME = "tree"

#: How often the fetch's wall-clock and disk budget are re-checked while
#: the clone runs in its worker thread.
_POLL_INTERVAL_S = 0.05

#: How many materialised files between two deadline checks.
_DEADLINE_EVERY = 64

# Git tree entry modes.  Anything else — a gitlink (submodule) included —
# is refused rather than guessed at.
_MODE_TREE = 0o040000
_MODE_BLOB = 0o100644
_MODE_BLOB_EXEC = 0o100755
_MODE_SYMLINK = 0o120000
_MODE_GITLINK = 0o160000

#: Path components a fetched tree may never contain.  ``.git`` would make
#: the installed tree a repository (and, with a crafted config, one that
#: runs code); the rest cannot be written as a relative path at all.
_FORBIDDEN_COMPONENTS = frozenset({"", ".", "..", ".git"})


#: Redirect hop limit for the clone transport.  Same answer as
#: :data:`carpenter.packages.fetch_archive.MAX_REDIRECTS` and
#: ``tool_backends/web.py``, so the platform has one.
MAX_REDIRECTS = 10

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int | None]:
    """Return ``(scheme, host, port)`` with the default port filled in.

    The identity a fetch is allowed to stay on.  Comparing the whole
    origin rather than only the host is deliberate: a redirect that
    keeps the host but changes the scheme or the port is a different
    service, and an ``https`` -> ``http`` hop would put the clone (and
    the credential, if the transport ever stopped stripping it) on the
    wire in clear.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        port = parts.port
    except ValueError:  # pragma: no cover — defensive
        port = None
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    return (scheme, host, port)


def _gated_pool_manager_class(base_url: str):
    """Build a ``PoolManager`` subclass that follows no unchecked hop.

    The dispatcher (:func:`carpenter.packages.fetch.fetch_source`) puts
    the clone URL past :func:`carpenter.security.egress.check_url` once.
    urllib3 follows redirects *inside* one ``request()`` call, so without
    this a ``307`` from an allowed host walks the platform onto loopback,
    a link-local address or a cloud metadata endpoint, and dulwich only
    notices afterwards.  :mod:`carpenter.packages.fetch`'s docstring
    calls re-checking every hop mandatory and
    :mod:`carpenter.packages.fetch_archive` already does it; this is the
    git transport's half.

    Two rules, both enforced here because this is the only place that
    sees a hop before it is requested:

    * **Every hop goes past the egress guard**, exactly as the archive
      fetcher's hand-rolled redirect loop does.
    * **A hop may not leave the origin the operator consented to.**  A
      package source that moves host mid-fetch is not a case this
      platform serves, and refusing it keeps the recorded provenance
      honest: the host on the approval page, in the ``source_url``
      column and in the audit log is then necessarily the host the bytes
      came from.  Following a cross-host hop and *relabelling* the
      provenance would be the alternative, but attribution is the
      design's primary mitigation (WP5-plan 8.2) and a source whose host
      is not the one the operator read is not worth the complexity.

    Only ``GET``/``HEAD`` hops are followed at all: dulwich POSTs the
    pack negotiation with a generator body that cannot be replayed, and
    a redirect that silently dropped it would be worse than a refusal.
    """
    import urllib3

    allowed = _origin(base_url)

    class _GatedPoolManager(urllib3.PoolManager):
        """A pool manager that drives redirects by hand, through the gate."""

        def urlopen(self, method, url, redirect=True, **kw):  # noqa: D102
            hops = 0
            current = url
            while True:
                response = super().urlopen(
                    method, current, redirect=False, **kw,
                )
                location = (
                    response.get_redirect_location() if redirect else None
                )
                if not location:
                    if hops:
                        # dulwich reads ``geturl()`` to notice that the
                        # repository moved and to rebase the next
                        # request; it must see where we ended up.
                        try:
                            response.url = current
                        except NotImplementedError:  # pragma: no cover
                            pass
                    return response
                target = urljoin(current, location)
                response.drain_conn()
                if hops >= MAX_REDIRECTS:
                    raise FetchError(
                        f"the git source {redact(url)} redirected more than "
                        f"{MAX_REDIRECTS} times; refusing to follow the "
                        f"chain any further. Nothing was fetched.",
                    )
                if str(method).upper() not in ("GET", "HEAD"):
                    raise FetchError(
                        f"the git source {redact(url)} answered a "
                        f"{str(method).upper()} with a redirect; a package "
                        f"fetch does not replay a request body onto another "
                        f"URL. Nothing was fetched.",
                    )
                if _origin(target) != allowed:
                    logger.warning(
                        "Refused a cross-origin redirect while fetching a "
                        "package source: %s -> %s",
                        redact(current), redact(target),
                    )
                    raise FetchError(
                        f"the git source {redact(url)} redirected to a "
                        f"different scheme, host or port "
                        f"({redact(target)}); a package fetch follows a "
                        f"redirect only within the origin the request named, "
                        f"so that the host shown for approval and recorded "
                        f"as provenance is the host the bytes came from. "
                        f"Name the final URL as the source instead.",
                    )
                try:
                    egress.check_url(target)
                except egress.EgressDenied as exc:
                    raise FetchError(
                        f"refusing to follow a redirect while fetching the "
                        f"git source {redact(url)}: {exc} A redirect may "
                        f"not take the platform to a destination the egress "
                        f"guard would not have accepted in the first place.",
                    ) from exc
                current = target
                hops += 1

    return _GatedPoolManager


def _pool_manager(url: str, timeout: float):
    """Build the clone's HTTP transport, ignoring the ambient environment.

    Deliberately NOT ``dulwich.client.default_urllib3_manager``: that
    reads ``https_proxy`` / ``http_proxy`` / ``all_proxy`` from the
    platform process's environment even when it is handed an empty git
    configuration, and a proxy is a destination the egress guard never
    resolved and never checked.  The guard's whole claim is that the host
    it inspected is the host that gets connected to, so the fetcher
    builds its own pool manager and sends the request where the URL says.
    An operator who needs a proxy for package fetches has to say so in
    code; there is no silent environment route around the gate.

    Everything else matches what ``default_urllib3_manager`` would have
    produced for ``config=None``: dulwich's user agent, certificate
    verification required, and one socket timeout for the whole budget.
    """
    from dulwich.client import default_user_agent_string

    manager_cls = _gated_pool_manager_class(url)
    return manager_cls(
        headers={"User-agent": default_user_agent_string()},
        timeout=timeout,
        cert_reqs="CERT_REQUIRED",
    )


def _scrub(text: str, secret: tuple[str, str] | None) -> str:
    """Return ``text`` with either half of ``secret`` masked.

    Defence in depth for D8: every message this module raises or logs is
    built from credential-free fields, and this makes a mistake in that
    reasoning harmless rather than a leak.  Both halves of the pair are
    masked, because which half carries the token depends on the forge.
    """
    if not secret or not text:
        return text
    for part in secret:
        if part:
            text = text.replace(part, "***")
    return text


def _summarise(exc: BaseException, secret: tuple[str, str] | None) -> str:
    """Return a one-line, credential-free summary of ``exc``.

    Operator-facing errors quote at most this: the exception's first
    line, truncated.  A dulwich traceback in an error message tells an
    operator nothing they can act on, and the deeper frames are where an
    URL (and therefore, in another implementation, a token) would be
    most likely to surface.
    """
    text = str(exc).strip().splitlines()
    first = text[0] if text else type(exc).__name__
    if len(first) > 200:
        first = first[:200] + "…"
    return _scrub(first, secret) or type(exc).__name__


# ── credentials (D8) ────────────────────────────────────────────────


def _credential_for(spec: SourceSpec) -> tuple[str, str] | None:
    """Return the HTTP basic-auth pair for ``spec``, or ``None``.

    A credential reaches a fetch exactly one way: the operator's
    ``package_sources`` entry names an **environment variable**
    (``credential_ref``), and the value of that variable in the
    platform's own environment is used at call time.  A package may not
    supply one, the canonical URL never carries one
    (:func:`carpenter.packages.sources.redact` and
    ``_canonical_remote_url`` strip it), and nothing here writes one to
    disk, to the install record or to a log.

    The value is a token, or a ``user:token`` pair if the forge requires
    a named user; the first ``:`` separates them.  A bare token is sent
    as the basic-auth *username* with an empty password, which is the
    form a token-authenticated forge accepts without knowing the
    account name.

    Raises:
        FetchError: the named variable is unset or empty — the fetch
            cannot proceed and only an operator can fix it.  The message
            names the variable, never its value.
    """
    if not spec.alias:
        return None
    try:
        entries = configured_sources()
    except SourceSpecError as exc:  # pragma: no cover — defensive
        raise FetchError(
            f"package source {spec.alias!r} cannot be read from the "
            f"configured package_sources: {exc}",
        ) from exc
    entry = next((e for e in entries if e.name == spec.alias), None)
    if entry is None or not entry.credential_ref:
        return None
    name = entry.credential_ref
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise FetchError(
            f"package source {spec.alias!r} authenticates with the "
            f"environment variable {name}, which is unset or empty in the "
            f"platform's environment; an operator must supply it (a read "
            f"token for the source) before this source can be fetched",
        )
    if ":" in value:
        username, _, password = value.partition(":")
        return username, password
    return value, ""


# ── error translation ───────────────────────────────────────────────


def _translate(
    exc: BaseException, *, spec: SourceSpec, secret: tuple[str, str] | None,
) -> FetchError:
    """Map a dulwich failure onto one actionable operator message.

    The four cases an operator can actually act on — the repository is
    not there, the credential is wrong or missing, the host did not
    resolve, the host did not answer — are distinguished, because the
    fix differs for each and dulwich's own message for two of them is
    an empty string.
    """
    from dulwich import errors as dulwich_errors
    from dulwich import client as dulwich_client

    where = redact(spec.url or spec.raw)
    detail = _summarise(exc, secret)

    if isinstance(exc, FetchError):
        return exc
    if isinstance(
        exc,
        (dulwich_client.HTTPUnauthorized, dulwich_client.HTTPProxyUnauthorized),
    ):
        return FetchError(
            f"the git source {where} refused the credential presented for "
            f"it (HTTP 401). If the source is private, an operator must "
            f"set its package_sources credential_ref variable to a token "
            f"with read access; if it is public, remove the credential_ref.",
        )
    if isinstance(exc, dulwich_errors.NotGitRepository):
        return FetchError(
            f"the git source {where} is not a git repository, or is not "
            f"readable without a credential (the forge answered 404). "
            f"Check the repository path, and whether this source needs a "
            f"credential_ref.",
        )
    if isinstance(exc, ValueError) and "not a valid branch or tag" in str(exc):
        return FetchError(
            f"the git source {where} has no branch or tag {spec.ref!r}; "
            f"name an existing branch, tag or 40-character commit id",
        )

    lowered = detail.lower()
    if (
        "nameresolution" in lowered
        or "failed to resolve" in lowered
        or "name or service not known" in lowered
        or "nodename nor servname" in lowered
        or "temporary failure in name resolution" in lowered
    ):
        return FetchError(
            f"the host of the git source {where} could not be resolved "
            f"({detail}). Check the hostname and the platform host's DNS.",
        )
    if "timed out" in lowered or "timeout" in lowered:
        return FetchError(
            f"the git source {where} stopped responding mid-fetch "
            f"({detail})",
        )
    if (
        "connection refused" in lowered
        or "newconnectionerror" in lowered
        or "max retries exceeded" in lowered
        or "connection reset" in lowered
    ):
        return FetchError(
            f"the host of the git source {where} did not accept a "
            f"connection ({detail}). Check the port and that the forge is "
            f"reachable from the platform host.",
        )
    return FetchError(f"could not fetch the git source {where}: {detail}")


# ── budgets ─────────────────────────────────────────────────────────


def _dir_bytes(root: Path) -> int:
    """Return the apparent size of everything under ``root``.

    Used while the clone runs: dulwich's HTTP transport does not report
    the bytes it reads (``report_activity`` is wired for the local and
    ssh clients only), so what the clone costs is measured where it
    lands instead of on the wire.
    """
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, onerror=None):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:  # a file the clone is still writing
                continue
    return total


def _limit_error(limit: str, observed: object, allowed: object) -> FetchError:
    """Build the one message shape every cap breach uses."""
    return FetchError(
        f"git source exceeds the {limit} fetch limit: observed "
        f"{observed}, allowed {allowed}. Nothing was installed.",
    )


class _CloneWorker(threading.Thread):
    """Runs one blocking dulwich clone so the caller can time it out.

    dulwich is synchronous and offers no cancellation, so a wall-clock
    budget can only be honest about *who waits*, not about stopping the
    work: this thread is a daemon the caller **abandons** when the
    budget runs out.  Two things keep that from being a leak.  The
    transport is built with a socket timeout equal to the same budget,
    so an unresponsive server makes the abandoned thread fail on its own
    shortly afterwards; and everything it writes is inside the
    quarantine, which the dispatcher removes when the fetch raises, so a
    straggler's next write fails and it dies with it.  What it cannot do
    is affect the caller's result, which is already an error.
    """

    def __init__(self, work) -> None:
        super().__init__(name="package-git-fetch", daemon=True)
        self._work = work
        self.error: BaseException | None = None

    def run(self) -> None:  # pragma: no cover — exercised via fetch()
        try:
            self._work()
        except BaseException as exc:  # noqa: BLE001 — reported to the caller
            self.error = exc


# ── the fetcher ─────────────────────────────────────────────────────


class GitFetcher:
    """Materialises a package tree from a remote git repository.

    Registered for ``kind="git"``.  The dispatcher
    (:func:`carpenter.packages.fetch.fetch_source`) has already created
    the empty ``0700`` quarantine and run the egress gate over
    ``spec.url`` by the time :meth:`fetch` is called; this class owns
    everything from the clone onwards, including ref pinning, the
    resource caps and the tree hardening.
    """

    kinds: ClassVar[frozenset[str]] = frozenset({"git"})

    def fetch(
        self,
        spec: SourceSpec,
        dest: Path,
        *,
        limits: FetchLimits,
        require_manifest: bool = True,
    ) -> FetchResult:
        """Clone ``spec`` at its ref and return the package tree.

        Args:
            spec: A ``kind="git"`` spec with a canonical ``url``, an
                optional ``ref`` (branch, tag, 40-hex commit, or
                ``None`` for the remote's default branch) and an
                optional ``subdir``.
            dest: The quarantine directory to write inside.
            limits: Byte, file and wall-clock ceilings, all enforced
                before the tree is handed back.
            require_manifest: When ``False``, return the addressed
                directory even though it holds no ``manifest.yaml`` —
                a monorepo's index root, for
                :mod:`carpenter.packages.discover`.  This is the only
                check it relaxes: the clone is still shallow and
                capped, submodules are still refused, and
                :func:`_harden_tree` still runs over everything
                materialised.

        Returns:
            A :class:`~.fetch.FetchResult` whose ``tree`` holds
            ``manifest.yaml`` (unless ``require_manifest`` is
            ``False``), whose ``resolved`` is the 40-hex commit
            actually fetched, and whose ``staging`` is ``dest``.

        Raises:
            FetchError: the spec is not fetchable, the clone failed, the
                ref did not resolve (or did not match an explicit
                commit), a cap was breached, the tree contains something
                this platform refuses to install, or the subdir is not a
                package.  Never returns a partial tree.
        """
        if spec.kind != "git":
            raise FetchError(
                f"GitFetcher cannot fetch a {spec.kind!r} source",
            )
        url = spec.url
        if not url:
            raise FetchError(
                f"git source {spec.raw!r} has no repository URL",
            )

        deadline = time.monotonic() + max(float(limits.timeout_s), 0.0)
        credential = _credential_for(spec)

        clone_dir = dest / _CLONE_DIR_NAME
        tree_root = dest / _TREE_DIR_NAME
        logger.info(
            "Fetching package source %s at %s%s",
            redact(url), spec.ref or "the default branch",
            f" (subdir {spec.subdir})" if spec.subdir else "",
        )

        self._clone(
            spec, url, clone_dir,
            credential=credential, limits=limits,
            deadline=deadline, dest=dest,
        )

        from dulwich.repo import Repo

        try:
            repo = Repo(str(clone_dir))
        except Exception as exc:  # pragma: no cover — defensive
            raise _translate(exc, spec=spec, secret=credential) from exc
        try:
            commit = self._resolve_commit(repo, spec, secret=credential)
            root_tree = commit.tree
            self._refuse_submodules(repo, root_tree, spec)
            subtree = self._subtree(repo, root_tree, spec)
            tree_root.mkdir(parents=True, exist_ok=True)
            target = tree_root / spec.subdir if spec.subdir else tree_root
            total, files = self._materialise(
                repo, subtree, target,
                limits=limits, deadline=deadline, spec=spec,
            )
        finally:
            repo.close()

        _harden_tree(tree_root, spec)
        # The one check a listing fetch skips: an index root is a
        # directory OF packages, so it has no manifest of its own.
        # Everything above this line ran either way.
        if require_manifest and not (target / "manifest.yaml").is_file():
            where = (
                f"subdir {spec.subdir!r} of the git source"
                if spec.subdir else "the git source"
            )
            raise FetchError(
                f"{where} {redact(url)} has no manifest.yaml, so it is not "
                f"a capability package",
            )

        # The clone, and with it ``.git``, never reaches the installer:
        # the install hash must depend on the package bytes alone.
        import shutil

        shutil.rmtree(clone_dir, ignore_errors=True)

        resolved = commit.id.decode("ascii")
        logger.info(
            "Fetched package source %s at %s (%d files, %d bytes)",
            redact(url), resolved, files, total,
        )
        return FetchResult(
            tree=target, staging=dest, resolved=resolved,
            bytes_fetched=total, files_fetched=files,
        )

    # ── the clone ───────────────────────────────────────────────────

    def _clone(
        self, spec: SourceSpec, url: str, clone_dir: Path, *,
        credential: tuple[str, str] | None,
        limits: FetchLimits, deadline: float, dest: Path,
    ) -> None:
        """Clone ``url`` into ``clone_dir`` within the fetch's budget.

        Shallow (``depth=1``) whenever the ref names something the
        remote advertises, which is every case but an explicit commit
        id: a server only serves a ``want`` it advertised, so pinning an
        arbitrary commit needs the history that reaches it.  No working
        tree is written (``checkout=False``) — the tree is materialised
        from the object store instead, under the caps.
        """
        from dulwich import porcelain
        from dulwich.config import ConfigDict

        pinned_sha = bool(spec.ref and _SHA1_RE.match(spec.ref))
        depth = None if pinned_sha else 1
        branch = None if pinned_sha else spec.ref
        username, password = credential or (None, None)

        def work() -> None:
            # A socket timeout equal to the whole budget, so an
            # unresponsive peer ends this thread even once the caller
            # has stopped waiting for it.
            pool = _pool_manager(
                url, timeout=max(float(limits.timeout_s), 1.0),
            )
            repo = porcelain.clone(
                url, str(clone_dir),
                checkout=False, depth=depth, branch=branch,
                errstream=io.BytesIO(),
                # An empty config on purpose: see the module docstring.
                config=ConfigDict(),
                pool_manager=pool,
                username=username, password=password,
            )
            repo.close()

        worker = _CloneWorker(work)
        worker.start()
        while True:
            worker.join(_POLL_INTERVAL_S)
            if not worker.is_alive():
                break
            observed = _dir_bytes(dest)
            if observed > limits.max_bytes:
                raise _limit_error("max_bytes", observed, limits.max_bytes)
            if time.monotonic() >= deadline:
                raise _limit_error(
                    "timeout_s", f"still cloning after {limits.timeout_s}s",
                    f"{limits.timeout_s}s",
                )
        if worker.error is not None:
            raise _translate(
                worker.error, spec=spec, secret=credential,
            ) from None
        observed = _dir_bytes(dest)
        if observed > limits.max_bytes:
            raise _limit_error("max_bytes", observed, limits.max_bytes)

    # ── ref pinning ─────────────────────────────────────────────────

    def _resolve_commit(
        self, repo, spec: SourceSpec, *, secret: tuple[str, str] | None,
    ):
        """Return the commit object ``spec.ref`` names.

        An annotated tag is peeled to the commit it points at, so
        ``resolved`` is always a commit id and never a tag object id.
        An explicit commit id is looked up directly and then checked
        against what was asked for.
        """
        from dulwich.objects import Commit, Tag

        pinned = bool(spec.ref and _SHA1_RE.match(spec.ref))
        if pinned:
            wanted = spec.ref.encode("ascii")  # type: ignore[union-attr]
            try:
                obj = repo.object_store[wanted]
            except KeyError:
                raise FetchError(
                    f"commit {spec.ref} was not found in the git source "
                    f"{redact(spec.url or spec.raw)}; a pinned commit must "
                    f"be reachable from one of the repository's branches "
                    f"or tags",
                ) from None
        else:
            try:
                obj = repo[repo.head()]
            except Exception as exc:
                raise _translate(exc, spec=spec, secret=secret) from exc

        seen = 0
        while isinstance(obj, Tag):
            seen += 1
            if seen > 10:  # pragma: no cover — defensive
                raise FetchError(
                    f"the ref {spec.ref!r} of the git source "
                    f"{redact(spec.url or spec.raw)} is a chain of tags that "
                    f"does not end at a commit",
                )
            obj = repo.object_store[obj.object[1]]
        if not isinstance(obj, Commit):
            raise FetchError(
                f"the ref {spec.ref or 'HEAD'!r} of the git source "
                f"{redact(spec.url or spec.raw)} is a "
                f"{type(obj).__name__.lower()}, not a commit",
            )
        if pinned and obj.id.decode("ascii") != spec.ref:
            # Belt and braces: the fetched commit is the one that was
            # pinned, or nothing is installed.
            raise FetchError(
                f"the git source {redact(spec.url or spec.raw)} resolved to "
                f"commit {obj.id.decode('ascii')}, not the pinned "
                f"{spec.ref}",
            )
        return obj

    # ── submodules and the subdir ───────────────────────────────────

    def _refuse_submodules(self, repo, root_tree: bytes, spec: SourceSpec) -> None:
        """Refuse a repository that declares submodules.

        A fetch is not transitive: a submodule is a second source, with
        its own host, its own credential and its own trust decision, and
        none of those have been through the egress gate or the operator
        consent this one has.
        """
        from dulwich.object_store import tree_lookup_path

        try:
            tree_lookup_path(
                repo.object_store.__getitem__, root_tree, b".gitmodules",
            )
        except KeyError:
            return
        raise FetchError(
            f"the git source {redact(spec.url or spec.raw)} declares "
            f"submodules (.gitmodules); package fetches are not transitive, "
            f"so a package must vendor what it needs",
        )

    def _subtree(self, repo, root_tree: bytes, spec: SourceSpec) -> bytes:
        """Return the tree sha of the package root inside the clone."""
        if not spec.subdir:
            return root_tree
        from dulwich.object_store import tree_lookup_path

        try:
            mode, sha = tree_lookup_path(
                repo.object_store.__getitem__, root_tree,
                spec.subdir.encode("utf-8"),
            )
        except (KeyError, NotADirectoryError):
            raise FetchError(
                f"the git source {redact(spec.url or spec.raw)} has no "
                f"subdir {spec.subdir!r} at the fetched commit",
            ) from None
        if mode == _MODE_GITLINK:
            raise FetchError(
                f"subdir {spec.subdir!r} of the git source "
                f"{redact(spec.url or spec.raw)} is a submodule; package "
                f"fetches are not transitive",
            )
        if not stat.S_ISDIR(mode):
            raise FetchError(
                f"subdir {spec.subdir!r} of the git source "
                f"{redact(spec.url or spec.raw)} is not a directory",
            )
        return sha

    # ── materialisation under the caps ──────────────────────────────

    def _materialise(
        self, repo, tree_sha: bytes, target: Path, *,
        limits: FetchLimits, deadline: float, spec: SourceSpec,
    ) -> tuple[int, int]:
        """Write the package subtree to ``target``, enforcing ``limits``.

        Only the subtree that *is* the package is written: a monorepo's
        other packages are never materialised, so the caps bound the
        package rather than the repository it lives in.

        Every entry is checked before anything is created — the mode
        (only a directory, a regular file or a safe symlink), every path
        component, and the running byte and file totals — so a breach
        raises before the offending bytes are on disk.  Files are opened
        ``"xb"``, exclusively: a tree that names the same path twice, or
        names a path through something already written, fails rather
        than overwriting.
        """
        from dulwich.object_store import iter_tree_contents

        total = 0
        files = 0
        target.mkdir(parents=True, exist_ok=True)
        for index, entry in enumerate(
            iter_tree_contents(repo.object_store, tree_sha, include_trees=True),
        ):
            if index % _DEADLINE_EVERY == 0 and time.monotonic() >= deadline:
                raise _limit_error(
                    "timeout_s",
                    f"still writing the fetched tree after "
                    f"{limits.timeout_s}s",
                    f"{limits.timeout_s}s",
                )
            rel = self._entry_path(entry.path, spec)
            if rel is None:
                continue
            path = target / rel
            shown = rel.as_posix()
            mode = entry.mode
            if mode == _MODE_TREE:
                path.mkdir(parents=True, exist_ok=True)
                continue
            if mode == _MODE_GITLINK:
                raise FetchError(
                    f"the git source {redact(spec.url or spec.raw)} contains "
                    f"a submodule at {shown!r}; package fetches are not "
                    f"transitive",
                )
            if mode == _MODE_SYMLINK:
                self._write_symlink(repo, entry.sha, path, shown, spec)
                files += 1
                if files > limits.max_files:
                    raise _limit_error("max_files", files, limits.max_files)
                continue
            if mode not in (_MODE_BLOB, _MODE_BLOB_EXEC):
                raise FetchError(
                    f"the git source {redact(spec.url or spec.raw)} contains "
                    f"{shown!r} with unsupported mode {mode:o}; a package "
                    f"tree "
                    f"may hold only directories, regular files and symlinks",
                )

            blob = repo.object_store[entry.sha]
            size = blob.raw_length()
            if size > limits.max_file_bytes:
                raise _limit_error(
                    "max_file_bytes", f"{size} bytes in {shown!r}",
                    limits.max_file_bytes,
                )
            if total + size > limits.max_bytes:
                raise _limit_error(
                    "max_bytes", total + size, limits.max_bytes,
                )
            files += 1
            if files > limits.max_files:
                raise _limit_error("max_files", files, limits.max_files)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "xb") as handle:
                handle.write(blob.data)
            if mode == _MODE_BLOB_EXEC:
                path.chmod(0o755)
            total += size
        return total, files

    def _entry_path(self, raw: bytes, spec: SourceSpec) -> Path | None:
        """Validate one tree entry's path and return it, or ``None``.

        ``None`` is the subtree's own root entry, which
        :func:`iter_tree_contents` yields with an empty path.  Every
        component is checked against :data:`_FORBIDDEN_COMPONENTS` and
        the whole path is required to be relative, so nothing a remote
        can name escapes the directory being written.
        """
        if raw in (b"", b"."):
            return None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise FetchError(
                f"the git source {redact(spec.url or spec.raw)} contains a "
                f"path that is not valid UTF-8; such a tree cannot be "
                f"installed safely",
            ) from None
        if "\\" in text or "\0" in text or text.startswith("/"):
            raise FetchError(
                f"the git source {redact(spec.url or spec.raw)} contains an "
                f"unsafe path {text!r}",
            )
        parts = text.split("/")
        for part in parts:
            if part in _FORBIDDEN_COMPONENTS or part.lower() == ".git":
                raise FetchError(
                    f"the git source {redact(spec.url or spec.raw)} contains "
                    f"the path {text!r}, whose component {part!r} is refused "
                    f"in an installed package tree",
                )
        return Path(*parts)

    def _write_symlink(
        self, repo, sha: bytes, path: Path, shown: str, spec: SourceSpec,
    ) -> None:
        """Create one symlink, or refuse it.

        A symlink in a package tree is only allowed to point at
        something inside the same tree, by a relative path: an absolute
        target, or a relative one that climbs out, would make the
        installer copy — or the loaded package read — a file the
        operator never consented to.
        """
        raw = repo.object_store[sha].data
        try:
            link_target = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise FetchError(
                f"the git source {redact(spec.url or spec.raw)} contains a "
                f"symlink at {shown!r} whose target is not valid UTF-8",
            ) from None
        if not link_target or link_target.startswith("/") or "\0" in link_target:
            raise FetchError(
                f"the git source {redact(spec.url or spec.raw)} contains the "
                f"symlink {shown!r} -> {link_target!r}, an absolute or "
                f"empty target; a package tree may only link inside itself",
            )
        landing = os.path.normpath(
            os.path.join(os.path.dirname(shown), link_target),
        )
        if landing == ".." or landing.startswith(".." + os.sep) or os.path.isabs(landing):
            raise FetchError(
                f"the git source {redact(spec.url or spec.raw)} contains the "
                f"symlink {shown!r} -> {link_target!r}, which resolves "
                f"outside the fetched tree",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(link_target, path)


# ── hardening the materialised tree ─────────────────────────────────


def _harden_tree(root: Path, spec: SourceSpec) -> None:
    """Refuse anything in ``root`` that must not be installed.

    This runs on the real filesystem rather than on the git tree, so it
    holds whatever produced the bytes: every path is checked with
    ``lstat``, every symlink is resolved against ``root``, and a regular
    file with more than one link is refused because a hard link into the
    tree would let an installed package's contents be changed from
    outside it (and would make the package hash a lie).
    """
    resolved_root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        for name in list(dirnames) + list(filenames):
            path = here / name
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if stat.S_ISLNK(info.st_mode):
                landing = Path(os.path.normpath(
                    os.path.join(dirpath, os.readlink(path)),
                ))
                try:
                    landing.resolve().relative_to(resolved_root)
                except ValueError:
                    raise FetchError(
                        f"the fetched tree of {redact(spec.url or spec.raw)} "
                        f"contains the symlink "
                        f"{path.relative_to(root).as_posix()!r}, which "
                        f"resolves outside the fetched tree",
                    ) from None
                continue
            if not stat.S_ISREG(info.st_mode):
                raise FetchError(
                    f"the fetched tree of {redact(spec.url or spec.raw)} "
                    f"contains {path.relative_to(root).as_posix()!r}, which "
                    f"is neither a regular file, a directory nor a symlink",
                )
            if info.st_nlink > 1:
                raise FetchError(
                    f"the fetched tree of {redact(spec.url or spec.raw)} "
                    f"contains {path.relative_to(root).as_posix()!r}, a hard "
                    f"link ({info.st_nlink} links); an installed package "
                    f"tree must own its own bytes",
                )


register_fetcher(GitFetcher())

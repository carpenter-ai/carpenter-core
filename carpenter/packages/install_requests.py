"""Build a package-install request: resolve, fetch, preview, record, URL.

This is the first half of the two-phase flow a chat agent uses to get a
package installed (DESIGN D11).  It does everything that can be done
*without* anyone's consent — resolve the source specs, fetch the bytes
into quarantine, describe what is in them, write the pending row — and
then stops, handing back a one-time URL for a human to decide on.  The
install itself happens only if that human approves, out of the model
turn (WP5 T5-T7).

**Nothing in the fetched bytes is executed here.**  The preview is
``yaml.safe_load`` (:func:`~carpenter.packages.manifest.load_manifest`),
path and rglob checks
(:func:`~carpenter.packages.security.validate_manifest_security`), an
``ast.parse``
(:func:`~carpenter.packages.static_preview.inspect_package_chat_tools`)
and a byte hash
(:func:`~carpenter.packages.installer.compute_package_hash`).  Never
call ``installer.classify_package_chat_tools`` from this module: it
imports the package's chat-tool modules, which for a tree the operator
has not yet agreed to trust means arbitrary remote code runs *before*
the prompt that was meant to inform them — the preview would be the
exploit.  Nothing here shells out either: the git fetcher is dulwich, so
no hooks run, and ``.git`` never reaches the installed tree.

The fetched tree is **kept** in its quarantine and re-verified against
the previewed hash at install time; it is never re-fetched.  Re-fetching
at approval time would be a TOCTOU hole — the operator approves tree
hash H and a hostile remote serves H' to the second fetch.

Refusals are :class:`InstallRequestRefused` and their messages are
written for the chat agent to relay to the person asking: they name what
to change (a config key, a spec) and never carry the request token.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import fetch as fetch_mod
from . import pending_install
from .fetch import FetchError, FetchLimits, fetch_source, staging_root_dir
from .installer import compute_package_hash, get_install_record
from .manifest import ManifestError, load_manifest
from .security import PackageSecurityError, validate_manifest_security
from .sources import SourceSpecError, parse_source, redact, source_id
from .static_preview import inspect_package_chat_tools, write_consent_required

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_OPEN_REQUESTS",
    "DEFAULT_MAX_REQUESTS_PER_HOUR",
    "DEFAULT_MAX_SOURCES",
    "InstallRequest",
    "InstallRequestError",
    "InstallRequestRefused",
    "REJECTED_SOURCE_WINDOW_HOURS",
    "REQUEST_URL_PREFIX",
    "create_install_request",
    "limits_for",
    "request_path",
    "request_url",
]

#: Route prefix the approval page is served from (WP5 T5 owns the route;
#: this is the one definition of the path both halves agree on).
REQUEST_URL_PREFIX = "/api/package-install/"

#: Caps, overridable per deployment under the ``package_install`` config
#: key.  They are deliberately small.  The attack this flow is built
#: against is a third party talking the chat agent into asking for a
#: hostile package, and the last line of defence is a human reading the
#: approval page — so the thing to ration is how often the agent may put
#: a page in front of them.  An unlimited asker wears that defence down.
DEFAULT_MAX_SOURCES = 5
DEFAULT_MAX_OPEN_REQUESTS = 3
DEFAULT_MAX_REQUESTS_PER_HOUR = 5

#: How long a rejection keeps a source out.  A human saying no is the
#: strongest signal in the system; re-asking immediately is a retry loop
#: against their patience.
REJECTED_SOURCE_WINDOW_HOURS = 24


class InstallRequestError(Exception):
    """A package-install request could not be built."""


class InstallRequestRefused(InstallRequestError):
    """The request was refused, with an explanation fit to show the agent.

    Separate from :class:`InstallRequestError` so a caller can tell
    "this was refused, and here is what to tell the user" from an
    internal failure.  The message never contains a request token.
    """


@dataclass(frozen=True)
class InstallRequest:
    """A written request, plus everything the reply needs.

    Attributes:
        record: The persisted
            :class:`~carpenter.packages.pending_install.PendingInstall`.
        url: The approval URL.  Absolute when ``public_base_url`` is
            configured, otherwise the bare path — see ``url_is_absolute``.
        url_is_absolute: False when ``public_base_url`` is unset, in
            which case the URL is only usable from a browser already
            pointed at this platform.  The reply must say so plainly
            rather than handing the operator a link that cannot work
            from a chat app or an email.
        staging_root: Quarantine directory holding every fetched tree in
            the batch.  Kept until the request is decided or expires.
    """

    record: pending_install.PendingInstall
    url: str
    url_is_absolute: bool
    staging_root: Path

    @property
    def token(self) -> str:
        return self.record.token

    @property
    def preview(self) -> tuple[dict, ...]:
        return self.record.preview

    @property
    def package_names(self) -> tuple[str, ...]:
        return tuple(
            str(p.get("manifest", {}).get("name", "")) for p in self.preview
        )


# ── URLs ────────────────────────────────────────────────────────────

def request_path(token: str) -> str:
    """The relative approval path for a token."""
    return f"{REQUEST_URL_PREFIX}{token}"


def request_url(token: str) -> tuple[str, bool]:
    """Return ``(url, is_absolute)`` for a token's approval page.

    Reuses :func:`carpenter.api.review.absolutize_review_url`, which
    prefixes ``public_base_url`` when it is set.  That function is
    generically about making a platform URL reachable despite its
    review-flavoured name; it is imported lazily so this module does not
    drag the HTTP layer into a chat-tool import.
    """
    from ..api.review import absolutize_review_url

    path = request_path(token)
    url = absolutize_review_url(path)
    return url, url.startswith(("http://", "https://"))


# ── config ──────────────────────────────────────────────────────────

def _section(config: dict | None) -> dict:
    if config is not None:
        section = config.get("package_install")
    else:
        from .. import config as _config
        section = _config.CONFIG.get("package_install")
    return section if isinstance(section, dict) else {}


def _positive_int(section: dict, key: str, default: int) -> int:
    raw = section.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def limits_for(config: dict | None = None) -> FetchLimits:
    """Fetch ceilings for a chat-requested install.

    The :class:`~carpenter.packages.fetch.FetchLimits` defaults, with a
    deployment able to tighten them under ``package_install``.  Disk use
    is bounded by these times the request TTL; the expiry sweep is the
    collector.
    """
    section = _section(config)
    base = FetchLimits()
    return FetchLimits(
        max_bytes=_positive_int(section, "max_fetch_bytes", base.max_bytes),
        max_files=_positive_int(section, "max_fetch_files", base.max_files),
        max_file_bytes=_positive_int(
            section, "max_fetch_file_bytes", base.max_file_bytes,
        ),
        timeout_s=float(_positive_int(
            section, "fetch_timeout_s", int(base.timeout_s),
        )),
    )


# ── DB plumbing ─────────────────────────────────────────────────────

@contextmanager
def _reading(conn: sqlite3.Connection | None):
    """Yield a connection for reads: the caller's, or a fresh one."""
    if conn is not None:
        yield conn
        return
    from ..db import db_connection
    with db_connection() as db:
        yield db


@contextmanager
def _writing(conn: sqlite3.Connection | None):
    """Yield a connection for the insert: the caller's, or a transaction."""
    if conn is not None:
        yield conn
        return
    from ..db import db_transaction
    with db_transaction() as db:
        yield db


# ── validation ──────────────────────────────────────────────────────

def _validate_sources(sources, *, max_sources: int) -> list[str]:
    """Shape-check the model's ``sources`` argument.

    Repeated here even though the tool's input schema says the same
    thing, because schema validation only checks shape
    (``invocation._validate_tool_call``) and this is the boundary where
    untrusted model output becomes a fetch.
    """
    if isinstance(sources, str) or not isinstance(sources, (list, tuple)):
        raise InstallRequestRefused(
            "sources must be a list of source spec strings, e.g. "
            "[\"git+https://forge.example/org/repo.git#subdir=packages/x\"].",
        )
    if not sources:
        raise InstallRequestRefused(
            "sources is empty: name at least one package source to request.",
        )
    if len(sources) > max_sources:
        raise InstallRequestRefused(
            f"a single request may name at most {max_sources} sources "
            f"({len(sources)} given). Ask for fewer, in separate requests.",
        )
    out: list[str] = []
    for item in sources:
        if not isinstance(item, str) or not item.strip():
            raise InstallRequestRefused(
                "every entry in sources must be a non-empty string.",
            )
        out.append(item.strip())
    return out


def _parse_specs(raw_sources: list[str]):
    """Parse each spec, refusing a local path and a duplicate source."""
    specs = []
    seen: dict[str, str] = {}
    for raw in raw_sources:
        try:
            spec = parse_source(raw)
        except SourceSpecError as exc:
            raise InstallRequestRefused(f"{raw!r} is not a usable source: {exc}")
        if spec.kind == "local":
            # A local directory has no fetch, so there is no quarantined
            # copy to pin: the bytes could change between the preview the
            # operator reads and the install they approve, and the
            # operator's own filesystem is not something the chat agent
            # should be aiming an installer at.  The CLI is the path for
            # a local tree, where the operator IS the trust anchor.
            raise InstallRequestRefused(
                f"{raw!r} is a local path. A chat-requested install must name "
                f"a remote source (git+https://... or an https://... "
                f"archive); a local directory is installed by the operator "
                f"with `python3 -m carpenter packages install --path ...`.",
            )
        sid = source_id(spec)
        if sid in seen:
            raise InstallRequestRefused(
                f"{raw!r} and {seen[sid]!r} are the same source; name it once.",
            )
        seen[sid] = raw
        specs.append(spec)
    return specs


def _check_rate_limits(db, *, specs, section: dict, now: str) -> None:
    """Refuse an agent that is asking too often, or asking again.

    By SQL count, against the request table itself, so the limits hold
    across restarts and across every conversation — a per-process
    counter would reset on recycle and a per-conversation one would be
    bypassed by starting another conversation.
    """
    max_open = _positive_int(
        section, "max_open_requests", DEFAULT_MAX_OPEN_REQUESTS,
    )
    max_per_hour = _positive_int(
        section, "max_requests_per_hour", DEFAULT_MAX_REQUESTS_PER_HOUR,
    )
    open_now = pending_install.count_active_requests(db)
    if open_now >= max_open:
        raise InstallRequestRefused(
            f"there are already {open_now} package-install requests waiting "
            f"for the operator to decide (the limit is {max_open}). Ask them "
            f"to approve or reject the outstanding ones first.",
        )
    hour_ago = (
        datetime.fromisoformat(now) - timedelta(hours=1)
    ).isoformat()
    recent = pending_install.count_requests_since(db, hour_ago)
    if recent >= max_per_hour:
        raise InstallRequestRefused(
            f"{recent} package-install requests have been made in the last "
            f"hour, which is the limit ({max_per_hour}). Wait before asking "
            f"again, or ask the operator to install it themselves.",
        )
    window_start = (
        datetime.fromisoformat(now)
        - timedelta(hours=REJECTED_SOURCE_WINDOW_HOURS)
    ).isoformat()
    rejected = pending_install.rejected_source_ids_since(db, window_start)
    for spec in specs:
        if source_id(spec) in rejected:
            raise InstallRequestRefused(
                f"the operator rejected {spec.raw!r} within the last "
                f"{REJECTED_SOURCE_WINDOW_HOURS} hours. Do not ask again — "
                f"ask them directly why it was refused.",
            )


# ── preview ─────────────────────────────────────────────────────────

def _egress_status(host: str, config: dict | None) -> dict:
    """Host + whether it is named in ``web_egress_allowlist``.

    The gate itself already ran (the fetch dispatcher calls
    ``egress.check_url`` before any fetcher), so this is reporting, not
    enforcement: it tells the operator whether reaching this host needed
    an explicit allowlist entry — i.e. whether it is a private-network
    destination — and names the config key verbatim so the page can say
    where that decision lives.
    """
    if config is not None:
        entries = config.get("web_egress_allowlist") or []
    else:
        from .. import config as _config
        entries = _config.CONFIG.get("web_egress_allowlist") or []
    listed = any(
        isinstance(e, str) and e.strip().lower() == host.lower()
        for e in entries
    )
    return {
        "host": host,
        "in_web_egress_allowlist": listed,
        "allowlist_key": "web_egress_allowlist",
    }


def _manifest_block(manifest) -> dict:
    """Name, version, first line of description, and the counts block."""
    description = (manifest.description or "").strip()
    first_line = description.splitlines()[0] if description else ""
    return {
        "name": manifest.name,
        "version": manifest.version,
        "description": description,
        "description_first_line": first_line,
        "kb_namespace": manifest.kb_namespace or manifest.name,
        "counts": {
            "chat_tools": len(manifest.chat_tools),
            "arc_templates": len(manifest.arc_templates),
            "judge_handlers": len(manifest.judge_handlers),
            "data_models": len(manifest.data_models),
            "kb_articles": len(manifest.kb_articles),
            "trigger_subscriptions": len(manifest.trigger_subscriptions),
            "triggers": len(manifest.triggers),
            "allowlist_proposals": len(manifest.allowlist_proposals),
            "credential_requirements": len(manifest.credential_requirements),
            "platform_capabilities": len(manifest.platform_capabilities),
        },
    }


def _capability_blocks(manifest) -> list[dict]:
    """The declared platform capabilities, with the installer's wording.

    ``installer._describe_capability`` is reused so the approval page and
    the terminal consent prompt describe a grant identically — two
    wordings for the same grant is how an operator ends up approving
    something they would have refused in the other surface.
    """
    from .installer import _describe_capability

    out = []
    for cap in manifest.platform_capabilities:
        grant = cap.grant
        out.append({
            "verb": cap.verb,
            "kind": cap.kind,
            "module": cap.module,
            "handler": cap.handler,
            "grant": {
                "protocol": grant.protocol,
                "host_from": grant.host_from,
                "port": grant.port,
                "credential_ref": grant.credential_ref,
            },
            "describe": _describe_capability(cap),
        })
    return out


def _credential_blocks(manifest) -> list[dict]:
    """Declared credential requirements — NAMES only, never values.

    A package never ships credentials and this record never holds one:
    the operator supplies values separately, and the manifest only says
    which variables the package will read.
    """
    out = []
    for cred in manifest.credential_requirements:
        entry = {
            "kind": getattr(cred, "kind", ""),
            "provider": getattr(cred, "provider", ""),
            "env_key_prefix": getattr(cred, "env_key_prefix", ""),
        }
        required = getattr(cred, "required_keys", ()) or ()
        entry["env_vars"] = [
            f"{entry['env_key_prefix']}_{suffix}" for suffix in required
        ]
        scopes = getattr(cred, "scopes", ()) or ()
        if scopes:
            entry["scopes"] = list(scopes)
        out.append(entry)
    return out


def _static_tool_blocks(tree: Path, manifest) -> dict:
    """What the package's chat tools are, read with an AST parse only."""
    preview = inspect_package_chat_tools(tree, manifest)
    return {
        "chat_tool_modules": list(manifest.chat_tools),
        "chat_tools": [
            {
                "name": tool.name,
                "module": tool.module,
                "description": tool.description,
                "capabilities": list(tool.capabilities),
                "write_capabilities": list(tool.write_capabilities),
                "trust_boundary": tool.trust_boundary,
                "requires_user_confirm": tool.requires_user_confirm,
                "is_write_capable": tool.is_write_capable,
                "is_platform_boundary": tool.is_platform_boundary,
            }
            for tool in preview.tools
        ],
        "chat_tools_unresolved": list(preview.unresolved),
        "write_consent_required": write_consent_required(preview, manifest),
        "chat_tools_read_statically": True,
    }


def _already_installed_block(db, name: str) -> dict | None:
    """The prior install of this package name, if there is one."""
    record = get_install_record(db, name)
    if record is None:
        return None
    return {
        "version": record.get("version"),
        "hash": record.get("hash"),
        "installed_at": record.get("installed_at"),
        "source_id": record.get("source_id"),
        "source_url": record.get("source_url"),
        "write_chat_tools_allowed": record.get("write_chat_tools_allowed"),
    }


# ── the builder ─────────────────────────────────────────────────────

def create_install_request(
    sources,
    *,
    conversation_id: int | None = None,
    channel_type: str | None = None,
    request_text: str | None = None,
    conn: sqlite3.Connection | None = None,
    config: dict | None = None,
    limits: FetchLimits | None = None,
    now: str | None = None,
) -> InstallRequest:
    """Resolve, fetch, preview and record a package-install request.

    Args:
        sources: The model-supplied source spec strings.
        conversation_id: Conversation the request came from, recorded so
            the operator can see where it was asked for and so the
            outcome can be reported back there.
        channel_type: That conversation's channel type.  Looked up from
            ``channel_bindings`` when not given.
        request_text: The verbatim triggering user message.  Looked up
            from the conversation's last user message when not given;
            this is the attribution block on the approval page, and the
            one mitigation that lets a human notice "I never asked for
            this".
        conn: Connection to use for reads and the insert.  Default opens
            its own, so this is safe to call from a tool handler that
            holds no transaction.
        config: Config mapping to read limits and the TTL from (tests).
        limits: Fetch ceilings.  Default :func:`limits_for`.
        now: Override for the request clock (tests).

    Returns:
        The :class:`InstallRequest`, including the minted URL.

    Raises:
        InstallRequestRefused: The request was refused — bad spec, local
            path, duplicate source, rate limit, a source the egress
            guard denies, a tree whose manifest fails to load or fails
            the security guards.  Nothing is written and the quarantine
            is removed.
        InstallRequestError: Something else went wrong; also leaves
            nothing behind.
    """
    section = _section(config)
    at = now or pending_install.utc_now_iso()
    raw_sources = _validate_sources(
        sources,
        max_sources=_positive_int(
            section, "max_sources_per_request", DEFAULT_MAX_SOURCES,
        ),
    )
    specs = _parse_specs(raw_sources)

    with _reading(conn) as db:
        _check_rate_limits(db, specs=specs, section=section, now=at)
        if request_text is None and conversation_id is not None:
            request_text = _last_user_message(db, conversation_id)
        if channel_type is None and conversation_id is not None:
            channel_type = _channel_type_for(db, conversation_id)

    # One quarantine directory per batch, named with its own random id
    # rather than the request token: the token is the credential, and a
    # path appears in fetcher logs and OS-level error strings.
    batch_dir = staging_root_dir() / f"req-{uuid.uuid4().hex[:16]}"
    batch_dir.mkdir(parents=True, exist_ok=False)
    try:
        batch_dir.chmod(0o700)
    except OSError:  # pragma: no cover — best effort on odd filesystems
        logger.warning("Could not set 0700 on %s", batch_dir)

    results = []
    previews: list[dict] = []
    try:
        for spec in specs:
            result = _fetch_one(spec, batch_dir, limits or limits_for(config))
            results.append(result)
            previews.append(
                _preview_for(spec, result, config=config, conn=conn, at=at),
            )
        _refuse_duplicate_names(previews)
    except Exception:
        for result in results:
            fetch_mod.cleanup(result)
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise

    try:
        with _writing(conn) as db:
            record = pending_install.create_request(
                db,
                sources=raw_sources,
                preview=previews,
                request_text=request_text,
                conversation_id=conversation_id,
                channel_type=channel_type,
                staging_root=str(batch_dir),
                requested_at=at,
                config=config,
            )
    except Exception:
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise

    url, absolute = request_url(record.token)
    logger.warning(
        "Package install requested: token=%s sources=%s hosts=%s "
        "hashes=%s conversation=%s expires=%s",
        record.token_prefix,
        [p["source_id"] for p in previews],
        [p["egress"]["host"] for p in previews],
        [p["tree_hash"][:12] for p in previews],
        conversation_id,
        record.expires_at,
    )
    return InstallRequest(
        record=record, url=url, url_is_absolute=absolute,
        staging_root=batch_dir,
    )


def _fetch_one(spec, batch_dir: Path, limits: FetchLimits):
    """Fetch one source into the batch quarantine, or refuse."""
    try:
        return fetch_source(spec, limits=limits, staging_root=batch_dir)
    except FetchError as exc:
        # The dispatcher's message already names ``web_egress_allowlist``
        # and the host when the egress guard was the refusal, so it is
        # quoted rather than reworded.
        raise InstallRequestRefused(
            f"could not fetch {spec.raw!r}: {exc}",
        ) from exc


def _preview_for(spec, result, *, config, conn, at: str) -> dict:
    """Build one package's preview entry.  Executes no package code."""
    tree = Path(result.tree)
    manifest_path = tree / "manifest.yaml"
    try:
        manifest = load_manifest(manifest_path)
    except (ManifestError, FileNotFoundError, OSError) as exc:
        raise InstallRequestRefused(
            f"{spec.raw!r} does not hold a usable capability package: {exc}",
        ) from exc
    try:
        import yaml
        with open(manifest_path, encoding="utf-8") as fp:
            raw_manifest = yaml.safe_load(fp) or {}
    except Exception as exc:  # noqa: BLE001 — any read/parse failure
        raise InstallRequestRefused(
            f"{spec.raw!r}: manifest.yaml could not be re-read for the "
            f"security checks ({exc}).",
        ) from exc
    try:
        validate_manifest_security(
            manifest, raw_manifest=raw_manifest, manifest_path=manifest_path,
        )
    except PackageSecurityError as exc:
        # Refused at REQUEST time, so a tree like this never reaches an
        # approval page: there is no operator checkbox for it.
        raise InstallRequestRefused(
            f"{spec.raw!r} was refused by the package security guards: "
            f"{exc}",
        ) from exc

    host = ""
    if spec.url:
        try:
            host = urlsplit(spec.url).hostname or ""
        except ValueError:  # pragma: no cover — defensive
            host = ""

    with _reading(conn) as db:
        already = _already_installed_block(db, manifest.name)

    entry = {
        "spec": spec.raw,
        "source_id": source_id(spec),
        "kind": spec.kind,
        "url_redacted": redact(spec.url) if spec.url else "",
        "ref": spec.ref or "",
        "resolved": result.resolved or "",
        "subdir": spec.subdir or "",
        "egress": _egress_status(host, config),
        "tree_hash": compute_package_hash(tree),
        "tree_path": str(tree),
        "staging": str(result.staging) if result.staging else "",
        "bytes": int(result.bytes_fetched),
        "files": int(result.files_fetched),
        "fetched_at": at,
        "manifest": _manifest_block(manifest),
        "capabilities": _capability_blocks(manifest),
        "credentials": _credential_blocks(manifest),
        "allowlist_proposals": [
            {"policy_type": p.policy_type, "value": p.value}
            for p in manifest.allowlist_proposals
        ],
        "already_installed": already,
    }
    entry.update(_static_tool_blocks(tree, manifest))
    return entry


def _refuse_duplicate_names(previews: list[dict]) -> None:
    """Refuse a batch that would install the same package name twice."""
    seen: dict[str, str] = {}
    for entry in previews:
        name = entry["manifest"]["name"]
        if name in seen:
            raise InstallRequestRefused(
                f"{entry['spec']!r} and {seen[name]!r} both provide a "
                f"package called {name!r}; only one can be installed. Ask "
                f"for one of them.",
            )
        seen[name] = entry["spec"]


# ── conversation attribution ────────────────────────────────────────

def _last_user_message(db, conversation_id: int) -> str | None:
    """The most recent user message in a conversation, verbatim.

    This is the attribution block on the approval page.  It is stored
    verbatim and rendered escaped: the operator has to be able to read
    exactly what was said, including a hostile string, and decide
    whether they recognise it.
    """
    try:
        row = db.execute(
            "SELECT content FROM messages WHERE conversation_id = ? "
            "AND role = 'user' ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
    except sqlite3.Error:  # pragma: no cover — missing table in a stub DB
        return None
    if row is None:
        return None
    content = row[0]
    return content if isinstance(content, str) else None


def _channel_type_for(db, conversation_id: int) -> str | None:
    """The conversation's channel type, or None when it has no binding.

    None is left as None rather than guessed at: a web-UI conversation
    has no ``channel_bindings`` row, and so does an internally created
    one, and the approval page should say which it knows rather than
    claim a channel.
    """
    try:
        row = db.execute(
            "SELECT channel_type FROM channel_bindings "
            "WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
    except sqlite3.Error:  # pragma: no cover — missing table in a stub DB
        return None
    if row is None:
        return None
    return row[0] if isinstance(row[0], str) else None

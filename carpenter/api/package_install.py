"""Operator approval page and decide route for a chat-requested install.

The second half of the two-phase flow a chat agent uses to get a package
installed (D11 in ``docs/2026-10-21_package-remote-sources-design.md``).
:mod:`carpenter.packages.install_requests` has
already resolved, fetched into quarantine and statically previewed the
sources, and written a ``pending_package_installs`` row; this module
puts that row in front of a human and records what they decide.

**The uuid4 token in the URL names the request; it does not approve
it.**  The GET is left out of the UI-token gate in
:mod:`carpenter.api.auth` so the link opens from a chat app or an email,
which means the token must be unguessable, is single-use, expires, and
is logged only as its first eight characters.  The decide route is
**not** exempt: approving installs unsandboxed platform-side code, and
the approval URL is returned to the chat agent that asked and persisted
in the conversation, so possession of the link is not evidence a human
acted.  Deciding therefore needs a session authenticated to the
platform as well, and the page says so (:data:`AUTH_NOTE_LINES`).

Three rules this module exists to enforce:

* **A GET mutates nothing** except marking a row whose TTL has run out
  as ``expired``.  There is no approve-on-GET, because an email client
  preloading an image must not be able to install software.
* **The page is a decision surface, not an input surface.**  The only
  thing it sends back is the per-package grant checkboxes.  Destination,
  package name, source spec and tree hash all come from the recorded
  row; nothing installable can be influenced by the browser.
* **Everything rendered is escaped.**  A package description or a
  ``request_text`` is attacker-influenced text — a third party talking
  the chat agent into asking is the whole threat model — so every value
  goes through :func:`_esc` on its way into the HTML.

The install itself is not performed here: approving enqueues
``package.install.approved`` and returns.  A 10-60s fetch-verify,
copytree and hash on the event loop would stall the server, and a
browser refresh mid-request would open a double-install window.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import shutil
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .. import config
from ..db import db_connection, db_transaction
from ..packages import fetch as fetch_mod
from ..packages import pending_install
from ..packages.install_requests import (
    BATCH_DIR_PREFIX,
    LEGACY_BATCH_DIR_PREFIX,
    REQUEST_URL_PREFIX,
)
from ..packages.pending_install import PendingInstallStateError
from .auth import extract_token
from .static import load_template, read_asset

logger = logging.getLogger(__name__)

#: Work-queue event the approval enqueues.  The handler lives in a
#: sibling work package (WP5 T7); this module only enqueues.
INSTALL_APPROVED_EVENT = "package.install.approved"

#: Idempotency-key prefix for that work item.
IDEMPOTENCY_PREFIX = "pkg_install:"

#: The platform-capability banner, worded as
#: :func:`carpenter.packages.installer.confirm_platform_capabilities`
#: words it at the terminal.  Two wordings for the same grant is how an
#: operator ends up approving on one surface what they would have
#: refused on the other, so these sentences are deliberately identical;
#: ``tests/api/test_package_install_approval.py`` asserts they still
#: appear in that function's source.
CAPABILITY_BANNER_LINES = (
    "Installing this package will register TRUSTED, platform-side",
    "dispatch-verb handlers. These run with the platform's egress and",
    "credentials — a DIFFERENT trust class than the package's sandboxed",
    "executor scripts. Trusted handler code CANNOT be sandboxed.",
)

#: The consent sentences that follow the capability list, split exactly
#: as ``confirm_platform_capabilities`` splits them.
CAPABILITY_CONSENT_LINES = (
    "Granting trust means you have reviewed this package's handler",
    "code and accept that it runs with platform privileges.",
)

#: What ``allowlist_proposals`` cost, stated where they are granted.
RATCHET_WARNING = (
    "One-way ratchet: uninstalling the package does NOT remove these "
    "security-policy entries (SD5). Approving widens what the platform "
    "will reach, permanently, until an operator removes them by hand."
)

#: Said on the page, because the one thing an operator must not have to
#: discover by clicking is that the button will refuse them.  Approving
#: deliberately needs more than the link: see :func:`carpenter.api.auth.
#: is_protected`.
AUTH_NOTE_LINES = (
    "Reading this page needs only the link. APPROVING needs you to be "
    "signed in to this platform as well.",
    "If the buttons refuse with \u201cnot signed in\u201d, open this page "
    "from the Carpenter web UI (or append your platform token) and decide "
    "there. The link alone cannot install anything \u2014 whoever or "
    "whatever passed it on, including the assistant that asked, cannot "
    "approve it.",
)

#: What a credential requirement is, and is not.
CREDENTIAL_NOTE = (
    "Variable names only. You supply the values via a separate one-time "
    "link; the package never ships credentials, and no value appears on "
    "this page."
)


# ── escaping ────────────────────────────────────────────────────────

def _esc(value) -> str:
    """HTML-escape any value, including ``None`` and non-strings.

    Quotes are escaped too, so the result is safe in an attribute as
    well as in text.  Everything that reaches the page goes through
    here: the request text came from a chat message and a manifest
    description came off a remote, and both are attacker-influenced.
    """
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _esc_pre(value) -> str:
    """Escape a multi-line value for a ``<pre>`` block."""
    return _esc(value)


def _flag(value: bool, yes: str, no: str) -> str:
    """A yes/no span, coloured so the unusual answer stands out."""
    cls = "flag-yes" if value else "flag-no"
    return f'<span class="{cls}">{_esc(yes if value else no)}</span>'


def _kv_rows(pairs) -> str:
    """A two-column table from ``(label, value_html)`` pairs."""
    rows = "".join(
        f"<tr><th>{_esc(label)}</th><td>{value}</td></tr>"
        for label, value in pairs
    )
    return f'<table class="kv">{rows}</table>'


# ── preview accessors ───────────────────────────────────────────────

def _package_name(entry: dict) -> str:
    """The manifest name of one preview entry, or ``""``."""
    manifest = entry.get("manifest")
    if not isinstance(manifest, dict):
        return ""
    return str(manifest.get("name") or "")


def _package_names(record) -> list[str]:
    """Every package name in the batch, in ``sources`` order."""
    return [_package_name(e) for e in record.preview]


# ── page blocks ─────────────────────────────────────────────────────

def _render_attribution(record) -> str:
    """The FIRST block: who asked, where, and in whose words.

    This is the one mitigation that lets a human notice "I never asked
    for this".  A third party who talks the chat agent into requesting a
    hostile package cannot stop the triggering message being shown.
    """
    if record.conversation_id is None:
        asked_where = (
            "No conversation was recorded for this request. Treat that as "
            "suspicious: a request nobody can attribute should be rejected."
        )
    else:
        channel = (
            f"over the <code>{_esc(record.channel_type)}</code> channel"
            if record.channel_type
            else "with no channel binding recorded"
        )
        asked_where = (
            f"Asked from conversation "
            f"<code>#{_esc(record.conversation_id)}</code> {channel}."
        )

    if record.request_text:
        text_block = (
            f'<div class="request-text">{_esc(record.request_text)}</div>'
        )
    else:
        text_block = (
            '<div class="request-text">(no triggering message was '
            'recorded)</div>'
        )

    return (
        '<div class="attribution">'
        "<h3>The chat agent asked for this because of the following "
        "message</h3>"
        '<p class="warn">If you do not recognise it, reject.</p>'
        f'<div class="asked">{asked_where}</div>'
        f"{text_block}"
        '<p class="note">The message is quoted verbatim from the '
        "conversation. It may itself have been planted by something the "
        "agent read &mdash; an email, a web page, a knowledge-base entry "
        "&mdash; so recognising the words is not the same as having asked "
        "for the install.</p>"
        "</div>"
    )


def _render_timing(record) -> str:
    """Requested/expires, and the one-shot contract."""
    return (
        '<div class="timing">'
        + _kv_rows([
            ("Requested", _esc(record.requested_at)),
            ("Link expires", _esc(record.expires_at)),
            ("Request", f"<code>{_esc(record.token_prefix)}</code>"),
        ])
        + '<p class="note">This link can be used once. After a decision, or '
        "after it expires, it stops working.</p>"
        "</div>"
    )


def _render_provenance(entry: dict) -> str:
    """Where these exact bytes came from."""
    tree_hash = str(entry.get("tree_hash") or "")
    egress = entry.get("egress") if isinstance(entry.get("egress"), dict) else {}
    host = str(egress.get("host") or "")
    listed = bool(egress.get("in_web_egress_allowlist"))
    key = str(egress.get("allowlist_key") or "web_egress_allowlist")

    rows = [
        ("Source spec", f"<code>{_esc(entry.get('spec'))}</code>"),
        ("Source id", f"<code>{_esc(entry.get('source_id'))}</code>"),
        ("Kind", _esc(entry.get("kind"))),
        ("URL (redacted)", f"<code>{_esc(entry.get('url_redacted'))}</code>"),
        ("Requested ref", f"<code>{_esc(entry.get('ref') or '(default)')}</code>"),
        ("Resolved commit/sha256",
         f"<code>{_esc(entry.get('resolved') or '(none recorded)')}</code>"),
        ("Subdir", f"<code>{_esc(entry.get('subdir') or '(repository root)')}</code>"),
        ("Tree hash", f"<code>{_esc(tree_hash)}</code>"),
        ("Tree hash (short)", f"<code>{_esc(tree_hash[:12])}</code>"),
        ("Bytes fetched", _esc(entry.get("bytes"))),
        ("Files fetched", _esc(entry.get("files"))),
        ("Fetched at", _esc(entry.get("fetched_at"))),
        ("Host", f"<code>{_esc(host)}</code>"),
        (
            f"In {key}",
            _flag(
                listed,
                f"yes — reaching this host needed an explicit {key} entry",
                f"no — not named in {key}",
            ),
        ),
    ]
    return (
        "<h4>Provenance</h4>"
        + _kv_rows(rows)
        + '<p class="note">These bytes are already on disk, in quarantine. '
        "They are re-hashed against the tree hash above before anything is "
        "installed and are never re-fetched, so what you approve is what "
        "gets installed.</p>"
    )


def _render_manifest(entry: dict) -> str:
    """Name, version, first line of description, and the counts block."""
    manifest = entry.get("manifest") if isinstance(entry.get("manifest"), dict) else {}
    counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
    rows = [
        ("Description", _esc(manifest.get("description_first_line"))),
        ("KB namespace", f"<code>{_esc(manifest.get('kb_namespace'))}</code>"),
    ]
    rows += [
        (label.replace("_", " "), _esc(counts.get(label, 0)))
        for label in (
            "chat_tools", "arc_templates", "judge_handlers", "data_models",
            "kb_articles", "trigger_subscriptions", "triggers",
            "allowlist_proposals", "credential_requirements",
            "platform_capabilities",
        )
    ]
    return "<h4>Manifest</h4>" + _kv_rows(rows)


def _render_capabilities(entry: dict) -> str:
    """``platform_capabilities``, as loud as the terminal banner."""
    caps = entry.get("capabilities") or []
    if not caps:
        return (
            '<div class="note">This package declares no '
            "<code>platform_capabilities</code>: no trusted, platform-side "
            "handler is registered by installing it.</div>"
        )
    name = _package_name(entry)
    described = "\n".join(str(cap.get("describe") or "") for cap in caps)
    banner = "<br>".join(_esc(line) for line in CAPABILITY_BANNER_LINES)
    consent = " ".join(_esc(line) for line in CAPABILITY_CONSENT_LINES)
    return (
        '<div class="caps">'
        f"<h4>Security: package &#39;{_esc(name)}&#39; requests "
        f"PLATFORM-LEVEL TRUST</h4>"
        f'<p class="banner">{banner}</p>'
        f"<div><strong>Capabilities requested "
        f"({len(caps)}):</strong></div>"
        f"<pre>{_esc_pre(described)}</pre>"
        f'<p class="banner">{consent}</p>'
        '<p class="note">All or nothing: the grant covers every verb listed, '
        "because the terminal consent prompt is all-or-nothing too. Leaving "
        "the box unticked still installs the package &mdash; it just "
        "registers none of these verbs.</p>"
        "</div>"
    )


def _render_allowlist_proposals(entry: dict) -> str:
    """``allowlist_proposals`` plus the ratchet warning."""
    proposals = entry.get("allowlist_proposals") or []
    if not proposals:
        return ""
    items = "".join(
        "<li><code>{}</code> &rarr; <code>{}</code></li>".format(
            _esc(p.get("policy_type") if isinstance(p, dict) else ""),
            _esc(p.get("value") if isinstance(p, dict) else p),
        )
        for p in proposals
    )
    return (
        '<div class="ratchet">'
        f"<h4>Security-policy entries this package adds ({len(proposals)})</h4>"
        f"<ul>{items}</ul>"
        f'<p class="warn">{_esc(RATCHET_WARNING)}</p>'
        "</div>"
    )


def _render_credentials(entry: dict) -> str:
    """Credential requirements, as variable NAMES only."""
    creds = entry.get("credentials") or []
    if not creds:
        return ""
    blocks = []
    for cred in creds:
        if not isinstance(cred, dict):
            continue
        names = cred.get("env_vars") or []
        listed = "".join(f"<li><code>{_esc(n)}</code></li>" for n in names)
        scopes = cred.get("scopes") or []
        scope_html = (
            f'<div class="note">Scopes: {_esc(", ".join(str(s) for s in scopes))}</div>'
            if scopes else ""
        )
        blocks.append(
            f"<div><strong>{_esc(cred.get('kind'))}</strong> "
            f"/ {_esc(cred.get('provider'))} "
            f"(<code>{_esc(cred.get('env_key_prefix'))}</code>)</div>"
            f"<ul>{listed}</ul>{scope_html}"
        )
    return (
        '<div class="creds">'
        f"<h4>Credentials this package will read ({len(creds)})</h4>"
        + "".join(blocks)
        + f'<p class="note">{_esc(CREDENTIAL_NOTE)}</p>'
        "</div>"
    )


def _declares_chat_tools(entry: dict) -> bool:
    """Whether the preview showed any chat tool, named or unreadable.

    The same three fields :func:`_render_chat_tools` renders from, so
    "nothing was shown" and "nothing can be granted" cannot drift
    apart.  An unreadable module counts as declaring one: the page says
    to treat it as write-capable, so the grant is meaningful there.
    """
    return bool(
        entry.get("chat_tools")
        or entry.get("chat_tools_unresolved")
        or entry.get("chat_tool_modules")
    )


def _render_chat_tools(entry: dict) -> str:
    """The chat tools, read statically, and what could not be read."""
    tools = entry.get("chat_tools") or []
    unresolved = entry.get("chat_tools_unresolved") or []
    modules = entry.get("chat_tool_modules") or []
    if not tools and not unresolved and not modules:
        return ""

    rows = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        marks = []
        if tool.get("is_write_capable"):
            marks.append("WRITE-CAPABLE")
        if tool.get("is_platform_boundary"):
            marks.append("platform boundary")
        if tool.get("requires_user_confirm"):
            marks.append("requires confirmation")
        caps = ", ".join(str(c) for c in (tool.get("capabilities") or []))
        rows.append(
            "<li><code>{}</code> ({}) — {}{}{}</li>".format(
                _esc(tool.get("name")),
                _esc(tool.get("module")),
                _esc(tool.get("description") or "(no description)"),
                f" <em>[{_esc(', '.join(marks))}]</em>" if marks else "",
                f'<div class="note">capabilities: {_esc(caps)}</div>' if caps else "",
            )
        )
    tool_list = f"<ul>{''.join(rows)}</ul>" if rows else ""

    module_list = (
        "<div>Declared chat-tool modules: "
        + ", ".join(f"<code>{_esc(m)}</code>" for m in modules)
        + "</div>"
    ) if modules else ""

    unresolved_html = ""
    if unresolved:
        unresolved_html = (
            f"<div><strong>Could not be read statically "
            f"({len(unresolved)}):</strong></div><ul>"
            + "".join(f"<li>{_esc(u)}</li>" for u in unresolved)
            + "</ul>"
            '<p class="note">Anything in this list is unknown, not safe. '
            "Treat the package as if it declared a write-capable tool.</p>"
        )

    write_consent = bool(entry.get("write_consent_required"))
    return (
        '<div class="tools">'
        "<h4>Chat tools this package would add</h4>"
        f"{module_list}{tool_list}{unresolved_html}"
        + (
            '<p class="warn">A write-capable chat tool is declared (or could '
            "not be ruled out), so installing it needs the write-tools box "
            "ticked below.</p>" if write_consent else ""
        )
        + '<p class="note">Read by parsing the package\'s source only '
        "(<code>ast.parse</code>): <strong>the package’s own code was "
        "not executed</strong> to produce this list, and will not run until "
        "after you approve.</p>"
        "</div>"
    )


def _render_already_installed(entry: dict) -> str:
    """The prior install of this name, if any."""
    prior = entry.get("already_installed")
    if not isinstance(prior, dict):
        return (
            '<div class="note">Not currently installed: this would be a new '
            "package.</div>"
        )
    return (
        '<div class="already">'
        "<strong>Already installed &mdash; approving REPLACES it.</strong>"
        + _kv_rows([
            ("Installed version", _esc(prior.get("version"))),
            ("Installed hash", f"<code>{_esc(prior.get('hash'))}</code>"),
            ("Installed at", _esc(prior.get("installed_at"))),
            ("From source id", f"<code>{_esc(prior.get('source_id'))}</code>"),
            ("From URL", f"<code>{_esc(prior.get('source_url'))}</code>"),
            ("Write chat tools allowed",
             _esc(prior.get("write_chat_tools_allowed"))),
        ])
        + "</div>"
    )


def _render_grants(entry: dict) -> str:
    """The per-package checkboxes, all default UNCHECKED.

    A box is rendered only for a grant this package could use: a
    capability grant for a package declaring no verbs, or a
    write-chat-tools grant for a package declaring no chat tools, is
    consent to something the page never showed as requested, and
    :func:`_normalize_grants` refuses it even if it is posted.
    """
    name = _package_name(entry)
    caps = entry.get("capabilities") or []
    boxes = []
    if caps:
        verbs = ", ".join(str(c.get("verb")) for c in caps if isinstance(c, dict))
        boxes.append(
            '<label><input type="checkbox" '
            f'name="grant_capabilities[{_esc(name)}]" '
            f'data-pkg="{_esc(name)}" data-grant="capabilities">'
            f"Grant PLATFORM-LEVEL TRUST for all {len(caps)} declared "
            "verbs</label>"
            f'<div class="sub">All or nothing: {_esc(verbs)}. Unticked, the '
            "package still installs with no verbs registered.</div>"
        )
    if _declares_chat_tools(entry):
        boxes.append(
            '<label><input type="checkbox" '
            f'name="allow_write_chat_tools[{_esc(name)}]" '
            f'data-pkg="{_esc(name)}" data-grant="write_chat_tools">'
            "Allow this package's write-capable chat tools</label>"
            '<div class="sub">Unticked, a package declaring a write-capable '
            "chat tool is refused at install time rather than installed "
            "without it.</div>"
        )
    else:
        boxes.append(
            '<div class="sub">This package declares no chat tools, so there '
            "is no write-tools grant to give: it is recorded as not "
            "granted whatever is sent.</div>"
        )
    return (
        '<div class="grants">'
        f"<h4>Grants for {_esc(name)}</h4>"
        + "".join(boxes)
        + '<p class="note">Every box starts unticked. Nothing here is '
        "remembered for next time.</p>"
        "</div>"
    )


def _render_package(entry: dict, index: int) -> str:
    """One package's full block."""
    manifest = entry.get("manifest") if isinstance(entry.get("manifest"), dict) else {}
    name = _package_name(entry) or "(unnamed)"
    version = str(manifest.get("version") or "")
    return (
        '<div class="pkg">'
        f"<h3>{index}. {_esc(name)} "
        f'<span class="version">{_esc(version)}</span></h3>'
        + _render_already_installed(entry)
        + _render_provenance(entry)
        + _render_manifest(entry)
        + _render_capabilities(entry)
        + _render_allowlist_proposals(entry)
        + _render_credentials(entry)
        + _render_chat_tools(entry)
        + _render_grants(entry)
        + "</div>"
    )


def _render_auth_note() -> str:
    """The "approving needs a signed-in session" block."""
    return (
        '<div class="warn">'
        + "".join(f"<p>{_esc(line)}</p>" for line in AUTH_NOTE_LINES)
        + "</div>"
    )


def _viewer_ui_token(request: Request) -> str:
    """The platform token to hand the page's script, or ``""``.

    Echoed back only when the viewer already presented the configured
    ``ui_token``, so the page never hands the platform credential to
    someone who merely holds the approval link — and never reflects an
    attacker-chosen string into the script either.
    """
    configured = config.CONFIG.get("ui_token", "") or ""
    if not configured:
        return ""
    provided = extract_token(request)
    if provided and hmac.compare_digest(provided, configured):
        return configured
    return ""


# ── pages ───────────────────────────────────────────────────────────

def _render_approval_page(record, ui_token: str = "") -> HTMLResponse:
    """The approval page for a ``requested`` row.

    ``ui_token`` is the platform token the viewer presented, if any; the
    page's script sends it with the decision because the decide route
    requires platform authentication on top of the link.
    """
    names = [n for n in _package_names(record) if n]
    subtitle = (
        f"{len(record.preview)} package(s): "
        + ", ".join(_esc(n) for n in names)
    )
    packages = "".join(
        _render_package(entry, i)
        for i, entry in enumerate(record.preview, start=1)
    )
    css = read_asset("package-install.css")
    js = read_asset("package-install.js").replace(
        "__DECIDE_URL__", f"{REQUEST_URL_PREFIX}{record.token}/decide",
    ).replace("__UI_TOKEN__", ui_token)
    html_page = load_template(
        "package-install.html",
        token_short=record.token_prefix,
        css=css,
        js=js,
        subtitle=subtitle,
        attribution=_render_attribution(record),
        timing=_render_timing(record),
        packages=packages,
        auth_note=_render_auth_note(),
        footnote=(
            "Nothing on this page can change what gets installed: the "
            "destination, the package name, the source spec and the tree "
            "hash all come from the recorded request. The only thing this "
            "page sends is the grant tick boxes."
        ),
    )
    return HTMLResponse(content=html_page)


def _render_status_page(record) -> HTMLResponse:
    """The status page for a row that has already been decided."""
    rows = [("Requested", _esc(record.requested_at))]
    if record.decided_at:
        rows.append(("Decided", _esc(record.decided_at)))
    if record.finished_at:
        rows.append(("Finished", _esc(record.finished_at)))
    if record.grants:
        rows.append(("Grants", f"<code>{_esc(json.dumps(record.grants, sort_keys=True))}</code>"))
    if record.error:
        rows.append(("Note", _esc(record.error)))
    if record.result:
        rows.append(
            ("Result", f"<pre>{_esc(json.dumps(record.result, indent=2, sort_keys=True))}</pre>")
        )

    names = [n for n in _package_names(record) if n]
    packages = (
        '<div class="timing"><h3>Packages in this request</h3><ul>'
        + "".join(f"<li><code>{_esc(n)}</code></li>" for n in names)
        + "</ul></div>"
    ) if names else ""

    css = read_asset("package-install.css")
    html_page = load_template(
        "package-install-status.html",
        token_short=record.token_prefix,
        css=css,
        subtitle=f"{len(record.preview)} package(s)",
        status=_esc(record.status),
        details=_kv_rows(rows),
        packages=packages,
    )
    return HTMLResponse(content=html_page)


# ── expiry ──────────────────────────────────────────────────────────

def _expire_lazily(token: str) -> None:
    """Mark a timed-out request ``expired``.

    The only mutation a GET is allowed to perform.  A lost race (the row
    was decided between the read and this write) is ignored: the row has
    an outcome, which is better than ``expired``.
    """
    try:
        with db_transaction() as db:
            pending_install.mark_expired(db, token)
    except PendingInstallStateError:
        logger.info(
            "Package install request %s was decided before the lazy expiry "
            "could mark it",
            pending_install.token_prefix(token),
        )


def _gone() -> HTTPException:
    """The 410 for a spent or timed-out link.  Carries no token."""
    return HTTPException(
        status_code=410,
        detail=(
            "This package install approval link is no longer valid: it has "
            "already been decided or it expired. Nothing was installed. Ask "
            "again if you still want it."
        ),
    )


# ── routes ──────────────────────────────────────────────────────────

async def view_request(request: Request):
    """Render the approval page, or a status page for a decided row.

    Mutates nothing except marking an expired row ``expired``.  A GET
    never approves: an email client preloading a link must not be able
    to install software.
    """
    token = request.path_params["token"]
    with db_connection() as db:
        record = pending_install.get_request(db, token)

    if record is None:
        # Identical to a swept row, so probing cannot tell "never
        # existed" from "already gone".
        raise HTTPException(
            status_code=404,
            detail="Package install request not found",
        )

    if record.status == pending_install.STATUS_REQUESTED and record.is_expired():
        logger.warning(
            "Package install request %s viewed after expiry; marking expired",
            record.token_prefix,
        )
        _expire_lazily(record.token)
        raise _gone()

    if record.status == pending_install.STATUS_EXPIRED:
        raise _gone()

    if record.status != pending_install.STATUS_REQUESTED:
        return _render_status_page(record)

    logger.info(
        "Package install approval page served for request %s (%d package(s))",
        record.token_prefix, len(record.preview),
    )
    return _render_approval_page(record, _viewer_ui_token(request))


def _normalize_grants(record, raw) -> dict:
    """Build ``grants_json`` from the posted checkboxes.

    Keyed by the package names recorded in the request, never by what
    the browser sent: an unknown name is ignored, and a grant the
    *preview* did not show as requested is forced false — a capability
    grant for a package declaring no verbs, or a write-chat-tools grant
    for a package declaring no chat tools.  Recording consent to
    something that was never asked for is worse than useless: the
    install reads ``grants_json`` back verbatim, and the row is also the
    audit record of what the operator agreed to.  That is what keeps the
    page a decision surface — nothing installable comes from the request
    body.
    """
    posted = raw if isinstance(raw, dict) else {}
    grants: dict[str, dict] = {}
    for entry in record.preview:
        name = _package_name(entry)
        if not name:
            continue
        sent = posted.get(name)
        sent = sent if isinstance(sent, dict) else {}
        declares_caps = bool(entry.get("capabilities"))
        declares_tools = _declares_chat_tools(entry)
        for field, declared in (
            ("capabilities", declares_caps),
            ("write_chat_tools", declares_tools),
        ):
            if sent.get(field) and not declared:
                logger.warning(
                    "Package install request %s: ignoring the %s grant for "
                    "%r, which declares none",
                    record.token_prefix, field, name,
                )
        grants[name] = {
            "capabilities": bool(sent.get("capabilities")) and declares_caps,
            "write_chat_tools": (
                bool(sent.get("write_chat_tools")) and declares_tools
            ),
        }
    return grants


def _cleanup_quarantine(record) -> None:
    """Delete the batch's quarantine directory after a rejection.

    Guarded on the path's shape — it must be a batch directory directly
    inside the quarantine root — so a corrupted or hand-edited row
    cannot turn a rejection into an arbitrary ``rmtree``.  The legacy
    prefix is accepted because a row written before the rename points at
    a directory with the old name.
    """
    root = record.staging_root
    if not root:
        return
    path = Path(root)
    if path.parent.name != fetch_mod.STAGING_DIR_NAME or not path.name.startswith(
        (BATCH_DIR_PREFIX, LEGACY_BATCH_DIR_PREFIX),
    ):
        logger.warning(
            "Refusing to remove quarantine for request %s: %r is not a "
            "package-staging batch directory",
            record.token_prefix, path.name,
        )
        return
    shutil.rmtree(path, ignore_errors=True)
    logger.warning(
        "Quarantine for rejected package install request %s removed",
        record.token_prefix,
    )


def _enqueue_install(record) -> int | None:
    """Enqueue the approved batch and wake the main loop.

    Enqueued *after* the approval commits, not inside it:
    ``work_queue.enqueue`` opens its own ``db_transaction()``, and a
    nested write transaction on one thread is refused outright
    (``db.py``).  The idempotency key makes a re-enqueue harmless, and
    the handler refuses any row that is not ``approved``, so the small
    window between the two writes costs nothing worse than a request
    that needs re-deciding.

    The payload carries the row **id**, not the token: the work-queue
    table is not a place to keep a credential.
    """
    from ..core.engine import work_queue
    from ..core.engine.main_loop import wake_signal

    work_id = work_queue.enqueue(
        INSTALL_APPROVED_EVENT,
        {
            "request_id": record.id,
            "token_prefix": record.token_prefix,
            "packages": [n for n in _package_names(record) if n],
        },
        idempotency_key=f"{IDEMPOTENCY_PREFIX}{record.id}",
        max_retries=work_queue.SINGLE_ATTEMPT,
        priority=50,
    )
    wake_signal.set()
    return work_id


async def submit_decision(request: Request):
    """Record the operator's approve/reject for one request.

    One-shot: the token is spent by the first decision, and a second
    POST is a 410.  Approving flips the row to ``approved`` with the
    grants and enqueues the install; rejecting flips it to ``rejected``
    and deletes the quarantine.  Neither path installs anything inline.
    """
    token = request.path_params["token"]
    try:
        body = await request.json()
    except ValueError as exc:  # json.JSONDecodeError subclasses it
        raise HTTPException(
            status_code=400, detail="Expected a JSON body",
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")

    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        raise HTTPException(
            status_code=400,
            detail="decision must be 'approve' or 'reject'",
        )

    with db_connection() as db:
        record = pending_install.get_request(db, token)

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Package install request not found",
        )

    if record.status == pending_install.STATUS_REQUESTED and record.is_expired():
        logger.warning(
            "Package install request %s decided after expiry; refusing",
            record.token_prefix,
        )
        _expire_lazily(record.token)
        raise _gone()

    if record.status != pending_install.STATUS_REQUESTED:
        logger.warning(
            "Second decision refused for package install request %s "
            "(status %s)",
            record.token_prefix, record.status,
        )
        raise _gone()

    if decision == "approve":
        grants = _normalize_grants(record, body.get("grants"))
        try:
            with db_transaction() as db:
                updated = pending_install.mark_approved(
                    db, record.token, grants=grants,
                )
        except PendingInstallStateError as exc:
            # Lost a race with another decision on the same token.
            logger.warning(
                "Concurrent decision on package install request %s: %s",
                record.token_prefix, exc,
            )
            raise HTTPException(
                status_code=409,
                detail="This request was decided by someone else first",
            ) from exc
        logger.warning(
            "Operator APPROVED package install request %s; grants=%s",
            updated.token_prefix, grants,
        )
        work_id = _enqueue_install(updated)
        return JSONResponse(content={
            "status": updated.status,
            "decision": "approve",
            "recorded": True,
            "queued": work_id is not None,
            "grants": grants,
        })

    try:
        with db_transaction() as db:
            updated = pending_install.mark_rejected(
                db, record.token,
                reason="rejected by the operator on the approval page",
            )
    except PendingInstallStateError as exc:
        logger.warning(
            "Concurrent decision on package install request %s: %s",
            record.token_prefix, exc,
        )
        raise HTTPException(
            status_code=409,
            detail="This request was decided by someone else first",
        ) from exc
    logger.warning(
        "Operator REJECTED package install request %s; nothing installed",
        updated.token_prefix,
    )
    _cleanup_quarantine(updated)
    return JSONResponse(content={
        "status": updated.status,
        "decision": "reject",
        "recorded": True,
        "queued": False,
    })


routes = [
    Route(
        f"{REQUEST_URL_PREFIX}{{token}}/decide",
        submit_decision,
        methods=["POST"],
    ),
    Route(f"{REQUEST_URL_PREFIX}{{token}}", view_request, methods=["GET"]),
]

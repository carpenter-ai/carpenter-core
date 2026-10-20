"""Chat tool: ask the operator to approve installing capability packages.

``request_package_install(sources)`` installs nothing.  It resolves the
named sources, fetches them into quarantine, builds a preview without
executing any of the fetched code, records a pending request and returns
a one-time URL where a human decides.  If they approve, the platform
performs the install itself, out of the model turn, and reports back into
the conversation.

Why a request instead of an install (DESIGN D11): the platform's chat
confirmation seam is synchronous with no timeout and no conversation id,
and an operator's "yes" typed in chat while a turn is blocked is
persisted and then dropped, so a blocking confirmation handler cannot
work.  The one proven human yes/no mechanism here is a one-time link.
This is also why the existing ``install_package`` tool is operator-only
and refused when no confirmation handler is registered — on this
platform, none is.

Three things about this module are load-bearing:

* ``trust_boundary="platform"`` is forced: a ``"chat"``-boundary tool may
  not declare write capabilities, and this one writes to the filesystem
  (the quarantine) and the database (the request row).  A
  platform-boundary tool whose name is NOT in
  ``carpenter.chat_tool_registry.PLATFORM_TOOLS`` makes tool loading
  raise at startup, so that entry ships in the same commit as this file.
  **If the platform refuses to start naming this tool, delete this
  file** — the name is gone from core's allowlist and this seed module
  is older than the core it is running against.
* ``requires_user_confirm=False`` is the whole point.  ``True`` with no
  registered confirmation handler returns the generic "cannot be
  executed on this platform" refusal, which is exactly the dead end the
  approval link exists to avoid.
* The tool is refused to ARC agents, by core
  (``chat_tool_registry.CHAT_ONLY_TOOLS``) and again here.  An arc runs
  unattended and may be acting on content a third party wrote, and the
  approval link goes back to a conversation on the assumption a person
  is reading it.

``always_available=False`` keeps the tool out of the cached prompt
prefix.  At a 200k context window every tool fits, but a platform
configured with ``context_window <= 16384`` offers only the five-tool
ultra-core set and this tool is invisible there; the lever is
``tool_lists.core_tools_add`` in ``config.yaml``.

Nothing in this module reads or logs a credential, and the approval
token is never returned to the model in a form it could repeat: only the
URL is, and that URL is the credential — which is why the reply says to
hand it to a person rather than act on it.
"""

from __future__ import annotations

from carpenter.chat_tool_loader import FinalReply, chat_tool


def _format_package(entry: dict) -> list[str]:
    """One package's lines in the reply the operator is about to read."""
    manifest = entry.get("manifest", {})
    name = manifest.get("name", "?")
    version = manifest.get("version", "?")
    tree_hash = str(entry.get("tree_hash", ""))[:12]
    lines = [
        f"  • {name} v{version} (hash {tree_hash}) from "
        f"{entry.get('url_redacted') or entry.get('spec', '?')}",
    ]
    resolved = entry.get("resolved") or ""
    if resolved:
        lines.append(f"      pinned at {resolved[:12]}")
    caps = entry.get("capabilities") or []
    if caps:
        verbs = ", ".join(str(c.get("verb", "?")) for c in caps)
        lines.append(
            f"      declares {len(caps)} PLATFORM-LEVEL capability "
            f"verb(s): {verbs} — granting them is a separate tick box on "
            f"the page",
        )
    if entry.get("write_consent_required"):
        lines.append(
            "      ships write-capable chat tools, which stay off unless "
            "the page's opt-in is ticked",
        )
    creds = entry.get("credentials") or []
    if creds:
        names = ", ".join(
            var for cred in creds for var in cred.get("env_vars", [])
        )
        lines.append(
            f"      needs credential values you supply separately: "
            f"{names or 'see the page'}",
        )
    prior = entry.get("already_installed")
    if prior:
        lines.append(
            f"      already installed at v{prior.get('version')} "
            f"(hash {str(prior.get('hash') or '')[:12]}) — this would "
            f"replace it",
        )
    return lines


@chat_tool(
    description=(
        "Ask the operator to approve installing one or more capability "
        "packages from remote sources. This does NOT install anything: it "
        "fetches each source into a quarantine directory, reads its "
        "manifest WITHOUT running any of its code, and returns a one-time "
        "link where a human approves or rejects the whole set. Pass each "
        "source as a spec string: "
        "'git+https://host/org/repo.git#subdir=packages/<name>' (optionally "
        "'@<tag-or-commit>'), an 'https://host/path/pkg.tar.gz' archive, or "
        "a bare package name when the operator has configured package "
        "sources. Use this when a person asks for a package to be "
        "installed. Give them the link and stop; the platform installs the "
        "package itself if they approve, and reports the outcome here. Do "
        "not call this repeatedly: the operator sees one page per call."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 5,
                "description": (
                    "Package source specs to request, in install order. "
                    "One approval covers the whole list, so only include "
                    "packages the person actually asked for."
                ),
            },
        },
        "required": ["sources"],
    },
    # The quarantine directory and the request row.  Both are platform
    # state, which is why this tool cannot live at the chat boundary.
    capabilities=["filesystem_write", "database_write"],
    trust_boundary="platform",
    always_available=False,
    requires_user_confirm=False,
)
def request_package_install(tool_input, **kwargs):
    """Mint an approval link for a package install.  Installs nothing."""
    from carpenter.packages.install_requests import (
        InstallRequestError,
        InstallRequestRefused,
        create_install_request,
    )

    # Belt and braces: core refuses this for an arc agent
    # (chat_tool_registry.CHAT_ONLY_TOOLS), and this seed module may be
    # running against an older core that does not.
    if kwargs.get("executor_arc_id") is not None:
        return (
            "Error: 'request_package_install' can only be called from a "
            "chat conversation with a person in it, not by an arc agent. "
            "Ask the user to request this in chat themselves."
        )

    sources = tool_input.get("sources")
    try:
        request = create_install_request(
            sources,
            conversation_id=kwargs.get("conversation_id"),
        )
    except InstallRequestRefused as exc:
        # A plain string, so the normal flow continues and the model can
        # explain the refusal in its own words.
        return f"Package install request refused: {exc}"
    except InstallRequestError as exc:
        return f"Package install request failed: {exc}"
    except Exception as exc:  # noqa: BLE001 — never raise into the loop
        return f"Package install request failed: {exc}"

    lines = [
        "Nothing has been installed. A package install needs a person to "
        "approve it, so I have prepared a request:",
        "",
    ]
    for entry in request.preview:
        lines.extend(_format_package(entry))
    lines += [
        "",
        "Approve or reject the whole set here (the link works once and "
        f"expires {request.record.expires_at}):",
        f"    {request.url}",
    ]
    if not request.url_is_absolute:
        lines += [
            "",
            "That link is a path, not a full URL: this platform has no "
            "'public_base_url' configured, so I cannot say which host to "
            "put in front of it. Open it in a browser already pointed at "
            "this platform, or ask the operator to set 'public_base_url' "
            "in config.yaml so future links are complete.",
        ]
    lines += [
        "",
        "The page shows where the bytes came from, the tree hash, what "
        "the package declares and the message this request was made for. "
        "If you did not ask for this, reject it.",
    ]
    # A FinalReply ends the turn with no further model call, so this text
    # reaches the person verbatim: a model paraphrase of a security
    # decision surface is not worth the round trip.
    return FinalReply("\n".join(lines))

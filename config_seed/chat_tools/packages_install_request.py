"""Chat tools: find capability packages, and ask to install one.

Two tools, deliberately in one module because they are two halves of
one workflow: ``list_available_packages`` says what a configured source
offers, and ``request_package_install`` asks a human to approve
installing something from it.  Neither installs anything by itself and
neither runs any of the fetched code.

``list_available_packages(source=None)`` is read-only and exists so a
person can say "install the email package" without first finding a URL
to paste: it reads each candidate's ``manifest.yaml`` out of a
quarantined fetch, caches the listing, and hands back the exact spec
strings the install tool takes.  It is NOT refused to arc agents — it
reads metadata, it can only be pointed at a source the operator already
configured, and the listing cache's TTL bounds how often it can make the
platform talk to a remote at all.

``request_package_install(sources)`` installs nothing.  It resolves the
named sources, fetches them into quarantine, builds a preview without
executing any of the fetched code, records a pending request and returns
a one-time URL where a human decides.  If they approve, the platform
performs the install itself, out of the model turn, and reports back into
the conversation.

Why a request instead of an install (D11 in the design doc
``docs/2026-10-21_package-remote-sources-design.md``): the platform's chat
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
        "",
        "Approving requires being signed in to this platform: open the "
        "link from the Carpenter web UI, or with your platform token. "
        "The link alone shows you what was requested; it cannot approve "
        "anything, and neither can I.",
    ]
    # A FinalReply ends the turn with no further model call, so this text
    # reaches the person verbatim: a model paraphrase of a security
    # decision surface is not worth the round trip.
    return FinalReply("\n".join(lines))


def _format_listing(listing) -> list[str]:
    """One source's packages, with the spec string for each."""
    lines = [
        f"Source {listing.source!r} ({listing.url}) — listing read from "
        f"{listing.resolved[:12] or 'an unpinned revision'}, fetched "
        f"{listing.fetched_at}:",
    ]
    if not listing.packages:
        lines.append("  (this source offers no capability packages)")
    for pkg in listing.packages:
        summary = f" — {pkg.description}" if pkg.description else ""
        lines.append(f"  • {pkg.name} v{pkg.version}{summary}")
        lines.append(f"      spec: {pkg.spec}")
    for problem in listing.errors:
        lines.append(f"  ! {problem}")
    return lines


@chat_tool(
    description=(
        "List the capability packages a configured package source offers, "
        "so a person can ask for one by name instead of pasting a URL. "
        "Read-only: it reads each package's manifest and NOTHING else — no "
        "package code is imported or run, and nothing is installed. Call "
        "it with no arguments to see which sources the operator has "
        "configured and how many packages each one offers; call it with "
        "source='<alias>' to see that source's packages, each with the "
        "exact spec string to pass to request_package_install. The reply "
        "says which commit or digest the listing was read from and when."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": (
                    "Alias of a configured package source. Omit to list "
                    "the configured sources themselves."
                ),
            },
        },
        "required": [],
    },
    # Reads config, the listing cache and the fetched manifests.  The one
    # write this tool causes is the platform's own listing cache file,
    # which is why it sits at the platform boundary rather than the chat
    # one (a chat-boundary tool may not write at all) while declaring
    # only the read capabilities the caller actually gets.
    capabilities=["config_read", "filesystem_read"],
    trust_boundary="platform",
    always_available=False,
    requires_user_confirm=False,
)
def list_available_packages(tool_input, **kwargs):
    """Describe what the configured package sources offer."""
    from carpenter.packages.discover import (
        DiscoveryError,
        cached_listing,
        list_remote_packages,
        list_source_aliases,
    )

    nothing_happened = (
        "Nothing was installed and none of this package code was run: a "
        "listing reads manifests only. To install one, call "
        "request_package_install with the spec string above and give the "
        "person the approval link it returns."
    )

    source = (tool_input or {}).get("source")
    try:
        if source:
            # Never with refresh=True: the cached listing's TTL is what
            # keeps a curious agent from making the platform clone a
            # remote on every question.
            listing = list_remote_packages(str(source))
            lines = _format_listing(listing)
        else:
            aliases = list_source_aliases()
            if not aliases:
                return (
                    "No package source is configured, so there is nothing "
                    "to list. An operator adds one to the "
                    "'package_sources' config key (name, url and, for a "
                    "repository holding many packages, a subdir_template "
                    "such as 'packages/{name}'), and adds its host to "
                    "'web_egress_allowlist' when it is not on the public "
                    "internet. Nothing was installed."
                )
            lines = ["Configured package sources, in search order:"]
            for alias in aliases:
                cached = cached_listing(alias)
                if cached is None:
                    lines.append(
                        f"  • {alias} — not listed yet; ask me to list "
                        f"this source by name and I will read it.",
                    )
                    continue
                pin = cached.resolved[:12] or "an unpinned revision"
                lines.append(
                    f"  • {alias} — {len(cached.packages)} package(s), "
                    f"read from {pin} and last fetched "
                    f"{cached.fetched_at}",
                )
            lines.append(
                "Counts come from the cached listings, not a fresh fetch. "
                "Ask for one source by name for its packages and their "
                "install specs.",
            )
    except DiscoveryError as exc:
        return f"Could not list packages: {exc}. Nothing was installed."
    except Exception as exc:  # noqa: BLE001 — never raise into the loop
        return f"Listing packages failed: {exc}. Nothing was installed."

    lines += ["", nothing_happened]
    return "\n".join(lines)

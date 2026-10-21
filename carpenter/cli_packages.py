"""Operator CLI for capability-package management.

Provides ``python3 -m carpenter packages <subcommand>`` with four
subcommands:

* ``install <spec>`` (or ``--path <dir>``) — install a capability
  package from a local directory **or a remote source**, previewing the
  manifest and (for packages declaring ``platform_capabilities``) gating
  the install on PLATFORM-LEVEL TRUST consent.
* ``update <name> [--ref <ref>]`` — re-fetch the source the install
  recorded, at its ref, and apply it: a three-way merge that keeps the
  operator's edits to the installed tree where it can, reports the
  conflicts it cannot merge, and says plainly when there is no pristine
  version to merge against.  ``--ref`` follows a different ref instead —
  the way to move a package from one tag to the next — and the new ref
  is REMEMBERED as the package's ``source_ref``, so later updates follow
  it too.
* ``uninstall <name>`` — remove an installed package (dir + DB rows +
  capability grants).
* ``list`` — show installed packages and their granted capability verbs.

This fills the gap left by the chat-tool ``install_package``: that tool
passes no ``capability_input_fn``, so a package that declares
``platform_capabilities`` always raises ``InstallError`` (the
interactive-only trust-ack refuses to auto-grant). The operator CLI runs
on a tty and can therefore obtain the explicit consent the trust-ack
requires (interactively, or via the explicit ``--accept-capabilities``
flag).

**What ``install`` accepts** is exactly the grammar of
:mod:`carpenter.packages.sources`, with no CLI-only subset:

* a bare package name, resolved through the configured
  ``package_sources`` (declaration order IS search order) and otherwise
  through the legacy local source tree — either of which may be remote;
* an absolute path or ``file://`` URL naming a package directory, which
  is also what ``--path`` takes;
* ``git+https://host/org/repo.git[@ref][#subdir=rel/path]``;
* ``https://host/path/pkg.tar.gz[#sha256=<64 lowercase hex>]`` (``.tgz``
  and ``.tar`` too).

A remote spec is materialised by
:func:`carpenter.packages.fetch.fetch_source` — the one fetch entry
point, which owns the ``web_egress_allowlist`` gate, the per-call
quarantine under ``{base_dir}/cache/package-staging``, the resource caps
(:func:`carpenter.packages.install_requests.limits_for`), credential
injection by variable name and the tree hardening — and the package is
installed from that quarantined tree, which is removed again on **every**
exit path: success, refusal, failure or ``KeyboardInterrupt``.  Nothing
about fetching, extracting, hardening or hashing is reimplemented here.

Two things that are deliberately *not* here.  There is **no approval
link**: the operator at this terminal is the human the chat path's
approval page exists to ask, so the preview plus the consent prompts
below are the whole gate, and the pending-request machinery is not
imported.  And the preview is built with
:func:`carpenter.packages.static_preview.inspect_package_chat_tools` —
an ``ast.parse``, never
``installer.classify_package_chat_tools``, which imports the package's
modules.  For a tree the operator has not yet consented to, importing it
would run remote code *before* the prompt meant to inform them; see the
module docstring of :mod:`carpenter.packages.install_requests`, which is
careful about the same thing.

Provenance is recorded exactly as the approved-install path records it
(``source_kind`` / ``source_url`` redacted / ``source_ref`` /
``source_resolved`` / ``source_subdir`` / ``source_id`` / ``fetched_at``
/ ``shipped_hash``), because they are the same
:func:`carpenter.packages.installer.install_package` call with the same
``provenance`` and ``resolved`` arguments.  A CLI install is therefore
updatable: ``packages update`` rebuilds its spec from those columns.

The command functions take an ``argv`` list (matching the dispatch style
of :func:`carpenter.__main__._cmd_setup_credential`) and return an int
exit code, so they are directly unit-testable without a subprocess.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

#: A leading URL scheme, used only to tell an operator who passed a
#: remote source to ``--path`` which flag they wanted.  The grammar in
#: :mod:`carpenter.packages.sources` remains the only authority on what
#: a spec means; this just avoids resolving ``git+https://…`` as a
#: relative directory and reporting "directory not found".
_SCHEME_RE = re.compile(r"^(?:git\+)?[A-Za-z][A-Za-z0-9+.\-]*://")


# ── source / dest path conventions ──────────────────────────────────
#
# Both of these delegate: the source grammar lives in
# ``carpenter.packages.sources`` and the destination convention in
# ``carpenter.packages.installer``, so this CLI and the install chat tool
# resolve identically instead of each keeping its own copy (they once
# disagreed about ``base_dir``).


def _spec_for(source_name: str | None, path: str | None):
    """Parse what the operator named into a canonical ``SourceSpec``.

    One parse for both routes, and the result is both the thing that is
    fetched and the provenance that is recorded — so the install row can
    never describe a different source from the one the bytes came from.

    ``--path`` is resolved to an absolute path first and then parsed as a
    local spec, which is what makes ``--path ./pkg`` and the absolute
    path it expands to one source with one ``source_id``.

    Args:
        source_name: The positional spec, or ``None`` when ``--path``
            was used.
        path: The ``--path`` value, or ``None``.

    Returns:
        A :class:`carpenter.packages.sources.SourceSpec`.  Its ``kind``
        is what decides whether anything is fetched.

    Raises:
        SourceSpecError: the spec is not one of the accepted forms, or
            ``--path`` was given a remote source.  The message states
            the accepted forms, or names the argument that takes one.
    """
    from .packages.sources import SourceSpecError, parse_source

    if not path:
        return parse_source(source_name or "")

    text = path.strip()
    if text.lower().startswith("file://"):
        # A local directory written as a URL: the grammar handles it, and
        # resolving it as a filesystem path would mangle it.
        return parse_source(text)
    if _SCHEME_RE.match(text):
        raise SourceSpecError(
            f"--path takes a local directory, and {text!r} names a "
            f"remote source. Pass it as the positional <source> argument "
            f"instead, which does fetch a remote source.",
        )
    return parse_source(str(Path(text).expanduser().resolve()))


def _source_dir_for(source_name: str) -> Path:
    """Resolve a source spec to a local package directory.

    Kept for callers that only want the local case (and for symmetry
    with the pre-remote CLI).  A remote spec has no directory until it
    is fetched, so it is refused here rather than guessed at — the
    install path uses :func:`_spec_for` and fetches instead.

    Raises:
        SourceSpecError: the spec is not valid, or it resolves to a
            remote source, which has no local directory to name.
    """
    from .packages.sources import SourceSpecError, describe, parse_source

    spec = parse_source(source_name)
    if spec.kind != "local" or spec.path is None:
        raise SourceSpecError(
            f"source {spec.raw!r} resolves to a remote source "
            f"({describe(spec)}), which has no local directory until it "
            f"is fetched",
        )
    return spec.path


@contextmanager
def _fetched(spec):
    """Fetch a remote ``spec`` into quarantine and always clean it up.

    Delegates wholly to :func:`carpenter.packages.fetch.fetch_source`,
    which is the only thing in the platform that reaches a package
    source: it runs the ``web_egress_allowlist`` gate before any
    fetcher, creates the per-call quarantine under
    ``{base_dir}/cache/package-staging``, enforces the
    :class:`~carpenter.packages.fetch.FetchLimits` this CLI takes from
    :func:`carpenter.packages.install_requests.limits_for` (one set of
    numbers for both install paths, not two), injects a configured
    source's ``credential_ref`` by variable name, and hardens the tree.

    The quarantine is removed in a ``finally``, so it goes whether the
    install succeeded, was refused, raised, or the operator pressed
    ctrl-c at a consent prompt.  A *failed* fetch leaves nothing to
    remove — the dispatcher deletes its own quarantine before raising —
    so this contract holds on that path too.

    Yields:
        The :class:`~carpenter.packages.fetch.FetchResult`.

    Raises:
        FetchError: the egress gate refused the destination, a cap was
            exceeded, or the fetch failed.  The message is the
            dispatcher's or the fetcher's own; it already names the host
            and the config key when the gate was the refusal, and is
            quoted rather than reworded.
    """
    from .packages import fetch as fetch_mod
    from .packages.install_requests import limits_for
    from .packages.sources import describe

    print(f"  Fetching {describe(spec)} ...", file=sys.stderr)
    result = fetch_mod.fetch_source(spec, limits=limits_for())
    try:
        yield result
    finally:
        fetch_mod.cleanup(result)


def _install_destination_for(name: str) -> Path:
    """Install destination for a package: ``<base_dir>/packages/<name>``."""
    from .packages.installer import default_install_dest

    return default_install_dest(name)


def _package_env_path(name: str) -> Path:
    """Per-package credential .env path: ``<base>/config/packages/<name>/.env``."""
    try:
        from . import config
        base_dir = config.CONFIG.get("base_dir")
    except Exception:  # noqa: BLE001
        base_dir = None
    base = Path(base_dir) if base_dir else Path(os.path.expanduser("~/carpenter"))
    return base / "config" / "packages" / name / ".env"


# ── manifest preview ────────────────────────────────────────────────


def _print_manifest_preview(
    manifest,
    pkg_hash: str,
    *,
    stream,
    static=None,
    source=None,
    resolved: str = "",
    fetched=None,
) -> None:
    """Print a human-readable summary of what installing this package does.

    Args:
        source: optional :class:`carpenter.packages.sources.SourceSpec`
            the tree came from.  Given, the preview names the source,
            what the fetch resolved to (git commit sha40 or archive
            sha256) and what it transferred, so the operator decides
            about *these* bytes from *that* remote rather than about a
            package name.  Built from the canonical, credential-free
            fields, so it can never print a token.
        resolved: What pins the fetched bytes, from
            :attr:`carpenter.packages.fetch.FetchResult.resolved`.
        fetched: Optional :class:`~carpenter.packages.fetch.FetchResult`,
            for the transferred byte/file counts.
        static: optional
            :class:`carpenter.packages.static_preview.StaticPreview`.
            When provided, the preview enumerates the package's chat
            tools grouped by capability (read-only — will register; vs
            write/effectful — gated, listing each tool + its write
            capabilities) so the operator's write opt-in is informed.

            That list is read from the source with an AST parse; the
            package's own code is never executed to build this preview,
            which is what makes it safe to show BEFORE the operator has
            agreed to install.  The price is that a non-literal
            declaration cannot be read, so the preview prints every such
            value as explicitly undetermined rather than guessing.
    """
    p = lambda *a: print(*a, file=stream)  # noqa: E731
    p("")
    p(f"  Package : {manifest.name} v{manifest.version}")
    p(f"  Hash    : {pkg_hash[:12]}...")
    desc = (manifest.description or "").strip().splitlines()
    if desc:
        p(f"  About   : {desc[0]}")
    if source is not None:
        from .packages.sources import describe

        p(f"  Source  : {describe(source)}")
        if resolved:
            p(f"  Resolved: {resolved}")
        if fetched is not None and fetched.staging is not None:
            p(
                f"  Fetched : {fetched.bytes_fetched} byte(s) in "
                f"{fetched.files_fetched} file(s), into a quarantine that "
                f"is removed when this command exits"
            )
    p("")
    p("  Contributes:")
    p(f"    chat tools           : {len(manifest.chat_tools)}")
    p(f"    arc templates        : {len(manifest.arc_templates)}")
    p(f"    judge handlers       : {len(manifest.judge_handlers)}")
    p(f"    data models          : {len(manifest.data_models)}")
    p(f"    kb articles          : {len(manifest.kb_articles)}")
    p(f"    trigger subscriptions: {len(manifest.trigger_subscriptions)}")
    p(f"    triggers             : {len(manifest.triggers)}")
    p(f"    allowlist proposals  : {len(manifest.allowlist_proposals)}")

    # Chat tools grouped by capability so the operator's write opt-in is
    # informed.  Read-only tools register by default; write/effectful
    # tools are GATED and only register with an explicit --allow-write-
    # chat-tools opt-in.
    if static is not None:
        from .packages.static_preview import (
            NOT_EXECUTED_NOTICE,
            UNRESOLVED_NOTICE,
        )

        ro, wr = static.as_classified()
        p("")
        p("  Chat tools by capability:")
        p(f"    {NOT_EXECUTED_NOTICE}")
        p(f"    read-only (register by default): {len(ro)}")
        for t in ro:
            caps = ", ".join(t["capabilities"]) or "pure"
            p(f"      • {t['name']}  [{caps}]")
        if wr:
            p("")
            p(
                "    write / effectful (GATED — require "
                "--allow-write-chat-tools):"
            )
            for t in wr:
                wcaps = ", ".join(t["write_capabilities"])
                confirm = (
                    " (requires user confirm)"
                    if t["requires_user_confirm"] else ""
                )
                p(f"      • {t['name']}  write caps: {wcaps}{confirm}")
        else:
            p("    write / effectful: 0")
        refused = static.platform_boundary_tools
        if refused:
            p("")
            p(
                "    declares trust_boundary='platform' (REFUSED at load, "
                f"whatever you opt into): {', '.join(refused)}"
            )
        if static.unresolved:
            p("")
            p(f"    NOT DETERMINED ({len(static.unresolved)}):")
            p(f"    {UNRESOLVED_NOTICE}")
            for note in static.unresolved:
                p(f"      • {note}")

    # Credential requirements (env-var keys the package needs).
    creds = manifest.credential_requirements
    if creds:
        p("")
        p("  Declared credential requirements:")
        for c in creds:
            kind = getattr(c, "kind", "?")
            prefix = getattr(c, "env_key_prefix", "?")
            if kind == "env":
                keys = ", ".join(
                    f"{prefix}_{s}" for s in getattr(c, "required_keys", ())
                )
                p(f"    • env ({c.provider}): {keys}")
            elif kind == "oauth":
                p(
                    f"    • oauth ({c.provider}): {prefix}_* "
                    f"(authorize via OAuth flow)"
                )

    # Platform capabilities — the trust-bearing part. Make it prominent.
    caps = manifest.platform_capabilities
    if caps:
        p("")
        p("  " + "=" * 68)
        p("  PLATFORM CAPABILITIES (grants PLATFORM-LEVEL TRUST):")
        p("  " + "=" * 68)
        for cap in caps:
            g = cap.grant
            host_var = f"{g.credential_ref}_{g.host_from}"
            p(
                f"    • verb {cap.verb!r} (kind={cap.kind}) — "
                f"handler {cap.module}:{cap.handler}"
            )
            p(
                f"        egress: {g.protocol}://<{host_var}>:{g.port}  "
                f"credential: {g.credential_ref}_*"
            )
    p("")


# ── install ─────────────────────────────────────────────────────────


def _cmd_install(argv: list[str]) -> int:
    """Handle: python3 -m carpenter packages install <spec|--path dir>."""
    parser = argparse.ArgumentParser(
        prog="python3 -m carpenter packages install",
        description=(
            "Install a capability package from a local directory or a "
            "remote source. Accepted forms for <source>: a bare package "
            "name, resolved through the configured package sources "
            "(config package_sources, in list order) and otherwise under "
            "~/repos/carpenter-packages/packages/<name>/; an absolute "
            "path or file:// URL naming a package directory; "
            "git+https://host/org/repo.git[@ref][#subdir=rel/path]; or "
            "https://host/path/pkg.tar.gz[#sha256=<64 lowercase hex>] "
            "(.tgz and .tar too). Plain http:// is accepted only for a "
            "loopback host. A remote source is fetched into a quarantine "
            "under <base_dir>/cache/package-staging/, which is removed "
            "again however this command exits; its host must be listed in "
            "the web_egress_allowlist config key. The package installs to "
            "<base_dir>/packages/<manifest_name>/ either way, and the "
            "source is recorded so `packages update` can re-fetch it."
        ),
    )
    parser.add_argument(
        "source_name", nargs="?", metavar="source",
        help="Package source: a bare name (resolved through the configured "
             "package sources, else under "
             "~/repos/carpenter-packages/packages/), an absolute path or "
             "file:// URL, git+https://host/org/repo.git[@ref]"
             "[#subdir=rel/path], or https://host/path/pkg.tar.gz"
             "[#sha256=<64 hex>].",
    )
    parser.add_argument(
        "--path",
        help="Install from an explicit local source directory. Equivalent "
             "to naming that directory as <source>; it accepts no remote "
             "form.",
    )
    parser.add_argument(
        "--accept-capabilities", action="store_true",
        help="Explicit command-line consent to grant PLATFORM-LEVEL TRUST "
             "for any platform_capabilities the package declares. Required "
             "to install a capability package non-interactively.",
    )
    parser.add_argument(
        "--allow-write-chat-tools", action="store_true",
        help="Explicit operator opt-in to enable the package's WRITE-capable "
             "chat tools (those declaring arc_create / external_effect / "
             "database_write / filesystem_write). The chat agent is read-only "
             "by default, so without this flag (and without confirming the "
             "interactive prompt) the package's write chat tools are GATED "
             "and not registered. SEPARATE from --accept-capabilities.",
    )
    args = parser.parse_args(argv)

    if bool(args.path) == bool(args.source_name):
        print(
            "ERROR: provide exactly one of <source> or --path <dir>.",
            file=sys.stderr,
        )
        return 2

    from .packages.fetch import FetchError
    from .packages.sources import SourceSpecError

    try:
        spec = _spec_for(args.source_name, args.path)
    except SourceSpecError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # A local source is the operator's own tree and the trust anchor for
    # the install: it is read in place, nothing is fetched and nothing is
    # quarantined (``LocalDirectoryFetcher`` would copy nothing either).
    if spec.kind == "local":
        return _install_from_tree(
            Path(spec.path or ""), spec=spec, resolved="", args=args,
        )

    # A remote source: one fetch, through the one fetch entry point, and
    # the quarantine goes on every exit path including a ctrl-c at a
    # consent prompt.  No approval link is minted — the operator at this
    # terminal is the human that flow exists to ask.
    try:
        with _fetched(spec) as fetched:
            return _install_from_tree(
                Path(fetched.tree), spec=spec,
                resolved=fetched.resolved, args=args, fetched=fetched,
            )
    except FetchError as exc:
        # Quoted, not reworded: the dispatcher's egress refusal already
        # names the host and the web_egress_allowlist key that fixes it,
        # and a fetcher's cap refusal names the cap it hit.
        print(
            f"ERROR: could not fetch {spec.raw!r}: {exc}", file=sys.stderr,
        )
        return 1


def _install_from_tree(
    source_path: Path, *, spec, resolved: str, args, fetched=None,
) -> int:
    """Preview, consent to, and install the package tree at ``source_path``.

    The one install body for both routes: ``source_path`` is either the
    operator's own directory or the quarantined tree a fetch produced,
    and from here on they are treated identically — same preview, same
    consent gates, same installer call, same provenance columns.  That
    sameness is the point: a CLI install from a remote must not produce a
    row ``packages update`` cannot use, and it cannot, because ``spec``
    and ``resolved`` go straight to
    :func:`carpenter.packages.installer.install_package`, exactly as
    :mod:`carpenter.packages.install_batch` passes them after an approved
    fetch.

    Args:
        source_path: The package root (the directory holding
            ``manifest.yaml``).
        spec: The :class:`~carpenter.packages.sources.SourceSpec` the
            tree came from, recorded as provenance.
        resolved: Git commit sha40 or archive sha256; ``""`` for local.
        args: The parsed ``install`` arguments.
        fetched: The :class:`~carpenter.packages.fetch.FetchResult`, for
            the preview's transfer counts.  ``None`` for a local source.

    Returns:
        The process exit code.
    """
    from .db import db_transaction
    from .packages import installer, static_preview
    from .packages.manifest import ManifestError, load_manifest
    from .packages.security import PackageSecurityError

    provenance = spec

    if not source_path.is_dir():
        print(
            f"ERROR: source package directory not found at {source_path}.",
            file=sys.stderr,
        )
        return 1
    manifest_file = source_path / "manifest.yaml"
    if not manifest_file.is_file():
        print(
            f"ERROR: source directory {source_path} has no manifest.yaml.",
            file=sys.stderr,
        )
        return 1

    # Load + preview the manifest before doing anything.
    try:
        manifest = load_manifest(manifest_file)
        pkg_hash = installer.compute_package_hash(source_path)
    except (ManifestError, installer.InstallError) as exc:
        print(f"ERROR: could not load source package: {exc}", file=sys.stderr)
        return 1

    # Classify the package's chat tools so the preview can group them by
    # capability and the operator's write opt-in is informed.  Read
    # STATICALLY (AST only): this preview is shown before the operator
    # has agreed to install anything, so importing the source tree to
    # build it would execute a not-yet-trusted package's module-level
    # code — the very decision the preview exists to inform.  Anything
    # the parse cannot settle is reported, never guessed.
    static = static_preview.inspect_package_chat_tools(source_path, manifest)
    write_tools = [t["name"] for t in static.as_classified()[1]]

    _print_manifest_preview(
        manifest, pkg_hash, stream=sys.stderr, static=static,
        # A local source needs no provenance line: the operator is
        # standing in the directory they named.  A remote one does.
        source=spec if spec.kind != "local" else None,
        resolved=resolved, fetched=fetched,
    )

    dest_path = _install_destination_for(manifest.name)

    # Decide the write-chat-tools opt-in posture.  The chat agent is
    # read-only by default; the package's write chat tools only register
    # when the operator explicitly opts in (--allow-write-chat-tools or,
    # interactively, by confirming the prompt).  Unlike platform
    # capabilities, NON-interactive without the flag does NOT fail — the
    # write chat tools are simply gated off (silently) and the package
    # still installs.
    #
    # The static read drives this gate, and it fails closed: when the
    # package declares chat tools but the parse could not settle
    # something about them, it is treated as write-capable.  A preview
    # that cannot prove a package is read-only must not install it as if
    # it were.
    allow_write_chat_tools = bool(args.allow_write_chat_tools)
    needs_write_consent = static_preview.write_consent_required(
        static, manifest,
    )
    if needs_write_consent and not allow_write_chat_tools:
        if write_tools:
            headline = (
                f"  Package {manifest.name!r} ships {len(write_tools)} "
                f"WRITE-capable chat tool(s): {', '.join(write_tools)}"
            )
        else:
            headline = (
                f"  Package {manifest.name!r} declares chat tools whose "
                f"capabilities could NOT be determined without running its "
                f"code ({len(static.unresolved)} item(s) above), so they are "
                f"treated as WRITE-capable"
            )
        if hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
            print("", file=sys.stderr)
            print(headline, file=sys.stderr)
            print(
                "  The chat agent is read-only by default. Enable these "
                "write chat tools for this package?",
                file=sys.stderr,
            )
            try:
                resp = input(
                    "  Type 'yes' to opt in (default: gated off): ",
                )
            except EOFError:
                resp = ""
            if isinstance(resp, str) and resp.strip().lower() == "yes":
                allow_write_chat_tools = True
                print(
                    "  Opted IN: write chat tools will register.",
                    file=sys.stderr,
                )
            else:
                print(
                    "  Gated OFF: write chat tools will NOT register "
                    "(re-install with --allow-write-chat-tools to enable).",
                    file=sys.stderr,
                )
        else:
            # Non-interactive without the flag → simply gated off.
            print(headline, file=sys.stderr)
            print(
                "  NOTE: write chat tools are GATED off (chat agent is "
                "read-only by default). Re-run with "
                "--allow-write-chat-tools to enable them.",
                file=sys.stderr,
            )

    # Decide the capability-consent posture.
    has_caps = bool(manifest.platform_capabilities)
    capability_input_fn = None
    if has_caps:
        verbs = ", ".join(c.verb for c in manifest.platform_capabilities)
        if args.accept_capabilities:
            # Explicit command-line operator consent: auto-confirm and echo
            # exactly which capabilities are being granted.
            print(
                "  --accept-capabilities: GRANTING PLATFORM-LEVEL TRUST for "
                f"verb(s): {verbs}",
                file=sys.stderr,
            )
            capability_input_fn = lambda *_a, **_k: "yes"  # noqa: E731
        elif not (hasattr(sys.stdin, "isatty") and sys.stdin.isatty()):
            # Non-interactive with declared capabilities and no explicit
            # flag: fail closed. Platform-level trust cannot be granted
            # without operator consent.
            print(
                f"ERROR: package {manifest.name!r} declares platform "
                f"capabilities ({verbs}) which grant PLATFORM-LEVEL TRUST. "
                "This cannot be granted non-interactively. Re-run on a "
                "terminal to confirm, or pass --accept-capabilities to "
                "consent explicitly on the command line.",
                file=sys.stderr,
            )
            return 1
        # else: interactive tty, no flag → install_package's
        # confirm_platform_capabilities() will prompt on the tty.

    try:
        with db_transaction() as db:
            result = installer.install_package(
                source_path, dest_path,
                conn=db,
                capability_input_fn=capability_input_fn,
                capability_prompt_stream=sys.stderr,
                allow_write_chat_tools=allow_write_chat_tools,
                # The provenance the install row records, and the pin of
                # the bytes it records it for.  Together with
                # ``shipped_hash`` (which install_package sets from the
                # tree hash) this is the whole of what ``packages
                # update`` later rebuilds its source spec from, and what
                # keys the pristine-archive cache by logical source.
                provenance=provenance,
                resolved=resolved,
            )
    except (installer.InstallError, ManifestError, PackageSecurityError) as exc:
        print(f"ERROR: install failed: {exc}", file=sys.stderr)
        return 1

    # Report success.
    verb = "Re-installed" if result.was_update else "Installed"
    print(
        f"{verb} {result.name} v{result.version} "
        f"(hash {result.hash[:12]}) at {result.dest_path}"
    )

    if spec.kind != "local":
        from .packages.sources import describe

        print(f"  Source   : {describe(spec)}")
        if resolved:
            print(f"  Resolved : {resolved}")
        print(
            f"  The source is recorded, so `python3 -m carpenter packages "
            f"update {result.name}` re-fetches it at its ref."
        )

    if result.platform_capabilities_granted:
        print("")
        print("  Granted platform capabilities (PLATFORM-LEVEL TRUST):")
        for cap in result.platform_capabilities_granted:
            g = cap.get("grant", {})
            print(
                f"    • {cap.get('verb')} (kind={cap.get('kind')}) → "
                f"egress {g.get('protocol')}://...:{g.get('port')}"
            )
    elif has_caps:
        print("")
        print(
            "  NOTE: platform capabilities were DECLINED; the package's "
            "capability verbs are NOT registered."
        )

    if write_tools:
        print("")
        if result.write_chat_tools_allowed:
            print(
                "  Write chat tools: ENABLED (operator opted in). The "
                "package's write-capable chat tools WILL register on "
                "next server start."
            )
        else:
            print(
                "  Write chat tools: GATED OFF (read-only default). The "
                "package's write-capable chat tools will NOT register. "
                "Re-install with --allow-write-chat-tools to enable."
            )

    if result.env_credential_requests:
        print("")
        print("  This package needs environment-variable credentials:")
        for req in result.env_credential_requests:
            print(f"    • {req.get('key')} (provider: {req.get('provider')})")
        print(
            f"  Put per-package credentials in "
            f"{_package_env_path(result.name)}"
        )
        print(
            "  (or supply them via the one-time credential request links "
            "shown in the server log)."
        )

    if result.allowlist_added:
        print("")
        print("  Allowlist entries merged into platform policy:")
        for ptype, value in result.allowlist_added:
            print(f"    • {ptype}: {value}")

    print("")
    print(
        "  Restart the server to load the package — the registry scans "
        "installed packages at startup."
    )
    print("")
    return 0


# ── update ──────────────────────────────────────────────────────────


def _print_update_plan(plan, *, stream) -> None:
    """Print what an update would do, before the operator decides."""
    from .packages.sources import describe
    from .packages.update import (
        STRATEGY_CLOBBER,
        STRATEGY_NOOP,
        STRATEGY_RECONCILE,
    )

    strategies = {
        STRATEGY_RECONCILE: (
            "three-way merge against the pristine tree of the installed "
            "version"
        ),
        STRATEGY_CLOBBER: (
            "REPLACE the installed tree — no pristine tree to merge "
            "against, so local edits are LOST"
        ),
        STRATEGY_NOOP: "nothing to do",
    }
    p = lambda *a: print(*a, file=stream)  # noqa: E731
    p("")
    p(f"  Package  : {plan.name}")
    p(f"  Version  : {plan.installed_version} -> {plan.fetched_version}")
    p(
        f"  Tree hash: {plan.installed_hash[:12]}... -> "
        f"{plan.fetched_hash[:12]}..."
    )
    p(f"  Source   : {describe(plan.source)}")
    if plan.ref_override:
        p(
            f"  Ref      : {plan.ref_override} (--ref; recorded as this "
            f"package's ref when the update is applied)"
        )
    if plan.resolved:
        p(f"  Resolved : {plan.resolved}")
    p(
        f"  Strategy : {plan.strategy} — "
        f"{strategies.get(plan.strategy, 'unknown')}"
    )
    if plan.local_edits:
        p(f"  Your changes ({len(plan.local_edits)}):")
        for path in plan.local_edits:
            p(f"    • {path}")
    if plan.conflicts:
        p("  " + "=" * 68)
        p(f"  CONFLICTS ({len(plan.conflicts)}) — changed by you AND "
          f"by the new version:")
        for path in plan.conflicts:
            p(f"    • {path}")
        p("  " + "=" * 68)
    if plan.notes:
        p("  Notes:")
        for note in plan.notes:
            p(f"    - {note}")
    p("")


def _cmd_update(argv: list[str]) -> int:
    """Handle: python3 -m carpenter packages update <name>."""
    from .db import db_connection, db_transaction
    from .packages import static_preview
    from .packages.manifest import ManifestError, load_manifest
    from .packages.update import (
        STRATEGY_NOOP,
        UpdateError,
        apply_update,
        cleanup_plan,
        plan_update,
    )

    parser = argparse.ArgumentParser(
        prog="python3 -m carpenter packages update",
        description=(
            "Update an installed capability package from the source its "
            "install recorded, following that source's ref — or the ref "
            "given by --ref, which is then REMEMBERED as the package's "
            "recorded ref. The plan is printed and confirmed before "
            "anything is written. Edits you made to the installed tree "
            "are merged where possible, reported where they conflict, "
            "and never discarded without saying so."
        ),
    )
    parser.add_argument(
        "name", help="Installed package name (manifest 'name' field).",
    )
    parser.add_argument(
        "--ref", metavar="REF",
        help="Follow this git ref (branch, tag or 40-hex commit) instead "
             "of the one the install recorded — how a package is moved "
             "from one tag to the next. The new ref is REMEMBERED: "
             "applying the update records it as the package's "
             "source_ref, so later updates follow it too, and "
             "source_resolved records the commit actually fetched. "
             "Refused for a package installed from a local directory, "
             "which has no ref to follow.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Apply the plan without asking. Required to update "
             "non-interactively, and it also accepts a plan that "
             "REPLACES the installed tree when there is no pristine "
             "version to merge against.",
    )
    parser.add_argument(
        "--accept-conflicts", action="store_true",
        help="Apply even when a file was changed both by you and by the "
             "new version. The NEW version's content wins every "
             "conflicting file and your edit there is discarded.",
    )
    args = parser.parse_args(argv)

    # Planning fetches, so it is deliberately NOT inside the write
    # transaction: the operator's confirmation happens between the two,
    # and holding SQLite's writer lock across a prompt would block every
    # other writer for as long as the operator takes to read.
    try:
        with db_connection() as db:
            plan = plan_update(args.name, conn=db, ref=args.ref)
    except UpdateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        # Show the fetched version the same way install shows a source
        # tree: from the manifest plus a STATIC read of its chat tools.
        # The package's own code is never executed to build this.
        try:
            manifest = load_manifest(plan.fetched_tree / "manifest.yaml")
            static = static_preview.inspect_package_chat_tools(
                plan.fetched_tree, manifest,
            )
            _print_manifest_preview(
                manifest, plan.fetched_hash, stream=sys.stderr,
                static=static,
            )
        except ManifestError as exc:  # pragma: no cover — planned already
            print(
                f"ERROR: the fetched package is unreadable: {exc}",
                file=sys.stderr,
            )
            return 1

        _print_update_plan(plan, stream=sys.stderr)

        if plan.strategy == STRATEGY_NOOP and not plan.ref_override:
            print(
                f"{plan.name} is already up to date at "
                f"{plan.installed_version} "
                f"(hash {plan.installed_hash[:12]}); nothing was changed."
            )
            return 0

        if plan.strategy == STRATEGY_NOOP:
            # The named ref ships the bytes already installed, so there
            # is nothing to install and nothing to confirm — no files
            # are replaced, so --yes is not asked for.  The apply still
            # runs, because the operator named a ref and that is a
            # request about the recorded source: dropping it here would
            # send the next update back to the old ref.  It writes
            # nothing at all if the row already names this ref.
            try:
                with db_transaction() as db:
                    result = apply_update(plan, conn=db)
            except UpdateError as exc:
                print(f"ERROR: update failed: {exc}", file=sys.stderr)
                return 1
            print(
                f"{plan.name} is already up to date at "
                f"{plan.installed_version} "
                f"(hash {plan.installed_hash[:12]}); no files were "
                f"changed."
            )
            print(
                f"  Recorded ref: {plan.ref_override} (pinned at "
                f"{result.resolved}); later updates follow it."
            )
            return 0

        if plan.conflicts and not args.accept_conflicts:
            print(
                f"ERROR: {len(plan.conflicts)} file(s) were changed both "
                f"by you and by the new version, so they cannot be "
                f"merged automatically: {', '.join(plan.conflicts)}. "
                f"Nothing was changed. Re-run with --accept-conflicts to "
                f"take the new version's content for those files "
                f"(discarding your edits there), or copy your changes "
                f"out of {plan.install_path} first.",
                file=sys.stderr,
            )
            return 1

        if not args.yes:
            if not (hasattr(sys.stdin, "isatty") and sys.stdin.isatty()):
                print(
                    "ERROR: refusing to apply an update "
                    "non-interactively without --yes.",
                    file=sys.stderr,
                )
                return 1
            try:
                resp = input("  Type 'yes' to apply this update: ")
            except EOFError:
                resp = ""
            if not (isinstance(resp, str) and resp.strip().lower() == "yes"):
                print("Update declined; nothing was changed.")
                return 0

        try:
            with db_transaction() as db:
                result = apply_update(
                    plan, conn=db, accept_conflicts=args.accept_conflicts,
                )
        except UpdateError as exc:
            print(f"ERROR: update failed: {exc}", file=sys.stderr)
            return 1
    finally:
        # Every exit from here owes the fetched tree a cleanup, and the
        # ones that reach apply_update have had it done for them —
        # cleanup_plan is idempotent, so saying it once covers both.
        cleanup_plan(plan)

    print(
        f"Updated {result.name} {result.from_version} -> "
        f"{result.to_version} (hash {result.to_hash[:12]}) at "
        f"{result.dest_path}"
    )
    if result.local_edits_preserved:
        print("")
        print("  Your edits were KEPT in:")
        for path in result.local_edits_preserved:
            print(f"    • {path}")
    if result.conflicts_overridden:
        print("")
        print("  Your edits were DISCARDED in favour of the new version:")
        for path in result.conflicts_overridden:
            print(f"    • {path}")
    if result.overwritten:
        print("")
        print(
            f"  {len(result.overwritten)} file(s) were overwritten "
            f"wholesale (no pristine version to merge against):"
        )
        for path in result.overwritten:
            print(f"    • {path}")
    if not result.archive_cached:
        print("")
        print(
            "  NOTE: the new version's pristine tree could not be "
            "cached, so the next update will have nothing to merge "
            "against."
        )
    print("")
    print(f"  Restart needed: {'yes' if result.needs_restart else 'no'}")
    print(f"  {result.restart_reason}.")
    print("")
    return 0


# ── uninstall ───────────────────────────────────────────────────────


def _cmd_uninstall(argv: list[str]) -> int:
    """Handle: python3 -m carpenter packages uninstall <name>."""
    from .db import db_transaction
    from .packages import installer

    parser = argparse.ArgumentParser(
        prog="python3 -m carpenter packages uninstall",
        description="Uninstall a previously-installed capability package.",
    )
    parser.add_argument(
        "name", help="Installed package name (manifest 'name' field).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Skip the non-terminal-arc safety check (operator override).",
    )
    parser.add_argument(
        "--preserve-state", action="store_true",
        help="Archive the package's package_state rows before the FK "
             "cascade wipes them (for a later restore on re-install).",
    )
    args = parser.parse_args(argv)

    try:
        with db_transaction() as db:
            record = installer.get_install_record(db, args.name)
            if record is None:
                print(
                    f"ERROR: package {args.name!r} is not installed.",
                    file=sys.stderr,
                )
                return 1
            granted = installer.granted_verbs_for_package(db, args.name)
            result = installer.uninstall_package(
                args.name, conn=db,
                force=args.force,
                archive_state=args.preserve_state,
            )
    except installer.InstallError as exc:
        print(f"ERROR: uninstall failed: {exc}", file=sys.stderr)
        return 1

    print(f"Uninstalled {result.name}")
    print(f"  Removed install dir : {result.removed_path}")
    print("  Removed DB rows     : installed_packages + templates")
    print(
        "  Reclaimed archives  : "
        f"{len(result.reclaimed_archives)} cached pristine archive(s)"
    )
    if granted:
        print(
            "  Dropped capability grants: " + ", ".join(sorted(granted))
        )
    state_note = (
        "archived to package_state_archive"
        if args.preserve_state
        else "wiped (FK cascade)"
    )
    print(f"  Per-package state   : {state_note}")
    print(
        "  Allowlist entries the package contributed are NOT removed "
        "(one-way ratchet)."
    )
    print("")
    print("  Restart the server to unload the package.")
    print("")
    return 0


# ── list ────────────────────────────────────────────────────────────


def _cmd_list(argv: list[str]) -> int:
    """Handle: python3 -m carpenter packages list."""
    from .db import db_connection
    from .packages import installer

    parser = argparse.ArgumentParser(
        prog="python3 -m carpenter packages list",
        description="List installed capability packages.",
    )
    parser.parse_args(argv)

    with db_connection() as db:
        records = installer.list_install_records(db)
        # Resolve granted verbs per package from the same connection.
        verbs_by_pkg = {
            r["name"]: sorted(
                installer.granted_verbs_for_package(db, r["name"])
            )
            for r in records
        }

    if not records:
        print("No capability packages installed.")
        return 0

    name_w = max([len("NAME")] + [len(r["name"]) for r in records])
    ver_w = max([len("VERSION")] + [len(r["version"]) for r in records])
    print(
        f"{'NAME':<{name_w}}  {'VERSION':<{ver_w}}  {'HASH':<12}  "
        f"CAPABILITY VERBS"
    )
    for r in records:
        verbs = verbs_by_pkg.get(r["name"], [])
        verbs_str = ", ".join(verbs) if verbs else "-"
        print(
            f"{r['name']:<{name_w}}  {r['version']:<{ver_w}}  "
            f"{r['hash'][:12]:<12}  {verbs_str}"
        )
    return 0


# ── dispatcher ──────────────────────────────────────────────────────


_SUBCOMMANDS = {
    "install": _cmd_install,
    "update": _cmd_update,
    "uninstall": _cmd_uninstall,
    "list": _cmd_list,
}


def cmd_packages(argv: list[str]) -> int:
    """Dispatch ``python3 -m carpenter packages <subcommand> ...``.

    Returns an int exit code (0 success, nonzero on any failure). The
    caller (``__main__.main``) translates this into ``sys.exit``.
    """
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "usage: python3 -m carpenter packages <command> [options]\n"
            "\n"
            "commands:\n"
            "  install <source> [--accept-capabilities] "
            "[--allow-write-chat-tools]\n"
            "  install --path DIR [--accept-capabilities] "
            "[--allow-write-chat-tools]\n"
            "  update <name> [--ref REF] [--yes] [--accept-conflicts]\n"
            "  uninstall <name> [--force] [--preserve-state]\n"
            "  list\n"
            "\n"
            "<source> for install is one of:\n"
            "  <name>                       a bare package name, resolved "
            "through the\n"
            "                               configured package_sources, "
            "else the local\n"
            "                               source tree\n"
            "  /abs/path | file:///abs/path a local package directory "
            "(same as --path)\n"
            "  git+https://host/org/repo.git[@ref][#subdir=rel/path]\n"
            "  https://host/path/pkg.tar.gz[#sha256=<64 hex>]   (.tgz, "
            ".tar too)\n"
            "\n"
            "A remote source is fetched into a quarantine that is removed "
            "when the\n"
            "command exits; its host must be in the web_egress_allowlist "
            "config key.\n",
            file=sys.stderr if (argv and argv[0] not in ("-h", "--help"))
            else sys.stdout,
        )
        return 0 if (argv and argv[0] in ("-h", "--help")) else 2

    sub = argv[0]
    handler = _SUBCOMMANDS.get(sub)
    if handler is None:
        print(
            f"ERROR: unknown packages subcommand {sub!r}. "
            f"Known: {', '.join(sorted(_SUBCOMMANDS))}.",
            file=sys.stderr,
        )
        return 2
    return handler(argv[1:])

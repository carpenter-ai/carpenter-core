"""Operator CLI for capability-package management.

Provides ``python3 -m carpenter packages <subcommand>`` with three
subcommands:

* ``install <source_name>`` (or ``--path <dir>``) — install a capability
  package, previewing the manifest and (for packages declaring
  ``platform_capabilities``) gating the install on PLATFORM-LEVEL TRUST
  consent.
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

The command functions take an ``argv`` list (matching the dispatch style
of :func:`carpenter.__main__._cmd_setup_credential`) and return an int
exit code, so they are directly unit-testable without a subprocess.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


# ── source / dest path conventions (mirror config_seed/chat_tools/packages.py)


def _source_dir_for(source_name: str) -> Path:
    """Conventional source path: ``~/repos/carpenter-packages/packages/<name>``."""
    return Path(
        os.path.expanduser(
            f"~/repos/carpenter-packages/packages/{source_name}"
        )
    )


def _install_destination_for(name: str) -> Path:
    """Install destination for a package: ``~/carpenter/packages/<name>``.

    Honours the platform ``base_dir`` config when set so tests (and
    relocated installs) land under the configured base rather than a
    hardcoded ``~/carpenter``.
    """
    try:
        from . import config
        base_dir = config.CONFIG.get("base_dir")
    except Exception:  # noqa: BLE001 — config may be unavailable in stripped builds
        base_dir = None
    if base_dir:
        return Path(base_dir) / "packages" / name
    return Path(os.path.expanduser(f"~/carpenter/packages/{name}"))


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
    read_only_tools=None,
    write_tools=None,
) -> None:
    """Print a human-readable summary of what installing this package does.

    Args:
        read_only_tools / write_tools: optional classified chat-tool
            lists from :func:`installer.classify_package_chat_tools`.
            When provided, the preview enumerates the package's chat
            tools grouped by capability (read-only — will register; vs
            write/effectful — gated, listing each tool + its write
            capabilities) so the operator's write opt-in is informed.
    """
    p = lambda *a: print(*a, file=stream)  # noqa: E731
    p("")
    p(f"  Package : {manifest.name} v{manifest.version}")
    p(f"  Hash    : {pkg_hash[:12]}...")
    desc = (manifest.description or "").strip().splitlines()
    if desc:
        p(f"  About   : {desc[0]}")
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
    if read_only_tools is not None or write_tools is not None:
        ro = read_only_tools or []
        wr = write_tools or []
        p("")
        p("  Chat tools by capability:")
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
    """Handle: python3 -m carpenter packages install <source_name|--path dir>."""
    from .db import db_transaction
    from .packages import installer
    from .packages.manifest import ManifestError, load_manifest
    from .packages.security import PackageSecurityError

    parser = argparse.ArgumentParser(
        prog="python3 -m carpenter packages install",
        description=(
            "Install a capability package. By default resolves the source "
            "from ~/repos/carpenter-packages/packages/<source_name>/ and "
            "installs to <base_dir>/packages/<manifest_name>/."
        ),
    )
    parser.add_argument(
        "source_name", nargs="?",
        help="Package directory name under ~/repos/carpenter-packages/packages/",
    )
    parser.add_argument(
        "--path",
        help="Install from an explicit source directory instead of the "
             "conventional source repo location.",
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
            "ERROR: provide exactly one of <source_name> or --path <dir>.",
            file=sys.stderr,
        )
        return 2

    source_path = (
        Path(args.path).expanduser().resolve()
        if args.path
        else _source_dir_for(args.source_name)
    )

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
    # capability and the operator's write opt-in is informed.  Best-effort:
    # a module that fails to import simply yields no entries.
    try:
        read_only_tools, write_tools = installer.classify_package_chat_tools(
            manifest,
        )
    except Exception:  # noqa: BLE001 — preview is best-effort
        read_only_tools, write_tools = [], []

    _print_manifest_preview(
        manifest, pkg_hash, stream=sys.stderr,
        read_only_tools=read_only_tools, write_tools=write_tools,
    )

    dest_path = _install_destination_for(manifest.name)

    # Decide the write-chat-tools opt-in posture.  The chat agent is
    # read-only by default; the package's write chat tools only register
    # when the operator explicitly opts in (--allow-write-chat-tools or,
    # interactively, by confirming the prompt).  Unlike platform
    # capabilities, NON-interactive without the flag does NOT fail — the
    # write chat tools are simply gated off (silently) and the package
    # still installs.
    allow_write_chat_tools = bool(args.allow_write_chat_tools)
    if write_tools and not allow_write_chat_tools:
        wnames = ", ".join(t["name"] for t in write_tools)
        if hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
            print("", file=sys.stderr)
            print(
                f"  Package {manifest.name!r} ships {len(write_tools)} "
                f"WRITE-capable chat tool(s): {wnames}",
                file=sys.stderr,
            )
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
            print(
                f"  NOTE: {len(write_tools)} write chat tool(s) ({wnames}) "
                "are GATED off (chat agent is read-only by default). "
                "Re-run with --allow-write-chat-tools to enable them.",
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
            "  install <source_name> [--path DIR] [--accept-capabilities] "
            "[--allow-write-chat-tools]\n"
            "  uninstall <name> [--force] [--preserve-state]\n"
            "  list\n",
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

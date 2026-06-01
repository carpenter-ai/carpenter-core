"""Chat tools for installing and uninstalling capability packages.

D24 stage 3a (B-min) ships two write-side tools alongside the existing
read-only ``list_packages``:

* ``install_package(source_name)`` — copies a package from
  ``~/repos/carpenter-packages/packages/<source_name>/`` to
  ``~/carpenter/packages/<name>/``, hashes the contents, records the
  install in the ``installed_packages`` SQL table, and (on next
  server restart) loads the package via the hash-pinned path.
* ``uninstall_package(name)`` — removes the install dir + DB rows.
  Refuses if any non-terminal arc still references a template the
  package shipped (D24 SD9).  Allowlists are NOT touched (SD5; the
  one-way ratchet).

Both tools use the standard chat-tool human-confirmation pattern
(``requires_user_confirm=True``) per D24 SD4.  The platform's
confirmation handler shows the operator the full ``tool_input`` dict
plus the platform-prepared summary in the ``preview`` field.

Trust-model notes:

* The tools have ``trust_boundary='platform'`` because they mutate
  platform state (filesystem + DB) and must always be available to
  the chat agent — extension/package-side tools are confined to the
  chat boundary, so platform-state-mutating tools have to live as
  platform-shipped chat tools.  See ``carpenter/chat_tool_loader.py``
  for the boundary contract.
* They live in ``config_seed/chat_tools/`` (not the package framework
  itself) because that's where chat tools live by convention; the
  package framework's ``carpenter/packages/`` module exposes the
  underlying ``install_package`` / ``uninstall_package`` Python API.
"""

from __future__ import annotations

import os
from pathlib import Path

from carpenter.chat_tool_loader import chat_tool


def _source_dir_for(source_name: str) -> Path:
    """Return the conventional source path for a package name.

    Stage 3a only knows about the local
    ``~/repos/carpenter-packages/packages/<name>/`` layout (SD1).
    Remote / signed packages are out of scope for D24.
    """
    return Path(
        os.path.expanduser(
            f"~/repos/carpenter-packages/packages/{source_name}"
        )
    )


def _install_destination_for(name: str) -> Path:
    """Return the install destination for a package name (SD2)."""
    return Path(
        os.path.expanduser(f"~/carpenter/packages/{name}")
    )


def _summarize_manifest(manifest, pkg_hash: str) -> str:
    """Human-readable preview shown in the confirmation dialog."""
    chat_n = len(manifest.chat_tools)
    tmpl_n = len(manifest.arc_templates)
    judges_n = len(manifest.judge_handlers)
    models_n = len(manifest.data_models)
    kb_n = len(manifest.kb_articles)
    subs_n = len(manifest.trigger_subscriptions)
    parts = [
        f"name: {manifest.name}",
        f"version: {manifest.version}",
        f"hash: {pkg_hash[:12]}...",
        f"description: {manifest.description}",
        f"chat tools: {chat_n}",
        f"arc templates: {tmpl_n}",
        f"judge handlers: {judges_n}",
        f"data models: {models_n}",
        f"kb articles: {kb_n}",
        f"trigger subscriptions: {subs_n}",
    ]
    return "\n".join(parts)


@chat_tool(
    description=(
        "Install a capability package from the local source repo "
        "(~/repos/carpenter-packages/packages/<source_name>/) into the "
        "platform's install directory (~/carpenter/packages/<name>/). "
        "Copies + hash-pins the contents per D24 SD3.  Requires user "
        "confirmation; the operator sees the manifest summary and "
        "package hash before the copy happens.  If the package is "
        "already installed, this re-installs (atomically swaps) the "
        "newer version into place.  Restart the server after install "
        "to pick up the new chat tools / templates / handlers."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "source_name": {
                "type": "string",
                "description": (
                    "Package directory name in "
                    "~/repos/carpenter-packages/packages/.  Typically "
                    "matches the manifest's 'name' field."
                ),
            },
        },
        "required": ["source_name"],
    },
    capabilities=["filesystem_write", "database_write"],
    trust_boundary="platform",
    always_available=False,
    requires_user_confirm=True,
)
def install_package(tool_input, **kwargs):
    """Install a capability package from the local source repo."""
    from carpenter.db import db_transaction
    from carpenter.packages import installer

    source_name = tool_input.get("source_name")
    if not source_name or not isinstance(source_name, str):
        return "Error: source_name (string) is required."

    source_path = _source_dir_for(source_name)
    if not source_path.is_dir():
        return (
            f"Error: source package directory not found at {source_path}. "
            f"Place the package under ~/repos/carpenter-packages/packages/ "
            f"and try again."
        )
    manifest_file = source_path / "manifest.yaml"
    if not manifest_file.is_file():
        return (
            f"Error: source directory {source_path} has no manifest.yaml."
        )

    # Best-effort summary for log surface (the actual confirmation
    # dialog is rendered by the platform's confirmation handler).
    try:
        from carpenter.packages import load_manifest
        manifest = load_manifest(manifest_file)
        pkg_hash = installer.compute_package_hash(source_path)
        preview = _summarize_manifest(manifest, pkg_hash)
    except Exception as exc:
        return f"Error parsing source package: {exc}"

    dest_path = _install_destination_for(manifest.name)

    try:
        with db_transaction() as db:
            result = installer.install_package(
                source_path, dest_path, conn=db,
            )
    except installer.InstallError as exc:
        return f"Install failed: {exc}"
    except Exception as exc:
        return f"Install failed: {exc}"

    verb = "Re-installed" if result.was_update else "Installed"
    return (
        f"{verb} {result.name} v{result.version} "
        f"(hash {result.hash[:12]}) at {result.dest_path}.\n"
        f"Manifest summary:\n{preview}\n"
        f"NOTE: restart the server to pick up the package's chat "
        f"tools / templates."
    )


@chat_tool(
    description=(
        "Uninstall a previously-installed capability package.  Removes "
        "the install dir at ~/carpenter/packages/<name>/ and the DB "
        "install record.  Refuses if any non-terminal arc still "
        "references a template the package shipped (D24 SD9); the "
        "operator must wait for those arcs to terminate or cancel "
        "them.  Allowlist entries the package contributed at install "
        "time are NOT removed (SD5; one-way ratchet).  Per-package "
        "mutable state (rows in ``package_state``) is WIPED by default; "
        "pass ``preserve_state=true`` to archive it for a later restore "
        "on re-install.  Requires user confirmation."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Package name (manifest 'name' field).  This is "
                    "the install-side identifier, not the source-repo "
                    "directory name."
                ),
            },
            "preserve_state": {
                "type": "boolean",
                "description": (
                    "If true, copy the package's package_state rows "
                    "into package_state_archive before the FK cascade "
                    "wipes them.  A future re-install can restore "
                    "state from the archive.  Default false (wipe)."
                ),
                "default": False,
            },
        },
        "required": ["name"],
    },
    capabilities=["filesystem_write", "database_write"],
    trust_boundary="platform",
    always_available=False,
    requires_user_confirm=True,
)
def uninstall_package(tool_input, **kwargs):
    """Uninstall a previously-installed capability package."""
    from carpenter.db import db_transaction
    from carpenter.packages import installer

    name = tool_input.get("name")
    if not name or not isinstance(name, str):
        return "Error: name (string) is required."
    preserve_state = bool(tool_input.get("preserve_state", False))

    try:
        with db_transaction() as db:
            record = installer.get_install_record(db, name)
            if record is None:
                return (
                    f"Package {name!r} is not installed.  Nothing to "
                    f"do.  (Source-repo packages loaded via the "
                    f"back-compat shim are not 'installed' and have "
                    f"no install record to remove.)"
                )
            blockers = installer.list_blocking_arcs(db, name)
            if blockers:
                lines = [
                    f"Cannot uninstall {name!r}: {len(blockers)} "
                    f"non-terminal arc(s) still reference templates "
                    f"this package shipped:",
                ]
                for arc_id, tmpl, status in blockers[:10]:
                    lines.append(
                        f"  arc #{arc_id} -> template {tmpl!r} "
                        f"(status={status!r})"
                    )
                if len(blockers) > 10:
                    lines.append(f"  ... and {len(blockers)-10} more")
                lines.append(
                    "Cancel/terminate those arcs and try again."
                )
                return "\n".join(lines)

            result = installer.uninstall_package(
                name, conn=db, archive_state=preserve_state,
            )
    except installer.InstallError as exc:
        return f"Uninstall failed: {exc}"
    except Exception as exc:
        return f"Uninstall failed: {exc}"
    state_note = (
        "Per-package state was archived to package_state_archive."
        if preserve_state
        else "Per-package state was wiped (FK cascade)."
    )
    return (
        f"Uninstalled {result.name} (removed {result.removed_path}). "
        f"Allowlists were not touched (SD5).  {state_note}  Restart "
        f"the server to unload the package's chat tools."
    )

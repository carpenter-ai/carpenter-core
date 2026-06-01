"""Chat tools for introspecting loaded capability packages.

Phase A ships a single read-only tool, ``list_packages``, that returns
the set of capability packages the platform discovered at startup.

Read-only by design: there is no chat tool for installing, removing,
or otherwise mutating packages in Phase A — install lifecycle is a
later phase and any mutation surface would need a fresh trust review.
"""

from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description=(
        "List capability packages loaded by the platform at startup. "
        "Returns each package's name, version, description, and the "
        "names of chat tools it contributed.  Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    capabilities=["config_read"],
    trust_boundary="chat",
)
def list_packages(tool_input, **kwargs):
    """Return a human-readable summary of loaded capability packages."""
    from carpenter.packages import get_registry

    packages = get_registry().list_packages()
    if not packages:
        return "No capability packages loaded."

    lines: list[str] = []
    for pkg in sorted(packages, key=lambda p: p.manifest.name):
        m = pkg.manifest
        tool_summary = (
            f" [{len(pkg.chat_tool_names)} tool(s): "
            f"{', '.join(pkg.chat_tool_names)}]"
            if pkg.chat_tool_names
            else " [no tools]"
        )
        err_summary = (
            f" ({len(pkg.load_errors)} load error(s))"
            if pkg.load_errors
            else ""
        )
        lines.append(
            f"- {m.name} v{m.version}: {m.description}{tool_summary}{err_summary}"
        )

    return "Loaded capability packages:\n" + "\n".join(lines)

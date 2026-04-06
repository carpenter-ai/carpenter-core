"""Chat tools for knowledge base navigation and search."""

from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description=(
        "Navigate the knowledge base. Call with no path for the root index. "
        "Call with a folder path to list children. Call with a leaf path to "
        "read the full entry. Entries contain [[links]] -- call kb_describe on "
        "any link target to follow it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "KB path (e.g. 'scheduling', 'arcs/planning'). Omit for root.",
            },
        },
        "required": [],
    },
    capabilities=["kb_read"],
    always_available=True,
)
def kb_describe(tool_input, **kwargs):
    from carpenter.kb import get_store
    store = get_store()
    path = tool_input.get("path", "")
    conversation_id = kwargs.get("conversation_id")

    if not path:
        entry = store.get_entry("")
        if entry:
            store.log_access("_root", conversation_id=conversation_id)
            inbound = store.get_inbound_links("_root")
            footer = f"\n\n---\nReferenced by {len(inbound)} entries -- use kb_links_in(\"_root\") to see them." if inbound else ""
            return entry["content"] + footer
        return "KB root not found. The knowledge base may not be initialized."

    children = store.list_children(path)
    if children:
        entry = store.get_entry(path)
        store.log_access(path, conversation_id=conversation_id)
        content = entry["content"] if entry else f"# {path}\n"
        lines = [content, "", "## Contents"]
        for child in children:
            icon = "\U0001f4c1" if child["is_folder"] else "\U0001f4c4"
            desc = f" -- {child['description']}" if child.get("description") else ""
            lines.append(f"- {icon} [[{child['path']}]]{desc}")
        inbound = store.get_inbound_links(path)
        if inbound:
            lines.append(f"\n---\nReferenced by {len(inbound)} entries -- use kb_links_in(\"{path}\") to see them.")
        return "\n".join(lines)

    entry = store.get_entry(path)
    if entry is None:
        return f"KB entry not found: {path}"
    store.log_access(path, conversation_id=conversation_id)
    inbound = store.get_inbound_links(path)
    footer = ""
    if inbound:
        footer = f"\n\n---\nReferenced by {len(inbound)} entries -- use kb_links_in(\"{path}\") to see them."
    return entry["content"] + footer


@chat_tool(
    description=(
        "Search the knowledge base by keyword or natural language. Returns "
        "the most relevant entries with paths and descriptions. Use path_prefix "
        "to scope searches: 'conversations/' for past conversations, "
        "'reflections/' for self-reflections, 'work/' for work history."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query.",
            },
            "max_results": {
                "type": "integer",
                "description": "Max results (default 5).",
            },
            "path_prefix": {
                "type": "string",
                "description": (
                    "Scope results to entries under this path prefix "
                    "(e.g. 'conversations/', 'reflections/', 'work/')."
                ),
            },
        },
        "required": ["query"],
    },
    capabilities=["kb_read"],
    always_available=True,
)
def kb_search(tool_input, **kwargs):
    from carpenter.kb import get_store
    store = get_store()
    query = tool_input["query"]
    max_results = tool_input.get("max_results", 5)
    path_prefix = tool_input.get("path_prefix")
    results = store.search(query, max_results, path_prefix=path_prefix)
    if not results:
        scope = f" under '{path_prefix}'" if path_prefix else ""
        return f"No KB entries found matching '{query}'{scope}."
    lines = [f"KB search results for '{query}':"]
    for r in results:
        lines.append(f"- [[{r['path']}]] -- {r['title']}: {r['description']}")
    return "\n".join(lines)


@chat_tool(
    description=(
        "List entries that link TO this path. Shows what references or "
        "depends on this capability."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "KB path to find inbound links for.",
            },
        },
        "required": ["path"],
    },
    capabilities=["kb_read"],
    always_available=True,
)
def kb_links_in(tool_input, **kwargs):
    from carpenter.kb import get_store
    store = get_store()
    path = tool_input["path"]
    inbound = store.get_inbound_links(path)
    if not inbound:
        return f"No entries link to '{path}'."
    lines = [f"Entries linking to '{path}':"]
    for link in inbound:
        text = f" ({link['link_text']})" if link.get("link_text") else ""
        lines.append(f"- [[{link['source_path']}]] -- {link['title']}{text}")
    return "\n".join(lines)


@chat_tool(
    description=(
        "Get knowledge base health metrics: total entries, link counts, "
        "orphans, broken links, oversized entries, and stale entries."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    capabilities=["kb_read"],
)
def get_kb_health(tool_input, **kwargs):
    from carpenter.kb.health import graph_metrics
    metrics = graph_metrics()
    lines = ["KB Health Metrics:"]
    lines.append(f"  Total entries: {metrics['total_entries']}")
    lines.append(f"  Total links: {metrics['total_links']}")
    lines.append(f"  Avg links/entry: {metrics['avg_links_per_entry']:.1f}")
    if metrics["orphan_entries"]:
        lines.append(f"  Orphan entries ({len(metrics['orphan_entries'])}): {', '.join(metrics['orphan_entries'][:10])}")
    if metrics["broken_links"]:
        lines.append(f"  Broken links ({len(metrics['broken_links'])}): {', '.join(metrics['broken_links'][:10])}")
    if metrics["oversized_entries"]:
        lines.append(f"  Oversized entries ({len(metrics['oversized_entries'])}): {', '.join(metrics['oversized_entries'][:10])}")
    if metrics["stale_entries"]:
        lines.append(f"  Stale entries ({len(metrics['stale_entries'])}): {', '.join(metrics['stale_entries'][:10])}")
    if metrics["unreachable_entries"]:
        lines.append(f"  Unreachable entries ({len(metrics['unreachable_entries'])}): {', '.join(metrics['unreachable_entries'][:10])}")
    if not any([metrics["orphan_entries"], metrics["broken_links"],
               metrics["oversized_entries"], metrics["stale_entries"],
               metrics["unreachable_entries"]]):
        lines.append("  No issues found.")
    return "\n".join(lines)

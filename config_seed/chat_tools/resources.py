"""Chat tools for Resource introspection.

Resources are first-class rows for externally-sourced content with
provenance-derived trust (see ``carpenter/core/resources/``).  Chat
agents can only read *trusted* Resources — raw ingest (untrusted) is
readable only by sandboxed EXECUTOR arcs with a template contract.
"""

from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description=(
        "Read the content of a trusted Resource by id.  Resources are the "
        "canonical form for externally-sourced content (e.g. web pages, "
        "summaries).  Only Resources that a JUDGE has approved as derived "
        "from a known template can be read from chat — raw untrusted "
        "ingest is refused.  Use read_arc_result or arc completion "
        "notifications to discover Resource ids."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "resource_id": {
                "type": "integer",
                "description": "The Resource id to read.",
            },
            "offset": {
                "type": "integer",
                "description": (
                    "Character offset to start reading from (default 0). "
                    "Use with limit to paginate large Resources."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Maximum characters to return (default 50000). "
                    "Use with offset to paginate through large Resources."
                ),
            },
        },
        "required": ["resource_id"],
    },
    capabilities=["database_read", "filesystem_read"],
    always_available=True,
)
def read_resource(tool_input, **kwargs):
    from carpenter.core.resources import (
        get_resource,
        is_trusted,
        read_resource_content,
    )

    resource_id = tool_input["resource_id"]
    offset = tool_input.get("offset", 0)
    limit = tool_input.get("limit", 50_000)

    row = get_resource(resource_id)
    if row is None:
        return f"Resource #{resource_id} not found."

    if row.get("deleted_at") is not None:
        return (
            f"Resource #{resource_id} has been cleaned up "
            f"(deleted_at={row['deleted_at']}) and is no longer readable."
        )

    if not is_trusted(resource_id):
        ct = row.get("content_type") or "unknown"
        verdict = row.get("template_verdict")
        verdict_str = verdict if verdict is not None else "none (raw ingest)"
        return (
            f"Resource #{resource_id} is untrusted (content_type={ct}, "
            f"template_verdict={verdict_str}). Only approved derived "
            "Resources can be read from chat."
        )

    try:
        content = read_resource_content(
            resource_id, offset, limit, caller_arc_id=None,
        )
    except FileNotFoundError as e:
        return f"Resource #{resource_id} cannot be read: {e}"
    except ValueError as e:
        return f"Invalid read parameters for Resource #{resource_id}: {e}"

    content_type = row.get("content_type") or "unknown"
    byte_size = row.get("byte_size")
    byte_size_str = str(byte_size) if byte_size is not None else "unknown"
    has_more = (
        byte_size is not None and (offset + limit) < byte_size
    )

    header_parts = [
        f"[Resource #{resource_id}",
        f"content_type={content_type}",
        f"byte_size={byte_size_str}",
        f"offset={offset}",
        f"limit={limit}",
    ]
    if has_more:
        remaining = byte_size - offset - limit
        header_parts.append(
            f"more={remaining} bytes remaining — call read_resource("
            f"{resource_id}, offset={offset + limit}) to continue"
        )
    header = ", ".join(header_parts) + "]"
    return f"{header}\n{content}"

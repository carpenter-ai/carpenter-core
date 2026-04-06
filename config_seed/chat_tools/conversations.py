"""Chat tools for conversation introspection."""

from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description=(
        "List recent conversations with start time, last message time, "
        "message count, and token usage. Newest first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max results (default 10).",
            },
        },
        "required": [],
    },
    capabilities=["database_read"],
)
def list_conversations(tool_input, **kwargs):
    from carpenter.db import get_db
    limit = tool_input.get("limit", 10)
    db = get_db()
    try:
        rows = db.execute(
            "SELECT c.id, c.title, c.archived, c.started_at, c.last_message_at, c.context_tokens, "
            "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS msg_count "
            "FROM conversations c ORDER BY c.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        db.close()
    if not rows:
        return "No conversations found."
    lines = []
    for r in rows:
        title = f' "{r["title"]}"' if r["title"] else ""
        archived = " [archived]" if r["archived"] else ""
        lines.append(
            f"conv#{r['id']}{title}{archived}  messages={r['msg_count']}  tokens={r['context_tokens']}\n"
            f"  started: {r['started_at']}  last_msg: {r['last_message_at']}"
        )
    return "\n".join(lines)


@chat_tool(
    description=(
        "Get all messages from a specific conversation. Shows role, content "
        "preview, whether structured content (tool_use) is present, and "
        "timestamps. Use to review prior conversation context."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "conversation_id": {
                "type": "integer",
                "description": "The conversation ID to inspect.",
            },
            "limit": {
                "type": "integer",
                "description": "Max messages to return (default 50). Use 0 for all.",
            },
        },
        "required": ["conversation_id"],
    },
    capabilities=["database_read"],
)
def get_conversation_messages(tool_input, **kwargs):
    from carpenter.db import get_db
    conv_id = tool_input["conversation_id"]
    limit = tool_input.get("limit", 50)
    db = get_db()
    try:
        if limit == 0:
            rows = db.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id ASC",
                (conv_id,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM messages WHERE conversation_id = ? "
                "ORDER BY id ASC LIMIT ?",
                (conv_id, limit),
            ).fetchall()
    finally:
        db.close()
    if not rows:
        return f"No messages in conversation #{conv_id}."
    lines = [f"Conversation #{conv_id} ({len(rows)} messages):"]
    for r in rows:
        content_preview = (r["content"] or "")[:200]
        has_json = " [structured]" if r["content_json"] else ""
        arc = f" arc=#{r['arc_id']}" if r["arc_id"] else ""
        lines.append(
            f"\n  msg#{r['id']} [{r['role']}]{has_json}{arc}  ({r['created_at']})\n"
            f"    {content_preview}"
        )
    return "\n".join(lines)

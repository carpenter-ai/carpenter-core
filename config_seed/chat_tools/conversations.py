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


@chat_tool(
    description=(
        "Pin THIS conversation to a specific AI provider/model, or clear the "
        "pin. Only affects the current conversation — the global default for "
        "other chats is unchanged. Useful when the user wants to try an "
        "alternate backend (e.g. a local Ollama model) without touching "
        "server-wide configuration. "
        "Supported providers: anthropic, ollama, tinfoil, chain, local. "
        "The model string is the bare identifier (e.g. 'qwen3.5:9b' for "
        "Ollama, 'claude-haiku-4-5' for Anthropic). "
        "Pass clear=true with no provider/model to revert to the default."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "description": (
                    "Provider name: anthropic, ollama, tinfoil, chain, or "
                    "local. Required unless clear=true."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Bare model identifier for the provider. "
                    "Required unless clear=true."
                ),
            },
            "clear": {
                "type": "boolean",
                "description": (
                    "If true, remove the pin and revert to the global "
                    "default. Default false."
                ),
            },
        },
        "required": [],
    },
    capabilities=["database_write"],
    trust_boundary="platform",
    always_available=True,
)
def set_conversation_model(tool_input, **kwargs):
    """Pin or clear a per-conversation provider/model override."""
    from carpenter.agent import conversation as conv_mod

    conv_id = kwargs.get("conversation_id")
    if conv_id is None:
        return "Error: no active conversation_id (tool called out of context)."

    if tool_input.get("clear"):
        conv_mod.clear_conversation_model_override(conv_id)
        return (
            f"Cleared model pin on conversation #{conv_id}. "
            "Subsequent turns will use the global default."
        )

    provider = (tool_input.get("provider") or "").strip()
    model = (tool_input.get("model") or "").strip()
    if not provider or not model:
        return (
            "Error: both 'provider' and 'model' are required (unless "
            "clear=true)."
        )

    try:
        conv_mod.set_conversation_model_override(conv_id, provider, model)
    except ValueError as exc:
        return f"Error: {exc}"

    return (
        f"Pinned conversation #{conv_id} to {provider}:{model}. "
        "Only this conversation is affected; the global default is unchanged."
    )


@chat_tool(
    description=(
        "Show the current per-conversation model pin for THIS conversation, "
        "or report that it is using the global default."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    capabilities=["database_read"],
)
def get_conversation_model(tool_input, **kwargs):
    """Report the current model pin for this conversation."""
    from carpenter.agent import conversation as conv_mod

    conv_id = kwargs.get("conversation_id")
    if conv_id is None:
        return "Error: no active conversation_id (tool called out of context)."

    pin = conv_mod.get_conversation_model_override(conv_id)
    if pin is None:
        return (
            f"Conversation #{conv_id} has no model pin; using global default."
        )
    return f"Conversation #{conv_id} is pinned to {pin}."

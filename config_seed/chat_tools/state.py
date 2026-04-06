"""Chat tools for persistent state queries."""

import json

from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description="Get a value from persistent state by key.",
    input_schema={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "The state key to retrieve.",
            },
        },
        "required": ["key"],
    },
    capabilities=["database_read"],
    always_available=True,
)
def get_state(tool_input, **kwargs):
    from carpenter.tool_backends import state as state_backend
    result = state_backend.handle_get({
        "key": tool_input["key"],
        "arc_id": 0,  # conversation-level state uses arc_id=0
    })
    return json.dumps(result.get("value"))

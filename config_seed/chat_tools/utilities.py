"""Chat tools for simple utility operations."""

from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description="Reverse the characters in a string.",
    input_schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to reverse.",
            },
        },
        "required": ["text"],
    },
    capabilities=["pure"],
)
def reverse_string(tool_input, **kwargs):
    return tool_input["text"][::-1]

# Tool Confirmation Mechanism

## Overview

The `requires_user_confirm` flag enables chat tools to require user confirmation before execution. This provides a clean, platform-agnostic confirmation gate for effectful tools (send email, create calendar event, launch Android intent) without platform-specific workarounds.

## Design

### Tool Registration

Tools opt in to confirmation by setting `requires_user_confirm=True`:

**Python decorator:**
```python
from carpenter.chat_tool_loader import chat_tool

@chat_tool(
    description="Send an email",
    input_schema={...},
    requires_user_confirm=True,
)
def send_email(tool_input, **kwargs):
    # Implementation
    pass
```

**Extension tool registration:**
```python
from carpenter.chat_tool_loader import register_extension_tool

register_extension_tool(
    name="send_email",
    description="Send an email",
    input_schema={...},
    handler=send_email_handler,
    requires_user_confirm=True,
)
```

### Platform Confirmation Handler

Platforms register a confirmation handler at startup:

```python
from carpenter.chat_tool_loader import set_confirmation_handler

def platform_confirmation_handler(tool_name: str, tool_input: dict) -> bool:
    """Show platform-specific confirmation UI.

    Args:
        tool_name: Name of the tool being called
        tool_input: Parameters passed to the tool

    Returns:
        True if user confirmed, False if declined
    """
    # Platform-specific implementation:
    # - Android: Show confirmation dialog
    # - CLI: Interactive prompt
    # - Web: Modal dialog
    # - etc.

    return show_confirmation_dialog(tool_name, tool_input)

# Register at platform startup
set_confirmation_handler(platform_confirmation_handler)
```

### Execution Flow

When the AI calls a tool:

1. **Tool invocation** — AI requests tool execution
2. **Check flag** — System checks `requires_user_confirm`
3. **If True:**
   - Check if confirmation handler is registered
   - If no handler: Reject with error message
   - If handler exists: Call handler with tool name and parameters
   - If handler returns `True`: Execute tool
   - If handler returns `False`: Return "User declined" to AI
4. **If False:** Execute tool normally (no confirmation)

### AI Response to Declined Execution

If the user declines, the AI receives:
```
User declined to execute this tool.
```

The AI can then:
- Acknowledge the user's decision
- Suggest alternatives
- Ask if the user wants to modify parameters
- Continue the conversation naturally

### No Handler Registered

If a tool requires confirmation but no handler is registered, execution fails with:
```
Error: Tool 'tool_name' requires user confirmation, but no confirmation handler
is registered. This tool cannot be executed on this platform.
```

This is a **fail-safe design** — tools requiring confirmation cannot execute without platform support.

## Use Cases

Typical tools that should require confirmation:

- **Email/messaging:** Send email, SMS, chat message
- **Calendar:** Create/modify events
- **External integrations:** Post to social media, trigger webhooks
- **Android intents:** Launch apps, make calls, send messages
- **Payments/transactions:** Any financial operation
- **Data deletion:** Irreversible destructive operations

## Implementation Notes

- **Minimal core change:** ~20 lines of logic in invocation flow
- **Platform agnostic:** Core provides the mechanism, platforms implement UI
- **Fail-safe:** Tools requiring confirmation cannot execute without handler
- **Optional:** Platforms can choose whether to implement confirmation UI
- **Default:** `requires_user_confirm=False` (no change to existing tools)

## Examples

See `docs/examples/tool_with_confirmation.py` for complete examples of:
- Tool definition with confirmation
- Platform confirmation handler
- Integration in platform startup code

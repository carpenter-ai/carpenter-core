"""Example: Chat tool with user confirmation requirement.

This demonstrates the requires_user_confirm flag, which enables
platform-level confirmation gates for effectful tools.

Use cases:
- Send email
- Create calendar event
- Launch Android intent
- Post to social media
- Make payment
"""

from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description="Send an email message (requires user confirmation)",
    input_schema={
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Recipient email address",
            },
            "subject": {
                "type": "string",
                "description": "Email subject line",
            },
            "body": {
                "type": "string",
                "description": "Email body content",
            },
        },
        "required": ["to", "subject", "body"],
    },
    capabilities=["pure"],  # Extension tools must use read-only capabilities
    requires_user_confirm=True,  # Platform will request confirmation before execution
)
def send_email(tool_input, **kwargs):
    """Send an email with user confirmation.

    This tool demonstrates the confirmation mechanism. When the AI calls
    this tool, the platform will:
    1. Check requires_user_confirm flag
    2. Call the registered confirmation handler
    3. Show user a confirmation dialog with tool name and parameters
    4. Only execute if user confirms

    If the user declines, the AI receives "User declined to execute this tool."
    and can continue the conversation.
    """
    to = tool_input["to"]
    subject = tool_input["subject"]
    body = tool_input["body"]

    # In a real implementation, this would send the email
    # For this example, we just return a success message
    return f"Email sent to {to} with subject '{subject}'"


@chat_tool(
    description="Create a calendar event (requires user confirmation)",
    input_schema={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Event title",
            },
            "start_time": {
                "type": "string",
                "description": "Start time in ISO 8601 format",
            },
            "duration_minutes": {
                "type": "integer",
                "description": "Duration in minutes",
            },
        },
        "required": ["title", "start_time", "duration_minutes"],
    },
    capabilities=["pure"],
    requires_user_confirm=True,
)
def create_calendar_event(tool_input, **kwargs):
    """Create a calendar event with user confirmation."""
    title = tool_input["title"]
    start = tool_input["start_time"]
    duration = tool_input["duration_minutes"]

    # In a real implementation, this would create the calendar event
    return f"Calendar event '{title}' created for {start} ({duration} minutes)"


# Example platform confirmation handler registration
# (Platform packages like carpenter-android would call this at startup)

def example_confirmation_handler(tool_name: str, tool_input: dict) -> bool:
    """Example confirmation handler (platform-specific).

    Real implementations would show a platform-appropriate UI:
    - Android: Dialog with tool details
    - CLI: Interactive prompt
    - Web: Modal dialog

    Args:
        tool_name: Name of the tool being called
        tool_input: Parameters passed to the tool

    Returns:
        True if user confirmed, False if declined
    """
    print(f"\n[Confirmation Request]")
    print(f"Tool: {tool_name}")
    print(f"Parameters: {tool_input}")

    response = input("Execute this tool? (yes/no): ").strip().lower()
    return response in ("yes", "y")


# Platform startup code would register the handler:
# from carpenter.chat_tool_loader import set_confirmation_handler
# set_confirmation_handler(example_confirmation_handler)

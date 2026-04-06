"""Message formatting for different channel types.

Converts platform-internal markdown to channel-appropriate formatting.
"""

import re


def format_for_channel(text: str, channel_type: str) -> str:
    """Format a message for a specific channel type.

    Args:
        text: Message text (may contain markdown).
        channel_type: Target channel type.

    Returns:
        Formatted text appropriate for the channel.
    """
    if channel_type == "web":
        return text  # Web UI renders markdown directly
    elif channel_type == "telegram":
        return _format_telegram(text)
    elif channel_type == "signal":
        return _strip_markdown(text)
    elif channel_type == "whatsapp":
        return _format_whatsapp(text)
    else:
        return text  # Unknown channel, pass through


def _format_telegram(text: str) -> str:
    """Convert to Telegram MarkdownV2 format.

    Escapes special characters that Telegram's MarkdownV2 parser
    requires to be escaped.
    """
    # Telegram MarkdownV2 special chars that need escaping
    # (when not part of markdown syntax)
    special = r"_[]()~`>#+=|{}.!-"

    result = []
    i = 0
    in_code_block = False
    in_inline_code = False

    while i < len(text):
        # Code blocks (```)
        if text[i:i+3] == "```":
            in_code_block = not in_code_block
            result.append("```")
            i += 3
            continue

        # Inline code (`)
        if text[i] == "`" and not in_code_block:
            in_inline_code = not in_inline_code
            result.append("`")
            i += 1
            continue

        # Don't escape inside code
        if in_code_block or in_inline_code:
            result.append(text[i])
            i += 1
            continue

        # Escape special characters
        if text[i] in special:
            result.append(f"\\{text[i]}")
        else:
            result.append(text[i])
        i += 1

    return "".join(result)


def _strip_markdown(text: str) -> str:
    """Strip markdown formatting for plain-text channels."""
    # Remove code blocks but keep content
    text = re.sub(r"```\w*\n?", "", text)
    # Remove bold/italic markers
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    # Remove inline code backticks
    text = re.sub(r"`(.+?)`", r"\1", text)
    # Remove link markdown, keep text
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    # Remove header markers
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    return text


def split_message(text: str, max_length: int) -> list[str]:
    """Split a message into chunks that fit within a channel's length limit.

    Splits at paragraph boundaries first, then sentence boundaries,
    then hard-cuts at max_length as a last resort.

    Args:
        text: The message text to split.
        max_length: Maximum length per chunk.

    Returns:
        List of text chunks, each within max_length.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Try to split at a paragraph boundary (double newline)
        cut = remaining.rfind("\n\n", 0, max_length)
        if cut > 0:
            chunks.append(remaining[:cut])
            remaining = remaining[cut + 2:]  # skip the double newline
            continue

        # Try to split at a single newline
        cut = remaining.rfind("\n", 0, max_length)
        if cut > 0:
            chunks.append(remaining[:cut])
            remaining = remaining[cut + 1:]
            continue

        # Try to split at a sentence boundary (. ! ?)
        for sep in (". ", "! ", "? "):
            cut = remaining.rfind(sep, 0, max_length)
            if cut > 0:
                chunks.append(remaining[:cut + 1])  # include the punctuation
                remaining = remaining[cut + 2:]  # skip punctuation + space
                break
        else:
            # Try to split at a space
            cut = remaining.rfind(" ", 0, max_length)
            if cut > 0:
                chunks.append(remaining[:cut])
                remaining = remaining[cut + 1:]
            else:
                # Hard cut — no good boundary found
                chunks.append(remaining[:max_length])
                remaining = remaining[max_length:]

    return [c for c in chunks if c]


def _format_whatsapp(text: str) -> str:
    """Convert to WhatsApp formatting.

    WhatsApp uses *bold*, _italic_, ~strikethrough~, ```monospace```.
    """
    # Convert **bold** to *bold*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    # Convert ~~strikethrough~~ to ~strikethrough~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)
    return text

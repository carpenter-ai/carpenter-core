"""Chat web UI for Carpenter.

Provides an HTMX-powered chat interface with a dark monokai theme.
Supports multiple conversations with isolated history.
"""
import html
import logging
import re
import sqlite3

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.routing import Route

from ..agent import conversation
from .static import read_asset, load_template

logger = logging.getLogger(__name__)


def _escape_html(text: str) -> str:
    """Escape HTML entities in text."""
    return html.escape(text, quote=True)


def _linkify(text: str) -> str:
    """Turn URLs in already-escaped HTML text into clickable links.

    Handles both absolute URLs (http/https) and relative review URLs (/api/review/...).
    """
    # Absolute URLs
    text = re.sub(
        r'(https?://[^\s<>&]+)',
        r'<a href="\1" target="_blank" style="color:#66d9ef;text-decoration:underline;">\1</a>',
        text,
    )
    # Relative review URLs
    text = re.sub(
        r'(/api/review/[a-f0-9-]+)',
        r'<a href="\1" target="_blank" style="color:#66d9ef;text-decoration:underline;">\1</a>',
        text,
    )
    return text


def _render_content(text: str) -> str:
    """Render message content with code block support.

    Detects ```lang ... ``` fenced code blocks and wraps them in styled
    <pre> tags. Regular text is escaped, linkified, and has newlines
    converted to <br>.
    """
    parts = re.split(r"(```\w*\n.*?```)", text, flags=re.DOTALL)
    rendered = []
    for part in parts:
        match = re.match(r"```\w*\n(.*?)```", part, flags=re.DOTALL)
        if match:
            code = _escape_html(match.group(1))
            rendered.append(
                f'<pre style="background:#1e1e1e;padding:12px;'
                f'border-radius:4px;overflow-x:auto;font-size:13px;'
                f'line-height:1.4;margin:8px 0;">{code}</pre>'
            )
        else:
            escaped = _escape_html(part).replace("\n", "<br>")
            rendered.append(_linkify(escaped))
    return "".join(rendered)


def _render_system_content(text: str) -> str:
    """Render system message content, turning review URLs into clickable links."""
    escaped = _escape_html(text)
    # Turn relative review URLs into clickable links
    escaped = re.sub(
        r'(/api/review/[a-f0-9-]+)',
        r'<a href="\1" target="_blank" style="color:#66d9ef;text-decoration:underline;">\1</a>',
        escaped,
    )
    return escaped


def _render_message(msg: dict) -> str:
    """Render a single message as an HTML div."""
    role = msg.get("role", "assistant")
    content = msg.get("content", "")

    if role == "system":
        rendered = _render_system_content(content)

        # Check for error_info in content_json
        content_json = msg.get("content_json")
        is_error = False
        if content_json:
            try:
                import json
                data = json.loads(content_json) if isinstance(content_json, str) else content_json
                is_error = "error_info" in data
            except (json.JSONDecodeError, TypeError):
                pass

        error_icon = '\u26a0\ufe0f ' if is_error else ''
        border_color = '#e74c3c' if is_error else '#66d9ef'  # Red vs blue

        return (
            '<div style="display:flex;justify-content:center;margin:6px 0;">'
            '<div class="system-message" style="background:#3e3d32;color:#75715e;padding:6px 14px;'
            'border-radius:8px;font-size:13px;font-style:italic;'
            f'border-left:3px solid {border_color};max-width:85%;word-wrap:break-word;">'
            f'{error_icon}{rendered}</div></div>'
        )

    rendered = _render_content(content)

    if role == "user":
        return (
            '<div style="display:flex;justify-content:flex-end;margin:8px 0;">'
            '<div style="background:#2d4a22;color:#a6e22e;padding:10px 14px;'
            'border-radius:12px 12px 2px 12px;max-width:75%;word-wrap:break-word;">'
            f'{rendered}'
            '<span style="display:inline-block;margin-left:8px;font-size:11px;'
            'opacity:0.5;vertical-align:middle;">&#x2713;</span>'
            '</div></div>'
        )
    else:
        return (
            '<div style="display:flex;justify-content:flex-start;margin:8px 0;">'
            '<div style="background:#3e3d32;color:#f8f8f2;padding:10px 14px;'
            'border-radius:12px 12px 12px 2px;max-width:75%;word-wrap:break-word;">'
            f'{rendered}</div></div>'
        )


async def chat_page(request: Request):
    """Serve the main chat page, scoped to a conversation."""
    token = request.query_params.get("token", "")
    c_param = request.query_params.get("c", "")

    # Determine which conversation to display
    conv_id = None
    if c_param:
        try:
            conv_id = int(c_param)
            # Verify it exists
            conv = conversation.get_conversation(conv_id)
            if conv is None:
                conv_id = None
        except (ValueError, TypeError):
            conv_id = None

    if conv_id is None:
        # Web UI is conversation-specific — no time-based boundary.
        # Just show the most recent non-archived conversation.
        conv_id = conversation.get_last_conversation()

    css = read_asset("chat.css")
    js = read_asset("chat.js")

    html_content = load_template("chat.html", css=css, js=js)

    # Inject token and conversation ID into placeholders
    poll_url = f"/api/chat/messages?c={conv_id}"
    if token:
        poll_url += "&token=" + token
    new_chat_url = "/new"
    if token:
        new_chat_url += "?token=" + token
    html_content = html_content.replace("__POLL_URL__", poll_url)
    html_content = html_content.replace("__UI_TOKEN__", token)
    html_content = html_content.replace("__CONV_ID__", str(conv_id))
    html_content = html_content.replace("__NEW_CHAT_URL__", new_chat_url)

    return HTMLResponse(content=html_content)


async def new_chat(request: Request):
    """Create a new conversation and redirect to it."""
    token = request.query_params.get("token", "")
    conv_id = conversation.create_conversation()
    redirect_url = f"/?c={conv_id}"
    if token:
        redirect_url += f"&token={token}"
    return RedirectResponse(url=redirect_url, status_code=302)


async def list_conversations(request: Request):
    """Return JSON list of conversations for the dropdown."""
    include_archived = request.query_params.get("include_archived", "").lower() == "true"
    archived_only = request.query_params.get("archived_only", "").lower() == "true"
    convs = conversation.list_conversations_with_preview(
        include_archived=include_archived,
        archived_only=archived_only,
    )
    return JSONResponse(content=convs)


async def archive_conversation_endpoint(request: Request):
    """Archive a conversation."""
    conv_id = int(request.path_params["conv_id"])
    conversation.archive_conversation(conv_id)
    return JSONResponse(content={"ok": True})


async def unarchive_conversation_endpoint(request: Request):
    """Unarchive a conversation."""
    conv_id = int(request.path_params["conv_id"])
    conversation.unarchive_conversation(conv_id)
    return JSONResponse(content={"ok": True})


async def chat_messages(request: Request):
    """Return an HTML fragment of all messages for HTMX polling.

    Accepts both conversation_id and c as query params.
    Returns empty string if no conversation or messages exist.
    """
    conversation_id_param = request.query_params.get("conversation_id")
    c_param = request.query_params.get("c")
    effective_id = int(conversation_id_param) if conversation_id_param else (int(c_param) if c_param else None)
    try:
        if effective_id is None:
            effective_id = conversation.get_or_create_conversation()
        messages = conversation.get_messages(effective_id)
    except (sqlite3.Error, ValueError, KeyError) as _exc:
        logger.exception("Failed to fetch messages")
        messages = []

    if not messages:
        return HTMLResponse(content="")

    display_messages = [m for m in messages if m["role"] not in ("tool_result", "tool_call")]
    fragments = [_render_message(msg) for msg in display_messages]
    return HTMLResponse(content="\n".join(fragments))


routes = [
    Route("/", chat_page, methods=["GET"]),
    Route("/new", new_chat, methods=["GET"]),
    Route("/api/conversations/{conv_id}/archive", archive_conversation_endpoint, methods=["POST"]),
    Route("/api/conversations/{conv_id}/unarchive", unarchive_conversation_endpoint, methods=["POST"]),
    Route("/api/conversations", list_conversations, methods=["GET"]),
    Route("/api/chat/messages", chat_messages, methods=["GET"]),
]

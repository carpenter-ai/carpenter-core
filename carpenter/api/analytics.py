"""Analytics dashboard for model health and retry monitoring.

Provides an HTMX-powered dashboard with live-updating sections:
- Model health cards with status badges and success rate bars
- Recent retry attempts table
- Error type breakdown
"""
import json
import logging
import sqlite3

from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from ..core.models import health as mh
from ..db import get_db, db_connection
from .static import read_asset, load_template

logger = logging.getLogger(__name__)


# Monokai color palette (matches ui.py)
_COLORS = {
    "bg": "#272822",
    "bg2": "#3e3d32",
    "bg_code": "#1e1e1e",
    "green": "#a6e22e",
    "cyan": "#66d9ef",
    "text": "#f8f8f2",
    "muted": "#75715e",
    "pink": "#f92672",
    "yellow": "#e6db74",
    "red": "#f44747",
    "purple": "#ae81ff",
    "orange": "#fd971f",
}

# Health status -> color mapping
_HEALTH_COLORS = {
    mh.ModelHealth.HEALTHY: _COLORS["green"],
    mh.ModelHealth.DEGRADED: _COLORS["yellow"],
    mh.ModelHealth.UNHEALTHY: _COLORS["pink"],
    mh.ModelHealth.CIRCUIT_OPEN: _COLORS["red"],
}


def _render_health_card(state: mh.ModelHealthState) -> str:
    """Render a single model health card as HTML."""
    color = _HEALTH_COLORS.get(state.health, _COLORS["muted"])
    status_label = state.health.value.upper().replace("_", " ")
    pct = int(state.success_rate * 100)
    bar_color = color

    # Circuit open countdown
    countdown = ""
    if state.circuit_open_until:
        countdown = (
            f'<div style="font-size:12px;color:{_COLORS["red"]};margin-top:4px;">'
            f'Circuit reopens: {state.circuit_open_until[:19]}Z</div>'
        )

    return f"""<div style="background:{_COLORS["bg"]};border:1px solid {color};
        border-radius:8px;padding:14px;min-width:220px;flex:1 1 260px;max-width:360px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <span style="font-weight:bold;color:{_COLORS["text"]};font-size:13px;
                overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:160px;"
                title="{state.model_id}">{state.model_id}</span>
            <span style="background:{color};color:{_COLORS["bg"]};padding:2px 8px;
                border-radius:4px;font-size:11px;font-weight:bold;white-space:nowrap;">
                {status_label}</span>
        </div>
        <div style="margin-bottom:6px;">
            <div style="display:flex;justify-content:space-between;font-size:12px;
                color:{_COLORS["muted"]};margin-bottom:2px;">
                <span>Success Rate</span><span>{pct}%</span>
            </div>
            <div style="background:{_COLORS["bg2"]};border-radius:4px;height:8px;overflow:hidden;">
                <div style="background:{bar_color};height:100%;width:{pct}%;
                    border-radius:4px;transition:width 0.3s;"></div>
            </div>
        </div>
        <div style="display:flex;gap:16px;font-size:12px;color:{_COLORS["muted"]};">
            <span>Failures: <span style="color:{_COLORS["text"]};">{state.consecutive_failures}</span></span>
            <span>Backoff: <span style="color:{_COLORS["text"]};">{state.backoff_multiplier:.1f}x</span></span>
            <span>Calls: <span style="color:{_COLORS["text"]};">{state.total_attempts}</span></span>
        </div>
        {countdown}
    </div>"""


async def analytics_page(request: Request):
    """Serve the analytics dashboard page."""
    css = read_asset("analytics.css")
    html = load_template("analytics.html", css=css)
    return HTMLResponse(content=html)


async def health_fragment(request: Request):
    """Return HTML fragment with model health cards."""
    try:
        states = mh.get_all_model_health()
    except (sqlite3.Error, KeyError, ValueError) as _exc:
        logger.exception("Failed to fetch model health")
        states = []

    if not states:
        return HTMLResponse(
            content=f'<div class="empty">No model health data yet.</div>'
        )

    cards = [_render_health_card(s) for s in states]
    html = (
        '<div style="display:flex;flex-wrap:wrap;gap:12px;">'
        + "".join(cards)
        + "</div>"
    )
    return HTMLResponse(content=html)


async def retries_fragment(request: Request):
    """Return HTML fragment with recent retry attempts table."""
    with db_connection() as db:
        try:
            rows = db.execute(
                "SELECT arc_id, content_json, created_at FROM arc_history "
                "WHERE entry_type = 'retry_attempt' "
                "ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        except sqlite3.Error as _exc:
            logger.exception("Failed to fetch retry attempts")
            rows = []

    if not rows:
        return HTMLResponse(
            content=f'<div class="empty">No retry attempts recorded.</div>'
        )

    table_rows = []
    for row in rows:
        try:
            data = json.loads(row["content_json"])
        except (json.JSONDecodeError, TypeError):
            continue

        error_type = data.get("error_type", "Unknown")
        retry_count = data.get("retry_count", "?")
        backoff = data.get("backoff_seconds", 0)
        message = data.get("error_message", "")
        # Truncate long messages
        if len(message) > 80:
            message = message[:77] + "..."

        # Color-code error types
        et_color = _COLORS["pink"] if "Rate" in error_type else _COLORS["orange"]
        if "Outage" in error_type:
            et_color = _COLORS["red"]
        elif "Network" in error_type:
            et_color = _COLORS["yellow"]

        created = row["created_at"][:19] if row["created_at"] else ""

        table_rows.append(
            f"<tr>"
            f'<td style="color:{_COLORS["muted"]};">{created}</td>'
            f'<td style="color:{_COLORS["cyan"]};">{row["arc_id"]}</td>'
            f'<td style="color:{et_color};">{error_type}</td>'
            f"<td>{retry_count}</td>"
            f"<td>{backoff:.1f}s</td>"
            f'<td style="color:{_COLORS["muted"]};font-size:12px;">{message}</td>'
            f"</tr>"
        )

    html = (
        "<table>"
        "<thead><tr>"
        "<th>Time</th><th>Arc</th><th>Error Type</th>"
        "<th>#</th><th>Backoff</th><th>Message</th>"
        "</tr></thead>"
        "<tbody>" + "".join(table_rows) + "</tbody>"
        "</table>"
    )
    return HTMLResponse(content=html)


async def errors_fragment(request: Request):
    """Return HTML fragment with error type breakdown."""
    with db_connection() as db:
        try:
            # Error counts from model_calls
            model_rows = db.execute(
                "SELECT error_type, COUNT(*) as cnt FROM model_calls "
                "WHERE success = 0 AND error_type IS NOT NULL "
                "GROUP BY error_type ORDER BY cnt DESC"
            ).fetchall()

            # Total calls for context
            total_row = db.execute(
                "SELECT COUNT(*) as total FROM model_calls"
            ).fetchone()
            total_calls = total_row["total"] if total_row else 0
        except sqlite3.Error as _exc:
            logger.exception("Failed to fetch error breakdown")
            model_rows = []
            total_calls = 0

    if not model_rows:
        return HTMLResponse(
            content=f'<div class="empty">No errors recorded.</div>'
        )

    max_count = max(r["cnt"] for r in model_rows)

    rows_html = []
    for row in model_rows:
        error_type = row["error_type"]
        count = row["cnt"]
        pct = int((count / total_calls * 100)) if total_calls > 0 else 0
        bar_width = int((count / max_count) * 100) if max_count > 0 else 0

        et_color = _COLORS["pink"]
        if "Rate" in error_type:
            et_color = _COLORS["yellow"]
        elif "Outage" in error_type:
            et_color = _COLORS["red"]
        elif "Network" in error_type:
            et_color = _COLORS["orange"]

        rows_html.append(
            f"<tr>"
            f'<td style="color:{et_color};font-weight:bold;">{error_type}</td>'
            f"<td>{count}</td>"
            f"<td>{pct}%</td>"
            f"<td>"
            f'<div style="background:{_COLORS["bg2"]};border-radius:4px;height:8px;'
            f'overflow:hidden;min-width:100px;">'
            f'<div style="background:{et_color};height:100%;width:{bar_width}%;'
            f'border-radius:4px;"></div>'
            f"</div>"
            f"</td>"
            f"</tr>"
        )

    html = (
        f'<div style="font-size:12px;color:{_COLORS["muted"]};margin-bottom:8px;">'
        f"Total calls: {total_calls}</div>"
        "<table>"
        "<thead><tr>"
        "<th>Error Type</th><th>Count</th><th>% of All</th><th>Distribution</th>"
        "</tr></thead>"
        "<tbody>" + "".join(rows_html) + "</tbody>"
        "</table>"
    )
    return HTMLResponse(content=html)


routes = [
    Route("/analytics", analytics_page, methods=["GET"]),
    Route("/api/analytics/health", health_fragment, methods=["GET"]),
    Route("/api/analytics/retries", retries_fragment, methods=["GET"]),
    Route("/api/analytics/errors", errors_fragment, methods=["GET"]),
]

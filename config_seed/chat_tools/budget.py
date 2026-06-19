"""Chat tools for inspecting and controlling the API budget circuit breaker.

These let the chat agent (and thus the user, mid-conversation) see current
spend against limits and quickly intervene — raise a limit, clear a tripped
breaker, or toggle the whole system — without editing config files. Useful
precisely when a ``cap``/``restrict`` trip is blocking autonomous work but
the user has decided the spend is legitimate.
"""

import json
import logging

from carpenter.chat_tool_loader import chat_tool
from carpenter.core import budget

logger = logging.getLogger(__name__)


@chat_tool(
    description=(
        "Show the API budget circuit breaker status: whether it's enabled, "
        "current measured usage per limit (calls / cost) against thresholds, "
        "and whether any breaker is latched (restrict/shutdown) or capping. "
        "Use to understand why autonomous work might be paused."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    capabilities=["database_read"],
)
def budget_status(tool_input, **kwargs):
    st = budget.status()
    lines = [
        f"enabled: {st['enabled']}   notify_human: {st['notify_human']}",
    ]
    if st.get("shutdown"):
        lines.append(f"SHUTDOWN latched: {st['shutdown']}")
    if st.get("restrict"):
        lines.append(f"RESTRICT latched: {st['restrict']}")
    if st.get("cap_active"):
        lines.append("CAP active for the current window (autonomous work paused).")
    lines.append("\nLimits (current measured value vs threshold):")
    measures = st.get("measures", {})
    for lim in st.get("limits", []):
        name = lim.get("name")
        val = measures.get(name)
        val_s = f"{val:.2f}" if isinstance(val, (int, float)) else "?"
        lines.append(
            f"  - {name}: {lim.get('metric')} {val_s} / {lim.get('threshold')} "
            f"over {lim.get('window_seconds')}s -> {lim.get('action')}"
        )
    overrides = st.get("threshold_overrides") or {}
    if overrides:
        lines.append(f"\nActive threshold overrides: {json.dumps(overrides)}")
    return "\n".join(lines)


@chat_tool(
    description=(
        "Control the API budget circuit breaker. Actions: "
        "'resume' clears any latched restrict/shutdown breaker so autonomous "
        "work can continue; "
        "'set_threshold' temporarily raises/lowers a named limit's threshold "
        "(provide 'name' and 'threshold', optional 'ttl_seconds' to auto-revert); "
        "'enable'/'disable' turn the whole breaker on/off. "
        "Use 'resume' after confirming a spend spike was legitimate."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["resume", "set_threshold", "enable", "disable"],
                "description": "What to do.",
            },
            "name": {
                "type": "string",
                "description": "Limit name (for set_threshold).",
            },
            "threshold": {
                "type": "number",
                "description": "New threshold value (for set_threshold).",
            },
            "ttl_seconds": {
                "type": "integer",
                "description": "Optional: auto-revert the override after N seconds.",
            },
        },
        "required": ["action"],
    },
    capabilities=["database_write"],
    trust_boundary="platform",
)
def budget_control(tool_input, **kwargs):
    action = tool_input.get("action")
    if action == "resume":
        cleared = budget.resume()
        return f"Budget breaker resumed. Cleared latches: {json.dumps(cleared)}"
    if action == "set_threshold":
        name = tool_input.get("name")
        threshold = tool_input.get("threshold")
        if not name or threshold is None:
            return "set_threshold requires 'name' and 'threshold'."
        budget.set_threshold_override(name, threshold, tool_input.get("ttl_seconds"))
        ttl = tool_input.get("ttl_seconds")
        suffix = f" (auto-reverts in {ttl}s)" if ttl else ""
        return f"Override set: {name} threshold -> {threshold}{suffix}"
    if action == "enable":
        budget.set_enabled(True)
        return "Budget breaker enabled."
    if action == "disable":
        budget.set_enabled(False)
        return "Budget breaker DISABLED. Spend is no longer bounded — re-enable soon."
    return f"Unknown action: {action!r}"

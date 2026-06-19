"""API budget circuit breaker — the universal safety net against runaway spend.

Every *paid* model call passes through :func:`guard_paid_call`, and every
*autonomous* work source (arc dispatch, cron/timer firing, trigger-driven
arc creation) passes through :func:`autonomous_allowed`. Together they
bound Anthropic API spend by call-rate and cost over configurable windows,
so that even an unforeseen feedback loop cannot burn credits unbounded.

This exists because a reflection→review trigger loop once dispatched ~2
calls/second for hours. A point-fix closed that specific loop; this module
guarantees the *class* of failure is bounded regardless of cause.

Concepts
--------
A *limit* is one independent rule::

    {name, metric, window_seconds, threshold, action, notify}

- ``metric``: ``"calls"`` (count of api_calls rows) or ``"cost_usd"``
  (token cost via the model registry) within the trailing window.
- ``action`` (escalating severity):
    - ``warn``     — notify only (rate-limited per limit); never blocks.
    - ``cap``      — block *autonomous* work until the window drains
      (self-clearing). Interactive chat keeps working so the human can
      intervene / raise the limit.
    - ``restrict`` — latch off autonomous functionality (cron/timer +
      trigger-arc creation) until a human clears it. Chat keeps working.
    - ``shutdown`` — latch a kill-switch blocking *all* paid calls until
      a human clears it. The nuclear option.

Limits are composable: set a low ``warn`` and a higher ``cap`` on the same
metric/window for a two-stage response.

State (kill-switch, restrict latch, runtime overrides, warn timestamps)
lives in the ``budget_state`` table so a process restart cannot reset an
active breaker — the original incident kept resuming across restarts.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

from .. import config
from ..db import db_transaction

logger = logging.getLogger(__name__)


class BudgetExceededError(RuntimeError):
    """Raised by :func:`guard_paid_call` when the kill-switch is active.

    Classified as fatal (non-retryable) so the invocation retry loop stops
    immediately rather than spinning — the gate runs *before* the HTTP
    request, so a retry would never spend anyway, but breaking is cleaner.
    """


# Module-level evaluation cache: limit measurement is bounded by the very
# breaker it feeds, but we still avoid a SQL aggregate on every single call.
_eval_lock = threading.Lock()
_last_eval_ts = 0.0
_cached_summary: dict | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── persistent state (budget_state kv) ──────────────────────────────


def _get_state(key: str, default=None, db=None):
    if db is not None:
        row = db.execute(
            "SELECT value_json FROM budget_state WHERE key = ?", (key,)
        ).fetchone()
    else:
        with db_transaction() as conn:
            row = conn.execute(
                "SELECT value_json FROM budget_state WHERE key = ?", (key,)
            ).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value_json"])
    except (json.JSONDecodeError, TypeError):
        return default


def _set_state(key: str, value, db=None) -> None:
    sql = (
        "INSERT INTO budget_state (key, value_json, updated_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
        "updated_at=excluded.updated_at"
    )
    args = (key, json.dumps(value), _now().isoformat())
    if db is not None:
        db.execute(sql, args)
    else:
        with db_transaction() as conn:
            conn.execute(sql, args)


def _del_state(key: str, db=None) -> None:
    if db is not None:
        db.execute("DELETE FROM budget_state WHERE key = ?", (key,))
    else:
        with db_transaction() as conn:
            conn.execute("DELETE FROM budget_state WHERE key = ?", (key,))


# ── config ──────────────────────────────────────────────────────────


def _cfg() -> dict:
    return config.CONFIG.get("api_budget", {}) or {}


def _enabled() -> bool:
    # A runtime override (set by the chat agent) wins over config so the
    # user can disable the breaker mid-conversation if it misfires.
    override = _get_state("enabled_override")
    if override is not None:
        return bool(override)
    return bool(_cfg().get("enabled", False))


def _effective_limits(db=None) -> list[dict]:
    """Config limits with any chat-set threshold overrides applied."""
    limits = _cfg().get("limits", []) or []
    overrides = _get_state("threshold_overrides", {}, db=db) or {}
    out = []
    for lim in limits:
        lim = dict(lim)
        name = lim.get("name")
        if name in overrides:
            ov = overrides[name]
            # Honour expiry so a temporary expansion auto-reverts to the
            # config threshold once the TTL passes.
            exp = ov.get("expires_at")
            if not (exp and exp < _now().isoformat()):
                lim["threshold"] = ov.get("threshold", lim.get("threshold"))
        out.append(lim)
    return out


# ── measurement ─────────────────────────────────────────────────────


def _measure(db, metric: str, window_seconds: int) -> float:
    cutoff = f"-{int(window_seconds)} seconds"
    if metric == "calls":
        row = db.execute(
            "SELECT COUNT(*) AS n FROM api_calls "
            "WHERE created_at >= datetime('now', ?)",
            (cutoff,),
        ).fetchone()
        return float(row["n"] if row else 0)
    if metric == "cost_usd":
        rows = db.execute(
            "SELECT model, "
            " SUM(input_tokens) AS inp, SUM(output_tokens) AS out, "
            " SUM(cache_creation_input_tokens) AS cc, "
            " SUM(cache_read_input_tokens) AS cr "
            "FROM api_calls WHERE created_at >= datetime('now', ?) "
            "GROUP BY model",
            (cutoff,),
        ).fetchall()
        return _cost_from_rows(rows)
    logger.warning("Unknown budget metric %r — treating as 0", metric)
    return 0.0


def _price_entry(model: str):
    """Resolve a model registry entry, tolerating dated suffixes.

    api_calls stores dated model strings (e.g. ``claude-haiku-4-5-20251001``)
    while the registry keys on the base id (``claude-haiku-4-5``). Try exact
    first, then the longest registry model_id that is a prefix of the call's
    model string.
    """
    from .models import registry

    if not model:
        return None
    if ":" in model:
        model = model.split(":", 1)[1]
    entry = registry.get_entry_by_model_id(model)
    if entry is not None:
        return entry
    best = None
    for e in registry.get_registry().values():
        mid = e.model_id or ""
        if mid and model.startswith(mid):
            if best is None or len(mid) > len(best.model_id):
                best = e
    return best


def _cost_from_rows(rows) -> float:
    total = 0.0
    for r in rows:
        entry = _price_entry(r["model"] or "")
        if entry is None:
            continue
        inp = (r["inp"] or 0)
        out = (r["out"] or 0)
        cc = (r["cc"] or 0)
        cr = (r["cr"] or 0)
        # Cache *creation* bills ~125% of base input; cache *read* bills the
        # cached rate. Approximation is fine for a safety threshold.
        total += (
            inp * entry.cost_per_mtok_in
            + cc * entry.cost_per_mtok_in * 1.25
            + cr * entry.cached_cost_per_mtok_in
            + out * entry.cost_per_mtok_out
        ) / 1_000_000.0
    return total


# ── evaluation + latching ───────────────────────────────────────────


def _maybe_warn(limit: dict, value: float, db, pending: list) -> None:
    name = limit.get("name", "?")
    # Rate-limit warns to once per window so we don't spam.
    window = int(limit.get("window_seconds", 3600))
    last = _get_state(f"warn_sent:{name}", db=db)
    now_ts = time.time()
    if last is not None and (now_ts - float(last)) < window:
        return
    _set_state(f"warn_sent:{name}", now_ts, db=db)
    pending.append((
        limit,
        f"[budget] WARN '{name}': {limit.get('metric')} reached "
        f"{value:.2f} (threshold {limit.get('threshold')}) over {window}s.",
    ))


def _latch(kind: str, limit: dict, value: float, db, pending: list) -> None:
    """Persist a restrict/shutdown latch if not already active; queue a notice."""
    state = _get_state(kind, db=db)
    if state and state.get("active"):
        return
    payload = {
        "active": True,
        "limit": limit.get("name"),
        "metric": limit.get("metric"),
        "value": round(value, 4),
        "threshold": limit.get("threshold"),
        "tripped_at": _now().isoformat(),
    }
    _set_state(kind, payload, db=db)
    verb = "RESTRICTED autonomous functionality" if kind == "restrict" else "SHUT DOWN all paid calls"
    pending.append((
        limit,
        f"[budget] {verb} — limit '{limit.get('name')}': "
        f"{limit.get('metric')}={value:.2f} >= {limit.get('threshold')}. "
        f"Clear via the budget chat tool or `carpenter budget resume`.",
    ))
    logger.critical(
        "Budget breaker %s by limit %s (%s=%.2f >= %s)",
        kind, limit.get("name"), limit.get("metric"), value, limit.get("threshold"),
    )


def _evaluate(force: bool = False) -> dict:
    """Measure all limits, fire warns, latch restrict/shutdown.

    Returns a summary dict: ``{block_autonomous, block_all, cap_active,
    restrict, shutdown, measures}``. Cached for ``eval_interval_seconds``.
    """
    global _last_eval_ts, _cached_summary

    if not _enabled():
        return {"block_autonomous": False, "block_all": False,
                "cap_active": False, "restrict": False, "shutdown": False,
                "measures": {}, "disabled": True}

    interval = float(_cfg().get("eval_interval_seconds", 5))
    with _eval_lock:
        now_ts = time.time()
        if not force and _cached_summary is not None and (now_ts - _last_eval_ts) < interval:
            return _cached_summary

        cap_active = False
        measures: dict[str, float] = {}
        pending: list = []
        with db_transaction() as db:
            for limit in _effective_limits(db):
                metric = limit.get("metric", "calls")
                window = int(limit.get("window_seconds", 3600))
                threshold = float(limit.get("threshold", 0))
                value = _measure(db, metric, window)
                measures[limit.get("name", f"{metric}/{window}")] = value
                if value < threshold:
                    continue
                action = limit.get("action", "warn")
                if action == "warn":
                    _maybe_warn(limit, value, db, pending)
                elif action == "cap":
                    cap_active = True
                    _maybe_warn(limit, value, db, pending)  # surface the cap too
                elif action == "restrict":
                    _latch("restrict", limit, value, db, pending)
                elif action == "shutdown":
                    _latch("shutdown", limit, value, db, pending)

            restrict = bool((_get_state("restrict", db=db) or {}).get("active"))
            shutdown = bool((_get_state("shutdown", db=db) or {}).get("active"))

        # Notifications run outside the DB transaction (they open their own).
        for limit, msg in pending:
            _notify(limit, msg)

        summary = {
            "block_autonomous": cap_active or restrict or shutdown,
            "block_all": shutdown,
            "cap_active": cap_active,
            "restrict": restrict,
            "shutdown": shutdown,
            "measures": measures,
        }
        _cached_summary = summary
        _last_eval_ts = now_ts
        return summary


# ── public guards ───────────────────────────────────────────────────


def guard_paid_call(model: str | None = None) -> None:
    """Block a paid model call when the shutdown kill-switch is latched.

    Cheap fast-path: a single kv read for the latch. Also opportunistically
    refreshes the evaluation (TTL-cached) so cost-based shutdown can latch
    even on a chat-only workload.

    Raises:
        BudgetExceededError: when the shutdown kill-switch is active.
    """
    if not _enabled():
        return
    # Fast latch check first (covers the common already-tripped case).
    if bool((_get_state("shutdown") or {}).get("active")):
        raise BudgetExceededError(
            "Carpenter API budget kill-switch is active — all paid model "
            "calls are blocked. Clear it via the budget chat tool or "
            "`carpenter budget resume`."
        )
    summary = _evaluate()
    if summary.get("block_all"):
        raise BudgetExceededError(
            "Carpenter API budget kill-switch tripped — all paid model "
            "calls are blocked."
        )


def autonomous_allowed() -> tuple[bool, str]:
    """Whether autonomous work (cron/timer firing, trigger-driven arc
    creation, arc dispatch) may proceed.

    Returns ``(allowed, reason)``. ``reason`` is empty when allowed.
    """
    if not _enabled():
        return True, ""
    summary = _evaluate()
    if summary.get("shutdown"):
        return False, "budget kill-switch active (shutdown)"
    if summary.get("restrict"):
        return False, "budget restrict active"
    if summary.get("cap_active"):
        return False, "budget cap reached for current window"
    return True, ""


# ── operator / chat-agent controls ──────────────────────────────────


def status() -> dict:
    """Full breaker status for the budget chat tool / CLI."""
    summary = _evaluate(force=True)
    return {
        "enabled": _enabled(),
        "notify_human": bool(_cfg().get("notify_human", False)),
        "shutdown": _get_state("shutdown"),
        "restrict": _get_state("restrict"),
        "cap_active": summary.get("cap_active", False),
        "measures": summary.get("measures", {}),
        "limits": _effective_limits(),
        "threshold_overrides": _get_state("threshold_overrides", {}),
    }


def resume() -> dict:
    """Clear all latched breakers (restrict + shutdown). Returns what was cleared."""
    cleared = {
        "restrict": _get_state("restrict"),
        "shutdown": _get_state("shutdown"),
    }
    _del_state("restrict")
    _del_state("shutdown")
    global _cached_summary, _last_eval_ts
    with _eval_lock:
        _cached_summary = None
        _last_eval_ts = 0.0
    logger.warning("Budget breaker manually resumed (cleared latches)")
    return cleared


def set_threshold_override(name: str, threshold: float,
                           ttl_seconds: int | None = None) -> None:
    """Temporarily raise/lower a named limit's threshold (chat-agent control).

    ``ttl_seconds`` auto-reverts the override so a quick expansion doesn't
    silently become permanent.
    """
    overrides = _get_state("threshold_overrides", {}) or {}
    entry = {"threshold": float(threshold)}
    if ttl_seconds:
        from datetime import timedelta
        entry["expires_at"] = (_now() + timedelta(seconds=int(ttl_seconds))).isoformat()
    overrides[name] = entry
    _set_state("threshold_overrides", overrides)
    global _cached_summary
    with _eval_lock:
        _cached_summary = None
    logger.warning("Budget threshold override set: %s -> %s (ttl=%s)",
                   name, threshold, ttl_seconds)


def set_enabled(enabled: bool) -> None:
    """Runtime enable/disable override (chat-agent control)."""
    _set_state("enabled_override", bool(enabled))
    global _cached_summary
    with _eval_lock:
        _cached_summary = None


# ── notification routing ────────────────────────────────────────────


def _notify(limit: dict, message: str) -> None:
    """Route a breaker message per the limit's notify config.

    Human messaging is gated by the top-level ``notify_human`` flag (off by
    default in the seed config) AND the per-limit ``notify.enabled`` flag.
    When suppressed we still always log.
    """
    notify_cfg = limit.get("notify") or {}
    human = bool(_cfg().get("notify_human", False)) and bool(notify_cfg.get("enabled", False))
    if not human:
        logger.warning("%s (human notify suppressed)", message)
        return
    priority = notify_cfg.get("priority", "urgent")
    try:
        from .notifications import notify as _user_notify
        _user_notify(message, priority=priority, category="budget")
    except Exception:
        logger.exception("Budget notify failed; message was: %s", message)

"""Enforcement-wiring tests for the API budget circuit breaker.

``tests/core/test_budget.py`` proves the breaker's *internal* logic (measure,
latch, cap/restrict/shutdown semantics). This file proves the breaker is
actually *wired into* the four autonomous-spend chokepoints, plus that its
error is classified as fatal so the invocation retry loop breaks instead of
spinning:

1. paid-call gate          — anthropic.call() -> guard_paid_call() (before I/O)
2. arc-dispatch gate       — scan_for_ready_arcs() -> autonomous_allowed()
3. cron-firing gate        — check_cron() -> autonomous_allowed()
4. trigger-arc-create gate — handle_subscription_create_arc() -> autonomous_allowed()
5. fatal classification    — classify_error(BudgetExceededError) -> type fatal

Each test drives the REAL chokepoint and the REAL budget functions; only
truly external effects (network, breaker notifications) are stubbed.
"""

from __future__ import annotations

import json

import pytest

from carpenter import config
from carpenter.core import budget
from carpenter.db import db_transaction, db_connection


# ── shared helpers (mirrors test_budget.py) ─────────────────────────


def _reset_cache():
    budget._cached_summary = None
    budget._last_eval_ts = 0.0


def _trip_shutdown():
    """Latch the kill-switch via the real evaluate path (0-threshold limit)."""
    config.CONFIG["api_budget"] = {
        "enabled": True,
        "eval_interval_seconds": 0,
        "limits": [{"name": "s", "metric": "calls", "window_seconds": 3600,
                    "threshold": 0, "action": "shutdown"}],
    }
    _reset_cache()


def _trip_restrict():
    """Block autonomous work (restrict latch) without blocking paid calls."""
    config.CONFIG["api_budget"] = {
        "enabled": True,
        "eval_interval_seconds": 0,
        "limits": [{"name": "r", "metric": "calls", "window_seconds": 3600,
                    "threshold": 0, "action": "restrict"}],
    }
    _reset_cache()


def _breaker_off():
    """Enabled breaker with no limits — nothing ever trips."""
    config.CONFIG["api_budget"] = {
        "enabled": True,
        "eval_interval_seconds": 0,
        "limits": [],
    }
    _reset_cache()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    _reset_cache()
    # Never route breaker messages to Signal/email during tests.
    monkeypatch.setattr(budget, "_notify", lambda limit, msg: None)
    yield
    _reset_cache()


# ── 1. paid-call gate (anthropic provider) ──────────────────────────


def test_paid_call_gate_blocks_and_makes_no_network_request(monkeypatch):
    """anthropic.call() raises BudgetExceededError from the gate and never
    touches the network when the shutdown breaker is latched."""
    from carpenter.agent.providers import anthropic

    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("network request issued despite kill-switch")

    monkeypatch.setattr(anthropic.httpx, "post", _boom)
    _trip_shutdown()

    with pytest.raises(budget.BudgetExceededError):
        anthropic.call("sys", [{"role": "user", "content": "hi"}],
                       model="claude-haiku-4-5-20251001")


def test_paid_call_gate_does_not_block_when_breaker_inactive(monkeypatch):
    """With the breaker enabled-but-untripped, the gate must let the call
    proceed to network I/O (we prove the gate passed by asserting the network
    layer — not the gate — is what runs)."""
    from carpenter.agent.providers import anthropic

    reached = {"network": False}

    def _stop(*args, **kwargs):
        reached["network"] = True
        raise RuntimeError("network reached")  # not a BudgetExceededError

    monkeypatch.setattr(anthropic.httpx, "post", _stop)
    _breaker_off()

    # The gate must NOT raise; the call fails later at the network stub.
    with pytest.raises(RuntimeError) as exc:
        anthropic.call("sys", [{"role": "user", "content": "hi"}],
                       model="claude-haiku-4-5-20251001")
    assert not isinstance(exc.value, budget.BudgetExceededError)
    assert reached["network"] is True


# ── 2. arc-dispatch gate (scan_for_ready_arcs) ──────────────────────


def _insert_pending_root_arc(name="ready-root"):
    with db_transaction() as db:
        cur = db.execute(
            "INSERT INTO arcs (parent_id, name, goal, status) "
            "VALUES (NULL, ?, 'do a thing', 'pending')",
            (name,),
        )
        return cur.lastrowid


def _dispatch_rows_for(arc_id):
    with db_connection() as db:
        return db.execute(
            "SELECT id FROM work_queue WHERE event_type = 'arc.dispatch' "
            "AND payload_json = ? AND status IN ('pending', 'claimed')",
            (json.dumps({"arc_id": arc_id}),),
        ).fetchall()


def test_arc_dispatch_gate_blocks_enqueue_when_tripped():
    """scan_for_ready_arcs() enqueues no arc.dispatch work for a ready root
    arc while the breaker is restricting autonomous work."""
    from carpenter.core.arcs import dispatch_handler

    arc_id = _insert_pending_root_arc()
    _trip_restrict()
    assert budget.autonomous_allowed()[0] is False  # precondition

    dispatch_handler.scan_for_ready_arcs()

    assert _dispatch_rows_for(arc_id) == []


def test_arc_dispatch_gate_enqueues_when_not_tripped():
    """The same ready root arc IS enqueued once the breaker is not tripped —
    proving the gate, not some other filter, is what suppressed it above."""
    from carpenter.core.arcs import dispatch_handler

    arc_id = _insert_pending_root_arc()
    _breaker_off()
    assert budget.autonomous_allowed()[0] is True  # precondition

    dispatch_handler.scan_for_ready_arcs()

    assert len(_dispatch_rows_for(arc_id)) == 1


# ── 3. cron-firing gate (check_cron) ────────────────────────────────


def _add_due_cron(name="due-cron"):
    """Register a recurring cron then force its next_fire_at into the past."""
    from carpenter.core.engine import trigger_manager

    cron_id = trigger_manager.add_cron(
        name=name, cron_expr="* * * * *", event_type="cron.message",
    )
    with db_transaction() as db:
        db.execute(
            "UPDATE cron_entries SET next_fire_at = datetime('now', '-1 minute') "
            "WHERE id = ?",
            (cron_id,),
        )
    return cron_id


def _timer_fired_count():
    with db_connection() as db:
        row = db.execute(
            "SELECT COUNT(*) AS n FROM events WHERE event_type = 'timer.fired'"
        ).fetchone()
        return row["n"]


def test_cron_gate_emits_nothing_when_tripped():
    """check_cron() returns 0 and emits no timer.fired event for a due cron
    while autonomous work is blocked."""
    from carpenter.core.engine import trigger_manager

    _add_due_cron()
    _trip_restrict()
    assert budget.autonomous_allowed()[0] is False  # precondition

    emitted = trigger_manager.check_cron()

    assert emitted == 0
    assert _timer_fired_count() == 0


def test_cron_gate_fires_due_cron_when_not_tripped():
    """A due cron emits exactly one timer.fired event when not tripped."""
    from carpenter.core.engine import trigger_manager

    _add_due_cron()
    _breaker_off()
    assert budget.autonomous_allowed()[0] is True  # precondition

    emitted = trigger_manager.check_cron()

    assert emitted == 1
    assert _timer_fired_count() == 1


# ── 4. trigger-arc-creation gate (handle_subscription_create_arc) ───


def _arc_count():
    with db_connection() as db:
        # Exclude the id=0 sentinel arc the template DB ships with.
        row = db.execute("SELECT COUNT(*) AS n FROM arcs WHERE id != 0").fetchone()
        return row["n"]


def test_trigger_arc_creation_gate_returns_none_when_tripped():
    """handle_subscription_create_arc() returns None and creates no arc while
    autonomous work is blocked."""
    from carpenter.core.engine import subscriptions

    before = _arc_count()
    _trip_restrict()
    assert budget.autonomous_allowed()[0] is False  # precondition

    result = subscriptions.handle_subscription_create_arc(
        {"_subscription": "loop-sub", "arc_name": "spawned"}
    )

    assert result is None
    assert _arc_count() == before


def test_trigger_arc_creation_proceeds_when_not_tripped():
    """The same payload creates a root arc when the breaker is not tripped —
    proving the gate is what blocked creation above (not a bad payload)."""
    from carpenter.core.engine import subscriptions

    before = _arc_count()
    _breaker_off()
    assert budget.autonomous_allowed()[0] is True  # precondition

    result = subscriptions.handle_subscription_create_arc(
        {"_subscription": "loop-sub", "arc_name": "spawned"}
    )

    assert isinstance(result, int)
    assert _arc_count() == before + 1


# ── 5. fatal error classification ───────────────────────────────────


def test_budget_error_classified_as_fatal_type():
    """classify_error(BudgetExceededError) yields type 'BudgetExceededError'
    — the signal the invocation retry loop uses to break immediately."""
    from carpenter.agent import error_classifier

    info = error_classifier.classify_error(
        budget.BudgetExceededError("x"), retry_count=1,
    )
    assert info.type == "BudgetExceededError"

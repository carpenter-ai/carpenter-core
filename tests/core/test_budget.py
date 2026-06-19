"""Tests for the API budget circuit breaker (carpenter/core/budget.py).

The breaker is the universal safety net against runaway API spend: it bounds
call-rate and cost over configurable windows and, crucially, latches its
shutdown/restrict state in the DB so a process restart cannot reset it.
"""

from __future__ import annotations

import pytest

from carpenter import config
from carpenter.core import budget
from carpenter.db import db_transaction


def _reset_cache():
    budget._cached_summary = None
    budget._last_eval_ts = 0.0


def _insert_calls(n, model="claude-haiku-4-5-20251001",
                  input_tokens=100, output_tokens=10):
    with db_transaction() as db:
        for _ in range(n):
            db.execute(
                "INSERT INTO api_calls "
                "(model, input_tokens, output_tokens, "
                " cache_creation_input_tokens, cache_read_input_tokens) "
                "VALUES (?, ?, ?, 0, 0)",
                (model, input_tokens, output_tokens),
            )


def _set_limits(limits, *, enabled=True, notify_human=False):
    config.CONFIG["api_budget"] = {
        "enabled": enabled,
        "notify_human": notify_human,
        "eval_interval_seconds": 0,  # no TTL caching in tests
        "limits": limits,
    }


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    _reset_cache()
    # Capture breaker notifications instead of routing to real channels.
    sent = []
    monkeypatch.setattr(budget, "_notify",
                        lambda limit, msg: sent.append((limit.get("name"), msg)))
    yield sent
    _reset_cache()


# ── measurement ─────────────────────────────────────────────────────


def test_measure_calls_counts_window():
    _insert_calls(5)
    with db_transaction() as db:
        assert budget._measure(db, "calls", 3600) == 5.0


def test_measure_cost_uses_registry_pricing(monkeypatch):
    # Inject deterministic pricing so the math is independent of registry
    # plumbing: $0.8/Mtok in, $4/Mtok out.
    from types import SimpleNamespace
    fake = SimpleNamespace(model_id="claude-haiku-4-5", cost_per_mtok_in=0.8,
                           cost_per_mtok_out=4.0, cached_cost_per_mtok_in=0.08)
    monkeypatch.setattr(budget, "_price_entry", lambda model: fake)
    # 1,000,000 input + 1,000,000 output → $0.8 + $4.0 = $4.80
    _insert_calls(1, input_tokens=1_000_000, output_tokens=1_000_000)
    with db_transaction() as db:
        cost = budget._measure(db, "cost_usd", 3600)
    assert cost == pytest.approx(4.8, rel=1e-3)


# ── actions ─────────────────────────────────────────────────────────


def test_warn_notifies_but_does_not_block(_clean):
    _set_limits([{"name": "w", "metric": "calls", "window_seconds": 3600,
                  "threshold": 3, "action": "warn",
                  "notify": {"enabled": True, "priority": "low"}}],
                notify_human=True)
    _insert_calls(4)
    allowed, _ = budget.autonomous_allowed()
    assert allowed is True
    assert any(name == "w" for name, _ in _clean)


def test_cap_blocks_autonomous_but_not_paid_calls():
    _set_limits([{"name": "c", "metric": "calls", "window_seconds": 3600,
                  "threshold": 1000, "action": "cap"}])
    _insert_calls(1000)
    allowed, reason = budget.autonomous_allowed()
    assert allowed is False and "cap" in reason
    # cap must NOT block the user's own paid calls (so they can intervene).
    budget.guard_paid_call("claude-haiku-4-5-20251001")  # no raise


def test_cap_self_clears_when_window_drains():
    _set_limits([{"name": "c", "metric": "calls", "window_seconds": 3600,
                  "threshold": 5, "action": "cap"}])
    _insert_calls(5)
    assert budget.autonomous_allowed()[0] is False
    # Move existing rows out of the window → cap clears.
    with db_transaction() as db:
        db.execute("UPDATE api_calls SET created_at = datetime('now', '-2 hours')")
    _reset_cache()
    assert budget.autonomous_allowed()[0] is True


def test_restrict_latches_and_blocks_autonomous_only():
    _set_limits([{"name": "r", "metric": "calls", "window_seconds": 3600,
                  "threshold": 5, "action": "restrict"}])
    _insert_calls(6)
    assert budget.autonomous_allowed()[0] is False
    budget.guard_paid_call("claude-haiku-4-5-20251001")  # restrict doesn't block paid
    # Latched: persists even after the metric drops below threshold.
    with db_transaction() as db:
        db.execute("UPDATE api_calls SET created_at = datetime('now', '-2 hours')")
    _reset_cache()
    assert budget.autonomous_allowed()[0] is False


def test_shutdown_latches_and_blocks_all_paid_calls():
    _set_limits([{"name": "s", "metric": "calls", "window_seconds": 3600,
                  "threshold": 5, "action": "shutdown"}])
    _insert_calls(6)
    with pytest.raises(budget.BudgetExceededError):
        budget.guard_paid_call("claude-haiku-4-5-20251001")
    assert budget.autonomous_allowed()[0] is False


def test_shutdown_survives_restart():
    _set_limits([{"name": "s", "metric": "calls", "window_seconds": 3600,
                  "threshold": 5, "action": "shutdown"}])
    _insert_calls(6)
    budget._evaluate(force=True)  # latch it
    # Simulate a process restart: drop in-memory cache AND the triggering rows.
    _reset_cache()
    with db_transaction() as db:
        db.execute("DELETE FROM api_calls")
    # Kill-switch is persisted in budget_state → still blocks.
    with pytest.raises(budget.BudgetExceededError):
        budget.guard_paid_call("claude-haiku-4-5-20251001")


# ── operator controls ───────────────────────────────────────────────


def test_resume_clears_latches():
    _set_limits([{"name": "s", "metric": "calls", "window_seconds": 3600,
                  "threshold": 5, "action": "shutdown"}])
    _insert_calls(6)
    budget._evaluate(force=True)
    budget.resume()
    with db_transaction() as db:
        db.execute("DELETE FROM api_calls")
    _reset_cache()
    budget.guard_paid_call("claude-haiku-4-5-20251001")  # no raise
    assert budget.autonomous_allowed()[0] is True


def test_threshold_override_lets_user_expand():
    _set_limits([{"name": "c", "metric": "calls", "window_seconds": 3600,
                  "threshold": 5, "action": "cap"}])
    _insert_calls(6)
    assert budget.autonomous_allowed()[0] is False
    budget.set_threshold_override("c", 100)
    _reset_cache()
    assert budget.autonomous_allowed()[0] is True


def test_disable_override_bypasses_breaker():
    _set_limits([{"name": "s", "metric": "calls", "window_seconds": 3600,
                  "threshold": 1, "action": "shutdown"}])
    _insert_calls(5)
    budget.set_enabled(False)
    _reset_cache()
    budget.guard_paid_call("claude-haiku-4-5-20251001")  # disabled → no raise
    assert budget.autonomous_allowed()[0] is True


def test_disabled_by_config_is_noop():
    _set_limits([{"name": "s", "metric": "calls", "window_seconds": 3600,
                  "threshold": 1, "action": "shutdown"}], enabled=False)
    _insert_calls(5)
    budget.guard_paid_call("claude-haiku-4-5-20251001")  # no raise
    assert budget.autonomous_allowed() == (True, "")

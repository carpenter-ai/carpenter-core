"""Tests for the Signal notification channel, budget chat tools, and budget
pricing resolution.

Three areas under test:
  A) ``carpenter.core.notifications._send_signal`` — the Signal REST channel
     (config gating, HTTP request shape, error tolerance) and its reachability
     via the public ``notify()`` routing for urgent priority.
  B) ``config_seed/chat_tools/budget.py`` — the ``budget_status`` /
     ``budget_control`` chat tools wrapping ``carpenter.core.budget``.
  C) ``carpenter.core.budget._price_entry`` — model-string -> registry entry
     resolution tolerating dated suffixes and ``provider:`` prefixes.

Only the true boundaries are mocked: ``urllib.request.urlopen`` for the HTTP
calls, and the model registry source for pricing. The units under test run for
real.
"""

import importlib.util
import json
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from carpenter import config
from carpenter.core import budget
from carpenter.core import notifications


# ── Helpers ─────────────────────────────────────────────────────────


def _reset_budget_cache():
    budget._cached_summary = None
    budget._last_eval_ts = 0.0


def _load_budget_tools():
    """Import config_seed/chat_tools/budget.py by file path.

    It is a config_seed module (not on the package path), so load it via
    importlib like the chat_tool_loader does. The ``@chat_tool`` decorator
    returns the function unchanged, so the loaded functions are directly
    callable.
    """
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "config_seed" / "chat_tools" / "budget.py"
    spec = importlib.util.spec_from_file_location("_test_budget_tool", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResp:
    """Minimal context-manager standing in for an http.client.HTTPResponse."""

    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


@pytest.fixture
def signal_enabled():
    """Configure an enabled, fully-specified Signal channel."""
    config.CONFIG["notifications"] = {
        "batch_window": 0,
        "routing": {},
        "signal": {
            "enabled": True,
            "base_url": "http://signal.local:8080/",
            "bot_number": "+15550001111",
            "recipient": "+15559998888",
            "timeout": 7,
        },
    }


# ── A) Signal channel ───────────────────────────────────────────────


def test_signal_disabled_makes_no_http_call(monkeypatch):
    """enabled=False -> returns False and never touches the network."""
    config.CONFIG["notifications"] = {"signal": {"enabled": False}}
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: calls.append(a) or _FakeResp(200))

    assert notifications._send_signal("hi", "urgent", None) is False
    assert calls == []


def test_signal_missing_bot_or_recipient_returns_false(monkeypatch):
    """Enabled but missing bot_number/recipient -> False, no HTTP call."""
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: calls.append(a) or _FakeResp(200))

    config.CONFIG["notifications"] = {
        "signal": {"enabled": True, "bot_number": "", "recipient": "+1555"},
    }
    assert notifications._send_signal("hi", "urgent", None) is False

    config.CONFIG["notifications"] = {
        "signal": {"enabled": True, "bot_number": "+1555", "recipient": ""},
    }
    assert notifications._send_signal("hi", "urgent", None) is False

    assert calls == []


def test_signal_success_posts_correct_request(monkeypatch, signal_enabled):
    """A 200 response -> True; POST to {base_url}/v2/send with message/number/recipient."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        return _FakeResp(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = notifications._send_signal("the message body", "urgent", None)
    assert result is True

    req = captured["req"]
    # URL: base_url had a trailing slash that must be stripped before /v2/send.
    assert req.full_url == "http://signal.local:8080/v2/send"
    assert req.full_url.endswith("/v2/send")
    assert req.get_method() == "POST"
    assert req.headers.get("Content-type") == "application/json"

    body = json.loads(req.data.decode("utf-8"))
    assert body["message"] == "the message body"
    assert body["number"] == "+15550001111"
    assert body["recipients"] == ["+15559998888"]

    # Configured timeout is honoured.
    assert captured["timeout"] == 7


def test_signal_http_error_returns_false(monkeypatch, signal_enabled):
    """An HTTPError from urlopen is swallowed -> returns False, never raises."""
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 500, "Server Error", hdrs=None, fp=None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert notifications._send_signal("hi", "urgent", None) is False


def test_signal_url_error_returns_false(monkeypatch, signal_enabled):
    """A transport URLError is swallowed -> returns False, never raises."""
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert notifications._send_signal("hi", "urgent", None) is False


def test_notify_routes_urgent_to_signal(monkeypatch):
    """notify(priority='urgent') with signal in default_routing reaches _send_signal."""
    config.CONFIG["notifications"] = {
        "batch_window": 0,  # immediate delivery, no batching thread
        "routing": {},
        "default_routing": {"urgent": ["signal"]},
        "signal": {
            "enabled": True,
            "base_url": "http://signal.local:8080",
            "bot_number": "+15550001111",
            "recipient": "+15559998888",
            "timeout": 5,
        },
    }
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeResp(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    notifications.notify("urgent thing", priority="urgent")

    assert "req" in captured, "notify() did not reach _send_signal"
    assert captured["req"].full_url.endswith("/v2/send")
    body = json.loads(captured["req"].data.decode("utf-8"))
    assert body["message"] == "urgent thing"


# ── B) Budget chat tools ────────────────────────────────────────────


@pytest.fixture
def budget_tools(monkeypatch):
    """Load the budget chat tools and silence real notifications."""
    mod = _load_budget_tools()
    # Suppress human/log notifications fired during evaluation/latching.
    monkeypatch.setattr(budget, "_notify", lambda *a, **k: None)
    _reset_budget_cache()
    return mod


def test_budget_status_reflects_config(budget_tools):
    """budget_status({}) returns a string showing enabled state and limit names."""
    config.CONFIG["api_budget"] = {
        "enabled": True,
        "notify_human": False,
        "limits": [
            {"name": "calls-hourly", "metric": "calls",
             "window_seconds": 3600, "threshold": 1000, "action": "warn"},
        ],
    }
    _reset_budget_cache()

    out = budget_tools.budget_status({})
    assert isinstance(out, str)
    assert "enabled: True" in out
    assert "calls-hourly" in out


def test_budget_control_resume_clears_shutdown_latch(budget_tools):
    """resume clears a latched shutdown breaker so it no longer shows active."""
    config.CONFIG["api_budget"] = {
        "enabled": True,
        "notify_human": False,
        "limits": [
            {"name": "kill", "metric": "calls", "window_seconds": 3600,
             "threshold": 0, "action": "shutdown"},
        ],
    }
    _reset_budget_cache()

    # threshold 0 with metric calls trips immediately (value >= 0).
    budget._evaluate(force=True)
    assert (budget.status()["shutdown"] or {}).get("active") is True

    # Raise the limit so re-evaluation won't immediately re-latch — otherwise
    # resume() correctly clears the latch but the still-tripping condition
    # re-trips it on the next status() evaluation. This proves resume cleared
    # the persisted latch state.
    config.CONFIG["api_budget"]["limits"][0]["threshold"] = 10_000_000
    _reset_budget_cache()

    msg = budget_tools.budget_control({"action": "resume"})
    assert "resumed" in msg.lower()
    # The cleared payload reports what was latched before clearing.
    cleared = json.loads(msg.split("Cleared latches:", 1)[1])
    assert (cleared.get("shutdown") or {}).get("active") is True

    _reset_budget_cache()
    st = budget.status()
    assert not (st["shutdown"] or {}).get("active")
    assert not (st["restrict"] or {}).get("active")


def test_budget_control_set_threshold_records_override(budget_tools):
    """set_threshold records an override visible in status()."""
    config.CONFIG["api_budget"] = {
        "enabled": True,
        "notify_human": False,
        "limits": [
            {"name": "calls-hourly", "metric": "calls",
             "window_seconds": 3600, "threshold": 10, "action": "warn"},
        ],
    }
    _reset_budget_cache()

    msg = budget_tools.budget_control(
        {"action": "set_threshold", "name": "calls-hourly", "threshold": 999}
    )
    assert "calls-hourly" in msg

    overrides = budget.status()["threshold_overrides"]
    assert "calls-hourly" in overrides
    assert overrides["calls-hourly"]["threshold"] == 999


def test_budget_control_set_threshold_requires_args(budget_tools):
    """set_threshold without name/threshold returns a clear error, no exception."""
    config.CONFIG["api_budget"] = {"enabled": True, "limits": []}
    _reset_budget_cache()
    msg = budget_tools.budget_control({"action": "set_threshold"})
    assert "requires" in msg.lower()


def test_budget_control_disable_then_enable(budget_tools):
    """disable bypasses the breaker (autonomous allowed even when tripped);
    enable restores enforcement."""
    config.CONFIG["api_budget"] = {
        "enabled": True,
        "notify_human": False,
        "limits": [
            {"name": "kill", "metric": "calls", "window_seconds": 3600,
             "threshold": 0, "action": "shutdown"},
        ],
    }
    _reset_budget_cache()

    # Latch shutdown so the breaker would otherwise block autonomous work.
    budget._evaluate(force=True)
    _reset_budget_cache()
    allowed, _reason = budget.autonomous_allowed()
    assert allowed is False

    budget_tools.budget_control({"action": "disable"})
    _reset_budget_cache()
    allowed, reason = budget.autonomous_allowed()
    assert allowed is True
    assert reason == ""

    budget_tools.budget_control({"action": "enable"})
    _reset_budget_cache()
    allowed, _reason = budget.autonomous_allowed()
    assert allowed is False  # latch still present, enforcement restored


def test_budget_control_unknown_action(budget_tools):
    """An unrecognised action returns an 'unknown action' string, no exception."""
    config.CONFIG["api_budget"] = {"enabled": True, "limits": []}
    _reset_budget_cache()
    msg = budget_tools.budget_control({"action": "bogus"})
    assert "unknown action" in msg.lower()
    assert "bogus" in msg


# ── C) Pricing resolution (_price_entry) ────────────────────────────


def _entry(model_id, cin=1.0, cout=2.0, ccached=0.1):
    """A ModelEntry-like object with just the fields _price_entry touches."""
    return types.SimpleNamespace(
        model_id=model_id,
        cost_per_mtok_in=cin,
        cost_per_mtok_out=cout,
        cached_cost_per_mtok_in=ccached,
    )


def _patch_registry(monkeypatch, entries):
    """Monkeypatch the registry source used by _price_entry.

    entries: dict of registry-key -> entry object. get_entry_by_model_id does
    an exact model_id match; get_registry returns the full dict for prefix
    fallback.
    """
    from carpenter.core.models import registry

    def fake_get_by_id(model_id):
        mid = model_id.split(":", 1)[1] if ":" in model_id else model_id
        for e in entries.values():
            if e.model_id == mid:
                return e
        return None

    monkeypatch.setattr(registry, "get_entry_by_model_id", fake_get_by_id)
    monkeypatch.setattr(registry, "get_registry", lambda: dict(entries))


def test_price_entry_exact_match(monkeypatch):
    """An exact model_id resolves via get_entry_by_model_id."""
    haiku = _entry("claude-haiku-4-5")
    _patch_registry(monkeypatch, {"haiku": haiku})
    assert budget._price_entry("claude-haiku-4-5") is haiku


def test_price_entry_dated_suffix(monkeypatch):
    """A dated suffix resolves to the base entry via longest-prefix fallback."""
    haiku = _entry("claude-haiku-4-5")
    _patch_registry(monkeypatch, {"haiku": haiku})
    # Exact lookup misses (registry keys on the base id); prefix match wins.
    assert budget._price_entry("claude-haiku-4-5-20251001") is haiku


def test_price_entry_provider_prefix(monkeypatch):
    """A provider:-prefixed dated model resolves after stripping the prefix."""
    haiku = _entry("claude-haiku-4-5")
    _patch_registry(monkeypatch, {"haiku": haiku})
    assert budget._price_entry("anthropic:claude-haiku-4-5-20251001") is haiku


def test_price_entry_unknown_returns_none(monkeypatch):
    """An unrelated model string resolves to None."""
    haiku = _entry("claude-haiku-4-5")
    _patch_registry(monkeypatch, {"haiku": haiku})
    assert budget._price_entry("gpt-4o-2024-05-13") is None


def test_price_entry_longest_prefix_wins(monkeypatch):
    """When several registry ids are prefixes, the longest one wins."""
    short = _entry("claude-sonnet-4")
    long = _entry("claude-sonnet-4-5")
    _patch_registry(monkeypatch, {"short": short, "long": long})
    # Both "claude-sonnet-4" and "claude-sonnet-4-5" prefix the dated string,
    # but the more specific (longer) id should be chosen.
    assert budget._price_entry("claude-sonnet-4-5-20251101") is long

"""Telling the conversation what the install did — for no model calls.

The property that matters most here is a negative one: notifying the
operator must cost zero inference.  The outcome is already exact, so the
notification is an INSERT the web UI renders from its existing poll, not
a round trip through the chat agent.
"""

from __future__ import annotations

import asyncio

import pytest

from carpenter.agent import conversation
from carpenter.db import db_connection
from carpenter.packages import install_batch as ib
from carpenter.packages import install_notify as notify
from carpenter.packages import pending_install as pi


# ── record builders ─────────────────────────────────────────────────

def _result(
    *,
    name="alpha",
    status=ib.PKG_STATUS_INSTALLED,
    version="1.0.0",
    loaded=True,
    needs_restart=False,
    verbs=(),
    declared=0,
    rollback=None,
    error=None,
    batch_status="installed",
    rolled_back=(),
    kept=(),
):
    return {
        "status": batch_status,
        "packages": [{
            "name": name,
            "spec": "git+https://forge.invalid/org/alpha.git",
            "source_url": "https://forge.invalid/org/alpha.git",
            "status": status,
            "version": version,
            "hash": "a" * 64,
            "hash_short": "a" * 12,
            "previous_version": None,
            "capabilities_declared": declared,
            "capabilities_granted": bool(verbs),
            "granted_verbs": list(verbs),
            "write_chat_tools_requested": False,
            "write_chat_tools_allowed": False,
            "load": {
                "loaded": loaded,
                "needs_restart": needs_restart,
                "reason": "registered" if loaded else "already imported",
                "components": {},
            },
            "rollback": rollback,
            "error": error,
        }],
        "rolled_back": list(rolled_back),
        "not_rolled_back": [],
        "security_policies_kept": list(kept),
        "hot_load": {"attempted": True, "error": None},
        "error": error,
    }


def _record(**kw):
    defaults = dict(
        id=1,
        token="11111111-2222-3333-4444-555555555555",
        status=pi.STATUS_INSTALLED,
        requested_at=pi.utc_now_iso(),
        expires_at=pi.utc_now_iso(),
        conversation_id=None,
        channel_type=None,
        result=_result(),
    )
    defaults.update(kw)
    return pi.PendingInstall(**defaults)


@pytest.fixture
def conv_id():
    return conversation.create_conversation()


@pytest.fixture
def no_model(monkeypatch):
    """Fail loudly if anything tries to call a model."""
    from carpenter.agent import invocation

    calls: list = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return {"response": "should not happen"}

    monkeypatch.setattr(invocation, "invoke_for_chat", spy)
    return calls


def _messages(conv_id):
    with db_connection() as db:
        return db.execute(
            "SELECT role, content, hidden FROM messages "
            "WHERE conversation_id = ? ORDER BY id",
            (conv_id,),
        ).fetchall()


# ── the notification itself ─────────────────────────────────────────

def test_the_outcome_is_a_visible_system_message(conv_id, no_model):
    record = _record(conversation_id=conv_id)

    assert asyncio.run(notify.notify_install_outcome(record)) is True

    rows = _messages(conv_id)
    assert len(rows) == 1
    assert rows[0]["role"] == "system"
    assert rows[0]["hidden"] == 0, "the operator has to be able to see it"
    assert "installed alpha v1.0.0" in rows[0]["content"]


def test_notifying_calls_no_model(conv_id, no_model):
    asyncio.run(notify.notify_install_outcome(_record(conversation_id=conv_id)))
    assert no_model == [], "a notification must cost zero inference"


def test_notify_agent_is_off_by_default():
    assert notify.notify_agent_enabled({}) is False
    assert notify.notify_agent_enabled({"package_install": {}}) is False
    from carpenter import config as _config
    assert _config.DEFAULTS["package_install"]["notify_agent"] is False


def test_notify_agent_on_re_invokes_the_agent(conv_id, no_model):
    record = _record(conversation_id=conv_id)

    asyncio.run(notify.notify_install_outcome(
        record, config={"package_install": {"notify_agent": True}},
    ))

    assert len(no_model) == 1
    args, kwargs = no_model[0]
    assert kwargs["conversation_id"] == conv_id
    assert kwargs["_message_already_saved"] is True
    assert kwargs["_system_triggered"] is True


def test_a_failing_re_invocation_does_not_fail_the_notification(
    conv_id, monkeypatch,
):
    from carpenter.agent import invocation

    def boom(*args, **kwargs):
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(invocation, "invoke_for_chat", boom)
    record = _record(conversation_id=conv_id)

    assert asyncio.run(notify.notify_install_outcome(
        record, config={"package_install": {"notify_agent": True}},
    )) is True
    assert len(_messages(conv_id)) == 1


def test_a_request_with_no_conversation_tells_nobody(no_model):
    assert asyncio.run(
        notify.notify_install_outcome(_record(conversation_id=None)),
    ) is False


def test_the_message_never_carries_the_token(conv_id, no_model):
    record = _record(conversation_id=conv_id)
    message = notify.render_install_message(record)
    assert record.token not in message
    assert record.token_prefix not in message


# ── channel push ────────────────────────────────────────────────────

class _FakeConnector:
    kind = "channel"

    def __init__(self, channel_type="signal", fail=False):
        self.channel_type = channel_type
        self.enabled = True
        self.started = True
        self.fail = fail
        self.sent: list = []

    async def send_message(self, conversation_id, text, metadata=None):
        if self.fail:
            raise RuntimeError("the socket is gone")
        self.sent.append((conversation_id, text))
        return True


class _FakeRegistry:
    def __init__(self, connectors):
        self._connectors = connectors

    def list_connectors(self, kind=None):
        return [c for c in self._connectors if kind is None or c.kind == kind]


@pytest.fixture
def connectors(monkeypatch):
    def _install(*cs):
        from carpenter.channels import registry as channels_registry
        monkeypatch.setattr(
            channels_registry, "get_connector_registry",
            lambda: _FakeRegistry(list(cs)),
        )
        return cs
    return _install


def test_a_non_web_channel_gets_a_push(conv_id, no_model, connectors):
    (signal,) = connectors(_FakeConnector("signal"))
    record = _record(conversation_id=conv_id, channel_type="signal")

    asyncio.run(notify.notify_install_outcome(record))

    assert len(signal.sent) == 1
    assert signal.sent[0][0] == conv_id
    assert "installed alpha" in signal.sent[0][1]


def test_a_web_conversation_is_not_pushed_to(conv_id, no_model, connectors):
    (web,) = connectors(_FakeConnector("web"))
    record = _record(conversation_id=conv_id, channel_type="web")

    asyncio.run(notify.notify_install_outcome(record))

    assert web.sent == [], "the web UI polls; a push would duplicate it"
    assert len(_messages(conv_id)) == 1


def test_a_failed_push_does_not_fail_the_notification(
    conv_id, no_model, connectors,
):
    (signal,) = connectors(_FakeConnector("signal", fail=True))
    record = _record(conversation_id=conv_id, channel_type="signal")

    assert asyncio.run(notify.notify_install_outcome(record)) is True
    assert len(_messages(conv_id)) == 1


def test_an_unstarted_connector_is_not_used(conv_id, no_model, connectors):
    (signal,) = connectors(_FakeConnector("signal"))
    signal.started = False
    record = _record(conversation_id=conv_id, channel_type="signal")

    asyncio.run(notify.notify_install_outcome(record))

    assert signal.sent == []


# ── message content ─────────────────────────────────────────────────

def test_the_message_names_verbs_and_the_live_load():
    record = _record(
        conversation_id=1,
        result=_result(declared=1, verbs=("mailer.fetch",)),
    )
    message = notify.render_install_message(record)
    assert "mailer.fetch" in message
    assert "loaded live" in message
    assert "hash " + "a" * 12 in message
    assert "https://forge.invalid/org/alpha.git" in message


def test_the_message_says_a_restart_is_needed():
    record = _record(
        conversation_id=1,
        result=_result(loaded=False, needs_restart=True),
    )
    message = notify.render_install_message(record)
    assert "already imported in this process" in message
    assert "A platform restart is needed" in message
    assert "Nothing was restarted automatically" in message


def test_a_failed_batch_reports_the_rollback():
    record = _record(
        status=pi.STATUS_FAILED,
        conversation_id=1,
        error="beta failed to install: boom",
        result=_result(
            status=ib.PKG_STATUS_ROLLED_BACK,
            rollback="alpha was installed by this batch and has been "
                     "uninstalled again",
            batch_status="failed",
            rolled_back=("alpha",),
            kept=[{"type": "domain", "value": "mail.example.com",
                   "package": "alpha"}],
        ),
    )
    message = notify.render_install_message(record)

    assert "FAILED" in message
    assert "rolled back alpha" in message
    assert "Rolled back" in message
    assert "mail.example.com" in message
    assert "one-way ratchet" in message
    assert "beta failed to install: boom" in message
    assert "nothing was auto-retried" in message


def test_a_refusal_with_no_packages_still_says_why():
    record = _record(
        status=pi.STATUS_FAILED,
        conversation_id=1,
        error="the fetched tree is no longer on disk; ask again",
        result={"status": "failed", "packages": []},
    )
    message = notify.render_install_message(record)
    assert "no longer on disk" in message
    assert "ask again" in message

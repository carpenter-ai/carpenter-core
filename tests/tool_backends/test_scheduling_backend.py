"""Tests for the scheduling tool backend."""
import pytest

from carpenter.tool_backends.scheduling import (
    handle_add_once,
    handle_add_cron,
    handle_remove_cron,
    handle_list_cron,
    handle_enable_cron,
    ALLOWED_EVENT_TYPES,
)


def test_handle_add_cron():
    result = handle_add_cron({
        "name": "daily-check",
        "cron_expr": "0 9 * * *",
        "event_type": "cron.message",
        "event_payload": {"message": "hello"},
    })
    assert "cron_id" in result
    assert isinstance(result["cron_id"], int)


def test_handle_add_cron_arc_dispatch():
    result = handle_add_cron({
        "name": "arc-job",
        "cron_expr": "0 9 * * *",
        "event_type": "arc.dispatch",
        "event_payload": {"arc_id": 42},
    })
    assert "cron_id" in result


def test_handle_add_once_cron_message():
    result = handle_add_once({
        "name": "one-shot-msg",
        "at_iso": "2030-12-31T23:59:00",
        "event_type": "cron.message",
        "event_payload": {"message": "reminder!"},
    })
    assert "cron_id" in result


def test_handle_add_once_arc_dispatch():
    result = handle_add_once({
        "name": "one-shot-arc",
        "at_iso": "2030-12-31T23:59:00",
        "event_type": "arc.dispatch",
        "event_payload": {"arc_id": 99},
    })
    assert "cron_id" in result


def test_handle_remove_cron():
    handle_add_cron({
        "name": "to-remove",
        "cron_expr": "0 0 * * *",
        "event_type": "cron.message",
    })
    result = handle_remove_cron({"name": "to-remove"})
    assert result["removed"] is True

    # Removing again should return False
    result = handle_remove_cron({"name": "to-remove"})
    assert result["removed"] is False


def test_handle_list_cron():
    handle_add_cron({
        "name": "job-a",
        "cron_expr": "*/5 * * * *",
        "event_type": "cron.message",
    })
    handle_add_cron({
        "name": "job-b",
        "cron_expr": "*/10 * * * *",
        "event_type": "arc.dispatch",
    })
    result = handle_list_cron({})
    assert len(result["entries"]) == 2
    names = [e["name"] for e in result["entries"]]
    assert "job-a" in names
    assert "job-b" in names


def test_handle_enable_cron():
    handle_add_cron({
        "name": "toggleable",
        "cron_expr": "0 * * * *",
        "event_type": "cron.message",
    })
    # Disable it
    result = handle_enable_cron({"name": "toggleable", "enabled": False})
    assert result["found"] is True

    # Verify it's disabled
    entries = handle_list_cron({})["entries"]
    entry = [e for e in entries if e["name"] == "toggleable"][0]
    assert entry["enabled"] is False or entry["enabled"] == 0

    # Enable it again
    result = handle_enable_cron({"name": "toggleable", "enabled": True})
    assert result["found"] is True

    # Non-existent entry
    result = handle_enable_cron({"name": "nonexistent", "enabled": True})
    assert result["found"] is False


# ── event_type validation ────────────────────────────────────────────


@pytest.mark.parametrize("bad_type", [
    "chat_message",
    "user_message",
    "scheduled_message",
    "cron.fire",
    "message",
    "",
])
def test_add_cron_rejects_invalid_event_type(bad_type):
    with pytest.raises(ValueError, match="Invalid event_type"):
        handle_add_cron({
            "name": "bad-cron",
            "cron_expr": "*/5 * * * *",
            "event_type": bad_type,
        })


@pytest.mark.parametrize("bad_type", [
    "chat_message",
    "user_message",
    "scheduled_message",
])
def test_add_once_rejects_invalid_event_type(bad_type):
    with pytest.raises(ValueError, match="Invalid event_type"):
        handle_add_once({
            "name": "bad-once",
            "at_iso": "2030-12-31T23:59:00",
            "event_type": bad_type,
        })


@pytest.mark.parametrize("good_type", sorted(ALLOWED_EVENT_TYPES))
def test_add_cron_accepts_valid_event_types(good_type):
    result = handle_add_cron({
        "name": f"valid-{good_type}",
        "cron_expr": "0 * * * *",
        "event_type": good_type,
    })
    assert "cron_id" in result


def test_add_once_merges_conversation_id():
    """conversation_id from callback context is injected into event_payload."""
    result = handle_add_once({
        "name": "ctx-test",
        "at_iso": "2030-12-31T23:59:00",
        "event_type": "cron.message",
        "event_payload": {"message": "hi"},
        "conversation_id": 42,
    })
    assert "cron_id" in result

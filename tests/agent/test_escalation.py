"""Integration tests for model escalation flows."""

import json
from unittest.mock import patch
import pytest
from carpenter.agent import invocation, conversation
from carpenter.tool_backends import state as state_backend
from carpenter import config

# ---------------------------------------------------------------------------
# Model name constants — update HERE when models change, not in every test.
# ---------------------------------------------------------------------------
MODEL_HAIKU = "anthropic:claude-haiku-4.5-20241022"
MODEL_SONNET = "anthropic:claude-sonnet-4-20250514"
MODEL_OPUS = "anthropic:claude-opus-4-6"
MODEL_OLLAMA_CODER = "ollama:qwen2.5-coder:32b"
MODEL_OLLAMA_SMALL = "ollama:qwen2.5:7b"


@pytest.fixture(autouse=True)
def mock_escalation_config(test_db, monkeypatch):
    """Inject test escalation config for all tests.

    Must depend on test_db to ensure database paths are preserved.
    """
    # Get current config (which has test database paths from test_db fixture)
    current_config = config.CONFIG.copy()

    # Add escalation config
    current_config.update({
        "model_roles": {**config.CONFIG.get("model_roles", {}), "chat": MODEL_HAIKU},
        "escalation": {
            "require_confirmation": True,
            "stacks": {
                "coding": [
                    MODEL_HAIKU,
                    MODEL_SONNET,
                    MODEL_OPUS,
                ],
                "general": [
                    MODEL_HAIKU,
                    MODEL_SONNET,
                ],
            },
            "pricing": {
                MODEL_HAIKU: [0.80, 4.00],
                MODEL_SONNET: [3.00, 15.00],
                MODEL_OPUS: [15.00, 75.00],
            },
        },
    })
    monkeypatch.setattr(config, "CONFIG", current_config)


@pytest.fixture
def conv_id():
    """Create a test conversation."""
    return conversation.create_conversation()


def test_escalate_tool_stores_pending(conv_id, test_db):
    """Escalation tool execution stores pending state correctly."""
    from carpenter.db import get_db
    # Record an API call to establish current model
    db = get_db()
    db.execute(
        "INSERT INTO api_calls (conversation_id, model, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?)",
        (conv_id, MODEL_HAIKU, 100, 50),
    )
    db.commit()
    db.close()

    # Execute escalation tool
    result = invocation._execute_chat_tool(
        "escalate_current_arc",
        {"reason": "Complex refactoring needed", "task_type": "coding"},
        conversation_id=conv_id,
    )

    # Should return confirmation prompt
    assert f"escalate to {MODEL_SONNET}".lower() in result.lower()
    assert "~3x" in result
    assert "Complex refactoring needed" in result

    # Check pending state was stored
    pending = state_backend.handle_get({"arc_id": 0, "key": "pending_escalation"})
    assert pending["value"]["target_model"] == MODEL_SONNET
    assert pending["value"]["reason"] == "Complex refactoring needed"
    assert pending["value"]["task_type"] == "coding"


def test_escalate_tool_already_at_top(conv_id, test_db):
    """Return message when already at highest tier."""
    from carpenter.db import get_db
    # Record API call with top-tier model
    db = get_db()
    db.execute(
        "INSERT INTO api_calls (conversation_id, model, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?)",
        (conv_id, MODEL_OPUS, 100, 50),
    )
    db.commit()
    db.close()

    # Try to escalate
    result = invocation._execute_chat_tool(
        "escalate_current_arc",
        {"reason": "Need more power", "task_type": "coding"},
        conversation_id=conv_id,
    )

    assert "highest available model tier" in result.lower()


def test_escalation_approval_yes(conv_id, test_db, monkeypatch):
    """User approves escalation with 'yes'."""
    # Set up pending escalation
    state_backend.handle_set({
        "arc_id": 0,
        "key": "pending_escalation",
        "value": {
            "target_model": MODEL_SONNET,
            "reason": "Complex task",
            "task_type": "coding",
            "conversation_id": conv_id,
        },
    })

    # Mock _invoke_with_escalated_model to track the call
    called_with = {}
    def mock_invoke(user_msg, conv_id, target_model, reason, api_key):
        called_with["target_model"] = target_model
        called_with["reason"] = reason
        return {
            "conversation_id": conv_id,
            "response_text": "Escalated response",
            "code": None,
            "message_id": 123,
        }
    monkeypatch.setattr(invocation, "_invoke_with_escalated_model", mock_invoke)

    # User says "yes"
    result = invocation.invoke_for_chat("yes", conversation_id=conv_id)

    # Should have called _invoke_with_escalated_model
    assert called_with["target_model"] == MODEL_SONNET
    assert called_with["reason"] == "Complex task"
    assert result["response_text"] == "Escalated response"

    # Pending state should be cleared
    pending = state_backend.handle_get({"arc_id": 0, "key": "pending_escalation"})
    assert pending["value"] is None

    # Escalation history should be logged
    history = state_backend.handle_get({"arc_id": 0, "key": "escalation_history"})
    assert len(history["value"]) == 1
    assert history["value"][0]["target_model"] == MODEL_SONNET


def test_escalation_approval_no(conv_id, test_db, monkeypatch):
    """User declines escalation with 'no'."""
    # Set up pending escalation
    state_backend.handle_set({
        "arc_id": 0,
        "key": "pending_escalation",
        "value": {
            "target_model": MODEL_SONNET,
            "reason": "Complex task",
            "task_type": "coding",
            "conversation_id": conv_id,
        },
    })

    # Mock AI call to verify normal flow continues
    def mock_call(*args, **kwargs):
        return {
            "content": [{"type": "text", "text": "Normal response"}],
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "stop_reason": "end_turn",
        }
    monkeypatch.setattr(invocation.claude_client, "call", mock_call)

    # User says "no"
    result = invocation.invoke_for_chat("no", conversation_id=conv_id)

    # Should continue with normal model
    assert "Normal response" in result["response_text"]

    # Pending state should be cleared
    pending = state_backend.handle_get({"arc_id": 0, "key": "pending_escalation"})
    assert pending["value"] is None


def test_escalation_approval_ambiguous(conv_id, test_db):
    """Ambiguous response clears stale escalation and continues normal invocation."""
    # Set up pending escalation
    state_backend.handle_set({
        "arc_id": 0,
        "key": "pending_escalation",
        "value": {
            "target_model": MODEL_SONNET,
            "reason": "Complex task",
            "task_type": "coding",
            "conversation_id": conv_id,
        },
    })

    # User says something ambiguous
    result = invocation.invoke_for_chat("maybe later", conversation_id=conv_id)

    # Pending state should be cleared (no longer blocks chat)
    pending = state_backend.handle_get({"arc_id": 0, "key": "pending_escalation"})
    assert pending["value"] is None

    # Result should be a normal invocation (response_text from AI, not a re-prompt)
    assert result["conversation_id"] == conv_id


def test_escalation_with_auto_approval(conv_id, test_db, monkeypatch):
    """Skip confirmation when require_confirmation is False."""
    # Update config to disable confirmation
    test_config = {
        **config.CONFIG,
        "escalation": {
            **config.CONFIG["escalation"],
            "require_confirmation": False,
        },
    }
    monkeypatch.setattr(config, "CONFIG", test_config)

    # Record API call
    from carpenter.db import get_db
    db = get_db()
    db.execute(
        "INSERT INTO api_calls (conversation_id, model, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?)",
        (conv_id, MODEL_HAIKU, 100, 50),
    )
    db.commit()
    db.close()

    # Execute escalation tool
    result = invocation._execute_chat_tool(
        "escalate_current_arc",
        {"reason": "Complex refactoring", "task_type": "coding"},
        conversation_id=conv_id,
    )

    # Should auto-approve
    assert "escalated to" in result.lower()
    assert "continuing" in result.lower()


def test_escalation_tracks_model_in_api_calls(conv_id, test_db, monkeypatch):
    """API calls table records the escalated model name."""
    # Mock AI call
    def mock_call(*args, **kwargs):
        return {
            "content": [{"type": "text", "text": "Escalated response"}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "stop_reason": "end_turn",
        }
    monkeypatch.setattr(invocation.claude_client, "call", mock_call)

    # Call _invoke_with_escalated_model directly
    result = invocation._invoke_with_escalated_model(
        "test message",
        conv_id,
        MODEL_SONNET,
        "Testing escalation",
        api_key=None,
    )

    # Check API call was logged with correct model
    from carpenter.db import get_db
    db = get_db()
    row = db.execute(
        "SELECT model FROM api_calls WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
        (conv_id,)
    ).fetchone()
    db.close()
    assert row["model"] == MODEL_SONNET


def test_escalation_logs_history(conv_id, test_db, monkeypatch):
    """Escalation history is tracked in arc_state."""
    # Set up pending escalation
    state_backend.handle_set({
        "arc_id": 0,
        "key": "pending_escalation",
        "value": {
            "target_model": MODEL_SONNET,
            "reason": "First escalation",
            "task_type": "coding",
            "conversation_id": conv_id,
        },
    })

    # Mock _invoke_with_escalated_model
    def mock_invoke(user_msg, conv_id, target_model, reason, api_key):
        return {
            "conversation_id": conv_id,
            "response_text": "Response",
            "code": None,
            "message_id": 123,
        }
    monkeypatch.setattr(invocation, "_invoke_with_escalated_model", mock_invoke)

    # Approve first escalation
    invocation.invoke_for_chat("yes", conversation_id=conv_id)

    # Set up second escalation
    state_backend.handle_set({
        "arc_id": 0,
        "key": "pending_escalation",
        "value": {
            "target_model": MODEL_OPUS,
            "reason": "Second escalation",
            "task_type": "coding",
            "conversation_id": conv_id,
        },
    })

    # Approve second escalation
    invocation.invoke_for_chat("yes", conversation_id=conv_id)

    # Check history has both entries
    history = state_backend.handle_get({"arc_id": 0, "key": "escalation_history"})
    assert len(history["value"]) == 2
    assert history["value"][0]["reason"] == "First escalation"
    assert history["value"][1]["reason"] == "Second escalation"


def test_escalation_cross_provider(conv_id, test_db, monkeypatch):
    """Cross-provider escalation (Ollama -> Anthropic) works."""
    # Update config to start with Ollama
    test_config = {
        **config.CONFIG,
        "model_roles": {**config.CONFIG.get("model_roles", {}), "chat": MODEL_OLLAMA_CODER},
        "escalation": {
            "require_confirmation": True,
            "stacks": {
                "coding": [
                    MODEL_OLLAMA_CODER,
                    MODEL_HAIKU,
                ],
            },
            "pricing": {
                MODEL_OLLAMA_CODER: [0.00, 0.00],
                MODEL_HAIKU: [0.80, 4.00],
            },
        },
    }
    monkeypatch.setattr(config, "CONFIG", test_config)

    # Record API call with Ollama model
    from carpenter.db import get_db
    db = get_db()
    db.execute(
        "INSERT INTO api_calls (conversation_id, model, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?)",
        (conv_id, MODEL_OLLAMA_CODER, 100, 50),
    )
    db.commit()
    db.close()

    # Execute escalation tool
    result = invocation._execute_chat_tool(
        "escalate_current_arc",
        {"reason": "Need Claude", "task_type": "coding"},
        conversation_id=conv_id,
    )

    # Should propose Anthropic model
    assert MODEL_HAIKU in result
    assert "paid" in result.lower() or "cost" in result.lower()


# Patch the call attribute on the actual module, not a name binding in
# another module's namespace.  _invoke_with_escalated_model obtains its
# client via model_resolver.create_client_for_model(), which does a local
# import and returns the real ollama_client module object.  Patching
# "invocation.ollama_client" only replaces the name in invocation's
# namespace and would not intercept this code path.  See the docstring on
# create_client_for_model() for the full explanation.
@patch("carpenter.agent.providers.ollama.call")
def test_escalation_to_ollama_converts_history_to_openai_format(
    mock_call, conv_id, test_db, monkeypatch
):
    """Escalating from Haiku to an Ollama model sends history in OpenAI format.

    This is a regression test for the history format-mismatch bug: when
    _invoke_with_escalated_model targets an OpenAI-standard provider, the
    conversation history (loaded in canonical Anthropic format from the DB)
    must be converted to OpenAI format before the API call.
    """
    # Seed the conversation with a prior tool-use round-trip so the history
    # contains canonical tool_use / tool_result blocks.
    conversation.add_message(conv_id, "user", "What is the state of foo?")
    tool_use_blocks = [
        {"type": "tool_use", "id": "tu_prior", "name": "get_state", "input": {"key": "foo"}},
    ]
    conversation.add_message(
        conv_id, "assistant", "",
        content_json=json.dumps(tool_use_blocks),
    )
    tool_result_blocks = [
        {"type": "tool_result", "tool_use_id": "tu_prior", "content": "foo=42"},
    ]
    conversation.add_message(
        conv_id, "tool_result", "get_state: foo=42",
        content_json=json.dumps(tool_result_blocks),
    )
    conversation.add_message(conv_id, "assistant", "The state of foo is 42.")

    # Mock ollama_client.call to return a valid OpenAI-format response.
    mock_call.return_value = {
        "choices": [
            {"message": {"role": "assistant", "content": "Escalated response."}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 15},
    }

    # Escalate to an Ollama model.
    result = invocation._invoke_with_escalated_model(
        "Carry on with the analysis.",
        conv_id,
        MODEL_OLLAMA_SMALL,
        "Testing cross-provider escalation with tool history",
        api_key=None,
    )

    assert result["response_text"] == "Escalated response."
    assert mock_call.call_count == 1

    # Inspect the messages passed to ollama_client.call.
    # Signature: client.call(system, messages, **kwargs)
    _, messages_arg = mock_call.call_args[0]

    # The history assistant message must use tool_calls (OpenAI format),
    # not a content list with tool_use blocks (Anthropic format).
    tool_assistant_msgs = [
        m for m in messages_arg
        if m.get("role") == "assistant" and "tool_calls" in m
    ]
    assert tool_assistant_msgs, (
        "Expected an assistant message with 'tool_calls' in the escalated "
        f"call; got roles/keys: {[(m.get('role'), list(m.keys())) for m in messages_arg]}"
    )
    assert tool_assistant_msgs[0]["tool_calls"][0]["id"] == "tu_prior"

    # The tool result must appear as role:'tool', not as a user message with
    # tool_result content blocks.
    tool_messages = [m for m in messages_arg if m.get("role") == "tool"]
    assert tool_messages, (
        "Expected a role:'tool' message in the escalated call; "
        f"got roles: {[m.get('role') for m in messages_arg]}"
    )
    assert tool_messages[0]["tool_call_id"] == "tu_prior"

    # No Anthropic-format blocks should appear anywhere.
    for msg in messages_arg:
        content = msg.get("content")
        if isinstance(content, list):
            types = {b.get("type") for b in content}
            assert "tool_use" not in types, f"Anthropic tool_use block found in: {msg}"
            assert "tool_result" not in types, f"Anthropic tool_result block found in: {msg}"

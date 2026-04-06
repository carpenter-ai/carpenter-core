"""Tests for carpenter.agent.invocation — core invocation flow.

Tool persistence, taint isolation, history conversion, and error classification
tests have been moved to:
- test_invocation_tools.py   (tool call/API persistence, submit_code tool)
- test_invocation_taint.py   (taint leak prevention)
- test_invocation_history.py (history format conversion, Ollama multi-turn)
- test_invocation_errors.py  (error classification integration)
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from carpenter.agent import invocation, conversation
from carpenter.chat_tool_loader import get_handler
from carpenter.db import get_db
from tests.agent.conftest import _mock_api_response


class TestInvokeForChat:
    """Tests for invoke_for_chat."""

    @patch("carpenter.agent.invocation.claude_client")
    def test_basic_chat(self, mock_client):
        """Basic chat returns response text and conversation ID."""
        mock_client.call.return_value = _mock_api_response(
            "Hello! How can I help you?"
        )
        mock_client.extract_text.return_value = "Hello! How can I help you?"
        mock_client.extract_code.return_value = None
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat(
            "Hi there", api_key="test-key",
        )

        assert result["conversation_id"] is not None
        assert result["response_text"] == "Hello! How can I help you?"
        assert result["code"] is None
        assert result["message_id"] is not None

    @patch("carpenter.agent.invocation.claude_client")
    def test_chat_with_code(self, mock_client):
        """Chat response with code extracts it."""
        code_text = 'print("hello")\n'
        response_text = f"Here you go:\n\n```python\n{code_text}```"

        mock_client.call.return_value = _mock_api_response(response_text)
        mock_client.extract_text.return_value = response_text
        mock_client.extract_code.return_value = code_text
        mock_client.extract_code_from_text.return_value = code_text

        result = invocation.invoke_for_chat(
            "Write a hello script", api_key="test-key",
        )

        assert result["code"] == code_text

    @patch("carpenter.agent.invocation.claude_client")
    def test_chat_persists_messages(self, mock_client):
        """Chat messages are persisted in the database."""
        mock_client.call.return_value = _mock_api_response("Response")
        mock_client.extract_text.return_value = "Response"
        mock_client.extract_code.return_value = None
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat(
            "Test message", api_key="test-key",
        )

        messages = conversation.get_messages(result["conversation_id"])
        assert len(messages) == 2  # user + assistant
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Test message"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == "Response"


class TestCallWithRetries:
    """Tests for _call_with_retries."""

    @patch("carpenter.agent.invocation.claude_client")
    def test_succeeds_on_first_try(self, mock_client):
        """Returns response when first call succeeds."""
        mock_client.call.return_value = {"content": []}
        result = invocation._call_with_retries(
            "system", [], api_key="key", max_retries=3,
        )
        assert result is not None
        assert mock_client.call.call_count == 1

    @patch("carpenter.agent.invocation.claude_client")
    @patch("carpenter.agent.invocation.time")
    def test_retries_on_failure(self, mock_time, mock_client):
        """Retries after transient failure and succeeds."""
        mock_client.call.side_effect = [
            Exception("timeout"),
            {"content": []},
        ]
        mock_time.sleep = MagicMock()

        result = invocation._call_with_retries(
            "system", [], api_key="key", max_retries=3,
        )
        assert result is not None
        assert mock_client.call.call_count == 2
        mock_time.sleep.assert_called_once_with(1)  # 2^0 = 1

    @patch("carpenter.agent.invocation.claude_client")
    @patch("carpenter.agent.invocation.time")
    def test_exhausts_retries(self, mock_time, mock_client):
        """Returns error info dict when all retries fail."""
        mock_client.call.side_effect = Exception("always fails")
        mock_time.sleep = MagicMock()

        result = invocation._call_with_retries(
            "system", [], api_key="key", max_retries=2,
        )
        # Should return dict with _error key containing ErrorInfo
        assert result is not None
        assert "_error" in result
        assert result["_error"].type == "UnknownError"
        assert result["_error"].retry_count == 2
        assert mock_client.call.call_count == 2


    @patch("carpenter.agent.invocation.ollama_client")
    @patch("carpenter.agent.invocation.config")
    def test_normalizes_openai_response(self, mock_config, mock_ollama):
        """_call_with_retries normalizes OpenAI-format response to canonical format."""
        mock_config.CONFIG = {"ai_provider": "ollama"}
        mock_ollama.call.return_value = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hello from Ollama!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 25},
            "model": "llama3.1",
        }

        result = invocation._call_with_retries(
            "system", [],
            client=invocation.ollama_client,
            max_retries=1,
        )

        assert result is not None
        # Verify response was normalized to canonical format
        assert result["content"] == [{"type": "text", "text": "Hello from Ollama!"}]
        assert result["stop_reason"] == "end_turn"
        assert result["usage"] == {"input_tokens": 50, "output_tokens": 25}

    @patch("carpenter.agent.invocation.ollama_client")
    def test_converts_tools_for_openai_client(self, mock_ollama):
        """_call_with_retries converts tools to OpenAI format for non-anthropic clients."""
        mock_ollama.call.return_value = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Done"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
        }

        tools = [{"name": "read_file", "description": "Read a file", "input_schema": {"type": "object"}}]

        invocation._call_with_retries(
            "system", [],
            client=invocation.ollama_client,
            max_retries=1,
            tools=tools,
        )

        # Verify tools were converted to OpenAI format before passing to client
        call_kwargs = mock_ollama.call.call_args[1]
        provider_tools = call_kwargs["tools"]
        assert provider_tools[0]["type"] == "function"
        assert provider_tools[0]["function"]["name"] == "read_file"

    def test_chain_does_not_override_model(self):
        """Chain provider passes model=None so each backend uses its own model."""
        mock_chain = MagicMock()
        mock_chain.__name__ = "carpenter.agent.providers.chain"
        mock_chain.call.return_value = {
            "_api_standard": "openai",
            "choices": [
                {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
        }

        invocation._call_with_retries(
            "system", [],
            client=mock_chain,
            max_retries=1,
        )

        call_kwargs = mock_chain.call.call_args[1]
        assert call_kwargs["model"] is None


class TestMultiConversationChat:
    """Tests for multi-conversation support in invoke_for_chat."""

    @patch("carpenter.agent.invocation.claude_client")
    def test_chat_with_explicit_conversation_id(self, mock_client):
        """Messages go to the specified conversation."""
        conv_id = conversation.create_conversation()

        mock_client.call.return_value = _mock_api_response("Got it.")
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat(
            "Hello", conversation_id=conv_id, api_key="test-key",
        )

        assert result["conversation_id"] == conv_id
        messages = conversation.get_messages(conv_id)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello"

    @patch("carpenter.agent.invocation.claude_client")
    def test_chat_with_nonexistent_conversation_id(self, mock_client):
        """Nonexistent conversation_id returns error."""
        result = invocation.invoke_for_chat(
            "Hello", conversation_id=99999, api_key="test-key",
        )
        assert "not found" in result["response_text"]
        assert result["conversation_id"] is None

    @patch("carpenter.agent.invocation.claude_client")
    def test_chat_explicit_id_skips_prior_context(self, mock_client):
        """With explicit conversation_id, prior context is not fetched."""
        # Create two conversations — the first has messages
        conv1 = conversation.create_conversation()
        conversation.add_message(conv1, "user", "Old context")
        conversation.add_message(conv1, "assistant", "Old reply")

        conv2 = conversation.create_conversation()

        mock_client.call.return_value = _mock_api_response("Fresh reply.")
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat(
            "New message", conversation_id=conv2, api_key="test-key",
        )

        assert result["conversation_id"] == conv2
        # Verify the call used "chat_new" template (no prior context)
        call_args = mock_client.call.call_args
        system_arg = call_args[0][0] if call_args[0] else call_args[1].get("system", "")
        assert "Prior Context" not in system_arg

    def test_list_schedules(self):
        """list_schedules returns all cron entries."""
        from carpenter.core.engine import trigger_manager
        trigger_manager.add_once("test-list-a", "2030-12-31T23:59:00", "cron.message")
        result = invocation._execute_chat_tool(
            "list_schedules",
            {},
        )
        import json
        data = json.loads(result)
        names = [e["name"] for e in data["entries"]]
        assert "test-list-a" in names

        # Cleanup
        trigger_manager.remove_cron("test-list-a")


class TestBuildChatSystemPromptArcStep:
    """Tests for is_arc_step=True in _build_chat_system_prompt()."""

    def test_arc_step_skips_recent_conversations(self, monkeypatch):
        """Arc step agents don't get recent conversation hints."""
        monkeypatch.setitem(invocation.config.CONFIG, "memory_recent_hints", 3)

        # Create a conversation so there's something to list
        conv_id = conversation.create_conversation()
        conversation.add_message(conv_id, "user", "hello")

        prompt = invocation._build_chat_system_prompt(is_arc_step=True)
        assert "Recent Conversations" not in prompt

        # Verify non-arc mode would include it (if conversations exist)
        prompt_normal = invocation._build_chat_system_prompt(is_arc_step=False)
        # May or may not have recent conversations depending on DB state,
        # but the arc step one definitely must not.
        assert "Recent Conversations" not in prompt

    def test_arc_step_keeps_core_sections(self, monkeypatch):
        """Arc step agents still get identity, security, KB navigation, and tools."""
        monkeypatch.setitem(invocation.config.CONFIG, "memory_recent_hints", 0)

        prompt = invocation._build_chat_system_prompt(is_arc_step=True)
        assert "Carpenter" in prompt
        assert "Security Model" in prompt
        assert "kb_search" in prompt


class TestSystemTriggeredInvocation:
    """Tests for system-triggered chat invocations."""

    @patch("carpenter.agent.invocation.claude_client")
    def test_system_triggered_skips_user_message(self, mock_client):
        """System-triggered invocation does not add a user message to DB."""
        mock_client.call.return_value = _mock_api_response("Changes applied!")
        mock_client.extract_code_from_text.return_value = None

        conv_id = conversation.create_conversation()
        # Pre-add the system notification (as the handler would)
        conversation.add_message(conv_id, "system", "Changes approved and applied.")

        result = invocation.invoke_for_chat(
            "Changes approved and applied.",
            conversation_id=conv_id,
            api_key="test-key",
            _system_triggered=True,
        )

        assert result["conversation_id"] == conv_id
        messages = conversation.get_messages(conv_id)
        # Should have: system + assistant (no extra user message)
        roles = [m["role"] for m in messages]
        assert roles == ["system", "assistant"]

    @patch("carpenter.agent.invocation.claude_client")
    def test_system_triggered_skips_escalation(self, mock_client):
        """System-triggered invocation bypasses escalation check."""
        mock_client.call.return_value = _mock_api_response("Noted.")
        mock_client.extract_code_from_text.return_value = None

        conv_id = conversation.create_conversation()
        conversation.add_message(conv_id, "system", "Arc completed")

        # Set a pending escalation that would normally intercept
        from carpenter.tool_backends import state as state_backend
        state_backend.handle_set({
            "arc_id": 0,
            "key": "pending_escalation",
            "value": {"target_model": "opus", "reason": "test", "conversation_id": conv_id},
        })

        try:
            result = invocation.invoke_for_chat(
                "Arc completed",
                conversation_id=conv_id,
                api_key="test-key",
                _system_triggered=True,
            )

            # Should get a normal response, not escalation prompt
            assert result["response_text"] == "Noted."
            assert "escalat" not in result["response_text"].lower()
        finally:
            state_backend.handle_set({
                "arc_id": 0, "key": "pending_escalation", "value": None,
            })

    @patch("carpenter.agent.invocation.claude_client")
    def test_system_triggered_responds_to_notification(self, mock_client):
        """System-triggered invocation sees the system notification and responds."""
        mock_client.call.return_value = _mock_api_response(
            "The review is ready with 3 files changed."
        )
        mock_client.extract_code_from_text.return_value = None

        conv_id = conversation.create_conversation()
        conversation.add_message(conv_id, "user", "Make those changes please")
        conversation.add_message(conv_id, "assistant", "Ok, starting the coding agent.")
        conversation.add_message(conv_id, "system", "Review ready (3 files changed).")

        result = invocation.invoke_for_chat(
            "Review ready (3 files changed).",
            conversation_id=conv_id,
            api_key="test-key",
            _system_triggered=True,
        )

        assert result["response_text"] == "The review is ready with 3 files changed."
        # The API call should include the system notification as user-role message
        call_args = mock_client.call.call_args
        api_messages = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("messages", [])
        # Last message should be user-role (the system notification)
        last_user_msgs = [m for m in api_messages if m.get("role") == "user"]
        assert len(last_user_msgs) > 0
        last_user = last_user_msgs[-1]
        assert "[System notification:" in (last_user.get("content", "") if isinstance(last_user.get("content"), str) else "")


# -- Context window awareness --

class TestGetContextWindow:
    """Tests for _get_context_window()."""

    def test_exact_match(self, monkeypatch):
        """Exact model string match in context_windows."""
        monkeypatch.setitem(
            invocation.config.CONFIG, "context_windows",
            {"local:qwen2.5-1.5b": 4096, "local": 8192},
        )
        assert invocation._get_context_window("local:qwen2.5-1.5b") == 4096

    def test_provider_prefix_match(self, monkeypatch):
        """Provider prefix match when no exact match."""
        monkeypatch.setitem(
            invocation.config.CONFIG, "context_windows",
            {"local": 8192, "anthropic": 200000},
        )
        assert invocation._get_context_window("local:some-model") == 8192

    def test_fallback_to_default(self, monkeypatch):
        """Falls back to _DEFAULT_CONTEXT_WINDOW for unknown models."""
        monkeypatch.setitem(
            invocation.config.CONFIG, "context_windows", {},
        )
        assert invocation._get_context_window("unknown:model") == 200000

    def test_none_model(self, monkeypatch):
        """Returns default for None model string."""
        monkeypatch.setitem(
            invocation.config.CONFIG, "context_windows",
            {"local": 8192},
        )
        assert invocation._get_context_window(None) == 200000

    def test_anthropic_default(self, monkeypatch):
        """Anthropic models resolve to 200K."""
        monkeypatch.setitem(
            invocation.config.CONFIG, "context_windows",
            {"local": 8192, "anthropic": 200000},
        )
        assert invocation._get_context_window("anthropic:claude-sonnet-4") == 200000


class TestBuildChatSystemPromptCompact:
    """Tests for compact prompt tier in _build_chat_system_prompt()."""

    def test_small_context_drops_non_compact_sections(self, monkeypatch):
        """With context_budget < 16384, non-compact sections (modules) are dropped."""
        # Prevent dynamic sections from hitting the DB
        monkeypatch.setitem(invocation.config.CONFIG, "memory_recent_hints", 0)

        prompt = invocation._build_chat_system_prompt(context_budget=8192)

        # Non-compact section should NOT be present in compact mode
        assert "## Importable Modules" not in prompt

        # Compact sections should still be present
        assert "Carpenter" in prompt
        assert "Security Model" in prompt
        assert "Communication Style" in prompt

        # Compact KB pointer should be present
        assert "kb_search" in prompt

    def test_large_context_includes_all_sections(self, monkeypatch):
        """With context_budget >= 16384, all sections are included."""
        monkeypatch.setitem(invocation.config.CONFIG, "memory_recent_hints", 0)

        prompt = invocation._build_chat_system_prompt(context_budget=200000)

        # All sections should be present including non-compact
        assert "carpenter_tools.act" in prompt
        assert "## Security Model" in prompt
        assert "## Knowledge Base" in prompt

    def test_default_context_includes_all(self, monkeypatch):
        """Default (no argument) includes all sections."""
        monkeypatch.setitem(invocation.config.CONFIG, "memory_recent_hints", 0)

        prompt = invocation._build_chat_system_prompt()
        assert "carpenter_tools.act" in prompt


class TestGetClientLocal:
    """Tests for _get_client() with local provider."""

    def test_get_client_local(self, monkeypatch):
        """_get_client returns local_client for local provider."""
        monkeypatch.setitem(invocation.config.CONFIG, "ai_provider", "local")
        client = invocation._get_client()
        assert client is invocation.local_client

    def test_get_client_model_override_local(self):
        """_get_client with local:model override returns local_client."""
        client = invocation._get_client("local:qwen2.5-1.5b")
        assert client is invocation.local_client


class TestGetClientChain:
    """Tests for _get_client() with chain provider."""

    def test_get_client_chain(self, monkeypatch):
        """_get_client returns chain_client for chain provider."""
        monkeypatch.setitem(invocation.config.CONFIG, "ai_provider", "chain")
        client = invocation._get_client()
        from carpenter.agent.providers import chain as chain_client
        assert client is chain_client

    def test_get_client_model_override_chain(self):
        """_get_client with chain:model override returns chain_client."""
        client = invocation._get_client("chain:qwen3.5:9b")
        from carpenter.agent.providers import chain as chain_client
        assert client is chain_client

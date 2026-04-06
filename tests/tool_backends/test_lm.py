"""Tests for carpenter.tool_backends.lm."""

from unittest.mock import patch, MagicMock

import pytest

from carpenter.tool_backends import lm as lm_backend
from carpenter import config


class TestHandleCall:
    def test_missing_prompt(self, test_db):
        result = lm_backend.handle_call({})
        assert "error" in result
        assert "prompt is required" in result["error"]

    def test_basic_call(self, test_db, monkeypatch):
        """Basic lm.call with explicit model resolves and calls client."""
        current = config.CONFIG.copy()
        current["model_roles"] = {
            **current.get("model_roles", {}),
            "default_step": "anthropic:claude-sonnet-4-20250514",
        }
        monkeypatch.setattr(config, "CONFIG", current)

        mock_client = MagicMock()
        mock_client.call.return_value = {
            "content": [{"type": "text", "text": "Hello world"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        mock_client.extract_text.return_value = "Hello world"

        with patch(
            "carpenter.tool_backends.lm.create_client_for_model",
            return_value=mock_client,
        ):
            result = lm_backend.handle_call({
                "prompt": "Say hello",
                "model": "anthropic:claude-sonnet-4-20250514",
            })

        assert result["content"] == "Hello world"
        assert result["model"] == "anthropic:claude-sonnet-4-20250514"
        assert result["role"] == "assistant"
        assert result["usage"]["input_tokens"] == 10

    def test_model_role_resolution(self, test_db, monkeypatch):
        """model_role parameter resolves via get_model_for_role."""
        current = config.CONFIG.copy()
        current["model_roles"] = {
            **current.get("model_roles", {}),
            "default_step": "anthropic:claude-sonnet-4-20250514",
        }
        monkeypatch.setattr(config, "CONFIG", current)

        mock_client = MagicMock()
        mock_client.call.return_value = {
            "content": [{"type": "text", "text": "OK"}],
            "usage": {},
        }
        mock_client.extract_text.return_value = "OK"

        with patch(
            "carpenter.tool_backends.lm.create_client_for_model",
            return_value=mock_client,
        ):
            result = lm_backend.handle_call({
                "prompt": "Test",
                "model_role": "default_step",
            })

        assert result["content"] == "OK"
        assert result["model"] == "anthropic:claude-sonnet-4-20250514"

    def test_rejects_unknown_model(self, test_db, monkeypatch):
        """Models not in model_roles are rejected."""
        current = config.CONFIG.copy()
        current["model_roles"] = {
            "default": "anthropic:claude-sonnet-4-20250514",
            "default_step": "anthropic:claude-sonnet-4-20250514",
            "chat": "anthropic:claude-sonnet-4-20250514",
        }
        monkeypatch.setattr(config, "CONFIG", current)

        result = lm_backend.handle_call({
            "prompt": "Test",
            "model": "anthropic:evil-model",
        })

        assert "error" in result
        assert "not in the allowed" in result["error"]

    def test_agent_role_system_prompt(self, test_db, monkeypatch):
        """agent_role resolves system prompt from agent_roles config."""
        current = config.CONFIG.copy()
        current["model_roles"] = {
            **current.get("model_roles", {}),
            "default_step": "anthropic:claude-sonnet-4-20250514",
        }
        current["agent_roles"] = {
            "test-role": {
                "system_prompt": "You are a test role.",
            },
        }
        monkeypatch.setattr(config, "CONFIG", current)

        mock_client = MagicMock()
        mock_client.call.return_value = {
            "content": [{"type": "text", "text": "OK"}],
            "usage": {},
        }
        mock_client.extract_text.return_value = "OK"

        with patch(
            "carpenter.tool_backends.lm.create_client_for_model",
            return_value=mock_client,
        ):
            result = lm_backend.handle_call({
                "prompt": "Test",
                "agent_role": "test-role",
            })

        # Verify the system prompt was passed
        call_args = mock_client.call.call_args
        assert call_args[0][0] == "You are a test role."

    def test_error_handling(self, test_db, monkeypatch):
        """API errors return error dict instead of raising."""
        current = config.CONFIG.copy()
        current["model_roles"] = {
            **current.get("model_roles", {}),
            "default_step": "anthropic:claude-sonnet-4-20250514",
        }
        monkeypatch.setattr(config, "CONFIG", current)

        mock_client = MagicMock()
        mock_client.call.side_effect = Exception("API failed")

        with patch(
            "carpenter.tool_backends.lm.create_client_for_model",
            return_value=mock_client,
        ):
            result = lm_backend.handle_call({
                "prompt": "Test",
                "model": "anthropic:claude-sonnet-4-20250514",
            })

        assert "error" in result
        assert "API failed" in result["error"]

"""Tests for messaging delivery."""

from unittest.mock import patch

from carpenter.agent import conversation
from carpenter.db import get_db
from carpenter.tool_backends import messaging


# --- handle_send tests ---


class TestHandleSend:
    """messaging.handle_send should deliver to conversations."""

    def _create_conversation(self):
        db = get_db()
        try:
            cursor = db.execute(
                "INSERT INTO conversations (started_at, last_message_at) "
                "VALUES (datetime('now'), datetime('now'))"
            )
            conv_id = cursor.lastrowid
            db.commit()
        finally:
            db.close()
        return conv_id

    def test_with_conversation_id_delivers(self):
        """handle_send with conversation_id inserts message and returns delivered=True."""
        conv_id = self._create_conversation()
        result = messaging.handle_send({
            "message": "hello from executor",
            "conversation_id": conv_id,
        })
        assert result["success"] is True
        assert result["delivered"] is True

        # Verify message was actually inserted
        db = get_db()
        try:
            row = db.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
                (conv_id,),
            ).fetchone()
        finally:
            db.close()
        assert row is not None
        assert row["role"] == "assistant"
        assert "hello from executor" in row["content"]

    def test_without_conversation_id_not_delivered(self):
        """handle_send without conversation_id returns delivered=False."""
        result = messaging.handle_send({"message": "no context"})
        assert result["success"] is True
        assert result["delivered"] is False

    def test_empty_message(self):
        """handle_send with empty message still delivers if conv_id present."""
        conv_id = self._create_conversation()
        result = messaging.handle_send({
            "message": "",
            "conversation_id": conv_id,
        })
        assert result["delivered"] is True

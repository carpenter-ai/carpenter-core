"""Tests for conversation tool backend."""
import pytest

from carpenter.agent import conversation
from carpenter.tool_backends.conversation import (
    handle_rename,
    handle_archive,
    handle_archive_batch,
    handle_archive_all,
)


class TestConversationBackend:

    def test_rename(self):
        conv_id = conversation.create_conversation()
        result = handle_rename({"conversation_id": conv_id, "title": "New Title"})
        assert result["conversation_id"] == conv_id
        assert result["title"] == "New Title"
        conv = conversation.get_conversation(conv_id)
        assert conv["title"] == "New Title"

    def test_rename_missing_conversation_id(self):
        result = handle_rename({"title": "New Title"})
        assert "error" in result

    def test_rename_missing_title(self):
        conv_id = conversation.create_conversation()
        result = handle_rename({"conversation_id": conv_id})
        assert "error" in result

    def test_archive(self):
        conv_id = conversation.create_conversation()
        result = handle_archive({"conversation_id": conv_id})
        assert result["archived"] is True
        conv = conversation.get_conversation(conv_id)
        assert conv["archived"] == 1

    def test_archive_missing_conversation_id(self):
        result = handle_archive({})
        assert "error" in result

    def test_archive_batch(self):
        ids = [conversation.create_conversation() for _ in range(3)]
        result = handle_archive_batch({"conversation_ids": ids})
        assert result["archived_count"] == 3
        assert result["conversation_ids"] == ids
        for cid in ids:
            assert conversation.get_conversation(cid)["archived"] == 1

    def test_archive_batch_empty(self):
        result = handle_archive_batch({"conversation_ids": []})
        assert "error" in result

    def test_archive_batch_missing_param(self):
        result = handle_archive_batch({})
        assert "error" in result

    def test_archive_batch_idempotent(self):
        cid = conversation.create_conversation()
        conversation.archive_conversation(cid)
        result = handle_archive_batch({"conversation_ids": [cid]})
        assert result["archived_count"] == 0

    def test_archive_all(self):
        ids = [conversation.create_conversation() for _ in range(3)]
        result = handle_archive_all({})
        assert result["archived_count"] >= 3
        for cid in ids:
            assert conversation.get_conversation(cid)["archived"] == 1

    def test_archive_all_with_exclude(self):
        ids = [conversation.create_conversation() for _ in range(3)]
        result = handle_archive_all({"exclude_ids": [ids[1]]})
        assert conversation.get_conversation(ids[0])["archived"] == 1
        assert conversation.get_conversation(ids[1])["archived"] == 0
        assert conversation.get_conversation(ids[2])["archived"] == 1

    def test_archive_all_invalid_exclude(self):
        result = handle_archive_all({"exclude_ids": "not-a-list"})
        assert "error" in result

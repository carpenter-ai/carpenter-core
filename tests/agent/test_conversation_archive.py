"""Tests for conversation archiving and unarchiving."""

import pytest
from carpenter.agent import conversation


def test_archive_conversation(test_db):
    """Archive a conversation."""
    conv_id = conversation.create_conversation()
    conversation.add_message(conv_id, "user", "Test message")

    # Initially not archived
    conv = conversation.get_conversation(conv_id)
    assert conv["archived"] == 0

    # Archive it
    conversation.archive_conversation(conv_id)
    conv = conversation.get_conversation(conv_id)
    assert conv["archived"] == 1

    # Should not appear in default listing
    active = conversation.list_conversations_with_preview(include_archived=False)
    assert not any(c["id"] == conv_id for c in active)

    # Should appear in archived listing
    all_convs = conversation.list_conversations_with_preview(include_archived=True)
    archived_conv = next((c for c in all_convs if c["id"] == conv_id), None)
    assert archived_conv is not None
    assert archived_conv["archived"] == 1


def test_unarchive_conversation(test_db):
    """Unarchive a conversation."""
    conv_id = conversation.create_conversation()
    conversation.add_message(conv_id, "user", "Test message")

    # Archive then unarchive
    conversation.archive_conversation(conv_id)
    conversation.unarchive_conversation(conv_id)

    # Should be unarchived
    conv = conversation.get_conversation(conv_id)
    assert conv["archived"] == 0

    # Should appear in default listing
    active = conversation.list_conversations_with_preview(include_archived=False)
    assert any(c["id"] == conv_id for c in active)


def test_archive_conversations_batch(test_db):
    """Batch archive multiple conversations."""
    ids = [conversation.create_conversation() for _ in range(4)]
    # Archive first 3
    count = conversation.archive_conversations_batch(ids[:3])
    assert count == 3
    for cid in ids[:3]:
        assert conversation.get_conversation(cid)["archived"] == 1
    # Fourth should remain unarchived
    assert conversation.get_conversation(ids[3])["archived"] == 0


def test_archive_conversations_batch_empty(test_db):
    """Batch archive with empty list returns 0."""
    assert conversation.archive_conversations_batch([]) == 0


def test_archive_all_conversations(test_db):
    """Archive all conversations."""
    ids = [conversation.create_conversation() for _ in range(3)]
    count = conversation.archive_all_conversations()
    assert count >= 3
    for cid in ids:
        assert conversation.get_conversation(cid)["archived"] == 1


def test_archive_all_conversations_with_exclude(test_db):
    """Archive all except excluded IDs."""
    ids = [conversation.create_conversation() for _ in range(4)]
    keep = [ids[1], ids[3]]
    count = conversation.archive_all_conversations(exclude_ids=keep)
    assert count >= 2
    assert conversation.get_conversation(ids[0])["archived"] == 1
    assert conversation.get_conversation(ids[1])["archived"] == 0
    assert conversation.get_conversation(ids[2])["archived"] == 1
    assert conversation.get_conversation(ids[3])["archived"] == 0

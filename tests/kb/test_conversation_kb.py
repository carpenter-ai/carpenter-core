"""Tests for conversation → KB entry creation."""

from carpenter.db import get_db
from carpenter.kb import get_store
from carpenter.kb.conversation_kb import (
    _sanitize_title,
    backfill_conversations,
    create_conversation_entry,
)


def _create_conversation(title, summary=None):
    """Insert a test conversation and return its ID."""
    db = get_db()
    try:
        cursor = db.execute(
            "INSERT INTO conversations (title, summary) VALUES (?, ?)",
            (title, summary),
        )
        db.commit()
        return cursor.lastrowid
    finally:
        db.close()


class TestCreateConversationEntry:
    def test_creates_entry(self):
        conv_id = _create_conversation("Setting up webhooks", "Configured Forgejo webhooks for PR review.")
        store = get_store()
        path = create_conversation_entry(conv_id, store)
        assert path is not None
        assert path.startswith("conversations/")
        assert str(conv_id) in path

        entry = store.get_entry(path)
        assert entry is not None
        assert "webhooks" in entry["content"].lower()
        assert entry["entry_type"] == "conversation_summary"

    def test_returns_none_without_summary(self):
        conv_id = _create_conversation("No summary here", None)
        store = get_store()
        path = create_conversation_entry(conv_id, store)
        assert path is None

    def test_sanitizes_title(self):
        assert _sanitize_title("Hello, World! 123") == "hello-world-123"
        assert _sanitize_title("Special $#@! chars") == "special-chars"
        assert _sanitize_title("") == "untitled"
        assert len(_sanitize_title("a" * 100)) <= 50

    def test_backfills_existing(self):
        _create_conversation("Conv A", "Summary A about alpha.")
        _create_conversation("Conv B", "Summary B about beta.")
        _create_conversation("Conv C", None)  # no summary

        store = get_store()
        count = backfill_conversations(store)
        assert count == 2

    def test_high_water_mark_skips_already_backfilled(self):
        _create_conversation("Conv D", "Summary D about delta.")
        _create_conversation("Conv E", "Summary E about epsilon.")

        store = get_store()
        count1 = backfill_conversations(store)
        assert count1 == 2

        # Second backfill should find nothing new
        count2 = backfill_conversations(store)
        assert count2 == 0

        # Add a new conversation — only it should be backfilled
        _create_conversation("Conv F", "Summary F about phi.")
        count3 = backfill_conversations(store)
        assert count3 == 1

    def test_nonexistent_conversation_id(self):
        """create_conversation_entry returns None for an ID that doesn't exist in DB."""
        store = get_store()
        result = create_conversation_entry(999999, store)
        assert result is None

    def test_empty_string_summary_treated_as_no_summary(self):
        """An empty-string summary (not NULL) is treated the same as no summary."""
        conv_id = _create_conversation("Empty summary conv", "")
        store = get_store()
        path = create_conversation_entry(conv_id, store)
        assert path is None

    def test_unicode_title_sanitized(self):
        """Unicode characters in the title are stripped to hyphens/removed."""
        conv_id = _create_conversation(
            "Configurer les webhooks \u00e9v\u00e9nements \u2014 d\u00e9ploiement",
            "Set up event webhooks for deployment pipeline.",
        )
        store = get_store()
        path = create_conversation_entry(conv_id, store)
        assert path is not None
        # The KB path segment should only have [a-z0-9-]
        segment = path.split("/", 1)[1]
        # id-sanitized_title
        sanitized_part = segment.split("-", 1)[1]
        import re
        assert re.fullmatch(r"[a-z0-9-]+", sanitized_part), f"Unexpected chars in: {sanitized_part}"

    def test_long_title_truncated_in_kb_path(self):
        """A very long title is truncated to <=50 chars in the sanitized KB path segment."""
        long_title = "A" * 300
        conv_id = _create_conversation(long_title, "Summary for long-titled conversation.")
        store = get_store()
        path = create_conversation_entry(conv_id, store)
        assert path is not None
        # The sanitized portion (after "conversations/{id}-") should be <= 50 chars
        segment = path.split("/", 1)[1]
        parts = segment.split("-", 1)
        sanitized_part = parts[1] if len(parts) > 1 else ""
        assert len(sanitized_part) <= 50

    def test_content_includes_summary_and_title(self):
        """The KB entry content includes both the title and the summary text."""
        conv_id = _create_conversation(
            "Multi-sentence test",
            "First sentence about testing. Second sentence with more details.",
        )
        store = get_store()
        path = create_conversation_entry(conv_id, store)
        assert path is not None
        entry = store.get_entry(path)
        assert entry is not None
        # Content should include the title as H1 and the full summary
        assert "# Multi-sentence test" in entry["content"]
        assert "First sentence about testing." in entry["content"]
        assert "Second sentence with more details." in entry["content"]

    def test_sanitize_title_strips_leading_trailing_hyphens(self):
        """_sanitize_title strips leading and trailing hyphens from the result."""
        assert _sanitize_title("---hello---") == "hello"
        assert _sanitize_title("  !!!test!!!  ") == "test"

    def test_sanitize_title_collapses_consecutive_special_chars(self):
        """Multiple consecutive special chars become a single hyphen."""
        assert _sanitize_title("foo   @#$   bar") == "foo-bar"

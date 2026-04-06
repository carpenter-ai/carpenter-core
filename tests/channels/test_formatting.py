"""Tests for channel message formatting."""

import pytest

from carpenter.channels.formatting import (
    format_for_channel,
    split_message,
    _format_telegram,
    _strip_markdown,
    _format_whatsapp,
)


class TestFormatForChannel:
    def test_web_passthrough(self):
        text = "**bold** and `code`"
        assert format_for_channel(text, "web") == text

    def test_unknown_channel_passthrough(self):
        text = "Hello world"
        assert format_for_channel(text, "unknown") == text

    def test_signal_strips_markdown(self):
        text = "**bold** and *italic*"
        result = format_for_channel(text, "signal")
        assert "**" not in result
        assert "*" not in result
        assert "bold" in result

    def test_telegram_escapes(self):
        text = "Hello."
        result = format_for_channel(text, "telegram")
        assert "\\." in result


class TestTelegramFormatting:
    def test_escapes_dots(self):
        assert _format_telegram("Hello.") == "Hello\\."

    def test_escapes_parens(self):
        assert _format_telegram("a(b)") == "a\\(b\\)"

    def test_preserves_code_blocks(self):
        text = "```\ncode.here\n```"
        result = _format_telegram(text)
        assert "code.here" in result  # content preserved inside code block

    def test_preserves_inline_code(self):
        text = "run `test.py` now"
        result = _format_telegram(text)
        assert "test.py" in result  # not escaped inside backticks


class TestStripMarkdown:
    def test_strips_bold(self):
        assert _strip_markdown("**bold**") == "bold"

    def test_strips_italic(self):
        assert _strip_markdown("*italic*") == "italic"

    def test_strips_underline_italic(self):
        assert _strip_markdown("_text_") == "text"

    def test_strips_inline_code(self):
        assert _strip_markdown("`code`") == "code"

    def test_strips_headers(self):
        assert _strip_markdown("## Header").strip() == "Header"

    def test_strips_links(self):
        assert _strip_markdown("[Click](https://example.com)") == "Click"

    def test_strips_code_blocks(self):
        text = "```python\nprint('hi')\n```"
        result = _strip_markdown(text)
        assert "```" not in result
        assert "print('hi')" in result


class TestSplitMessage:
    def test_short_message_no_split(self):
        text = "Hello world"
        assert split_message(text, 100) == ["Hello world"]

    def test_exact_length_no_split(self):
        text = "A" * 100
        assert split_message(text, 100) == [text]

    def test_splits_at_paragraph_boundary(self):
        text = "First paragraph.\n\nSecond paragraph."
        chunks = split_message(text, 25)
        assert len(chunks) == 2
        assert chunks[0] == "First paragraph."
        assert chunks[1] == "Second paragraph."

    def test_splits_at_newline(self):
        text = "Line one.\nLine two."
        chunks = split_message(text, 15)
        assert len(chunks) == 2
        assert chunks[0] == "Line one."
        assert chunks[1] == "Line two."

    def test_splits_at_sentence(self):
        text = "First sentence. Second sentence."
        chunks = split_message(text, 20)
        assert len(chunks) == 2
        assert chunks[0] == "First sentence."
        assert chunks[1] == "Second sentence."

    def test_splits_at_space(self):
        text = "word1 word2 word3"
        chunks = split_message(text, 10)
        assert all(len(c) <= 10 for c in chunks)

    def test_hard_cut_no_boundaries(self):
        text = "A" * 200
        chunks = split_message(text, 100)
        assert len(chunks) == 2
        assert chunks[0] == "A" * 100
        assert chunks[1] == "A" * 100

    def test_empty_string(self):
        assert split_message("", 100) == [""]

    def test_multiple_paragraphs(self):
        text = "P1.\n\nP2.\n\nP3.\n\nP4."
        chunks = split_message(text, 10)
        assert all(len(c) <= 10 for c in chunks)
        full = "\n\n".join(chunks)
        # All content should be present
        assert "P1." in full
        assert "P4." in full


class TestWhatsAppFormatting:
    def test_converts_bold(self):
        assert _format_whatsapp("**bold**") == "*bold*"

    def test_converts_strikethrough(self):
        assert _format_whatsapp("~~strike~~") == "~strike~"

    def test_preserves_italic(self):
        # WhatsApp uses _italic_ same as markdown
        assert _format_whatsapp("_italic_") == "_italic_"

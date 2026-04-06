"""Tests for carpenter.kb.parse — markdown parsing utilities."""

from carpenter.kb.parse import (
    extract_links,
    extract_auto_sections,
    replace_auto_sections,
    extract_title_and_description,
)


class TestExtractLinks:
    def test_simple_link(self):
        links = extract_links("See [[scheduling/tools]] for details.")
        assert links == [("scheduling/tools", None)]

    def test_link_with_text(self):
        links = extract_links("See [[scheduling/tools|the scheduling tools]].")
        assert links == [("scheduling/tools", "the scheduling tools")]

    def test_multiple_links(self):
        content = "[[arcs/tools]] · [[messaging/tools]] · [[scheduling/config]]"
        links = extract_links(content)
        assert len(links) == 3
        assert links[0] == ("arcs/tools", None)
        assert links[1] == ("messaging/tools", None)
        assert links[2] == ("scheduling/config", None)

    def test_no_links(self):
        links = extract_links("No links here.")
        assert links == []

    def test_mixed_links(self):
        content = "See [[a]] and [[b|display]] here."
        links = extract_links(content)
        assert links == [("a", None), ("b", "display")]

    def test_whitespace_in_links(self):
        links = extract_links("[[ scheduling/tools ]]")
        assert links == [("scheduling/tools", None)]


class TestExtractAutoSections:
    def test_simple_auto_section(self):
        content = "Before\n<!-- auto:tools source.py -->generated content<!-- /auto -->\nAfter"
        sections = extract_auto_sections(content)
        assert len(sections) == 1
        assert sections[0]["name"] == "tools"
        assert sections[0]["source"] == "source.py"
        assert sections[0]["content"] == "generated content"

    def test_no_auto_sections(self):
        sections = extract_auto_sections("No auto sections here.")
        assert sections == []

    def test_multiple_auto_sections(self):
        content = (
            "<!-- auto:a file1.py -->content1<!-- /auto -->\n"
            "middle\n"
            "<!-- auto:b file2.py -->content2<!-- /auto -->"
        )
        sections = extract_auto_sections(content)
        assert len(sections) == 2
        assert sections[0]["name"] == "a"
        assert sections[1]["name"] == "b"


class TestReplaceAutoSections:
    def test_replace_section(self):
        content = "Before\n<!-- auto:tools src.py -->old<!-- /auto -->\nAfter"
        result = replace_auto_sections(content, {"tools": "new"})
        assert "<!-- auto:tools src.py -->new<!-- /auto -->" in result
        assert "old" not in result

    def test_replace_nonexistent_section(self):
        content = "Before\n<!-- auto:tools src.py -->old<!-- /auto -->\nAfter"
        result = replace_auto_sections(content, {"other": "new"})
        assert "old" in result  # unchanged


class TestExtractTitleAndDescription:
    def test_basic(self):
        content = "# My Title\n\nThis is the description.\n\n## Details\nMore stuff."
        title, desc = extract_title_and_description(content)
        assert title == "My Title"
        assert desc == "This is the description."

    def test_no_title(self):
        content = "No heading here.\nJust text."
        title, desc = extract_title_and_description(content)
        assert title == ""
        assert desc == "No heading here."

    def test_empty_content(self):
        title, desc = extract_title_and_description("")
        assert title == ""
        assert desc == ""

    def test_title_only(self):
        content = "# Title Only"
        title, desc = extract_title_and_description(content)
        assert title == "Title Only"
        assert desc == ""

    def test_skips_subheadings(self):
        content = "# Title\n\n## Subtitle\n\nActual description."
        title, desc = extract_title_and_description(content)
        assert title == "Title"
        assert desc == "Actual description."

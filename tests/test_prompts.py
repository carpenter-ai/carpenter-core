"""Tests for carpenter.prompts — prompt template loader."""

import os
from pathlib import Path

from carpenter.prompts import (
    PromptSection,
    install_prompt_defaults,
    load_prompt_sections,
    render_prompt_sections,
    _parse_front_matter,
)


class TestParseFrontMatter:
    def test_no_front_matter(self):
        meta, body = _parse_front_matter("Just some content.")
        assert meta == {}
        assert body == "Just some content."

    def test_with_front_matter(self):
        content = "---\ncompact: true\n---\nBody here."
        meta, body = _parse_front_matter(content)
        assert meta.get("compact") is True
        assert body == "Body here."

    def test_unclosed_front_matter(self):
        content = "---\ncompact: true\nBody here."
        meta, body = _parse_front_matter(content)
        assert meta == {}
        assert body == content

    def test_empty_front_matter(self):
        content = "---\n---\nBody."
        meta, body = _parse_front_matter(content)
        # yaml.safe_load of empty string returns None → empty dict
        assert meta == {} or meta is None
        assert body == "Body."


class TestInstallPromptDefaults:
    def test_installs_to_new_dir(self, tmp_path):
        prompts_dir = str(tmp_path / "fresh_prompts")
        result = install_prompt_defaults(prompts_dir)
        assert result["status"] == "installed"
        assert result["copied"] > 0
        assert os.path.isdir(prompts_dir)

    def test_skips_existing_dir(self, tmp_path):
        prompts_dir = str(tmp_path / "empty_prompts")
        os.makedirs(prompts_dir)
        result = install_prompt_defaults(prompts_dir)
        assert result["status"] == "exists"
        assert result["copied"] == 0


class TestLoadPromptSections:
    def test_loads_default_templates(self, tmp_path):
        prompts_dir = str(tmp_path / "prompts")
        install_prompt_defaults(prompts_dir)
        sections = load_prompt_sections(prompts_dir)
        assert len(sections) > 0
        # First section should be identity (00-identity.md)
        assert sections[0].name == "identity"
        assert sections[0].order == 0

    def test_sorted_by_order(self, tmp_path):
        prompts_dir = str(tmp_path / "prompts")
        install_prompt_defaults(prompts_dir)
        sections = load_prompt_sections(prompts_dir)
        orders = [s.order for s in sections]
        assert orders == sorted(orders)

    def test_compact_flag(self, tmp_path):
        prompts_dir = str(tmp_path / "prompts")
        install_prompt_defaults(prompts_dir)
        sections = load_prompt_sections(prompts_dir)
        # identity should be compact=True
        identity = next(s for s in sections if s.name == "identity")
        assert identity.compact is True
        # A file without compact front-matter should default to compact=False
        Path(prompts_dir, "99-extra.md").write_text("Extra section.")
        sections = load_prompt_sections(prompts_dir)
        extra = next(s for s in sections if s.name == "extra")
        assert extra.compact is False

    def test_empty_dir(self, tmp_path):
        prompts_dir = str(tmp_path / "empty")
        os.makedirs(prompts_dir)
        sections = load_prompt_sections(prompts_dir)
        assert sections == []

    def test_nonexistent_dir(self, tmp_path):
        sections = load_prompt_sections(str(tmp_path / "nope"))
        assert sections == []

    def test_custom_section(self, tmp_path):
        prompts_dir = str(tmp_path / "custom_prompts")
        os.makedirs(prompts_dir)
        Path(prompts_dir, "50-custom.md").write_text("Custom section content.")
        sections = load_prompt_sections(prompts_dir)
        assert len(sections) == 1
        assert sections[0].name == "custom"
        assert sections[0].order == 50
        assert sections[0].content == "Custom section content."


class TestRenderPromptSections:
    def test_no_jinja_passthrough(self):
        sections = [PromptSection("test", "Plain text.", False, 0)]
        result = render_prompt_sections(sections, {})
        assert result[0].content == "Plain text."

    def test_jinja_rendering(self):
        sections = [PromptSection("test", "Model: {{ model_name }}", False, 0)]
        result = render_prompt_sections(sections, {"model_name": "claude-3"})
        assert result[0].content == "Model: claude-3"

    def test_jinja_not_installed(self, monkeypatch):
        """When jinja2 isn't available, sections pass through unchanged."""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "jinja2":
                raise ImportError("No jinja2")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        sections = [PromptSection("test", "{{ var }}", False, 0)]
        result = render_prompt_sections(sections, {"var": "value"})
        assert result[0].content == "{{ var }}"

    def test_preserves_section_metadata(self):
        sections = [PromptSection("foo", "{{ x }}", True, 5)]
        result = render_prompt_sections(sections, {"x": "bar"})
        assert result[0].name == "foo"
        assert result[0].compact is True
        assert result[0].order == 5
        assert result[0].content == "bar"


class TestPromptIntegration:
    def test_full_load_and_render(self, tmp_path):
        """Install defaults, load, render with context."""
        # Use a fresh directory (not the symlinked shared one) so we get
        # exactly the shipped defaults without pollution from other tests.
        prompts_dir = str(tmp_path / "fresh_prompts")
        install_prompt_defaults(prompts_dir)
        sections = load_prompt_sections(prompts_dir)
        rendered = render_prompt_sections(
            sections,
            {"model_name": "test-model"},
        )

        # Should have all 4 default sections
        assert len(rendered) == 4

        # All should have content when context variables are provided
        for s in rendered:
            assert s.content.strip(), f"Section {s.name} has no content"

    def test_compact_filtering(self, tmp_path):
        """Compact mode should return only compact=True sections."""
        prompts_dir = str(tmp_path / "prompts")
        install_prompt_defaults(prompts_dir)
        sections = load_prompt_sections(prompts_dir)
        compact_sections = [s for s in sections if s.compact]
        non_compact = [s for s in sections if not s.compact]
        # Should have both types
        assert len(compact_sections) > 0
        assert len(non_compact) > 0

"""Tests for the resource template registry."""

from carpenter.core.resources import registry


class TestTemplateLookup:
    def setup_method(self):
        # Ensure a clean cache per test — the registry is module-scoped.
        registry.reload_templates()

    def test_loads_seed_yaml(self):
        entries = registry.list_templates()
        assert len(entries) >= 1
        names = {e["name"] for e in entries}
        assert "html_to_summary" in names

    def test_get_template_for_html_returns_html_to_summary(self):
        t = registry.get_template_for("html")
        assert t is not None
        assert t["name"] == "html_to_summary"
        assert t["produces_content_type"] == "text-summary"

    def test_get_template_for_unknown_returns_none(self):
        assert registry.get_template_for("pdf") is None
        assert registry.get_template_for("") is None

    def test_get_template_by_name(self):
        t = registry.get_template_by_name("html_to_summary")
        assert t is not None
        assert t["consumes_content_type"] == "html"

    def test_get_template_by_name_unknown(self):
        assert registry.get_template_by_name("does-not-exist") is None
        assert registry.get_template_by_name("") is None

    def test_returned_dicts_are_copies(self):
        """Mutating the returned dict must not poison the cache."""
        t = registry.get_template_by_name("html_to_summary")
        assert t is not None
        t["name"] = "mutated"
        again = registry.get_template_by_name("html_to_summary")
        assert again is not None
        assert again["name"] == "html_to_summary"

    def test_reload_clears_cache(self):
        # Prime the cache.
        registry.list_templates()
        assert registry._templates is not None
        registry.reload_templates()
        assert registry._templates is None
        # Next call re-loads lazily.
        again = registry.list_templates()
        assert any(e["name"] == "html_to_summary" for e in again)
        assert registry._templates is not None

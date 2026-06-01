"""Shape tests for config_seed/resource_templates.yaml.

Verifies the seed file parses and that every entry has the required
fields.  Keeps the contract between the seed content and consumers
(PR3's fetch_web_content, future registry users) explicit.
"""

from pathlib import Path

import pytest
import yaml


SEED_PATH = (
    Path(__file__).resolve().parents[3]
    / "config_seed"
    / "resource_templates.yaml"
)

REQUIRED_FIELDS = ("name", "consumes_content_type", "produces_content_type")


def _load_seed() -> dict:
    with open(SEED_PATH, "r") as f:
        return yaml.safe_load(f)


def test_seed_file_exists():
    assert SEED_PATH.exists(), f"Seed YAML not found: {SEED_PATH}"


def test_seed_parses_as_mapping():
    data = _load_seed()
    assert isinstance(data, dict)
    assert "templates" in data
    assert isinstance(data["templates"], list)
    assert len(data["templates"]) >= 1


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_every_template_has_required_field(field):
    data = _load_seed()
    for entry in data["templates"]:
        assert isinstance(entry, dict), f"Non-mapping entry: {entry!r}"
        assert field in entry, (
            f"Template {entry.get('name', '<unnamed>')} missing {field!r}"
        )
        assert entry[field], f"Template {entry['name']} has empty {field}"


def test_html_to_summary_template_shape():
    """Anchor test — PR3 depends on this exact template being present."""
    data = _load_seed()
    by_name = {e["name"]: e for e in data["templates"]}
    assert "html_to_summary" in by_name
    t = by_name["html_to_summary"]
    assert t["consumes_content_type"] == "html"
    assert t["produces_content_type"] == "text-summary"
    # Soft fields — present but not shape-checked beyond existence
    assert "reviewer_profile" in t
    assert "judge_profile" in t
    assert "goal_template" in t

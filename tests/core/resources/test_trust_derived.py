"""Tests for derived Resource trust.

The trust function MUST ignore any stored flag and derive trust purely
from (produced_by_template, template_verdict).
"""

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager
from carpenter.core.resources.trust import resource_trust, is_trusted


def _trusted_arc(name="arc"):
    return arc_manager.create_arc(name=name, integrity_level="trusted")


class TestResourceTrust:
    def test_raw_resource_untrusted(self):
        arc = _trusted_arc()
        rid = res_manager.create_resource(
            content_type="text/html", file_path=None,
            produced_by_arc_id=arc,
        )
        row = res_manager.get_resource(rid)
        assert resource_trust(row) == "untrusted"
        assert is_trusted(rid) is False

    def test_pending_derived_untrusted(self):
        arc = _trusted_arc()
        rid = res_manager.derive_resource(
            content_type="text/html", file_path=None,
            produced_by_arc_id=arc,
            produced_by_template="web.fetch_url.v1",
            # default verdict is pending
        )
        row = res_manager.get_resource(rid)
        assert row["template_verdict"] == "pending"
        assert resource_trust(row) == "untrusted"

    def test_approved_with_template_trusted(self):
        arc = _trusted_arc()
        rid = res_manager.derive_resource(
            content_type="text/html", file_path=None,
            produced_by_arc_id=arc,
            produced_by_template="web.fetch_url.v1",
        )
        res_manager.mark_template_verdict(rid, "approved")
        row = res_manager.get_resource(rid)
        assert resource_trust(row) == "trusted"
        assert is_trusted(rid) is True

    def test_rejected_untrusted(self):
        arc = _trusted_arc()
        rid = res_manager.derive_resource(
            content_type="text/html", file_path=None,
            produced_by_arc_id=arc,
            produced_by_template="web.fetch_url.v1",
        )
        res_manager.mark_template_verdict(rid, "rejected")
        row = res_manager.get_resource(rid)
        assert resource_trust(row) == "untrusted"

    def test_approved_without_template_impossible(self):
        """mark_template_verdict refuses to flip a raw-ingest row."""
        arc = _trusted_arc()
        rid = res_manager.create_resource(
            content_type="text/html", file_path=None,
            produced_by_arc_id=arc,
        )
        with pytest.raises(ValueError, match="produced_by_template"):
            res_manager.mark_template_verdict(rid, "approved")
        # And trust is still untrusted
        assert is_trusted(rid) is False

    def test_is_trusted_unknown_id(self):
        assert is_trusted(999_999) is False

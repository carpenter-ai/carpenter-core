"""Invariant tests for ``mark_template_verdict``.

Decisions enforced here:
- Raw-ingest Resources (produced_by_template NULL) cannot be reclassified.
- Only 'approved' and 'rejected' are accepted (NULL/'pending' disallowed as
  target values — 'pending' is a creation-only state).
- Idempotent when the new verdict equals the current one.
- approved <-> rejected transitions are REJECTED (terminal decision).
"""

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager


def _derived_resource() -> int:
    arc = arc_manager.create_arc(name="producer", integrity_level="trusted")
    return res_manager.derive_resource(
        content_type="text/html", file_path=None,
        produced_by_arc_id=arc,
        produced_by_template="web.fetch_url.v1",
    )


class TestVerdictInvariants:
    def test_rejects_null_produced_by_template(self):
        arc = arc_manager.create_arc(name="a", integrity_level="trusted")
        rid = res_manager.create_resource(
            content_type="text/html", file_path=None,
            produced_by_arc_id=arc,
        )
        with pytest.raises(ValueError, match="produced_by_template"):
            res_manager.mark_template_verdict(rid, "approved")

    def test_rejects_invalid_verdict_string(self):
        rid = _derived_resource()
        with pytest.raises(ValueError, match="Invalid verdict"):
            res_manager.mark_template_verdict(rid, "maybe")

    def test_rejects_pending_as_target(self):
        """'pending' is a creation-only state; you can't revert to it."""
        rid = _derived_resource()
        with pytest.raises(ValueError, match="Invalid verdict"):
            res_manager.mark_template_verdict(rid, "pending")

    def test_idempotent_on_repeat_approved(self):
        rid = _derived_resource()
        res_manager.mark_template_verdict(rid, "approved")
        res_manager.mark_template_verdict(rid, "approved")  # no error
        row = res_manager.get_resource(rid)
        assert row["template_verdict"] == "approved"

    def test_idempotent_on_repeat_rejected(self):
        rid = _derived_resource()
        res_manager.mark_template_verdict(rid, "rejected")
        res_manager.mark_template_verdict(rid, "rejected")
        row = res_manager.get_resource(rid)
        assert row["template_verdict"] == "rejected"

    def test_transition_approved_to_rejected_forbidden(self):
        rid = _derived_resource()
        res_manager.mark_template_verdict(rid, "approved")
        with pytest.raises(ValueError, match="terminal"):
            res_manager.mark_template_verdict(rid, "rejected")

    def test_transition_rejected_to_approved_forbidden(self):
        rid = _derived_resource()
        res_manager.mark_template_verdict(rid, "rejected")
        with pytest.raises(ValueError, match="terminal"):
            res_manager.mark_template_verdict(rid, "approved")

    def test_pending_to_approved_allowed(self):
        rid = _derived_resource()
        res_manager.mark_template_verdict(rid, "approved")
        assert res_manager.get_resource(rid)["template_verdict"] == "approved"

    def test_pending_to_rejected_allowed(self):
        rid = _derived_resource()
        res_manager.mark_template_verdict(rid, "rejected")
        assert res_manager.get_resource(rid)["template_verdict"] == "rejected"

    def test_missing_resource_raises(self):
        with pytest.raises(ValueError, match="Resource"):
            res_manager.mark_template_verdict(999_999, "approved")

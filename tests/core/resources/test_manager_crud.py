"""CRUD tests for the Resource manager."""

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager


def _trusted_arc(name: str = "arc") -> int:
    return arc_manager.create_arc(name=name, integrity_level="trusted")


def _untrusted_arc(parent: int, name: str = "u") -> int:
    """add_child path is the only way to create an untrusted arc."""
    return arc_manager.add_child(parent, name, integrity_level="untrusted")


class TestCreateAndGet:
    def test_create_resource_minimal(self):
        arc = _trusted_arc()
        rid = res_manager.create_resource(
            content_type="text/html",
            file_path="/tmp/x.html",
            produced_by_arc_id=arc,
        )
        row = res_manager.get_resource(rid)
        assert row is not None
        assert row["content_type"] == "text/html"
        assert row["file_path"] == "/tmp/x.html"
        assert row["produced_by_arc_id"] == arc
        assert row["produced_by_template"] is None
        assert row["template_verdict"] is None
        assert row["pinned"] == 0
        assert row["deprecated_at"] is None
        assert row["deleted_at"] is None

    def test_create_resource_nullable_fields(self):
        rid = res_manager.create_resource(
            content_type="application/octet-stream",
            file_path=None,
            produced_by_arc_id=None,
        )
        row = res_manager.get_resource(rid)
        assert row["file_path"] is None
        assert row["produced_by_arc_id"] is None

    def test_get_resource_missing(self):
        assert res_manager.get_resource(999_999) is None

    def test_derive_resource_defaults_pending(self):
        arc = _trusted_arc()
        rid = res_manager.derive_resource(
            content_type="text/html",
            file_path="/tmp/d.html",
            produced_by_arc_id=arc,
            produced_by_template="web.fetch_url.v1",
        )
        row = res_manager.get_resource(rid)
        assert row["produced_by_template"] == "web.fetch_url.v1"
        assert row["template_verdict"] == "pending"

    def test_derive_requires_template_name(self):
        arc = _trusted_arc()
        with pytest.raises(ValueError, match="produced_by_template"):
            res_manager.derive_resource(
                content_type="text/html",
                file_path=None,
                produced_by_arc_id=arc,
                produced_by_template="",
            )

    def test_derive_rejects_bad_verdict(self):
        arc = _trusted_arc()
        with pytest.raises(ValueError, match="template_verdict"):
            res_manager.derive_resource(
                content_type="text/html",
                file_path=None,
                produced_by_arc_id=arc,
                produced_by_template="web.fetch_url.v1",
                template_verdict="maybe",
            )


class TestLinkArcResource:
    def test_output_role_any_arc(self):
        parent = _trusted_arc("p")
        untrusted = _untrusted_arc(parent)
        rid = res_manager.create_resource(
            content_type="text/html",
            file_path=None,
            produced_by_arc_id=untrusted,
        )
        # Untrusted arc producing a Resource is ALLOWED.
        link_id = res_manager.link_arc_resource(
            arc_id=untrusted, resource_id=rid, role="output"
        )
        assert link_id > 0

    def test_input_role_forbidden_for_untrusted(self):
        parent = _trusted_arc("p2")
        untrusted = _untrusted_arc(parent)
        rid = res_manager.create_resource(
            content_type="text/html",
            file_path=None,
            produced_by_arc_id=parent,
        )
        with pytest.raises(ValueError, match="Untrusted arc"):
            res_manager.link_arc_resource(
                arc_id=untrusted, resource_id=rid, role="input"
            )

    def test_input_role_allowed_for_trusted(self):
        arc = _trusted_arc("t")
        rid = res_manager.create_resource(
            content_type="text/html",
            file_path=None,
            produced_by_arc_id=None,
        )
        link_id = res_manager.link_arc_resource(
            arc_id=arc, resource_id=rid, role="input"
        )
        assert link_id > 0

    def test_invalid_role_raises(self):
        arc = _trusted_arc()
        rid = res_manager.create_resource(
            content_type="text/html",
            file_path=None,
            produced_by_arc_id=None,
        )
        with pytest.raises(ValueError, match="Invalid role"):
            res_manager.link_arc_resource(
                arc_id=arc, resource_id=rid, role="consume"
            )

    def test_link_is_idempotent(self):
        arc = _trusted_arc()
        rid = res_manager.create_resource(
            content_type="text/html",
            file_path=None,
            produced_by_arc_id=None,
        )
        first = res_manager.link_arc_resource(
            arc_id=arc, resource_id=rid, role="input"
        )
        second = res_manager.link_arc_resource(
            arc_id=arc, resource_id=rid, role="input"
        )
        assert first == second

    def test_missing_arc_raises(self):
        rid = res_manager.create_resource(
            content_type="text/html",
            file_path=None,
            produced_by_arc_id=None,
        )
        with pytest.raises(ValueError, match="Arc"):
            res_manager.link_arc_resource(
                arc_id=999_999, resource_id=rid, role="output"
            )

    def test_missing_resource_raises(self):
        arc = _trusted_arc()
        with pytest.raises(ValueError, match="Resource"):
            res_manager.link_arc_resource(
                arc_id=arc, resource_id=999_999, role="output"
            )


class TestListResourcesForArc:
    def test_round_trip_by_role(self):
        producer = _trusted_arc("producer")
        consumer = _trusted_arc("consumer")

        r1 = res_manager.create_resource(
            content_type="text/html", file_path=None,
            produced_by_arc_id=producer,
        )
        r2 = res_manager.create_resource(
            content_type="text/html", file_path=None,
            produced_by_arc_id=producer,
        )
        res_manager.link_arc_resource(arc_id=producer, resource_id=r1, role="output")
        res_manager.link_arc_resource(arc_id=producer, resource_id=r2, role="output")
        res_manager.link_arc_resource(arc_id=consumer, resource_id=r1, role="input")

        outputs = res_manager.list_resources_for_arc(producer, role="output")
        assert {r["id"] for r in outputs} == {r1, r2}

        inputs = res_manager.list_resources_for_arc(consumer, role="input")
        assert [r["id"] for r in inputs] == [r1]

        # No role filter returns both
        all_for_producer = res_manager.list_resources_for_arc(producer)
        assert len(all_for_producer) == 2

    def test_invalid_role_raises(self):
        arc = _trusted_arc()
        with pytest.raises(ValueError, match="Invalid role"):
            res_manager.list_resources_for_arc(arc, role="nope")

    def test_empty_for_unknown_arc(self):
        assert res_manager.list_resources_for_arc(999_999) == []


class TestPinAndRetain:
    def test_pin_unpin(self):
        rid = res_manager.create_resource(
            content_type="text/plain", file_path=None, produced_by_arc_id=None,
        )
        assert res_manager.get_resource(rid)["pinned"] == 0
        res_manager.pin(rid)
        assert res_manager.get_resource(rid)["pinned"] == 1
        res_manager.unpin(rid)
        assert res_manager.get_resource(rid)["pinned"] == 0

    def test_set_retain_until_set_and_clear(self):
        rid = res_manager.create_resource(
            content_type="text/plain", file_path=None, produced_by_arc_id=None,
        )
        res_manager.set_retain_until(rid, "2030-01-01T00:00:00+00:00")
        assert res_manager.get_resource(rid)["retain_until"].startswith("2030-01-01")
        res_manager.set_retain_until(rid, None)
        assert res_manager.get_resource(rid)["retain_until"] is None

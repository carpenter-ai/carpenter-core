"""Deprecation tests."""

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager


def _arc(name):
    return arc_manager.create_arc(name=name, integrity_level="trusted")


def test_deprecate_resource_idempotent():
    rid = res_manager.create_resource(
        content_type="text/plain", file_path=None, produced_by_arc_id=None,
    )
    assert res_manager.get_resource(rid)["deprecated_at"] is None

    res_manager.deprecate_resource(rid)
    first = res_manager.get_resource(rid)["deprecated_at"]
    assert first is not None

    res_manager.deprecate_resource(rid)
    second = res_manager.get_resource(rid)["deprecated_at"]
    # Idempotent: the timestamp does NOT get bumped.
    assert first == second


def test_deprecate_inputs_of_arc_only_inputs():
    producer = _arc("producer")
    consumer = _arc("consumer")
    sibling = _arc("sibling")

    inp = res_manager.create_resource(
        content_type="text/plain", file_path=None, produced_by_arc_id=producer,
    )
    out = res_manager.create_resource(
        content_type="text/plain", file_path=None, produced_by_arc_id=consumer,
    )
    other = res_manager.create_resource(
        content_type="text/plain", file_path=None, produced_by_arc_id=sibling,
    )

    res_manager.link_arc_resource(arc_id=consumer, resource_id=inp, role="input")
    res_manager.link_arc_resource(arc_id=consumer, resource_id=out, role="output")
    res_manager.link_arc_resource(arc_id=sibling, resource_id=other, role="input")

    count = res_manager.deprecate_inputs_of_arc(consumer)
    assert count == 1

    assert res_manager.get_resource(inp)["deprecated_at"] is not None
    assert res_manager.get_resource(out)["deprecated_at"] is None
    assert res_manager.get_resource(other)["deprecated_at"] is None


def test_deprecate_inputs_of_arc_skips_already_deprecated():
    arc = _arc("a")
    r = res_manager.create_resource(
        content_type="text/plain", file_path=None, produced_by_arc_id=None,
    )
    res_manager.link_arc_resource(arc_id=arc, resource_id=r, role="input")
    res_manager.deprecate_resource(r)

    # Already deprecated — should not be counted again.
    count = res_manager.deprecate_inputs_of_arc(arc)
    assert count == 0


def test_deprecate_inputs_of_unknown_arc_is_zero():
    assert res_manager.deprecate_inputs_of_arc(999_999) == 0

"""Lineage traversal tests."""

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager


def _arc(name):
    return arc_manager.create_arc(name=name, integrity_level="trusted")


def test_lineage_two_generations():
    """A -> R1; B (inputs R1) -> R2; lineage(R2) == [R2, R1]."""
    arc_a = _arc("A")
    arc_b = _arc("B")

    r1 = res_manager.create_resource(
        content_type="text/html", file_path=None,
        produced_by_arc_id=arc_a,
    )
    res_manager.link_arc_resource(arc_id=arc_a, resource_id=r1, role="output")
    res_manager.link_arc_resource(arc_id=arc_b, resource_id=r1, role="input")

    r2 = res_manager.create_resource(
        content_type="text/plain", file_path=None,
        produced_by_arc_id=arc_b,
    )
    res_manager.link_arc_resource(arc_id=arc_b, resource_id=r2, role="output")

    lineage = res_manager.get_lineage(r2)
    ids = [r["id"] for r in lineage]
    assert ids == [r2, r1]


def test_lineage_single_resource_no_producer():
    rid = res_manager.create_resource(
        content_type="text/plain", file_path=None,
        produced_by_arc_id=None,
    )
    lineage = res_manager.get_lineage(rid)
    assert [r["id"] for r in lineage] == [rid]


def test_lineage_missing_returns_empty():
    assert res_manager.get_lineage(999_999) == []


def test_lineage_dedup_diamond():
    """A produces R0; B and C each consume R0 and produce R_b, R_c; D consumes both.
    R0 should appear only once in lineage(R_d)."""
    a = _arc("A")
    b = _arc("B")
    c = _arc("C")
    d = _arc("D")

    r0 = res_manager.create_resource(
        content_type="text/plain", file_path=None, produced_by_arc_id=a,
    )
    res_manager.link_arc_resource(arc_id=a, resource_id=r0, role="output")
    res_manager.link_arc_resource(arc_id=b, resource_id=r0, role="input")
    res_manager.link_arc_resource(arc_id=c, resource_id=r0, role="input")

    r_b = res_manager.create_resource(
        content_type="text/plain", file_path=None, produced_by_arc_id=b,
    )
    res_manager.link_arc_resource(arc_id=b, resource_id=r_b, role="output")

    r_c = res_manager.create_resource(
        content_type="text/plain", file_path=None, produced_by_arc_id=c,
    )
    res_manager.link_arc_resource(arc_id=c, resource_id=r_c, role="output")

    r_d = res_manager.create_resource(
        content_type="text/plain", file_path=None, produced_by_arc_id=d,
    )
    res_manager.link_arc_resource(arc_id=d, resource_id=r_b, role="input")
    res_manager.link_arc_resource(arc_id=d, resource_id=r_c, role="input")
    res_manager.link_arc_resource(arc_id=d, resource_id=r_d, role="output")

    lineage = res_manager.get_lineage(r_d)
    ids = [r["id"] for r in lineage]
    # r_d first, then r_b and r_c (same BFS generation — order within a
    # generation is link-time order), then r0 exactly once.
    assert ids[0] == r_d
    assert set(ids[1:3]) == {r_b, r_c}
    assert ids[3] == r0
    assert len(ids) == 4  # no duplicate r0

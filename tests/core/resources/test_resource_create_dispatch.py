"""Integration tests for the ``resource.create`` dispatch tool (Phase B PR B1).

``resource.create`` lets any running arc register a new raw Resource row
that it will subsequently write a blob to and then finalize via
``resource.finalize``.  The row is ``produced_by_template=NULL`` — a raw
ingest, forever untrusted per ``resource_trust``.

Content-type is a free-form label (NOT validated, NOT a trust claim):
the explicit design decision is to let producers pass any string and
defer trust to template-driven review pipelines.
"""

import os
from pathlib import Path

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager
from carpenter.core.resources import is_trusted, resource_trust
from carpenter.tool_backends import resource as resource_backend


def _make_arc(agent_type: str = "EXECUTOR", integrity: str = "trusted") -> int:
    """Create a root arc with the given agent_type / integrity_level."""
    return arc_manager.create_arc(
        name=f"test-{agent_type.lower()}",
        agent_type=agent_type,
        integrity_level=integrity,
    )


class TestResourceCreateBasic:
    def test_executor_creates_raw_resource(self):
        arc_id = _make_arc("EXECUTOR")
        result = resource_backend.handle_create(
            {
                "content_type": "text/plain",
                "source_descriptor": "unit-test",
                "_caller_arc_id": arc_id,
            }
        )

        assert "resource_id" in result
        assert "file_path" in result
        rid = result["resource_id"]

        row = res_manager.get_resource(rid)
        assert row is not None
        assert row["content_type"] == "text/plain"
        assert row["produced_by_arc_id"] == arc_id
        assert row["produced_by_template"] is None
        assert row["template_verdict"] is None
        assert row["source_descriptor"] == "unit-test"
        assert row["file_path"] == result["file_path"]

    def test_returned_path_parent_dir_exists_but_file_does_not(self):
        arc_id = _make_arc("EXECUTOR")
        result = resource_backend.handle_create(
            {"content_type": "application/octet-stream", "_caller_arc_id": arc_id}
        )
        path = Path(result["file_path"])
        assert path.parent.exists() and path.parent.is_dir()
        assert not path.exists(), "blob file must NOT be created by resource.create"

    def test_created_resource_is_linked_as_output(self):
        arc_id = _make_arc("EXECUTOR")
        result = resource_backend.handle_create(
            {"content_type": "text/plain", "_caller_arc_id": arc_id}
        )
        rid = result["resource_id"]

        outputs = res_manager.list_resources_for_arc(arc_id, role="output")
        assert any(r["id"] == rid for r in outputs)
        inputs = res_manager.list_resources_for_arc(arc_id, role="input")
        assert all(r["id"] != rid for r in inputs)

    def test_raw_resource_is_untrusted(self):
        """Raw Resources (produced_by_template=NULL) are forever untrusted."""
        arc_id = _make_arc("EXECUTOR")
        result = resource_backend.handle_create(
            {"content_type": "text/html", "_caller_arc_id": arc_id}
        )
        row = res_manager.get_resource(result["resource_id"])
        assert resource_trust(row) == "untrusted"
        assert is_trusted(result["resource_id"]) is False


class TestResourceCreateMultipleRoles:
    def test_planner_can_create(self):
        arc_id = _make_arc("PLANNER")
        result = resource_backend.handle_create(
            {"content_type": "application/json", "_caller_arc_id": arc_id}
        )
        row = res_manager.get_resource(result["resource_id"])
        assert row["produced_by_arc_id"] == arc_id
        assert row["produced_by_template"] is None

    def test_reviewer_can_create(self):
        arc_id = _make_arc("REVIEWER")
        result = resource_backend.handle_create(
            {"content_type": "text/markdown", "_caller_arc_id": arc_id}
        )
        row = res_manager.get_resource(result["resource_id"])
        assert row["produced_by_arc_id"] == arc_id
        assert row["produced_by_template"] is None


class TestResourceCreateThenFinalize:
    def test_write_and_finalize_flow(self):
        import hashlib

        arc_id = _make_arc("EXECUTOR")
        result = resource_backend.handle_create(
            {"content_type": "text/plain", "_caller_arc_id": arc_id}
        )
        rid = result["resource_id"]
        path = result["file_path"]

        # Caller writes the blob at the returned path, then finalizes.
        payload = b"hello world"
        with open(path, "wb") as f:
            f.write(payload)

        fin = resource_backend.handle_finalize(
            {"resource_id": rid, "_caller_arc_id": arc_id}
        )
        assert fin["ok"] is True
        assert fin["byte_size"] == len(payload)
        assert fin["content_hash"] == hashlib.sha256(payload).hexdigest()

        row = res_manager.get_resource(rid)
        assert row["byte_size"] == len(payload)
        assert row["content_hash"] == hashlib.sha256(payload).hexdigest()


class TestResourceCreateUntrustedArcProducer:
    def test_untrusted_arc_can_produce(self):
        """Untrusted arcs may PRODUCE Resources — trust gating is on input role."""
        parent = _make_arc("PLANNER")
        child = arc_manager.add_child(
            parent, "untrusted-kid", integrity_level="untrusted",
            agent_type="EXECUTOR",
        )

        result = resource_backend.handle_create(
            {"content_type": "text/plain", "_caller_arc_id": child}
        )
        row = res_manager.get_resource(result["resource_id"])
        assert row["produced_by_arc_id"] == child

        # And it's linked as output on the untrusted arc.
        outputs = res_manager.list_resources_for_arc(child, role="output")
        assert any(r["id"] == result["resource_id"] for r in outputs)


class TestResourceCreateConsumableByOtherArc:
    def test_different_trusted_arc_can_link_as_input(self):
        """The raw Resource can later be read as input by a trusted arc."""
        producer = _make_arc("EXECUTOR")
        result = resource_backend.handle_create(
            {"content_type": "text/plain", "_caller_arc_id": producer}
        )
        rid = result["resource_id"]

        consumer = _make_arc("REVIEWER", integrity="trusted")
        link_id = res_manager.link_arc_resource(
            arc_id=consumer, resource_id=rid, role="input"
        )
        assert link_id > 0

        inputs = res_manager.list_resources_for_arc(consumer, role="input")
        assert any(r["id"] == rid for r in inputs)


class TestResourceCreateValidation:
    def test_missing_caller_arc_id_raises(self):
        with pytest.raises(ValueError, match="_caller_arc_id"):
            resource_backend.handle_create({"content_type": "text/plain"})

    def test_missing_content_type_raises(self):
        arc_id = _make_arc("EXECUTOR")
        with pytest.raises(ValueError, match="content_type"):
            resource_backend.handle_create({"_caller_arc_id": arc_id})

    def test_empty_content_type_raises(self):
        arc_id = _make_arc("EXECUTOR")
        with pytest.raises(ValueError, match="content_type"):
            resource_backend.handle_create(
                {"content_type": "", "_caller_arc_id": arc_id}
            )

    def test_non_string_content_type_raises(self):
        arc_id = _make_arc("EXECUTOR")
        with pytest.raises(ValueError, match="content_type"):
            resource_backend.handle_create(
                {"content_type": 123, "_caller_arc_id": arc_id}
            )

    def test_non_string_source_descriptor_raises(self):
        arc_id = _make_arc("EXECUTOR")
        with pytest.raises(ValueError, match="source_descriptor"):
            resource_backend.handle_create(
                {
                    "content_type": "text/plain",
                    "source_descriptor": 42,
                    "_caller_arc_id": arc_id,
                }
            )


class TestResourceCreateDispatchRegistration:
    def test_registered_in_dispatch_table(self):
        from carpenter.api.callbacks import _DISPATCH
        assert "resource.create" in _DISPATCH
        assert callable(_DISPATCH["resource.create"])

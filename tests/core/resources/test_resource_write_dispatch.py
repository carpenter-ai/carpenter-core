"""Integration tests for the ``resource.write`` dispatch tool.

``resource.write`` is the trusted persistence path for producer arcs —
notably untrusted EXECUTOR arcs, which cannot write the Resource blob
themselves (``files.write`` refuses out-of-workspace writes and ``open``
is blocked in the RestrictedPython sandbox).  The handler does natural
Python file I/O on the trusted side: it writes the content payload (a
``str`` verbatim, or any JSON-serializable object via ``json.dump``) AND
finalizes (byte_size / content_hash) in one call.

Auth mirrors ``resource.finalize``: the caller arc must be the
Resource's ``produced_by_arc_id`` when both are present.
"""

import hashlib
import json

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager
from carpenter.core.resources import read_resource_content
from carpenter.tool_backends import resource as resource_backend


def _make_arc(agent_type: str = "EXECUTOR", integrity: str = "trusted") -> int:
    """Create a root arc with the given agent_type / integrity_level."""
    return arc_manager.create_arc(
        name=f"test-{agent_type.lower()}",
        agent_type=agent_type,
        integrity_level=integrity,
    )


def _create_resource(arc_id: int, content_type: str = "application/json") -> dict:
    return resource_backend.handle_create(
        {"content_type": content_type, "_caller_arc_id": arc_id}
    )


class TestResourceWriteDict:
    def test_dict_persists_json_and_sets_stats(self):
        arc_id = _make_arc("EXECUTOR")
        created = _create_resource(arc_id)
        rid = created["resource_id"]

        payload = {"subject": "hi", "from": "a@b.com", "n": 3}
        result = resource_backend.handle_write(
            {"resource_id": rid, "content": payload, "_caller_arc_id": arc_id}
        )

        assert result["ok"] is True
        assert result["resource_id"] == rid
        assert result["byte_size"] > 0

        # The blob is exactly what json.dump produced (sorted keys, indent).
        expected = json.dumps(payload, sort_keys=True, indent=2)
        on_disk = open(created["file_path"], encoding="utf-8").read()
        assert on_disk == expected

        # byte_size / content_hash match the bytes on disk.
        raw = expected.encode("utf-8")
        assert result["byte_size"] == len(raw)
        assert result["content_hash"] == hashlib.sha256(raw).hexdigest()

        row = res_manager.get_resource(rid)
        assert row["byte_size"] == len(raw)
        assert row["content_hash"] == hashlib.sha256(raw).hexdigest()

    def test_blob_readable_back_via_read_resource_content(self):
        arc_id = _make_arc("EXECUTOR")
        created = _create_resource(arc_id)
        rid = created["resource_id"]

        payload = {"k": "v", "list": [1, 2, 3]}
        resource_backend.handle_write(
            {"resource_id": rid, "content": payload, "_caller_arc_id": arc_id}
        )

        # An untrusted/constrained arc may read raw bytes back.
        text = read_resource_content(rid, caller_arc_id=None)
        assert json.loads(text) == payload


class TestResourceWriteStr:
    def test_str_written_verbatim(self):
        arc_id = _make_arc("EXECUTOR")
        created = _create_resource(arc_id, content_type="text/plain")
        rid = created["resource_id"]

        content = "raw email bytes\nwith newlines and unicode: café"
        result = resource_backend.handle_write(
            {"resource_id": rid, "content": content, "_caller_arc_id": arc_id}
        )

        on_disk = open(created["file_path"], encoding="utf-8").read()
        assert on_disk == content

        raw = content.encode("utf-8")
        assert result["byte_size"] == len(raw)
        assert result["content_hash"] == hashlib.sha256(raw).hexdigest()

        text = read_resource_content(rid, caller_arc_id=None)
        assert text == content


class TestResourceWriteAuth:
    def test_non_producer_caller_is_refused(self):
        producer = _make_arc("EXECUTOR")
        created = _create_resource(producer)
        rid = created["resource_id"]

        other = _make_arc("EXECUTOR")
        with pytest.raises(PermissionError, match="not the producer"):
            resource_backend.handle_write(
                {"resource_id": rid, "content": {"x": 1}, "_caller_arc_id": other}
            )


class TestResourceWriteValidation:
    def test_missing_resource_id_raises(self):
        with pytest.raises(ValueError, match="resource_id"):
            resource_backend.handle_write({"content": {"x": 1}})

    def test_missing_content_raises(self):
        arc_id = _make_arc("EXECUTOR")
        created = _create_resource(arc_id)
        with pytest.raises(ValueError, match="content"):
            resource_backend.handle_write(
                {"resource_id": created["resource_id"], "_caller_arc_id": arc_id}
            )

    def test_unknown_resource_raises(self):
        with pytest.raises(ValueError, match="not found"):
            resource_backend.handle_write(
                {"resource_id": 999999, "content": {"x": 1}}
            )


class TestResourceWriteDeprecateInputs:
    def test_deprecate_inputs_retires_consumed_resources(self):
        # An input Resource consumed by the writer arc.
        input_producer = _make_arc("EXECUTOR")
        input_res = _create_resource(input_producer)["resource_id"]

        writer = _make_arc("REVIEWER", integrity="trusted")
        res_manager.link_arc_resource(
            arc_id=writer, resource_id=input_res, role="input"
        )

        out = _create_resource(writer)["resource_id"]
        result = resource_backend.handle_write(
            {
                "resource_id": out,
                "content": {"derived": True},
                "deprecate_inputs": True,
                "_caller_arc_id": writer,
            }
        )
        assert result["deprecated_inputs"] == 1
        row = res_manager.get_resource(input_res)
        assert row["deprecated_at"] is not None


class TestResourceWriteDispatchRegistration:
    def test_registered_in_dispatch_table(self):
        from carpenter.api.callbacks import _DISPATCH

        assert "resource.write" in _DISPATCH
        assert callable(_DISPATCH["resource.write"])

    def test_executor_not_blocked_by_allowlist(self):
        """EXECUTOR has allowed_tools=None, so no whitelist gates the verb."""
        from carpenter.core.trust.types import AgentType, get_agent_capabilities

        caps = get_agent_capabilities()
        assert caps[AgentType.EXECUTOR]["allowed_tools"] is None

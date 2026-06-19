"""Regression: a TRUSTED REVIEWER arc's submit_code'd code CAN reach
``dispatch("resource.write", ...)`` and persist a blob.

This drives code through the real submit_code path (save_code ->
approved -> code_manager.execute) with the executing arc being a trusted
REVIEWER.  It documents that the infrastructure is NOT blocked for
trusted REVIEWER arcs — the historical live failure ("dispatch() is not
available / no resource.write API") was an LLM code-generation failure,
which the structured ``submit_extract`` tool (see
tests/agent/test_submit_extract.py) replaces with a no-code emit path.
"""

import json

from carpenter.core import code_manager
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.resources import manager as res_manager
from carpenter.core.resources import read_resource_content
from carpenter.tool_backends import resource as resource_backend
from carpenter.db import db_transaction


def test_trusted_reviewer_executed_code_can_dispatch_resource_write():
    """Faithful submit_code path: trusted REVIEWER arc, approved code,
    real execution session — does dispatch('resource.write') reach the
    handler and persist the blob?"""
    reviewer = arc_manager.create_arc(
        name="probe-reviewer",
        agent_type="REVIEWER",
        integrity_level="trusted",
    )
    created = resource_backend.handle_create(
        {"content_type": "EmailTriageExtract", "_caller_arc_id": reviewer}
    )
    rid = created["resource_id"]

    code = (
        'extract = {"provider_message_id": "abc12", "category": "personal", '
        '"schema_version": "1.0"}\n'
        f'dispatch("resource.write", {{"resource_id": {rid}, '
        '"content": extract})\n'
    )
    saved = code_manager.save_code(code, source="chat_agent", name="probe")
    with db_transaction() as db:
        db.execute(
            "UPDATE code_files SET review_status = 'approved' WHERE id = ?",
            (saved["code_file_id"],),
        )
    result = code_manager.execute(
        saved["code_file_id"],
        arc_id=reviewer,
        execution_context="arc-step",
    )

    assert result["execution_status"] == "success", result

    text = read_resource_content(rid, caller_arc_id=None)
    assert json.loads(text) == {
        "provider_message_id": "abc12",
        "category": "personal",
        "schema_version": "1.0",
    }
    row = res_manager.get_resource(rid)
    assert row["byte_size"] > 0
    # verdict stays pending — write does not approve
    assert row["template_verdict"] in (None, "pending")

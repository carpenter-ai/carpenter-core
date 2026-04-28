"""Integration tests for the agent-finalization verification hook.

These tests exercise the path the user described as
"whenever the agent returns, thinking that its work is done": the coding
agent writes one or more files, signals end_turn, and the registered
content-type verifiers re-run on every touched file.  If a verifier
returns ok=False the finalization is rejected — the agent is forced to
loop again with the structured findings as feedback, mirroring the
``submit_code`` rejection surface today.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from carpenter.agent import coding_agent


# A YAML template that will FAIL the yaml-template verifier — declares
# integrity_level: untrusted on the EXECUTOR but no REVIEWER + JUDGE
# downstream, no output_type: json.
_BAD_YAML = """\
name: bad-template
description: missing trust topology
steps:
  - name: fetch
    description: do the fetch
    order: 0
    agent_type: EXECUTOR
    integrity_level: untrusted
"""

# A correctly shaped template — passes the verifier.
_GOOD_YAML = """\
name: good-template
description: complete trust chain
steps:
  - name: fetch
    description: do the fetch
    order: 0
    agent_type: EXECUTOR
    integrity_level: untrusted
    output_type: json
  - name: review
    description: review the fetch
    order: 1
    agent_type: REVIEWER
    agent_role: security-reviewer
  - name: judge
    description: judge it
    order: 2
    agent_type: JUDGE
    agent_role: judge
"""


def _mock_resp(payload: dict) -> MagicMock:
    m = MagicMock()
    m.json.return_value = payload
    m.raise_for_status = MagicMock()
    m.headers = {}
    return m


def test_finalization_rejected_when_yaml_template_invalid(tmp_path):
    """Agent writes a malformed template, then signals end_turn.

    The verifier must reject the finalization and the agent must be
    given a follow-up user message containing the findings.  The next
    response (end_turn with no further writes) is then accepted because
    the bad file is still on disk but ``MAX_VERIFY_REJECTS`` budgets
    eventually allow the run to exit.
    """
    ws = str(tmp_path)
    rel_path = "config_seed/templates/bad.yaml"

    write_response = {
        "content": [
            {"type": "tool_use", "id": "t1", "name": "write_file",
             "input": {"path": rel_path, "content": _BAD_YAML}},
        ],
        "stop_reason": "tool_use",
    }
    end_response = {
        "content": [{"type": "text", "text": "All done."}],
        "stop_reason": "end_turn",
    }

    # Sequence: write_file → end_turn (rejected) → end_turn (rejected) →
    # end_turn (rejected, budget exhausted, loop breaks).
    responses = [write_response] + [end_response] * 5

    with patch(
        "httpx.post",
        side_effect=[_mock_resp(r) for r in responses],
    ):
        result = coding_agent.run(
            ws, "Write a template",
            {
                "type": "builtin", "model": "test-model",
                "max_tokens": 200, "max_iterations": 10,
            },
        )

    # The verifier rejected at least once: the agent should have gone
    # through more than 2 iterations (write + first end_turn).
    assert result["iterations"] >= 3
    assert result["exit_code"] == 0

    # The bad file is still on disk (verifier is read-only).
    assert os.path.exists(os.path.join(ws, rel_path))


def test_finalization_accepted_when_yaml_template_valid(tmp_path):
    """A well-formed template passes the verifier on the first try."""
    ws = str(tmp_path)
    rel_path = "config_seed/templates/good.yaml"

    write_response = {
        "content": [
            {"type": "tool_use", "id": "t1", "name": "write_file",
             "input": {"path": rel_path, "content": _GOOD_YAML}},
        ],
        "stop_reason": "tool_use",
    }
    end_response = {
        "content": [{"type": "text", "text": "All done."}],
        "stop_reason": "end_turn",
    }

    with patch(
        "httpx.post",
        side_effect=[_mock_resp(write_response), _mock_resp(end_response)],
    ):
        result = coding_agent.run(
            ws, "Write a template",
            {
                "type": "builtin", "model": "test-model",
                "max_tokens": 200, "max_iterations": 5,
            },
        )

    assert result["exit_code"] == 0
    # Exactly 2 iterations — write + end_turn accepted on first try.
    assert result["iterations"] == 2


def test_finalization_unaffected_for_files_without_registered_verifier(
    tmp_path,
):
    """Editing a random Python file does not trigger any verifier."""
    ws = str(tmp_path)

    write_response = {
        "content": [
            {"type": "tool_use", "id": "t1", "name": "write_file",
             "input": {"path": "scratch/util.py",
                       "content": "x = 1\n"}},
        ],
        "stop_reason": "tool_use",
    }
    end_response = {
        "content": [{"type": "text", "text": "Done."}],
        "stop_reason": "end_turn",
    }

    with patch(
        "httpx.post",
        side_effect=[_mock_resp(write_response), _mock_resp(end_response)],
    ):
        result = coding_agent.run(
            ws, "Write a util",
            {
                "type": "builtin", "model": "test-model",
                "max_tokens": 200, "max_iterations": 5,
            },
        )

    assert result["exit_code"] == 0
    assert result["iterations"] == 2


def test_verify_touched_files_returns_findings(tmp_path):
    """Direct unit test of the helper used inside run()."""
    ws = str(tmp_path)
    rel = "config_seed/templates/x.yaml"
    abs_path = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w") as f:
        f.write(_BAD_YAML)

    feedback = coding_agent._verify_touched_files(ws, {rel})
    assert len(feedback) == 1
    assert feedback[0]["path"] == rel
    assert feedback[0]["findings"]
    assert any(
        fnd["severity"] == "error" for fnd in feedback[0]["findings"]
    )


def test_verify_touched_files_skips_unregistered_paths(tmp_path):
    """Paths that don't match any content type are silently skipped."""
    ws = str(tmp_path)
    abs_path = os.path.join(ws, "scratch.py")
    with open(abs_path, "w") as f:
        f.write("x = 1\n")

    feedback = coding_agent._verify_touched_files(ws, {"scratch.py"})
    assert feedback == []


def test_format_verification_feedback_includes_line_and_fix():
    """Feedback rendering surfaces line, severity, and fix hint."""
    feedback = [
        {
            "path": "config_seed/templates/x.yaml",
            "findings": [
                {
                    "severity": "error",
                    "line": 7,
                    "message": "missing required field 'name'",
                    "fix_hint": "Add a name (see KB: workflows/template-schema).",
                },
            ],
        },
    ]
    rendered = coding_agent._format_verification_feedback(feedback)
    assert "REJECTED" in rendered
    assert "config_seed/templates/x.yaml" in rendered
    assert "line 7" in rendered
    assert "missing required field" in rendered
    assert "Add a name" in rendered

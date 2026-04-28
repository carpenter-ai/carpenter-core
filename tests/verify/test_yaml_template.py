"""Tests for the YAML workflow template verifier."""

from __future__ import annotations

import textwrap

from carpenter.verify.yaml_template import verify_yaml_template


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msgs(result) -> list[str]:
    return [f.message for f in result.findings]


def _errors(result) -> list:
    return [f for f in result.findings if f.severity == "error"]


def _warnings(result) -> list:
    return [f for f in result.findings if f.severity == "warning"]


# ---------------------------------------------------------------------------
# Schema-level
# ---------------------------------------------------------------------------

def test_well_formed_template_accepts():
    yaml_text = textwrap.dedent("""
        name: simple
        description: A simple workflow
        steps:
          - name: do-it
            description: do the thing
            order: 0
            agent_type: EXECUTOR
    """).strip()
    result = verify_yaml_template(yaml_text)
    assert result.ok is True, _msgs(result)
    assert _errors(result) == []


def test_invalid_yaml_syntax_reports_parse_error():
    yaml_text = "name: foo\nsteps: [oops"
    result = verify_yaml_template(yaml_text)
    assert result.ok is False
    assert any("YAML parse error" in f.message for f in result.findings)


def test_missing_top_level_keys():
    yaml_text = "name: only-name\n"
    result = verify_yaml_template(yaml_text)
    assert result.ok is False
    msgs = _msgs(result)
    assert any("description" in m for m in msgs)
    assert any("steps" in m for m in msgs)


def test_steps_must_be_list():
    yaml_text = textwrap.dedent("""
        name: foo
        description: bar
        steps:
          one: not-a-list
    """).strip()
    result = verify_yaml_template(yaml_text)
    assert result.ok is False
    assert any("'steps' must be a list" in m for m in _msgs(result))


def test_step_missing_name():
    yaml_text = textwrap.dedent("""
        name: foo
        description: bar
        steps:
          - description: nameless
            order: 0
    """).strip()
    result = verify_yaml_template(yaml_text)
    assert result.ok is False
    assert any("missing required field 'name'" in m for m in _msgs(result))


def test_unknown_top_level_key_is_warning():
    yaml_text = textwrap.dedent("""
        name: foo
        description: bar
        steps: []
        nonsense_key: value
    """).strip()
    result = verify_yaml_template(yaml_text)
    # Warning, but ok stays True (no error-severity findings on schema).
    assert any(
        f.severity == "warning" and "nonsense_key" in f.message
        for f in result.findings
    )


def test_unknown_step_field_is_warning():
    yaml_text = textwrap.dedent("""
        name: foo
        description: bar
        steps:
          - name: x
            order: 0
            wibble: 7
    """).strip()
    result = verify_yaml_template(yaml_text)
    assert any(
        f.severity == "warning" and "wibble" in f.message
        for f in result.findings
    )


def test_invalid_agent_type_rejects():
    yaml_text = textwrap.dedent("""
        name: foo
        description: bar
        steps:
          - name: x
            order: 0
            agent_type: WIZARD
    """).strip()
    result = verify_yaml_template(yaml_text)
    assert result.ok is False
    assert any("invalid agent_type" in m for m in _msgs(result))


def test_invalid_integrity_level_rejects():
    yaml_text = textwrap.dedent("""
        name: foo
        description: bar
        steps:
          - name: x
            order: 0
            integrity_level: rusty
    """).strip()
    result = verify_yaml_template(yaml_text)
    assert result.ok is False
    assert any("invalid integrity_level" in m for m in _msgs(result))


# ---------------------------------------------------------------------------
# Trust topology
# ---------------------------------------------------------------------------

def _untrusted_template(*extra_lines: str) -> str:
    body = textwrap.dedent("""
        name: untrusted-flow
        description: Demo
        steps:
          - name: fetch
            description: do the fetch
            order: 0
            agent_type: EXECUTOR
            integrity_level: untrusted
            output_type: json
    """).strip()
    if extra_lines:
        body += "\n" + "\n".join(extra_lines)
    return body


def test_untrusted_executor_without_reviewer_rejects():
    yaml_text = _untrusted_template()
    result = verify_yaml_template(yaml_text)
    assert result.ok is False
    msgs = _msgs(result)
    assert any("no downstream REVIEWER" in m for m in msgs)
    assert any("no downstream JUDGE" in m for m in msgs)


def test_untrusted_executor_full_chain_passes():
    yaml_text = textwrap.dedent("""
        name: untrusted-flow
        description: Demo
        steps:
          - name: fetch
            description: fetch
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
    """).strip()
    result = verify_yaml_template(yaml_text)
    assert result.ok is True, _msgs(result)


def test_untrusted_executor_missing_output_type_rejects():
    yaml_text = textwrap.dedent("""
        name: untrusted-flow
        description: Demo
        steps:
          - name: fetch
            description: fetch
            order: 0
            agent_type: EXECUTOR
            integrity_level: untrusted
          - name: review
            description: review
            order: 1
            agent_type: REVIEWER
            agent_role: security-reviewer
          - name: judge
            description: judge
            order: 2
            agent_type: JUDGE
            agent_role: judge
    """).strip()
    result = verify_yaml_template(yaml_text)
    assert result.ok is False
    assert any("output_type: json" in m for m in _msgs(result))


def test_judge_cannot_be_untrusted():
    yaml_text = textwrap.dedent("""
        name: t
        description: d
        steps:
          - name: j
            description: x
            order: 0
            agent_type: JUDGE
            integrity_level: untrusted
            agent_role: judge
    """).strip()
    result = verify_yaml_template(yaml_text)
    assert result.ok is False
    assert any("JUDGE arcs must be trusted" in m for m in _msgs(result))


def test_reviewer_cannot_be_untrusted():
    yaml_text = textwrap.dedent("""
        name: t
        description: d
        steps:
          - name: r
            description: x
            order: 0
            agent_type: REVIEWER
            integrity_level: untrusted
            agent_role: security-reviewer
    """).strip()
    result = verify_yaml_template(yaml_text)
    assert result.ok is False
    assert any("REVIEWER arcs must be trusted" in m for m in _msgs(result))


def test_unknown_reviewer_profile_rejects():
    yaml_text = textwrap.dedent("""
        name: untrusted-flow
        description: Demo
        steps:
          - name: fetch
            description: fetch
            order: 0
            agent_type: EXECUTOR
            integrity_level: untrusted
            output_type: json
          - name: review
            description: review
            order: 1
            agent_type: REVIEWER
            agent_role: random-name
          - name: judge
            description: judge
            order: 2
            agent_type: JUDGE
            agent_role: judge
    """).strip()
    result = verify_yaml_template(yaml_text)
    assert result.ok is False
    assert any("not a registered reviewer" in m for m in _msgs(result))


def test_unknown_judge_profile_rejects():
    yaml_text = textwrap.dedent("""
        name: untrusted-flow
        description: Demo
        steps:
          - name: fetch
            description: fetch
            order: 0
            agent_type: EXECUTOR
            integrity_level: untrusted
            output_type: json
          - name: review
            description: review
            order: 1
            agent_type: REVIEWER
            agent_role: security-reviewer
          - name: judge
            description: judge
            order: 2
            agent_type: JUDGE
            agent_role: not-the-judge
    """).strip()
    result = verify_yaml_template(yaml_text)
    assert result.ok is False
    assert any(
        "not the registered 'judge' role" in m for m in _msgs(result)
    )


def test_judge_must_have_highest_order():
    yaml_text = textwrap.dedent("""
        name: untrusted-flow
        description: Demo
        steps:
          - name: fetch
            description: fetch
            order: 0
            agent_type: EXECUTOR
            integrity_level: untrusted
            output_type: json
          - name: judge
            description: judge
            order: 1
            agent_type: JUDGE
            agent_role: judge
          - name: review
            description: review
            order: 2
            agent_type: REVIEWER
            agent_role: security-reviewer
    """).strip()
    result = verify_yaml_template(yaml_text)
    assert result.ok is False
    assert any(
        "highest 'order'" in m or "highest order" in m
        for m in _msgs(result)
    )


# ---------------------------------------------------------------------------
# Goal placeholder safety
# ---------------------------------------------------------------------------

def test_placeholder_inside_fenced_code_warns():
    yaml_text = textwrap.dedent("""
        name: t
        description: d
        steps:
          - name: x
            order: 0
            agent_type: EXECUTOR
            description: |
              Run this code:
              ```
              dispatch($goal)
              ```
    """).strip()
    result = verify_yaml_template(yaml_text)
    warns = _warnings(result)
    assert any("inside a fenced code block" in w.message for w in warns)


def test_placeholder_outside_fenced_code_does_not_warn():
    yaml_text = textwrap.dedent("""
        name: t
        description: d
        steps:
          - name: x
            order: 0
            agent_type: EXECUTOR
            description: 'Extract: $goal'
    """).strip()
    result = verify_yaml_template(yaml_text)
    warns = [
        w for w in _warnings(result)
        if "fenced code block" in w.message
    ]
    assert warns == []


# ---------------------------------------------------------------------------
# Line-number quality
# ---------------------------------------------------------------------------

def test_findings_include_line_numbers():
    yaml_text = textwrap.dedent("""
        name: t
        description: d
        steps:
          - name: bad
            order: 0
            agent_type: WIZARD
    """).strip()
    result = verify_yaml_template(yaml_text)
    bad = [f for f in result.findings if "agent_type" in f.message]
    assert bad, _msgs(result)
    # The agent_type field is on line 6 (1-indexed) of the stripped doc.
    assert bad[0].line is not None and bad[0].line > 1


def test_findings_carry_fix_hints():
    yaml_text = textwrap.dedent("""
        name: t
        description: d
        steps:
          - name: bad
            order: 0
            agent_type: WIZARD
    """).strip()
    result = verify_yaml_template(yaml_text)
    for f in result.findings:
        # Every error finding must point at a KB article.
        if f.severity == "error":
            assert f.fix_hint, f.message

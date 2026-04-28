"""Tests for the python-arc-step verifier (thin wrapper)."""

from __future__ import annotations

from unittest.mock import patch

from carpenter.verify import VerificationResult as InnerResult
from carpenter.verify.python_arc_step import verify_python_arc_step


def test_passing_inner_result_yields_passing_outer():
    inner = InnerResult(
        verified=True, hard_reject=False, reason="ok",
        violations=[], code_hash="h", policy_version=1,
    )
    with patch(
        "carpenter.verify.verify_code", return_value=inner,
    ):
        result = verify_python_arc_step("code", None)
    assert result.ok is True
    assert result.findings == []


def test_hard_reject_produces_error_findings():
    inner = InnerResult(
        verified=False, hard_reject=True,
        reason="Untyped string literals found",
        violations=[
            "Line 4: untyped string literal 'foo' "
            "— wrap in Label(), URL(), etc.",
        ],
        code_hash="h",
    )
    with patch(
        "carpenter.verify.verify_code", return_value=inner,
    ):
        result = verify_python_arc_step("code", None)
    assert result.ok is False
    assert len(result.findings) == 1
    fnd = result.findings[0]
    assert fnd.severity == "error"
    assert fnd.line == 4
    assert "untyped string literal" in fnd.message


def test_non_hard_reject_produces_warning_findings():
    inner = InnerResult(
        verified=False, hard_reject=False,
        reason="Code uses constructs outside the verifiable subset",
        violations=["Some construct"],
        code_hash="h",
    )
    with patch(
        "carpenter.verify.verify_code", return_value=inner,
    ):
        result = verify_python_arc_step("code", None)
    # Warnings only — ok stays True.
    assert result.ok is True
    assert all(f.severity == "warning" for f in result.findings)


def test_no_violations_still_emits_one_finding():
    inner = InnerResult(
        verified=False, hard_reject=True,
        reason="Syntax error: …",
        violations=[],
        code_hash="h",
    )
    with patch(
        "carpenter.verify.verify_code", return_value=inner,
    ):
        result = verify_python_arc_step("code", None)
    assert result.ok is False
    assert len(result.findings) == 1
    assert result.findings[0].line is None


def test_arc_id_forwarded_via_context():
    inner = InnerResult(
        verified=True, hard_reject=False, reason="ok", violations=[],
        code_hash="h", policy_version=1,
    )
    with patch(
        "carpenter.verify.verify_code", return_value=inner,
    ) as mock_verify:
        verify_python_arc_step("code", {"arc_id": 42})
    mock_verify.assert_called_once()
    _, kwargs = mock_verify.call_args
    assert kwargs.get("arc_id") == 42

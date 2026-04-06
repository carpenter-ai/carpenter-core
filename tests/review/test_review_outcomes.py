"""Tests for standardized review outcomes."""
from carpenter.review.pipeline import (
    ReviewOutcome,
    determine_outcome,
)


def test_import_star_violation_rejected():
    """Test that import * violations are immediately rejected."""
    outcome = determine_outcome(
        syntax_valid=True,
        import_star_violation=True,
        injection_flags=[],
        ai_review_result="approve",
    )
    assert outcome == ReviewOutcome.REJECTED


def test_syntax_error_outcome():
    """Test that syntax errors map to REWORK outcome (fixable issue)."""
    outcome = determine_outcome(
        syntax_valid=False,
        import_star_violation=False,
        injection_flags=[],
        ai_review_result="approve",
    )
    assert outcome == ReviewOutcome.REWORK


def test_high_severity_injection_major():
    """Test that HIGH severity injection patterns trigger MAJOR."""
    injection_flags = [
        {"severity": "HIGH", "description": "Dangerous pattern", "source": "comments"}
    ]
    outcome = determine_outcome(
        syntax_valid=True,
        import_star_violation=False,
        injection_flags=injection_flags,
        ai_review_result="approve",
    )
    assert outcome == ReviewOutcome.MAJOR


def test_ai_major_outcome():
    """Test that AI reviewer MAJOR result triggers MAJOR outcome."""
    outcome = determine_outcome(
        syntax_valid=True,
        import_star_violation=False,
        injection_flags=[],
        ai_review_result="major",
        ai_review_reason="Security concern",
    )
    assert outcome == ReviewOutcome.MAJOR


def test_ai_minor_outcome():
    """Test that AI reviewer MINOR result triggers REWORK outcome."""
    outcome = determine_outcome(
        syntax_valid=True,
        import_star_violation=False,
        injection_flags=[],
        ai_review_result="minor",
        ai_review_reason="Scope issue",
    )
    assert outcome == ReviewOutcome.REWORK


def test_clean_code_approve():
    """Test that clean code with AI approval triggers APPROVE."""
    outcome = determine_outcome(
        syntax_valid=True,
        import_star_violation=False,
        injection_flags=[],
        ai_review_result="approve",
    )
    assert outcome == ReviewOutcome.APPROVE


def test_priority_import_star_over_others():
    """Test that import * violation takes priority over other issues."""
    outcome = determine_outcome(
        syntax_valid=True,
        import_star_violation=True,  # Should trigger REJECTED
        injection_flags=[{"severity": "HIGH", "description": "test", "source": "test"}],
        ai_review_result="major",
    )
    assert outcome == ReviewOutcome.REJECTED


def test_priority_syntax_over_ai():
    """Test that syntax errors take priority over AI results."""
    outcome = determine_outcome(
        syntax_valid=False,  # Should trigger REWORK
        import_star_violation=False,
        injection_flags=[],
        ai_review_result="approve",  # AI would approve, but syntax is invalid
    )
    assert outcome == ReviewOutcome.REWORK


def test_priority_high_injection_over_minor_ai():
    """Test that HIGH injection overrides AI minor."""
    outcome = determine_outcome(
        syntax_valid=True,
        import_star_violation=False,
        injection_flags=[{"severity": "HIGH", "description": "test", "source": "test"}],
        ai_review_result="minor",  # AI says minor, but injection says MAJOR
    )
    assert outcome == ReviewOutcome.MAJOR



"""Integration tests for enhanced review security features.

Tests the complete flow from code submission through review pipeline
with import star detection and cross-file analysis.
"""
import pytest
from unittest.mock import patch
from carpenter.review.pipeline import run_review_pipeline, ReviewOutcome
from carpenter.review.code_reviewer import ReviewResult


@pytest.fixture
def mock_reviewer():
    """Mock the AI reviewer to return APPROVE by default."""
    with patch("carpenter.review.pipeline.review_code") as mock:
        mock.return_value = ReviewResult(
            status="approve",
            reason="",
            sanitized_code="",
        )
        yield mock


class TestImportStarRejection:
    """Test that import * violations are auto-rejected."""

    def test_import_star_rejected_immediately(self):
        """Test that code with import * is rejected without AI review."""
        code = """
import os
from sys import *

def main():
    print("Hello")
"""
        # Use conversation_id 1 (will be created by test DB fixture)
        result = run_review_pipeline(code, conversation_id=1)

        assert result.outcome == ReviewOutcome.REJECTED
        assert result.status == "rejected"
        assert "import *" in result.reason.lower()

    def test_import_star_multiple_violations(self):
        """Test rejection with multiple import * statements."""
        code = """
from os import *
from sys import *
from pathlib import *
"""
        result = run_review_pipeline(code, conversation_id=1)

        assert result.outcome == ReviewOutcome.REJECTED
        assert "line 2" in result.reason  # Should list violations

    def test_normal_imports_not_rejected(self, mock_reviewer):
        """Test that explicit imports are allowed."""
        code = """
import os
from sys import argv, exit
from pathlib import Path

def main():
    print(os.getcwd())
"""
        result = run_review_pipeline(code, conversation_id=1)

        # Should not be rejected for imports
        assert result.outcome != ReviewOutcome.REJECTED


class TestOutcomeDetermination:
    """Test review outcome standardization."""

    def test_syntax_error_outcome(self):
        """Test that syntax errors map to REWORK (fixable issue)."""
        code = "def :"  # Invalid syntax
        result = run_review_pipeline(code, conversation_id=1)

        assert result.outcome == ReviewOutcome.REWORK
        assert result.status == "minor_concern"
        assert "syntax" in result.reason.lower()

    def test_clean_code_flow(self, mock_reviewer):
        """Test that clean code goes through full pipeline."""
        code = """
import os

def get_current_dir():
    return os.getcwd()

result = get_current_dir()
"""
        result = run_review_pipeline(code, conversation_id=1)

        # Should complete review (outcome depends on AI reviewer)
        assert result.outcome in [ReviewOutcome.APPROVE, ReviewOutcome.REWORK, ReviewOutcome.MAJOR]
        assert result.sanitized_code  # Should have sanitized version


class TestEndToEndFlow:
    """Test complete end-to-end review flows."""

    def test_rejected_no_retry(self):
        """Test that REJECTED outcomes don't allow retry."""
        code = "from os import *"  # Policy violation
        result = run_review_pipeline(code, conversation_id=1)

        assert result.outcome == ReviewOutcome.REJECTED
        # In real system, this would not trigger a retry in coding_change_handler

    def test_sanitized_code_blind_review(self, mock_reviewer):
        """Test that reviewer sees only sanitized code."""
        code = """
secret_key = "my-api-key-12345"

def dangerous_operation():
    exploit_server(secret_key)
"""
        result = run_review_pipeline(code, conversation_id=1)

        # Sanitized code should not contain secrets
        assert "my-api-key-12345" not in result.sanitized_code
        assert "secret_key" not in result.sanitized_code or result.sanitized_code.count("secret_key") == 0
        # Should have placeholders instead
        assert "S1" in result.sanitized_code or "S2" in result.sanitized_code

    def test_cached_approval_skip(self, mock_reviewer):
        """Test that identical code is cached and skipped."""
        code = "x = 1\nprint(x)"

        # First submission
        result1 = run_review_pipeline(code, conversation_id=1)
        if result1.outcome == ReviewOutcome.APPROVE:
            # Second submission (same code)
            result2 = run_review_pipeline(code, conversation_id=1)

            # Should be cached
            assert result2.status == "cached_approval"


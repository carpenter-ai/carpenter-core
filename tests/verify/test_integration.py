"""Integration tests for verify_code() orchestrator."""

import pytest
from unittest.mock import patch

from carpenter.verify import verify_code, VerificationResult
from carpenter.verify.hash_store import compute_code_hash, add_verified_hash


class TestVerifyCodeAllTrusted:
    """Code with only trusted data paths."""

    def test_simple_arc_code_verified(self):
        code = """
from carpenter_tools.act import arc
from carpenter_tools.declarations import Label, UnstructuredText
arc.create(name=Label("test"), goal=UnstructuredText("do-thing"))
"""
        result = verify_code(code)
        assert isinstance(result, VerificationResult)
        assert result.verified
        assert not result.hard_reject

    def test_messaging_code_verified(self):
        code = """
from carpenter_tools.act import messaging
from carpenter_tools.declarations import UnstructuredText
messaging.send(message=UnstructuredText("hello world"))
"""
        result = verify_code(code)
        assert result.verified

    def test_verified_code_cached(self):
        """Second call should use hash cache."""
        code = """
from carpenter_tools.act import arc
from carpenter_tools.declarations import Label, UnstructuredText
arc.create(name=Label("cached"), goal=UnstructuredText("test-caching"))
"""
        r1 = verify_code(code)
        assert r1.verified
        r2 = verify_code(code)
        assert r2.verified
        assert "hash" in r2.reason.lower() or "previously" in r2.reason.lower()


class TestVerifyCodeWhitelistFails:
    """Code using non-whitelisted constructs."""

    def test_while_loop_not_verifiable(self):
        code = "while True:\n    pass"
        result = verify_code(code)
        assert not result.verified
        assert not result.hard_reject  # Not verifiable, not a hard reject

    def test_function_def_not_verifiable(self):
        code = "def foo():\n    return 1"
        result = verify_code(code)
        assert not result.verified
        assert not result.hard_reject

    def test_stdlib_import_not_verifiable(self):
        code = "from os import path"
        result = verify_code(code)
        assert not result.verified
        assert not result.hard_reject


class TestVerifyCodeHardReject:
    """Code that tries to use C data in conditions without policy type."""

    def test_bare_literal_comparison_hard_reject(self):
        """Untyped string literals cause hard reject."""
        code = """
x = state.get('email', arc_id=10)
if x == "plain_string":
    y = 1
"""
        result = verify_code(code, arc_id=1)

        assert not result.verified
        assert result.hard_reject
        assert "Untyped string" in result.reason


class TestVerifyCodeToolArgTypes:
    """Code with wrong SecurityType for tool arguments."""

    def test_correctly_typed_tool_args_pass(self):
        """Correctly typed tool args pass verify_code."""
        code = """
from carpenter_tools.act import arc
from carpenter_tools.declarations import Label, UnstructuredText
arc.create(name=Label("test"), goal=UnstructuredText("do thing"))
"""
        result = verify_code(code)
        assert result.verified

    def test_wrongly_typed_tool_args_hard_reject(self):
        """Using wrong SecurityType for tool args causes hard reject."""
        code = """
from carpenter_tools.act import arc
from carpenter_tools.declarations import URL
arc.create(name=URL("https://example.com"))
"""
        result = verify_code(code)
        assert not result.verified
        assert result.hard_reject
        assert "wrong SecurityType" in result.reason


class TestVerifyCodeSyntaxError:
    def test_syntax_error(self):
        result = verify_code("def ")
        assert not result.verified
        assert not result.hard_reject  # Whitelist catches syntax errors first


class TestVerifyCodeDisabled:
    """When verification is disabled in config."""

    def test_disabled_skips_verification(self, monkeypatch):
        monkeypatch.setattr(
            "carpenter.config.CONFIG",
            {**__import__("carpenter.config", fromlist=["CONFIG"]).CONFIG,
             "verification": {"enabled": False}},
        )
        # When disabled, verify_code should still work but the pipeline
        # won't call it. This tests the function directly.
        code = "from carpenter_tools.act import arc\nfrom carpenter_tools.declarations import Label, UnstructuredText\narc.create(name=Label('test'), goal=UnstructuredText('goal'))"
        result = verify_code(code)
        assert result.verified  # Still verifies when called directly


class TestPipelineIntegration:
    """Test that the pipeline uses verification correctly."""

    def test_verified_code_in_pipeline(self, monkeypatch):
        """Verified code gets auto-approved regardless of LLM."""
        from carpenter.review.pipeline import run_review_pipeline, ReviewOutcome, clear_cache
        from carpenter.db import get_db

        clear_cache()

        # Create a conversation
        db = get_db()
        try:
            db.execute(
                "INSERT INTO conversations (id, title) VALUES (1, 'test')"
            )
            db.commit()
        finally:
            db.close()

        # Mock the LLM reviewer to return MAJOR (shouldn't matter for verified code)
        from carpenter.review.code_reviewer import ReviewResult
        mock_review = ReviewResult(
            status="major",
            reason="LLM found major issues",
            sanitized_code="",
        )
        monkeypatch.setattr(
            "carpenter.review.pipeline.review_code",
            lambda *a, **kw: mock_review,
        )
        # Step 4c: progressive text review needs a model — patch it out so this
        # test stays focused on the verification auto-approval behaviour.
        monkeypatch.setattr(
            "carpenter.review.pipeline.run_progressive_text_review",
            lambda texts: (False, []),
        )

        code = """
from carpenter_tools.act import arc
from carpenter_tools.declarations import Label, UnstructuredText
arc.create(name=Label("test"), goal=UnstructuredText("do-thing"))
"""
        result = run_review_pipeline(code, 1)
        # Verified code is auto-approved even though LLM says MAJOR
        assert result.outcome == ReviewOutcome.APPROVE
        assert result.status == "approved"

    def test_non_verifiable_code_forces_major(self, monkeypatch):
        """Non-verifiable code (while loop) forces MAJOR even if LLM approves."""
        from carpenter.review.pipeline import run_review_pipeline, ReviewOutcome, clear_cache
        from carpenter.db import get_db

        clear_cache()

        db = get_db()
        try:
            db.execute(
                "INSERT OR IGNORE INTO conversations (id, title) VALUES (2, 'test2')"
            )
            db.commit()
        finally:
            db.close()

        from carpenter.review.code_reviewer import ReviewResult
        mock_review = ReviewResult(
            status="approve",
            reason="",
            sanitized_code="",
        )
        monkeypatch.setattr(
            "carpenter.review.pipeline.review_code",
            lambda *a, **kw: mock_review,
        )
        # Step 4c: progressive text review needs a model — patch it out so this
        # test stays focused on the non-verifiable-code-forces-MAJOR behaviour.
        monkeypatch.setattr(
            "carpenter.review.pipeline.run_progressive_text_review",
            lambda texts: (False, []),
        )

        code = """
from carpenter_tools.act import arc
from carpenter_tools.declarations import Label, UnstructuredText
while True:
    arc.create(name=Label("loop"), goal=UnstructuredText("bad"))
"""
        result = run_review_pipeline(code, 2)
        assert result.outcome == ReviewOutcome.MAJOR
        assert result.status == "major_alert"

"""Tests for carpenter.agent.templates."""

import os
import pytest

from carpenter.agent import templates


def test_render_chat_new():
    """chat_new template renders with system prompt and active work."""
    result = templates.render(
        "chat_new",
        system_prompt="You are helpful.",
    )
    assert "You are helpful." in result
    assert "Active Work" in result
    # new_message should NOT be in system prompt (it's in the messages array)
    assert "New Message" not in result


def test_render_chat_compacted():
    """chat_compacted template renders with prior context."""
    result = templates.render(
        "chat_compacted",
        system_prompt="System",
        prior_context_tail="Last 10 lines...",
    )
    assert "Prior Context" in result
    assert "Last 10 lines..." in result
    # new_message should NOT be in system prompt
    assert "New Message" not in result


def test_render_step():
    """step template renders with arc context."""
    result = templates.render(
        "step",
        arc_context="Arc #42: Search for data",
        step_details="Step 1: Query API",
    )
    assert "Arc #42" in result
    assert "Step 1" in result
    assert "carpenter_tools" in result


def test_render_review():
    """review template renders with code."""
    result = templates.render(
        "review",
        code_under_review="print('hi')",
        review_criteria="Check for security issues",
    )
    assert "print('hi')" in result
    assert "security issues" in result


def test_render_with_defaults():
    """Templates render fine with missing variables (defaults used)."""
    result = templates.render("chat_new")
    assert "Active Work" in result


def test_get_template_unknown():
    """Unknown template name raises ValueError."""
    with pytest.raises(ValueError, match="Unknown template"):
        templates.get_template("nonexistent_template")


def test_get_template_from_user_dir(tmp_path, monkeypatch):
    """Templates can be loaded from user override directory."""
    # Create a custom template file in a user-override directory
    user_dir = tmp_path / "user_prompts"
    user_dir.mkdir()
    (user_dir / "custom.md").write_text("Hello {{ name }}!")

    # Point the user template dir to our temp dir
    monkeypatch.setattr(
        "carpenter.agent.templates._get_user_template_dir",
        lambda: str(user_dir),
    )

    result = templates.render("custom", name="World")
    assert result == "Hello World!"


def test_user_override_takes_precedence(tmp_path, monkeypatch):
    """User template overrides built-in when both exist."""
    user_dir = tmp_path / "user_prompts"
    user_dir.mkdir()
    (user_dir / "chat_new.md").write_text("CUSTOM: {{ system_prompt }}")

    monkeypatch.setattr(
        "carpenter.agent.templates._get_user_template_dir",
        lambda: str(user_dir),
    )

    result = templates.render("chat_new", system_prompt="test")
    assert result == "CUSTOM: test"
    # Should NOT contain the built-in "Active Work" heading
    assert "Active Work" not in result


def test_builtin_used_when_no_user_override(tmp_path, monkeypatch):
    """Built-in template is used when user dir exists but has no override."""
    user_dir = tmp_path / "empty_user_prompts"
    user_dir.mkdir()

    monkeypatch.setattr(
        "carpenter.agent.templates._get_user_template_dir",
        lambda: str(user_dir),
    )

    # Should still load the built-in chat_new template
    result = templates.render("chat_new", system_prompt="SP")
    assert "SP" in result
    assert "Active Work" in result

"""Tests for the content-type-keyed verifier registry."""

from __future__ import annotations

import pytest

from carpenter.verify import registry as registry_mod
from carpenter.verify.registry import (
    VerificationFinding,
    VerificationResult,
    detect_content_type,
    list_content_types,
    register_verifier,
    unregister_verifier,
    verify,
)


def test_unknown_content_type_passes():
    """A content type with no registered verifier returns ok=True."""
    result = verify("never-registered-12345", "anything")
    assert result.ok is True
    assert result.findings == []


def test_register_and_dispatch():
    """A registered verifier is invoked for its content type."""

    calls: list[tuple[str, dict | None]] = []

    def fake(content: str, context):
        calls.append((content, context))
        return VerificationResult.passing()

    register_verifier("test-fake-type", fake)
    try:
        result = verify("test-fake-type", "hello", context={"k": "v"})
        assert result.ok is True
        assert calls == [("hello", {"k": "v"})]
    finally:
        unregister_verifier("test-fake-type")


def test_findings_make_result_not_ok():
    """An error-severity finding flips ok=False."""

    def reject(content: str, context):
        return VerificationResult.from_findings([
            VerificationFinding(
                severity="error",
                line=3,
                message="bad",
                fix_hint="fix it",
            )
        ])

    register_verifier("test-reject-type", reject)
    try:
        result = verify("test-reject-type", "anything")
        assert result.ok is False
        assert len(result.findings) == 1
        assert result.findings[0].severity == "error"
    finally:
        unregister_verifier("test-reject-type")


def test_warning_findings_keep_ok_true():
    """Warning-only findings still produce ok=True."""

    def warn(content: str, context):
        return VerificationResult.from_findings([
            VerificationFinding(
                severity="warning",
                line=1,
                message="meh",
                fix_hint="consider it",
            )
        ])

    register_verifier("test-warn-type", warn)
    try:
        result = verify("test-warn-type", "anything")
        assert result.ok is True
        assert len(result.findings) == 1
    finally:
        unregister_verifier("test-warn-type")


def test_default_verifiers_registered():
    """yaml-template and python-arc-step are pre-registered."""
    types = list_content_types()
    assert "yaml-template" in types
    assert "python-arc-step" in types


def test_re_register_overwrites():
    """Re-registering replaces the prior verifier."""
    seen: list[str] = []

    def v1(content, context):
        seen.append("v1")
        return VerificationResult.passing()

    def v2(content, context):
        seen.append("v2")
        return VerificationResult.passing()

    register_verifier("test-overwrite-type", v1)
    register_verifier("test-overwrite-type", v2)
    try:
        verify("test-overwrite-type", "x")
        assert seen == ["v2"]
    finally:
        unregister_verifier("test-overwrite-type")


def test_detect_content_type_yaml_template():
    """Templates under config_seed/templates/*.yaml resolve to yaml-template."""
    assert detect_content_type(
        "config_seed/templates/coding-change.yaml"
    ) == "yaml-template"
    assert detect_content_type(
        "/abs/path/config_seed/templates/foo.yml"
    ) == "yaml-template"
    assert detect_content_type(
        "carpenter/foo.py"
    ) is None
    assert detect_content_type(
        "config_seed/prompts/foo.md"
    ) is None


def test_detect_content_type_handles_windows_separators():
    """Backslash separators resolve identically to forward slashes."""
    assert detect_content_type(
        r"config_seed\templates\coding-change.yaml"
    ) == "yaml-template"

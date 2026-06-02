"""Coverage-gap tests for :func:`carpenter.core.arcs.verification.create_verification_arcs`.

These tests close gaps identified by the post-PR-#25 coverage audit:

- B10: ``source_category`` is propagated from the implementation arc to
  quality / correctness / docs children (but NOT judge).
- B12: ``template_id`` is propagated to the swapped Python-only verifier
  arc on yaml-change / kb-change.
- B13: Missing implementation arc raises ``ValueError``.
- B16: Every created verification arc has
  ``verification_target_id == implementation_arc_id``.
- B20: Malformed (non-string / non-dict / dict) ``_workflow_template``
  values fall back to the default ``coding-change`` template.
- B21: An unknown template name (one that is not in
  ``_WORKFLOW_CORRECTNESS_STEP``) falls back to the default
  ``verify-correctness`` REVIEWER arc.
- B23: :func:`try_create_verification_arcs` swallows ``ValueError`` /
  ``sqlite3.Error`` and returns ``False`` without raising.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from carpenter import config
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.arcs import verification as verification_arcs
from carpenter.core.engine import template_manager
from carpenter.core.workflows._arc_state import (
    get_arc_state,
    set_arc_state,
)


TEMPLATES_SRC = (
    Path(__file__).resolve().parents[2] / "config_seed" / "templates"
)


def _load_template_by_name(tmp_path, name: str) -> int:
    dest = tmp_path / "templates"
    dest.mkdir(exist_ok=True)
    src = TEMPLATES_SRC / f"{name}.yaml"
    out = dest / f"{name}.yaml"
    shutil.copy(src, out)
    return template_manager.load_template(str(out))


@pytest.fixture(autouse=True)
def _enable_verification(monkeypatch):
    monkeypatch.setitem(config.CONFIG, "verification", {"enabled": True})
    yield


def _make_impl_arc(
    name: str = "coding-change: do thing",
    *,
    template_id: int | None = None,
) -> int:
    parent = arc_manager.create_arc(name="parent", goal="parent")
    return arc_manager.create_arc(
        name=name,
        goal="do the thing",
        parent_id=parent,
        agent_type="EXECUTOR",
        integrity_level="trusted",
        template_id=template_id,
    )


# ── B10: source_category inheritance ─────────────────────────────────────


class TestSourceCategoryInheritance:
    """``source_category`` is copied from impl arc to verification children.

    Quality, correctness, and docs inherit; judge does not (it is
    Python-only with no model selection).
    """

    def test_source_category_inherited_by_correctness_non_platform(self):
        impl_id = _make_impl_arc()
        set_arc_state(impl_id, "source_category", "userland")

        v_ids = verification_arcs.create_verification_arcs(
            impl_id, require_completed=False,
        )
        # Non-platform: correctness, judge, docs (3 arcs).
        arcs = {arc_manager.get_arc(v)["name"]: v for v in v_ids}
        assert (
            get_arc_state(arcs["verify-correctness"], "source_category")
            == "userland"
        )
        assert (
            get_arc_state(arcs["post-verification-docs"], "source_category")
            == "userland"
        )

    def test_source_category_inherited_by_quality_platform(self, tmp_path):
        impl_id = _make_impl_arc(
            name=f"coding-change-platform-{tmp_path}",
        )
        set_arc_state(impl_id, "source_category", "platform")

        v_ids = verification_arcs.create_verification_arcs(
            impl_id, require_completed=False,
        )
        # Platform: quality + correctness + judge + docs.
        arcs = {arc_manager.get_arc(v)["name"]: v for v in v_ids}
        assert "verify-quality" in arcs
        assert (
            get_arc_state(arcs["verify-quality"], "source_category")
            == "platform"
        )
        assert (
            get_arc_state(arcs["verify-correctness"], "source_category")
            == "platform"
        )
        assert (
            get_arc_state(arcs["post-verification-docs"], "source_category")
            == "platform"
        )

    def test_source_category_inherited_by_swapped_yaml_verifier(self):
        """yaml-change swap path also inherits source_category."""
        impl_id = _make_impl_arc()
        set_arc_state(impl_id, "source_category", "userland")
        set_arc_state(impl_id, "_workflow_template", "yaml-change")

        v_ids = verification_arcs.create_verification_arcs(
            impl_id, require_completed=False,
        )
        arcs = {arc_manager.get_arc(v)["name"]: v for v in v_ids}
        assert "lint-yaml" in arcs
        assert (
            get_arc_state(arcs["lint-yaml"], "source_category") == "userland"
        )

    def test_missing_source_category_does_not_error(self):
        """When the impl arc has no source_category, children get None."""
        impl_id = _make_impl_arc()
        # No source_category set on impl_id.

        v_ids = verification_arcs.create_verification_arcs(
            impl_id, require_completed=False,
        )
        # Should not raise; children simply have no source_category.
        for v in v_ids:
            assert get_arc_state(v, "source_category") is None


# ── B12: template_id propagation for swapped verifiers ───────────────────


class TestTemplateIdPropagation:
    """Swapped Python-only verifiers (yaml-change / kb-change) carry the
    parent's template_id so the step handler can resolve template metadata.
    """

    def test_template_id_propagated_to_lint_yaml_arc(self, tmp_path):
        tid = _load_template_by_name(tmp_path, "yaml-change")
        impl_id = _make_impl_arc(template_id=tid)
        set_arc_state(impl_id, "_workflow_template", "yaml-change")

        v_ids = verification_arcs.create_verification_arcs(
            impl_id, require_completed=False,
        )
        lint_arc = next(
            arc_manager.get_arc(v)
            for v in v_ids
            if arc_manager.get_arc(v)["name"] == "lint-yaml"
        )
        assert lint_arc["template_id"] == tid

    def test_template_id_propagated_to_kb_format_arc(self, tmp_path):
        tid = _load_template_by_name(tmp_path, "kb-change")
        impl_id = _make_impl_arc(template_id=tid)
        set_arc_state(impl_id, "_workflow_template", "kb-change")

        v_ids = verification_arcs.create_verification_arcs(
            impl_id, require_completed=False,
        )
        kb_arc = next(
            arc_manager.get_arc(v)
            for v in v_ids
            if arc_manager.get_arc(v)["name"] == "verify-kb-format"
        )
        assert kb_arc["template_id"] == tid


# ── B13: error path when impl arc not found ──────────────────────────────


class TestImplArcNotFound:
    def test_raises_value_error_for_nonexistent_arc(self):
        with pytest.raises(ValueError, match="not found"):
            verification_arcs.create_verification_arcs(99999999)


# ── B16: verification_target_id on every child ───────────────────────────


class TestVerificationTargetIdOnAllChildren:
    """Every verification arc (including judge, quality, swapped verifiers)
    must point back at the implementation arc via verification_target_id."""

    def test_all_default_children_have_target_id(self):
        impl_id = _make_impl_arc()
        v_ids = verification_arcs.create_verification_arcs(
            impl_id, require_completed=False,
        )
        for v in v_ids:
            arc = arc_manager.get_arc(v)
            assert arc["verification_target_id"] == impl_id, (
                f"{arc['name']!r} missing verification_target_id"
            )

    def test_all_yaml_change_children_have_target_id(self):
        impl_id = _make_impl_arc()
        set_arc_state(impl_id, "_workflow_template", "yaml-change")
        v_ids = verification_arcs.create_verification_arcs(
            impl_id, require_completed=False,
        )
        for v in v_ids:
            arc = arc_manager.get_arc(v)
            assert arc["verification_target_id"] == impl_id, (
                f"{arc['name']!r} missing verification_target_id"
            )

    def test_all_kb_change_children_have_target_id(self):
        impl_id = _make_impl_arc()
        set_arc_state(impl_id, "_workflow_template", "kb-change")
        v_ids = verification_arcs.create_verification_arcs(
            impl_id, require_completed=False,
        )
        for v in v_ids:
            arc = arc_manager.get_arc(v)
            assert arc["verification_target_id"] == impl_id, (
                f"{arc['name']!r} missing verification_target_id"
            )


# ── B20: malformed _workflow_template values ─────────────────────────────


class TestMalformedWorkflowTemplate:
    """Non-string ``_workflow_template`` values must not break creation.

    The except block in :func:`create_verification_arcs` catches anything
    raised while reading the state key; non-string values (lists, dicts,
    ints) silently fall back to ``coding-change`` and produce the legacy
    REVIEWER correctness arc.
    """

    def test_empty_string_falls_back_to_coding_change(self):
        impl_id = _make_impl_arc()
        set_arc_state(impl_id, "_workflow_template", "")

        v_ids = verification_arcs.create_verification_arcs(
            impl_id, require_completed=False,
        )
        names = {arc_manager.get_arc(v)["name"] for v in v_ids}
        assert "verify-correctness" in names
        assert "lint-yaml" not in names

    def test_non_string_value_falls_back_to_coding_change(self):
        impl_id = _make_impl_arc()
        # arc_state stores JSON, so a list is a legal value.
        set_arc_state(impl_id, "_workflow_template", ["yaml-change"])

        v_ids = verification_arcs.create_verification_arcs(
            impl_id, require_completed=False,
        )
        names = {arc_manager.get_arc(v)["name"] for v in v_ids}
        # Non-string value should not match the swap table.
        assert "verify-correctness" in names
        assert "lint-yaml" not in names

    def test_int_value_falls_back_to_coding_change(self):
        impl_id = _make_impl_arc()
        set_arc_state(impl_id, "_workflow_template", 7)

        v_ids = verification_arcs.create_verification_arcs(
            impl_id, require_completed=False,
        )
        names = {arc_manager.get_arc(v)["name"] for v in v_ids}
        assert "verify-correctness" in names

    def test_state_read_exception_falls_back_to_coding_change(self):
        impl_id = _make_impl_arc()

        # Simulate get_arc_state raising — the inner try/except should
        # swallow it and we should still get a verify-correctness arc.
        with patch(
            "carpenter.core.workflows._arc_state.get_arc_state",
            side_effect=RuntimeError("boom"),
        ):
            v_ids = verification_arcs.create_verification_arcs(
                impl_id, require_completed=False,
            )
        names = {arc_manager.get_arc(v)["name"] for v in v_ids}
        assert "verify-correctness" in names


# ── B21: unknown workflow template names ─────────────────────────────────


class TestUnknownWorkflowTemplate:
    """An unknown template name (not present in
    ``_WORKFLOW_CORRECTNESS_STEP``) keeps the default REVIEWER arc."""

    def test_unknown_template_keeps_verify_correctness(self):
        impl_id = _make_impl_arc()
        set_arc_state(impl_id, "_workflow_template", "frobnicate-change")

        v_ids = verification_arcs.create_verification_arcs(
            impl_id, require_completed=False,
        )
        names = {arc_manager.get_arc(v)["name"] for v in v_ids}
        assert "verify-correctness" in names
        assert "lint-yaml" not in names
        assert "verify-kb-format" not in names


# ── B23: try_create_verification_arcs error handling ─────────────────────


class TestTryCreateVerificationArcsErrorHandling:
    """The convenience wrapper swallows ValueError / sqlite3.Error so that
    a verification-arc creation failure does not crash the calling
    handler.  It must return False and not write the pending flag.
    """

    def test_returns_false_when_arc_missing(self):
        # Arc id does not exist → get_arc returns None → try block exits
        # without setting verification_pending.
        ok = verification_arcs.try_create_verification_arcs(
            99999999, label="missing",
        )
        assert ok is False

    def test_returns_false_on_value_error(self):
        impl_id = _make_impl_arc()
        # Inject a ValueError out of the real create_verification_arcs.
        with patch.object(
            verification_arcs,
            "create_verification_arcs",
            side_effect=ValueError("boom"),
        ):
            ok = verification_arcs.try_create_verification_arcs(
                impl_id, label="arc",
            )
        assert ok is False
        # Pending flag must NOT be set on failure.
        assert get_arc_state(impl_id, "_verification_pending") is None
        assert get_arc_state(impl_id, "_verification_arc_ids") is None

    def test_returns_false_on_sqlite_error(self):
        impl_id = _make_impl_arc()
        with patch.object(
            verification_arcs,
            "create_verification_arcs",
            side_effect=sqlite3.Error("db gone"),
        ):
            ok = verification_arcs.try_create_verification_arcs(
                impl_id, label="arc",
            )
        assert ok is False
        assert get_arc_state(impl_id, "_verification_pending") is None

    def test_success_sets_pending_and_ids(self):
        impl_id = _make_impl_arc()
        ok = verification_arcs.try_create_verification_arcs(
            impl_id, label="arc",
        )
        # is_coding_arc is True for "coding-change: …" and verification is
        # enabled, so the wrapper should create arcs and flip the flag.
        assert ok is True
        v_ids = get_arc_state(impl_id, "_verification_arc_ids")
        assert isinstance(v_ids, list) and len(v_ids) >= 3
        assert get_arc_state(impl_id, "_verification_pending") is True

    def test_non_coding_arc_returns_false_without_error(self):
        # Non-coding arc: should_create_verification_arcs returns False, so
        # wrapper does not call create_verification_arcs and returns False.
        parent = arc_manager.create_arc(name="parent", goal="parent")
        arc_id = arc_manager.create_arc(
            name="chat-task",
            goal="not a coding change",
            parent_id=parent,
        )
        ok = verification_arcs.try_create_verification_arcs(
            arc_id, label="chat",
        )
        assert ok is False
        assert get_arc_state(arc_id, "_verification_pending") is None

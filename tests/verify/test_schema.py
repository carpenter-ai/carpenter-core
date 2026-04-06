"""Tests for attrs schema-based policy type resolution (verify/_schema.py)."""

import pytest
from unittest.mock import patch, MagicMock

import attrs

from carpenter.verify._schema import (
    resolve_policy_type,
    _get_field_policy_type,
    _get_output_contract,
    _load_model_class,
)


# ── Test models ──────────────────────────────────────────────────────

@attrs.define
class AnnotatedModel:
    """Model with policy_type annotations on fields."""
    email: str = attrs.field(metadata={"policy_type": "email"})
    count: int = attrs.field(metadata={"policy_type": "int_range"})
    name: str = ""  # no policy type
    domain: str = attrs.field(default="", metadata={"policy_type": "domain"})
    flag: bool = attrs.field(default=False, metadata={"policy_type": "bool"})


@attrs.define
class UnannotatedModel:
    """Model with no policy annotations."""
    value: str
    number: int


# ── _get_field_policy_type tests ─────────────────────────────────────

class TestGetFieldPolicyType:
    def test_annotated_email(self):
        assert _get_field_policy_type(AnnotatedModel, "email") == "email"

    def test_annotated_int_range(self):
        assert _get_field_policy_type(AnnotatedModel, "count") == "int_range"

    def test_annotated_domain(self):
        assert _get_field_policy_type(AnnotatedModel, "domain") == "domain"

    def test_annotated_bool(self):
        assert _get_field_policy_type(AnnotatedModel, "flag") == "bool"

    def test_unannotated_field(self):
        assert _get_field_policy_type(AnnotatedModel, "name") is None

    def test_missing_field(self):
        assert _get_field_policy_type(AnnotatedModel, "nonexistent") is None

    def test_unannotated_model(self):
        assert _get_field_policy_type(UnannotatedModel, "value") is None

    def test_not_a_model(self):
        assert _get_field_policy_type(str, "anything") is None

    def test_invalid_policy_type_ignored(self):
        @attrs.define
        class BadModel:
            x: str = attrs.field(metadata={"policy_type": "not_a_real_type"})

        assert _get_field_policy_type(BadModel, "x") is None


# ── _load_model_class tests ──────────────────────────────────────────

class TestLoadModelClass:
    def test_invalid_format(self):
        assert _load_model_class("no_colon_here") is None

    def test_missing_module(self):
        assert _load_model_class("nonexistent_module_xyz:SomeClass") is None

    def test_missing_class(self):
        # data_models.example exists but has no "NonExistentClass"
        assert _load_model_class("example:NonExistentClass") is None


# ── resolve_policy_type integration tests ────────────────────────────

class TestResolvePolicyType:
    def test_no_output_contract_returns_none(self):
        """Arc without output_contract → None."""
        with patch("carpenter.verify._schema._get_output_contract", return_value=None):
            assert resolve_policy_type(42, "email") is None

    def test_resolves_from_contract(self):
        """Arc with valid output_contract resolves field policy type."""
        import sys
        import types
        test_mod = types.ModuleType("data_models.__test_schema_model__")
        test_mod.AnnotatedModel = AnnotatedModel
        sys.modules["data_models.__test_schema_model__"] = test_mod

        try:
            with patch("carpenter.verify._schema._get_output_contract",
                       return_value="__test_schema_model__:AnnotatedModel"):
                assert resolve_policy_type(42, "email") == "email"
                assert resolve_policy_type(42, "count") == "int_range"
                assert resolve_policy_type(42, "name") is None
        finally:
            del sys.modules["data_models.__test_schema_model__"]

    def test_bad_contract_returns_none(self):
        """Invalid contract format → None."""
        with patch("carpenter.verify._schema._get_output_contract",
                   return_value="bad_module_xyz:NoClass"):
            assert resolve_policy_type(42, "email") is None


# ── _get_output_contract tests ───────────────────────────────────────

class TestGetOutputContract:
    def test_returns_none_on_db_error(self):
        """Database errors are caught and return None."""
        with patch("carpenter.db.get_db", side_effect=Exception("db error")):
            assert _get_output_contract(42) is None


# ── _resolve_untyped_inputs integration tests ────────────────────────

class TestResolveUntypedInputs:
    def test_fills_detected_type_from_schema(self):
        """InputSpec with detected_type=None gets filled from output_contract."""
        from carpenter.verify import _resolve_untyped_inputs
        from carpenter.verify.taint import InputSpec

        inp = InputSpec(key="email", arc_id=42, integrity_level="constrained")
        assert inp.detected_type is None

        import sys
        import types
        test_mod = types.ModuleType("data_models.__test_resolve_model__")
        test_mod.AnnotatedModel = AnnotatedModel
        sys.modules["data_models.__test_resolve_model__"] = test_mod

        try:
            with patch("carpenter.verify._schema._get_output_contract",
                       return_value="__test_resolve_model__:AnnotatedModel"):
                _resolve_untyped_inputs([inp])
            assert inp.detected_type == "email"
        finally:
            del sys.modules["data_models.__test_resolve_model__"]

    def test_skips_already_typed(self):
        """InputSpec with detected_type already set is not overwritten."""
        from carpenter.verify import _resolve_untyped_inputs
        from carpenter.verify.taint import InputSpec

        inp = InputSpec(key="email", arc_id=42, integrity_level="constrained",
                        detected_type="domain")  # already set
        _resolve_untyped_inputs([inp])
        assert inp.detected_type == "domain"  # unchanged

    def test_skips_no_arc_id(self):
        """InputSpec without arc_id is skipped."""
        from carpenter.verify import _resolve_untyped_inputs
        from carpenter.verify.taint import InputSpec

        inp = InputSpec(key="email", arc_id=None, integrity_level="constrained")
        _resolve_untyped_inputs([inp])
        assert inp.detected_type is None

    def test_graceful_on_resolve_failure(self):
        """Schema resolution failure doesn't crash — detected_type stays None."""
        from carpenter.verify import _resolve_untyped_inputs
        from carpenter.verify.taint import InputSpec

        inp = InputSpec(key="email", arc_id=42, integrity_level="constrained")
        with patch("carpenter.verify._schema.resolve_policy_type", side_effect=Exception("boom")):
            _resolve_untyped_inputs([inp])
        assert inp.detected_type is None

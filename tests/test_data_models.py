"""Tests for structured arc I/O with attrs data models."""

import json
import os
from unittest.mock import patch, MagicMock

import attrs
import cattrs
import pytest

from carpenter import config


# ── Test models ──────────────────────────────────────────────────────

@attrs.define
class SimpleResult:
    status: str
    output: str | None = None


@attrs.define
class InnerMetrics:
    duration_ms: int
    tokens_used: int


@attrs.define
class NestedResult:
    status: str
    metrics: InnerMetrics


@attrs.define
class OptionalFieldsModel:
    name: str
    description: str | None = None
    tags: list[str] = attrs.Factory(list)
    score: float = 0.0


# ── set_typed tests ──────────────────────────────────────────────────

class TestSetTyped:
    """Tests for carpenter_tools.act.state.set_typed."""

    def test_serializes_pydantic_model(self):
        """set_typed calls set() with JSON-serialized model data."""
        from carpenter_tools.act import state as act_state

        model = SimpleResult(status="ok", output="hello")

        with patch.object(act_state, "set") as mock_set:
            mock_set.return_value = {"success": True}
            result = act_state.set_typed("result_key", model)

        mock_set.assert_called_once()
        call_args = mock_set.call_args
        assert call_args[0][0] == "result_key"
        # The second arg should be a JSON string of the model data
        data = json.loads(call_args[0][1])
        assert data["status"] == "ok"
        assert data["output"] == "hello"

    @pytest.mark.parametrize("value", [{"status": "ok"}, "just a string", 42])
    def test_rejects_non_attrs(self, value):
        """set_typed raises TypeError for non-attrs values."""
        from carpenter_tools.act import state as act_state

        with pytest.raises(TypeError, match="attrs class instance"):
            act_state.set_typed("key", value)

    def test_nested_model_serialization(self):
        """set_typed correctly serializes nested Pydantic models."""
        from carpenter_tools.act import state as act_state

        model = NestedResult(
            status="done",
            metrics=InnerMetrics(duration_ms=1500, tokens_used=200),
        )

        with patch.object(act_state, "set") as mock_set:
            mock_set.return_value = {"success": True}
            act_state.set_typed("nested_key", model)

        data = json.loads(mock_set.call_args[0][1])
        assert data["status"] == "done"
        assert data["metrics"]["duration_ms"] == 1500
        assert data["metrics"]["tokens_used"] == 200

    def test_optional_fields_serialization(self):
        """set_typed correctly handles optional fields with defaults."""
        from carpenter_tools.act import state as act_state

        # Only set required fields, use defaults for the rest
        model = OptionalFieldsModel(name="test")

        with patch.object(act_state, "set") as mock_set:
            mock_set.return_value = {"success": True}
            act_state.set_typed("optional_key", model)

        data = json.loads(mock_set.call_args[0][1])
        assert data["name"] == "test"
        assert data["description"] is None
        assert data["tags"] == []
        assert data["score"] == 0.0


# ── get_typed tests ──────────────────────────────────────────────────

class TestGetTyped:
    """Tests for carpenter_tools.read.state.get_typed."""

    def test_deserializes_valid_data(self):
        """get_typed returns a validated model instance."""
        from carpenter_tools.read import state as read_state

        json_str = json.dumps({"status": "ok", "output": "hello"})

        with patch.object(read_state, "get", return_value=json_str):
            result = read_state.get_typed("key", SimpleResult)

        assert isinstance(result, SimpleResult)
        assert result.status == "ok"
        assert result.output == "hello"

    def test_raises_keyerror_on_missing(self):
        """get_typed raises KeyError when the key is not found."""
        from carpenter_tools.read import state as read_state

        with patch.object(read_state, "get", return_value=None):
            with pytest.raises(KeyError, match="State key 'missing' not found"):
                read_state.get_typed("missing", SimpleResult)

    def test_raises_validation_error_on_bad_data(self):
        """get_typed raises ClassValidationError when data does not match model."""
        from carpenter_tools.read import state as read_state

        # Missing required 'status' field
        json_str = json.dumps({"output": "hello"})

        with patch.object(read_state, "get", return_value=json_str):
            with pytest.raises(cattrs.errors.ClassValidationError):
                read_state.get_typed("key", SimpleResult)

    def test_nested_model_deserialization(self):
        """get_typed correctly deserializes nested models."""
        from carpenter_tools.read import state as read_state

        json_str = json.dumps({
            "status": "done",
            "metrics": {"duration_ms": 1500, "tokens_used": 200},
        })

        with patch.object(read_state, "get", return_value=json_str):
            result = read_state.get_typed("key", NestedResult)

        assert isinstance(result, NestedResult)
        assert isinstance(result.metrics, InnerMetrics)
        assert result.metrics.duration_ms == 1500

    def test_optional_fields_deserialization(self):
        """get_typed correctly handles optional fields."""
        from carpenter_tools.read import state as read_state

        # Only required field present
        json_str = json.dumps({"name": "test"})

        with patch.object(read_state, "get", return_value=json_str):
            result = read_state.get_typed("key", OptionalFieldsModel)

        assert result.name == "test"
        assert result.description is None
        assert result.tags == []
        assert result.score == 0.0


# ── Round-trip test ──────────────────────────────────────────────────

class TestRoundTrip:
    """Tests that set_typed -> get_typed produces equivalent models."""

    def test_simple_round_trip(self):
        """Round-trip through set_typed + get_typed returns equivalent model."""
        from carpenter_tools.act import state as act_state
        from carpenter_tools.read import state as read_state

        original = SimpleResult(status="complete", output="done")

        # Capture what set_typed would send to set()
        captured_value = None

        def capture_set(key, value):
            nonlocal captured_value
            captured_value = value
            return {"success": True}

        with patch.object(act_state, "set", side_effect=capture_set):
            act_state.set_typed("round_trip_key", original)

        # Feed that value back through get_typed
        with patch.object(read_state, "get", return_value=captured_value):
            restored = read_state.get_typed("round_trip_key", SimpleResult)

        assert restored == original
        assert restored.status == "complete"
        assert restored.output == "done"

    def test_nested_round_trip(self):
        """Round-trip with nested models preserves all data."""
        from carpenter_tools.act import state as act_state
        from carpenter_tools.read import state as read_state

        original = NestedResult(
            status="finished",
            metrics=InnerMetrics(duration_ms=3000, tokens_used=500),
        )

        captured_value = None

        def capture_set(key, value):
            nonlocal captured_value
            captured_value = value
            return {"success": True}

        with patch.object(act_state, "set", side_effect=capture_set):
            act_state.set_typed("nested_rt", original)

        with patch.object(read_state, "get", return_value=captured_value):
            restored = read_state.get_typed("nested_rt", NestedResult)

        assert restored == original


# ── Contract validation tests ────────────────────────────────────────

class TestContractValidation:
    """Tests for data_model_validation module."""

    def test_parse_contract_ref_valid(self):
        """parse_contract_ref splits module:class correctly."""
        from carpenter.core.arcs.data_model_validation import parse_contract_ref

        mod, cls = parse_contract_ref("data_models.example:TaskResult")
        assert mod == "data_models.example"
        assert cls == "TaskResult"

    def test_parse_contract_ref_no_colon(self):
        """parse_contract_ref raises ValueError for missing colon."""
        from carpenter.core.arcs.data_model_validation import parse_contract_ref

        with pytest.raises(ValueError, match="expected format"):
            parse_contract_ref("data_models.example.TaskResult")

    def test_parse_contract_ref_empty_parts(self):
        """parse_contract_ref raises ValueError for empty module or class."""
        from carpenter.core.arcs.data_model_validation import parse_contract_ref

        with pytest.raises(ValueError, match="non-empty"):
            parse_contract_ref(":TaskResult")

        with pytest.raises(ValueError, match="non-empty"):
            parse_contract_ref("data_models.example:")

    def test_validate_contract_with_valid_data(self):
        """validate_contract returns a model instance for valid data."""
        from carpenter.core.arcs.data_model_validation import validate_contract

        data = {"status": "ok", "output": "hello", "error": None, "metrics": None}
        result = validate_contract(data, "data_models.example:TaskResult")

        assert result.status == "ok"
        assert result.output == "hello"

    def test_validate_contract_with_json_string(self):
        """validate_contract accepts JSON strings."""
        from carpenter.core.arcs.data_model_validation import validate_contract

        json_str = json.dumps({"status": "ok"})
        result = validate_contract(json_str, "data_models.example:TaskResult")
        assert result.status == "ok"

    def test_validate_contract_catches_mismatch(self):
        """validate_contract raises ClassValidationError on schema mismatch."""
        from carpenter.core.arcs.data_model_validation import validate_contract

        # Missing required 'status' field
        with pytest.raises(cattrs.errors.ClassValidationError):
            validate_contract({"output": "hello"}, "data_models.example:TaskResult")

    def test_validate_contract_bad_module(self):
        """validate_contract raises ImportError for non-existent module."""
        from carpenter.core.arcs.data_model_validation import validate_contract

        with pytest.raises(ImportError):
            validate_contract({"status": "ok"}, "nonexistent_module:Foo")

    def test_validate_contract_bad_class(self):
        """validate_contract raises AttributeError for non-existent class."""
        from carpenter.core.arcs.data_model_validation import validate_contract

        with pytest.raises(AttributeError):
            validate_contract({"status": "ok"}, "data_models.example:NonExistent")

    def test_load_model_class(self):
        """load_model_class returns the actual attrs class."""
        from carpenter.core.arcs.data_model_validation import load_model_class

        cls = load_model_class("data_models.example:TaskResult")
        assert cls.__name__ == "TaskResult"
        # Verify it's an attrs class
        assert attrs.has(cls)


# ── Code manager PYTHONPATH integration (removed) ──────────────────
# The subprocess-based PYTHONPATH injection test was removed along with
# the subprocess executor. The RestrictedPython executor runs in-process
# and does not need PYTHONPATH injection.



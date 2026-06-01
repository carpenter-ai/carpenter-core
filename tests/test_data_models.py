"""Tests for structured arc I/O with attrs data models.

NOTE: Tests for carpenter_tools.act.state.set_typed /
carpenter_tools.read.state.get_typed were removed when those functions
became pure declarations (bodies reduced to ``...``).  Tool calls reach
the real handlers in carpenter/tool_backends/ via the executor compat
shim, not through local body execution.  See the ``carpenter_tools``
package docstring for the invocation model.
"""

import json

import attrs
import cattrs
import pytest


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



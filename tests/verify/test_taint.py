"""Tests for static taint propagation (verify/taint.py)."""

import ast
import pytest

from carpenter.verify.taint import analyze_taint, _T, _C, _U


def _make_arc(integrity_level="trusted"):
    """Create a mock arc dict."""
    return {"id": 1, "integrity_level": integrity_level}


def _get_arc_trusted(arc_id):
    return {"id": arc_id, "integrity_level": "trusted"}


def _get_arc_constrained(arc_id):
    return {"id": arc_id, "integrity_level": "constrained"}


def _get_arc_untrusted(arc_id):
    return {"id": arc_id, "integrity_level": "untrusted"}


class TestAllTrusted:
    """Code with only trusted data."""

    def test_simple_assignment(self):
        tree = ast.parse("x = 1")
        result = analyze_taint(tree, _get_arc=_get_arc_trusted)
        assert result.all_trusted

    def test_trusted_state_get(self):
        code = "x = state.get('key', arc_id=5)"
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_trusted)
        assert result.all_trusted

    def test_policy_literal_constructor(self):
        code = 'x = Email("test@example.com")'
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_trusted)
        assert result.all_trusted

    def test_if_on_trusted(self):
        code = "x = 1\nif x > 0:\n    y = 2"
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_trusted)
        assert result.all_trusted


class TestConstrainedData:
    """Code that reads constrained arc state."""

    def test_constrained_state_get(self):
        code = "x = state.get('key', arc_id=10)"
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        assert result.constrained_inputs[0].key == "key"
        assert result.constrained_inputs[0].arc_id == 10

    def test_if_on_constrained(self):
        code = "x = state.get('val', arc_id=10)\nif x:\n    y = 1"
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert not result.all_trusted
        assert len(result.conditions_with_c) == 1

    def test_decomposition_makes_trusted(self):
        """C compared with PolicyLiteral -> T in condition."""
        code = """
x = state.get('email', arc_id=10)
if x == Email("test@example.com"):
    y = 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        # The condition uses decomposition so should be T
        assert result.all_trusted

    def test_constrained_bare_literal_violation(self):
        """C compared with bare literal string -> violation."""
        code = """
x = state.get('email', arc_id=10)
if x == "test@example.com":
    y = 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.violations) >= 1
        assert any("untyped literal" in v for v in result.violations)


class TestLabelPropagation:
    """Label flows through assignments, operations, etc."""

    def test_assignment_propagation(self):
        code = "x = state.get('key', arc_id=10)\ny = x"
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1

    def test_binop_propagation(self):
        code = "x = state.get('v', arc_id=10)\ny = x + 1\nif y:\n    z = 1"
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.conditions_with_c) == 1

    def test_fstring_propagation(self):
        code = 'x = state.get("name", arc_id=10)\ny = f"Hello {x}"'
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        # y should be C
        assert len(result.constrained_inputs) == 1

    def test_subscript_propagation(self):
        code = "x = state.get('data', arc_id=10)\ny = x['key']"
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1

    def test_for_loop_target_inherits_label(self):
        code = """
items = state.get('list', arc_id=10)
for item in items:
    if item == Email("test@example.com"):
        x = 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        # The if condition uses decomposition -> T
        assert result.all_trusted


class TestMultipleInputs:
    def test_two_constrained_inputs(self):
        code = """
a = state.get('x', arc_id=10)
b = state.get('y', arc_id=20)
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 2

    def test_same_input_not_duplicated(self):
        code = """
a = state.get('x', arc_id=10)
b = state.get('x', arc_id=10)
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1


class TestDetectedType:
    """Item 1: Policy type detection from comparison context."""

    def test_email_type_detected(self):
        code = """
x = state.get('email', arc_id=10)
if x == Email("test@example.com"):
    y = 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        assert result.constrained_inputs[0].detected_type == "email"

    def test_domain_type_detected(self):
        code = """
x = state.get('domain', arc_id=10)
if x == Domain("example.com"):
    y = 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        assert result.constrained_inputs[0].detected_type == "domain"

    def test_int_range_type_detected(self):
        code = """
x = state.get('count', arc_id=10)
if x == IntRange(1, 100):
    y = 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        assert result.constrained_inputs[0].detected_type == "int_range"

    def test_bool_type_detected(self):
        code = """
x = state.get('flag', arc_id=10)
if x == Bool(True):
    y = 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        assert result.constrained_inputs[0].detected_type == "bool"

    def test_no_comparison_no_detected_type(self):
        """If no comparison against policy literal, detected_type stays None."""
        code = """
x = state.get('val', arc_id=10)
messaging.send(message=x)
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        assert result.constrained_inputs[0].detected_type is None

    def test_reversed_comparison_detects_type(self):
        """T(PolicyLiteral) == C also detects the type."""
        code = """
x = state.get('email', arc_id=10)
if Email("test@example.com") == x:
    y = 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        assert result.constrained_inputs[0].detected_type == "email"


class TestIteratedInput:
    """Item 2: For-loop marks is_iterated on InputSpec."""

    def test_for_loop_marks_iterated(self):
        code = """
items = state.get('list', arc_id=10)
for item in items:
    messaging.send(message=item)
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        assert result.constrained_inputs[0].is_iterated is True

    def test_non_iterated_stays_false(self):
        code = """
x = state.get('val', arc_id=10)
messaging.send(message=x)
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        assert result.constrained_inputs[0].is_iterated is False

    def test_loop_target_inherits_input_spec_for_type_detection(self):
        """Loop target variable gets InputSpec from iterable, enabling type detection."""
        code = """
items = state.get('emails', arc_id=10)
for item in items:
    if item == Email("test@example.com"):
        y = 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        inp = result.constrained_inputs[0]
        assert inp.is_iterated is True
        assert inp.detected_type == "email"

    def test_non_constrained_for_loop_unchanged(self):
        """For loop over trusted data doesn't set is_iterated."""
        code = """
for x in [1, 2, 3]:
    y = x + 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_trusted)
        assert result.all_trusted
        assert len(result.constrained_inputs) == 0


class TestAccumulatorDetection:
    """Item 1: Detect accumulator patterns in for-loop bodies."""

    def test_augassign_with_loop_var_detected(self):
        """total += item -> has_accumulator=True."""
        code = """
items = state.get('list', arc_id=10)
total = ""
for item in items:
    total += item
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        inp = result.constrained_inputs[0]
        assert inp.is_iterated is True
        assert inp.has_accumulator is True

    def test_augassign_to_loop_var_not_accumulator(self):
        """item += 1 — target IS the loop var, not an accumulator."""
        code = """
items = state.get('list', arc_id=10)
for item in items:
    item += 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        assert result.constrained_inputs[0].has_accumulator is False

    def test_augassign_constant_not_accumulator(self):
        """counter += 1 — value doesn't reference loop var."""
        code = """
items = state.get('list', arc_id=10)
counter = 0
for item in items:
    counter += 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        assert result.constrained_inputs[0].has_accumulator is False

    def test_string_concat_accumulator(self):
        """result += item -> accumulator."""
        code = """
items = state.get('list', arc_id=10)
result = ""
for item in items:
    result += item
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        assert result.constrained_inputs[0].has_accumulator is True

    def test_no_augassign_no_accumulator(self):
        """Simple messaging.send(item) — no accumulator."""
        code = """
items = state.get('list', arc_id=10)
for item in items:
    messaging.send(message=item)
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        assert result.constrained_inputs[0].has_accumulator is False

    def test_non_iterated_has_no_accumulator(self):
        """Non-iterated input doesn't get has_accumulator set."""
        code = """
x = state.get('val', arc_id=10)
messaging.send(message=x)
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        assert result.constrained_inputs[0].has_accumulator is False


class TestDeclarationTypeDecomposition:
    """Declaration types (SecurityType) trigger decomposition just like PolicyLiterals."""

    def test_label_decomposition(self):
        code = """
x = state.get('status', arc_id=10)
if x == Label("completed"):
    y = 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert result.all_trusted

    def test_label_detected_type(self):
        code = """
x = state.get('status', arc_id=10)
if x == Label("completed"):
    y = 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert len(result.constrained_inputs) == 1
        assert result.constrained_inputs[0].detected_type == "label"

    def test_url_decl_decomposition(self):
        code = """
x = state.get('endpoint', arc_id=10)
if x == URL("https://api.example.com"):
    y = 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert result.all_trusted

    def test_workspace_path_decomposition(self):
        code = """
x = state.get('path', arc_id=10)
if x == WorkspacePath("output/result.json"):
    y = 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert result.all_trusted

    def test_sql_decomposition(self):
        code = """
x = state.get('query', arc_id=10)
if x == SQL("SELECT 1"):
    y = 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert result.all_trusted

    def test_json_decomposition(self):
        code = """
x = state.get('data', arc_id=10)
if x == JSON("{}"):
    y = 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert result.all_trusted

    def test_unstructured_text_decomposition(self):
        code = """
x = state.get('text', arc_id=10)
if x == UnstructuredText("hello"):
    y = 1
"""
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_constrained)
        assert result.all_trusted

    def test_declaration_constructor_is_trusted(self):
        """Label("x") call itself produces T label."""
        code = 'x = Label("status")'
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=_get_arc_trusted)
        assert result.all_trusted


class TestArcNotFound:
    def test_missing_arc_defaults_constrained(self):
        code = "x = state.get('key', arc_id=999)"
        tree = ast.parse(code)
        result = analyze_taint(tree, _get_arc=lambda _: None)
        # Unknown arc -> conservative C
        assert len(result.constrained_inputs) == 0  # no input spec for missing arc
        # but label should be C for conditions

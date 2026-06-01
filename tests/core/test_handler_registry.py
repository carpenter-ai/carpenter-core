"""Tests for carpenter.core.engine.handler_registry."""

import pytest

from carpenter.core.engine import handler_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    handler_registry.clear_registry()
    yield
    handler_registry.clear_registry()


async def _noop(arc_id: int, arc_info: dict) -> None:
    return None


async def _other(arc_id: int, arc_info: dict) -> None:
    return None


def test_register_and_lookup():
    handler_registry.register_step_handler("reflection", "save", _noop)
    assert handler_registry.lookup_step_handler("reflection", "save") is _noop


def test_lookup_missing_returns_none():
    assert handler_registry.lookup_step_handler("nope", "nope") is None


def test_lookup_isolated_per_template():
    handler_registry.register_step_handler("reflection", "save", _noop)
    assert handler_registry.lookup_step_handler("other-template", "save") is None
    assert handler_registry.lookup_step_handler("reflection", "other-step") is None


def test_register_overwrites():
    handler_registry.register_step_handler("reflection", "save", _noop)
    handler_registry.register_step_handler("reflection", "save", _other)
    assert handler_registry.lookup_step_handler("reflection", "save") is _other


def test_unregister():
    handler_registry.register_step_handler("reflection", "save", _noop)
    assert handler_registry.unregister_step_handler("reflection", "save") is True
    assert handler_registry.lookup_step_handler("reflection", "save") is None
    # Second unregister is a no-op.
    assert handler_registry.unregister_step_handler("reflection", "save") is False


def test_clear_registry():
    handler_registry.register_step_handler("a", "x", _noop)
    handler_registry.register_step_handler("b", "y", _other)
    handler_registry.clear_registry()
    assert handler_registry.registered_handlers() == []


def test_registered_handlers_sorted():
    handler_registry.register_step_handler("z-tmpl", "step", _noop)
    handler_registry.register_step_handler("a-tmpl", "step", _noop)
    assert handler_registry.registered_handlers() == [
        ("a-tmpl", "step"),
        ("z-tmpl", "step"),
    ]


def test_register_rejects_empty_names():
    with pytest.raises(ValueError):
        handler_registry.register_step_handler("", "step", _noop)
    with pytest.raises(ValueError):
        handler_registry.register_step_handler("tmpl", "", _noop)


def test_register_rejects_non_callable():
    with pytest.raises(TypeError):
        handler_registry.register_step_handler("tmpl", "step", "not a callable")  # type: ignore[arg-type]


def test_lookup_with_empty_names_returns_none():
    handler_registry.register_step_handler("tmpl", "step", _noop)
    assert handler_registry.lookup_step_handler("", "step") is None
    assert handler_registry.lookup_step_handler("tmpl", "") is None


def test_module_reexport():
    """The engine package re-exports the registry API."""
    from carpenter.core import engine

    assert engine.register_step_handler is handler_registry.register_step_handler
    assert engine.lookup_step_handler is handler_registry.lookup_step_handler


# ── D2 PR-α: dual lookup (role + name) ──────────────────────────────


def test_register_by_role_and_lookup_by_role():
    """Registering by role yields a hit under that role."""
    handler_registry.register_step_handler("reflection", "persist", _noop)
    assert handler_registry.lookup_step_handler("reflection", "persist") is _noop


def test_role_and_name_can_coexist():
    """A template can register the same handler under both a role and a
    legacy step name; they live as independent registry keys."""
    handler_registry.register_step_handler("reflection", "persist", _noop)
    handler_registry.register_step_handler("reflection", "save-reflection", _noop)
    # Both lookups succeed because the registry stores both keys.
    assert handler_registry.lookup_step_handler("reflection", "persist") is _noop
    assert handler_registry.lookup_step_handler("reflection", "save-reflection") is _noop


def test_dispatch_dual_lookup_role_first_then_name():
    """Simulates the dispatch-side lookup pattern: role first, fallback to
    name. Registry is plain key/value; the dispatch convention is the
    composition of two lookups, asserted here directly."""
    handler_registry.register_step_handler("reflection", "persist", _noop)
    # An arc with step_role="persist" and step name="save-reflection":
    role_hit = handler_registry.lookup_step_handler("reflection", "persist")
    name_hit = handler_registry.lookup_step_handler("reflection", "save-reflection")
    assert role_hit is _noop
    assert name_hit is None
    # Now a legacy template registers by name; an arc with no role:
    handler_registry.register_step_handler("legacy-tmpl", "step-x", _other)
    role_hit2 = handler_registry.lookup_step_handler("legacy-tmpl", None)  # type: ignore[arg-type]
    name_hit2 = handler_registry.lookup_step_handler("legacy-tmpl", "step-x")
    assert role_hit2 is None  # empty role → None
    assert name_hit2 is _other

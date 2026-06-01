"""Template step handler registry.

Templates may ship Python step handlers that the engine routes to instead of
invoking an LLM agent. Without a registry, the engine would have to hardcode
per-template branches in `dispatch_handler.py`; this module replaces that
pattern with a single generic lookup.

Templates register handlers at template load time via a `register_handlers`
entrypoint in their package. The dispatch path then asks the registry whether
the (template, step) pair has a Python handler; if so, it routes there;
otherwise it falls through to normal agent dispatch.

The second key component is semantically the step's ``role`` (the structural
identifier declared in the template YAML), but the registry accepts either a
role or a step ``name`` and dispatch consults both. Older templates that
register by step name continue to work unchanged. See D18 / D2 PR-α
(2026-04-29) for the rationale.

The registry is process-local and in-memory. Templates re-register on each
process start, just like subscriptions and triggers.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

# Handler signature: async def handler(arc_id: int, arc_info: dict) -> None
StepHandler = Callable[[int, dict], Awaitable[None]]

_REGISTRY: dict[tuple[str, str], StepHandler] = {}


def register_step_handler(
    template_name: str, step_role: str, handler: StepHandler,
) -> None:
    """Register a Python handler for a (template, step) pair.

    ``step_role`` is semantically the step's role (the template-declared
    structural identifier), but for backward compatibility may also be a
    step ``name``. The dispatch path looks up by role first then by name.

    Re-registration overwrites the previous handler. This is intentional:
    template reloads should not error.
    """
    if not template_name or not step_role:
        raise ValueError("template_name and step_role must be non-empty")
    if not callable(handler):
        raise TypeError(f"handler must be callable, got {type(handler).__name__}")
    _REGISTRY[(template_name, step_role)] = handler


def lookup_step_handler(
    template_name: str, step_role: str,
) -> Optional[StepHandler]:
    """Return the registered handler for (template, step), or None.

    ``step_role`` is matched literally against the registry key — callers
    that need both role and name semantics should call this twice (role
    first, then name) per the dispatch convention.
    """
    if not template_name or not step_role:
        return None
    return _REGISTRY.get((template_name, step_role))


def unregister_step_handler(template_name: str, step_role: str) -> bool:
    """Remove a handler. Returns True if one was present."""
    return _REGISTRY.pop((template_name, step_role), None) is not None


def clear_registry() -> None:
    """Drop all registered handlers. Intended for tests."""
    _REGISTRY.clear()


def registered_handlers() -> list[tuple[str, str]]:
    """Return the list of (template_name, step_role) pairs currently registered."""
    return sorted(_REGISTRY.keys())

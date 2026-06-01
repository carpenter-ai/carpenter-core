"""Reflection workflow template package.

Ships the ``reflection`` template alongside its Python step handlers and
the per-arc activity gatherer. The engine's template loader imports this
module at startup and calls :func:`register_handlers`, which wires the
three Python-only step roles (``prepare``, ``persist``, ``dispatch``)
into the handler registry so dispatch routes them here instead of
invoking an LLM agent.

Reflection triggers on ``arc.status_changed`` events for root arcs
reaching ``completed`` status; the subscription that fires it is
declared in ``reflection.yaml`` under ``triggers:`` and loaded at
startup by ``template_manager.load_template_triggers``. There is no
longer any cadence-based timer and no coordinator-side wiring: the
template is fully self-contained.
"""

from __future__ import annotations


def register_handlers(registry) -> None:
    """Register Python step handlers for the reflection template.

    ``registry`` is the ``carpenter.core.engine.handler_registry`` module
    (duck-typed — any object exposing ``register_step_handler`` works).
    """
    from .step_handlers import (
        handle_dispatch_actions,
        handle_gather_activity,
        handle_save_reflection,
    )

    # Register by step role (per D18 / D2 PR-α). The reflection.yaml
    # template declares ``role: prepare/persist/dispatch`` for these
    # three Python-handled steps; the dispatch path looks up by role
    # first and falls back to name for legacy compat.
    registry.register_step_handler(
        "reflection", "prepare", handle_gather_activity,
    )
    registry.register_step_handler(
        "reflection", "persist", handle_save_reflection,
    )
    registry.register_step_handler(
        "reflection", "dispatch", handle_dispatch_actions,
    )

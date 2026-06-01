"""Skill-KB review workflow template package.

Ships the ``skill-kb-review`` template alongside its Python step handlers.
The engine's template loader imports this module at startup and calls
:func:`register_handlers`, which wires the three Python-only steps
(``classify-source``, ``text-review``, ``human-escalation``) into the
handler registry so dispatch routes them here instead of invoking an LLM
agent.

The template is triggered by a subscription declared in
``skill-kb-review.yaml`` on the platform-emitted ``kb.entry_written``
event (filtered to ``skills/`` path + agent source). All feature logic
lives in this package — the platform has no knowledge of skill-KB
review specifics.
"""

from __future__ import annotations


def register_handlers(registry) -> None:
    """Register Python step handlers for the skill-kb-review template.

    ``registry`` is the ``carpenter.core.engine.handler_registry`` module
    (duck-typed — any object exposing ``register_step_handler`` works).
    """
    from .step_handlers import (
        handle_classify_source,
        handle_human_escalation,
        handle_text_review,
    )

    registry.register_step_handler(
        "skill-kb-review", "classify-source", handle_classify_source,
    )
    registry.register_step_handler(
        "skill-kb-review", "text-review", handle_text_review,
    )
    registry.register_step_handler(
        "skill-kb-review", "human-escalation", handle_human_escalation,
    )

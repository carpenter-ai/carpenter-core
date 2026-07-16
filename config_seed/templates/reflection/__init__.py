"""Reflection workflow template package.

Ships the ``reflection`` template alongside its Python step handlers and
the activity gatherer. The engine's template loader imports this module
at startup and calls :func:`register_handlers`, which wires the four
Python-only step roles (``prepare``, ``analyze``, ``persist``,
``dispatch``) into the handler registry so dispatch routes them here
instead of invoking an LLM agent directly. :func:`register_handlers`
also calls :func:`_register_cadence` to install the daily cron that
drives the pipeline.

Reflection is driven by a **daily cadence**, NOT per-arc completion.
A cron (default ``0 4 * * *``, overridable via ``reflection.daily_cron``)
emits ``reflection.daily_tick``; :func:`daily_tick.handle_reflection_tick`
batches the root arcs that completed since the last tick into ``period``
reflection arcs. This replaces an earlier ``arc.status_changed`` trigger
that could form an unbounded feedback loop.

**v2 pipeline (triage-gated):** each batch runs
``gather-activity`` → ``triage`` → ``reflect`` → ``save-reflection`` →
``dispatch-actions``. The ``triage`` step is a cheap haiku call that
decides whether the batch is worth synthesising a KB or tool change
over. When triage returns ``needs_synthesis=false``, the ``reflect``
Python handler short-circuits without invoking the LLM, and the
downstream steps see empty output and no-op — no KB write, no action
dispatch, no token spend beyond triage. The old "diary" KB writes
(``reflections/by-day/{date}``, ``reflections/by-arc/{arc_id}``) have
been removed; KB knowledge only lands via reviewed kb-change action
arcs.
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
        handle_reflect_gated,
        handle_save_reflection,
    )

    # Register by step role (per D18 / D2 PR-α). The reflection.yaml
    # template declares ``role: prepare/analyze/persist/dispatch`` for
    # the Python-handled steps; the dispatch path looks up by role
    # first and falls back to name for legacy compat.
    registry.register_step_handler(
        "reflection", "prepare", handle_gather_activity,
    )
    # The reflect step is an EXECUTOR in reflection.yaml — the Python
    # handler ``handle_reflect_gated`` intercepts dispatch, reads the
    # sibling triage output, and either short-circuits (no LLM call) or
    # invokes the standard EXECUTOR agent path itself. Registering by
    # role suppresses the default engine dispatch.
    registry.register_step_handler(
        "reflection", "analyze", handle_reflect_gated,
    )
    registry.register_step_handler(
        "reflection", "persist", handle_save_reflection,
    )
    registry.register_step_handler(
        "reflection", "dispatch", handle_dispatch_actions,
    )

    _register_cadence()


def _register_cadence() -> None:
    """Wire the daily-cadence batching: a cron that emits
    ``reflection.daily_tick`` and the work-item handler that turns a tick
    into batched ``period`` reflections.

    Reflection no longer triggers per arc-completion (which could form a
    feedback loop); it runs once per day over the arcs that completed since
    the last tick. The schedule is config-overridable via
    ``reflection.daily_cron`` (default 04:00 daily).
    """
    import logging

    from carpenter import config
    from carpenter.core.engine import main_loop

    from .daily_tick import handle_reflection_tick

    main_loop.register_handler("reflection.daily_tick", handle_reflection_tick)

    cron_expr = config.CONFIG.get("reflection", {}).get("daily_cron", "0 4 * * *")
    try:
        from carpenter.core.engine import trigger_manager
        trigger_manager.add_cron(
            name="reflection-daily-tick",
            cron_expr=cron_expr,
            event_type="reflection.daily_tick",
        )
    except Exception as exc:  # pragma: no cover - best-effort registration
        if not ("UNIQUE" in str(exc) or "already" in str(exc).lower()):
            logging.getLogger(__name__).warning(
                "reflection: failed to register daily cron: %s", exc,
            )

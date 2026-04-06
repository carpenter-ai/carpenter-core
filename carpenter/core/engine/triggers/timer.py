"""Timer trigger — cron-based event emission.

Wraps the trigger_manager cron logic. On start(), registers a cron entry
in the database. When ``check_cron()`` finds the entry is due, it emits a
``timer.fired`` event into the event pipeline. The built-in
``_builtin.timer_forward`` subscription routes the event to the work_queue
using the cron entry's original event_type.

Flow: cron_entries → timer.fired event → subscription → work_queue → handler
"""

import logging

from .base import PollableTrigger

logger = logging.getLogger(__name__)


class TimerTrigger(PollableTrigger):
    """Cron-based timer trigger.

    Config:
        schedule: cron expression (e.g., "0 23 * * *")
        emits: event type to emit when schedule fires
        payload: optional static payload to include
    """

    @classmethod
    def trigger_type(cls) -> str:
        return "timer"

    def start(self) -> None:
        """Register the cron entry in the database if not already present."""
        from .. import trigger_manager

        schedule = self.config.get("schedule")
        emits = self.config.get("emits", f"timer.{self.name}")
        payload = self.config.get("payload")

        if not schedule:
            logger.warning("TimerTrigger %s has no schedule configured", self.name)
            return

        try:
            trigger_manager.add_cron(
                name=f"trigger:{self.name}",
                cron_expr=schedule,
                event_type=emits,
                event_payload=payload,
            )
            logger.info("TimerTrigger %s registered cron: %s -> %s", self.name, schedule, emits)
        except (ValueError, Exception) as exc:
            # UNIQUE constraint violation = already registered, which is fine
            if "UNIQUE" in str(exc) or "already" in str(exc).lower():
                logger.debug("TimerTrigger %s cron already registered", self.name)
            else:
                logger.exception("TimerTrigger %s failed to register cron", self.name)

    def check(self) -> None:
        """No-op: cron firing is handled by trigger_manager.check_cron().

        check_cron() emits timer.fired events into the event pipeline.
        The built-in _builtin.timer_forward subscription then routes them
        to work_queue items. This trigger type's value is in providing
        config-driven cron registration via start().
        """
        pass

    def stop(self) -> None:
        """Optionally remove the cron entry on shutdown."""
        # Don't remove — cron entries persist across restarts and are
        # deduplicated by name.
        pass

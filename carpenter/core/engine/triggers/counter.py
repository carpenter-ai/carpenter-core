"""Counter trigger — fires when event count reaches a threshold.

Query-based counting: counts events of a configured type since the last
fire time. When the count reaches the threshold, emits a new event.

Atomic check+fire: updates last_fired_at and inserts the event in a
single transaction to prevent double-firing.
"""

import json
import logging
from datetime import datetime, timezone

from ....db import get_db, db_transaction
from .base import PollableTrigger

logger = logging.getLogger(__name__)


class CounterTrigger(PollableTrigger):
    """Event counter trigger — fires when count reaches threshold.

    Config:
        counts: event type to count (e.g., "arc.status_changed")
        filter: optional payload filter — only count matching events
        threshold: number of events needed to fire
        emits: event type to emit when threshold reached
        payload: optional static payload to include
    """

    @classmethod
    def trigger_type(cls) -> str:
        return "counter"

    def start(self) -> None:
        """Ensure trigger_state row exists."""
        with db_transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO trigger_state "
                "(trigger_name, trigger_type) VALUES (?, ?)",
                (self.name, "counter"),
            )

    def check(self) -> None:
        """Count matching events since last fire and emit if threshold reached."""
        counts_type = self.config.get("counts")
        threshold = self.config.get("threshold", 1)
        emits = self.config.get("emits", f"counter.{self.name}")
        event_filter = self.config.get("filter")
        payload = dict(self.config.get("payload", {}))

        if not counts_type:
            return

        with db_transaction() as db:
            try:
                # Get last fired time
                state = db.execute(
                    "SELECT last_fired_at FROM trigger_state WHERE trigger_name = ?",
                    (self.name,),
                ).fetchone()

                last_fired = state["last_fired_at"] if state else None

                # Count events since last fire
                if last_fired:
                    events = db.execute(
                        "SELECT payload_json FROM events "
                        "WHERE event_type = ? AND created_at > ? "
                        "ORDER BY created_at ASC",
                        (counts_type, last_fired),
                    ).fetchall()
                else:
                    events = db.execute(
                        "SELECT payload_json FROM events "
                        "WHERE event_type = ? "
                        "ORDER BY created_at ASC",
                        (counts_type,),
                    ).fetchall()

                # Apply filter if configured
                if event_filter:
                    matching = 0
                    for event in events:
                        try:
                            event_payload = json.loads(event["payload_json"])
                            if all(
                                event_payload.get(k) == v
                                for k, v in event_filter.items()
                            ):
                                matching += 1
                        except (json.JSONDecodeError, TypeError):
                            continue
                else:
                    matching = len(events)

                if matching < threshold:
                    return

                # Atomic: update last_fired_at + emit event in one transaction
                now = datetime.now(timezone.utc).isoformat()
                idempotency_key = f"counter-{self.name}-{now}"

                payload["count"] = matching
                payload["threshold"] = threshold
                payload["counts_type"] = counts_type

                db.execute(
                    "UPDATE trigger_state SET last_fired_at = ?, counter = counter + 1 "
                    "WHERE trigger_name = ?",
                    (now, self.name),
                )

                db.execute(
                    "INSERT OR IGNORE INTO events "
                    "(event_type, payload_json, source, priority, idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        emits,
                        json.dumps(payload),
                        f"trigger:{self.name}",
                        0,
                        idempotency_key,
                    ),
                )

                logger.info(
                    "CounterTrigger %s fired: %d/%d events of type %s",
                    self.name, matching, threshold, counts_type,
                )

            except Exception:
                logger.exception("Error in CounterTrigger %s check", self.name)

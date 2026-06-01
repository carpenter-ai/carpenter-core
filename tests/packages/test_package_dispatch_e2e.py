"""End-to-end test for the ``package.dispatch`` pipeline.

This test walks the full path that real package events take:

1. Install a fake capability package whose manifest declares a
   ``trigger_subscriptions`` entry pointing at a Python handler.
2. Emit an event matching the subscription's ``event``.
3. Run :func:`subscriptions.process_subscriptions` to drain matching
   events into ``package.dispatch`` work items.
4. Run the work item through
   :func:`carpenter.packages.subscription_handler.dispatch_package_handler`.
5. Assert the package handler was invoked with the original event
   payload.
6. Uninstall the package and confirm the subscription is removed.

The test deliberately does NOT bypass the subscription registry; it
goes through ``installer.install_package``, the in-memory subscription
list, and the work-side dispatcher exactly as the running daemon does.

Deferred from PR #308 review.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter.core.engine import event_bus, subscriptions
from carpenter.db import get_db
from carpenter.packages.installer import (
    ensure_installer_tables,
    install_package,
    uninstall_package,
)
from carpenter.packages.subscription_handler import dispatch_package_handler


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def reset_subscriptions():
    """Wipe the in-memory subscription list around each test."""
    subscriptions.reset()
    yield subscriptions
    subscriptions.reset()


@pytest.fixture
def installer_db():
    """Ensure the installer tables exist on the test DB.

    The autouse ``test_db`` fixture (tests/conftest.py) provisions a
    real SQLite file from the template; we just need to make sure the
    ``installed_packages`` tables are present.  ``install_package``
    will write to this same DB, and ``dispatch_package_handler`` reads
    from it via ``db_connection()`` (which uses ``CONFIG['database_path']``).
    """
    conn = get_db()
    try:
        ensure_installer_tables(conn)
        conn.commit()
    finally:
        conn.close()
    # Hand the connection back to install_package as a fresh handle so
    # writes commit before dispatch_package_handler reads.
    conn = sqlite3.connect(get_db_path())
    try:
        yield conn
    finally:
        conn.close()


def get_db_path() -> str:
    from carpenter import config
    return config.CONFIG["database_path"]


def _write_pkg(
    src_root: Path, name: str, manifest_yaml: str,
    files: dict[str, str],
) -> Path:
    pkg = src_root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.yaml").write_text(dedent(manifest_yaml))
    for rel, content in files.items():
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dedent(content))
    return pkg


# ── Test ────────────────────────────────────────────────────────────


class TestPackageDispatchE2E:
    """End-to-end ``package.dispatch`` flow: install → emit → dispatch."""

    def test_event_routes_to_package_handler(
        self, tmp_path, installer_db, reset_subscriptions,
    ):
        # Use a sentinel file the handler writes to on each invocation.
        # This avoids relying on shared module state, which is fragile
        # given the dynamic ``_carpenter_pkg_.*`` import namespace.
        sentinel = tmp_path / "handler_invocations.json"

        # The handler stringifies the sentinel path inline so it doesn't
        # need any import-time configuration.
        handler_body = (
            "import json\n"
            "from pathlib import Path\n"
            f"_SENTINEL = Path({str(sentinel)!r})\n"
            "def run(payload):\n"
            "    existing = []\n"
            "    if _SENTINEL.exists():\n"
            "        existing = json.loads(_SENTINEL.read_text())\n"
            "    existing.append(payload)\n"
            "    _SENTINEL.write_text(json.dumps(existing))\n"
            "    return payload\n"
        )

        src = _write_pkg(
            tmp_path / "src", "e2e_pkg", """
                name: e2e_pkg
                version: "0.1.0"
                description: End-to-end dispatch test package.
                trigger_subscriptions:
                  - {event: e2e.ping, handler: handlers.h:run}
            """,
            files={
                "handlers/__init__.py": "",
                "handlers/h.py": handler_body,
            },
        )
        dest = tmp_path / "installed" / "e2e_pkg"

        # Step 1: install — registers the in-memory subscription AND
        # writes the ``installed_packages`` row that
        # ``dispatch_package_handler`` later reads to resolve the
        # install dir.
        result = install_package(src, dest, conn=installer_db)
        installer_db.commit()
        assert result.trigger_subscriptions_registered == 1

        # Sanity: the subscription is tagged with the source package and
        # uses the package_dispatch action type.
        pkg_subs = [
            s for s in reset_subscriptions.get_subscriptions()
            if s.source_package == "e2e_pkg"
        ]
        assert len(pkg_subs) == 1
        assert pkg_subs[0].action_type == "package_dispatch"
        assert pkg_subs[0].event_type == "e2e.ping"
        assert pkg_subs[0].action_config == {
            "package": "e2e_pkg",
            "handler": "handlers.h:run",
        }

        # Step 2: emit the matching event via the event bus.
        original_payload = {"hello": "world", "n": 42}
        eid = event_bus.record_event("e2e.ping", original_payload)
        assert eid is not None

        # Step 3: drain the event through the subscription pipeline.
        # This should create a single ``package.dispatch`` work item.
        created = subscriptions.process_subscriptions()
        assert created == 1

        # Locate the enqueued work item so we can hand it to the
        # work-side handler exactly as the main loop would.
        db = get_db()
        try:
            row = db.execute(
                "SELECT id, payload_json FROM work_queue "
                "WHERE event_type = 'package.dispatch'"
            ).fetchone()
        finally:
            db.close()
        assert row is not None
        work_id = row["id"]
        work_payload = json.loads(row["payload_json"])
        assert work_payload["package"] == "e2e_pkg"
        assert work_payload["handler"] == "handlers.h:run"
        assert work_payload["event_payload"] == original_payload

        # Step 4: invoke the dispatcher (the work-queue handler).
        result_value = asyncio.run(
            dispatch_package_handler(work_id, work_payload),
        )

        # Step 5: assert the package handler was invoked with the
        # original event payload.
        assert sentinel.exists(), (
            "Package handler did not run — sentinel file was not "
            "created."
        )
        invocations = json.loads(sentinel.read_text())
        assert invocations == [original_payload]
        # ``dispatch_package_handler`` returns whatever the handler
        # returned; our fake echoes the payload.
        assert result_value == original_payload

        # Step 6: uninstall and verify the subscription is removed.
        uninstall_package("e2e_pkg", conn=installer_db)
        installer_db.commit()
        remaining = [
            s for s in reset_subscriptions.get_subscriptions()
            if s.source_package == "e2e_pkg"
        ]
        assert remaining == []

        # After uninstall, a fresh event should NOT produce a new work
        # item (no subscription remains, and even if one stale work
        # item slipped in, the install dir is gone so the dispatcher
        # would just log and return).
        event_bus.record_event("e2e.ping", {"after": "uninstall"})
        assert subscriptions.process_subscriptions() == 0

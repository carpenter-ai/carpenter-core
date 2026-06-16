"""Tests for loading capability-package triggers at daemon STARTUP.

Regression coverage for: a package's in-process poll triggers were only
instantiated at install time (``install_package`` -> ``_install_triggers``)
and never re-created on a daemon restart.  ``discover_and_register`` —
the startup path — registered every other artifact (chat tools, data
models, judges, arc templates, platform capabilities, subscriptions) but
NOT the package's triggers, so after a restart a package's poll triggers
vanished: they never re-entered the pollable-trigger registry and never
ticked.

These tests simulate a restart by resetting the trigger registry (which
clears ``_instances``, mimicking a fresh process) AFTER install, then
running ``discover_and_register`` and asserting the trigger instance is
present again in ``get_pollable_triggers()``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter.core.engine.triggers import registry as trigger_registry
from carpenter.packages.registry import PackageRegistry
from carpenter.packages.installer import (
    ensure_installer_tables,
    install_package,
)


TRIGGER_BODY = dedent("""\
    from carpenter.core.engine.triggers.base import PollableTrigger


    class _StartupProbe(PollableTrigger):
        '''Pollable trigger used in startup-load integration tests.'''

        @classmethod
        def trigger_type(cls) -> str:
            return 'startup_probe'

        def start(self) -> None:
            pass

        def check(self) -> None:
            pass
""")


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_installer_tables(conn)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS package_state (
            package_name TEXT NOT NULL,
            key          TEXT NOT NULL,
            value_json   TEXT NOT NULL,
            version      INTEGER NOT NULL DEFAULT 1,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (package_name, key),
            FOREIGN KEY (package_name) REFERENCES installed_packages(name)
                ON DELETE CASCADE
        );
    """)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _reset_registry():
    trigger_registry.reset()
    yield
    trigger_registry.reset()


def _make_source(tmp_path: Path, name: str) -> Path:
    src = tmp_path / "src" / name
    src.mkdir(parents=True, exist_ok=True)
    (src / "manifest.yaml").write_text(
        f"name: {name}\n"
        f"version: \"0.1.0\"\n"
        f"description: Startup trigger load test package.\n"
        f"triggers:\n"
        f"  - name: poller\n"
        f"    type: startup_probe\n"
        f"    module: triggers/probe.py\n"
    )
    (src / "triggers").mkdir()
    (src / "triggers" / "__init__.py").write_text("")
    (src / "triggers" / "probe.py").write_text(TRIGGER_BODY)
    return src


def _poll_trigger_packages() -> list[str]:
    return [t.source_package for t in trigger_registry.get_pollable_triggers()]


class TestStartupTriggerLoad:
    def test_trigger_registered_after_simulated_restart(
        self, tmp_path, db_conn,
    ):
        """A package's poll trigger reappears in the pollable-trigger
        registry after ``discover_and_register`` runs on a fresh process
        (NOT a re-install)."""
        src = _make_source(tmp_path, "tpkg")
        dest_root = tmp_path / "installed"
        dest = dest_root / "tpkg"

        install_package(src, dest, conn=db_conn)
        db_conn.commit()
        # Sanity: install registered the poll trigger.
        assert "tpkg" in _poll_trigger_packages()

        # ---- simulate a daemon restart: fresh process => empty _instances.
        trigger_registry.reset()
        # BEFORE the fix: the trigger is absent after a restart.
        assert "tpkg" not in _poll_trigger_packages()

        # The startup path (discover_and_register) must re-instantiate it.
        registry = PackageRegistry()
        loaded = registry.discover_and_register(
            search_paths=[dest_root], db_conn=db_conn,
        )
        assert len(loaded) == 1
        assert loaded[0].manifest.name == "tpkg"

        # AFTER the fix: the poll trigger is present again.
        polls = trigger_registry.get_pollable_triggers()
        tpkg_polls = [t for t in polls if t.source_package == "tpkg"]
        assert len(tpkg_polls) == 1
        assert tpkg_polls[0].name == "poller"
        # Surfaced in the package's artifact counts.
        assert loaded[0].artifact_counts.get("triggers") == 1

    def test_discover_twice_is_idempotent(self, tmp_path, db_conn):
        """Running ``discover_and_register`` twice in one process does not
        create duplicate trigger instances."""
        src = _make_source(tmp_path, "tpkg")
        dest_root = tmp_path / "installed"
        dest = dest_root / "tpkg"
        install_package(src, dest, conn=db_conn)
        db_conn.commit()

        trigger_registry.reset()

        registry = PackageRegistry()
        registry.discover_and_register(
            search_paths=[dest_root], db_conn=db_conn,
        )
        first = trigger_registry.instances_for_package("tpkg")
        assert len(first) == 1

        # A second discover (e.g. a re-scan) must not duplicate the
        # package's trigger instances — _install_triggers drops prior
        # registrations via unregister_for_package before re-loading.
        registry2 = PackageRegistry()
        registry2.discover_and_register(
            search_paths=[dest_root], db_conn=db_conn,
        )
        second = trigger_registry.instances_for_package("tpkg")
        assert len(second) == 1

    def test_refused_package_triggers_not_loaded(self, tmp_path, db_conn):
        """A package that fails SD6 verify (tampered) is refused, and its
        triggers are NOT loaded at startup."""
        src = _make_source(tmp_path, "tpkg")
        dest_root = tmp_path / "installed"
        dest = dest_root / "tpkg"
        install_package(src, dest, conn=db_conn)
        db_conn.commit()

        trigger_registry.reset()
        # Tamper with the installed copy so verify_install fails.
        (dest / "triggers" / "probe.py").write_text("# tampered\n")

        registry = PackageRegistry()
        loaded = registry.discover_and_register(
            search_paths=[dest_root], db_conn=db_conn,
        )
        assert loaded == []
        assert "tpkg" not in _poll_trigger_packages()

"""Tests for installer-level trigger registration (D24 / Phase 3a, PR-B).

Covers:

* ``install_package`` instantiates Trigger subclasses declared in the
  manifest's ``triggers:`` block, tags them with the package name,
  and threads a :class:`PackageStateHandle` to each instance.
* ``uninstall_package`` drops both instances and type registrations,
  invoking ``stop()`` on each instance.
* Re-installing the same package is idempotent (prior trigger
  registrations are dropped before the new ones go in).
* The :class:`InstallResult` reports the trigger count via
  ``triggers_installed``.
* A disabled trigger entry registers the type but does not instantiate.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter.core.engine.triggers import registry as trigger_registry
from carpenter.packages import installer
from carpenter.packages.installer import (
    ensure_installer_tables,
    install_package,
    uninstall_package,
)


TRIGGER_BODY = dedent("""\
    from carpenter.core.engine.triggers.base import PollableTrigger


    class _LifecycleProbe(PollableTrigger):
        '''Pollable trigger used in installer integration tests.

        Records start/stop/check invocations on a class-level list so
        the test can observe lifecycle hook fan-out.
        '''

        events: list[tuple[str, str]] = []

        @classmethod
        def trigger_type(cls) -> str:
            return 'lifecycle_probe'

        def start(self) -> None:
            _LifecycleProbe.events.append(('start', self.name))

        def stop(self) -> None:
            _LifecycleProbe.events.append(('stop', self.name))

        def check(self) -> None:
            _LifecycleProbe.events.append(('check', self.name))
""")


# A trigger whose start() reads + writes package_state via its handle --
# this mirrors the real GmailPollTrigger / imap_poll watermark CAS that
# fires during install_package's outer db_transaction().  Before the fix
# the state ops opened a SECOND connection on the install thread and
# tripped carpenter.db's nested-transaction deadlock guard.
STATEFUL_TRIGGER_BODY = dedent("""\
    from carpenter.core.engine.triggers.base import PollableTrigger


    class _StatefulProbe(PollableTrigger):
        '''Trigger that touches package_state in start().'''

        @classmethod
        def trigger_type(cls) -> str:
            return 'stateful_probe'

        def start(self) -> None:
            # Read-with-version then CAS-insert -- exactly the watermark
            # pattern that produced the install-time tracebacks.
            existing = self.package_state.get_with_version('watermark')
            if existing is None:
                self.package_state.cas('watermark', 0, {'id': 1})
            else:
                self.package_state.set('watermark', {'id': 2})

        def check(self) -> None:
            pass
""")


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_installer_tables(conn)
    # Also create package_state + archive tables so the installer's
    # archive_state path doesn't trip on missing schema.
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
    # Reset the class-level lifecycle event list so tests don't leak.
    yield
    trigger_registry.reset()


def _make_source(tmp_path: Path, name: str, *, triggers_yaml: str) -> Path:
    src = tmp_path / "src" / name
    src.mkdir(parents=True, exist_ok=True)
    manifest = (
        f"name: {name}\n"
        f"version: \"0.1.0\"\n"
        f"description: Trigger lifecycle test package.\n"
        f"{triggers_yaml}"
    )
    (src / "manifest.yaml").write_text(manifest)
    (src / "triggers").mkdir()
    (src / "triggers" / "__init__.py").write_text("")
    (src / "triggers" / "probe.py").write_text(TRIGGER_BODY)
    return src


def _reset_probe_events():
    """Reset the lifecycle probe's class-level event list.

    Imported lazily because the module is loaded by the installer at
    install_package time — until then the class doesn't exist.
    """
    import sys
    for mod_name, mod in list(sys.modules.items()):
        if not mod_name.startswith("carpenter_pkg_trigger_"):
            continue
        probe = getattr(mod, "_LifecycleProbe", None)
        if probe is not None:
            probe.events.clear()


class TestInstallerTriggers:
    def test_install_instantiates_trigger(self, tmp_path, db_conn):
        triggers_yaml = dedent("""\
            triggers:
              - name: probe1
                type: lifecycle_probe
                module: triggers/probe.py
        """)
        src = _make_source(tmp_path, "tpkg", triggers_yaml=triggers_yaml)
        dest = tmp_path / "installed" / "tpkg"

        result = install_package(src, dest, conn=db_conn)
        db_conn.commit()

        assert result.triggers_installed == 1
        # Instance is registered and tagged with the package name.
        instances = trigger_registry.instances_for_package("tpkg")
        assert len(instances) == 1
        inst = instances[0]
        assert inst.name == "probe1"
        assert inst.source_package == "tpkg"
        # A package_state handle bound to the same package was threaded
        # through.
        from carpenter.packages.state import PackageStateHandle
        assert isinstance(inst.package_state, PackageStateHandle)
        assert inst.package_state.package_name == "tpkg"
        # start() was invoked once.
        events = type(inst).events
        assert ("start", "probe1") in events

    def test_uninstall_drops_instance_and_type(self, tmp_path, db_conn):
        triggers_yaml = dedent("""\
            triggers:
              - name: probe1
                type: lifecycle_probe
                module: triggers/probe.py
        """)
        src = _make_source(tmp_path, "tpkg", triggers_yaml=triggers_yaml)
        dest = tmp_path / "installed" / "tpkg"
        install_package(src, dest, conn=db_conn)
        db_conn.commit()

        # Sanity precondition.
        assert trigger_registry.get_trigger_type("lifecycle_probe") is not None
        instances_before = trigger_registry.instances_for_package("tpkg")
        probe_cls = type(instances_before[0])
        # Clear events to isolate the uninstall's stop() call.
        probe_cls.events.clear()

        uninstall_package("tpkg", conn=db_conn)
        db_conn.commit()

        # Instance + type both gone.
        assert trigger_registry.instances_for_package("tpkg") == []
        assert trigger_registry.get_trigger_type("lifecycle_probe") is None
        # stop() fired on the uninstalled instance.
        assert ("stop", "probe1") in probe_cls.events

    def test_reinstall_is_idempotent(self, tmp_path, db_conn):
        triggers_yaml = dedent("""\
            triggers:
              - name: probe1
                type: lifecycle_probe
                module: triggers/probe.py
        """)
        src = _make_source(tmp_path, "tpkg", triggers_yaml=triggers_yaml)
        dest = tmp_path / "installed" / "tpkg"

        install_package(src, dest, conn=db_conn)
        db_conn.commit()
        instances_first = trigger_registry.instances_for_package("tpkg")
        assert len(instances_first) == 1

        # Re-install: prior trigger instance should be replaced, not duplicated.
        install_package(src, dest, conn=db_conn)
        db_conn.commit()
        instances_second = trigger_registry.instances_for_package("tpkg")
        assert len(instances_second) == 1
        # New object — old one was stopped + dropped.
        assert instances_second[0] is not instances_first[0]

    def test_disabled_trigger_not_instantiated(self, tmp_path, db_conn):
        triggers_yaml = dedent("""\
            triggers:
              - name: dormant
                type: lifecycle_probe
                module: triggers/probe.py
                enabled: false
        """)
        src = _make_source(tmp_path, "tpkg", triggers_yaml=triggers_yaml)
        dest = tmp_path / "installed" / "tpkg"

        result = install_package(src, dest, conn=db_conn)
        db_conn.commit()

        assert result.triggers_installed == 0
        # Type registration happens even for disabled entries (the
        # module is imported so its classes are available), but no
        # instance is created.
        assert trigger_registry.get_trigger_type("lifecycle_probe") is not None
        assert trigger_registry.instances_for_package("tpkg") == []

    def test_package_without_triggers_section(self, tmp_path, db_conn):
        src = tmp_path / "src" / "plain"
        src.mkdir(parents=True)
        (src / "manifest.yaml").write_text(dedent("""\
            name: plain
            version: "0.1.0"
            description: No triggers package.
        """))
        dest = tmp_path / "installed" / "plain"
        result = install_package(src, dest, conn=db_conn)
        db_conn.commit()
        assert result.triggers_installed == 0
        assert trigger_registry.instances_for_package("plain") == []

    def test_two_packages_isolated(self, tmp_path, db_conn):
        # pkg-a and pkg-b each declare their own lifecycle_probe-typed
        # trigger but from separate trigger modules.  Uninstalling
        # pkg-a must not touch pkg-b's registration.
        triggers_yaml_a = dedent("""\
            triggers:
              - name: probe_a
                type: probe_a_type
                module: triggers/probe.py
        """)
        triggers_yaml_b = dedent("""\
            triggers:
              - name: probe_b
                type: probe_b_type
                module: triggers/probe.py
        """)
        # Each package's probe.py defines a class with its own trigger_type.
        body_a = TRIGGER_BODY.replace(
            "'lifecycle_probe'", "'probe_a_type'",
        )
        body_b = TRIGGER_BODY.replace(
            "'lifecycle_probe'", "'probe_b_type'",
        )
        for pkg_name, triggers_yaml, body in (
            ("pkg-a", triggers_yaml_a, body_a),
            ("pkg-b", triggers_yaml_b, body_b),
        ):
            src = tmp_path / "src" / pkg_name
            src.mkdir(parents=True)
            manifest = (
                f"name: {pkg_name}\n"
                f"version: \"0.1.0\"\n"
                f"description: Isolation test.\n"
                f"{triggers_yaml}"
            )
            (src / "manifest.yaml").write_text(manifest)
            (src / "triggers").mkdir()
            (src / "triggers" / "probe.py").write_text(body)
            install_package(
                src, tmp_path / "installed" / pkg_name, conn=db_conn,
            )
            db_conn.commit()

        # Both registered.
        assert trigger_registry.get_trigger_type("probe_a_type") is not None
        assert trigger_registry.get_trigger_type("probe_b_type") is not None
        assert len(trigger_registry.instances_for_package("pkg-a")) == 1
        assert len(trigger_registry.instances_for_package("pkg-b")) == 1

        # Uninstall pkg-a.
        uninstall_package("pkg-a", conn=db_conn)
        db_conn.commit()

        # pkg-a is gone; pkg-b survives.
        assert trigger_registry.get_trigger_type("probe_a_type") is None
        assert trigger_registry.instances_for_package("pkg-a") == []
        assert trigger_registry.get_trigger_type("probe_b_type") is not None
        assert len(trigger_registry.instances_for_package("pkg-b")) == 1


class TestInstallInsideDbTransaction:
    """Regression: a trigger whose ``start()`` touches package_state must
    not trip the same-thread deadlock guard when the package is installed
    inside a real ``db_transaction()`` (the way the CLI does it).

    Mirrors ``test_merge_inside_db_transaction_no_nested_connection`` in
    ``test_bfull_install.py`` -- before the fix, the trigger's start-time
    state ops called ``get_db()`` on the install thread while the outer
    write transaction was active, raising ``RuntimeError`` and printing a
    traceback on every install of a stateful-trigger package.
    """

    def _write_stateful_pkg(self, tmp_path: Path, name: str) -> Path:
        triggers_yaml = dedent("""\
            triggers:
              - name: wm
                type: stateful_probe
                module: triggers/probe.py
        """)
        src = tmp_path / "src" / name
        src.mkdir(parents=True, exist_ok=True)
        (src / "manifest.yaml").write_text(
            f"name: {name}\n"
            f"version: \"0.1.0\"\n"
            f"description: Stateful trigger test package.\n"
            f"{triggers_yaml}"
        )
        (src / "triggers").mkdir()
        (src / "triggers" / "__init__.py").write_text("")
        (src / "triggers" / "probe.py").write_text(STATEFUL_TRIGGER_BODY)
        return src

    def test_stateful_trigger_start_no_nested_connection(
        self, tmp_path, test_db, caplog,
    ):
        """No RuntimeError escapes/logs, and the trigger's start-time
        state write is persisted by the install transaction."""
        from carpenter.db import db_transaction, get_db

        # Ensure the installer tables exist in the real test DB.
        conn0 = get_db()
        try:
            ensure_installer_tables(conn0)
            conn0.commit()
        finally:
            conn0.close()

        src = self._write_stateful_pkg(tmp_path, "wmpkg")
        dest = tmp_path / "installed" / "wmpkg"

        with caplog.at_level("ERROR"):
            with db_transaction() as db:
                result = install_package(src, dest, conn=db)
        assert result.triggers_installed == 1

        # No deadlock-guard RuntimeError surfaced via logging.exception.
        guard_msgs = [
            r.getMessage() + (r.exc_text or "")
            for r in caplog.records
            if "db_transaction() is active" in (
                r.getMessage() + (r.exc_text or "")
            )
        ]
        assert guard_msgs == [], guard_msgs

        # The watermark written inside start() committed with the install.
        from carpenter.packages import state as pkg_state
        conn = get_db()
        try:
            row = pkg_state.get_with_version("wmpkg", "watermark", conn=conn)
        finally:
            conn.close()
        assert row is not None
        value, _version = row
        assert value == {"id": 1}

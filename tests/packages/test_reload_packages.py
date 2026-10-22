"""Live loading of a newly installed capability package (no restart).

``reload_packages()`` exists because of an asymmetry in
``loaders._import_package_module``: it caches every module it imports
under ``_carpenter_pkg_.<pkg>.<dotted>`` and returns the cache on
re-entry, so re-installing a *changed* package in a live process would
register the OLD module bodies.  A package installed for the FIRST time
in this process has no such cache entry, so its components can be
registered live.  These tests pin both halves of that: the fresh
install loads and is usable, and an already-imported package is refused
with ``needs_restart=True`` and is NOT re-imported.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter import chat_tool_loader, config
from carpenter.core.engine.triggers import registry as trigger_registry
from carpenter.packages.installer import (
    ensure_installer_tables,
    install_package,
)
from carpenter.packages.registry import PackageRegistry, get_registry
from carpenter.packages.reload import (
    PackageLoadOutcome,
    cached_module_names,
    reload_packages,
)

from .conftest import write_package


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def conn():
    """A real connection to the per-test database, installer tables added."""
    from carpenter.db import get_db
    c = get_db()
    ensure_installer_tables(c)
    c.commit()
    yield c
    c.close()


@pytest.fixture
def install_root(tmp_path, monkeypatch):
    """Install destination, pointed at by the capability-package config."""
    root = tmp_path / "installed"
    root.mkdir()
    monkeypatch.setitem(
        config.CONFIG, "capability_packages",
        {"search_paths": [str(root)]},
    )
    return root


@pytest.fixture
def source_root(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def _reset_triggers_and_subscriptions():
    from carpenter.core.engine import subscriptions
    trigger_registry.reset()
    subscriptions.reset()
    yield
    trigger_registry.reset()
    subscriptions.reset()


# ── Package fixtures ────────────────────────────────────────────────


ALPHA_MANIFEST = """\
name: alpha
version: "0.1.0"
description: A package contributing one of every loadable component.
chat_tools:
  - tools.py
data_models:
  - AlphaBriefing
  - AlphaExtract
arc_templates:
  - name: alpha-triage
    path: templates/alpha-triage/template.yaml
    briefing_kind: AlphaBriefing
    extract_kind: AlphaExtract
    judge_handler: judges.alpha:judge_alpha
judge_handlers:
  - name: judge_alpha
    module: judges.alpha
trigger_subscriptions:
  - {event: alpha.ping, handler: handlers.ping:run}
triggers:
  - name: poller
    type: alpha_probe
    module: triggers/probe.py
"""

ALPHA_FILES = {
    "tools.py": """\
        from carpenter.chat_tool_loader import chat_tool


        @chat_tool(
            description="Alpha's read-only chat tool.",
            input_schema={"type": "object", "properties": {}},
            capabilities=["pure"],
        )
        def alpha_echo(tool_input, **kwargs):
            return "alpha-v1"
    """,
    "data_models.py": """\
        from dataclasses import dataclass


        @dataclass(frozen=True)
        class AlphaBriefing:
            keyword: str


        @dataclass(frozen=True)
        class AlphaExtract:
            qty: int
    """,
    "judges/__init__.py": "",
    "judges/alpha.py": """\
        def judge_alpha(extract):
            class _Result:
                approved = True
                reason = ""
                checks = []
            return _Result()
    """,
    "handlers/__init__.py": "",
    "handlers/ping.py": "def run(payload):\n    return payload\n",
    "templates/alpha-triage/template.yaml": """\
        name: alpha-triage
        description: Alpha's template.
        steps:
          - name: extract
            role: extract
            order: 1
    """,
    "triggers/__init__.py": "",
    "triggers/probe.py": """\
        from carpenter.core.engine.triggers.base import PollableTrigger


        class _AlphaProbe(PollableTrigger):
            '''Pollable trigger used by the live-reload tests.'''

            @classmethod
            def trigger_type(cls) -> str:
                return 'alpha_probe'

            def start(self) -> None:
                pass

            def check(self) -> None:
                pass
    """,
}


BETA_MANIFEST = """\
name: beta
version: "0.1.0"
description: A second package, chat tool only.
chat_tools:
  - tools.py
"""

BETA_FILES = {
    "tools.py": """\
        from carpenter.chat_tool_loader import chat_tool


        @chat_tool(
            description="Beta's read-only chat tool.",
            input_schema={"type": "object", "properties": {}},
            capabilities=["pure"],
        )
        def beta_echo(tool_input, **kwargs):
            return "beta-v1"
    """,
}


def _write_source(
    source_root: Path, name: str, manifest_yaml: str,
    files: dict[str, str] | None = None,
) -> Path:
    return write_package(
        source_root, name=name, manifest_yaml=manifest_yaml,
        files={k: dedent(v) for k, v in (files or {}).items()},
    )


def _install(
    source_root: Path, install_root: Path, conn, name: str,
    manifest_yaml: str, files: dict[str, str] | None = None,
) -> Path:
    """Write a source tree and install it, exactly as an operator would."""
    src = _write_source(source_root, name, manifest_yaml, files)
    dest = install_root / name
    install_package(src, dest, conn=conn)
    conn.commit()
    return dest


def _install_alpha(source_root, install_root, conn, *, echo="alpha-v1") -> Path:
    files = dict(ALPHA_FILES)
    files["tools.py"] = files["tools.py"].replace("alpha-v1", echo)
    return _install(
        source_root, install_root, conn, "alpha", ALPHA_MANIFEST, files,
    )


def _install_beta(source_root, install_root, conn) -> Path:
    return _install(
        source_root, install_root, conn, "beta", BETA_MANIFEST, BETA_FILES,
    )


def _by_name(outcomes) -> dict[str, PackageLoadOutcome]:
    return {o.name: o for o in outcomes}


def _template_rows(conn, name: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM workflow_templates WHERE name = ?", (name,),
    ).fetchone()[0]


def _package_subscription_names(package: str) -> list[str]:
    from carpenter.core.engine import subscriptions
    return [
        s.name for s in subscriptions.get_subscriptions()
        if s.source_package == package
    ]


# ── The premise: a first install has nothing cached ─────────────────


class TestFreshInstallLoadsLive:
    def test_install_leaves_nothing_in_the_module_cache(
        self, source_root, install_root, conn,
    ):
        """The whole design rests on this: installing does not import."""
        _install_alpha(source_root, install_root, conn)
        assert cached_module_names("alpha") == ()

    def test_fresh_package_is_registered_and_its_chat_tool_callable(
        self, source_root, install_root, conn,
    ):
        _install_alpha(source_root, install_root, conn)

        outcomes = reload_packages(conn=conn)

        assert [o.name for o in outcomes] == ["alpha"]
        outcome = outcomes[0]
        assert outcome.loaded is True
        assert outcome.needs_restart is False
        assert dict(outcome.components) == {
            "chat_tools": 1,
            "data_models": 2,
            "arc_templates": 1,
            "judge_handlers": 1,
            "triggers": 1,
            "trigger_subscriptions": 1,
        }
        assert "registered" in outcome.reason

        # The chat tool is live in BOTH dicts and actually callable.
        assert "alpha_echo" in chat_tool_loader._loaded_tools
        assert "alpha_echo" in chat_tool_loader._extension_tools
        tool = chat_tool_loader._loaded_tools["alpha_echo"]
        assert tool.handler(None) == "alpha-v1"
        assert tool.trust_boundary == "chat"

        # And the rest of the components reached their registries.
        entry = get_registry().get("alpha")
        assert entry is not None
        assert entry.load_errors == ()
        assert entry.template_names == ("alpha-triage",)
        assert _template_rows(conn, "alpha-triage") == 1
        from carpenter.packages.handler_registry import get_handler_registry
        handlers = get_handler_registry()
        assert handlers.lookup_kind("AlphaExtract") is not None
        assert handlers.lookup_judge("alpha-triage") is not None
        assert _package_subscription_names("alpha") == ["_pkg.alpha.0"]
        assert [
            t.source_package
            for t in trigger_registry.get_pollable_triggers()
        ] == ["alpha"]


# ── The hash-pin gate ───────────────────────────────────────────────


class TestHashPinGate:
    def test_tampered_install_is_skipped_never_loaded(
        self, source_root, install_root, conn,
    ):
        dest = _install_alpha(source_root, install_root, conn)
        # Someone edits the installed tree behind the platform's back.
        (dest / "tools.py").write_text(
            dedent(ALPHA_FILES["tools.py"]).replace(
                "alpha-v1", "tampered",
            ),
        )

        outcomes = reload_packages(conn=conn)

        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.name == "alpha"
        assert outcome.loaded is False
        assert outcome.needs_restart is False
        assert "hash pin" in outcome.reason
        assert dict(outcome.components) == {}
        # Nothing from the tampered package is live.
        assert "alpha_echo" not in chat_tool_loader._loaded_tools
        assert get_registry().get("alpha") is None
        assert cached_module_names("alpha") == ()

    def test_missing_install_record_is_skipped(
        self, source_root, install_root, conn,
    ):
        """An install dir with no ``installed_packages`` row is refused."""
        _install_alpha(source_root, install_root, conn)
        conn.execute("DELETE FROM installed_packages WHERE name = 'alpha'")
        conn.commit()

        outcomes = reload_packages(conn=conn)

        assert outcomes[0].loaded is False
        assert outcomes[0].needs_restart is False
        assert get_registry().get("alpha") is None


# ── The honest refusal: modules already cached ──────────────────────


class TestAlreadyImportedNeedsRestart:
    def test_reinstall_after_startup_load_refuses_and_keeps_old_module(
        self, source_root, install_root, conn,
    ):
        _install_alpha(source_root, install_root, conn, echo="alpha-v1")
        # Simulate the cold start that loaded the package.
        get_registry().discover_and_register([install_root], db_conn=conn)
        assert chat_tool_loader._loaded_tools["alpha_echo"].handler(
            None,
        ) == "alpha-v1"
        cached_before = cached_module_names("alpha")
        assert cached_before
        module_before = sys.modules[cached_before[0]]

        # A new version of the same package is installed over the top.
        _install_alpha(source_root, install_root, conn, echo="alpha-v2")

        outcomes = reload_packages(conn=conn, only=["alpha"])

        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.loaded is False
        assert outcome.needs_restart is True
        assert "sys.modules" in outcome.reason
        assert "_carpenter_pkg_.alpha." in outcome.reason
        # Nothing was re-imported: same module objects, same names.
        assert cached_module_names("alpha") == cached_before
        assert sys.modules[cached_before[0]] is module_before
        # The live tool is still the OLD body — which is exactly why a
        # restart is the honest answer.
        assert chat_tool_loader._loaded_tools["alpha_echo"].handler(
            None,
        ) == "alpha-v1"

    def test_import_based_preview_before_install_forces_a_restart(
        self, source_root, install_root, conn,
    ):
        """``classify_package_chat_tools`` imports the SOURCE tree under
        the same package namespace, so a caller that builds its install
        preview by importing poisons the cache for the install that
        follows.  (WP4's AST preview is what avoids this.)"""
        from carpenter.packages.installer import classify_package_chat_tools
        from carpenter.packages.manifest import load_manifest

        src = _write_source(
            source_root, "alpha", ALPHA_MANIFEST, ALPHA_FILES,
        )
        read_only, write_capable = classify_package_chat_tools(
            load_manifest(src / "manifest.yaml"),
        )
        assert [t["name"] for t in read_only] == ["alpha_echo"]
        assert write_capable == []
        assert cached_module_names("alpha")  # the preview cached it

        install_package(src, install_root / "alpha", conn=conn)
        conn.commit()

        outcomes = reload_packages(conn=conn)

        assert outcomes[0].loaded is False
        assert outcomes[0].needs_restart is True
        assert "restart" in outcomes[0].reason
        assert get_registry().get("alpha") is None
        assert "alpha_echo" not in chat_tool_loader._loaded_tools


# ── Idempotency, per component type ─────────────────────────────────


class TestIdempotency:
    def test_second_call_registers_nothing_twice(
        self, source_root, install_root, conn,
    ):
        _install_alpha(source_root, install_root, conn)

        first = reload_packages(conn=conn)
        second = reload_packages(conn=conn)

        assert first[0].loaded is True
        assert second[0].loaded is False
        assert second[0].name == "alpha"
        # Already loaded AND cached: a re-load would need a restart.
        assert second[0].needs_restart is True
        # The second call still reports what the package has registered.
        assert dict(second[0].components)["chat_tools"] == 1

        entry = get_registry().get("alpha")
        assert entry is not None
        # No duplicate registrations, and no accumulated load errors.
        assert entry.load_errors == ()
        assert entry.chat_tool_names == ("alpha_echo",)
        assert len(get_registry().list_packages()) == 1
        # chat tools
        assert sum(
            1 for n in chat_tool_loader._loaded_tools if n == "alpha_echo"
        ) == 1
        # arc templates
        assert _template_rows(conn, "alpha-triage") == 1
        # data models + judge handlers
        from carpenter.packages.handler_registry import get_handler_registry
        handlers = get_handler_registry()
        assert len(handlers.list_kinds()) == 2
        assert len(handlers.list_judges()) == 1
        # trigger subscriptions
        assert _package_subscription_names("alpha") == ["_pkg.alpha.0"]
        # in-process triggers
        assert len(trigger_registry.get_pollable_triggers()) == 1

    def test_second_call_on_a_module_free_package_is_not_a_restart(
        self, source_root, install_root, conn,
    ):
        """A package with no python at all is 'already registered', not
        'needs restart' — there is no module cache to be stale."""
        _install(
            source_root, install_root, conn, "quiet", """\
            name: quiet
            version: "0.1.0"
            description: No python, nothing to cache.
            """,
        )

        assert reload_packages(conn=conn)[0].loaded is True
        second = reload_packages(conn=conn)[0]
        assert second.loaded is False
        assert second.needs_restart is False
        assert "already registered" in second.reason


# ── only= filtering ─────────────────────────────────────────────────


class TestOnlyFilter:
    def test_only_registers_the_named_package(
        self, source_root, install_root, conn,
    ):
        _install_alpha(source_root, install_root, conn)
        _install_beta(source_root, install_root, conn)

        outcomes = reload_packages(conn=conn, only=["beta"])

        assert [o.name for o in outcomes] == ["beta"]
        assert outcomes[0].loaded is True
        assert "beta_echo" in chat_tool_loader._loaded_tools
        # alpha was discovered but deliberately left alone.
        assert get_registry().get("alpha") is None
        assert "alpha_echo" not in chat_tool_loader._loaded_tools

    def test_unknown_name_is_reported_not_silently_dropped(
        self, source_root, install_root, conn,
    ):
        _install_alpha(source_root, install_root, conn)

        outcomes = reload_packages(conn=conn, only=["alpha", "ghost"])

        by_name = _by_name(outcomes)
        assert by_name["alpha"].loaded is True
        ghost = by_name["ghost"]
        assert ghost.loaded is False
        assert ghost.needs_restart is False
        assert "not found" in ghost.reason

    def test_empty_only_does_nothing(
        self, source_root, install_root, conn,
    ):
        _install_alpha(source_root, install_root, conn)
        assert reload_packages(conn=conn, only=[]) == ()
        assert get_registry().get("alpha") is None


# ── Trap #9: never clear already-registered extension tools ─────────


class TestExtensionToolsSurvive:
    def test_reloading_one_package_keeps_another_packages_chat_tool(
        self, source_root, install_root, conn,
    ):
        """``chat_tool_loader.load_chat_tools`` clears
        ``_extension_tools``; a reload must never go near it, or every
        package chat tool registered earlier disappears."""
        _install_alpha(source_root, install_root, conn)
        assert reload_packages(conn=conn, only=["alpha"])[0].loaded is True
        assert "alpha_echo" in chat_tool_loader._extension_tools
        alpha_tool = chat_tool_loader._loaded_tools["alpha_echo"]

        _install_beta(source_root, install_root, conn)
        outcomes = reload_packages(conn=conn, only=["beta"])

        assert _by_name(outcomes)["beta"].loaded is True
        assert "beta_echo" in chat_tool_loader._loaded_tools
        # Alpha's tool is untouched — same object, still in both dicts.
        assert chat_tool_loader._loaded_tools["alpha_echo"] is alpha_tool
        assert chat_tool_loader._extension_tools["alpha_echo"] is alpha_tool
        assert chat_tool_loader._loaded_tools["alpha_echo"].handler(
            None,
        ) == "alpha-v1"
        # And alpha's registry entry gained no spurious load errors.
        assert get_registry().get("alpha").load_errors == ()


# ── One bad package must not stop the others ────────────────────────


class TestOneBadPackageIsIsolated:
    def test_registration_failure_is_reported_and_the_rest_load(
        self, source_root, install_root, conn, monkeypatch,
    ):
        _install_alpha(source_root, install_root, conn)
        _install_beta(source_root, install_root, conn)

        real_register = PackageRegistry.register_package

        def _boom(self, manifest, *, db_conn=None):
            if manifest.name == "alpha":
                raise RuntimeError("loader exploded")
            return real_register(self, manifest, db_conn=db_conn)

        monkeypatch.setattr(PackageRegistry, "register_package", _boom)

        outcomes = _by_name(reload_packages(conn=conn))

        assert outcomes["alpha"].loaded is False
        assert outcomes["alpha"].needs_restart is False
        assert "registration failed: RuntimeError" in outcomes["alpha"].reason
        assert outcomes["beta"].loaded is True
        assert "beta_echo" in chat_tool_loader._loaded_tools

    def test_a_broken_chat_tool_module_is_a_non_fatal_load_error(
        self, source_root, install_root, conn,
    ):
        """Matches startup: a loader failure is logged and recorded on
        the package, the package still loads, and other packages are
        unaffected."""
        _install(
            source_root, install_root, conn, "broken", """\
            name: broken
            version: "0.1.0"
            description: Its chat-tool module raises on import.
            chat_tools:
              - tools.py
            """,
            {"tools.py": "raise RuntimeError('boom')\n"},
        )
        _install_beta(source_root, install_root, conn)

        outcomes = _by_name(reload_packages(conn=conn))

        assert outcomes["broken"].loaded is True
        assert outcomes["broken"].needs_restart is False
        assert "non-fatal load error" in outcomes["broken"].reason
        assert get_registry().get("broken").chat_tool_names == ()
        assert outcomes["beta"].loaded is True
        assert "beta_echo" in chat_tool_loader._loaded_tools


# ── The conn= convention ────────────────────────────────────────────


class TestConnectionHandling:
    def test_refuses_to_open_a_nested_transaction(self):
        from carpenter.db import db_transaction

        with db_transaction():
            with pytest.raises(RuntimeError, match="conn="):
                reload_packages()

    def test_opens_its_own_transaction_when_no_conn_is_passed(
        self, source_root, install_root,
    ):
        from carpenter.db import db_transaction

        src = _write_source(source_root, "beta", BETA_MANIFEST, BETA_FILES)
        with db_transaction() as db:
            ensure_installer_tables(db)
            install_package(src, install_root / "beta", conn=db)

        outcomes = reload_packages()

        assert _by_name(outcomes)["beta"].loaded is True
        assert "beta_echo" in chat_tool_loader._loaded_tools

    def test_subscription_record_is_the_source_of_truth(
        self, source_root, install_root, conn,
    ):
        """Reload re-registers subscriptions from the install dir's
        ``_subscriptions.json``, the same record startup reads."""
        from carpenter.core.engine import subscriptions

        dest = _install_alpha(source_root, install_root, conn)
        assert json.loads((dest / "_subscriptions.json").read_text()) == [
            {"event": "alpha.ping", "handler": "handlers.ping:run"},
        ]
        # Drop the in-memory registrations install made, as a fresh
        # process would not have them.
        subscriptions.reset()

        outcome = reload_packages(conn=conn, only=["alpha"])[0]

        assert dict(outcome.components)["trigger_subscriptions"] == 1
        assert _package_subscription_names("alpha") == ["_pkg.alpha.0"]

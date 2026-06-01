"""Tests for the manifest ``triggers:`` section (D24 / Phase 3a, PR-B).

Covers shape validation, path-safety checks, and round-tripping into
:class:`TriggerRef`.  Runtime instantiation is exercised in
``tests/packages/test_installer_triggers.py``.
"""

from __future__ import annotations

import pytest

from carpenter.packages.manifest import (
    ManifestError,
    TriggerRef,
    load_manifest,
)


class TestTriggersSection:
    def test_empty_triggers_default(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            """,
        )
        m = load_manifest(pkg_dir / "manifest.yaml")
        assert m.triggers == ()

    def test_single_trigger_round_trip(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            triggers:
              - name: poll
                type: gmail_poll
                module: triggers/gmail_poll.py
                config:
                  cadence_seconds: 900
            """,
            files={
                "triggers/__init__.py": "",
                "triggers/gmail_poll.py": "# placeholder\n",
            },
        )
        m = load_manifest(pkg_dir / "manifest.yaml")
        assert len(m.triggers) == 1
        t = m.triggers[0]
        assert isinstance(t, TriggerRef)
        assert t.name == "poll"
        assert t.type == "gmail_poll"
        assert t.module == "triggers/gmail_poll.py"
        assert t.config == {"cadence_seconds": 900}
        assert t.enabled is True

    def test_multiple_triggers_same_module(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            triggers:
              - name: inbox_poll
                type: gmail_poll
                module: triggers/gmail.py
              - name: sent_poll
                type: gmail_poll
                module: triggers/gmail.py
                config:
                  label: SENT
            """,
            files={"triggers/gmail.py": "# placeholder\n"},
        )
        m = load_manifest(pkg_dir / "manifest.yaml")
        assert len(m.triggers) == 2
        assert m.triggers[0].name == "inbox_poll"
        assert m.triggers[1].name == "sent_poll"
        assert m.triggers[1].config == {"label": "SENT"}

    def test_disabled_trigger(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            triggers:
              - name: dormant
                type: gmail_poll
                module: triggers/g.py
                enabled: false
            """,
            files={"triggers/g.py": "# placeholder\n"},
        )
        m = load_manifest(pkg_dir / "manifest.yaml")
        assert m.triggers[0].enabled is False

    def test_duplicate_name_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            triggers:
              - name: same
                type: a
                module: triggers/a.py
              - name: same
                type: a
                module: triggers/a.py
            """,
            files={"triggers/a.py": "# x\n"},
        )
        with pytest.raises(ManifestError, match="duplicate instance name"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_missing_required_key(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            triggers:
              - name: foo
                module: triggers/a.py
            """,
            files={"triggers/a.py": "# x\n"},
        )
        with pytest.raises(ManifestError, match="missing required keys"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_unknown_key_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            triggers:
              - name: t
                type: a
                module: triggers/a.py
                bogus: 1
            """,
            files={"triggers/a.py": "# x\n"},
        )
        with pytest.raises(ManifestError, match="unknown keys"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_module_must_exist(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            triggers:
              - name: t
                type: a
                module: triggers/missing.py
            """,
        )
        with pytest.raises(ManifestError, match="not found"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_module_must_be_relative(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            triggers:
              - name: t
                type: a
                module: /etc/passwd
            """,
        )
        with pytest.raises(ManifestError, match="must be relative"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_module_must_not_escape(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            triggers:
              - name: t
                type: a
                module: ../escape.py
            """,
        )
        with pytest.raises(ManifestError, match="not contain '..'"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_module_must_be_py(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            triggers:
              - name: t
                type: a
                module: triggers/not_python.txt
            """,
            files={"triggers/not_python.txt": "# x\n"},
        )
        with pytest.raises(ManifestError, match="must be a .py file"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_invalid_type_string(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            triggers:
              - name: t
                type: "BadType With Spaces"
                module: triggers/a.py
            """,
            files={"triggers/a.py": "# x\n"},
        )
        with pytest.raises(ManifestError, match="type must match"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_config_must_be_mapping(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            triggers:
              - name: t
                type: a
                module: triggers/a.py
                config: "not a dict"
            """,
            files={"triggers/a.py": "# x\n"},
        )
        with pytest.raises(ManifestError, match="must be a mapping"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_enabled_must_be_bool(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            triggers:
              - name: t
                type: a
                module: triggers/a.py
                enabled: yes_please
            """,
            files={"triggers/a.py": "# x\n"},
        )
        with pytest.raises(ManifestError, match="enabled must be a bool"):
            load_manifest(pkg_dir / "manifest.yaml")

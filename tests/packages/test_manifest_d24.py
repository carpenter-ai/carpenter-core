"""Tests for D24 stage 3a manifest schema extensions.

Covers the new declaration-only fields: ``arc_templates``,
``judge_handlers``, ``data_models``, ``kb_articles``,
``trigger_subscriptions``, plus the security-side change
(``judge_handlers`` removed from forbidden raw keys; ``judge`` and
``judge_handler`` singular still rejected).
"""

from __future__ import annotations

import pytest

from carpenter.packages.manifest import (
    ArcTemplateRef,
    JudgeHandlerRef,
    KbArticleRef,
    ManifestError,
    SubscriptionRef,
    load_manifest,
)
from carpenter.packages.security import (
    PackageSecurityError,
    validate_manifest_security,
)


# ── arc_templates ────────────────────────────────────────────────────


class TestArcTemplates:
    def test_empty_arc_templates_default(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            """,
        )
        m = load_manifest(pkg_dir / "manifest.yaml")
        assert m.arc_templates == ()

    def test_arc_template_with_kinds(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            data_models:
              - Brief
              - Extract
            arc_templates:
              - name: triage
                path: templates/triage.yaml
                briefing_kind: Brief
                extract_kind: Extract
            """,
            files={
                "templates/triage.yaml": "name: triage\n",
                "data_models.py": "from dataclasses import dataclass\n",
            },
        )
        m = load_manifest(pkg_dir / "manifest.yaml")
        assert len(m.arc_templates) == 1
        t = m.arc_templates[0]
        assert isinstance(t, ArcTemplateRef)
        assert t.name == "triage"
        assert t.path == "templates/triage.yaml"
        assert t.briefing_kind == "Brief"
        assert t.extract_kind == "Extract"

    def test_arc_template_path_must_exist(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            arc_templates:
              - name: triage
                path: templates/missing.yaml
            """,
        )
        with pytest.raises(ManifestError, match="not found at"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_arc_template_path_must_not_escape(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            arc_templates:
              - name: triage
                path: "../etc/passwd"
            """,
        )
        with pytest.raises(ManifestError, match="'..'"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_arc_template_briefing_kind_must_be_in_data_models(
        self, make_package,
    ):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            data_models:
              - Other
            arc_templates:
              - name: triage
                path: templates/triage.yaml
                briefing_kind: Missing
            """,
            files={
                "templates/triage.yaml": "x\n",
                "data_models.py": "",
            },
        )
        with pytest.raises(ManifestError, match="not declared in data_models"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_arc_template_duplicate_name_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            arc_templates:
              - name: triage
                path: templates/triage.yaml
              - name: triage
                path: templates/triage2.yaml
            """,
            files={
                "templates/triage.yaml": "x\n",
                "templates/triage2.yaml": "x\n",
            },
        )
        with pytest.raises(ManifestError, match="duplicate template name"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_arc_template_unknown_key_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            arc_templates:
              - name: triage
                path: templates/triage.yaml
                evil_key: yes
            """,
            files={"templates/triage.yaml": "x\n"},
        )
        with pytest.raises(ManifestError, match="unknown keys"):
            load_manifest(pkg_dir / "manifest.yaml")


# ── judge_handlers ───────────────────────────────────────────────────


class TestJudgeHandlers:
    def test_judge_handlers_field_now_allowed(self, make_package):
        """``judge_handlers`` was previously a forbidden raw key; D24
        stage 3a moves it to the allowed schema."""
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            judge_handlers:
              - name: judge_x
                module: judges.x
            """,
            files={"judges/x.py": "def run(extract):\n    return None\n"},
        )
        m = load_manifest(pkg_dir / "manifest.yaml")
        assert len(m.judge_handlers) == 1
        h = m.judge_handlers[0]
        assert isinstance(h, JudgeHandlerRef)
        assert h.name == "judge_x"
        assert h.module == "judges.x"

    def test_judge_handler_singular_still_forbidden(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            judge_handler: judges/foo.py
            """,
        )
        with pytest.raises(ManifestError, match="unknown fields"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_judge_keyword_still_forbidden_at_security_layer(
        self, make_package, tmp_path,
    ):
        """``judge`` (the bare key) is still in _FORBIDDEN_RAW_KEYS so
        it raises a security-flavored error at the security layer.

        At the manifest loader level it's already rejected as an
        unknown field; we reach here only by constructing a manifest
        that does NOT use the loader (this is a unit test for the
        security layer in isolation).
        """
        from carpenter.packages.security import (
            _FORBIDDEN_RAW_KEYS,
        )
        # Sanity: judge is forbidden but judge_handlers is not.
        assert "judge" in _FORBIDDEN_RAW_KEYS
        assert "judge_handler" in _FORBIDDEN_RAW_KEYS
        assert "judge_handlers" not in _FORBIDDEN_RAW_KEYS

    def test_judge_module_must_resolve_to_existing_file(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            judge_handlers:
              - name: judge_x
                module: judges.missing
            """,
            files={},
        )
        with pytest.raises(ManifestError, match="not found"):
            load_manifest(pkg_dir / "manifest.yaml")


# ── data_models ─────────────────────────────────────────────────────


class TestDataModels:
    def test_data_models_requires_data_models_py(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            data_models:
              - Foo
            """,
        )
        with pytest.raises(ManifestError, match="data_models.py not found"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_data_models_loads(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            data_models:
              - Foo
              - Bar
            """,
            files={"data_models.py": ""},
        )
        m = load_manifest(pkg_dir / "manifest.yaml")
        assert m.data_models == ("Foo", "Bar")

    def test_data_models_duplicate_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            data_models:
              - Foo
              - Foo
            """,
            files={"data_models.py": ""},
        )
        with pytest.raises(ManifestError, match="duplicate dataclass"):
            load_manifest(pkg_dir / "manifest.yaml")


# ── kb_articles ─────────────────────────────────────────────────────


class TestKbArticles:
    def test_kb_articles_path_must_exist(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            kb_namespace: p
            kb_articles:
              - path: kb/p/missing.md
                slug: p/missing
            """,
        )
        with pytest.raises(ManifestError, match="not found at"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_kb_articles_loads(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            kb_namespace: p
            kb_articles:
              - path: kb/p/overview.md
                slug: p/overview
            """,
            files={"kb/p/overview.md": "# overview\n"},
        )
        m = load_manifest(pkg_dir / "manifest.yaml")
        assert len(m.kb_articles) == 1
        ar = m.kb_articles[0]
        assert isinstance(ar, KbArticleRef)
        assert ar.path == "kb/p/overview.md"
        assert ar.slug == "p/overview"

    def test_kb_articles_slug_must_not_escape(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            kb_namespace: p
            kb_articles:
              - path: kb/p/overview.md
                slug: ../escape
            """,
            files={"kb/p/overview.md": "x\n"},
        )
        with pytest.raises(ManifestError, match="'..'"):
            load_manifest(pkg_dir / "manifest.yaml")


# ── trigger_subscriptions ───────────────────────────────────────────


class TestTriggerSubscriptions:
    def test_subscription_loads(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            trigger_subscriptions:
              - event: x.received
                handler: handlers.h:run
            """,
        )
        m = load_manifest(pkg_dir / "manifest.yaml")
        assert len(m.trigger_subscriptions) == 1
        s = m.trigger_subscriptions[0]
        assert isinstance(s, SubscriptionRef)
        assert s.event == "x.received"
        assert s.handler == "handlers.h:run"

    def test_subscription_handler_must_have_colon(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            trigger_subscriptions:
              - event: x.received
                handler: handlers.h
            """,
        )
        with pytest.raises(ManifestError, match="module:function"):
            load_manifest(pkg_dir / "manifest.yaml")

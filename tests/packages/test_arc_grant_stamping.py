"""Tests for capability-package arc-grant stamping.

The package-capability framework registers TRUSTED dispatch verbs and
gates them per-package: a verb is dispatchable only from an arc that
carries the owning package's grant (``pkg.<name>``) in its
``_capabilities`` arc_state (see
:mod:`carpenter.executor.dispatch_bridge`).  This module covers the
piece that *stamps* that grant onto a capability package's own arc
pipeline.

A capability package ships its arc tree as an arc template (loaded via
:func:`carpenter.packages.loaders.load_arc_templates`).  The template is
recorded with ``owner_package`` and, when instantiated, every step arc —
crucially the EXECUTOR child that calls ``dispatch(<verb>)`` — is stamped
with ``pkg.<owner>``.

Scoping is the security property under test:

* an arc instantiated from package P's template carries ``pkg.P`` and CAN
  invoke P's registered capability verb through ``validate_and_dispatch``;
* an arc NOT belonging to P is DENIED P's verb;
* the grant does not leak to platform-shipped (un-owned) templates or to a
  different package's template.
"""

from __future__ import annotations

import json

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.engine import template_manager
from carpenter.core.trust.capabilities import get_arc_capabilities
from carpenter.db import db_connection


# ── Helpers ─────────────────────────────────────────────────────────


@pytest.fixture
def reset_cap_registry():
    from carpenter.packages.capabilities import get_capability_registry
    get_capability_registry().reset()
    yield
    get_capability_registry().reset()


def _register_verb(package_name: str, verb: str):
    """Register a trivial trusted capability verb for ``package_name``."""
    from carpenter.packages.capabilities import get_capability_registry
    from carpenter.packages.manifest import EgressGrant
    get_capability_registry().register(
        package_name=package_name,
        verb=verb,
        kind="egress",
        handler=lambda params, ctx: {"ok": True, "pkg": ctx.package_name},
        grant=EgressGrant(
            protocol="demo", host_from="HOST", port=993,
            credential_ref="DEMO_MAIL",
        ),
        host="h.example.com",
    )


def _write_template_yaml(tmp_path, name: str) -> str:
    """Write a PLANNER→EXECUTOR→REVIEWER→JUDGE template YAML, return path."""
    yaml_text = f"""\
name: {name}
description: Capability-package pipeline template.
steps:
  - name: plan
    role: plan
    agent_type: PLANNER
    order: 1
  - name: do
    role: do
    agent_type: EXECUTOR
    order: 2
"""
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml_text)
    return str(path)


def _executor_arc_id(parent_id: int) -> int:
    """Return the id of the EXECUTOR child of ``parent_id``."""
    with db_connection() as db:
        row = db.execute(
            "SELECT id FROM arcs WHERE parent_id = ? AND agent_type = 'EXECUTOR'",
            (parent_id,),
        ).fetchone()
    assert row is not None, "template did not create an EXECUTOR child"
    return row["id"]


def _session_for(arc_id: int) -> str:
    """Create a reviewed arc-step execution session for ``arc_id``."""
    import uuid
    from datetime import datetime, timedelta, timezone
    sid = str(uuid.uuid4())
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with db_connection() as db:
        db.execute(
            "INSERT INTO execution_sessions "
            "(session_id, reviewed, expires_at, execution_context) "
            "VALUES (?, 1, ?, 'arc-step')",
            (sid, expires),
        )
        db.commit()
    return sid


# ── load_template records owner_package ─────────────────────────────


class TestOwnerPackageRecorded:
    def test_owner_package_persisted(self, tmp_path):
        path = _write_template_yaml(tmp_path, "owned-pipeline")
        template_manager.load_template(path, owner_package="capdemo")
        tpl = template_manager.get_template_by_name("owned-pipeline")
        assert tpl is not None
        assert tpl["owner_package"] == "capdemo"

    def test_platform_template_has_no_owner(self, tmp_path):
        path = _write_template_yaml(tmp_path, "platform-pipeline")
        template_manager.load_template(path)  # no owner_package
        tpl = template_manager.get_template_by_name("platform-pipeline")
        assert tpl is not None
        assert tpl["owner_package"] is None


# ── Stamping: every step arc carries pkg.<owner> ────────────────────


class TestStamping:
    def test_all_step_arcs_carry_owner_grant(self, tmp_path):
        path = _write_template_yaml(tmp_path, "stamp-pipeline")
        tid = template_manager.load_template(path, owner_package="capdemo")
        parent = arc_manager.create_arc(name="root", agent_type="PLANNER")
        arc_ids = template_manager.instantiate_template(tid, parent)
        assert arc_ids
        for arc_id in arc_ids:
            caps = get_arc_capabilities(arc_id)
            assert "pkg.capdemo" in caps, (
                f"arc {arc_id} missing owner grant; caps={caps}"
            )

    def test_executor_arc_carries_owner_grant(self, tmp_path):
        path = _write_template_yaml(tmp_path, "stamp-exec")
        tid = template_manager.load_template(path, owner_package="capdemo")
        parent = arc_manager.create_arc(name="root", agent_type="PLANNER")
        template_manager.instantiate_template(tid, parent)
        exec_id = _executor_arc_id(parent)
        assert "pkg.capdemo" in get_arc_capabilities(exec_id)

    def test_owner_grant_merges_with_template_capabilities(self, tmp_path):
        # A template that already declares capabilities should keep them
        # AND gain the owner grant.
        yaml_text = (
            "name: merge-pipeline\n"
            "description: t\n"
            "capabilities:\n"
            "  - kb.read\n"
            "steps:\n"
            "  - name: do\n"
            "    role: do\n"
            "    agent_type: EXECUTOR\n"
            "    order: 1\n"
        )
        path = tmp_path / "merge.yaml"
        path.write_text(yaml_text)
        tid = template_manager.load_template(
            str(path), owner_package="capdemo",
        )
        parent = arc_manager.create_arc(name="root", agent_type="PLANNER")
        template_manager.instantiate_template(tid, parent)
        exec_id = _executor_arc_id(parent)
        caps = get_arc_capabilities(exec_id)
        assert "pkg.capdemo" in caps
        assert "kb.read" in caps


# ── Scoping: only owned arcs get the grant ──────────────────────────


class TestScoping:
    def test_platform_template_arcs_have_no_owner_grant(self, tmp_path):
        # Un-owned (platform) template → no pkg.* grant stamped.
        path = _write_template_yaml(tmp_path, "scope-platform")
        tid = template_manager.load_template(path)  # no owner
        parent = arc_manager.create_arc(name="root", agent_type="PLANNER")
        arc_ids = template_manager.instantiate_template(tid, parent)
        for arc_id in arc_ids:
            caps = get_arc_capabilities(arc_id)
            assert not any(c.startswith("pkg.") for c in caps), caps

    def test_other_package_template_does_not_get_p_grant(self, tmp_path):
        # Package Q's template stamps pkg.Q, never pkg.P.
        path = _write_template_yaml(tmp_path, "scope-other")
        tid = template_manager.load_template(path, owner_package="otherpkg")
        parent = arc_manager.create_arc(name="root", agent_type="PLANNER")
        template_manager.instantiate_template(tid, parent)
        exec_id = _executor_arc_id(parent)
        caps = get_arc_capabilities(exec_id)
        assert "pkg.otherpkg" in caps
        assert "pkg.capdemo" not in caps


# ── End-to-end: stamped EXECUTOR can dispatch the verb ──────────────


class TestDispatchThroughStampedArc:
    def test_stamped_executor_can_invoke_owning_verb(
        self, tmp_path, reset_cap_registry,
    ):
        from carpenter.executor.dispatch_bridge import validate_and_dispatch

        _register_verb("capdemo", "demo.echo")
        path = _write_template_yaml(tmp_path, "e2e-pipeline")
        tid = template_manager.load_template(path, owner_package="capdemo")
        parent = arc_manager.create_arc(name="root", agent_type="PLANNER")
        template_manager.instantiate_template(tid, parent)
        exec_id = _executor_arc_id(parent)
        sid = _session_for(exec_id)

        out = validate_and_dispatch(
            "demo.echo", {"hello": 1}, session_id=sid, arc_id=exec_id,
        )
        assert out == {"ok": True, "pkg": "capdemo"}

    def test_non_package_arc_denied_owning_verb(
        self, tmp_path, reset_cap_registry,
    ):
        from carpenter.executor.dispatch_bridge import (
            DispatchError,
            validate_and_dispatch,
        )

        _register_verb("capdemo", "demo.echo")
        # An arc instantiated from a DIFFERENT package's template.
        path = _write_template_yaml(tmp_path, "e2e-other")
        tid = template_manager.load_template(path, owner_package="otherpkg")
        parent = arc_manager.create_arc(name="root", agent_type="PLANNER")
        template_manager.instantiate_template(tid, parent)
        exec_id = _executor_arc_id(parent)
        sid = _session_for(exec_id)

        with pytest.raises(DispatchError, match="own arcs"):
            validate_and_dispatch(
                "demo.echo", {"hello": 1}, session_id=sid, arc_id=exec_id,
            )

    def test_platform_template_arc_denied_owning_verb(
        self, tmp_path, reset_cap_registry,
    ):
        # An un-owned platform template's arc must not be able to invoke a
        # package's verb — the grant never leaks to non-package arcs.
        from carpenter.executor.dispatch_bridge import (
            DispatchError,
            validate_and_dispatch,
        )

        _register_verb("capdemo", "demo.echo")
        path = _write_template_yaml(tmp_path, "e2e-platform")
        tid = template_manager.load_template(path)  # no owner
        parent = arc_manager.create_arc(name="root", agent_type="PLANNER")
        template_manager.instantiate_template(tid, parent)
        exec_id = _executor_arc_id(parent)
        sid = _session_for(exec_id)

        with pytest.raises(DispatchError, match="own arcs"):
            validate_and_dispatch(
                "demo.echo", {"hello": 1}, session_id=sid, arc_id=exec_id,
            )


# ── Loader path: load_arc_templates records the owner ───────────────


class TestLoaderRecordsOwner:
    def test_load_arc_templates_sets_owner_package(
        self, tmp_path, monkeypatch,
    ):
        """``load_arc_templates`` records ``owner_package=manifest.name``."""
        from carpenter.packages.manifest import load_manifest
        from carpenter.packages.loaders import load_arc_templates

        pkg = tmp_path / "capdemo"
        (pkg / "templates" / "p").mkdir(parents=True)
        (pkg / "manifest.yaml").write_text(
            "name: capdemo\n"
            'version: "0.1.0"\n'
            "description: t\n"
            "arc_templates:\n"
            "  - name: capdemo-pipeline\n"
            "    path: templates/p/template.yaml\n"
        )
        (pkg / "templates" / "p" / "template.yaml").write_text(
            "name: capdemo-pipeline\n"
            "description: t\n"
            "steps:\n"
            "  - name: do\n"
            "    role: do\n"
            "    agent_type: EXECUTOR\n"
            "    order: 1\n"
        )
        manifest = load_manifest(pkg / "manifest.yaml")
        n, errors, names = load_arc_templates(manifest)
        assert errors == []
        assert n == 1
        assert names == ["capdemo-pipeline"]
        tpl = template_manager.get_template_by_name("capdemo-pipeline")
        assert tpl is not None
        assert tpl["owner_package"] == "capdemo"

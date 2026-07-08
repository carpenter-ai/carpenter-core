"""Tests for daily-cadence reflection batching and the subject model.

Reflection is no longer fired per arc-completion; a daily cron emits
``reflection.daily_tick`` and ``handle_reflection_tick`` batches the arcs
that completed since the last tick into ``period`` reflections. These tests
cover the subject descriptor, KB keying, eligibility filtering, and the
end-to-end batching handler.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import types
from datetime import datetime, timedelta, timezone

import pytest


def _recent_iso() -> str:
    """A timestamp safely inside the handler's default (now-24h, now] window."""
    return (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

from carpenter.core.engine import handler_registry, subscriptions, template_manager
from carpenter.core.engine.triggers import registry as trigger_registry
from carpenter.core.workflows._arc_state import get_arc_state, set_arc_state
from carpenter.db import db_transaction

TEMPLATES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "config_seed", "templates",
)


def _copy_seed(tmp_path):
    dest = str(tmp_path / "templates")
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(TEMPLATES_DIR):
        src = os.path.join(TEMPLATES_DIR, f)
        if os.path.isfile(src) and f.endswith((".yaml", ".yml")):
            shutil.copy(src, dest)
        elif os.path.isdir(src) and not f.startswith((".", "_")):
            shutil.copytree(src, os.path.join(dest, f))
    return dest


@pytest.fixture
def pkg(tmp_path):
    """Load the seed templates (registers the reflection package + its
    workflow_templates rows) and return its importable submodules.

    The ``carpenter_template_packages.reflection`` namespace only exists
    after the template loader runs, so imports must happen here, not at
    module import time.
    """
    trigger_registry.reset()
    subscriptions.reset()
    handler_registry.clear_registry()
    template_manager.load_templates_from_dir(_copy_seed(tmp_path))
    ns = types.SimpleNamespace(
        subject=importlib.import_module(
            "carpenter_template_packages.reflection._subject"),
        daily_tick=importlib.import_module(
            "carpenter_template_packages.reflection.daily_tick"),
        kb_entry=importlib.import_module(
            "carpenter_template_packages.reflection.kb_entry"),
    )
    yield ns
    trigger_registry.reset()
    subscriptions.reset()
    handler_registry.clear_registry()


# ── subject model ───────────────────────────────────────────────────


def test_subject_arc_ids_and_kb_path(pkg):
    _subject = pkg.subject
    arcs = {"kind": "arcs", "refs": [7]}
    period = {"kind": "period", "refs": [1, 2, 3],
              "window": {"from": "a", "to": "2026-06-19T00:00:00", "date": "2026-06-19"}}
    theme = {"kind": "theme", "theme": "email"}

    assert _subject.subject_arc_ids(arcs) == [7]
    assert _subject.subject_arc_ids(period) == [1, 2, 3]
    assert _subject.subject_arc_ids(theme) == []

    assert _subject.subject_kb_path(arcs) == "reflections/by-arc/7"
    assert _subject.subject_kb_path(period) == "reflections/by-day/2026-06-19"
    assert _subject.subject_kb_path(theme) == "reflections/by-theme/email"


def test_get_subject_legacy_fallback(pkg):
    _subject = pkg.subject
    # An arc with only a legacy scalar reflected_arc_id reads back as an
    # ``arcs`` subject of one — keeps old per-arc callers working.
    set_arc_state(0, "reflected_arc_id", 42)
    subject = _subject.get_subject(0)
    assert subject == {"kind": "arcs", "refs": [42]}


def test_get_subject_prefers_explicit_subject(pkg):
    _subject = pkg.subject
    set_arc_state(0, "reflected_arc_id", 42)
    set_arc_state(0, "reflection_subject",
                  {"kind": "period", "refs": [1, 2], "window": {"date": "d"}})
    subject = _subject.get_subject(0)
    assert subject["kind"] == "period"
    assert subject["refs"] == [1, 2]


def test_build_entry_from_period_subject(pkg):
    kb_entry = pkg.kb_entry
    subject = {"kind": "period", "refs": [5, 6],
               "window": {"from": "2026-06-18T00:00:00",
                          "to": "2026-06-19T00:00:00", "date": "2026-06-19"}}
    entry = kb_entry.build_reflection_entry(subject=subject, content="lessons")
    assert entry["kb_path"] == "reflections/by-day/2026-06-19"
    assert "subject_kind: period" in entry["content"]
    assert "lessons" in entry["content"]


# ── eligibility filtering ───────────────────────────────────────────


def _insert_arc(name, status="completed", parent_id=None, template_id=None,
                updated_at=None):
    if updated_at is None:
        updated_at = _recent_iso()
    with db_transaction() as db:
        cur = db.execute(
            "INSERT INTO arcs (name, status, parent_id, template_id, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, status, parent_id, template_id,
             _recent_iso(), updated_at),
        )
        return cur.lastrowid


def test_eligible_excludes_meta_templates_and_non_roots(pkg):
    daily_tick = pkg.daily_tick
    # The fixture already loaded these templates into workflow_templates.
    refl_tid = template_manager.get_template_by_name("reflection")["id"]
    review_tid = template_manager.get_template_by_name("skill-kb-review")["id"]

    user_arc = _insert_arc("user-goal")                        # eligible (no tmpl)
    user_arc2 = _insert_arc("user-goal-2")                     # eligible (no tmpl)
    refl_arc = _insert_arc("reflection", template_id=refl_tid)  # excluded (meta)
    review_arc = _insert_arc("review", template_id=review_tid)  # excluded (meta)
    child = _insert_arc("child", parent_id=user_arc)            # excluded (not root)
    pending = _insert_arc("pending", status="active")          # excluded (not completed)

    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with db_transaction() as db:
        eligible = daily_tick._eligible_root_arcs(db, since, until)

    assert set(eligible) == {user_arc, user_arc2}


# ── end-to-end batching handler ─────────────────────────────────────


@pytest.fixture
def _escalation_open(monkeypatch):
    """Open the reflection escalation gate for the end-to-end batching tests.

    The reflection.daily_tick handler now refuses to run unless an
    escalation destination is configured
    (see :func:`carpenter.core.reflection_escalation.ensure_escalation_ready`).
    These tests exercise the batching logic, not the gate itself, so we
    stub the gate to True.  ``get_or_create_reflection_home_conversation``
    is left as-is: it operates purely on the isolated test DB.
    """
    monkeypatch.setattr(
        "carpenter.core.reflection_escalation.ensure_escalation_ready",
        lambda: True,
    )


def test_daily_tick_creates_period_batches(pkg, _escalation_open):
    from carpenter import config
    daily_tick = pkg.daily_tick

    # Two eligible completed root arcs (no template → genuine user work).
    a1 = _insert_arc("goal-1")
    a2 = _insert_arc("goal-2")

    # batch_size large → a single period reflection over both arcs.
    config.CONFIG["reflection"] = {"batch_size": 20}
    asyncio.run(daily_tick.handle_reflection_tick(0, {}))

    with db_transaction() as db:
        refl_rows = db.execute(
            "SELECT id FROM arcs WHERE name = 'reflection' "
            "AND origin_kind = 'reflection' AND parent_id IS NULL"
        ).fetchall()
    assert len(refl_rows) == 1
    subject = get_arc_state(refl_rows[0]["id"], "reflection_subject")
    assert subject["kind"] == "period"
    assert set(subject["refs"]) == {a1, a2}

    # Watermark advanced → a second tick with no new arcs makes nothing.
    asyncio.run(daily_tick.handle_reflection_tick(0, {}))
    with db_transaction() as db:
        count = db.execute(
            "SELECT COUNT(*) AS n FROM arcs WHERE name = 'reflection' "
            "AND origin_kind = 'reflection' AND parent_id IS NULL"
        ).fetchone()["n"]
    assert count == 1


def test_daily_tick_splits_into_multiple_batches(pkg, _escalation_open):
    from carpenter import config
    daily_tick = pkg.daily_tick
    _subject = pkg.subject

    ids = [_insert_arc(f"goal-{i}") for i in range(3)]
    config.CONFIG["reflection"] = {"batch_size": 2}
    asyncio.run(daily_tick.handle_reflection_tick(0, {}))

    with db_transaction() as db:
        refl_rows = db.execute(
            "SELECT id FROM arcs WHERE name = 'reflection' "
            "AND origin_kind = 'reflection' AND parent_id IS NULL ORDER BY id"
        ).fetchall()
    # 3 arcs, batch_size 2 → ceil(3/2) = 2 reflection arcs.
    assert len(refl_rows) == 2
    all_refs = []
    paths = []
    for r in refl_rows:
        subj = get_arc_state(r["id"], "reflection_subject")
        all_refs.extend(subj["refs"])
        paths.append(_subject.subject_kb_path(subj))
    assert set(all_refs) == set(ids)
    # Multiple batches in a day get disambiguated KB keys.
    assert len(set(paths)) == 2

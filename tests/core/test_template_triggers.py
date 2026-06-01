"""Tests for template-owned ``triggers:`` declarations (P4)."""

import pytest

from carpenter.core.engine import subscriptions, template_manager


SAMPLE_YAML_WITH_TRIGGERS = """\
name: trig-tmpl
description: Template that declares its own triggers
triggers:
  - name: on-pr-opened
    "on": forgejo.pr.opened
    action:
      type: create_arc
      template_name: trig-tmpl
  - name: on-pr-closed
    "on": forgejo.pr.closed
    action:
      type: send_notification
      message: PR closed
steps:
  - name: handle
    description: Handle the event
    order: 0
"""


SAMPLE_YAML_NO_TRIGGERS = """\
name: notrig-tmpl
description: Template without triggers
steps:
  - name: step-a
    order: 0
"""


@pytest.fixture(autouse=True)
def _reset_subscriptions():
    subscriptions.reset()
    yield
    subscriptions.reset()


def _write_tmpl(tmp_path, filename, body):
    f = tmp_path / filename
    f.write_text(body)
    return str(f)


# ── load_template: triggers persisted ──────────────────────────────


def test_load_template_stores_triggers(tmp_path):
    path = _write_tmpl(tmp_path, "t.yaml", SAMPLE_YAML_WITH_TRIGGERS)
    tid = template_manager.load_template(path)
    tmpl = template_manager.get_template(tid)
    assert len(tmpl["triggers"]) == 2
    assert tmpl["triggers"][0]["on"] == "forgejo.pr.opened"
    assert tmpl["triggers"][1]["on"] == "forgejo.pr.closed"


def test_get_template_returns_empty_triggers_when_absent(tmp_path):
    path = _write_tmpl(tmp_path, "t.yaml", SAMPLE_YAML_NO_TRIGGERS)
    tid = template_manager.load_template(path)
    tmpl = template_manager.get_template(tid)
    assert tmpl["triggers"] == []


def test_list_templates_includes_triggers(tmp_path):
    _write_tmpl(tmp_path, "t1.yaml", SAMPLE_YAML_WITH_TRIGGERS)
    _write_tmpl(tmp_path, "t2.yaml", SAMPLE_YAML_NO_TRIGGERS)
    template_manager.load_templates_from_dir(str(tmp_path))

    listed = {t["name"]: t for t in template_manager.list_templates()}
    assert len(listed["trig-tmpl"]["triggers"]) == 2
    assert listed["notrig-tmpl"]["triggers"] == []


# ── collect_template_triggers ──────────────────────────────────────


def test_collect_template_triggers_namespaces_names(tmp_path):
    path = _write_tmpl(tmp_path, "t.yaml", SAMPLE_YAML_WITH_TRIGGERS)
    template_manager.load_template(path)

    configs = template_manager.collect_template_triggers()
    names = sorted(c["name"] for c in configs)
    assert names == ["trig-tmpl:on-pr-closed", "trig-tmpl:on-pr-opened"]


def test_collect_template_triggers_preserves_qualified_names(tmp_path):
    """A trigger whose name already has a colon is not re-namespaced."""
    yaml_body = """\
name: custom-tmpl
description: Template with a pre-namespaced trigger name
triggers:
  - name: ops:on-deploy
    event: deploy.event
    action:
      type: send_notification
      message: deployed
steps:
  - name: s
    order: 0
"""
    template_manager.load_template(_write_tmpl(tmp_path, "c.yaml", yaml_body))
    configs = template_manager.collect_template_triggers()
    assert [c["name"] for c in configs] == ["ops:on-deploy"]


def test_collect_template_triggers_ignores_templates_without_triggers(tmp_path):
    template_manager.load_template(_write_tmpl(tmp_path, "a.yaml", SAMPLE_YAML_NO_TRIGGERS))
    assert template_manager.collect_template_triggers() == []


# ── load_template_triggers: registration wiring ────────────────────


def test_load_template_triggers_registers_subscriptions(tmp_path):
    path = _write_tmpl(tmp_path, "t.yaml", SAMPLE_YAML_WITH_TRIGGERS)
    template_manager.load_template(path)

    count = template_manager.load_template_triggers()
    assert count == 2

    loaded = {s.name: s for s in subscriptions.get_subscriptions()}
    assert "trig-tmpl:on-pr-opened" in loaded
    assert loaded["trig-tmpl:on-pr-opened"].event_type == "forgejo.pr.opened"
    assert loaded["trig-tmpl:on-pr-opened"].action_type == "create_arc"


def test_load_template_triggers_zero_when_no_templates():
    assert template_manager.load_template_triggers() == 0


def test_yaml_bare_on_key_still_loads_subscription(tmp_path):
    """YAML 1.1 parses bare ``on:`` as True; loader tolerates it."""
    yaml_body = """\
name: booly-tmpl
description: Uses unquoted `on:` (YAML parses as True)
triggers:
  - name: on-x
    on: event.x
    action:
      type: send_notification
      message: hi
steps:
  - name: s
    order: 0
"""
    template_manager.load_template(_write_tmpl(tmp_path, "b.yaml", yaml_body))
    assert template_manager.load_template_triggers() == 1
    loaded = {s.name: s for s in subscriptions.get_subscriptions()}
    assert loaded["booly-tmpl:on-x"].event_type == "event.x"


def test_yaml_event_alias_loads_subscription(tmp_path):
    """``event:`` is accepted as an alias for ``on:`` (YAML ergonomics)."""
    yaml_body = """\
name: alias-tmpl
description: Uses `event:` alias
triggers:
  - name: fires
    event: my.event
    action:
      type: send_notification
      message: hi
steps:
  - name: s
    order: 0
"""
    template_manager.load_template(_write_tmpl(tmp_path, "a.yaml", yaml_body))
    assert template_manager.load_template_triggers() == 1
    loaded = {s.name: s for s in subscriptions.get_subscriptions()}
    assert loaded["alias-tmpl:fires"].event_type == "my.event"

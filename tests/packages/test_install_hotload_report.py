"""What the operator is told about using a just-installed package.

A chat-requested install is only useful if "install X, then use X" works
in the same session, so a successful batch registers its packages live
(D7).  Where that is unsound — a package whose modules this process
already imported, so a re-import would hand back the old bodies — the
operator is told a restart is needed instead.  Exactly one of three
things is said about each installed package, and nothing here restarts
or exits the process: reporting is this module's job, deciding is the
operator's.
"""

from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter.db import db_connection, db_transaction, get_db
from carpenter.packages import install_batch as ib
from carpenter.packages import install_requests as ir
from carpenter.packages import pending_install as pi
from carpenter.packages import reload as pkg_reload
from carpenter.packages.fetch import FetchError, FetchResult
from carpenter.packages.installer import ensure_installer_tables, get_install_record
from carpenter.packages.loaders import _PKG_NAMESPACE


ALPHA_MANIFEST = """\
name: alpha
version: "1.0.0"
description: A package with nothing to import.
"""

BETA_MANIFEST = """\
name: beta
version: "2.0.0"
description: Another one.
"""

TOOLS_MANIFEST = """\
name: toolsy
version: "0.1.0"
description: Ships one chat tool, so it has a module to import.
chat_tools:
  - tools.py
"""

TOOLS_FILES = {
    "tools.py": """\
        from carpenter.chat_tool_loader import chat_tool


        @chat_tool(
            description="Read a thing.",
            input_schema={"type": "object", "properties": {}},
            capabilities=["filesystem_read"],
        )
        def toolsy_read(tool_input, **kwargs):
            return "hi"
    """,
}


def _write_tree(dest: Path, manifest_yaml: str, files: dict | None = None):
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.yaml").write_text(dedent(manifest_yaml))
    for rel, content in (files or {}).items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dedent(content))
    return dest


@pytest.fixture
def fake_fetch(monkeypatch):
    trees: dict[str, tuple[str, dict]] = {}
    calls: list = []

    def register(fragment: str, manifest_yaml: str, files=None):
        trees[fragment] = (manifest_yaml, files or {})

    def fake_fetch_source(spec, *, limits=None, staging_root=None):
        calls.append(spec.raw)
        matching = [f for f in trees if f in spec.raw]
        if not matching:
            raise FetchError(f"no fixture registered for {spec.raw!r}")
        manifest_yaml, files = trees[max(matching, key=len)]
        quarantine = Path(staging_root) / f"_fetch-{len(calls)}"
        quarantine.mkdir(parents=True)
        tree = _write_tree(quarantine / "pkg", manifest_yaml, files)
        return FetchResult(
            tree=tree, staging=quarantine, resolved="c" * 40,
            bytes_fetched=4096, files_fetched=2,
        )

    monkeypatch.setattr(ir, "fetch_source", fake_fetch_source)
    fake_fetch_source.register = register  # type: ignore[attr-defined]
    fake_fetch_source.calls = calls  # type: ignore[attr-defined]
    return fake_fetch_source


@pytest.fixture
def installer_tables():
    db = get_db()
    try:
        ensure_installer_tables(db)
    finally:
        db.close()


def _spec(name: str) -> str:
    return f"git+https://forge.invalid/org/{name}.git"


def _approved(sources, grants=None):
    out = ir.create_install_request(sources)
    with db_transaction() as db:
        pi.mark_approved(db, out.record.token, grants=grants or {})
    return out


def _blocks(result):
    return {block["name"]: block for block in result["packages"]}


# ── loaded live ─────────────────────────────────────────────────────

def test_a_fresh_install_is_loaded_live(installer_tables, fake_fetch):
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = _approved([_spec("alpha")])

    batch = ib.perform_install_batch(out.record.id)

    block = _blocks(batch.result)["alpha"]
    assert block["load"]["loaded"] is True
    assert block["load"]["needs_restart"] is False
    assert block["load"]["reason"]
    assert ib.describe_load(block) == "loaded live"
    assert batch.result["hot_load"] == {"attempted": True, "error": None}


def test_a_live_load_registers_the_package_in_this_process(
    installer_tables, fake_fetch,
):
    """The point of hot-loading: the tool is usable without a restart."""
    fake_fetch.register("toolsy", TOOLS_MANIFEST, TOOLS_FILES)
    out = _approved([_spec("toolsy")])

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is True
    block = _blocks(batch.result)["toolsy"]
    assert block["load"]["loaded"] is True
    assert block["load"]["components"].get("chat_tools") == 1
    from carpenter.packages.registry import get_registry
    assert get_registry().get("toolsy") is not None


def test_only_the_batch_s_packages_are_considered(
    installer_tables, fake_fetch, monkeypatch,
):
    """``only=`` keeps a batch from re-registering unrelated packages."""
    seen: list = []
    real = pkg_reload.reload_packages

    def spy(*, only=None, conn=None):
        seen.append(list(only or []))
        return real(only=only, conn=conn)

    monkeypatch.setattr(pkg_reload, "reload_packages", spy)
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    fake_fetch.register("beta", BETA_MANIFEST)
    out = _approved([_spec("alpha"), _spec("beta")])

    ib.perform_install_batch(out.record.id)

    assert seen == [["alpha", "beta"]]


def test_both_packages_of_a_batch_are_reported_on(installer_tables, fake_fetch):
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    fake_fetch.register("beta", BETA_MANIFEST)
    out = _approved([_spec("alpha"), _spec("beta")])

    batch = ib.perform_install_batch(out.record.id)

    blocks = _blocks(batch.result)
    assert set(blocks) == {"alpha", "beta"}
    for block in blocks.values():
        assert ib.describe_load(block) == "loaded live"


# ── restart required ────────────────────────────────────────────────

def test_an_already_imported_package_reports_restart_required(
    installer_tables, fake_fetch, monkeypatch,
):
    """D7: ``_import_package_module`` caches, so a re-import is a lie."""
    fake_fetch.register("toolsy", TOOLS_MANIFEST, TOOLS_FILES)
    out = _approved([_spec("toolsy")])
    monkeypatch.setitem(sys.modules, f"{_PKG_NAMESPACE}.toolsy.tools", object())

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is True, "the install itself must still succeed"
    block = _blocks(batch.result)["toolsy"]
    assert block["load"]["needs_restart"] is True
    assert block["load"]["loaded"] is False
    assert ib.describe_load(block) == (
        "a platform restart is required (already imported in this process)"
    )


def test_a_failed_live_load_does_not_fail_the_install(
    installer_tables, fake_fetch, monkeypatch,
):
    def boom(**kwargs):
        raise RuntimeError("the registry is wedged")

    monkeypatch.setattr(pkg_reload, "reload_packages", boom)
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = _approved([_spec("alpha")])

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is True
    assert batch.result["hot_load"]["error"] == "the registry is wedged"
    block = _blocks(batch.result)["alpha"]
    assert block["load"]["needs_restart"] is True
    assert "restart" in ib.describe_load(block)
    with db_connection() as db:
        assert get_install_record(db, "alpha") is not None


def test_a_package_the_load_pass_skips_is_still_reported(
    installer_tables, fake_fetch, monkeypatch,
):
    """Silence is not an outcome: every package gets one of the three."""
    monkeypatch.setattr(pkg_reload, "reload_packages", lambda **kw: ())
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = _approved([_spec("alpha")])

    batch = ib.perform_install_batch(out.record.id)

    block = _blocks(batch.result)["alpha"]
    assert block["load"]["loaded"] is False
    assert block["load"]["needs_restart"] is True
    assert "restart" in ib.describe_load(block)


def test_a_skipped_package_reports_why_without_claiming_a_restart(
    installer_tables, fake_fetch, monkeypatch,
):
    """A hash-pin failure is neither "live" nor "needs a restart"."""
    outcome = pkg_reload.PackageLoadOutcome(
        name="alpha", loaded=False, needs_restart=False,
        reason="installed hash does not match the recorded one",
    )
    monkeypatch.setattr(pkg_reload, "reload_packages", lambda **kw: (outcome,))
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = _approved([_spec("alpha")])

    batch = ib.perform_install_batch(out.record.id)

    block = _blocks(batch.result)["alpha"]
    assert ib.describe_load(block) == (
        "not loaded: installed hash does not match the recorded one"
    )


# ── what is never done ──────────────────────────────────────────────

def test_a_rolled_back_batch_is_never_hot_loaded(
    installer_tables, fake_fetch, monkeypatch,
):
    from carpenter.packages import installer

    real = installer.install_package

    def wrapper(source_path, dest_path, **kwargs):
        if Path(dest_path).name == "beta":
            raise installer.InstallError("boom")
        return real(source_path, dest_path, **kwargs)

    monkeypatch.setattr(installer, "install_package", wrapper)
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    fake_fetch.register("beta", BETA_MANIFEST)
    out = _approved([_spec("alpha"), _spec("beta")])

    batch = ib.perform_install_batch(out.record.id)

    assert batch.result["hot_load"] is None
    assert _blocks(batch.result)["alpha"]["load"] is None


def test_nothing_restarts_or_exits_the_process(installer_tables, fake_fetch):
    """Not even a restart-pending flag: the operator decides that."""
    from carpenter.core.engine import main_loop

    before = main_loop._restart_pending
    fake_fetch.register("toolsy", TOOLS_MANIFEST, TOOLS_FILES)
    out = _approved([_spec("toolsy")])
    sys.modules[f"{_PKG_NAMESPACE}.toolsy.tools"] = object()
    try:
        batch = ib.perform_install_batch(out.record.id)
    finally:
        sys.modules.pop(f"{_PKG_NAMESPACE}.toolsy.tools", None)

    assert _blocks(batch.result)["toolsy"]["load"]["needs_restart"] is True
    assert main_loop._restart_pending is before


# ── the sentence the operator reads ─────────────────────────────────

def test_the_chat_message_carries_exactly_one_load_verdict(
    installer_tables, fake_fetch,
):
    from carpenter.packages import install_notify

    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = _approved([_spec("alpha")])
    batch = ib.perform_install_batch(out.record.id)
    with db_transaction() as db:
        record = pi.set_status(
            db, out.record.token, pi.STATUS_INSTALLING, expect=pi.STATUS_APPROVED,
        )
        record = pi.mark_installed(db, out.record.token, result=batch.result)

    message = install_notify.render_install_message(record)

    assert message.count("loaded live") == 1
    assert "restart is required" not in message
    assert "A platform restart is needed" not in message


def test_the_chat_message_says_a_restart_is_needed(
    installer_tables, fake_fetch, monkeypatch,
):
    from carpenter.packages import install_notify

    fake_fetch.register("toolsy", TOOLS_MANIFEST, TOOLS_FILES)
    out = _approved([_spec("toolsy")])
    monkeypatch.setitem(sys.modules, f"{_PKG_NAMESPACE}.toolsy.tools", object())
    batch = ib.perform_install_batch(out.record.id)
    with db_transaction() as db:
        pi.set_status(
            db, out.record.token, pi.STATUS_INSTALLING, expect=pi.STATUS_APPROVED,
        )
        record = pi.mark_installed(db, out.record.token, result=batch.result)

    message = install_notify.render_install_message(record)

    assert "already imported in this process" in message
    assert "A platform restart is needed" in message
    assert "loaded live" not in message


def test_the_report_survives_the_round_trip_through_sql(
    installer_tables, fake_fetch,
):
    """``result_json`` is what the page and the message both read."""
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = _approved([_spec("alpha")])
    batch = ib.perform_install_batch(out.record.id)
    with db_transaction() as db:
        pi.set_status(
            db, out.record.token, pi.STATUS_INSTALLING, expect=pi.STATUS_APPROVED,
        )
        pi.mark_installed(db, out.record.token, result=batch.result)

    with db_connection() as db:
        stored = pi.get_request(db, out.record.token)

    assert stored.result["packages"][0]["load"]["loaded"] is True
    display = ib.result_for_display(stored.result)
    assert display["status"] == "installed"
    assert display["lines"] == ib.summarise(batch.result)
    assert display["error"] is None

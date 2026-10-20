"""Installing an approved batch: grants, hash re-verification, rollback.

Two layers are under test.  :func:`perform_install_batch` is the
synchronous worker-thread half: it re-verifies the bytes the operator
approved, installs each package in its own transaction with the grants
they ticked, and compensates when one of them fails.
``handle_package_install_approved`` is the async work-queue half: it
owns the row's status and must drop a re-dispatched item.

No network: the fetch dispatcher is replaced by a fixture that
materialises a tree in the quarantine directory it is handed, which is
the same contract WP1's ``fetch_source`` has.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter import config
from carpenter.db import db_connection, db_transaction, get_db
from carpenter.packages import install_batch as ib
from carpenter.packages import install_requests as ir
from carpenter.packages import installer
from carpenter.packages import pending_install as pi
from carpenter.packages.fetch import FetchError, FetchResult
from carpenter.packages.installer import (
    ensure_installer_tables,
    get_install_record,
    list_granted_capabilities,
)


# ── fixture packages ────────────────────────────────────────────────

ALPHA_MANIFEST = """\
name: alpha
version: "1.0.0"
description: |
  The first package of a batch.
  A second line nobody should see.
"""

BETA_MANIFEST = """\
name: beta
version: "2.0.0"
description: The second package of a batch.
"""

ALPHA_V2_MANIFEST = """\
name: alpha
version: "2.0.0"
description: The first package, upgraded.
"""

MAILER_MANIFEST = """\
name: mailer
version: "0.3.0"
description: Declares a platform capability.
platform_capabilities:
  - verb: mailer.fetch
    kind: egress
    module: handlers.imap
    handler: fetch
    grant:
      protocol: imap
      host_from: IMAP_HOST
      port: 993
      credential_ref: MAILER
credential_requirements:
  - kind: env
    provider: imap_smtp
    env_key_prefix: MAILER
    required_keys:
      - IMAP_HOST
      - PASSWORD
allowlist_proposals:
  - type: domain
    value: mail.example.com
"""

MAILER_FILES = {
    "handlers/__init__.py": "",
    "handlers/imap.py": """\
        def fetch(params, ctx):
            return {"ok": True}
    """,
}

WRITER_MANIFEST = """\
name: writer
version: "0.1.0"
description: Ships a write-capable chat tool.
chat_tools:
  - tools.py
"""

WRITER_FILES = {
    "tools.py": """\
        from carpenter.chat_tool_loader import chat_tool


        @chat_tool(
            description="Write a thing.",
            input_schema={"type": "object", "properties": {}},
            capabilities=["database_write"],
        )
        def writer_write(tool_input, **kwargs):
            return "done"
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
    """Materialise a fixture tree in the quarantine the caller passes.

    Same contract as WP1's dispatcher (a fresh directory under
    ``staging_root``, a tree holding ``manifest.yaml``, a ``resolved``
    pin) with no git, no HTTP and no egress check.
    """
    trees: dict[str, tuple[str, dict]] = {}
    calls: list = []

    def register(url_fragment: str, manifest_yaml: str, files=None):
        trees[url_fragment] = (manifest_yaml, files or {})

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


def _approved(sources, grants, **kw):
    """Build a request from ``sources`` and approve it with ``grants``."""
    out = ir.create_install_request(sources, **kw)
    with db_transaction() as db:
        pi.mark_approved(db, out.record.token, grants=grants)
    return out


def _reload_record(token):
    with db_connection() as db:
        return pi.get_request(db, token)


def _blocks(result):
    return {block["name"]: block for block in result["packages"]}


# ── the happy path ──────────────────────────────────────────────────

def test_both_packages_of_a_batch_install(installer_tables, fake_fetch):
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    fake_fetch.register("beta", BETA_MANIFEST)
    out = _approved(
        [_spec("alpha"), _spec("beta")],
        {"alpha": {}, "beta": {}},
    )

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is True
    assert batch.error is None
    blocks = _blocks(batch.result)
    assert blocks["alpha"]["status"] == ib.PKG_STATUS_INSTALLED
    assert blocks["beta"]["status"] == ib.PKG_STATUS_INSTALLED
    assert blocks["alpha"]["version"] == "1.0.0"
    assert len(blocks["alpha"]["hash"]) == 64
    assert blocks["alpha"]["hash_short"] == blocks["alpha"]["hash"][:12]

    with db_connection() as db:
        assert get_install_record(db, "alpha") is not None
        assert get_install_record(db, "beta") is not None


def test_the_destination_comes_from_the_installer_not_a_hardcoded_path(
    installer_tables, fake_fetch,
):
    """A relocated ``base_dir`` must be honoured (one definition, D24 SD2)."""
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = _approved([_spec("alpha")], {"alpha": {}})

    ib.perform_install_batch(out.record.id)

    expected = installer.default_install_dest("alpha")
    assert expected.parent == Path(config.CONFIG["base_dir"]) / "packages"
    with db_connection() as db:
        record = get_install_record(db, "alpha")
    assert Path(record["install_path"]) == expected


def test_provenance_is_recorded_from_the_approved_preview(
    installer_tables, fake_fetch,
):
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = _approved([_spec("alpha")], {"alpha": {}})
    previewed = out.preview[0]

    ib.perform_install_batch(out.record.id)

    with db_connection() as db:
        record = get_install_record(db, "alpha")
    assert record["source_kind"] == "git"
    assert record["source_url"] == previewed["url_redacted"]
    assert record["source_resolved"] == "c" * 40
    # The id a later request will compare against must not drift.
    assert record["source_id"] == previewed["source_id"]


# ── capability grants ───────────────────────────────────────────────

def test_a_ticked_grant_registers_the_declared_verbs(
    installer_tables, fake_fetch,
):
    fake_fetch.register("mailer", MAILER_MANIFEST, MAILER_FILES)
    out = _approved(
        [_spec("mailer")],
        {"mailer": {"capabilities": True, "write_chat_tools": False}},
    )

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is True
    block = _blocks(batch.result)["mailer"]
    assert block["capabilities_granted"] is True
    assert block["granted_verbs"] == ["mailer.fetch"]
    with db_connection() as db:
        assert [c["verb"] for c in list_granted_capabilities(db, "mailer")] == [
            "mailer.fetch",
        ]


def test_an_unticked_grant_still_installs_with_no_verbs(
    installer_tables, fake_fetch,
):
    """The key regression: a capability package IS installable from chat.

    ``confirm_platform_capabilities`` refuses a non-interactive install
    outright, so without the injected ``capability_input_fn`` this path
    could not install a capability-declaring package at all.  Declining
    the grant must install the package and register nothing.
    """
    fake_fetch.register("mailer", MAILER_MANIFEST, MAILER_FILES)
    out = _approved([_spec("mailer")], {"mailer": {"capabilities": False}})

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is True
    block = _blocks(batch.result)["mailer"]
    assert block["status"] == ib.PKG_STATUS_INSTALLED
    assert block["capabilities_granted"] is False
    assert block["granted_verbs"] == []
    with db_connection() as db:
        assert get_install_record(db, "mailer") is not None
        assert list_granted_capabilities(db, "mailer") == []


def test_a_missing_grants_block_grants_nothing(installer_tables, fake_fetch):
    """No entry for a package is "not granted", never "granted"."""
    fake_fetch.register("mailer", MAILER_MANIFEST, MAILER_FILES)
    out = _approved([_spec("mailer")], {})

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is True
    assert _blocks(batch.result)["mailer"]["granted_verbs"] == []


def test_the_consent_transcript_is_logged(installer_tables, fake_fetch, caplog):
    fake_fetch.register("mailer", MAILER_MANIFEST, MAILER_FILES)
    out = _approved([_spec("mailer")], {"mailer": {"capabilities": True}})

    with caplog.at_level("WARNING"):
        ib.perform_install_batch(out.record.id)

    transcripts = [
        r.getMessage() for r in caplog.records
        if "Capability consent transcript" in r.getMessage()
    ]
    assert transcripts, "the grant banner must be preserved as a consent record"
    assert "PLATFORM-LEVEL TRUST" in transcripts[0]
    assert "GRANTED" in transcripts[0]


def test_the_write_chat_tools_flag_is_carried_through(
    installer_tables, fake_fetch,
):
    fake_fetch.register("writer", WRITER_MANIFEST, WRITER_FILES)
    out = _approved([_spec("writer")], {"writer": {"write_chat_tools": True}})

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is True
    block = _blocks(batch.result)["writer"]
    assert block["write_chat_tools_requested"] is True
    assert block["write_chat_tools_allowed"] is True
    with db_connection() as db:
        record = get_install_record(db, "writer")
    assert record["write_chat_tools_allowed"] == 1


def test_an_unticked_write_flag_leaves_the_tools_gated(
    installer_tables, fake_fetch,
):
    fake_fetch.register("writer", WRITER_MANIFEST, WRITER_FILES)
    out = _approved([_spec("writer")], {"writer": {}})

    batch = ib.perform_install_batch(out.record.id)

    assert _blocks(batch.result)["writer"]["write_chat_tools_allowed"] is False
    with db_connection() as db:
        record = get_install_record(db, "writer")
    assert record["write_chat_tools_allowed"] == 0


# ── the bytes the operator approved ─────────────────────────────────

def test_a_changed_staging_tree_is_refused_and_never_refetched(
    installer_tables, fake_fetch,
):
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = _approved([_spec("alpha")], {"alpha": {}})
    fetches_before = len(fake_fetch.calls)
    tree = Path(out.preview[0]["tree_path"])
    (tree / "surprise.py").write_text("print('swapped in after approval')\n")

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is False
    assert "no longer matches the bytes that were approved" in batch.error
    assert out.preview[0]["tree_hash"][:12] in batch.error
    assert len(fake_fetch.calls) == fetches_before, "must never re-fetch"
    with db_connection() as db:
        assert get_install_record(db, "alpha") is None


def test_a_deleted_staging_tree_says_ask_again(installer_tables, fake_fetch):
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = _approved([_spec("alpha")], {"alpha": {}})
    shutil.rmtree(out.staging_root)

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is False
    assert "no longer on disk" in batch.error
    assert "ask again" in batch.error
    with db_connection() as db:
        assert get_install_record(db, "alpha") is None


def test_one_bad_tree_stops_the_whole_batch_before_anything_installs(
    installer_tables, fake_fetch,
):
    """Verification is batch-wide, so a bad tree needs no rollback."""
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    fake_fetch.register("beta", BETA_MANIFEST)
    out = _approved(
        [_spec("alpha"), _spec("beta")], {"alpha": {}, "beta": {}},
    )
    (Path(out.preview[1]["tree_path"]) / "extra").write_text("x")

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is False
    blocks = _blocks(batch.result)
    assert blocks["alpha"]["status"] == ib.PKG_STATUS_NOT_ATTEMPTED
    assert blocks["beta"]["status"] == ib.PKG_STATUS_NOT_ATTEMPTED
    with db_connection() as db:
        assert get_install_record(db, "alpha") is None
        assert get_install_record(db, "beta") is None


# ── compensating rollback (D5) ──────────────────────────────────────

@pytest.fixture
def fail_install_of(monkeypatch):
    """Make ``install_package`` raise for one package name only."""
    real = installer.install_package

    def _arm(name: str, message: str = "the installer blew up"):
        def wrapper(source_path, dest_path, **kwargs):
            if Path(dest_path).name == name:
                raise installer.InstallError(message)
            return real(source_path, dest_path, **kwargs)
        monkeypatch.setattr(installer, "install_package", wrapper)
    return _arm


def test_a_later_failure_uninstalls_what_this_batch_installed_fresh(
    installer_tables, fake_fetch, fail_install_of,
):
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    fake_fetch.register("beta", BETA_MANIFEST)
    out = _approved(
        [_spec("alpha"), _spec("beta")], {"alpha": {}, "beta": {}},
    )
    fail_install_of("beta")

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is False
    assert "beta failed to install" in batch.error
    blocks = _blocks(batch.result)
    assert blocks["alpha"]["status"] == ib.PKG_STATUS_ROLLED_BACK
    assert blocks["beta"]["status"] == ib.PKG_STATUS_FAILED
    assert batch.result["rolled_back"] == ["alpha"]
    assert "uninstalled again" in blocks["alpha"]["rollback"]
    with db_connection() as db:
        assert get_install_record(db, "alpha") is None
        assert get_install_record(db, "beta") is None
    assert not installer.default_install_dest("alpha").exists()


def test_an_upgrade_is_never_rolled_back(
    installer_tables, fake_fetch, fail_install_of, tmp_path,
):
    """Rolling an upgrade back would remove software the operator had."""
    # alpha v1 is already installed, by any route.
    first = _write_tree(tmp_path / "preinstalled" / "alpha", ALPHA_MANIFEST)
    with db_transaction() as db:
        installer.install_package(
            first, installer.default_install_dest("alpha"), conn=db,
        )

    fake_fetch.register("alpha", ALPHA_V2_MANIFEST)
    fake_fetch.register("beta", BETA_MANIFEST)
    out = _approved(
        [_spec("alpha"), _spec("beta")], {"alpha": {}, "beta": {}},
    )
    assert out.preview[0]["already_installed"]["version"] == "1.0.0"
    fail_install_of("beta")

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is False
    blocks = _blocks(batch.result)
    assert blocks["alpha"]["status"] == ib.PKG_STATUS_UPGRADED
    assert batch.result["rolled_back"] == []
    assert batch.result["not_rolled_back"] == [
        {"name": "alpha", "reason": "it was an upgrade, not a fresh install"},
    ]
    assert "cannot be rolled back" in blocks["alpha"]["rollback"]
    assert "v1.0.0" in blocks["alpha"]["rollback"]
    assert "archive cache" in blocks["alpha"]["rollback"]
    # Still installed, at the new version.
    with db_connection() as db:
        record = get_install_record(db, "alpha")
    assert record is not None
    assert record["version"] == "2.0.0"


def test_a_rollback_names_the_security_policies_it_leaves_behind(
    installer_tables, fake_fetch, fail_install_of,
):
    """SD5: uninstall does not retract merged allowlist entries."""
    fake_fetch.register("mailer", MAILER_MANIFEST, MAILER_FILES)
    fake_fetch.register("beta", BETA_MANIFEST)
    out = _approved(
        [_spec("mailer"), _spec("beta")],
        {"mailer": {"capabilities": True}, "beta": {}},
    )
    fail_install_of("beta")

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is False
    assert _blocks(batch.result)["mailer"]["status"] == ib.PKG_STATUS_ROLLED_BACK
    kept = batch.result["security_policies_kept"]
    assert {"type": "domain", "value": "mail.example.com",
            "package": "mailer"} in kept


def test_a_failed_first_package_leaves_the_rest_not_attempted(
    installer_tables, fake_fetch, fail_install_of,
):
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    fake_fetch.register("beta", BETA_MANIFEST)
    out = _approved(
        [_spec("alpha"), _spec("beta")], {"alpha": {}, "beta": {}},
    )
    fail_install_of("alpha")

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is False
    blocks = _blocks(batch.result)
    assert blocks["alpha"]["status"] == ib.PKG_STATUS_FAILED
    assert blocks["beta"]["status"] == ib.PKG_STATUS_NOT_ATTEMPTED
    assert batch.result["rolled_back"] == []
    with db_connection() as db:
        assert get_install_record(db, "beta") is None


# ── a request that may not be installed ─────────────────────────────

def test_an_unapproved_request_is_refused(installer_tables, fake_fetch):
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = ir.create_install_request([_spec("alpha")])

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is False
    assert "not approved" in batch.error
    with db_connection() as db:
        assert get_install_record(db, "alpha") is None


def test_a_rejected_request_is_refused(installer_tables, fake_fetch):
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = ir.create_install_request([_spec("alpha")])
    with db_transaction() as db:
        pi.mark_rejected(db, out.record.token)

    batch = ib.perform_install_batch(out.record.id)

    assert batch.ok is False
    assert "'rejected'" in batch.error


def test_an_unknown_request_id_is_refused(installer_tables):
    batch = ib.perform_install_batch(99999)
    assert batch.ok is False
    assert "no longer exists" in batch.error


# ── the report the operator is shown ────────────────────────────────

def test_the_summary_names_every_package_and_its_outcome(
    installer_tables, fake_fetch,
):
    fake_fetch.register("mailer", MAILER_MANIFEST, MAILER_FILES)
    out = _approved([_spec("mailer")], {"mailer": {"capabilities": True}})

    batch = ib.perform_install_batch(out.record.id)
    lines = ib.summarise(batch.result)

    assert len(lines) == 1
    line = lines[0]
    assert "installed mailer v0.3.0" in line
    assert batch.result["packages"][0]["hash_short"] in line
    assert "forge.invalid" in line
    assert "mailer.fetch" in line
    assert "loaded live" in line


def test_the_summary_says_when_no_verbs_were_granted(
    installer_tables, fake_fetch,
):
    fake_fetch.register("mailer", MAILER_MANIFEST, MAILER_FILES)
    out = _approved([_spec("mailer")], {"mailer": {"capabilities": False}})

    batch = ib.perform_install_batch(out.record.id)

    assert "no platform verbs granted" in ib.summarise(batch.result)[0]


# ── the async work handler (T7) ─────────────────────────────────────

def _work_items(event_type=None):
    with db_connection() as db:
        if event_type is None:
            rows = db.execute("SELECT * FROM work_queue ORDER BY id").fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM work_queue WHERE event_type = ? ORDER BY id",
                (event_type,),
            ).fetchall()
    return [dict(r) for r in rows]


def _system_messages(conv_id):
    with db_connection() as db:
        return [
            dict(r) for r in db.execute(
                "SELECT role, content, hidden FROM messages "
                "WHERE conversation_id = ? AND role = 'system' ORDER BY id",
                (conv_id,),
            ).fetchall()
        ]


async def test_the_handler_installs_an_approved_request(
    installer_tables, fake_fetch,
):
    """The real thread hop: the install opens its own transactions.

    This goes through ``run_in_work_pool``, so a transaction held across
    the hop would trip ``db.py``'s per-thread guard and fail the test.
    """
    from carpenter.agent import conversation
    from carpenter.packages import install_approved_handler as handler

    conv_id = conversation.create_conversation()
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = _approved([_spec("alpha")], {"alpha": {}}, conversation_id=conv_id)

    await handler.handle_package_install_approved(1, {"request_id": out.record.id})

    final = _reload_record(out.record.token)
    assert final.status == pi.STATUS_INSTALLED
    assert final.result["packages"][0]["status"] == ib.PKG_STATUS_INSTALLED
    assert final.attempts == 1
    assert final.finished_at
    with db_connection() as db:
        assert get_install_record(db, "alpha") is not None

    messages = _system_messages(conv_id)
    assert len(messages) == 1
    assert "installed alpha v1.0.0" in messages[0]["content"]
    assert messages[0]["hidden"] == 0


async def test_the_handler_records_a_failure_and_says_so_in_chat(
    installer_tables, fake_fetch, fail_install_of,
):
    from carpenter.agent import conversation
    from carpenter.packages import install_approved_handler as handler

    conv_id = conversation.create_conversation()
    fake_fetch.register("alpha", ALPHA_MANIFEST)
    fake_fetch.register("beta", BETA_MANIFEST)
    out = _approved(
        [_spec("alpha"), _spec("beta")], {"alpha": {}, "beta": {}},
        conversation_id=conv_id,
    )
    fail_install_of("beta")

    await handler.handle_package_install_approved(1, {"request_id": out.record.id})

    final = _reload_record(out.record.token)
    assert final.status == pi.STATUS_FAILED
    assert "beta failed to install" in final.error
    assert final.result["rolled_back"] == ["alpha"]
    message = _system_messages(conv_id)[0]["content"]
    assert "FAILED" in message
    assert "rolled back alpha" in message


async def test_an_installing_row_is_dropped(installer_tables, fake_fetch):
    """A work item re-dispatched after a restart must install nothing."""
    from carpenter.packages import install_approved_handler as handler

    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = _approved([_spec("alpha")], {"alpha": {}})
    with db_transaction() as db:
        pi.mark_installing(db, out.record.token)

    await handler.handle_package_install_approved(7, {"request_id": out.record.id})

    final = _reload_record(out.record.token)
    assert final.status == pi.STATUS_INSTALLING, "the handler must not touch it"
    assert final.attempts == 1, "the claim must not be bumped a second time"
    with db_connection() as db:
        assert get_install_record(db, "alpha") is None


async def test_an_undecided_request_is_dropped(installer_tables, fake_fetch):
    from carpenter.packages import install_approved_handler as handler

    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = ir.create_install_request([_spec("alpha")])

    await handler.handle_package_install_approved(7, {"request_id": out.record.id})

    assert _reload_record(out.record.token).status == pi.STATUS_REQUESTED
    with db_connection() as db:
        assert get_install_record(db, "alpha") is None


async def test_an_unknown_request_is_dropped(installer_tables):
    from carpenter.packages import install_approved_handler as handler

    await handler.handle_package_install_approved(7, {"request_id": 4242})


async def test_a_crashing_batch_still_fails_the_row(
    installer_tables, fake_fetch, monkeypatch,
):
    from carpenter.packages import install_approved_handler as handler

    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = _approved([_spec("alpha")], {"alpha": {}})

    async def boom(_request_id):
        raise RuntimeError("the worker pool is gone")

    monkeypatch.setattr(handler, "_run_batch", boom)

    await handler.handle_package_install_approved(1, {"request_id": out.record.id})

    final = _reload_record(out.record.token)
    assert final.status == pi.STATUS_FAILED
    assert "the worker pool is gone" in final.error


# ── the enqueue seam the decide route uses ──────────────────────────

def test_enqueueing_an_approved_install_is_idempotent(
    installer_tables, fake_fetch,
):
    from carpenter.packages import install_approved_handler as handler

    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = _approved([_spec("alpha")], {"alpha": {}})

    first = handler.enqueue_approved_install(out.record)
    second = handler.enqueue_approved_install(out.record)

    assert first is not None
    assert second is None, "a second POST must not queue a second install"
    items = _work_items(handler.EVENT_TYPE)
    assert len(items) == 1
    assert items[0]["idempotency_key"] == f"pkg_install:{out.record.id}"
    assert out.record.token not in items[0]["idempotency_key"]
    assert items[0]["max_retries"] == 1
    assert items[0]["priority"] == handler.PRIORITY


def test_the_queued_payload_carries_the_id_not_the_token(
    installer_tables, fake_fetch,
):
    import json

    from carpenter.packages import install_approved_handler as handler

    fake_fetch.register("alpha", ALPHA_MANIFEST)
    out = _approved([_spec("alpha")], {"alpha": {}})
    handler.enqueue_approved_install(out.record)

    payload = json.loads(_work_items(handler.EVENT_TYPE)[0]["payload_json"])
    assert payload == {"request_id": out.record.id}
    assert out.record.token not in json.dumps(payload)


def test_the_handler_is_registered_under_its_event_type():
    from carpenter.packages import install_approved_handler as handler

    registered = {}
    handler.register_handlers(lambda event, fn: registered.__setitem__(event, fn))
    assert registered == {
        "package.install.approved": handler.handle_package_install_approved,
    }


def test_the_coordinator_registers_the_handler():
    """The handler is useless unless the coordinator wires it up."""
    import inspect

    from carpenter import coordinator

    source = inspect.getsource(coordinator.Coordinator._register_work_handlers)
    assert "install_approved_handler" in source

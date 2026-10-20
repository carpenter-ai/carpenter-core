"""Building a package-install request: resolve, fetch, preview, record.

The security property under test is that a request can be built from
hostile bytes without running any of them, and that a refusal leaves
nothing behind — no row, no quarantine.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter import config
from carpenter.db import db_connection, get_db
from carpenter.packages import install_requests as ir
from carpenter.packages import pending_install as pi
from carpenter.packages.fetch import FetchError, FetchResult
from carpenter.packages.installer import ensure_installer_tables


HELLO_MANIFEST = """\
name: hello
version: "1.2.3"
description: |
  A reference package.
  Second line that must not reach the summary line.
chat_tools:
  - tools.py
"""

HELLO_TOOLS = """\
from carpenter.chat_tool_loader import chat_tool

RAN = open("/tmp/should-never-exist-wp5a", "w")  # noqa — never executed


@chat_tool(
    description="Read a thing.",
    input_schema={"type": "object", "properties": {}},
    capabilities=["filesystem_read"],
)
def hello_read(tool_input, **kwargs):
    return "hi"


@chat_tool(
    description="Write a thing.",
    input_schema={"type": "object", "properties": {}},
    capabilities=["database_write"],
)
def hello_write(tool_input, **kwargs):
    return "done"
"""

CAPS_MANIFEST = """\
name: mailer
version: "0.2.0"
description: Fetches mail.
platform_capabilities:
  - verb: imap.fetch
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

CAPS_FILES = {
    "handlers/imap.py": """\
    def fetch(ctx, **kwargs):
        return {}
    """,
}

# A manifest the security guards refuse: a chat-tool path that escapes
# the package root.
EVIL_MANIFEST = """\
name: evil
version: "0.1.0"
description: Reaches outside its own tree.
chat_tools:
  - ../../outside.py
"""


# ── a fetcher that never touches the network ────────────────────────

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

    Stands in for WP1's dispatcher: same contract (a fresh ``_fetch-*``
    directory inside ``staging_root``, a tree holding ``manifest.yaml``,
    a ``resolved`` pin), with no git, no HTTP and no egress.
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
        # Longest match wins, so a test may register a catch-all host
        # fragment alongside a per-repository one.
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
    """The installer tables in the autouse test database."""
    db = get_db()
    try:
        ensure_installer_tables(db)
    finally:
        db.close()


@pytest.fixture
def staging_root():
    return Path(config.CONFIG["base_dir"]) / "cache" / "package-staging"


def _git(name="repo"):
    return f"git+https://forge.invalid/org/{name}.git"


def _request(sources, **kw):
    return ir.create_install_request(sources, **kw)


def _hello(fake_fetch, fragment="forge.invalid"):
    """Register the reference package for every spec a test asks for."""
    fake_fetch.register(fragment, HELLO_MANIFEST, {"tools.py": HELLO_TOOLS})


# ── the happy path ──────────────────────────────────────────────────

def test_a_request_records_a_row_and_mints_a_url(
    installer_tables, fake_fetch, staging_root,
):
    _hello(fake_fetch)
    out = _request([_git()], conversation_id=None, request_text="install hello")

    assert out.record.status == pi.STATUS_REQUESTED
    assert out.url == f"/api/package-install/{out.token}"
    assert out.url_is_absolute is False
    assert out.package_names == ("hello",)

    with db_connection() as db:
        stored = pi.get_request(db, out.token)
    assert stored is not None
    assert stored.sources == (_git(),)
    assert stored.request_text == "install hello"
    assert stored.staging_root == str(out.staging_root)

    entry = stored.preview[0]
    assert entry["kind"] == "git"
    assert entry["resolved"] == "c" * 40
    assert len(entry["tree_hash"]) == 64
    assert entry["manifest"]["version"] == "1.2.3"
    assert entry["manifest"]["description_first_line"] == "A reference package."
    assert entry["manifest"]["counts"]["chat_tools"] == 1
    assert entry["already_installed"] is None
    assert entry["egress"] == {
        "host": "forge.invalid",
        "in_web_egress_allowlist": False,
        "allowlist_key": "web_egress_allowlist",
    }


def test_the_tree_lands_in_quarantine_and_stays_there(
    installer_tables, fake_fetch, staging_root,
):
    _hello(fake_fetch)
    out = _request([_git()])
    assert out.staging_root.parent == staging_root
    assert out.staging_root.name.startswith("req-")
    tree = Path(out.preview[0]["tree_path"])
    assert tree.is_dir() and (tree / "manifest.yaml").is_file()
    assert out.staging_root in tree.parents


def test_the_quarantine_is_not_named_after_the_token(
    installer_tables, fake_fetch,
):
    """The token is the credential; a path reaches logs and OS errors."""
    _hello(fake_fetch)
    out = _request([_git()])
    assert out.token not in str(out.staging_root)
    assert out.token[:8] not in str(out.staging_root)


def test_no_package_code_is_imported(installer_tables, fake_fetch):
    """The preview is a parse, not an import — the whole point.

    ``tools.py`` opens a file at module level; a preview that imported
    it would create that file and leave a ``_carpenter_pkg_*`` entry in
    ``sys.modules``.
    """
    _hello(fake_fetch)
    before = {m for m in sys.modules if m.startswith("_carpenter_pkg_")}
    out = _request([_git()])
    after = {m for m in sys.modules if m.startswith("_carpenter_pkg_")}
    assert after == before
    assert not Path("/tmp/should-never-exist-wp5a").exists()

    tools = {t["name"]: t for t in out.preview[0]["chat_tools"]}
    assert set(tools) == {"hello_read", "hello_write"}
    assert tools["hello_write"]["is_write_capable"] is True
    assert tools["hello_read"]["is_write_capable"] is False
    assert out.preview[0]["write_consent_required"] is True
    assert out.preview[0]["chat_tools_read_statically"] is True
    assert out.preview[0]["chat_tool_modules"] == ["tools.py"]


def test_capabilities_credentials_and_allowlist_reach_the_preview(
    installer_tables, fake_fetch,
):
    fake_fetch.register("mail", CAPS_MANIFEST, CAPS_FILES)
    entry = _request([_git("mail")]).preview[0]

    cap = entry["capabilities"][0]
    assert cap["verb"] == "imap.fetch" and cap["kind"] == "egress"
    assert cap["grant"]["port"] == 993
    assert "TRUSTED handler handlers.imap:fetch" in cap["describe"]

    cred = entry["credentials"][0]
    assert cred["env_vars"] == ["MAILER_IMAP_HOST", "MAILER_PASSWORD"]
    assert "password" not in str(cred).lower() or "PASSWORD" in str(cred)

    assert entry["allowlist_proposals"] == [
        {"policy_type": "domain", "value": "mail.example.com"},
    ]


def test_a_credential_in_the_url_is_redacted_everywhere(
    installer_tables, fake_fetch,
):
    _hello(fake_fetch)
    spec = "git+https://user:hunter2@forge.invalid/org/repo.git"
    entry = _request([spec]).preview[0]
    assert "hunter2" not in entry["url_redacted"]
    assert "hunter2" not in str(entry)


def test_an_already_installed_package_is_flagged(
    installer_tables, fake_fetch,
):
    db = get_db()
    try:
        db.execute(
            "INSERT INTO installed_packages (name, version, hash, "
            "source_path, install_path, installed_at) "
            "VALUES ('hello', '1.0.0', 'deadbeef', '/s', '/i', 'then')",
        )
        db.commit()
    finally:
        db.close()
    _hello(fake_fetch)
    prior = _request([_git()]).preview[0]["already_installed"]
    assert prior["version"] == "1.0.0" and prior["hash"] == "deadbeef"


def test_two_sources_make_one_request(installer_tables, fake_fetch):
    _hello(fake_fetch, "repo")
    fake_fetch.register("mail", CAPS_MANIFEST, CAPS_FILES)
    out = _request([_git(), _git("mail")])
    assert out.package_names == ("hello", "mailer")
    assert len(out.record.sources) == 2
    trees = [Path(p["tree_path"]) for p in out.preview]
    assert trees[0] != trees[1]
    assert all(out.staging_root in t.parents for t in trees)


def test_public_base_url_makes_the_link_absolute(
    installer_tables, fake_fetch, monkeypatch,
):
    monkeypatch.setitem(
        config.CONFIG, "public_base_url", "https://platform.invalid/",
    )
    _hello(fake_fetch)
    out = _request([_git()])
    assert out.url == f"https://platform.invalid/api/package-install/{out.token}"
    assert out.url_is_absolute is True


# ── refusals ────────────────────────────────────────────────────────

def _assert_nothing_written(staging_root: Path):
    with db_connection() as db:
        assert pi.list_requests(db) == []
    leftovers = [
        p for p in staging_root.glob("req-*")
    ] if staging_root.is_dir() else []
    assert leftovers == []


@pytest.mark.parametrize("bad", [
    [], "git+https://forge.invalid/o/r.git", {"a": 1}, [""], ["  "], [None], [7],
])
def test_bad_sources_are_refused(installer_tables, staging_root, bad):
    with pytest.raises(ir.InstallRequestRefused):
        _request(bad)
    _assert_nothing_written(staging_root)


def test_too_many_sources_is_refused(installer_tables, staging_root):
    with pytest.raises(ir.InstallRequestRefused, match="at most 5"):
        _request([_git(str(i)) for i in range(6)])
    _assert_nothing_written(staging_root)


def test_a_local_path_is_refused(installer_tables, staging_root, tmp_path):
    with pytest.raises(ir.InstallRequestRefused, match="local path"):
        _request([str(tmp_path)])
    _assert_nothing_written(staging_root)


def test_an_unparseable_spec_is_refused(installer_tables, staging_root):
    with pytest.raises(ir.InstallRequestRefused, match="not a usable source"):
        _request(["git+ssh://forge.invalid/o/r.git"])
    _assert_nothing_written(staging_root)


def test_the_same_source_twice_is_refused(installer_tables, staging_root):
    with pytest.raises(ir.InstallRequestRefused, match="same source"):
        _request([_git(), _git()])
    _assert_nothing_written(staging_root)


def test_two_sources_providing_one_name_are_refused(
    installer_tables, fake_fetch, staging_root,
):
    _hello(fake_fetch, "one")
    _hello(fake_fetch, "two")
    with pytest.raises(ir.InstallRequestRefused, match="both provide"):
        _request([_git("one"), _git("two")])
    _assert_nothing_written(staging_root)


def test_a_fetch_failure_is_refused_and_cleans_up(
    installer_tables, fake_fetch, staging_root,
):
    """The egress refusal arrives this way, naming the config key."""
    def refusing(spec, *, limits=None, staging_root=None):
        raise FetchError(
            "refusing to fetch package source from host 'forge.invalid': the "
            "platform will not fetch from it. An operator must add the host "
            "to the web_egress_allowlist config key to allow it.",
        )

    _hello(fake_fetch)
    import carpenter.packages.install_requests as mod
    mod.fetch_source = refusing
    with pytest.raises(ir.InstallRequestRefused) as exc:
        _request([_git()])
    assert "web_egress_allowlist" in str(exc.value)
    _assert_nothing_written(staging_root)


def test_a_security_rejected_manifest_leaves_no_row_and_no_tree(
    installer_tables, fake_fetch, staging_root,
):
    fake_fetch.register("repo", EVIL_MANIFEST)
    with pytest.raises(ir.InstallRequestRefused, match="security guards"):
        _request([_git()])
    _assert_nothing_written(staging_root)


def test_a_malformed_manifest_leaves_no_row(
    installer_tables, fake_fetch, staging_root,
):
    fake_fetch.register("repo", "name: [not, a, string]\n")
    with pytest.raises(
        ir.InstallRequestRefused, match="usable capability package",
    ):
        _request([_git()])
    _assert_nothing_written(staging_root)


def test_the_second_source_failing_cleans_up_the_first(
    installer_tables, fake_fetch, staging_root,
):
    _hello(fake_fetch, "good")
    fake_fetch.register("bad", EVIL_MANIFEST)
    with pytest.raises(ir.InstallRequestRefused):
        _request([_git("good"), _git("bad")])
    _assert_nothing_written(staging_root)


# ── rate limits ─────────────────────────────────────────────────────

def test_too_many_open_requests_is_refused(
    installer_tables, fake_fetch, monkeypatch,
):
    monkeypatch.setitem(
        config.CONFIG, "package_install", {"max_open_requests": 2},
    )
    _hello(fake_fetch)
    _request([_git("a")])
    _request([_git("b")])
    with pytest.raises(ir.InstallRequestRefused, match="waiting"):
        _request([_git("c")])


def test_a_decided_request_does_not_count_against_the_open_limit(
    installer_tables, fake_fetch, monkeypatch,
):
    monkeypatch.setitem(
        config.CONFIG, "package_install",
        {"max_open_requests": 1, "max_requests_per_hour": 10},
    )
    _hello(fake_fetch)
    first = _request([_git("a")])
    with db_connection() as db:
        pi.mark_rejected(db, first.token)
        db.commit()
    assert _request([_git("b")]).record.status == pi.STATUS_REQUESTED


def test_the_hourly_limit_is_refused(
    installer_tables, fake_fetch, monkeypatch,
):
    monkeypatch.setitem(
        config.CONFIG, "package_install",
        {"max_open_requests": 50, "max_requests_per_hour": 2},
    )
    _hello(fake_fetch)
    _request([_git("a")])
    _request([_git("b")])
    with pytest.raises(ir.InstallRequestRefused, match="last hour"):
        _request([_git("c")])


def test_a_rejected_source_cannot_be_asked_for_again(
    installer_tables, fake_fetch, monkeypatch,
):
    monkeypatch.setitem(
        config.CONFIG, "package_install",
        {"max_open_requests": 50, "max_requests_per_hour": 50},
    )
    _hello(fake_fetch)
    first = _request([_git()])
    with db_connection() as db:
        pi.mark_rejected(db, first.token)
        db.commit()
    with pytest.raises(ir.InstallRequestRefused, match="rejected"):
        _request([_git()])


def test_an_approved_source_can_be_asked_for_again(
    installer_tables, fake_fetch, monkeypatch,
):
    monkeypatch.setitem(
        config.CONFIG, "package_install",
        {"max_open_requests": 50, "max_requests_per_hour": 50},
    )
    _hello(fake_fetch)
    first = _request([_git()])
    with db_connection() as db:
        pi.mark_approved(db, first.token, grants={})
        db.commit()
    assert _request([_git()]).record.status == pi.STATUS_REQUESTED


def test_limits_fall_back_when_the_config_is_junk(installer_tables, fake_fetch):
    assert ir.limits_for({"package_install": {"max_fetch_bytes": "lots"}}) \
        == ir.FetchLimits()
    assert ir.limits_for({"package_install": "nonsense"}) == ir.FetchLimits()
    tight = ir.limits_for({"package_install": {"max_fetch_files": 3}})
    assert tight.max_files == 3


# ── attribution ─────────────────────────────────────────────────────

def test_the_triggering_message_and_channel_are_captured(
    installer_tables, fake_fetch,
):
    from carpenter.agent import conversation

    conv_id = conversation.create_conversation()
    conversation.add_message(conv_id, "user", "please install hello")
    conversation.add_message(conv_id, "assistant", "sure")
    db = get_db()
    try:
        db.execute(
            "INSERT INTO channel_bindings (channel_type, channel_user_id, "
            "conversation_id) VALUES ('signal', 'someone', ?)", (conv_id,),
        )
        db.commit()
    finally:
        db.close()

    _hello(fake_fetch)
    out = _request([_git()], conversation_id=conv_id)
    assert out.record.request_text == "please install hello"
    assert out.record.channel_type == "signal"
    assert out.record.conversation_id == conv_id


def test_no_conversation_is_not_an_error(installer_tables, fake_fetch):
    _hello(fake_fetch)
    out = _request([_git()])
    assert out.record.conversation_id is None
    assert out.record.channel_type is None
    assert out.record.request_text is None


def test_a_hostile_request_text_is_stored_verbatim(
    installer_tables, fake_fetch,
):
    """Escaping is the renderer's job; the record must stay faithful."""
    hostile = "<script>alert(1)</script> install hello"
    _hello(fake_fetch)
    out = _request([_git()], request_text=hostile)
    with db_connection() as db:
        assert pi.get_request(db, out.token).request_text == hostile


# ── a caller-supplied connection ────────────────────────────────────

def test_a_caller_may_supply_its_own_connection(fake_fetch, tmp_path):
    """Used by the install-batch and test paths that own a transaction."""
    conn = sqlite3.connect(":memory:")
    ensure_installer_tables(conn)
    _hello(fake_fetch)
    out = _request([_git()], conn=conn)
    assert pi.get_request(conn, out.token).id == out.record.id
    conn.close()
    shutil.rmtree(out.staging_root, ignore_errors=True)

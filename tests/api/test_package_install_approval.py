"""The operator approval page and the decide route.

The security properties under test: the page shows everything a human
needs to notice "I never asked for this", it cannot be used to change
what gets installed, hostile strings in a manifest or a chat message
cannot escape into the HTML, a GET never approves anything, and a token
buys exactly one decision.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from textwrap import dedent

import pytest
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.testclient import TestClient

from carpenter import config
from carpenter.api import package_install
from carpenter.api.auth import TokenAuthMiddleware
from carpenter.api.http import http_exception_handler
from carpenter.db import db_connection, get_db
from carpenter.packages import install_requests as ir
from carpenter.packages import installer
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

# Declares a platform capability, a credential requirement and an
# allowlist proposal.  ``allowlist_proposals`` entries use the key
# ``type`` and must name a known policy type (``domain``) — a manifest
# saying ``policy_type`` or ``domain_allowlist`` does not load at all.
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

# Everything an attacker controls, in every field that reaches the page.
HOSTILE_MANIFEST = """\
name: hostile
version: "0.0.1"
description: >-
  <script>alert('desc')</script> & "quoted" <img src=x onerror=alert(1)>
"""

HOSTILE_REQUEST_TEXT = (
    "please install <script>alert('xss')</script> & \"this\" <b>now</b>"
)


# ── fixtures ────────────────────────────────────────────────────────

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

    The same stand-in for the fetch dispatcher that
    ``tests/packages/test_install_requests.py`` uses: no git, no HTTP,
    no egress.  Building the request through the real
    :func:`create_install_request` means the previews these tests render
    are the real thing, so a change to the preview's shape breaks these
    tests rather than silently emptying the page.
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
    return fake_fetch_source


@pytest.fixture
def installer_tables():
    db = get_db()
    try:
        ensure_installer_tables(db)
    finally:
        db.close()


@pytest.fixture
def client():
    app = Starlette(routes=package_install.routes)
    app.add_exception_handler(HTTPException, http_exception_handler)
    return TestClient(app)


def _git(name="repo"):
    return f"git+https://forge.invalid/org/{name}.git"


def _request(sources, **kw):
    return ir.create_install_request(sources, **kw)


def _hello(fake_fetch, fragment="forge.invalid"):
    fake_fetch.register(fragment, HELLO_MANIFEST, {"tools.py": HELLO_TOOLS})


def _checked_inputs(page: str) -> list[str]:
    """Every ``<input>`` tag that arrives already ticked.

    Checked by tag, not by substring: the page embeds JavaScript that
    reads ``box.checked``, so a bare "checked not in page" would pass
    for the wrong reason.
    """
    return [
        tag for tag in re.findall(r"<input[^>]*>", page)
        if "checked" in tag
    ]


def _status(token: str) -> str:
    with db_connection() as db:
        record = pi.get_request(db, token)
    return record.status


def _work_items(event_type=package_install.INSTALL_APPROVED_EVENT):
    with db_connection() as db:
        rows = db.execute(
            "SELECT id, payload_json, idempotency_key, max_retries, priority "
            "FROM work_queue WHERE event_type = ?",
            (event_type,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── the page ────────────────────────────────────────────────────────

def test_the_page_renders_full_provenance(installer_tables, fake_fetch, client):
    _hello(fake_fetch)
    out = _request([_git()], conversation_id=11, channel_type="web",
                   request_text="install hello please")
    entry = out.record.preview[0]

    resp = client.get(f"/api/package-install/{out.token}")
    assert resp.status_code == 200
    page = resp.text

    assert entry["tree_hash"] in page
    assert entry["tree_hash"][:12] in page
    assert entry["url_redacted"] in page
    assert entry["source_id"] in page
    assert entry["resolved"] in page
    assert entry["fetched_at"] in page
    assert "4096" in page and "forge.invalid" in page
    assert "web_egress_allowlist" in page
    assert "hello" in page and "1.2.3" in page
    assert "A reference package." in page
    # The second description line is deliberately not the summary line.
    assert "Second line that must not reach" not in page


def test_attribution_is_the_first_block(installer_tables, fake_fetch, client):
    _hello(fake_fetch)
    out = _request([_git()], conversation_id=42, channel_type="signal",
                   request_text="install hello please")

    page = client.get(f"/api/package-install/{out.token}").text
    attribution = page.index("install hello please")
    assert attribution < page.index("Provenance")
    assert attribution < page.index("Tree hash")
    assert attribution < page.index("<input")
    assert "#42" in page
    assert "signal" in page
    assert "If you do not recognise it, reject." in page


def test_a_request_with_no_conversation_says_so(
    installer_tables, fake_fetch, client,
):
    _hello(fake_fetch)
    out = _request([_git()], conversation_id=None, request_text=None)
    page = client.get(f"/api/package-install/{out.token}").text
    assert "No conversation was recorded" in page
    assert "no triggering message was recorded" in page


def test_capabilities_are_rendered_loudly(installer_tables, fake_fetch, client):
    fake_fetch.register("forge.invalid", CAPS_MANIFEST, CAPS_FILES)
    out = _request([_git("mail")])

    page = client.get(f"/api/package-install/{out.token}").text
    assert "PLATFORM-LEVEL TRUST" in page
    assert "CANNOT be sandboxed" in page
    assert "platform-side" in page
    assert "egress and" in page
    # installer._describe_capability's wording, verbatim.
    assert "verb &#x27;imap.fetch&#x27; (kind=egress)" in page
    assert "handlers.imap:fetch" in page
    assert "imap://&lt;MAILER_IMAP_HOST&gt;:993" in page
    # The all-or-nothing grant box exists and is unticked.
    assert 'name="grant_capabilities[mailer]"' in page
    assert _checked_inputs(page) == []


def test_the_capability_banner_matches_the_installer_wording():
    """The page and the terminal consent prompt must say the same thing.

    Two wordings for one grant is how an operator approves on one
    surface what they would have refused on the other, so the page
    copies ``confirm_platform_capabilities``' sentences.  This asserts
    they are still that function's sentences.
    """
    source = inspect.getsource(installer.confirm_platform_capabilities)
    for line in package_install.CAPABILITY_BANNER_LINES:
        assert line in source, line
    for line in package_install.CAPABILITY_CONSENT_LINES:
        assert line in source, line


def test_no_capability_checkbox_when_none_are_declared(
    installer_tables, fake_fetch, client,
):
    _hello(fake_fetch)
    out = _request([_git()])
    page = client.get(f"/api/package-install/{out.token}").text
    assert "grant_capabilities[" not in page
    assert "declares no" in page
    # The write-tools box is always offered.
    assert 'name="allow_write_chat_tools[hello]"' in page


def test_allowlist_proposals_carry_the_ratchet_warning(
    installer_tables, fake_fetch, client,
):
    fake_fetch.register("forge.invalid", CAPS_MANIFEST, CAPS_FILES)
    out = _request([_git("mail")])
    page = client.get(f"/api/package-install/{out.token}").text
    assert "mail.example.com" in page
    assert "One-way ratchet" in page
    assert "does NOT remove" in page


def test_credentials_are_variable_names_and_never_values(
    installer_tables, fake_fetch, client, monkeypatch,
):
    fake_fetch.register("forge.invalid", CAPS_MANIFEST, CAPS_FILES)
    # A live value for exactly the variable the manifest asks for.  It
    # must not reach the page: the manifest declares names, and the page
    # renders the manifest.
    monkeypatch.setitem(config.CONFIG, "MAILER_PASSWORD", "s3cr3t-value")
    out = _request([_git("mail")])

    page = client.get(f"/api/package-install/{out.token}").text
    assert "MAILER_IMAP_HOST" in page
    assert "MAILER_PASSWORD" in page
    assert "s3cr3t-value" not in page
    assert "never ships credentials" in page


def test_chat_tools_say_the_package_code_was_not_executed(
    installer_tables, fake_fetch, client,
):
    _hello(fake_fetch)
    out = _request([_git()])
    page = client.get(f"/api/package-install/{out.token}").text
    assert "hello_read" in page and "hello_write" in page
    assert "own code was" in page and "not executed" in page
    assert "WRITE-CAPABLE" in page


def test_an_unreadable_chat_tool_is_named_as_unresolved(
    installer_tables, fake_fetch, client,
):
    fake_fetch.register(
        "forge.invalid", HELLO_MANIFEST, {"tools.py": "def broken( :\n"},
    )
    out = _request([_git()])
    assert out.record.preview[0]["chat_tools_unresolved"]
    page = client.get(f"/api/package-install/{out.token}").text
    assert "Could not be read statically" in page
    assert "unknown, not safe" in page


def test_an_already_installed_package_shows_the_prior_version(
    installer_tables, fake_fetch, client,
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
    out = _request([_git()])
    page = client.get(f"/api/package-install/{out.token}").text
    assert "Already installed" in page
    assert "1.0.0" in page and "deadbeef" in page


def test_the_page_has_one_approve_one_reject_and_no_input_surface(
    installer_tables, fake_fetch, client,
):
    _hello(fake_fetch, "repo")
    fake_fetch.register("mail", CAPS_MANIFEST, CAPS_FILES)
    out = _request([_git(), _git("mail")])

    page = client.get(f"/api/package-install/{out.token}").text
    assert page.count("decide('approve')") == 1
    assert page.count("decide('reject')") == 1
    # Two packages, so two write-tools boxes plus one capability box —
    # and nothing else, because a checkbox is the only control the page
    # is allowed to have.
    inputs = re.findall(r"<input[^>]*>", page)
    assert len(inputs) == 3
    assert all('type="checkbox"' in tag for tag in inputs)
    assert _checked_inputs(page) == []
    # Nothing that could change the destination, name, spec or hash.
    assert "<textarea" not in page
    assert "<form" not in page
    assert "<select" not in page


def test_hostile_strings_cannot_escape_into_the_html(
    installer_tables, fake_fetch, client,
):
    fake_fetch.register("forge.invalid", HOSTILE_MANIFEST)
    out = _request([_git()], request_text=HOSTILE_REQUEST_TEXT)

    page = client.get(f"/api/package-install/{out.token}").text
    # The page's own <script> tag is the only one; neither payload got
    # its own.
    assert page.count("<script>") == 1
    assert "<script>alert" not in page
    assert "</script>" not in page.replace("</script>\n</body>", "", 1)
    assert "alert('xss')" not in page
    assert "alert(&#x27;xss&#x27;)" in page
    assert "&lt;script&gt;" in page
    assert "<img src=x" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page
    assert "&amp;" in page and "&quot;quoted&quot;" in page


def test_an_unknown_token_is_404(installer_tables, client):
    resp = client.get("/api/package-install/0000-not-a-token")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_a_get_mutates_nothing(installer_tables, fake_fetch, client):
    _hello(fake_fetch)
    out = _request([_git()])
    client.get(f"/api/package-install/{out.token}")
    client.get(f"/api/package-install/{out.token}")
    assert _status(out.token) == pi.STATUS_REQUESTED
    assert _work_items() == []


def test_the_full_token_is_never_logged(
    installer_tables, fake_fetch, client, caplog,
):
    _hello(fake_fetch)
    out = _request([_git()])
    with caplog.at_level("INFO"):
        client.get(f"/api/package-install/{out.token}")
        client.post(
            f"/api/package-install/{out.token}/decide",
            json={"decision": "reject"},
        )
    logged = "\n".join(
        r.getMessage() for r in caplog.records
        if r.name.startswith("carpenter")
    )
    assert out.token not in logged
    assert out.record.token_prefix in logged


# ── deciding ────────────────────────────────────────────────────────

def test_approve_flips_the_row_writes_grants_and_enqueues_once(
    installer_tables, fake_fetch, client,
):
    fake_fetch.register("forge.invalid", CAPS_MANIFEST, CAPS_FILES)
    out = _request([_git("mail")])

    resp = client.post(
        f"/api/package-install/{out.token}/decide",
        json={
            "decision": "approve",
            "grants": {"mailer": {
                "capabilities": True, "write_chat_tools": False,
            }},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == pi.STATUS_APPROVED
    assert body["queued"] is True

    with db_connection() as db:
        record = pi.get_request(db, out.token)
    assert record.status == pi.STATUS_APPROVED
    assert record.decided_at
    assert record.grants == {
        "mailer": {"capabilities": True, "write_chat_tools": False},
    }

    items = _work_items()
    assert len(items) == 1
    item = items[0]
    assert item["idempotency_key"] == f"pkg_install:{out.record.id}"
    assert out.record.token not in item["idempotency_key"]
    assert item["max_retries"] == 1
    assert item["priority"] == 50
    payload = json.loads(item["payload_json"])
    assert payload["request_id"] == record.id
    assert payload["packages"] == ["mailer"]
    # The credential does not go in the queue table.
    assert out.token not in item["payload_json"]
    assert payload["token_prefix"] == record.token_prefix


def test_approve_with_nothing_ticked_grants_nothing(
    installer_tables, fake_fetch, client,
):
    fake_fetch.register("forge.invalid", CAPS_MANIFEST, CAPS_FILES)
    out = _request([_git("mail")])
    client.post(
        f"/api/package-install/{out.token}/decide", json={"decision": "approve"},
    )
    with db_connection() as db:
        record = pi.get_request(db, out.token)
    assert record.grants == {
        "mailer": {"capabilities": False, "write_chat_tools": False},
    }


def test_grants_come_from_the_row_not_the_browser(
    installer_tables, fake_fetch, client,
):
    """A posted grant for an unknown package, or for capabilities a
    package never declared, is discarded."""
    _hello(fake_fetch)
    out = _request([_git()])
    client.post(
        f"/api/package-install/{out.token}/decide",
        json={
            "decision": "approve",
            "grants": {
                "hello": {"capabilities": True, "write_chat_tools": True},
                "evil-other-package": {"capabilities": True},
            },
        },
    )
    with db_connection() as db:
        record = pi.get_request(db, out.token)
    assert list(record.grants) == ["hello"]
    # hello declares no platform_capabilities, so the grant is forced off.
    assert record.grants["hello"] == {
        "capabilities": False, "write_chat_tools": True,
    }


def test_a_second_decision_is_refused(installer_tables, fake_fetch, client):
    _hello(fake_fetch)
    out = _request([_git()])
    first = client.post(
        f"/api/package-install/{out.token}/decide", json={"decision": "approve"},
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/package-install/{out.token}/decide", json={"decision": "reject"},
    )
    assert second.status_code == 410
    assert out.token not in second.text
    assert _status(out.token) == pi.STATUS_APPROVED
    assert len(_work_items()) == 1


def test_reject_cleans_the_quarantine_and_enqueues_nothing(
    installer_tables, fake_fetch, client,
):
    _hello(fake_fetch)
    out = _request([_git()])
    staging = Path(out.record.staging_root)
    assert staging.is_dir()

    resp = client.post(
        f"/api/package-install/{out.token}/decide", json={"decision": "reject"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == pi.STATUS_REJECTED
    assert _status(out.token) == pi.STATUS_REJECTED
    assert not staging.exists()
    assert _work_items() == []


def test_reject_leaves_a_path_outside_the_quarantine_alone(
    installer_tables, fake_fetch, client, tmp_path,
):
    """A corrupted ``staging_root`` must not turn reject into an rmtree."""
    _hello(fake_fetch)
    out = _request([_git()])
    elsewhere = tmp_path / "not-quarantine"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("keep")
    with db_connection() as db:
        db.execute(
            "UPDATE pending_package_installs SET staging_root = ? "
            "WHERE token = ?",
            (str(elsewhere), out.token),
        )
        db.commit()

    client.post(
        f"/api/package-install/{out.token}/decide", json={"decision": "reject"},
    )
    assert (elsewhere / "keep.txt").exists()


def test_a_decided_row_renders_a_status_page(
    installer_tables, fake_fetch, client,
):
    _hello(fake_fetch)
    out = _request([_git()])
    client.post(
        f"/api/package-install/{out.token}/decide", json={"decision": "approve"},
    )

    resp = client.get(f"/api/package-install/{out.token}")
    assert resp.status_code == 200
    page = resp.text
    assert "Status:" in page
    assert pi.STATUS_APPROVED in page
    assert "already been decided" in page
    # No decision controls on a spent link.
    assert "decide('approve')" not in page
    assert "<input" not in page
    assert "<script" not in page


def test_a_rejected_row_renders_a_status_page(
    installer_tables, fake_fetch, client,
):
    _hello(fake_fetch)
    out = _request([_git()])
    client.post(
        f"/api/package-install/{out.token}/decide", json={"decision": "reject"},
    )
    page = client.get(f"/api/package-install/{out.token}").text
    assert pi.STATUS_REJECTED in page


def test_an_expired_row_is_410_and_marked_expired(
    installer_tables, fake_fetch, client,
):
    _hello(fake_fetch)
    out = _request([_git()], now="2000-01-01T00:00:00+00:00")
    assert _status(out.token) == pi.STATUS_REQUESTED

    resp = client.get(f"/api/package-install/{out.token}")
    assert resp.status_code == 410
    assert "expired" in resp.json()["detail"]
    assert _status(out.token) == pi.STATUS_EXPIRED

    # And it stays gone, with no status page offering a decision.
    again = client.get(f"/api/package-install/{out.token}")
    assert again.status_code == 410


def test_deciding_an_expired_row_is_410(installer_tables, fake_fetch, client):
    _hello(fake_fetch)
    out = _request([_git()], now="2000-01-01T00:00:00+00:00")
    resp = client.post(
        f"/api/package-install/{out.token}/decide", json={"decision": "approve"},
    )
    assert resp.status_code == 410
    assert _status(out.token) == pi.STATUS_EXPIRED
    assert _work_items() == []


def test_deciding_an_unknown_token_is_404(installer_tables, client):
    resp = client.post(
        "/api/package-install/0000-not-a-token/decide",
        json={"decision": "approve"},
    )
    assert resp.status_code == 404


@pytest.mark.parametrize("body", [
    {},
    {"decision": "maybe"},
    {"decision": "install"},
    {"decision": None},
])
def test_a_decision_that_is_not_approve_or_reject_is_400(
    installer_tables, fake_fetch, client, body,
):
    _hello(fake_fetch)
    out = _request([_git()])
    resp = client.post(f"/api/package-install/{out.token}/decide", json=body)
    assert resp.status_code == 400
    assert _status(out.token) == pi.STATUS_REQUESTED


def test_a_non_object_body_is_400(installer_tables, fake_fetch, client):
    _hello(fake_fetch)
    out = _request([_git()])
    resp = client.post(f"/api/package-install/{out.token}/decide", json=["approve"])
    assert resp.status_code == 400


def test_the_batch_is_approved_whole(installer_tables, fake_fetch, client):
    """Two packages, one decision, one work item."""
    _hello(fake_fetch, "repo")
    fake_fetch.register("mail", CAPS_MANIFEST, CAPS_FILES)
    out = _request([_git(), _git("mail")])

    client.post(
        f"/api/package-install/{out.token}/decide",
        json={"decision": "approve", "grants": {
            "mailer": {"capabilities": True},
        }},
    )
    with db_connection() as db:
        record = pi.get_request(db, out.token)
    assert sorted(record.grants) == ["hello", "mailer"]
    items = _work_items()
    assert len(items) == 1
    assert json.loads(items[0]["payload_json"])["packages"] == ["hello", "mailer"]


def test_the_approval_survives_a_restart(installer_tables, fake_fetch, client):
    """The row is the only state, so a fresh client serves the same page.

    No module-level dict holds the request, which is why there is no
    ``recover_*`` function in this module.
    """
    _hello(fake_fetch)
    out = _request([_git()])
    # No module-level mutable store: api/review.py needs recover_review_links
    # because it has one, and this module deliberately does not.
    assert [
        name for name, value in vars(package_install).items()
        if isinstance(value, dict) and not name.startswith("__")
    ] == []
    fresh = TestClient(Starlette(routes=package_install.routes))
    resp = fresh.get(f"/api/package-install/{out.token}")
    assert resp.status_code == 200
    assert out.record.preview[0]["tree_hash"] in resp.text


# ── the decide route needs platform authentication (F3) ─────────────

UI_TOKEN = "ui-token-for-the-signed-in-operator"


@pytest.fixture
def gated_client(monkeypatch):
    """A client behind the real token middleware, with a UI token set.

    The plain ``client`` fixture mounts the routes bare, which is how
    the rest of this file tests the page itself.  These tests are about
    the gate, so they need the middleware the server actually installs.
    """
    monkeypatch.setitem(config.CONFIG, "ui_token", UI_TOKEN)
    app = Starlette(routes=package_install.routes)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_middleware(TokenAuthMiddleware)
    return TestClient(app)


def test_the_approval_page_stays_open_without_a_ui_token(
    installer_tables, fake_fetch, gated_client,
):
    """A human must still be able to read what was requested."""
    _hello(fake_fetch)
    out = _request([_git()])
    resp = gated_client.get(f"/api/package-install/{out.token}")
    assert resp.status_code == 200
    assert out.record.preview[0]["tree_hash"] in resp.text


def test_an_unauthenticated_post_cannot_approve(
    installer_tables, fake_fetch, gated_client,
):
    """F3: holding the link must not be enough to approve the install."""
    _hello(fake_fetch)
    out = _request([_git()])

    resp = gated_client.post(
        f"/api/package-install/{out.token}/decide",
        json={"decision": "approve"},
    )
    assert resp.status_code == 401
    assert _status(out.token) == pi.STATUS_REQUESTED
    assert _work_items() == []


def test_the_request_token_alone_is_not_sufficient_to_approve(
    installer_tables, fake_fetch, gated_client,
):
    """The approval token is not a platform credential.

    Presenting it where the platform token goes — the ``token`` query
    parameter and the bearer header — must not approve anything, which
    is what makes a leaked link (it lives in the conversation that
    asked) worth less than a signed-in session.
    """
    _hello(fake_fetch)
    out = _request([_git()])

    query = gated_client.post(
        f"/api/package-install/{out.token}/decide?token={out.token}",
        json={"decision": "approve"},
    )
    header = gated_client.post(
        f"/api/package-install/{out.token}/decide",
        json={"decision": "approve"},
        headers={"Authorization": f"Bearer {out.token}"},
    )
    assert query.status_code == 401
    assert header.status_code == 401
    assert _status(out.token) == pi.STATUS_REQUESTED
    assert _work_items() == []


def test_an_authenticated_post_approves(
    installer_tables, fake_fetch, gated_client,
):
    _hello(fake_fetch)
    out = _request([_git()])

    resp = gated_client.post(
        f"/api/package-install/{out.token}/decide",
        json={"decision": "approve"},
        headers={"Authorization": f"Bearer {UI_TOKEN}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == pi.STATUS_APPROVED
    assert _status(out.token) == pi.STATUS_APPROVED
    assert len(_work_items()) == 1


def test_an_unauthenticated_post_cannot_reject_either(
    installer_tables, fake_fetch, gated_client,
):
    """Rejection deletes the quarantine, so it is a decision too."""
    _hello(fake_fetch)
    out = _request([_git()])
    resp = gated_client.post(
        f"/api/package-install/{out.token}/decide",
        json={"decision": "reject"},
    )
    assert resp.status_code == 401
    assert _status(out.token) == pi.STATUS_REQUESTED


def test_the_page_says_approving_needs_a_signed_in_session(
    installer_tables, fake_fetch, client,
):
    _hello(fake_fetch)
    out = _request([_git()])
    page = client.get(f"/api/package-install/{out.token}").text
    assert "signed in" in page
    assert "Reading this page needs only the link" in page


def test_the_page_forwards_the_ui_token_only_to_a_signed_in_viewer(
    installer_tables, fake_fetch, monkeypatch, client,
):
    """The page must not hand the platform token to whoever has the link."""
    monkeypatch.setitem(config.CONFIG, "ui_token", UI_TOKEN)
    _hello(fake_fetch)
    out = _request([_git()])

    anonymous = client.get(f"/api/package-install/{out.token}").text
    assert UI_TOKEN not in anonymous

    signed_in = client.get(
        f"/api/package-install/{out.token}?token={UI_TOKEN}",
    ).text
    assert UI_TOKEN in signed_in


def test_a_wrong_token_in_the_page_url_is_not_echoed(
    installer_tables, fake_fetch, monkeypatch, client,
):
    monkeypatch.setitem(config.CONFIG, "ui_token", UI_TOKEN)
    _hello(fake_fetch)
    out = _request([_git()])
    page = client.get(
        f"/api/package-install/{out.token}?token=</script><script>x",
    ).text
    assert "<script>x" not in page

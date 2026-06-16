"""Tests for the package-capability framework.

Covers the FRAMEWORK only (not any imap/smtp handler):

* Manifest: valid ``platform_capabilities`` parses; bad verb / verb
  collision with PLATFORM_TOOLS / duplicate verb / missing module-handler
  / malformed grant / unknown keys all raise.
* Install: interactive grant → recorded + in InstallResult; declined →
  not granted; NON-interactive with capabilities declared → InstallError,
  nothing granted.
* Loader: granted verb registered + invokable through dispatch returning
  JSON; a declared-but-not-confirmed capability is NOT registered.
* Context: ``ctx.secret(ref)`` resolves platform-side and the value is NOT
  placed in the executor env.
* Allow-list: the package's verb is permitted for its own arcs and
  rejected for a different package's arc.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from carpenter.packages.manifest import ManifestError, load_manifest


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "example_capability_pkg"


# ── Helpers ─────────────────────────────────────────────────────────


def _write_cap_pkg(
    root: Path,
    *,
    name: str = "capdemo",
    manifest_extra: str = "",
    handler_body: str | None = None,
    verb: str = "demo.echo",
    cred_prefix: str = "DEMO_MAIL",
) -> Path:
    """Write a minimal capability package with one egress verb."""
    pkg = root / name
    (pkg / "handlers").mkdir(parents=True, exist_ok=True)
    if handler_body is None:
        handler_body = (
            "def handle(params, ctx):\n"
            "    has_pw = False\n"
            "    try:\n"
            "        has_pw = bool(ctx.secret('PASSWORD'))\n"
            "    except Exception:\n"
            "        has_pw = False\n"
            "    return {\n"
            "        'echo': params,\n"
            "        'host': ctx.host,\n"
            "        'port': ctx.port,\n"
            "        'protocol': ctx.protocol,\n"
            "        'has_password': has_pw,\n"
            "    }\n"
        )
    (pkg / "handlers" / "echo.py").write_text(handler_body)
    (pkg / "handlers" / "__init__.py").write_text("")
    manifest = dedent(f"""
        name: {name}
        version: "0.1.0"
        description: Capability framework test package.
        credential_requirements:
          - kind: env
            provider: demo
            env_key_prefix: {cred_prefix}
            required_keys:
              - HOST
              - PORT
              - PASSWORD
        platform_capabilities:
          - verb: {verb}
            kind: egress
            module: handlers.echo
            handler: handle
            grant:
              protocol: demo
              host_from: HOST
              port: 993
              credential_ref: {cred_prefix}
    """) + manifest_extra
    (pkg / "manifest.yaml").write_text(manifest)
    return pkg


# ── Manifest validation ─────────────────────────────────────────────


class TestManifest:
    def test_valid_platform_capabilities_parse(self, tmp_path):
        pkg = _write_cap_pkg(tmp_path)
        m = load_manifest(pkg / "manifest.yaml")
        assert len(m.platform_capabilities) == 1
        cap = m.platform_capabilities[0]
        assert cap.verb == "demo.echo"
        assert cap.kind == "egress"
        assert cap.module == "handlers.echo"
        assert cap.handler == "handle"
        assert cap.grant.protocol == "demo"
        assert cap.grant.host_from == "HOST"
        assert cap.grant.port == 993
        assert cap.grant.credential_ref == "DEMO_MAIL"

    def test_in_repo_example_fixture_parses(self):
        m = load_manifest(FIXTURE_DIR / "manifest.yaml")
        assert m.name == "example_capability"
        assert [c.verb for c in m.platform_capabilities] == ["example.echo"]

    def test_bad_verb_shape_raises(self, tmp_path):
        # No dot — not a namespace.verb.
        pkg = _write_cap_pkg(tmp_path, verb="echo")
        # Rewrite manifest verb (host_from cross-check still ok).
        with pytest.raises(ManifestError, match="verb must match"):
            load_manifest(pkg / "manifest.yaml")

    def test_verb_collision_with_builtin_dispatch_raises(self, tmp_path):
        # ``web.get`` is a built-in dispatch verb; a capability may not
        # shadow it.  (PLATFORM_TOOLS names are bare and can't match the
        # dotted verb shape, so the realistic collision target is the
        # dotted _DISPATCH verbs.)
        pkg = _write_cap_pkg(tmp_path, verb="web.get")
        with pytest.raises(ManifestError, match="collides with a built-in"):
            load_manifest(pkg / "manifest.yaml")

    def test_duplicate_verb_raises(self, tmp_path):
        # A second capability list entry with the same verb (4-space
        # indentation matching the dedented manifest the helper writes).
        extra = (
            "  - verb: demo.echo\n"
            "    kind: egress\n"
            "    module: handlers.echo\n"
            "    handler: handle\n"
            "    grant:\n"
            "      protocol: demo\n"
            "      host_from: HOST\n"
            "      port: 993\n"
            "      credential_ref: DEMO_MAIL\n"
        )
        pkg = _write_cap_pkg(tmp_path)
        text = (pkg / "manifest.yaml").read_text() + extra
        (pkg / "manifest.yaml").write_text(text)
        with pytest.raises(ManifestError, match="duplicate verb"):
            load_manifest(pkg / "manifest.yaml")

    def test_missing_handler_key_raises(self, tmp_path):
        pkg = _write_cap_pkg(tmp_path)
        text = (pkg / "manifest.yaml").read_text().replace(
            "    handler: handle\n", "",
        )
        (pkg / "manifest.yaml").write_text(text)
        with pytest.raises(ManifestError, match="missing required keys"):
            load_manifest(pkg / "manifest.yaml")

    def test_missing_module_file_raises(self, tmp_path):
        pkg = _write_cap_pkg(tmp_path)
        (pkg / "handlers" / "echo.py").unlink()
        with pytest.raises(ManifestError, match="source file"):
            load_manifest(pkg / "manifest.yaml")

    def test_malformed_grant_bad_port_raises(self, tmp_path):
        pkg = _write_cap_pkg(tmp_path)
        text = (pkg / "manifest.yaml").read_text().replace(
            "port: 993", "port: 99999",
        )
        (pkg / "manifest.yaml").write_text(text)
        with pytest.raises(ManifestError, match="port must be an integer"):
            load_manifest(pkg / "manifest.yaml")

    def test_grant_credential_ref_not_declared_raises(self, tmp_path):
        pkg = _write_cap_pkg(tmp_path)
        text = (pkg / "manifest.yaml").read_text().replace(
            "credential_ref: DEMO_MAIL", "credential_ref: NOPE_PREFIX",
        )
        (pkg / "manifest.yaml").write_text(text)
        with pytest.raises(ManifestError, match="does not name a declared"):
            load_manifest(pkg / "manifest.yaml")

    def test_grant_host_from_not_in_required_keys_raises(self, tmp_path):
        pkg = _write_cap_pkg(tmp_path)
        text = (pkg / "manifest.yaml").read_text().replace(
            "host_from: HOST", "host_from: NOTAKEY",
        )
        (pkg / "manifest.yaml").write_text(text)
        with pytest.raises(ManifestError, match="not one of credential"):
            load_manifest(pkg / "manifest.yaml")

    def test_unknown_key_in_capability_raises(self, tmp_path):
        pkg = _write_cap_pkg(tmp_path)
        text = (pkg / "manifest.yaml").read_text().replace(
            "    handler: handle\n",
            "    handler: handle\n    bogus: 1\n",
        )
        (pkg / "manifest.yaml").write_text(text)
        with pytest.raises(ManifestError, match="unknown keys"):
            load_manifest(pkg / "manifest.yaml")

    def test_unknown_kind_raises(self, tmp_path):
        pkg = _write_cap_pkg(tmp_path)
        text = (pkg / "manifest.yaml").read_text().replace(
            "kind: egress", "kind: telepathy",
        )
        (pkg / "manifest.yaml").write_text(text)
        with pytest.raises(ManifestError, match="kind must be one of"):
            load_manifest(pkg / "manifest.yaml")


# ── Install-time trust acknowledgment ───────────────────────────────


@pytest.fixture
def cap_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    from carpenter.packages.installer import ensure_installer_tables
    ensure_installer_tables(conn)
    yield conn
    conn.close()


class TestInstallTrustAck:
    def test_interactive_grant_recorded(self, tmp_path, cap_db):
        from carpenter.packages.installer import (
            install_package,
            list_granted_capabilities,
        )
        src = _write_cap_pkg(tmp_path / "src")
        dest = tmp_path / "installed" / "capdemo"
        result = install_package(
            src, dest, conn=cap_db,
            capability_input_fn=lambda _prompt: "yes",
        )
        assert len(result.platform_capabilities_granted) == 1
        assert result.platform_capabilities_granted[0]["verb"] == "demo.echo"
        recorded = list_granted_capabilities(cap_db, "capdemo")
        assert [c["verb"] for c in recorded] == ["demo.echo"]

    def test_declined_not_granted(self, tmp_path, cap_db):
        from carpenter.packages.installer import (
            install_package,
            list_granted_capabilities,
        )
        src = _write_cap_pkg(tmp_path / "src")
        dest = tmp_path / "installed" / "capdemo"
        result = install_package(
            src, dest, conn=cap_db,
            capability_input_fn=lambda _prompt: "no",
        )
        # Package still installs, but no capability is granted.
        assert result.platform_capabilities_granted == ()
        assert list_granted_capabilities(cap_db, "capdemo") == []
        # Install record exists (the package was installed).
        from carpenter.packages.installer import get_install_record
        assert get_install_record(cap_db, "capdemo") is not None

    def test_non_interactive_with_capabilities_fails(self, tmp_path, cap_db):
        from carpenter.packages.installer import InstallError, install_package
        src = _write_cap_pkg(tmp_path / "src")
        dest = tmp_path / "installed" / "capdemo"
        # No input_fn AND stdin is not a tty (test runner) → must fail.
        with pytest.raises(InstallError, match="PLATFORM-LEVEL TRUST"):
            install_package(src, dest, conn=cap_db)
        # Nothing materialised, nothing granted.
        assert not dest.exists()
        from carpenter.packages.installer import get_install_record
        assert get_install_record(cap_db, "capdemo") is None

    def test_package_without_capabilities_no_prompt(self, tmp_path, cap_db):
        """A package with no platform_capabilities installs without consent."""
        from carpenter.packages.installer import install_package
        pkg = tmp_path / "src" / "plain"
        pkg.mkdir(parents=True)
        (pkg / "manifest.yaml").write_text(dedent("""
            name: plain
            version: "0.1.0"
            description: No capabilities here.
        """))
        dest = tmp_path / "installed" / "plain"
        # No input_fn, no tty — but no capabilities, so no failure.
        result = install_package(pkg, dest, conn=cap_db)
        assert result.platform_capabilities_granted == ()


# ── Loader registration + dispatch ──────────────────────────────────


@pytest.fixture
def reset_cap_registry():
    from carpenter.packages.capabilities import get_capability_registry
    get_capability_registry().reset()
    yield
    get_capability_registry().reset()


class TestLoader:
    def test_granted_verb_registers_and_dispatches(
        self, tmp_path, monkeypatch, reset_cap_registry,
    ):
        from carpenter.packages.loaders import load_platform_capabilities
        from carpenter.packages.capabilities import get_capability_registry

        pkg = _write_cap_pkg(tmp_path)
        m = load_manifest(pkg / "manifest.yaml")
        # The egress host is resolved platform-side from the credential.
        monkeypatch.setenv("DEMO_MAIL_HOST", "imap.example.com")
        monkeypatch.setenv("DEMO_MAIL_PASSWORD", "s3cr3t")

        n, errs = load_platform_capabilities(
            m, granted_verbs=frozenset({"demo.echo"}),
        )
        assert n == 1, errs
        assert errs == []

        reg = get_capability_registry()
        assert reg.is_capability_verb("demo.echo")
        # Dispatch returns JSON-serialisable dict with confirmed scope.
        out = reg.dispatch("demo.echo", {"q": 1})
        assert out["echo"] == {"q": 1}
        assert out["host"] == "imap.example.com"
        assert out["port"] == 993
        assert out["protocol"] == "demo"
        # Secret resolved platform-side (value NOT returned).
        assert out["has_password"] is True
        assert "s3cr3t" not in str(out)

    def test_declared_but_not_granted_not_registered(
        self, tmp_path, monkeypatch, reset_cap_registry,
    ):
        from carpenter.packages.loaders import load_platform_capabilities
        from carpenter.packages.capabilities import get_capability_registry

        pkg = _write_cap_pkg(tmp_path)
        m = load_manifest(pkg / "manifest.yaml")
        monkeypatch.setenv("DEMO_MAIL_HOST", "imap.example.com")

        # Empty granted set → nothing registered.
        n, errs = load_platform_capabilities(m, granted_verbs=frozenset())
        assert n == 0
        assert not get_capability_registry().is_capability_verb("demo.echo")

    def test_handler_path_classified_t1(
        self, tmp_path, monkeypatch, reset_cap_registry,
    ):
        from carpenter.packages.loaders import load_platform_capabilities
        from carpenter.security.platform_paths import path_tier, PATH_TIER_T1

        pkg = _write_cap_pkg(tmp_path)
        m = load_manifest(pkg / "manifest.yaml")
        monkeypatch.setenv("DEMO_MAIL_HOST", "imap.example.com")
        load_platform_capabilities(m, granted_verbs=frozenset({"demo.echo"}))

        handler_file = pkg / "handlers" / "echo.py"
        assert path_tier(str(handler_file)) == PATH_TIER_T1


# ── CapabilityContext.secret resolution ─────────────────────────────


class TestCapabilityContext:
    def test_secret_resolves_platform_side(self, monkeypatch):
        from carpenter.packages.capabilities import CapabilityContext
        ctx = CapabilityContext(
            package_name="p", verb="x.y", kind="egress",
            protocol="imap", host="h", port=993, credential_ref="DEMO_MAIL",
        )
        monkeypatch.setenv("DEMO_MAIL_PASSWORD", "hunter2")
        assert ctx.secret("PASSWORD") == "hunter2"

    def test_secret_missing_raises(self, monkeypatch):
        from carpenter.packages.capabilities import (
            CapabilityContext,
            CapabilityError,
        )
        monkeypatch.delenv("DEMO_MAIL_NOPE", raising=False)
        ctx = CapabilityContext(
            package_name="p", verb="x.y", kind="egress",
            protocol="imap", host="h", port=993, credential_ref="DEMO_MAIL",
        )
        with pytest.raises(CapabilityError, match="not set platform-side"):
            ctx.secret("NOPE")

    def _imap_ctx(self, package_name="carpenter-imap-email"):
        from carpenter.packages.capabilities import CapabilityContext
        return CapabilityContext(
            package_name=package_name, verb="imap.fetch", kind="egress",
            protocol="imap", host="mail.example.com", port=993,
            credential_ref="IMAP_EMAIL",
        )

    def _write_package_env(self, base_dir, package_name, body):
        """Write a chmod-600 per-package .env under a tmp base_dir."""
        pkg_dir = base_dir / "config" / "packages" / package_name
        pkg_dir.mkdir(parents=True, exist_ok=True)
        env_path = pkg_dir / ".env"
        env_path.write_text(body, encoding="utf-8")
        env_path.chmod(0o600)
        return env_path

    def test_secret_resolves_from_per_package_env(self, tmp_path, monkeypatch):
        # The real-world case: env-credentialed package mailbox creds live
        # in the per-package .env, NOT os.environ or the main config.
        monkeypatch.delenv("IMAP_EMAIL_IMAP_USERNAME", raising=False)
        monkeypatch.delenv("IMAP_EMAIL_IMAP_PASSWORD", raising=False)
        monkeypatch.setattr(
            "carpenter.config.CONFIG", {"base_dir": str(tmp_path)},
        )
        self._write_package_env(
            tmp_path, "carpenter-imap-email",
            "IMAP_EMAIL_IMAP_USERNAME=alice@example.com\n"
            "IMAP_EMAIL_IMAP_PASSWORD=s3cr3t\n",
        )
        ctx = self._imap_ctx()
        assert ctx.secret("IMAP_USERNAME") == "alice@example.com"
        assert ctx.secret("IMAP_PASSWORD") == "s3cr3t"

    def test_per_package_env_parses_comments_and_blanks(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.delenv("IMAP_EMAIL_IMAP_USERNAME", raising=False)
        monkeypatch.setattr(
            "carpenter.config.CONFIG", {"base_dir": str(tmp_path)},
        )
        self._write_package_env(
            tmp_path, "carpenter-imap-email",
            "# mailbox credentials\n"
            "\n"
            "  IMAP_EMAIL_IMAP_USERNAME = bob@example.com  \n",
        )
        ctx = self._imap_ctx()
        assert ctx.secret("IMAP_USERNAME") == "bob@example.com"

    def test_per_package_env_isolation_across_packages(
        self, tmp_path, monkeypatch,
    ):
        # A context for a DIFFERENT package must NOT read another package's
        # per-package .env, even with the same credential_ref/key.
        monkeypatch.delenv("IMAP_EMAIL_IMAP_USERNAME", raising=False)
        monkeypatch.setattr(
            "carpenter.config.CONFIG", {"base_dir": str(tmp_path)},
        )
        self._write_package_env(
            tmp_path, "carpenter-imap-email",
            "IMAP_EMAIL_IMAP_USERNAME=alice@example.com\n",
        )
        from carpenter.packages.capabilities import CapabilityError
        other = self._imap_ctx(package_name="some-other-package")
        with pytest.raises(CapabilityError, match="not set platform-side"):
            other.secret("IMAP_USERNAME")

    def test_os_environ_takes_precedence_over_per_package_env(
        self, tmp_path, monkeypatch,
    ):
        # Existing behavior preserved: live process env wins.
        monkeypatch.setattr(
            "carpenter.config.CONFIG", {"base_dir": str(tmp_path)},
        )
        self._write_package_env(
            tmp_path, "carpenter-imap-email",
            "IMAP_EMAIL_IMAP_USERNAME=from-file@example.com\n",
        )
        monkeypatch.setenv("IMAP_EMAIL_IMAP_USERNAME", "from-env@example.com")
        ctx = self._imap_ctx()
        assert ctx.secret("IMAP_USERNAME") == "from-env@example.com"

    def test_per_package_env_missing_key_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("IMAP_EMAIL_IMAP_PASSWORD", raising=False)
        monkeypatch.setattr(
            "carpenter.config.CONFIG", {"base_dir": str(tmp_path)},
        )
        self._write_package_env(
            tmp_path, "carpenter-imap-email",
            "IMAP_EMAIL_IMAP_USERNAME=alice@example.com\n",
        )
        from carpenter.packages.capabilities import CapabilityError
        ctx = self._imap_ctx()
        with pytest.raises(CapabilityError, match="not set platform-side"):
            ctx.secret("IMAP_PASSWORD")

    def test_package_name_traversal_rejected(self, tmp_path, monkeypatch):
        # A package_name containing path-traversal must never be used to
        # build a path that escapes the per-package directory.  Plant a
        # secret at a sibling location the traversal would reach and prove
        # it is NOT read.
        monkeypatch.delenv("IMAP_EMAIL_IMAP_USERNAME", raising=False)
        monkeypatch.setattr(
            "carpenter.config.CONFIG", {"base_dir": str(tmp_path)},
        )
        # Where "../victim/.env" from config/packages/ would land:
        victim_dir = tmp_path / "config" / "victim"
        victim_dir.mkdir(parents=True, exist_ok=True)
        (victim_dir / ".env").write_text(
            "IMAP_EMAIL_IMAP_USERNAME=leaked@example.com\n",
        )
        from carpenter.packages.capabilities import (
            CapabilityError,
            _package_env_path,
        )
        # The path builder rejects unsafe names outright.
        assert _package_env_path("../victim") is None
        assert _package_env_path("a/b") is None
        assert _package_env_path("..") is None
        ctx = self._imap_ctx(package_name="../victim")
        with pytest.raises(CapabilityError, match="not set platform-side"):
            ctx.secret("IMAP_USERNAME")


# ── Allow-list: per-package arc scoping via dispatch bridge ─────────


class TestAllowList:
    """Exercise the per-package gate in validate_and_dispatch.

    The autouse ``test_db`` fixture (tests/conftest.py) provides a full
    DB with arcs + arc_state + execution_sessions tables.
    """

    def _register_verb(self, package_name, verb):
        from carpenter.packages.capabilities import get_capability_registry
        from carpenter.packages.manifest import EgressGrant
        reg = get_capability_registry()
        reg.register(
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

    def _make_arc(self, agent_type="EXECUTOR", capabilities=None):
        import json
        from carpenter.db import db_connection
        with db_connection() as db:
            cur = db.execute(
                "INSERT INTO arcs (name, integrity_level, agent_type, "
                "status) VALUES (?, 'trusted', ?, 'active')",
                ("t", agent_type),
            )
            arc_id = cur.lastrowid
            if capabilities:
                db.execute(
                    "INSERT INTO arc_state (arc_id, key, value_json) "
                    "VALUES (?, '_capabilities', ?)",
                    (arc_id, json.dumps(capabilities)),
                )
            db.commit()
        return arc_id

    def _session_for(self, arc_id):
        """Create a reviewed arc-step execution session for the arc."""
        import uuid
        from datetime import datetime, timedelta, timezone
        from carpenter.db import db_connection
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

    def test_verb_permitted_for_owning_package_arc(self, reset_cap_registry):
        from carpenter.executor.dispatch_bridge import validate_and_dispatch
        self._register_verb("capdemo", "demo.echo")
        arc_id = self._make_arc(capabilities=["pkg.capdemo"])
        sid = self._session_for(arc_id)
        out = validate_and_dispatch(
            "demo.echo", {"hello": 1},
            session_id=sid, arc_id=arc_id,
        )
        assert out == {"ok": True, "pkg": "capdemo"}

    def test_verb_rejected_for_other_package_arc(self, reset_cap_registry):
        from carpenter.executor.dispatch_bridge import (
            DispatchError,
            validate_and_dispatch,
        )
        self._register_verb("capdemo", "demo.echo")
        # Arc carries a DIFFERENT package's grant.
        arc_id = self._make_arc(capabilities=["pkg.otherpkg"])
        sid = self._session_for(arc_id)
        with pytest.raises(DispatchError, match="own arcs"):
            validate_and_dispatch(
                "demo.echo", {"hello": 1},
                session_id=sid, arc_id=arc_id,
            )

    def test_verb_rejected_for_arc_without_grant(self, reset_cap_registry):
        from carpenter.executor.dispatch_bridge import (
            DispatchError,
            validate_and_dispatch,
        )
        self._register_verb("capdemo", "demo.echo")
        arc_id = self._make_arc(capabilities=None)
        sid = self._session_for(arc_id)
        with pytest.raises(DispatchError, match="own arcs"):
            validate_and_dispatch(
                "demo.echo", {"hello": 1},
                session_id=sid, arc_id=arc_id,
            )

    def test_spoofed_caller_arc_id_in_params_is_overridden(
        self, reset_cap_registry,
    ):
        """An untrusted EXECUTOR script cannot spoof ``_caller_arc_id``.

        ``_caller_arc_id`` is a platform-injected caller-identity field, not
        a legitimate tool argument. The trusted ``arc_id`` argument (the real
        executing arc) must always win. Here arc A (the real caller) lacks the
        owning package's grant while arc B holds it; a malicious script
        pre-sets ``_caller_arc_id`` to B in its dispatch params to masquerade
        as the granted arc. The per-package gate must still DENY because the
        bridge overrides ``_caller_arc_id`` with the trusted ``arc_id`` (A).
        """
        from carpenter.executor.dispatch_bridge import (
            DispatchError,
            validate_and_dispatch,
        )
        self._register_verb("capdemo", "demo.echo")
        arc_a = self._make_arc(capabilities=None)            # real caller, no grant
        arc_b = self._make_arc(capabilities=["pkg.capdemo"])  # granted arc
        sid = self._session_for(arc_a)
        with pytest.raises(DispatchError, match="own arcs"):
            validate_and_dispatch(
                "demo.echo",
                {"hello": 1, "_caller_arc_id": arc_b},  # spoofed identity
                session_id=sid, arc_id=arc_a,
            )

"""Tests for installing a capability package from a REMOTE source.

``python3 -m carpenter packages install <spec>`` accepts the whole
source grammar, not just a local directory.  These tests drive the real
command function against real transports and assert the three things
that make a CLI remote install trustworthy:

* the bytes come through :func:`carpenter.packages.fetch.fetch_source`,
  so the egress gate, the caps and the quarantine all apply;
* the quarantine is gone when the command returns, whatever it returned;
* the install row records provenance the same way the approved-install
  path does, so ``packages update`` can re-fetch what the CLI installed.

Hermetic throughout.  A git source is a real bare-ish repository served
by dulwich's own WSGI app over loopback; an archive source is a
``.tar.gz`` served by ``http.server`` on loopback.  Nothing reaches the
network: where a test needs the egress guard to refuse, it substitutes
the guard's own resolver seam rather than letting a name reach DNS.

The source grammar accepts plain ``http://`` only for a loopback
literal, and the egress guard refuses ``127.0.0.1`` unless it is listed
in ``web_egress_allowlist`` — so these tests set that key, which is
exactly what an operator with a self-hosted forge on a private address
has to do.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from textwrap import dedent
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest
from dulwich import porcelain
from dulwich.repo import Repo
from dulwich.server import DictBackend
from dulwich.web import make_wsgi_chain

from carpenter import cli_packages, config
from carpenter.db import db_connection, db_transaction
from carpenter.packages import archive_cache, fetch, installer
from carpenter.security import egress

from .conftest import write_package

LOOPBACK = "127.0.0.1"
_AUTHOR = b"Fixture <fixture@example.invalid>"

MANIFEST = dedent("""\
    name: demo
    version: "0.1.0"
    description: A remote-install fixture package.
""")

#: A package declaring platform capabilities, so the consent gate is the
#: thing that stops a non-interactive install after the fetch.
CAP_MANIFEST = dedent("""\
    name: demo
    version: "0.1.0"
    description: A remote fixture declaring a trusted egress capability.
    credential_requirements:
      - kind: env
        provider: demo
        env_key_prefix: DEMO_MAIL
        required_keys:
          - HOST
          - PORT
          - PASSWORD
    platform_capabilities:
      - verb: demo.echo
        kind: egress
        module: handlers.echo
        handler: handle_echo
        grant:
          protocol: demo
          host_from: HOST
          port: 1234
          credential_ref: DEMO_MAIL
""")

CAP_FILES = {
    "handlers/__init__.py": "",
    "handlers/echo.py": (
        "def handle_echo(params, ctx):\n"
        "    return {'echo': params}\n"
    ),
}


# ── the platform under test ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def _installer_tables(test_db):
    """Create the installer tables on the per-test DB.

    The cached template DB is built with ``skip_migrations=True``, so
    this applies the same idempotent DDL the migration runs.
    """
    with db_transaction() as db:
        installer.ensure_installer_tables(db)


@pytest.fixture(autouse=True)
def allow_loopback_egress(monkeypatch):
    """Allowlist the loopback literal the fixture servers bind to."""
    monkeypatch.setitem(config.CONFIG, "web_egress_allowlist", [LOOPBACK])
    monkeypatch.setitem(config.CONFIG, "package_sources", [])


@pytest.fixture
def staging_root() -> Path:
    """The quarantine root the CLI's fetches will use.

    ``base_dir`` is the per-test temp directory, so this is isolated
    without the CLI being told anything test-specific — the assertion
    that it is empty afterwards is about production behaviour.
    """
    return fetch.staging_root_dir()


def _quarantined(staging_root: Path) -> list[str]:
    """Names of everything left under the quarantine root."""
    return sorted(p.name for p in staging_root.iterdir())


@pytest.fixture
def non_interactive(monkeypatch):
    """Make stdin look like a pipe, as a scripted operator run does."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    monkeypatch.setattr(
        "builtins.input",
        lambda *a, **k: pytest.fail("a non-interactive run must not prompt"),
    )


# ── the loopback git server ─────────────────────────────────────────


def _commit_all(root: Path, message: bytes = b"fixture commit") -> str:
    """Stage everything under ``root`` and commit it; return the sha40."""
    paths = [
        str(p) for p in sorted(root.rglob("*"))
        if ".git" not in p.parts and not p.is_dir()
    ]
    porcelain.add(str(root), paths=paths)
    sha = porcelain.commit(
        str(root), message=message, author=_AUTHOR, committer=_AUTHOR,
    )
    return sha.decode()


def _branch_of(root: Path) -> str:
    """The repository's current branch name.

    Read rather than assumed: which name a fresh repository's first
    branch gets is the git implementation's business, and a test that
    hardcodes one breaks when that changes.
    """
    return porcelain.active_branch(str(root)).decode()


class _QuietHandler(WSGIRequestHandler):
    """A request handler that does not write to the test's stderr."""

    def log_message(self, *args, **kwargs):  # noqa: D102
        pass


@pytest.fixture
def git_server():
    """Serve repositories over loopback HTTP; return a factory.

    ``serve({"pkgs": path})`` publishes ``<base>/pkgs`` and returns
    ``<base>``.  Commits made to a served repository after the server
    starts are visible to a later clone, which is what the update test
    needs.
    """
    started: list = []

    def serve(repos: dict[str, Path]) -> str:
        backend = DictBackend({
            f"/{name}".encode(): Repo(str(path))
            for name, path in repos.items()
        })
        server = make_server(
            LOOPBACK, 0, make_wsgi_chain(backend),
            handler_class=_QuietHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        return f"http://{LOOPBACK}:{server.server_port}"

    yield serve

    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@pytest.fixture
def monorepo(tmp_path: Path) -> Path:
    """A one-commit repository holding two packages under ``packages/``."""
    root = tmp_path / "forge" / "pkgs"
    write_package(
        root / "packages", name="demo",
        manifest_yaml=MANIFEST, files={"tools.py": "VALUE = 1\n"},
    )
    write_package(
        root / "packages", name="other",
        manifest_yaml=MANIFEST.replace("demo", "other"),
    )
    (root / "README.md").write_text("a self-hosted forge monorepo\n")
    porcelain.init(str(root))
    _commit_all(root)
    return root


# ── the loopback archive server ─────────────────────────────────────


class _ArchiveHandler(BaseHTTPRequestHandler):
    """Serves one fixed body per registered path."""

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        body = self.server.routes.get(self.path.split("?", 1)[0])
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # pragma: no cover — quiet tests
        pass


class _ArchiveServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.fixture
def archive_site():
    """A loopback HTTP server; ``publish(path, bytes)`` returns its URL."""
    server = _ArchiveServer((LOOPBACK, 0), _ArchiveHandler)
    server.routes = {}
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02},
        daemon=True,
    )
    thread.start()

    class _Site:
        def publish(self, path: str, body: bytes) -> str:
            server.routes[path] = body
            return f"http://{LOOPBACK}:{server.server_address[1]}{path}"

    try:
        yield _Site()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _tar_gz_of(tree: Path, out_path: Path) -> bytes:
    """Archive ``tree`` with the platform's own archiver; return bytes.

    Uses :func:`carpenter.packages.archive_cache.archive_tree` so what is
    served is byte-for-byte what the platform itself produces, rather
    than a shape only this test knows how to make.
    """
    archive_cache.archive_tree(tree, out_path)
    return out_path.read_bytes()


# ── install from a git source ───────────────────────────────────────


class TestGitSource:
    def test_install_from_a_git_spec_with_a_subdir(
        self, monorepo, git_server, staging_root, non_interactive, capsys,
    ):
        head = Repo(str(monorepo)).head().decode()
        branch = _branch_of(monorepo)
        base = git_server({"pkgs": monorepo})

        rc = cli_packages.cmd_packages([
            "install",
            f"git+{base}/pkgs@{branch}#subdir=packages/demo",
        ])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Installed demo v0.1.0" in out
        installed = Path(config.CONFIG["base_dir"]) / "packages" / "demo"
        assert (installed / "manifest.yaml").is_file()
        assert (installed / "tools.py").read_text() == "VALUE = 1\n"
        # The subdir really was honoured: the sibling package and the
        # repository's own files are not in the installed tree.
        assert not (installed / "README.md").exists()
        assert not (installed / "other").exists()
        # And the commit is reported as the pin, not the branch name.
        assert head in out
        assert _quarantined(staging_root) == []

    def test_the_preview_names_the_source_before_the_install(
        self, monorepo, git_server, non_interactive, capsys,
    ):
        base = git_server({"pkgs": monorepo})
        head = Repo(str(monorepo)).head().decode()

        rc = cli_packages.cmd_packages([
            "install", f"git+{base}/pkgs#subdir=packages/demo",
        ])

        assert rc == 0
        err = capsys.readouterr().err
        assert "Fetching git" in err
        assert f"{base}/pkgs" in err
        assert "subdir packages/demo" in err
        assert head in err

    def test_provenance_matches_what_an_approved_install_records(
        self, monorepo, git_server, non_interactive,
    ):
        head = Repo(str(monorepo)).head().decode()
        branch = _branch_of(monorepo)
        base = git_server({"pkgs": monorepo})
        spec_text = f"git+{base}/pkgs@{branch}#subdir=packages/demo"

        assert cli_packages.cmd_packages(["install", spec_text]) == 0

        with db_connection() as db:
            record = installer.get_install_record(db, "demo")

        from carpenter.packages.sources import parse_source, source_id

        spec = parse_source(spec_text)
        assert record["source_kind"] == "git"
        assert record["source_url"] == f"{base}/pkgs"
        assert record["source_ref"] == branch
        assert record["source_resolved"] == head
        assert record["source_subdir"] == "packages/demo"
        assert record["source_id"] == source_id(spec)
        assert record["fetched_at"] == record["installed_at"]
        # The install materialised the fetched tree verbatim, so the
        # pristine tree it shipped is the tree now on disk.
        assert record["shipped_hash"] == record["hash"]

    def test_the_pristine_archive_is_cached_under_the_source_id(
        self, monorepo, git_server, non_interactive, tmp_path,
    ):
        """Keyed by logical source, as the approved path keys it.

        Two remotes publishing the same ``name@version`` must not
        overwrite each other's pristine tree, which is what the reconcile
        in ``packages update`` merges against.
        """
        base = git_server({"pkgs": monorepo})
        spec_text = f"git+{base}/pkgs#subdir=packages/demo"
        assert cli_packages.cmd_packages(["install", spec_text]) == 0

        from carpenter.packages.sources import parse_source, source_id

        with db_connection() as db:
            record = installer.get_install_record(db, "demo")
        sid = source_id(parse_source(spec_text))

        # Verified against the recorded shipped_hash, so this asserts the
        # cache holds the exact bytes the row claims it shipped — which
        # is the precondition a reconciled update depends on.
        pristine = archive_cache.load_pristine_tree(
            "demo", "0.1.0", record["shipped_hash"], source_id=sid,
        )
        assert pristine["manifest.yaml"]
        assert pristine["tools.py"] == b"VALUE = 1\n"

    def test_the_recorded_row_is_usable_by_packages_update(
        self, monorepo, git_server, non_interactive, capsys,
    ):
        """The end-to-end proof that CLI provenance is not a dead end.

        ``packages update`` rebuilds its source spec from the seven
        columns an install wrote and nothing else, so an update that
        follows the ref and lands a new version is the only honest test
        that a CLI-written row is as good as an approval-page-written one.
        """
        base = git_server({"pkgs": monorepo})
        branch = _branch_of(monorepo)
        assert cli_packages.cmd_packages([
            "install", f"git+{base}/pkgs@{branch}#subdir=packages/demo",
        ]) == 0
        capsys.readouterr()

        # A new release on the same branch of the same repository.
        pkg = monorepo / "packages" / "demo"
        (pkg / "manifest.yaml").write_text(
            MANIFEST.replace('version: "0.1.0"', 'version: "0.2.0"'),
        )
        (pkg / "tools.py").write_text("VALUE = 2\n")
        second = _commit_all(monorepo, b"release 0.2.0")

        rc = cli_packages.cmd_packages(["update", "demo", "--yes"])

        assert rc == 0
        assert "Updated demo 0.1.0 -> 0.2.0" in capsys.readouterr().out
        installed = Path(config.CONFIG["base_dir"]) / "packages" / "demo"
        assert (installed / "tools.py").read_text() == "VALUE = 2\n"
        with db_connection() as db:
            record = installer.get_install_record(db, "demo")
        assert record["version"] == "0.2.0"
        assert record["source_resolved"] == second


    def test_ref_moves_the_package_from_one_tag_to_the_next(
        self, monorepo, git_server, non_interactive, capsys,
    ):
        """``update --ref`` against a real remote, and it is remembered.

        The ref an install recorded is the only ref an update could
        follow before this, so a package pinned to a tag could never be
        moved to the next one without uninstalling it.  Here the operator
        names the new tag: the fetch follows that tag, the row records
        it, and a LATER plain update follows the new tag rather than
        snapping back — the third commit on the branch is deliberately
        left untagged, so "up to date" is only possible if the recorded
        ref really moved.
        """
        base = git_server({"pkgs": monorepo})
        pkg = monorepo / "packages" / "demo"
        porcelain.tag_create(str(monorepo), b"v1")

        (pkg / "manifest.yaml").write_text(
            MANIFEST.replace('version: "0.1.0"', 'version: "0.2.0"'),
        )
        (pkg / "tools.py").write_text("VALUE = 2\n")
        second = _commit_all(monorepo, b"release 0.2.0")
        porcelain.tag_create(str(monorepo), b"v2")

        assert cli_packages.cmd_packages([
            "install", f"git+{base}/pkgs@v1#subdir=packages/demo",
        ]) == 0
        installed = Path(config.CONFIG["base_dir"]) / "packages" / "demo"
        assert (installed / "tools.py").read_text() == "VALUE = 1\n"
        capsys.readouterr()

        rc = cli_packages.cmd_packages(
            ["update", "demo", "--ref", "v2", "--yes"],
        )

        assert rc == 0, capsys.readouterr().err
        assert "Updated demo 0.1.0 -> 0.2.0" in capsys.readouterr().out
        assert (installed / "tools.py").read_text() == "VALUE = 2\n"
        with db_connection() as db:
            record = installer.get_install_record(db, "demo")
        assert record["source_ref"] == "v2"
        assert record["source_resolved"] == second

        # A third release on the branch, with no tag of its own.
        (pkg / "tools.py").write_text("VALUE = 3\n")
        _commit_all(monorepo, b"untagged work after v2")

        assert cli_packages.cmd_packages(["update", "demo", "--yes"]) == 0
        out = capsys.readouterr().out
        assert "already up to date" in out
        assert (installed / "tools.py").read_text() == "VALUE = 2\n"


# ── install from an archive source ──────────────────────────────────


class TestArchiveSource:
    @pytest.fixture
    def published(self, tmp_path, archive_site):
        """Publish a ``demo`` archive; return ``(url, sha256)``."""
        tree = write_package(
            tmp_path / "release", name="demo",
            manifest_yaml=MANIFEST, files={"tools.py": "VALUE = 1\n"},
        )
        body = _tar_gz_of(tree, tmp_path / "demo.tar.gz")
        url = archive_site.publish("/releases/demo-0.1.0.tar.gz", body)
        return url, hashlib.sha256(body).hexdigest()

    def test_install_from_an_archive_spec_with_a_sha256(
        self, published, staging_root, non_interactive, capsys,
    ):
        url, digest = published

        rc = cli_packages.cmd_packages(["install", f"{url}#sha256={digest}"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Installed demo v0.1.0" in out
        installed = Path(config.CONFIG["base_dir"]) / "packages" / "demo"
        assert (installed / "tools.py").read_text() == "VALUE = 1\n"
        assert _quarantined(staging_root) == []

        with db_connection() as db:
            record = installer.get_install_record(db, "demo")
        assert record["source_kind"] == "archive"
        assert record["source_url"] == url
        assert record["source_ref"] == ""
        # An archive's pin is the digest of the bytes that were fetched.
        assert record["source_resolved"] == digest
        assert record["shipped_hash"] == record["hash"]

    def test_a_mismatched_sha256_installs_nothing(
        self, published, staging_root, non_interactive, capsys,
    ):
        url, _digest = published
        wrong = "0" * 64

        rc = cli_packages.cmd_packages(["install", f"{url}#sha256={wrong}"])

        assert rc == 1
        assert _quarantined(staging_root) == []
        with db_connection() as db:
            assert installer.get_install_record(db, "demo") is None


# ── a bare name through the configured source registry ──────────────


class TestConfiguredSource:
    def test_a_bare_name_resolves_through_a_configured_entry(
        self, monorepo, git_server, monkeypatch, staging_root,
        non_interactive, capsys,
    ):
        """The form an operator actually types once a forge is declared."""
        base = git_server({"pkgs": monorepo})
        monkeypatch.setitem(config.CONFIG, "package_sources", [{
            "name": "forge",
            "url": f"git+{base}/pkgs",
            "subdir_template": "packages/{name}",
        }])

        rc = cli_packages.cmd_packages(["install", "demo"])

        assert rc == 0
        err = capsys.readouterr().err
        assert "via configured source 'forge'" in err
        installed = Path(config.CONFIG["base_dir"]) / "packages" / "demo"
        assert (installed / "tools.py").read_text() == "VALUE = 1\n"
        assert _quarantined(staging_root) == []

        with db_connection() as db:
            record = installer.get_install_record(db, "demo")
        assert record["source_kind"] == "git"
        assert record["source_subdir"] == "packages/demo"
        assert record["source_url"] == f"{base}/pkgs"


# ── refusals ────────────────────────────────────────────────────────


class TestRefusals:
    def test_the_egress_refusal_names_the_host_and_the_config_key(
        self, monkeypatch, staging_root, non_interactive, capsys,
    ):
        """The refusal is the gate's own, not a vaguer CLI rewording.

        Hermetic: the guard's resolver seam is substituted, so the host
        is refused for resolving to a private address without any name
        ever reaching DNS.
        """
        monkeypatch.setattr(
            egress, "_resolve_host", lambda host, port: ["10.1.2.3"],
        )
        monkeypatch.setitem(config.CONFIG, "web_egress_allowlist", [])

        rc = cli_packages.cmd_packages([
            "install", "git+https://forge.example/org/pkgs.git",
        ])

        assert rc == 1
        err = capsys.readouterr().err
        assert "forge.example" in err
        assert "web_egress_allowlist" in err
        assert _quarantined(staging_root) == []

    def test_a_fetch_cap_refusal_names_the_cap(
        self, monorepo, git_server, monkeypatch, staging_root,
        non_interactive, capsys,
    ):
        """The caps come from ``install_requests.limits_for``.

        One set of numbers for both install paths: tightening the
        ``package_install`` config key has to bind the CLI too, or an
        operator's ceiling is only half a ceiling.
        """
        monkeypatch.setitem(
            config.CONFIG, "package_install", {"max_fetch_files": 1},
        )
        base = git_server({"pkgs": monorepo})

        rc = cli_packages.cmd_packages([
            "install", f"git+{base}/pkgs#subdir=packages/demo",
        ])

        assert rc == 1
        err = capsys.readouterr().err
        assert "max_files" in err
        assert _quarantined(staging_root) == []
        with db_connection() as db:
            assert installer.get_install_record(db, "demo") is None

    def test_a_capability_package_still_needs_consent(
        self, tmp_path, git_server, staging_root, non_interactive, capsys,
    ):
        """The consent flow is unchanged by the source being remote."""
        root = tmp_path / "forge" / "caps"
        write_package(
            root, name="demo",
            manifest_yaml=CAP_MANIFEST, files=CAP_FILES,
        )
        pkg = root / "demo"
        porcelain.init(str(pkg))
        _commit_all(pkg)
        base = git_server({"caps": pkg})

        rc = cli_packages.cmd_packages(["install", f"git+{base}/caps"])

        assert rc == 1
        err = capsys.readouterr().err
        assert "PLATFORM-LEVEL TRUST" in err
        assert "--accept-capabilities" in err
        assert _quarantined(staging_root) == []
        with db_connection() as db:
            assert installer.get_install_record(db, "demo") is None

    def test_explicit_consent_installs_the_capability_package(
        self, tmp_path, git_server, staging_root, non_interactive, capsys,
    ):
        root = tmp_path / "forge" / "caps"
        write_package(
            root, name="demo",
            manifest_yaml=CAP_MANIFEST, files=CAP_FILES,
        )
        pkg = root / "demo"
        porcelain.init(str(pkg))
        _commit_all(pkg)
        base = git_server({"caps": pkg})

        rc = cli_packages.cmd_packages([
            "install", f"git+{base}/caps", "--accept-capabilities",
        ])

        assert rc == 0
        assert _quarantined(staging_root) == []
        with db_connection() as db:
            assert installer.granted_verbs_for_package(db, "demo") == (
                frozenset({"demo.echo"})
            )

    def test_path_refuses_a_remote_spec_and_names_the_argument(
        self, test_db, capsys,
    ):
        """``--path`` is the local route; the refusal says which is not."""
        rc = cli_packages.cmd_packages([
            "install", "--path", "git+https://forge.example/org/pkgs.git",
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "--path takes a local directory" in err
        assert "<source>" in err


# ── nothing executes before the operator has decided ────────────────


class TestConsentOrder:
    def test_no_package_module_is_imported_before_consent(
        self, tmp_path, git_server, staging_root, non_interactive,
        monkeypatch, capsys,
    ):
        """A remote tree's code must not run to build its own preview.

        The same property ``tests/packages/fixtures/hostile_preview_pkg``
        proves for a local source, over a real transport and stopped at
        the capability-consent gate — so the whole fetch-and-preview
        half of the command is covered, which is the half that happens
        before the operator has agreed to anything.  Its ``tools.py``
        writes a marker file the moment it is imported, so the marker's
        absence is a precise witness.
        """
        from carpenter.packages.static_preview import NOT_EXECUTED_NOTICE

        marker = tmp_path / "executed.marker"
        monkeypatch.setenv("CARPENTER_TEST_PREVIEW_MARKER", str(marker))

        hostile = (
            Path(__file__).parent / "fixtures" / "hostile_preview_pkg"
            / "tools.py"
        ).read_text()
        root = tmp_path / "forge" / "hostile"
        write_package(
            root, name="demo",
            manifest_yaml=CAP_MANIFEST + "chat_tools:\n  - tools.py\n",
            files={**CAP_FILES, "tools.py": hostile},
        )
        pkg = root / "demo"
        porcelain.init(str(pkg))
        _commit_all(pkg)
        base = git_server({"hostile": pkg})

        # Non-interactive with declared capabilities and no flag: the
        # command refuses AFTER printing the preview, which is exactly
        # the window the marker is watching.
        rc = cli_packages.cmd_packages(["install", f"git+{base}/hostile"])

        assert rc == 1
        assert not marker.exists(), (
            "the install preview executed the fetched package's "
            "module-level code"
        )
        err = capsys.readouterr().err
        assert NOT_EXECUTED_NOTICE in err
        # The tool was still described, from the AST alone.
        assert "hostile_tool" in err
        assert "filesystem_write" in err
        assert _quarantined(staging_root) == []


# ── the accepted forms are stated where an operator looks ───────────


class TestHelp:
    def test_the_usage_states_the_remote_forms(self, capsys):
        assert cli_packages.cmd_packages(["--help"]) == 0
        out = capsys.readouterr().out
        assert "git+https://host/org/repo.git" in out
        assert "pkg.tar.gz" in out
        assert "web_egress_allowlist" in out

    def test_install_help_states_the_remote_forms(self, capsys):
        with pytest.raises(SystemExit) as exit_info:
            cli_packages.cmd_packages(["install", "--help"])
        assert exit_info.value.code == 0
        out = capsys.readouterr().out
        assert "git+https://host/org/repo.git" in out
        assert "sha256" in out
        assert "web_egress_allowlist" in out


# ── the quarantine, both ways ───────────────────────────────────────


class TestQuarantine:
    def test_the_staging_root_is_empty_after_a_success(
        self, monorepo, git_server, staging_root, non_interactive,
    ):
        base = git_server({"pkgs": monorepo})
        assert cli_packages.cmd_packages([
            "install", f"git+{base}/pkgs#subdir=packages/demo",
        ]) == 0
        assert _quarantined(staging_root) == []

    def test_the_staging_root_is_empty_after_an_install_failure(
        self, monorepo, git_server, staging_root, non_interactive,
        monkeypatch,
    ):
        """A failure inside the installer still owes the fetch a cleanup.

        Driven at the installer rather than at the fetch, because a
        failed fetch cleans up its own quarantine — this is the path
        where the CLI's own ``finally`` is the only thing that does.
        """
        base = git_server({"pkgs": monorepo})

        def _boom(*a, **k):
            raise installer.InstallError("fixture: the install failed")

        monkeypatch.setattr(installer, "install_package", _boom)

        rc = cli_packages.cmd_packages([
            "install", f"git+{base}/pkgs#subdir=packages/demo",
        ])

        assert rc == 1
        assert _quarantined(staging_root) == []

    def test_the_staging_root_is_empty_after_a_keyboard_interrupt(
        self, monorepo, git_server, staging_root, non_interactive,
        monkeypatch,
    ):
        """ctrl-c at a consent prompt is an exit path like any other."""
        base = git_server({"pkgs": monorepo})

        def _interrupt(*a, **k):
            raise KeyboardInterrupt

        monkeypatch.setattr(installer, "install_package", _interrupt)

        with pytest.raises(KeyboardInterrupt):
            cli_packages.cmd_packages([
                "install", f"git+{base}/pkgs#subdir=packages/demo",
            ])

        assert _quarantined(staging_root) == []


# ── no approval machinery is involved ───────────────────────────────


def test_a_cli_remote_install_writes_no_pending_request(
    monorepo, git_server, staging_root, non_interactive,
):
    """The operator at the terminal IS the human an approval page asks.

    So a CLI install mints no token and writes no
    ``pending_package_installs`` row; it would be a second, weaker
    consent record for a decision already made out loud.
    """
    from carpenter.packages import pending_install

    base = git_server({"pkgs": monorepo})
    assert cli_packages.cmd_packages([
        "install", f"git+{base}/pkgs#subdir=packages/demo",
    ]) == 0

    with db_connection() as db:
        pending_install.ensure_pending_install_table(db)
        assert pending_install.count_active_requests(db) == 0

"""Tests for the package source-spec grammar and the source registry.

Every accepted form, every refusal (checked by message, because the
message is the only thing an operator sees), redaction, source-id
stability, and the configured-source registry including the bare-name
fallback that keeps pre-registry installs working.

No network and no real hosts: remote specs use ``.invalid`` /
``.example`` names, which are never resolved here because parsing is
pure, and the loopback-only ``http://`` exception uses ``127.0.0.1``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from carpenter.packages import sources
from carpenter.packages.sources import (
    SourceSpec,
    SourceSpecError,
    configured_sources,
    describe,
    parse_source,
    redact,
    source_id,
    source_search_order,
)

SHA256 = "a" * 64
SHA40 = "b" * 40


def _config(sources_list=None, search_order=None, **extra) -> dict:
    """Build a config mapping holding only the packages registry keys."""
    packages = dict(extra)
    packages["sources"] = sources_list if sources_list is not None else []
    packages["search_order"] = search_order if search_order is not None else []
    return {"packages": packages}


# ── accepted: local ─────────────────────────────────────────────────


class TestLocalForms:
    def test_absolute_path(self, tmp_path):
        spec = parse_source(str(tmp_path))
        assert spec.kind == "local"
        assert spec.path == tmp_path.resolve()
        assert spec.url is None
        assert spec.raw == str(tmp_path)

    def test_home_relative_path_is_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        spec = parse_source("~/pkgs/demo")
        assert spec.kind == "local"
        assert spec.path == (tmp_path / "pkgs" / "demo").resolve()

    def test_file_url(self, tmp_path):
        spec = parse_source(f"file://{tmp_path}")
        assert spec.kind == "local"
        assert spec.path == tmp_path.resolve()

    def test_file_url_with_localhost_host(self, tmp_path):
        spec = parse_source(f"file://localhost{tmp_path}")
        assert spec.path == tmp_path.resolve()

    def test_file_url_with_other_host_refused(self):
        with pytest.raises(SourceSpecError, match="file:// source host"):
            parse_source("file://elsewhere.invalid/srv/pkgs/demo")

    def test_relative_path_is_not_a_source(self):
        with pytest.raises(SourceSpecError, match="not a recognised source"):
            parse_source("./demo")


# ── accepted: git ───────────────────────────────────────────────────


class TestGitForms:
    def test_plain_https_repo(self):
        spec = parse_source("git+https://forge.example/org/repo.git")
        assert spec.kind == "git"
        assert spec.url == "https://forge.example/org/repo.git"
        assert spec.ref is None
        assert spec.subdir is None

    def test_ref_and_subdir(self):
        spec = parse_source(
            "git+https://forge.example/org/repo.git@v1.2.3"
            "#subdir=packages/demo",
        )
        assert (spec.ref, spec.subdir) == ("v1.2.3", "packages/demo")
        assert spec.url == "https://forge.example/org/repo.git"

    def test_commit_ref(self):
        spec = parse_source(f"git+https://forge.example/org/repo.git@{SHA40}")
        assert spec.ref == SHA40

    def test_port_is_kept(self):
        spec = parse_source("git+https://forge.example:3000/org/repo.git")
        assert spec.url == "https://forge.example:3000/org/repo.git"

    def test_userinfo_is_stripped_from_the_canonical_url(self):
        spec = parse_source(
            "git+https://bot:secrettoken@forge.example/org/repo.git@main",
        )
        assert spec.url == "https://forge.example/org/repo.git"
        assert "secrettoken" not in spec.url
        assert "secrettoken" not in spec.raw
        assert spec.raw == "git+https://***@forge.example/org/repo.git@main"
        assert spec.ref == "main"

    def test_loopback_http_is_accepted(self):
        for host in ("127.0.0.1", "127.0.0.1:3000", "localhost", "[::1]"):
            spec = parse_source(f"git+http://{host}/org/repo.git")
            assert spec.kind == "git"
            assert spec.url.startswith("http://")

    def test_non_loopback_http_is_refused(self):
        with pytest.raises(SourceSpecError, match="plain http:// is refused"):
            parse_source("git+http://forge.example/org/repo.git")

    def test_missing_repository_path_refused(self):
        with pytest.raises(SourceSpecError, match="no repository path"):
            parse_source("git+https://forge.example")

    def test_ssh_forms_refused(self):
        for spec in (
            "git+ssh://forge.example/org/repo.git",
            "ssh://forge.example/org/repo.git",
            "git://forge.example/org/repo.git",
        ):
            with pytest.raises(SourceSpecError, match="ssh and the git protocol"):
                parse_source(spec)

    def test_sha256_fragment_refused_on_git(self):
        with pytest.raises(SourceSpecError, match="only applies to an archive"):
            parse_source(
                f"git+https://forge.example/org/repo.git#sha256={SHA256}",
            )

    @pytest.mark.parametrize("ref", ["", "-delete", "a..b", "main@{1}", "x/"])
    def test_bad_refs_refused(self, ref):
        with pytest.raises(SourceSpecError, match="git ref"):
            parse_source(f"git+https://forge.example/org/repo.git@{ref}")


# ── accepted: archive ───────────────────────────────────────────────


class TestArchiveForms:
    @pytest.mark.parametrize("suffix", [".tar.gz", ".tgz", ".tar"])
    def test_suffixes(self, suffix):
        spec = parse_source(f"https://files.example/dl/demo-1.0{suffix}")
        assert spec.kind == "archive"
        assert spec.url.endswith(suffix)
        assert spec.digest is None

    def test_sha256_pin(self):
        spec = parse_source(
            f"https://files.example/dl/demo-1.0.tar.gz#sha256={SHA256}",
        )
        assert spec.digest == SHA256

    def test_loopback_http_is_accepted(self):
        spec = parse_source("http://127.0.0.1:8080/dl/demo-1.0.tar.gz")
        assert spec.kind == "archive"

    def test_non_loopback_http_refused(self):
        with pytest.raises(SourceSpecError, match="plain http:// is refused"):
            parse_source("http://files.example/dl/demo-1.0.tar.gz")

    def test_non_archive_https_refused(self):
        with pytest.raises(SourceSpecError, match="does not name an archive"):
            parse_source("https://forge.example/org/repo")

    def test_zip_refused(self):
        with pytest.raises(SourceSpecError, match="does not name an archive"):
            parse_source("https://files.example/dl/demo-1.0.zip")

    @pytest.mark.parametrize("digest", ["", "abc", "A" * 64, "g" * 64, "a" * 63])
    def test_bad_digest_refused(self, digest):
        with pytest.raises(SourceSpecError, match="sha256 fragment"):
            parse_source(
                f"https://files.example/dl/demo-1.0.tar.gz#sha256={digest}",
            )

    def test_credential_query_param_dropped_from_canonical_url(self):
        spec = parse_source(
            "https://files.example/dl/demo-1.0.tar.gz"
            "?token=secrettoken&page=2",
        )
        assert spec.url == "https://files.example/dl/demo-1.0.tar.gz?page=2"
        assert "secrettoken" not in spec.url
        assert "secrettoken" not in spec.raw


# ── refusals: subdir and fragments ──────────────────────────────────


class TestSubdirRefusals:
    @pytest.mark.parametrize("subdir", [
        "/packages/demo",        # absolute
        "../demo",               # escapes the tree
        "packages/../../demo",   # escapes less obviously
        "packages\\demo",        # not POSIX
        "",                      # empty
        ".",                     # names nothing
    ])
    def test_refused(self, subdir):
        with pytest.raises(SourceSpecError, match="subdir"):
            parse_source(
                f"git+https://forge.example/org/repo.git#subdir={subdir}",
            )

    def test_normalised(self):
        spec = parse_source(
            "git+https://forge.example/org/repo.git#subdir=./packages/demo/",
        )
        assert spec.subdir == "packages/demo"

    def test_unknown_fragment_key_refused(self):
        with pytest.raises(SourceSpecError, match="unknown source fragment"):
            parse_source("git+https://forge.example/org/repo.git#branch=main")

    def test_fragment_without_value_refused(self):
        with pytest.raises(SourceSpecError, match="not a key=value pair"):
            parse_source("git+https://forge.example/org/repo.git#subdir")


# ── refusals: the catch-all ─────────────────────────────────────────


class TestGeneralRefusals:
    @pytest.mark.parametrize("spec", ["", "   ", "Demo", "demo name", "ftp://h/x"])
    def test_refused(self, spec):
        with pytest.raises(SourceSpecError):
            parse_source(spec)

    def test_every_refusal_states_the_accepted_forms(self):
        for bad in ("", "Demo", "ssh://forge.example/o/r", "https://h/x.zip"):
            with pytest.raises(SourceSpecError) as excinfo:
                parse_source(bad)
            message = str(excinfo.value)
            assert "accepted source forms" in message
            assert "git+https://" in message
            assert ".tar.gz" in message

    def test_non_string_refused(self):
        with pytest.raises(SourceSpecError):
            parse_source(None)  # type: ignore[arg-type]


# ── redaction ───────────────────────────────────────────────────────


class TestRedact:
    def test_userinfo(self):
        assert redact("https://bot:secrettoken@forge.example/o/r.git") == (
            "https://***@forge.example/o/r.git"
        )

    def test_username_only_userinfo(self):
        assert redact("https://bot@forge.example/o/r.git") == (
            "https://***@forge.example/o/r.git"
        )

    @pytest.mark.parametrize("param", [
        "token", "access_token", "private_token", "api_key", "ACCESS_TOKEN",
    ])
    def test_token_query_params(self, param):
        out = redact(f"https://files.example/dl/p.tar.gz?{param}=secrettoken")
        assert out == f"https://files.example/dl/p.tar.gz?{param}=***"
        assert "secrettoken" not in out

    def test_other_query_params_kept(self):
        out = redact("https://files.example/dl/p.tar.gz?page=2&token=s3cr3t")
        assert "page=2" in out
        assert "s3cr3t" not in out

    def test_git_prefix_preserved(self):
        assert redact("git+https://bot:t@forge.example/o/r.git@main") == (
            "git+https://***@forge.example/o/r.git@main"
        )

    def test_non_url_unchanged(self):
        for value in ("", "demo", "/srv/pkgs/demo"):
            assert redact(value) == value


# ── identity ────────────────────────────────────────────────────────


class TestSourceId:
    def test_is_sixteen_lowercase_hex(self):
        sid = source_id(parse_source("git+https://forge.example/o/r.git"))
        assert len(sid) == 16
        assert sid == sid.lower()
        assert all(c in "0123456789abcdef" for c in sid)

    def test_stable_across_calls_and_credentials_and_refs(self):
        base = parse_source("git+https://forge.example/o/r.git")
        same = [
            "git+https://forge.example/o/r.git",
            "git+https://bot:secrettoken@forge.example/o/r.git",
            "git+https://forge.example/o/r.git@main",
            f"git+https://forge.example/o/r.git@{SHA40}",
        ]
        for spec in same:
            assert source_id(parse_source(spec)) == source_id(base)

    def test_two_remotes_same_package_name_do_not_collide(self):
        a = parse_source(
            "git+https://forge-a.example/org/pkgs.git#subdir=packages/demo",
        )
        b = parse_source(
            "git+https://forge-b.example/org/pkgs.git#subdir=packages/demo",
        )
        assert source_id(a) != source_id(b)

    def test_subdir_discriminates(self):
        a = parse_source("git+https://forge.example/o/r.git#subdir=packages/a")
        b = parse_source("git+https://forge.example/o/r.git#subdir=packages/b")
        assert source_id(a) != source_id(b)

    def test_kind_discriminates(self):
        git = SourceSpec(kind="git", raw="x", url="https://h.example/p")
        archive = SourceSpec(kind="archive", raw="x", url="https://h.example/p")
        assert source_id(git) != source_id(archive)

    def test_local_paths_discriminate(self, tmp_path):
        a = parse_source(str(tmp_path / "a"))
        b = parse_source(str(tmp_path / "b"))
        assert source_id(a) != source_id(b)
        assert source_id(a) == source_id(parse_source(str(tmp_path / "a")))

    def test_archive_digest_does_not_change_the_id(self):
        plain = parse_source("https://files.example/dl/demo-1.0.tar.gz")
        pinned = parse_source(
            f"https://files.example/dl/demo-1.0.tar.gz#sha256={SHA256}",
        )
        assert source_id(plain) == source_id(pinned)


# ── description ─────────────────────────────────────────────────────


class TestDescribe:
    def test_local(self, tmp_path):
        text = describe(parse_source(str(tmp_path)))
        assert "local directory" in text
        assert str(tmp_path.resolve()) in text

    def test_git_with_ref_and_subdir(self):
        text = describe(parse_source(
            "git+https://forge.example/o/r.git@main#subdir=packages/demo",
        ))
        assert "git https://forge.example/o/r.git" in text
        assert "at main" in text
        assert "subdir packages/demo" in text

    def test_git_without_ref(self):
        assert "default branch" in describe(
            parse_source("git+https://forge.example/o/r.git"),
        )

    def test_archive_pin_state(self):
        assert "no sha256 pin" in describe(
            parse_source("https://files.example/dl/demo-1.0.tar.gz"),
        )
        assert "sha256 aaaaaaaaaaaa" in describe(parse_source(
            f"https://files.example/dl/demo-1.0.tar.gz#sha256={SHA256}",
        ))

    def test_never_carries_a_credential(self):
        text = describe(parse_source(
            "git+https://bot:secrettoken@forge.example/o/r.git",
        ))
        assert "secrettoken" not in text

    def test_names_the_alias(self):
        config = _config(
            [{"name": "house", "url": "git+https://forge.example/o/pkgs.git"}],
            ["house"],
        )
        text = describe(parse_source("demo", config=config))
        assert "via configured source 'house'" in text


# ── the configured source registry ──────────────────────────────────


class TestConfiguredSources:
    def test_defaults_are_empty(self):
        assert configured_sources(_config()) == ()
        assert source_search_order(_config()) == ()

    def test_parsed_entry(self):
        entries = configured_sources(_config([{
            "name": "house",
            "url": "git+https://forge.example/org/pkgs.git",
            "subdir_template": "packages/{name}",
            "credential_ref": "HOUSE_FORGE_TOKEN",
        }]))
        assert len(entries) == 1
        assert entries[0].name == "house"
        assert entries[0].subdir_template == "packages/{name}"
        assert entries[0].credential_ref == "HOUSE_FORGE_TOKEN"

    def test_missing_keys_are_optional(self):
        entries = configured_sources(_config([{
            "name": "house", "url": "https://files.example/d/demo.tar.gz",
        }]))
        assert entries[0].subdir_template is None
        assert entries[0].credential_ref is None

    @pytest.mark.parametrize("entry,match", [
        ({"url": "git+https://f.example/o/r.git"}, "name must be"),
        ({"name": "Up", "url": "git+https://f.example/o/r.git"}, "name must be"),
        ({"name": "house"}, "url must be"),
        ({"name": "house", "url": ""}, "url must be"),
        (
            {"name": "house", "url": "git+ssh://f.example/o/r.git"},
            "url is invalid",
        ),
        (
            {"name": "house", "url": "/srv/pkgs"},
            "url is invalid",
        ),
        (
            {"name": "house", "url": "git+https://f.example/o/{name}.git"},
            "must not contain a placeholder",
        ),
        (
            {
                "name": "house", "url": "git+https://f.example/o/r.git",
                "subdir_template": "../{name}",
            },
            "subdir_template is invalid",
        ),
        (
            {
                "name": "house", "url": "git+https://f.example/o/r.git",
                "subdir_template": "packages/{version}",
            },
            "may only use the",
        ),
        (
            {
                "name": "house", "url": "git+https://f.example/o/r.git",
                "credential_ref": "not a var name",
            },
            "credential_ref must be the NAME",
        ),
        (
            {
                "name": "house", "url": "git+https://f.example/o/r.git",
                "credential_ref": "ghp_realtokenvalue-1",
            },
            "credential_ref must be the NAME",
        ),
        (
            {"name": "house", "url": "git+https://f.example/o/r.git",
             "token": "secret"},
            "unknown key",
        ),
    ])
    def test_malformed_entry_refused(self, entry, match):
        with pytest.raises(SourceSpecError, match=match):
            configured_sources(_config([entry]))

    def test_non_list_refused(self):
        with pytest.raises(SourceSpecError, match="must be a list"):
            configured_sources({"packages": {"sources": {"name": "house"}}})

    def test_non_mapping_entry_refused(self):
        with pytest.raises(SourceSpecError, match="must be a mapping"):
            configured_sources(_config(["house"]))

    def test_duplicate_alias_refused(self):
        entry = {"name": "house", "url": "git+https://f.example/o/r.git"}
        with pytest.raises(SourceSpecError, match="declared more than once"):
            configured_sources(_config([entry, dict(entry)]))

    def test_packages_not_a_mapping_refused(self):
        with pytest.raises(SourceSpecError, match="must be a mapping"):
            configured_sources({"packages": ["house"]})


class TestSearchOrder:
    def test_validated_against_declared_sources(self):
        config = _config(
            [{"name": "house", "url": "git+https://f.example/o/r.git"}],
            ["house"],
        )
        assert source_search_order(config) == ("house",)

    def test_unknown_alias_refused(self):
        config = _config(
            [{"name": "house", "url": "git+https://f.example/o/r.git"}],
            ["hous"],
        )
        with pytest.raises(SourceSpecError, match="not declared in"):
            source_search_order(config)

    def test_non_list_refused(self):
        with pytest.raises(SourceSpecError, match="must be a list"):
            source_search_order({"packages": {"search_order": "house"}})

    def test_non_string_entry_refused(self):
        config = _config(
            [{"name": "house", "url": "git+https://f.example/o/r.git"}],
            [{"name": "house"}],
        )
        with pytest.raises(SourceSpecError, match="must be a non-empty source"):
            source_search_order(config)


# ── bare names ──────────────────────────────────────────────────────


class TestBareNameResolution:
    def test_legacy_local_fallback_with_no_config(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        spec = parse_source("demo", config=_config())
        assert spec.kind == "local"
        assert spec.path == (
            tmp_path / "repos" / "carpenter-packages" / "packages" / "demo"
        )
        assert spec.alias is None
        assert spec.raw == "demo"

    def test_declared_but_unsearched_source_is_not_used(
        self, monkeypatch, tmp_path,
    ):
        """A source with an empty search_order is configured, not enabled."""
        monkeypatch.setenv("HOME", str(tmp_path))
        config = _config(
            [{"name": "house", "url": "git+https://f.example/o/r.git"}],
            [],
        )
        assert parse_source("demo", config=config).kind == "local"

    def test_resolves_through_the_first_searched_alias(self):
        config = _config(
            [
                {
                    "name": "house",
                    "url": "git+https://forge.example/org/pkgs.git",
                    "subdir_template": "packages/{name}",
                },
                {
                    "name": "other",
                    "url": "https://files.example/d/all.tar.gz",
                },
            ],
            ["house", "other"],
        )
        spec = parse_source("demo", config=config)
        assert spec.kind == "git"
        assert spec.url == "https://forge.example/org/pkgs.git"
        assert spec.subdir == "packages/demo"
        assert spec.alias == "house"
        assert spec.raw == "demo"

    def test_search_order_decides_which_alias_wins(self):
        config = _config(
            [
                {"name": "house", "url": "git+https://forge-a.example/o/p.git"},
                {"name": "other", "url": "git+https://forge-b.example/o/p.git"},
            ],
            ["other", "house"],
        )
        assert parse_source("demo", config=config).alias == "other"

    def test_all_candidates_are_returned_in_order(self):
        config = _config(
            [
                {"name": "house", "url": "git+https://forge-a.example/o/p.git"},
                {"name": "other", "url": "git+https://forge-b.example/o/p.git"},
            ],
            ["other", "house"],
        )
        candidates = sources.resolve_bare_name("demo", config=config)
        assert [c.alias for c in candidates] == ["other", "house"]

    def test_name_override_fills_the_subdir_template(self):
        config = _config(
            [{
                "name": "house",
                "url": "git+https://forge.example/org/pkgs.git",
                "subdir_template": "packages/{name}",
            }],
            ["house"],
        )
        spec = parse_source("demo", name="other-demo", config=config)
        assert spec.subdir == "packages/other-demo"

    def test_name_override_fills_the_legacy_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        spec = parse_source("demo", name="other-demo", config=_config())
        assert spec.path.name == "other-demo"

    def test_no_subdir_template_means_the_root_is_the_package(self):
        config = _config(
            [{"name": "house", "url": "git+https://forge.example/o/demo.git"}],
            ["house"],
        )
        assert parse_source("demo", config=config).subdir is None

    def test_malformed_registry_surfaces_on_a_bare_name(self):
        config = _config([{"name": "house", "url": "git+ssh://f.example/o/r"}], [])
        with pytest.raises(SourceSpecError, match="url is invalid"):
            parse_source("demo", config=config)

    def test_live_config_is_consulted_when_none_is_passed(self, monkeypatch):
        monkeypatch.setitem(
            __import__("carpenter.config", fromlist=["CONFIG"]).CONFIG,
            "packages",
            {
                "sources": [{
                    "name": "house",
                    "url": "git+https://forge.example/org/pkgs.git",
                    "subdir_template": "packages/{name}",
                }],
                "search_order": ["house"],
            },
        )
        spec = parse_source("demo")
        assert spec.alias == "house"
        assert spec.subdir == "packages/demo"

    def test_explicit_spec_ignores_the_registry(self, tmp_path):
        config = _config(
            [{"name": "house", "url": "git+https://forge.example/o/p.git"}],
            ["house"],
        )
        spec = parse_source(str(tmp_path), config=config)
        assert spec.kind == "local"
        assert spec.alias is None


# ── the dataclass ───────────────────────────────────────────────────


def test_source_spec_is_frozen():
    spec = parse_source("git+https://forge.example/o/r.git")
    with pytest.raises(Exception):
        spec.url = "https://elsewhere.invalid/o/r.git"  # type: ignore[misc]


def test_local_spec_carries_a_path_not_a_url(tmp_path):
    spec = parse_source(str(tmp_path))
    assert isinstance(spec.path, Path)
    assert spec.url is None and spec.ref is None and spec.digest is None

"""Tests for carpenter.seed — unified config_seed installer."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from carpenter.seed import (
    SEED_MANIFEST,
    SeedTarget,
    install_config_seed,
    install_single_target,
)


class TestManifest:
    def test_manifest_covers_legacy_installers(self):
        """The manifest must cover every target that had its own legacy installer:
        prompts, coding-prompts, kb, data_models. Drift here would mean we silently
        stopped installing something users expect."""
        names = {entry.name for entry in SEED_MANIFEST}
        assert {"prompts", "coding-prompts", "kb", "data_models"} <= names

    def test_manifest_entries_have_unique_names(self):
        names = [entry.name for entry in SEED_MANIFEST]
        assert len(names) == len(set(names))

    def test_manifest_seed_subdirs_exist_in_repo(self):
        """Every manifest entry's seed_subdir must exist under config_seed/;
        otherwise install_config_seed() will always report 'no_defaults'."""
        repo_seed_root = Path(__file__).resolve().parent.parent / "config_seed"
        for entry in SEED_MANIFEST:
            assert (repo_seed_root / entry.seed_subdir).is_dir(), (
                f"Seed dir missing for manifest entry {entry.name!r}: "
                f"{repo_seed_root / entry.seed_subdir}"
            )


class TestInstallConfigSeed:
    def test_fresh_install_copies_all_targets(self, tmp_path):
        base_dir = tmp_path / "base"
        results = install_config_seed(str(base_dir))

        assert set(results.keys()) == {entry.name for entry in SEED_MANIFEST}
        for name, result in results.items():
            assert result["status"] == "installed", f"{name}: {result}"
            assert result["copied"] > 0, f"{name} copied nothing"

        # Targets land at base_dir/<default_rel_target>.
        for entry in SEED_MANIFEST:
            assert (base_dir / entry.default_rel_target).is_dir()

    def test_existing_target_preserves_user_files_and_upserts_new(self, tmp_path):
        base_dir = tmp_path / "base"
        # Pre-create the prompts target with a user file AND a same-named
        # seed file that would collide with the seed copy.
        existing = base_dir / "config" / "prompts"
        existing.mkdir(parents=True)
        (existing / "marker.md").write_text("user content")
        # Pick a real seed file that exists in config_seed/prompts and
        # pre-create it so we can prove it is NOT overwritten.
        from carpenter.seed import _config_seed_root
        seed_prompt_files = list(
            (_config_seed_root() / "prompts").rglob("*.md")
        )
        assert seed_prompt_files, "config_seed/prompts must contain at least one .md"
        first = seed_prompt_files[0].relative_to(_config_seed_root() / "prompts")
        (existing / first).parent.mkdir(parents=True, exist_ok=True)
        (existing / first).write_text("customized")

        results = install_config_seed(str(base_dir))

        # Status is 'installed' because new seed files (everything except
        # the one we pre-created) were copied in. User files untouched.
        assert results["prompts"]["status"] == "installed"
        assert (existing / "marker.md").read_text() == "user content"
        assert (existing / first).read_text() == "customized"
        # Other targets still got installed.
        assert results["kb"]["status"] == "installed"

    def test_existing_target_with_all_files_present_reports_exists(self, tmp_path):
        """If every seed file already exists in the target, status is 'exists'."""
        base_dir = tmp_path / "base"
        # Install once to populate the target fully.
        install_config_seed(str(base_dir))
        # Run again — nothing new to copy.
        results = install_config_seed(str(base_dir))
        for name, result in results.items():
            assert result["status"] == "exists", f"{name}: {result}"
            assert result["copied"] == 0

    def test_missing_seed_dir_reports_no_defaults(self, tmp_path):
        """If a manifest entry points at a nonexistent seed subdir, we should
        get status 'no_defaults' rather than an exception."""
        base_dir = tmp_path / "base"
        bogus = (
            SeedTarget(
                name="nonexistent",
                seed_subdir="__does_not_exist__",
                default_rel_target="config/nope",
                count_glob="*.md",
                label="Nonexistent",
            ),
        )
        results = install_config_seed(str(base_dir), manifest=bogus)
        assert results["nonexistent"]["status"] == "no_defaults"
        assert results["nonexistent"]["copied"] == 0

    def test_override_redirects_target(self, tmp_path):
        base_dir = tmp_path / "base"
        custom_prompts = tmp_path / "elsewhere" / "my-prompts"

        results = install_config_seed(
            str(base_dir),
            overrides={"prompts": custom_prompts},
        )

        assert results["prompts"]["status"] == "installed"
        assert custom_prompts.is_dir()
        # Default prompts path was NOT created because the override redirected it.
        assert not (base_dir / "config" / "prompts").exists()

    def test_base_dir_required_when_no_override(self, tmp_path):
        with pytest.raises(ValueError):
            install_config_seed(None)


class TestInstallSingleTarget:
    def test_installs_known_target(self, tmp_path):
        target = tmp_path / "fresh_prompts"
        result = install_single_target("prompts", target)
        assert result["status"] == "installed"
        assert result["copied"] > 0
        assert target.is_dir()

    def test_unknown_target_raises(self, tmp_path):
        with pytest.raises(KeyError):
            install_single_target("not-a-thing", tmp_path / "x")

    def test_empty_existing_target_gets_upserted(self, tmp_path):
        """An empty pre-existing dir still receives the seed files."""
        target = tmp_path / "fresh_kb"
        target.mkdir()
        result = install_single_target("kb", target)
        assert result["status"] == "installed"
        assert result["copied"] > 0

    def test_upsert_does_not_overwrite_existing_files(self, tmp_path):
        """A pre-existing file in target is preserved; new seed files are added."""
        target = tmp_path / "partial_kb"
        target.mkdir()
        # Pre-create one file that the seed would otherwise install.
        from carpenter.seed import _config_seed_root
        kb_files = list((_config_seed_root() / "kb").rglob("*.md"))
        assert kb_files, "config_seed/kb must contain at least one .md"
        rel = kb_files[0].relative_to(_config_seed_root() / "kb")
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        (target / rel).write_text("custom")

        result = install_single_target("kb", target)
        assert result["status"] == "installed"
        assert result["copied"] >= 1
        # The pre-existing file is untouched.
        assert (target / rel).read_text() == "custom"

    def test_second_call_after_full_install_is_noop(self, tmp_path):
        target = tmp_path / "fresh_kb"
        install_single_target("kb", target)
        result = install_single_target("kb", target)
        assert result["status"] == "exists"
        assert result["copied"] == 0


class TestLegacyWrappers:
    """The legacy per-target functions must still return the same shape."""

    def test_install_prompt_defaults_wrapper(self, tmp_path):
        from carpenter.prompts import install_prompt_defaults
        target = str(tmp_path / "fresh_prompts")
        result = install_prompt_defaults(target)
        assert result["status"] == "installed"
        assert result["copied"] > 0
        assert os.path.isdir(target)

    def test_install_coding_prompt_defaults_wrapper(self, tmp_path):
        from carpenter.prompts import install_coding_prompt_defaults
        target = str(tmp_path / "fresh_coding_prompts")
        result = install_coding_prompt_defaults(target)
        assert result["status"] == "installed"

    def test_install_kb_seed_wrapper(self, tmp_path):
        from carpenter.kb import install_seed
        target = str(tmp_path / "fresh_kb")
        result = install_seed(target)
        assert result["status"] == "installed"
        assert result["copied"] > 0

    def test_install_data_models_defaults_wrapper(self, tmp_path):
        from carpenter.db import install_data_models_defaults
        target = str(tmp_path / "fresh_data_models")
        result = install_data_models_defaults(target)
        assert result["status"] == "installed"

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

    def test_existing_target_is_skipped(self, tmp_path):
        base_dir = tmp_path / "base"
        # Pre-create the prompts target so the installer must skip it.
        existing = base_dir / "config" / "prompts"
        existing.mkdir(parents=True)
        (existing / "marker.md").write_text("user content")

        results = install_config_seed(str(base_dir))

        assert results["prompts"]["status"] == "exists"
        assert results["prompts"]["copied"] == 0
        # User file is untouched.
        assert (existing / "marker.md").read_text() == "user content"
        # Other targets still got installed.
        assert results["kb"]["status"] == "installed"

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

    def test_skips_existing(self, tmp_path):
        target = tmp_path / "fresh_kb"
        target.mkdir()
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

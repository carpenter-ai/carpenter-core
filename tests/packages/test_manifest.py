"""Manifest schema and YAML-loader tests."""

from __future__ import annotations

import pytest

from carpenter.packages.manifest import (
    PackageManifest,
    ManifestError,
    load_manifest,
)


class TestValidManifest:
    """Round-trip valid manifests."""

    def test_minimal_valid_manifest(self, make_package):
        pkg_dir = make_package(
            "hello",
            """
            name: hello
            version: "0.1.0"
            description: Reference no-op package.
            """,
        )
        manifest = load_manifest(pkg_dir / "manifest.yaml")

        assert manifest.name == "hello"
        assert manifest.version == "0.1.0"
        assert manifest.description == "Reference no-op package."
        assert manifest.chat_tools == ()
        assert manifest.kb_namespace == "hello"  # defaults to name
        assert manifest.platform_compatibility == ("any",)
        assert manifest.source_path == pkg_dir.resolve()

    def test_manifest_with_chat_tools_and_kb_namespace(self, make_package):
        pkg_dir = make_package(
            "hello",
            """
            name: hello
            version: "1.2.3"
            description: With chat tools.
            chat_tools:
              - tools.py
              - more.py
            kb_namespace: hello
            platform_compatibility:
              - linux
              - any
            """,
            files={"tools.py": "", "more.py": ""},
        )
        manifest = load_manifest(pkg_dir / "manifest.yaml")
        assert manifest.chat_tools == ("tools.py", "more.py")
        assert manifest.kb_namespace == "hello"
        assert manifest.platform_compatibility == ("linux", "any")

    def test_numeric_version_coerced_to_string(self, make_package):
        pkg_dir = make_package(
            "hello",
            """
            name: hello
            version: 1.0
            description: Numeric version.
            """,
        )
        manifest = load_manifest(pkg_dir / "manifest.yaml")
        assert manifest.version == "1.0"


class TestRequiredFields:
    """Missing required fields are rejected with clear errors."""

    @pytest.mark.parametrize(
        "missing,manifest_yaml",
        [
            ("name", 'version: "0.1"\ndescription: x'),
            ("version", "name: hello\ndescription: x"),
            ("description", 'name: hello\nversion: "0.1"'),
        ],
    )
    def test_missing_required_field(self, make_package, missing, manifest_yaml):
        pkg_dir = make_package("hello", manifest_yaml)
        with pytest.raises(ManifestError, match="missing required fields"):
            load_manifest(pkg_dir / "manifest.yaml")


class TestUnknownFields:
    """Unknown fields are rejected (defense in depth for I10)."""

    def test_unknown_top_level_field_rejected(self, make_package):
        pkg_dir = make_package(
            "hello",
            """
            name: hello
            version: "0.1.0"
            description: x
            secret_backdoor: yes
            """,
        )
        with pytest.raises(ManifestError, match="unknown fields"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_judge_field_rejected_at_loader(self, make_package):
        """Even before security guards run, the loader rejects 'judge'."""
        pkg_dir = make_package(
            "hello",
            """
            name: hello
            version: "0.1.0"
            description: x
            judge: deterministic_check.py
            """,
        )
        with pytest.raises(ManifestError, match="unknown fields"):
            load_manifest(pkg_dir / "manifest.yaml")


class TestNameValidation:
    """Package names must be lowercase ASCII identifiers."""

    @pytest.mark.parametrize(
        "bad_name",
        ["Hello", "HELLO", "hello world", "hello/evil", "..", "1hello", ""],
    )
    def test_invalid_name_rejected(self, make_package, bad_name):
        pkg_dir = make_package(
            "pkg",
            f"""
            name: {bad_name!r}
            version: "0.1"
            description: x
            """,
        )
        with pytest.raises(ManifestError, match="'name' must match"):
            load_manifest(pkg_dir / "manifest.yaml")

    @pytest.mark.parametrize("good_name", ["hello", "h", "carp-email", "my_pkg", "abc123"])
    def test_valid_name_accepted(self, make_package, good_name):
        pkg_dir = make_package(
            "pkg",
            f"""
            name: {good_name}
            version: "0.1"
            description: x
            """,
        )
        manifest = load_manifest(pkg_dir / "manifest.yaml")
        assert manifest.name == good_name


class TestMalformedYaml:
    def test_empty_manifest_rejected(self, make_package):
        pkg_dir = make_package("hello", "")
        with pytest.raises(ManifestError, match="empty"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_yaml_syntax_error_rejected(self, make_package):
        pkg_dir = make_package("hello", "name: [unclosed")
        with pytest.raises(ManifestError, match="Malformed YAML"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_non_mapping_rejected(self, make_package):
        # Top-level list, not a mapping
        pkg_dir = make_package("hello", "- a\n- b\n")
        with pytest.raises(ManifestError, match="must be a YAML mapping"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_chat_tools_must_be_list(self, make_package):
        pkg_dir = make_package(
            "hello",
            """
            name: hello
            version: "0.1"
            description: x
            chat_tools: "not a list"
            """,
        )
        with pytest.raises(ManifestError, match="must be a list"):
            load_manifest(pkg_dir / "manifest.yaml")


class TestFileNotFound:
    def test_missing_manifest_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_manifest(tmp_path / "no-such.yaml")

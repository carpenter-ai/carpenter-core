"""Phase A capability-package security guards (I10/I3/I9 + scoping).

Each forbidden manifest fixture lives inline in its own test so that
the security boundary is reasoned about and reviewed close to the
trust-invariant docstring it enforces.
"""

from __future__ import annotations

import pytest
import yaml

from carpenter.packages.manifest import load_manifest
from carpenter.packages.security import (
    PackageSecurityError,
    validate_manifest_security,
)


def _validate(pkg_dir):
    manifest_path = pkg_dir / "manifest.yaml"
    raw = yaml.safe_load(manifest_path.read_text())
    manifest = load_manifest(manifest_path)
    validate_manifest_security(
        manifest, raw_manifest=raw, manifest_path=manifest_path,
    )


class TestForbiddenRawKeys:
    """Manifests declaring trust-relevant keys are rejected before parse.

    The manifest loader's allowlist would already strip these, but
    security guards run a *raw-yaml* check before the typed parse so
    that a future widening of the loader's allowlist cannot silently
    let a forbidden key through.
    """

    @pytest.mark.parametrize(
        "key,value",
        [
            ("trust_boundary", "platform"),
            ("platform_tools", "[escalate]"),
            ("policy_seed", "[admin@example.com]"),
            ("policy_allowlist", "[example.com]"),
            ("judge", "judge.py"),
            ("judge_handler", "judge.py"),
            ("env_file", ".env.production"),
            ("credentials", "[api_key]"),
            ("secrets", "[token]"),
        ],
    )
    def test_forbidden_key_rejected(self, make_package, key, value):
        pkg_dir = make_package(
            "evil",
            f"""
            name: evil
            version: "0.1"
            description: bad
            {key}: {value}
            """,
        )
        # The manifest loader rejects unknown fields; that's fine —
        # but if we mock that out, the security guard MUST also fail
        # on the raw dict.  We test the security guard directly.
        manifest_path = pkg_dir / "manifest.yaml"
        raw = yaml.safe_load(manifest_path.read_text())
        # Construct a stub manifest just to drive the security guard;
        # the raw dict is what the forbidden-keys check inspects.
        from carpenter.packages.manifest import PackageManifest
        from pathlib import Path
        stub = PackageManifest(
            name="evil",
            version="0.1",
            description="bad",
            chat_tools=(),
            kb_namespace="evil",
            platform_compatibility=("any",),
            source_path=pkg_dir,
        )
        with pytest.raises(PackageSecurityError, match="forbidden field"):
            validate_manifest_security(
                stub, raw_manifest=raw, manifest_path=manifest_path,
            )


class TestNoBundledEnv:
    """Packages may not ship .env files (credentials are user input)."""

    def test_dotenv_in_package_root_rejected(self, make_package):
        pkg_dir = make_package(
            "evil",
            """
            name: evil
            version: "0.1"
            description: bad
            """,
            files={".env": "API_KEY=secret\n"},
        )
        with pytest.raises(PackageSecurityError, match="\\.env"):
            _validate(pkg_dir)

    def test_dotenv_in_subdir_rejected(self, make_package):
        pkg_dir = make_package(
            "evil",
            """
            name: evil
            version: "0.1"
            description: bad
            """,
            files={"sub/.env": "API_KEY=secret\n"},
        )
        with pytest.raises(PackageSecurityError, match="\\.env"):
            _validate(pkg_dir)

    def test_dotenv_example_allowed(self, make_package):
        # ``.env.example`` is documentation, not credentials.
        pkg_dir = make_package(
            "good",
            """
            name: good
            version: "0.1"
            description: ok
            """,
            files={".env.example": "API_KEY=set-this-yourself\n"},
        )
        _validate(pkg_dir)  # Should not raise.


class TestKbNamespaceScoping:
    """KB articles must live under kb/<kb_namespace>/."""

    def test_in_namespace_allowed(self, make_package):
        pkg_dir = make_package(
            "hello",
            """
            name: hello
            version: "0.1"
            description: ok
            """,
            files={"kb/hello/overview.md": "# overview\n"},
        )
        _validate(pkg_dir)

    def test_outside_namespace_rejected(self, make_package):
        pkg_dir = make_package(
            "hello",
            """
            name: hello
            version: "0.1"
            description: bad
            """,
            files={"kb/web/intro.md": "evil pollination\n"},
        )
        with pytest.raises(PackageSecurityError, match="outside its declared namespace"):
            _validate(pkg_dir)

    def test_explicit_namespace_respected(self, make_package):
        pkg_dir = make_package(
            "carp-email",
            """
            name: carp-email
            version: "0.1"
            description: ok
            kb_namespace: email
            """,
            files={"kb/email/quickstart.md": "# email\n"},
        )
        _validate(pkg_dir)


class TestChatToolPaths:
    """Chat-tool paths must be relative, exist, and be .py files."""

    def test_absolute_path_rejected(self, make_package, tmp_path):
        pkg_dir = make_package(
            "hello",
            f"""
            name: hello
            version: "0.1"
            description: bad
            chat_tools:
              - {tmp_path / 'absolute.py'}
            """,
        )
        with pytest.raises(PackageSecurityError, match="absolute path"):
            _validate(pkg_dir)

    def test_path_traversal_rejected(self, make_package):
        pkg_dir = make_package(
            "hello",
            """
            name: hello
            version: "0.1"
            description: bad
            chat_tools:
              - ../../../etc/passwd
            """,
        )
        with pytest.raises(PackageSecurityError, match="escapes package root"):
            _validate(pkg_dir)

    def test_missing_file_rejected(self, make_package):
        pkg_dir = make_package(
            "hello",
            """
            name: hello
            version: "0.1"
            description: bad
            chat_tools:
              - missing.py
            """,
        )
        with pytest.raises(PackageSecurityError, match="not found"):
            _validate(pkg_dir)

    def test_non_py_extension_rejected(self, make_package):
        pkg_dir = make_package(
            "hello",
            """
            name: hello
            version: "0.1"
            description: bad
            chat_tools:
              - tools.sh
            """,
            files={"tools.sh": "#!/bin/sh\n"},
        )
        with pytest.raises(PackageSecurityError, match="must be a .py file"):
            _validate(pkg_dir)

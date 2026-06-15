"""Tests for the ``credential_requirements`` manifest field.

Covers both ``kind: oauth`` (OAuth-callback) and ``kind: env`` (plain
env-var credentials such as an IMAP/SMTP app password).
"""

from __future__ import annotations

import pytest

from carpenter.packages.manifest import (
    EnvCredentialRef,
    ManifestError,
    OAuthCredentialRef,
    load_manifest,
)


_MIN_OAUTH = """
            name: pkg
            version: "0.1"
            description: x
            credential_requirements:
              - kind: oauth
                provider: google
                env_key_prefix: GMAIL_OAUTH
                authorize_url: https://accounts.google.com/o/oauth2/v2/auth
                token_url: https://oauth2.googleapis.com/token
                scopes:
                  - https://www.googleapis.com/auth/gmail.readonly
            """


_MIN_ENV = """
            name: pkg
            version: "0.1"
            description: x
            credential_requirements:
              - kind: env
                provider: imap_smtp
                env_key_prefix: IMAP_EMAIL
                required_keys:
                  - IMAP_HOST
                  - IMAP_PORT
                  - SMTP_HOST
                  - SMTP_PORT
                  - USERNAME
                  - PASSWORD
            """


class TestCredentialsField:
    def test_no_credentials_defaults_empty(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            """,
        )
        m = load_manifest(pkg_dir / "manifest.yaml")
        assert m.credential_requirements == ()

    def test_oauth_credential_parsed(self, make_package):
        pkg_dir = make_package("p", _MIN_OAUTH)
        m = load_manifest(pkg_dir / "manifest.yaml")
        assert len(m.credential_requirements) == 1
        cred = m.credential_requirements[0]
        assert isinstance(cred, OAuthCredentialRef)
        assert cred.kind == "oauth"
        assert cred.provider == "google"
        assert cred.env_key_prefix == "GMAIL_OAUTH"
        assert cred.authorize_url.startswith("https://accounts.google.com")
        assert cred.token_url.startswith("https://oauth2.googleapis.com")
        assert cred.scopes == (
            "https://www.googleapis.com/auth/gmail.readonly",
        )

    def test_multiple_credentials(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            credential_requirements:
              - kind: oauth
                provider: google
                env_key_prefix: GMAIL_OAUTH
                authorize_url: https://a.example/auth
                token_url: https://a.example/token
                scopes: [read]
              - kind: oauth
                provider: slack
                env_key_prefix: SLACK_OAUTH
                authorize_url: https://b.example/auth
                token_url: https://b.example/token
                scopes: [chat:write]
            """,
        )
        m = load_manifest(pkg_dir / "manifest.yaml")
        assert {c.env_key_prefix for c in m.credential_requirements} == {
            "GMAIL_OAUTH", "SLACK_OAUTH",
        }

    def test_unknown_kind_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            _MIN_OAUTH.replace("kind: oauth", "kind: api_key"),
        )
        with pytest.raises(ManifestError, match="kind must be 'oauth'"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_unknown_keys_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            credential_requirements:
              - kind: oauth
                provider: google
                env_key_prefix: G_OAUTH
                authorize_url: https://a.example/auth
                token_url: https://a.example/token
                scopes: [read]
                bogus: yes
            """,
        )
        with pytest.raises(ManifestError, match="unknown keys"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_missing_required_keys_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            credential_requirements:
              - kind: oauth
                provider: google
                env_key_prefix: G
                authorize_url: https://a.example/auth
                token_url: https://a.example/token
            """,
        )
        with pytest.raises(ManifestError, match="missing required"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_non_https_url_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            _MIN_OAUTH.replace(
                "https://accounts.google.com/o/oauth2/v2/auth",
                "http://insecure.example/auth",
            ),
        )
        with pytest.raises(ManifestError, match="https://"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_bad_env_prefix_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            _MIN_OAUTH.replace("GMAIL_OAUTH", "lowercase-bad"),
        )
        with pytest.raises(ManifestError, match="env_key_prefix"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_empty_scopes_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            credential_requirements:
              - kind: oauth
                provider: google
                env_key_prefix: GMAIL_OAUTH
                authorize_url: https://a.example/auth
                token_url: https://a.example/token
                scopes: []
            """,
        )
        with pytest.raises(ManifestError, match="scopes"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_duplicate_env_prefix_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            credential_requirements:
              - kind: oauth
                provider: google
                env_key_prefix: SAME
                authorize_url: https://a.example/auth
                token_url: https://a.example/token
                scopes: [read]
              - kind: oauth
                provider: other
                env_key_prefix: SAME
                authorize_url: https://b.example/auth
                token_url: https://b.example/token
                scopes: [write]
            """,
        )
        with pytest.raises(ManifestError, match="duplicate env_key_prefix"):
            load_manifest(pkg_dir / "manifest.yaml")


class TestEnvCredentialsField:
    def test_env_credential_parsed(self, make_package):
        pkg_dir = make_package("p", _MIN_ENV)
        m = load_manifest(pkg_dir / "manifest.yaml")
        assert len(m.credential_requirements) == 1
        cred = m.credential_requirements[0]
        assert isinstance(cred, EnvCredentialRef)
        assert cred.kind == "env"
        assert cred.provider == "imap_smtp"
        assert cred.env_key_prefix == "IMAP_EMAIL"
        assert cred.required_keys == (
            "IMAP_HOST",
            "IMAP_PORT",
            "SMTP_HOST",
            "SMTP_PORT",
            "USERNAME",
            "PASSWORD",
        )

    def test_unknown_keys_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            credential_requirements:
              - kind: env
                provider: imap_smtp
                env_key_prefix: IMAP_EMAIL
                required_keys: [USERNAME, PASSWORD]
                bogus: yes
            """,
        )
        with pytest.raises(ManifestError, match="unknown keys"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_missing_required_keys_rejected(self, make_package):
        # Missing the ``required_keys`` manifest key entirely.
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            credential_requirements:
              - kind: env
                provider: imap_smtp
                env_key_prefix: IMAP_EMAIL
            """,
        )
        with pytest.raises(ManifestError, match="missing required"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_bad_env_prefix_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            _MIN_ENV.replace("IMAP_EMAIL", "lowercase-bad"),
        )
        with pytest.raises(ManifestError, match="env_key_prefix"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_empty_required_keys_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            credential_requirements:
              - kind: env
                provider: imap_smtp
                env_key_prefix: IMAP_EMAIL
                required_keys: []
            """,
        )
        with pytest.raises(ManifestError, match="required_keys"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_non_uppercase_required_key_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            credential_requirements:
              - kind: env
                provider: imap_smtp
                env_key_prefix: IMAP_EMAIL
                required_keys: [username, PASSWORD]
            """,
        )
        with pytest.raises(ManifestError, match="required_keys"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_duplicate_required_key_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            credential_requirements:
              - kind: env
                provider: imap_smtp
                env_key_prefix: IMAP_EMAIL
                required_keys: [USERNAME, USERNAME]
            """,
        )
        with pytest.raises(ManifestError, match="duplicate"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_empty_provider_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            credential_requirements:
              - kind: env
                provider: ""
                env_key_prefix: IMAP_EMAIL
                required_keys: [USERNAME, PASSWORD]
            """,
        )
        with pytest.raises(ManifestError, match="provider"):
            load_manifest(pkg_dir / "manifest.yaml")

    def test_duplicate_env_prefix_across_entries_rejected(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            credential_requirements:
              - kind: env
                provider: imap_smtp
                env_key_prefix: SAME
                required_keys: [USERNAME]
              - kind: env
                provider: other
                env_key_prefix: SAME
                required_keys: [TOKEN]
            """,
        )
        with pytest.raises(ManifestError, match="duplicate env_key_prefix"):
            load_manifest(pkg_dir / "manifest.yaml")


class TestMixedCredentials:
    def test_mixed_oauth_and_env_parses_both(self, make_package):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            credential_requirements:
              - kind: oauth
                provider: google
                env_key_prefix: GMAIL_OAUTH
                authorize_url: https://a.example/auth
                token_url: https://a.example/token
                scopes: [read]
              - kind: env
                provider: imap_smtp
                env_key_prefix: IMAP_EMAIL
                required_keys: [USERNAME, PASSWORD]
            """,
        )
        m = load_manifest(pkg_dir / "manifest.yaml")
        assert len(m.credential_requirements) == 2
        oauth, env = m.credential_requirements
        assert isinstance(oauth, OAuthCredentialRef)
        assert oauth.env_key_prefix == "GMAIL_OAUTH"
        assert isinstance(env, EnvCredentialRef)
        assert env.env_key_prefix == "IMAP_EMAIL"
        assert env.required_keys == ("USERNAME", "PASSWORD")

    def test_mixed_duplicate_env_prefix_across_kinds_rejected(
        self, make_package,
    ):
        pkg_dir = make_package(
            "p",
            """
            name: p
            version: "0.1"
            description: x
            credential_requirements:
              - kind: oauth
                provider: google
                env_key_prefix: SHARED
                authorize_url: https://a.example/auth
                token_url: https://a.example/token
                scopes: [read]
              - kind: env
                provider: imap_smtp
                env_key_prefix: SHARED
                required_keys: [USERNAME, PASSWORD]
            """,
        )
        with pytest.raises(ManifestError, match="duplicate env_key_prefix"):
            load_manifest(pkg_dir / "manifest.yaml")

"""Tests for carpenter.core.backup — backup/restore of config directory."""

import hashlib
import json
import os
import zipfile

import pytest

from carpenter.core.backup import (
    BackupError,
    _collect_files,
    _decrypt_credentials,
    _encrypt_credentials,
    _read_dot_env,
    _sha256_file,
    _should_exclude,
    _validate_backup,
    _write_dot_env,
    create_backup,
    has_changes,
    init_config_repo,
    restore_backup,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config_dir(tmp_path):
    """Create a realistic config directory for testing."""
    config = tmp_path / "config_dir"
    config.mkdir()

    # config.yaml
    (config / "config.yaml").write_text(
        "port: 7842\nhost: 127.0.0.1\n"
    )

    # chat_tools/
    (config / "chat_tools").mkdir()
    (config / "chat_tools" / "greet.py").write_text(
        "def run(): return 'hello'\n"
    )

    # data_models/
    (config / "data_models").mkdir()
    (config / "data_models" / "user.py").write_text(
        "class User: pass\n"
    )

    # kb/ (nested)
    (config / "kb").mkdir()
    (config / "kb" / "topic.md").write_text("# Topic\nSome knowledge.\n")
    (config / "kb" / "sub").mkdir()
    (config / "kb" / "sub" / "detail.md").write_text("Detail info.\n")

    # config_seed/
    (config / "config_seed").mkdir()
    (config / "config_seed" / "defaults.yaml").write_text("key: value\n")

    return config


def _make_dot_env(config_dir, creds=None):
    """Write a .env file into config_dir."""
    if creds is None:
        creds = {
            "ANTHROPIC_API_KEY": "sk-test-key-123",
            "UI_TOKEN": "tok-abc-456",
        }
    env_path = config_dir / ".env"
    lines = [f"{k}={v}" for k, v in creds.items()]
    env_path.write_text("\n".join(lines) + "\n")
    return creds


# ---------------------------------------------------------------------------
# File collection and exclusion
# ---------------------------------------------------------------------------

class TestExclusion:
    """Test file exclusion rules."""

    def test_exclude_git_dir(self):
        assert _should_exclude(".git/config", [])
        assert _should_exclude(".git/objects/abc123", [])

    def test_exclude_secrets_dir(self):
        assert _should_exclude("secrets/api.key", [])

    def test_exclude_env_file(self):
        assert _should_exclude(".env", [])

    def test_exclude_pycache(self):
        assert _should_exclude("__pycache__/module.cpython-311.pyc", [])

    def test_exclude_pyc_files(self):
        assert _should_exclude("chat_tools/greet.pyc", [])

    def test_include_normal_files(self):
        assert not _should_exclude("config.yaml", [])
        assert not _should_exclude("chat_tools/greet.py", [])
        assert not _should_exclude("kb/topic.md", [])

    def test_syncignore_patterns(self):
        patterns = ["*.tmp", "drafts/*"]
        assert _should_exclude("notes.tmp", patterns)
        assert _should_exclude("drafts/wip.md", patterns)
        assert not _should_exclude("config.yaml", patterns)

    def test_collect_files_excludes_properly(self, tmp_path):
        config = _make_config_dir(tmp_path)

        # Add files that should be excluded
        (config / ".env").write_text("SECRET=123\n")
        (config / "secrets").mkdir()
        (config / "secrets" / "key.pem").write_text("private\n")
        (config / "__pycache__").mkdir()
        (config / "__pycache__" / "mod.cpython-311.pyc").write_bytes(b"\x00")

        files = _collect_files(str(config))

        assert "config.yaml" in files
        assert "chat_tools/greet.py" in files
        assert "kb/topic.md" in files
        assert "kb/sub/detail.md" in files

        assert ".env" not in files
        assert "secrets/key.pem" not in files
        assert "__pycache__/mod.cpython-311.pyc" not in files

    def test_collect_files_with_syncignore(self, tmp_path):
        config = _make_config_dir(tmp_path)
        (config / ".syncignore").write_text("*.tmp\ndrafts/*\n")
        (config / "notes.tmp").write_text("temp\n")
        (config / "drafts").mkdir()
        (config / "drafts" / "wip.md").write_text("wip\n")

        files = _collect_files(str(config))
        assert "notes.tmp" not in files
        assert "drafts/wip.md" not in files
        # .syncignore itself is included
        assert ".syncignore" in files


# ---------------------------------------------------------------------------
# Git repo management
# ---------------------------------------------------------------------------

class TestGitRepo:
    """Test git repo initialization and change detection."""

    def test_init_creates_repo(self, tmp_path):
        config = _make_config_dir(tmp_path)
        init_config_repo(str(config))
        assert (config / ".git").is_dir()
        assert (config / ".gitignore").is_file()

    def test_init_idempotent(self, tmp_path):
        config = _make_config_dir(tmp_path)
        init_config_repo(str(config))
        init_config_repo(str(config))  # should not raise
        assert (config / ".git").is_dir()

    def test_has_changes_no_repo(self, tmp_path):
        config = _make_config_dir(tmp_path)
        assert not has_changes(str(config))

    def test_has_changes_clean(self, tmp_path):
        config = _make_config_dir(tmp_path)
        init_config_repo(str(config))
        assert not has_changes(str(config))

    def test_has_changes_modified_file(self, tmp_path):
        config = _make_config_dir(tmp_path)
        init_config_repo(str(config))

        # Modify a file
        (config / "config.yaml").write_text("port: 9999\n")
        assert has_changes(str(config))

    def test_has_changes_new_file(self, tmp_path):
        config = _make_config_dir(tmp_path)
        init_config_repo(str(config))

        # Add a new file
        (config / "new_tool.py").write_text("# new\n")
        assert has_changes(str(config))

    def test_has_changes_deleted_file(self, tmp_path):
        config = _make_config_dir(tmp_path)
        init_config_repo(str(config))

        # Delete a file
        (config / "config.yaml").unlink()
        assert has_changes(str(config))


# ---------------------------------------------------------------------------
# Credential encryption
# ---------------------------------------------------------------------------

class TestCredentialEncryption:
    """Test credential encryption/decryption round-trip."""

    def test_round_trip(self):
        creds = {"ANTHROPIC_API_KEY": "sk-test-123", "UI_TOKEN": "tok-456"}
        password = "hunter2"
        encrypted = _encrypt_credentials(creds, password)
        decrypted = _decrypt_credentials(encrypted, password)
        assert decrypted == creds

    def test_wrong_password(self):
        from cryptography.fernet import InvalidToken

        creds = {"KEY": "value"}
        encrypted = _encrypt_credentials(creds, "correct")
        with pytest.raises(InvalidToken):
            _decrypt_credentials(encrypted, "wrong")

    def test_encrypted_data_has_salt_prefix(self):
        creds = {"K": "V"}
        enc = _encrypt_credentials(creds, "pw")
        # Salt is 16 bytes, followed by Fernet token (at least ~100 bytes)
        assert len(enc) > 16

    def test_different_salts(self):
        """Each encryption should use a unique random salt."""
        creds = {"K": "V"}
        enc1 = _encrypt_credentials(creds, "pw")
        enc2 = _encrypt_credentials(creds, "pw")
        # Salt (first 16 bytes) should differ
        assert enc1[:16] != enc2[:16]


# ---------------------------------------------------------------------------
# .env helpers
# ---------------------------------------------------------------------------

class TestDotEnv:
    """Test .env file reading and writing."""

    def test_read_dot_env(self, tmp_path):
        config = tmp_path / "cfg"
        config.mkdir()
        creds = _make_dot_env(config)
        result = _read_dot_env(str(config))
        assert result == creds

    def test_read_missing_env(self, tmp_path):
        config = tmp_path / "cfg"
        config.mkdir()
        assert _read_dot_env(str(config)) == {}

    def test_read_env_with_comments(self, tmp_path):
        config = tmp_path / "cfg"
        config.mkdir()
        (config / ".env").write_text(
            "# comment\nKEY=val\n\n# another\nOTHER=123\n"
        )
        result = _read_dot_env(str(config))
        assert result == {"KEY": "val", "OTHER": "123"}

    def test_write_dot_env(self, tmp_path):
        config = tmp_path / "cfg"
        config.mkdir()
        _write_dot_env(str(config), {"A": "1", "B": "2"})
        result = _read_dot_env(str(config))
        assert result == {"A": "1", "B": "2"}

    def test_write_dot_env_merges(self, tmp_path):
        config = tmp_path / "cfg"
        config.mkdir()
        _write_dot_env(str(config), {"A": "1"})
        _write_dot_env(str(config), {"B": "2"})
        result = _read_dot_env(str(config))
        assert result == {"A": "1", "B": "2"}


# ---------------------------------------------------------------------------
# Backup creation
# ---------------------------------------------------------------------------

class TestCreateBackup:
    """Test backup ZIP creation."""

    def test_creates_zip(self, tmp_path):
        config = _make_config_dir(tmp_path)
        output = str(tmp_path / "backup.zip")
        result = create_backup(str(config), output)
        assert result == output
        assert os.path.isfile(output)

    def test_auto_filename_when_dir(self, tmp_path):
        config = _make_config_dir(tmp_path)
        output_dir = tmp_path / "backups"
        output_dir.mkdir()
        result = create_backup(str(config), str(output_dir))
        assert result.startswith(str(output_dir))
        assert result.endswith(".zip")
        assert "backup-" in os.path.basename(result)

    def test_zip_contains_manifest(self, tmp_path):
        config = _make_config_dir(tmp_path)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output)

        with zipfile.ZipFile(output, "r") as zf:
            assert "manifest.json" in zf.namelist()
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["schema_version"] == 1
            assert "timestamp" in manifest
            assert "checksums" in manifest
            assert "platform" in manifest

    def test_zip_contains_config_files(self, tmp_path):
        config = _make_config_dir(tmp_path)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output)

        with zipfile.ZipFile(output, "r") as zf:
            names = zf.namelist()
            assert "config.yaml" in names
            assert "chat_tools/greet.py" in names
            assert "data_models/user.py" in names
            assert "kb/topic.md" in names
            assert "kb/sub/detail.md" in names

    def test_zip_excludes_env(self, tmp_path):
        config = _make_config_dir(tmp_path)
        _make_dot_env(config)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output)

        with zipfile.ZipFile(output, "r") as zf:
            names = zf.namelist()
            assert ".env" not in names

    def test_zip_excludes_secrets(self, tmp_path):
        config = _make_config_dir(tmp_path)
        (config / "secrets").mkdir()
        (config / "secrets" / "key.pem").write_text("private\n")
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output)

        with zipfile.ZipFile(output, "r") as zf:
            names = zf.namelist()
            assert "secrets/key.pem" not in names

    def test_checksums_in_manifest(self, tmp_path):
        config = _make_config_dir(tmp_path)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output)

        with zipfile.ZipFile(output, "r") as zf:
            manifest = json.loads(zf.read("manifest.json"))
            checksums = manifest["checksums"]
            # Verify a checksum matches
            config_data = zf.read("config.yaml")
            expected = hashlib.sha256(config_data).hexdigest()
            assert checksums["config.yaml"] == expected

    def test_backup_with_password_includes_credentials(self, tmp_path):
        config = _make_config_dir(tmp_path)
        creds = _make_dot_env(config)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output, password="secret123")

        with zipfile.ZipFile(output, "r") as zf:
            names = zf.namelist()
            assert "credentials.enc" in names
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["credentials_included"] is True

    def test_backup_without_password_no_credentials(self, tmp_path):
        config = _make_config_dir(tmp_path)
        _make_dot_env(config)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output)

        with zipfile.ZipFile(output, "r") as zf:
            names = zf.namelist()
            assert "credentials.enc" not in names
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["credentials_included"] is False

    def test_backup_with_password_no_env(self, tmp_path):
        """Password provided but no .env file — no credentials.enc."""
        config = _make_config_dir(tmp_path)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output, password="secret123")

        with zipfile.ZipFile(output, "r") as zf:
            names = zf.namelist()
            assert "credentials.enc" not in names

    def test_commit_sha_in_manifest(self, tmp_path):
        config = _make_config_dir(tmp_path)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output)

        with zipfile.ZipFile(output, "r") as zf:
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["commit_sha"] is not None
            assert len(manifest["commit_sha"]) == 40  # full SHA-1 hex

    def test_uses_deflated_compression(self, tmp_path):
        config = _make_config_dir(tmp_path)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output)

        with zipfile.ZipFile(output, "r") as zf:
            for info in zf.infolist():
                assert info.compress_type == zipfile.ZIP_DEFLATED


# ---------------------------------------------------------------------------
# Backup validation
# ---------------------------------------------------------------------------

class TestValidation:
    """Test backup ZIP validation."""

    def test_valid_backup(self, tmp_path):
        config = _make_config_dir(tmp_path)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output)
        manifest = _validate_backup(output)
        assert manifest["schema_version"] == 1

    def test_missing_file(self, tmp_path):
        with pytest.raises(BackupError, match="not found"):
            _validate_backup(str(tmp_path / "nonexistent.zip"))

    def test_invalid_zip(self, tmp_path):
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"not a zip file")
        with pytest.raises(BackupError, match="Invalid ZIP"):
            _validate_backup(str(bad))

    def test_missing_manifest(self, tmp_path):
        bad = tmp_path / "no_manifest.zip"
        with zipfile.ZipFile(str(bad), "w") as zf:
            zf.writestr("hello.txt", "world")
        with pytest.raises(BackupError, match="missing manifest"):
            _validate_backup(str(bad))

    def test_checksum_mismatch(self, tmp_path):
        bad = tmp_path / "bad_checksum.zip"
        manifest = {
            "schema_version": 1,
            "checksums": {"file.txt": "0000000000000000000000000000000000000000000000000000000000000000"},
        }
        with zipfile.ZipFile(str(bad), "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("file.txt", "actual content")
        with pytest.raises(BackupError, match="Checksum mismatch"):
            _validate_backup(str(bad))

    def test_file_in_manifest_but_missing_from_zip(self, tmp_path):
        bad = tmp_path / "missing_file.zip"
        manifest = {
            "schema_version": 1,
            "checksums": {"gone.txt": "abc123"},
        }
        with zipfile.ZipFile(str(bad), "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
        with pytest.raises(BackupError, match="missing from ZIP"):
            _validate_backup(str(bad))


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

class TestRestore:
    """Test backup restoration."""

    def test_round_trip(self, tmp_path):
        """Create backup, modify config, restore, verify original content."""
        config = _make_config_dir(tmp_path)
        original_yaml = (config / "config.yaml").read_text()
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output)

        # Modify the config
        (config / "config.yaml").write_text("port: 9999\n")
        (config / "chat_tools" / "greet.py").unlink()
        (config / "new_file.txt").write_text("should survive restore\n")

        # Restore
        restore_backup(output, str(config))

        # Verify original content is back
        assert (config / "config.yaml").read_text() == original_yaml
        assert (config / "chat_tools" / "greet.py").is_file()

    def test_restore_to_empty_dir(self, tmp_path):
        """Restore to a fresh directory."""
        config = _make_config_dir(tmp_path)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output)

        # Restore to new location
        new_config = tmp_path / "restored"
        restore_backup(output, str(new_config))

        assert (new_config / "config.yaml").is_file()
        assert (new_config / "chat_tools" / "greet.py").is_file()
        assert (new_config / "kb" / "sub" / "detail.md").is_file()

    def test_restore_creates_safety_backup(self, tmp_path):
        """When restoring over existing files, a safety backup is created."""
        config = _make_config_dir(tmp_path)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output)

        # Restore over existing — should create safety backup
        restore_backup(output, str(config))

        # Check safety backup exists
        parent = config.parent
        safety_dirs = [
            d for d in parent.iterdir()
            if d.name.startswith("config_dir.pre-restore-")
        ]
        assert len(safety_dirs) == 1
        assert (safety_dirs[0] / "config.yaml").is_file()

    def test_restore_with_credentials(self, tmp_path):
        """Restore with encrypted credentials."""
        config = _make_config_dir(tmp_path)
        creds = _make_dot_env(config)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output, password="secret123")

        # Remove .env and restore
        (config / ".env").unlink()
        restore_backup(output, str(config), password="secret123")

        # Verify credentials restored
        restored = _read_dot_env(str(config))
        assert restored == creds

    def test_restore_wrong_password(self, tmp_path):
        """Restore with wrong password raises BackupError."""
        config = _make_config_dir(tmp_path)
        _make_dot_env(config)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output, password="correct")

        with pytest.raises(BackupError, match="wrong password"):
            restore_backup(output, str(config), password="wrong")

    def test_restore_no_password_skips_credentials(self, tmp_path):
        """Restore without password when backup has credentials — skips them."""
        config = _make_config_dir(tmp_path)
        _make_dot_env(config)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output, password="secret")

        # Remove .env
        (config / ".env").unlink()

        # Restore without password — should not crash, just skip credentials
        restore_backup(output, str(config))
        assert not (config / ".env").is_file()

    def test_restore_preserves_nested_dirs(self, tmp_path):
        """Nested directories (kb/sub/) should be created properly."""
        config = _make_config_dir(tmp_path)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output)

        new_config = tmp_path / "fresh"
        restore_backup(output, str(new_config))

        assert (new_config / "kb" / "sub" / "detail.md").read_text() == "Detail info.\n"

    def test_restore_commits_to_git(self, tmp_path):
        """After restore, the config dir should have a git repo with the restored state committed."""
        config = _make_config_dir(tmp_path)
        output = str(tmp_path / "backup.zip")
        create_backup(str(config), output)

        new_config = tmp_path / "fresh"
        restore_backup(output, str(new_config))

        # Should be a git repo with no pending changes
        assert (new_config / ".git").is_dir()
        assert not has_changes(str(new_config))

    def test_restore_invalid_zip(self, tmp_path):
        config = _make_config_dir(tmp_path)
        bad_zip = tmp_path / "bad.zip"
        bad_zip.write_bytes(b"not a zip")
        with pytest.raises(BackupError):
            restore_backup(str(bad_zip), str(config))


# ---------------------------------------------------------------------------
# SHA256 helper
# ---------------------------------------------------------------------------

class TestSha256:
    def test_sha256_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world\n")
        expected = hashlib.sha256(b"hello world\n").hexdigest()
        assert _sha256_file(str(f)) == expected

    def test_sha256_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        expected = hashlib.sha256(b"").hexdigest()
        assert _sha256_file(str(f)) == expected


# ---------------------------------------------------------------------------
# Integration: full workflow
# ---------------------------------------------------------------------------

class TestIntegrationWorkflow:
    """End-to-end workflow: create, modify, detect changes, backup, restore."""

    def test_full_workflow(self, tmp_path):
        # 1. Create config dir
        config = _make_config_dir(tmp_path)
        config_str = str(config)

        # 2. Initialize repo
        init_config_repo(config_str)
        assert not has_changes(config_str)

        # 3. Make changes
        (config / "config.yaml").write_text("port: 8080\nhost: 0.0.0.0\n")
        (config / "chat_tools" / "new_tool.py").write_text("def run(): pass\n")
        assert has_changes(config_str)

        # 4. Backup (with credentials)
        _make_dot_env(config)
        output = str(tmp_path / "full_backup.zip")
        create_backup(config_str, output, password="mypass")
        assert not has_changes(config_str)  # auto-committed

        # 5. Trash the config
        (config / "config.yaml").write_text("BROKEN\n")
        (config / "chat_tools" / "new_tool.py").unlink()

        # 6. Restore
        restore_backup(output, config_str, password="mypass")

        # 7. Verify
        assert (config / "config.yaml").read_text() == "port: 8080\nhost: 0.0.0.0\n"
        assert (config / "chat_tools" / "new_tool.py").is_file()
        assert not has_changes(config_str)

        # 8. Credentials restored
        creds = _read_dot_env(config_str)
        assert "ANTHROPIC_API_KEY" in creds
        assert creds["ANTHROPIC_API_KEY"] == "sk-test-key-123"

    def test_cross_platform_restore(self, tmp_path):
        """Backup on one 'platform', restore to different location."""
        src = _make_config_dir(tmp_path)
        backup_path = str(tmp_path / "portable.zip")
        create_backup(str(src), backup_path)

        # Restore to completely different path
        dst = tmp_path / "other_machine" / "carpenter"
        restore_backup(backup_path, str(dst))

        assert (dst / "config.yaml").is_file()
        assert (dst / "kb" / "sub" / "detail.md").is_file()
        # Git repo initialized at destination
        assert (dst / ".git").is_dir()
        assert not has_changes(str(dst))

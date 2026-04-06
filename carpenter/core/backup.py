"""Backup and restore for Carpenter's configuration directory.

Creates portable ZIP archives of the config directory for disaster recovery.
Uses a local git repo (via dulwich) for change detection and history.

The config directory is treated as a local git repository:
- Change detection via ``porcelain.status()`` (stat-based, near-instant)
- Commit SHA provides a Merkle root over all tracked files
- Commit history records what changed when
- Foundation for future sync features

Backup ZIP structure::

    backup-2026-04-04T15-30-00.zip/
      manifest.json              # version, timestamp, platform, commit SHA, checksums
      config.yaml
      chat_tools/*.py
      data_models/*.py
      kb/**                      # knowledge base (recursive)
      config_seed/               # customized seed files
      credentials.enc            # ONLY if password provided

Excluded from backup:
- ``.git/`` directory (internal state, not portable)
- ``.env`` files (credentials unless explicitly encrypted)
- ``secrets/`` directory
- Files matching ``.syncignore`` patterns
"""

import hashlib
import json
import logging
import os
import platform
import time
import zipfile
from pathlib import Path

import dulwich.porcelain as porcelain
from dulwich.repo import Repo

logger = logging.getLogger(__name__)

# Author/committer identity for auto-commits.
_GIT_IDENTITY = b"Carpenter Backup <backup@carpenter.local>"

# Manifest schema version — bump when the backup format changes.
_MANIFEST_VERSION = 1

# Patterns for files/dirs that are never backed up.
_EXCLUDE_DIRS = {".git", "secrets", "__pycache__", ".mypy_cache", ".pytest_cache"}
_EXCLUDE_FILES = {".env"}
_EXCLUDE_SUFFIXES = {".pyc", ".pyo"}

# PBKDF2 iteration count for credential encryption key derivation.
_PBKDF2_ITERATIONS = 600_000
_SALT_LENGTH = 16


def _load_syncignore(config_dir: str) -> list[str]:
    """Load .syncignore patterns from config dir (one glob per line).

    Returns an empty list if the file does not exist.
    """
    path = os.path.join(config_dir, ".syncignore")
    if not os.path.isfile(path):
        return []
    patterns = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return patterns


def _matches_syncignore(rel_path: str, patterns: list[str]) -> bool:
    """Check if a relative path matches any .syncignore pattern.

    Supports simple glob matching via Path.match().
    """
    p = Path(rel_path)
    for pattern in patterns:
        if p.match(pattern):
            return True
    return False


def _should_exclude(rel_path: str, syncignore_patterns: list[str]) -> bool:
    """Determine if a relative path should be excluded from backup."""
    parts = Path(rel_path).parts

    # Check directory exclusions
    for part in parts[:-1]:  # all components except the filename
        if part in _EXCLUDE_DIRS:
            return True

    # Check top-level directory name for the last component too (if it's a dir)
    filename = parts[-1] if parts else ""

    # Check file-level exclusions
    if filename in _EXCLUDE_FILES:
        return True
    if any(filename.endswith(suffix) for suffix in _EXCLUDE_SUFFIXES):
        return True

    # Check .syncignore patterns
    if syncignore_patterns and _matches_syncignore(rel_path, syncignore_patterns):
        return True

    return False


def _sha256_file(path: str) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _collect_files(config_dir: str) -> list[str]:
    """Walk config_dir and return relative paths of files to back up.

    Applies exclusion rules and .syncignore patterns.
    """
    syncignore = _load_syncignore(config_dir)
    result = []
    for dirpath, dirnames, filenames in os.walk(config_dir):
        # Prune excluded directories in-place so os.walk skips them
        dirnames[:] = [
            d for d in dirnames
            if d not in _EXCLUDE_DIRS
        ]
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, config_dir)
            if not _should_exclude(rel, syncignore):
                result.append(rel)
    result.sort()
    return result


# ---------------------------------------------------------------------------
# Git repo management
# ---------------------------------------------------------------------------

def init_config_repo(config_dir: str) -> None:
    """Initialize config dir as a local git repo if not already one.

    Creates an initial commit of all trackable files so that subsequent
    ``has_changes()`` calls have a baseline.
    """
    git_dir = os.path.join(config_dir, ".git")
    if os.path.isdir(git_dir):
        logger.debug("Config repo already initialized at %s", config_dir)
        return

    logger.info("Initializing config repo at %s", config_dir)
    porcelain.init(config_dir)

    # Create .gitignore for files we never track
    gitignore_path = os.path.join(config_dir, ".gitignore")
    if not os.path.exists(gitignore_path):
        gitignore_lines = [
            "# Carpenter backup — auto-generated .gitignore",
            ".env",
            "secrets/",
            "__pycache__/",
            "*.pyc",
            "*.pyo",
            ".mypy_cache/",
            ".pytest_cache/",
            "",
        ]
        with open(gitignore_path, "w") as f:
            f.write("\n".join(gitignore_lines))

    # Stage all trackable files and make initial commit
    _stage_all_trackable(config_dir)
    _auto_commit(config_dir, "Initial config snapshot")


def _stage_all_trackable(config_dir: str) -> None:
    """Stage all non-excluded files in the config repo.

    Uses ``porcelain.add()`` which stages new, modified, and deleted files.
    """
    porcelain.add(config_dir)


def _auto_commit(config_dir: str, message: str | None = None) -> str | None:
    """Create an auto-commit with the given message.

    Returns the hex SHA of the new commit, or None if there was nothing
    to commit.
    """
    status = porcelain.status(config_dir)

    has_staged = (
        status.staged["add"]
        or status.staged["modify"]
        or status.staged["delete"]
    )

    if not has_staged:
        # Nothing staged — check for unstaged/untracked and stage them
        has_work = (
            status.unstaged
            or status.untracked
        )
        if not has_work:
            return None
        _stage_all_trackable(config_dir)
        # Re-check after staging
        status = porcelain.status(config_dir)
        has_staged = (
            status.staged["add"]
            or status.staged["modify"]
            or status.staged["delete"]
        )
        if not has_staged:
            return None

    if message is None:
        message = f"Auto-backup {time.strftime('%Y-%m-%dT%H:%M:%S')}"

    sha = porcelain.commit(
        config_dir,
        message=message.encode("utf-8"),
        author=_GIT_IDENTITY,
        committer=_GIT_IDENTITY,
    )
    logger.info("Auto-committed config: %s (%s)", message, sha.decode())
    return sha.decode() if isinstance(sha, bytes) else str(sha)


def has_changes(config_dir: str) -> bool:
    """Check if config dir has changes since last commit.

    Uses ``porcelain.status()`` which relies on stat info (mtime/size)
    as a fast check before hashing — near-instant on typical config dirs.

    Returns False if the directory is not a git repo (not initialized).
    """
    git_dir = os.path.join(config_dir, ".git")
    if not os.path.isdir(git_dir):
        return False

    status = porcelain.status(config_dir)
    return bool(
        status.unstaged
        or status.untracked
        or status.staged["add"]
        or status.staged["modify"]
        or status.staged["delete"]
    )


def _get_head_sha(config_dir: str) -> str | None:
    """Return the hex SHA of HEAD, or None if no commits exist."""
    try:
        repo = Repo(config_dir)
        head = repo.head()
        return head.decode() if isinstance(head, bytes) else str(head)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Credential encryption
# ---------------------------------------------------------------------------

def _derive_fernet(password: str, salt: bytes):
    """Derive a Fernet instance from a password and salt via PBKDF2.

    Args:
        password: The user-provided password string.
        salt: Random salt bytes (should be ``_SALT_LENGTH`` bytes).

    Returns:
        A ``cryptography.fernet.Fernet`` instance keyed from the password.
    """
    from cryptography.fernet import Fernet
    import base64

    key = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS,
    )
    fernet_key = base64.urlsafe_b64encode(key)
    return Fernet(fernet_key)


def _encrypt_credentials(cred_data: dict, password: str) -> bytes:
    """Encrypt credential dict with a password-derived key.

    Returns salt (16 bytes) + Fernet-encrypted ciphertext.
    Raises ImportError if cryptography is not available.
    """
    salt = os.urandom(_SALT_LENGTH)
    f = _derive_fernet(password, salt)

    plaintext = json.dumps(cred_data, sort_keys=True).encode("utf-8")
    ciphertext = f.encrypt(plaintext)
    return salt + ciphertext


def _decrypt_credentials(data: bytes, password: str) -> dict:
    """Decrypt credential blob (salt + ciphertext) with a password.

    Returns the credential dict.
    Raises ImportError if cryptography is not available.
    Raises cryptography.fernet.InvalidToken on wrong password.
    """
    salt = data[:_SALT_LENGTH]
    ciphertext = data[_SALT_LENGTH:]

    f = _derive_fernet(password, salt)

    plaintext = f.decrypt(ciphertext)
    return json.loads(plaintext.decode("utf-8"))


def _read_dot_env(config_dir: str) -> dict:
    """Read .env file and return key-value pairs as a dict.

    Returns empty dict if .env does not exist.
    """
    env_path = os.path.join(config_dir, ".env")
    if not os.path.isfile(env_path):
        return {}

    result = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def _write_dot_env(config_dir: str, creds: dict) -> None:
    """Write credential key-value pairs to .env file.

    Merges with existing .env content (new values overwrite existing keys).
    """
    existing = _read_dot_env(config_dir)
    existing.update(creds)

    env_path = os.path.join(config_dir, ".env")
    with open(env_path, "w") as f:
        f.write("# Carpenter credentials — restored from backup\n")
        for key, value in sorted(existing.items()):
            f.write(f"{key}={value}\n")


# ---------------------------------------------------------------------------
# Backup creation
# ---------------------------------------------------------------------------

def create_backup(
    config_dir: str,
    output_path: str,
    password: str | None = None,
) -> str:
    """Create a backup ZIP of the config directory.

    Steps:
    1. Initialize git repo if needed
    2. Auto-commit any pending changes
    3. Collect files to back up (applying exclusion rules)
    4. Optionally encrypt credentials if password provided
    5. Write ZIP with manifest

    Args:
        config_dir: Path to the Carpenter config directory.
        output_path: Path for the output ZIP file. If this is a directory,
            a timestamped filename will be generated.
        password: Optional password for credential encryption. If provided,
            .env credentials are encrypted and included as credentials.enc.

    Returns:
        Absolute path to the created ZIP file.
    """
    config_dir = os.path.abspath(config_dir)

    # Ensure git repo exists
    init_config_repo(config_dir)

    # Auto-commit pending changes
    _auto_commit(config_dir)

    # Resolve output path
    if os.path.isdir(output_path):
        timestamp = time.strftime("%Y-%m-%dT%H-%M-%S")
        filename = f"backup-{timestamp}.zip"
        output_path = os.path.join(output_path, filename)
    output_path = os.path.abspath(output_path)

    # Collect files
    files = _collect_files(config_dir)

    # Compute checksums
    checksums = {}
    for rel_path in files:
        full_path = os.path.join(config_dir, rel_path)
        checksums[rel_path] = _sha256_file(full_path)

    # Get commit SHA
    commit_sha = _get_head_sha(config_dir)

    # Handle credentials
    credentials_encrypted = False
    if password:
        creds = _read_dot_env(config_dir)
        if creds:
            try:
                enc_data = _encrypt_credentials(creds, password)
                credentials_encrypted = True
            except ImportError:
                logger.warning(
                    "cryptography package not available — "
                    "skipping credential backup"
                )
                enc_data = None
        else:
            enc_data = None
    else:
        enc_data = None

    # Build manifest
    manifest = {
        "schema_version": _MANIFEST_VERSION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "timestamp_unix": int(time.time()),
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "commit_sha": commit_sha,
        "credentials_included": credentials_encrypted,
        "file_count": len(files),
        "checksums": checksums,
    }

    # Create ZIP
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Write manifest first
        manifest_json = json.dumps(manifest, indent=2, sort_keys=True)
        zf.writestr("manifest.json", manifest_json)

        # Write config files
        for rel_path in files:
            full_path = os.path.join(config_dir, rel_path)
            zf.write(full_path, rel_path)

        # Write encrypted credentials if available
        if enc_data is not None:
            zf.writestr("credentials.enc", enc_data)

    logger.info(
        "Backup created: %s (%d files, %.1f KB)",
        output_path,
        len(files),
        os.path.getsize(output_path) / 1024,
    )
    return output_path


# ---------------------------------------------------------------------------
# Backup restoration
# ---------------------------------------------------------------------------

class BackupError(Exception):
    """Raised when backup validation or restoration fails."""


def _validate_backup(zip_path: str) -> dict:
    """Validate backup ZIP structure and return parsed manifest.

    Raises BackupError if validation fails.
    """
    if not os.path.isfile(zip_path):
        raise BackupError(f"Backup file not found: {zip_path}")

    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except zipfile.BadZipFile:
        raise BackupError(f"Invalid ZIP file: {zip_path}")

    with zf:
        if "manifest.json" not in zf.namelist():
            raise BackupError("Backup ZIP missing manifest.json")

        try:
            manifest_data = zf.read("manifest.json")
            manifest = json.loads(manifest_data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BackupError(f"Invalid manifest.json: {exc}")

        if "schema_version" not in manifest:
            raise BackupError("Manifest missing schema_version")

        if "checksums" not in manifest:
            raise BackupError("Manifest missing checksums")

        # Verify file checksums
        checksums = manifest["checksums"]
        for rel_path, expected_hash in checksums.items():
            if rel_path not in zf.namelist():
                raise BackupError(
                    f"File listed in manifest but missing from ZIP: {rel_path}"
                )
            data = zf.read(rel_path)
            actual_hash = hashlib.sha256(data).hexdigest()
            if actual_hash != expected_hash:
                raise BackupError(
                    f"Checksum mismatch for {rel_path}: "
                    f"expected {expected_hash[:12]}..., "
                    f"got {actual_hash[:12]}..."
                )

    return manifest


def restore_backup(
    zip_path: str,
    config_dir: str,
    password: str | None = None,
) -> None:
    """Restore config directory from a backup ZIP.

    Steps:
    1. Validate ZIP structure and checksums
    2. If config_dir exists and has content, create a safety backup
    3. Extract files to config_dir
    4. If credentials.enc present and password provided, decrypt and restore
    5. Commit the restored state to the local git repo

    Args:
        zip_path: Path to the backup ZIP file.
        config_dir: Path to the target config directory.
        password: Password for decrypting credentials.enc (if present).

    Raises:
        BackupError: If the ZIP is invalid, checksums fail, or
            credential decryption fails.
    """
    zip_path = os.path.abspath(zip_path)
    config_dir = os.path.abspath(config_dir)

    # Validate
    manifest = _validate_backup(zip_path)
    logger.info(
        "Restoring backup from %s (created %s, %d files)",
        zip_path,
        manifest.get("timestamp", "unknown"),
        manifest.get("file_count", 0),
    )

    # Create safety backup of existing config if it has content
    _create_safety_backup(config_dir)

    # Ensure config dir exists
    os.makedirs(config_dir, exist_ok=True)

    # Extract files
    checksums = manifest["checksums"]
    with zipfile.ZipFile(zip_path, "r") as zf:
        for rel_path in checksums:
            target = os.path.join(config_dir, rel_path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            data = zf.read(rel_path)
            with open(target, "wb") as f:
                f.write(data)

        # Handle encrypted credentials
        if manifest.get("credentials_included") and "credentials.enc" in zf.namelist():
            if password:
                enc_data = zf.read("credentials.enc")
                try:
                    creds = _decrypt_credentials(enc_data, password)
                except ImportError:
                    raise BackupError(
                        "cryptography package required to decrypt credentials "
                        "but is not installed"
                    )
                except Exception as exc:
                    raise BackupError(
                        f"Failed to decrypt credentials (wrong password?): {exc}"
                    )
                _write_dot_env(config_dir, creds)
                logger.info("Credentials restored from encrypted backup")
            else:
                logger.warning(
                    "Backup contains encrypted credentials but no password "
                    "provided — skipping credential restore"
                )

    # Commit restored state to local git repo
    init_config_repo(config_dir)
    _stage_all_trackable(config_dir)
    restore_msg = (
        f"Restored from backup "
        f"(original commit: {manifest.get('commit_sha', 'unknown')[:12]})"
    )
    _auto_commit(config_dir, restore_msg)

    logger.info("Restore complete: %s", config_dir)


def _create_safety_backup(config_dir: str) -> str | None:
    """If config_dir has files, create a timestamped safety backup alongside it.

    Returns the path to the safety backup directory, or None if config_dir
    was empty or did not exist.
    """
    if not os.path.isdir(config_dir):
        return None

    # Check if directory has any content (excluding .git)
    has_content = False
    for entry in os.scandir(config_dir):
        if entry.name != ".git":
            has_content = True
            break

    if not has_content:
        return None

    timestamp = time.strftime("%Y%m%dT%H%M%S")
    parent = os.path.dirname(config_dir)
    basename = os.path.basename(config_dir)
    safety_dir = os.path.join(parent, f"{basename}.pre-restore-{timestamp}")

    logger.info(
        "Creating safety backup of existing config: %s -> %s",
        config_dir, safety_dir,
    )

    # Copy existing files (excluding .git) to safety directory
    os.makedirs(safety_dir, exist_ok=True)
    syncignore = _load_syncignore(config_dir)
    for dirpath, dirnames, filenames in os.walk(config_dir):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, config_dir)
            if not _should_exclude(rel, syncignore):
                target = os.path.join(safety_dir, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(full, "rb") as src, open(target, "wb") as dst:
                    dst.write(src.read())

    return safety_dir

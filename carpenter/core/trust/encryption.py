"""Fernet encryption for untrusted arc output.

Uses symmetric AES encryption (via cryptography.fernet.Fernet) to protect
untrusted output at rest. Keys are stored per reviewer in the review_keys table.
"""

import json
import logging

from ...db import get_db, db_connection, db_transaction
from .audit import log_trust_event

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False


def _require_cryptography():
    if not HAS_CRYPTOGRAPHY:
        raise RuntimeError(
            "cryptography package is required for trust encryption. "
            "Install with: pip install cryptography>=41.0"
        )


def generate_arc_key(
    target_arc_id: int,
    reviewer_arc_ids: list[int],
) -> bytes:
    """Generate a Fernet key and store it for each designated reviewer.

    Args:
        target_arc_id: The tainted arc whose output will be encrypted.
        reviewer_arc_ids: List of reviewer arc IDs authorized to decrypt.

    Returns:
        The raw Fernet key bytes (for immediate platform use).
    """
    _require_cryptography()

    key = Fernet.generate_key()

    with db_transaction() as db:
        for reviewer_id in reviewer_arc_ids:
            db.execute(
                "INSERT INTO review_keys "
                "(target_arc_id, reviewer_arc_id, fernet_key_encrypted) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(target_arc_id, reviewer_arc_id) "
                "DO UPDATE SET fernet_key_encrypted = excluded.fernet_key_encrypted",
                (target_arc_id, reviewer_id, key),
            )

    log_trust_event(target_arc_id, "encryption_key_created", {
        "reviewer_count": len(reviewer_arc_ids),
    })

    return key


def encrypt_output(key: bytes, plaintext: str) -> bytes:
    """Encrypt plaintext using a Fernet key.

    Args:
        key: Fernet key bytes.
        plaintext: String to encrypt.

    Returns:
        Ciphertext bytes.
    """
    _require_cryptography()
    f = Fernet(key)
    return f.encrypt(plaintext.encode("utf-8"))


def decrypt_for_reviewer(
    reviewer_arc_id: int,
    target_arc_id: int,
    ciphertext: bytes,
) -> str:
    """Decrypt ciphertext for an authorized reviewer.

    Args:
        reviewer_arc_id: The reviewer requesting decryption.
        target_arc_id: The tainted arc whose output is encrypted.
        ciphertext: The encrypted bytes.

    Returns:
        Decrypted plaintext string.

    Raises:
        PermissionError: If reviewer is not authorized for this target.
    """
    _require_cryptography()

    with db_connection() as db:
        row = db.execute(
            "SELECT fernet_key_encrypted FROM review_keys "
            "WHERE target_arc_id = ? AND reviewer_arc_id = ?",
            (target_arc_id, reviewer_arc_id),
        ).fetchone()

    if row is None:
        log_trust_event(target_arc_id, "decryption_denied", {
            "reviewer_arc_id": reviewer_arc_id,
        })
        raise PermissionError(
            f"Reviewer {reviewer_arc_id} is not authorized to decrypt "
            f"output from arc {target_arc_id}"
        )

    key = row["fernet_key_encrypted"]
    if isinstance(key, memoryview):
        key = bytes(key)

    log_trust_event(target_arc_id, "decryption_granted", {
        "reviewer_arc_id": reviewer_arc_id,
    })

    f = Fernet(key)
    return f.decrypt(ciphertext).decode("utf-8")


def decrypt_after_promotion(
    target_arc_id: int,
    ciphertext: bytes,
) -> str:
    """Decrypt ciphertext after trust promotion (any key works).

    Args:
        target_arc_id: The (now promoted) arc whose output is encrypted.
        ciphertext: The encrypted bytes.

    Returns:
        Decrypted plaintext string.

    Raises:
        PermissionError: If no keys found for this arc.
    """
    _require_cryptography()

    with db_connection() as db:
        row = db.execute(
            "SELECT fernet_key_encrypted FROM review_keys "
            "WHERE target_arc_id = ? LIMIT 1",
            (target_arc_id,),
        ).fetchone()

    if row is None:
        raise PermissionError(f"No encryption keys found for arc {target_arc_id}")

    key = row["fernet_key_encrypted"]
    if isinstance(key, memoryview):
        key = bytes(key)

    f = Fernet(key)
    return f.decrypt(ciphertext).decode("utf-8")

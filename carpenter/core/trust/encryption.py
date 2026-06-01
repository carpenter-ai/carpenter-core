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


# Historical note: an earlier ``decrypt_after_promotion(target_arc_id,
# ciphertext)`` helper lived here. It was intended to read a non-trusted
# arc's state in plaintext after a JUDGE promoted the arc to ``trusted``,
# by picking any row in ``review_keys`` (all reviewers share the same key
# per arc). It was never wired into the promotion flow in
# ``carpenter/core/workflows/review_manager.py::_check_and_promote()`` —
# promotion only flips ``arcs.integrity_level``; the on-disk
# ``arc_state.value_json`` rows stay ciphertext, and
# ``tool_backends/state.py::handle_get()`` keeps returning the
# ``__encrypted__:(encrypted)`` sentinel. No production code path reads
# promoted-arc state in plaintext today.
#
# Removed because the helper had zero non-test callers and a public
# "post-promotion decrypt" API that bypasses the reviewer ACL (any
# review_keys row will do) is a footgun: a future caller could trivially
# side-channel non-trusted data on the strength of a now-trusted label.
# If a rewrite-to-plaintext step at promotion time is ever desired, it
# should be designed as part of ``_check_and_promote()`` and gated on
# the JUDGE verdict, not as a standalone helper.

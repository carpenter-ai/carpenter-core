"""Per-arc state tool backend."""
import json
import logging
from ..db import get_db, db_connection, db_transaction
from .. import config

logger = logging.getLogger(__name__)

# Marker prefix for encrypted values
_ENCRYPTED_MARKER = "__encrypted__:"


def _is_arc_non_trusted(db, arc_id) -> bool:
    """Check if an arc has integrity_level != 'trusted' (i.e. constrained or untrusted)."""
    if arc_id is None:
        return False
    row = db.execute(
        "SELECT integrity_level FROM arcs WHERE id = ?", (arc_id,)
    ).fetchone()
    return row is not None and row["integrity_level"] != "trusted"


def _get_arc_fernet_key(db, arc_id) -> bytes | None:
    """Get the Fernet key for a non-trusted arc (if one exists)."""
    row = db.execute(
        "SELECT fernet_key_encrypted FROM review_keys "
        "WHERE target_arc_id = ? LIMIT 1",
        (arc_id,),
    ).fetchone()
    if row is None:
        return None
    key = row["fernet_key_encrypted"]
    return bytes(key) if isinstance(key, memoryview) else key


def handle_get(params: dict) -> dict:
    key = params["key"]
    arc_id = params.get("arc_id")
    default = params.get("default")
    with db_connection() as db:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = ?",
            (arc_id, key),
        ).fetchone()
        if row is None:
            return {"value": default}

        value_str = row["value_json"]

        # Check for encrypted marker
        if value_str.startswith(f'"{_ENCRYPTED_MARKER}'):
            # Value is encrypted — return marker to unauthorized callers.
            # Authorized decryption goes through trust_encryption.decrypt_for_reviewer().
            return {"value": _ENCRYPTED_MARKER + "(encrypted)", "encrypted": True}

        value = json.loads(value_str)
        return {"value": value}


def handle_set(params: dict) -> dict:
    key = params["key"]
    value = params["value"]
    arc_id = params.get("arc_id")
    with db_transaction() as db:
        value_json = json.dumps(value)

        # If arc is tainted and has a Fernet key, encrypt the value
        if _is_arc_non_trusted(db, arc_id):
            fernet_key = _get_arc_fernet_key(db, arc_id)
            enforce_encryption = config.CONFIG.get("encryption", {}).get("enforce", True)

            if fernet_key:
                try:
                    from ..core.trust.encryption import encrypt_output
                    ciphertext = encrypt_output(fernet_key, value_json)
                    value_json = json.dumps(
                        _ENCRYPTED_MARKER + ciphertext.decode("ascii")
                    )
                except ImportError as e:
                    if enforce_encryption:
                        raise RuntimeError(
                            f"Cannot store state for non-trusted arc {arc_id}: "
                            "cryptography library not available. "
                            "Install with: pip install cryptography>=41.0"
                        ) from e
                    else:
                        logger.warning(
                            "cryptography library not available for arc %s - storing plaintext. "
                            "Install with: pip install cryptography>=41.0 (Error: %s)",
                            arc_id, e
                        )
                except (TypeError, ValueError, RuntimeError) as e:
                    if enforce_encryption:
                        raise RuntimeError(
                            f"Cannot store state for non-trusted arc {arc_id}: "
                            f"encryption failed: {e}"
                        ) from e
                    else:
                        logger.error(
                            "Encryption failed for arc %s - storing plaintext. Error: %s",
                            arc_id, e
                        )
            else:
                # No encryption key found for non-trusted arc
                if enforce_encryption:
                    raise RuntimeError(
                        f"Cannot store state for non-trusted arc {arc_id}: "
                        "no encryption key found. "
                        "Encryption keys should be generated during arc creation."
                    )
                else:
                    logger.warning(
                        "Tainted arc %s has no encryption key - storing plaintext. "
                        "Encryption keys should be generated during arc creation.",
                        arc_id
                    )

        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?) "
            "ON CONFLICT(arc_id, key) DO UPDATE SET value_json = excluded.value_json, "
            "updated_at = CURRENT_TIMESTAMP",
            (arc_id, key, value_json),
        )
        return {"success": True}


def handle_delete(params: dict) -> dict:
    key = params["key"]
    arc_id = params.get("arc_id")
    with db_transaction() as db:
        db.execute(
            "DELETE FROM arc_state WHERE arc_id = ? AND key = ?",
            (arc_id, key),
        )
        return {"success": True}


def handle_list(params: dict) -> dict:
    arc_id = params.get("arc_id")
    with db_connection() as db:
        rows = db.execute(
            "SELECT key FROM arc_state WHERE arc_id = ?",
            (arc_id,),
        ).fetchall()
        return {"keys": [r["key"] for r in rows]}

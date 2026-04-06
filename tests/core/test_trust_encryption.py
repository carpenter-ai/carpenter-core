"""Tests for carpenter.core.trust_encryption."""

import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.trust.audit import get_trust_events

try:
    from cryptography.fernet import Fernet
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False

pytestmark = pytest.mark.skipif(
    not HAS_CRYPTOGRAPHY, reason="cryptography package not installed"
)

from carpenter.core.trust.encryption import (
    generate_arc_key,
    encrypt_output,
    decrypt_for_reviewer,
    decrypt_after_promotion,
)
from carpenter.db import get_db


# ── generate_arc_key ─────────────────────────────────────────────────

def test_generate_arc_key_returns_bytes():
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    reviewer = arc_manager.add_child(parent, "reviewer", integrity_level="trusted")
    key = generate_arc_key(target, [reviewer])
    assert isinstance(key, bytes)
    assert len(key) > 0


def test_generate_arc_key_stores_for_all_reviewers():
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    r1 = arc_manager.add_child(parent, "r1", integrity_level="trusted")
    r2 = arc_manager.add_child(parent, "r2", integrity_level="trusted")
    generate_arc_key(target, [r1, r2])

    db = get_db()
    try:
        rows = db.execute(
            "SELECT reviewer_arc_id FROM review_keys WHERE target_arc_id = ?",
            (target,),
        ).fetchall()
    finally:
        db.close()
    reviewer_ids = {row["reviewer_arc_id"] for row in rows}
    assert reviewer_ids == {r1, r2}


def test_generate_arc_key_audit_event():
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    reviewer = arc_manager.add_child(parent, "reviewer", integrity_level="trusted")
    generate_arc_key(target, [reviewer])

    events = get_trust_events(arc_id=target, event_type="encryption_key_created")
    assert len(events) >= 1


# ── encrypt/decrypt round-trip ───────────────────────────────────────

def test_encrypt_decrypt_round_trip():
    key = Fernet.generate_key()
    plaintext = "sensitive data: {'key': 'value'}"
    ciphertext = encrypt_output(key, plaintext)

    assert ciphertext != plaintext.encode()
    assert isinstance(ciphertext, bytes)

    # Decrypt via Fernet directly
    f = Fernet(key)
    assert f.decrypt(ciphertext).decode() == plaintext


def test_encrypt_output_different_each_time():
    key = Fernet.generate_key()
    plaintext = "same data"
    ct1 = encrypt_output(key, plaintext)
    ct2 = encrypt_output(key, plaintext)
    assert ct1 != ct2  # Fernet includes timestamp/nonce


# ── decrypt_for_reviewer ─────────────────────────────────────────────

def test_decrypt_for_authorized_reviewer():
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    reviewer = arc_manager.add_child(parent, "reviewer", integrity_level="trusted")
    key = generate_arc_key(target, [reviewer])

    plaintext = "secret output"
    ciphertext = encrypt_output(key, plaintext)

    result = decrypt_for_reviewer(reviewer, target, ciphertext)
    assert result == plaintext


def test_decrypt_for_unauthorized_reviewer():
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    authorized = arc_manager.add_child(parent, "auth", integrity_level="trusted")
    unauthorized = arc_manager.add_child(parent, "unauth", integrity_level="trusted")
    key = generate_arc_key(target, [authorized])

    ciphertext = encrypt_output(key, "secret")

    with pytest.raises(PermissionError, match="not authorized"):
        decrypt_for_reviewer(unauthorized, target, ciphertext)


def test_decrypt_for_reviewer_audit_events():
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    reviewer = arc_manager.add_child(parent, "reviewer", integrity_level="trusted")
    key = generate_arc_key(target, [reviewer])
    ciphertext = encrypt_output(key, "data")

    decrypt_for_reviewer(reviewer, target, ciphertext)
    events = get_trust_events(arc_id=target, event_type="decryption_granted")
    assert len(events) >= 1


def test_decrypt_denied_audit_event():
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    reviewer = arc_manager.add_child(parent, "reviewer", integrity_level="trusted")
    unauthorized = arc_manager.add_child(parent, "unauth", integrity_level="trusted")
    generate_arc_key(target, [reviewer])
    key = Fernet.generate_key()
    ciphertext = encrypt_output(key, "data")

    try:
        decrypt_for_reviewer(unauthorized, target, ciphertext)
    except PermissionError:
        pass

    events = get_trust_events(arc_id=target, event_type="decryption_denied")
    assert len(events) >= 1


# ── decrypt_after_promotion ──────────────────────────────────────────

def test_decrypt_after_promotion():
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    reviewer = arc_manager.add_child(parent, "reviewer", integrity_level="trusted")
    key = generate_arc_key(target, [reviewer])

    plaintext = "promoted data"
    ciphertext = encrypt_output(key, plaintext)

    result = decrypt_after_promotion(target, ciphertext)
    assert result == plaintext


def test_decrypt_after_promotion_no_keys():
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    key = Fernet.generate_key()
    ciphertext = encrypt_output(key, "data")

    with pytest.raises(PermissionError, match="No encryption keys"):
        decrypt_after_promotion(target, ciphertext)


def test_decrypt_after_promotion_multiple_reviewers():
    """Any reviewer's key can be used for post-promotion decryption."""
    parent = arc_manager.create_arc("parent")
    target = arc_manager.add_child(parent, "target", integrity_level="untrusted")
    r1 = arc_manager.add_child(parent, "r1", integrity_level="trusted")
    r2 = arc_manager.add_child(parent, "r2", integrity_level="trusted")
    key = generate_arc_key(target, [r1, r2])

    plaintext = "shared secret"
    ciphertext = encrypt_output(key, plaintext)

    # Both reviewers have the same key, so promotion should work
    result = decrypt_after_promotion(target, ciphertext)
    assert result == plaintext

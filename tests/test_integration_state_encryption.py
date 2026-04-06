"""State encryption integration tests.

Verifies that the state backend transparently encrypts values for untrusted arcs
with Fernet keys, and stores plaintext for trusted arcs.
"""

import json
import pytest

from carpenter.core.arcs import manager as arc_manager
from carpenter.core.workflows import review_manager
from carpenter.core.trust.encryption import (
    generate_arc_key,
    decrypt_for_reviewer,
)
from carpenter.tool_backends import state as state_backend
from carpenter.db import get_db


def test_untrusted_arc_state_set_encrypts():
    """Untrusted arc + Fernet key → handle_set() encrypts the value."""
    # Create untrusted arc
    parent = arc_manager.create_arc("project")
    target = arc_manager.add_child(parent, "untrusted-worker", integrity_level="untrusted")

    # Create reviewer and Fernet key
    reviewer = review_manager.create_review_arc(target, "reviewer")
    key = generate_arc_key(target, [reviewer])

    # Set a value via state backend
    result = state_backend.handle_set({
        "arc_id": target,
        "key": "secret_data",
        "value": "my-sensitive-value",
    })
    assert result["success"] is True

    # handle_get should return encrypted marker
    get_result = state_backend.handle_get({
        "arc_id": target,
        "key": "secret_data",
    })
    assert get_result.get("encrypted") is True
    assert "__encrypted__:" in str(get_result["value"])

    # Raw DB value should be ciphertext (not plaintext)
    db = get_db()
    try:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = ?",
            (target, "secret_data"),
        ).fetchone()
    finally:
        db.close()

    raw_value = row["value_json"]
    assert "my-sensitive-value" not in raw_value
    assert "__encrypted__:" in raw_value

    # Decrypt via reviewer: extract ciphertext from stored value
    stored = json.loads(raw_value)
    ciphertext_b64 = stored.replace("__encrypted__:", "")
    plaintext = decrypt_for_reviewer(reviewer, target, ciphertext_b64.encode("ascii"))
    # The encrypted content is the JSON serialization of the value
    assert json.loads(plaintext) == "my-sensitive-value"


def test_trusted_arc_state_set_no_encryption():
    """Trusted arc stores plaintext — no encryption applied."""
    arc_id = arc_manager.create_arc("trusted-worker", integrity_level="trusted")

    result = state_backend.handle_set({
        "arc_id": arc_id,
        "key": "public_data",
        "value": "not-secret",
    })
    assert result["success"] is True

    # handle_get returns plaintext directly
    get_result = state_backend.handle_get({
        "arc_id": arc_id,
        "key": "public_data",
    })
    assert get_result["value"] == "not-secret"
    assert get_result.get("encrypted") is None

    # Raw DB value is plaintext JSON
    db = get_db()
    try:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = ?",
            (arc_id, "public_data"),
        ).fetchone()
    finally:
        db.close()

    assert json.loads(row["value_json"]) == "not-secret"

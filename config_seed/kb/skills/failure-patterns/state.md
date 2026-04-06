# State and Data Failures

Failures related to the database, encryption, arc state machine, and platform state management.

---

## Encryption Unavailable (Fail-Closed)

**Symptoms**: Creating a tainted arc raises `RuntimeError: cryptography package is required for trust encryption`. Alternatively, `state.set()` on a tainted arc raises `RuntimeError` about encryption being unavailable or failing.

**Root cause**: The platform enforces encryption by default for tainted arc output (`encryption.enforce = true`). This requires the `cryptography>=41.0` Python package. If the package is not installed or cannot function (e.g., missing system libraries), any operation that needs encryption fails closed -- it refuses to proceed rather than storing data in plaintext.

**Escape**:
1. Check if `cryptography` is installed: look at the startup health check log for "Encryption: cryptography library available" vs the warning message.
2. If the package is missing, inform the user to install it: `pip install cryptography>=41.0`.
3. If the package is installed but not functional (rare -- may happen on unusual architectures), check the error details.
4. As a temporary workaround (NOT recommended for production), the user can set `encryption.enforce: false` in config.yaml. This allows plaintext storage of tainted output.
5. If you need to work with data that would normally be tainted, consider whether the operation can be restructured to avoid tainted arcs entirely (e.g., processing data through review arcs first).

**Prevention**: Ensure `cryptography>=41.0` is installed before creating tainted arcs. The install script checks for this and warns if missing.

**Escalation**: If the cryptography library cannot be installed (platform/architecture constraints), the user must decide whether to operate with `encryption.enforce: false`. This is a security policy decision.

---

## Database Locked

**Symptoms**: Operations fail with `sqlite3.OperationalError: database is locked`. State reads or writes, arc operations, or any database access raises this error.

**Root cause**: SQLite uses file-level locking. While WAL mode allows concurrent reads, only one writer can proceed at a time. A long-running write transaction blocks other writers. Common causes:
- Multiple platform instances pointing at the same database file.
- A long-running execution that holds a write transaction open.
- Database backup tools that lock the file.
- The SD card (on Raspberry Pi) being slow, extending lock hold times.

**Escape**:
1. Retry the operation. Most database locks are transient and resolve in milliseconds.
2. Check for other processes accessing the database: `fuser {database_path}` or `lsof {database_path}`.
3. Ensure only one platform instance is running per database file.
4. If the error occurs during execution callbacks, the code manager may be holding a transaction. Reduce callback frequency.
5. The platform uses `get_db()` / `db.close()` pattern for short-lived connections. If custom code holds a connection open for a long time, it may cause lock contention.

**Prevention**: Keep database transactions short. Close connections promptly via `db.close()`. Do not hold write transactions across network calls or long computations. The platform's batched transaction pattern (queuing audit events, single commit) was designed to minimize lock hold time.

**Escalation**: Chronic database locking on SD card deployments may require moving the database to a faster storage medium. Inform the user.

---

## State Key Not Found

**Symptoms**: `get_state()` returns `null` or `{"value": null}` for a key that was expected to exist. Code that depends on a state value fails because the value is missing.

**Root cause**: The state key was never set, was set in a different arc scope, or was set but the arc is different from the one being queried. State in Carpenter is scoped by arc ID (`arc_id`). Conversation-level state uses `arc_id = 0`. A key set in arc 5 is not visible to arc 7.

**Escape**:
1. Verify the key name is spelled correctly (state keys are case-sensitive strings).
2. Check the arc scope: was the state set in the same arc? Use `get_state` with the correct `arc_id`.
3. For conversation-level state (arc_id=0), ensure the state was set in a previous execution within the same conversation context.
4. Use `list_state` or query the `state` table directly to see what keys exist for the arc.
5. If the key should have been set by a previous arc step, check that the previous step completed successfully (it may have failed silently).

**Prevention**: Always handle the case where a state key does not exist. Use default values. Do not assume previous steps succeeded without checking.

**Escalation**: If state is missing due to a platform bug (e.g., state was set but is not readable), this needs investigation of the state backend and database.

---

## Arc in Frozen Status (Immutable)

**Symptoms**: Operations on an arc fail with errors about the arc being in a frozen or immutable status. Attempting to add children, change status, or modify a frozen arc raises `ValueError`.

**Root cause**: Arcs in statuses `completed`, `failed`, or `cancelled` are immutable. No transitions, no child additions, no modifications are allowed. This is a fundamental invariant of the arc state machine. Similarly, template-created arcs (`from_template=True`) are completely immutable regardless of status.

**Escape**:
1. Check the arc's current status: `SELECT status, from_template FROM arcs WHERE id = ?`.
2. If the arc is completed/failed/cancelled: you cannot modify it. Create a new arc instead for follow-up work.
3. If you need to retry failed work: create a new sibling arc under the same parent (if the parent is not frozen).
4. If the parent is also frozen: you need to create a new top-level arc or find an ancestor that is still active.
5. If the arc is from a template (`from_template=True`): template arcs are completely immutable by design. You cannot add children, change status, or modify them in any way. Work within the template's prescribed structure.

**Prevention**: Check arc status before attempting modifications. Plan arc structure upfront to avoid needing to modify frozen arcs later.

**Escalation**: If the workflow requires modifying completed arcs (e.g., reopening work), this is a design limitation. Inform the user and suggest creating new arcs instead.

---

## Arc Status Transition Violation

**Symptoms**: Attempting to change an arc's status raises `ValueError` about an invalid transition. For example, trying to move from `pending` directly to `completed`.

**Root cause**: The arc state machine enforces a strict transition graph:
- `pending` can go to: `active`, `cancelled`
- `active` can go to: `waiting`, `completed`, `failed`, `cancelled`
- `waiting` can go to: `active`, `cancelled`
- `completed`, `failed`, `cancelled`: no transitions (frozen)

**Escape**:
1. Check the current status and the desired target status against the transition rules above.
2. If you need to reach a status that requires an intermediate step, perform the transitions in sequence (e.g., `pending` -> `active` -> `completed`).
3. A common mistake is trying to complete a `pending` arc. You must first activate it (`pending` -> `active`), then complete it (`active` -> `completed`).
4. Another common case: trying to reactivate a `completed` arc. This is not allowed. Create a new arc instead.

**Prevention**: Follow the state machine transitions strictly. If writing code that manages arcs, include the intermediate transitions.

**Escalation**: None. The transition rules are fixed. Work within them.

---

## Taint Boundary Violation

**Symptoms**: Clean arcs cannot access `arc.read_output_UNTRUSTED` or `arc.read_state_UNTRUSTED` tools. Tainted conversations cannot modify skill KB entries without human review. Trust promotion is rejected because the promoting arc is not a JUDGE.

**Root cause**: The trust boundary system enforces strict separation between clean and tainted contexts:
- Clean arcs cannot access untrusted data tools.
- Planner agents are restricted to structural/messaging tools only.
- Only JUDGE arcs can promote trust (and only for their specific target arc -- no cascading).
- Tainted arcs must have at least one reviewer and a judge at creation time (`arc.create_batch`).
- Tainted conversations (those that have received untrusted tool output) require human review for skill KB modifications.

**Escape**:
1. If a clean arc needs untrusted data: it should not access it directly. Create a review arc to process the untrusted data, then access the reviewed output.
2. If a tainted conversation needs to modify skill knowledge: writing to a `skills/` KB path triggers the `skill-kb-review` workflow, which will escalate to human review. The platform notifies the user automatically.
3. If trust promotion is failing: verify the promoting arc has `agent_type = 'JUDGE'` and is targeting the correct arc. Trust promotion does not cascade to parent arcs.
4. For creating tainted arcs: use `arc.create_batch` which enforces the reviewer+judge requirement. Do not try to create tainted arcs individually without review infrastructure.
5. If the conversation was tainted by a taint-check failure (fail-closed behavior), the conversation is treated as tainted even if no untrusted data was actually processed. This is by design.

**Prevention**: Plan trust boundaries before creating arc structures. Keep clean and tainted workflows separate. Use review arcs as the bridge between untrusted and trusted zones.

**Escalation**: Trust boundary violations are security features, not bugs. If the workflow genuinely requires crossing trust boundaries, it must go through the review arc mechanism.

---

## Encryption Failure at Runtime

**Symptoms**: `state.set()` or arc output storage raises `RuntimeError` about encryption failing, even though `cryptography` is installed. Distinct from "Encryption Unavailable" above.

**Root cause**: The Fernet encryption operation itself failed. Possible causes:
- Corrupted encryption key in the `review_keys` table.
- Key mismatch (trying to decrypt with the wrong key).
- Data corruption in the ciphertext.
- Memory or system-level issues affecting the cryptographic operations.

**Escape**:
1. Check the specific error message. If it mentions key issues, the encryption key may be corrupted.
2. For decryption failures (`InvalidToken`): the ciphertext was encrypted with a different key than the one being used for decryption. Verify the reviewer-target arc relationship in `review_keys`.
3. If the key is corrupted: generate a new key for the arc pair. Note that existing encrypted data with the old key cannot be recovered.
4. When `encryption.enforce = true` (default), any encryption failure prevents the operation entirely (fail-closed). The data is not stored in plaintext.

**Prevention**: Do not manipulate the `review_keys` table directly. Use the trust encryption API (`trust_encryption.py`). Ensure database integrity.

**Escalation**: If encryption keys are corrupted, this may indicate a database integrity issue. Inform the user.

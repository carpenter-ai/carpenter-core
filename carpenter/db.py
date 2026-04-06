"""Database connection and initialization for Carpenter.

Uses SQLite in WAL mode with Row factory. Schema is idempotent
(all CREATE TABLE IF NOT EXISTS). Migrations handle column additions.
Optionally uses SQLCipher for at-rest encryption when configured.
"""

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .db_migrations import run_migrations as _run_migrations

logger = logging.getLogger(__name__)

def _read_schema() -> str:
    """Read schema.sql using importlib.resources (works on all platforms including Android/Chaquopy)."""
    import importlib.resources
    try:
        ref = importlib.resources.files("carpenter").joinpath("schema.sql")
        return ref.read_text(encoding="utf-8")
    except AttributeError:
        return importlib.resources.read_text("carpenter", "schema.sql")

# Try to import pysqlcipher3 for encrypted database support
_sqlcipher_module = None
try:
    from pysqlcipher3 import dbapi2 as _sqlcipher_module  # type: ignore[import-untyped]
except ImportError:
    pass


def get_db() -> sqlite3.Connection:
    """Get a database connection with WAL mode and Row factory.

    If ``db_encryption_key`` is set in config and pysqlcipher3 is
    available, uses SQLCipher for at-rest encryption. Falls back to
    plain sqlite3 with a warning if pysqlcipher3 is not installed.
    """
    db_path = config.CONFIG["database_path"]
    encryption_key = config.CONFIG.get("db_encryption_key")

    if encryption_key and _sqlcipher_module is not None:
        conn = _sqlcipher_module.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA key='{encryption_key}'")
    elif encryption_key and _sqlcipher_module is None:
        logger.warning(
            "db_encryption_key is set but pysqlcipher3 is not installed. "
            "Database will NOT be encrypted. Install with: pip install pysqlcipher3"
        )
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
    else:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_connection():
    """Context manager for database connections.

    Usage::

        with db_connection() as db:
            row = db.execute("SELECT ...").fetchone()
    """
    db = get_db()
    try:
        yield db
    finally:
        db.close()


_transaction_guard = threading.local()


@contextmanager
def db_transaction():
    """Context manager that auto-commits on success, rolls back on exception.

    Raises RuntimeError immediately if called while another db_transaction()
    is already active on the same thread. Nested write transactions on
    separate SQLite connections cause "database is locked" errors; this
    guard converts a 30-second timeout into an instant failure with a
    clear stack trace.

    Usage::

        with db_transaction() as db:
            db.execute("INSERT INTO ...")
    """
    if getattr(_transaction_guard, "active", False):
        raise RuntimeError(
            "Nested db_transaction() detected on the same thread. "
            "Move the inner write operation outside the outer transaction, "
            "or pass the existing db connection explicitly."
        )
    _transaction_guard.active = True
    db = get_db()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
        _transaction_guard.active = False


# Backward-compatible alias: some tests import _migrate from carpenter.db
def _migrate(conn: sqlite3.Connection):
    """Run data migrations for existing databases.

    This is a backward-compatible wrapper around db_migrations.run_migrations().
    """
    _run_migrations(conn)


def _recover_on_startup(db) -> None:
    """Reset work items and arcs that were orphaned by a previous crash or restart."""
    now = datetime.now(timezone.utc).isoformat()

    # Query active arc IDs BEFORE resetting so we can log arc_history
    active_arc_rows = db.execute(
        "SELECT id FROM arcs WHERE status='active'"
    ).fetchall()
    active_arc_ids = [row["id"] for row in active_arc_rows]

    # Reset claimed work items
    claimed = db.execute(
        "UPDATE work_queue SET status='pending', claimed_at=NULL "
        "WHERE status='claimed'"
    ).rowcount

    # Reset active arcs to pending
    active = db.execute(
        "UPDATE arcs SET status='pending' WHERE status='active'"
    ).rowcount

    # Log arc_history for each recovered arc
    for arc_id in active_arc_ids:
        db.execute(
            "INSERT INTO arc_history (arc_id, entry_type, content_json, actor, created_at) "
            "VALUES (?, 'status_change', ?, 'startup_recovery', ?)",
            (arc_id, json.dumps({"from": "active", "to": "pending",
                                 "reason": "orphaned by crash/restart"}), now),
        )

    # Reset waiting arcs to pending so retry can resume.
    # Retry state (_retry_count, _backoff_until) is preserved in arc_state.
    waiting_rows = db.execute(
        "SELECT id FROM arcs WHERE status='waiting'"
    ).fetchall()
    waiting_ids = [row["id"] for row in waiting_rows]

    waiting_reset = 0
    if waiting_ids:
        placeholders = ",".join("?" * len(waiting_ids))
        waiting_reset = db.execute(
            f"UPDATE arcs SET status='pending' WHERE id IN ({placeholders})",
            waiting_ids,
        ).rowcount
        for wid in waiting_ids:
            db.execute(
                "INSERT INTO arc_history (arc_id, entry_type, content_json, actor, created_at) "
                "VALUES (?, 'status_change', ?, 'startup_recovery', ?)",
                (wid, json.dumps({"from": "waiting", "to": "pending",
                                  "reason": "retry backoff interrupted by restart"}), now),
            )

    # Mark orphaned running code_executions as crashed
    crashed = db.execute(
        "UPDATE code_executions SET execution_status='crashed', completed_at=? "
        "WHERE execution_status='running'",
        (now,),
    ).rowcount

    # Delete expired execution_sessions
    expired = db.execute(
        "DELETE FROM execution_sessions WHERE expires_at < ?",
        (now,),
    ).rowcount

    # Clear stale pending_escalation (global state, arc_id=0).
    # If the server crashed while waiting for escalation approval,
    # the stale state blocks ALL chat invocations with an ambiguous-response
    # early return that silently drops messages.
    escalation_cleared = db.execute(
        "DELETE FROM arc_state WHERE arc_id = 0 AND key = 'pending_escalation'"
    ).rowcount

    if claimed or active or waiting_reset or crashed or expired or escalation_cleared:
        db.commit()
        parts = []
        if claimed:
            parts.append(f"reset {claimed} claimed work item(s)")
        if active:
            parts.append(f"reset {active} active arc(s) to pending")
        if waiting_reset:
            parts.append(f"reset {waiting_reset} waiting arc(s) to pending")
        if crashed:
            parts.append(f"marked {crashed} running execution(s) as crashed")
        if expired:
            parts.append(f"purged {expired} expired session(s)")
        if escalation_cleared:
            parts.append(f"cleared {escalation_cleared} stale escalation prompt(s)")
        logger.info("Startup recovery: %s", ", ".join(parts))


def _config_seed_dir() -> Path:
    """Return the config_seed/ directory at the repository root."""
    return Path(__file__).resolve().parent.parent / "config_seed"


def install_data_models_defaults(data_models_dir: str) -> dict:
    """Copy config_seed/data_models/ to data_models_dir if it doesn't exist.

    Same pattern as prompts.install_prompt_defaults(). Only copies on first install.

    Returns:
        {"status": "installed"|"exists"|"no_defaults", "copied": int}
    """
    if os.path.isdir(data_models_dir):
        return {"status": "exists", "copied": 0}

    seed_dir = str(_config_seed_dir() / "data_models")
    if not os.path.isdir(seed_dir):
        logger.warning("Data models seed directory not found: %s", seed_dir)
        return {"status": "no_defaults", "copied": 0}

    try:
        import shutil
        shutil.copytree(seed_dir, data_models_dir)
        count = sum(1 for _ in Path(data_models_dir).glob("*.py"))
        logger.info("Installed data model defaults: %d files to %s", count, data_models_dir)
        return {"status": "installed", "copied": count}
    except OSError as e:
        logger.error("Failed to install data model defaults: %s", e)
        return {"status": "error", "error": str(e), "copied": 0}


def _sync_credential_registry(base_dir: str) -> None:
    """Copy bundled credential_registry.yaml to config/ if not already present."""
    dest = Path(base_dir) / "config" / "credential_registry.yaml"
    if dest.exists():
        return
    try:
        src = _config_seed_dir() / "credential-registry.yaml"
        if src.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(str(src), str(dest))
            logger.info("Synced credential_registry.yaml to %s", dest)
        else:
            logger.warning("Seed credential_registry.yaml not found at %s", src)
    except OSError as _exc:
        logger.exception("Failed to sync credential_registry.yaml")


def _sync_model_registry(base_dir: str) -> None:
    """Copy bundled model_registry.yaml to config/ if not already present."""
    dest = Path(base_dir) / "config" / "model_registry.yaml"
    if dest.exists():
        return
    try:
        src = _config_seed_dir() / "model-registry.yaml"
        if src.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(str(src), str(dest))
            logger.info("Synced model_registry.yaml to %s", dest)
        else:
            logger.warning("Seed model_registry.yaml not found at %s", src)
    except OSError as _exc:
        logger.exception("Failed to sync model_registry.yaml")


def init_db(skip_migrations=False):
    """Initialize the database: create directories, apply schema, run migrations.

    Args:
        skip_migrations: If True, skip migration checks (useful for fresh test databases)
    """
    db_path = config.CONFIG["database_path"]
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    os.makedirs(config.CONFIG["log_dir"], exist_ok=True)
    os.makedirs(config.CONFIG["code_dir"], exist_ok=True)
    os.makedirs(config.CONFIG["workspaces_dir"], exist_ok=True)

    # Sync bundled registries to user's base_dir (first run)
    base_dir = config.CONFIG.get("base_dir", "")
    if base_dir:
        _sync_credential_registry(base_dir)
        _sync_model_registry(base_dir)

    conn = get_db()
    # Check if this is an existing DB that needs migration before schema runs.
    # The schema references new column names (e.g. integrity_level) and indexes
    # that won't exist until migration renames old columns (taint_level ->
    # integrity_level).  Running migration first on existing DBs avoids
    # "no such column" errors from CREATE INDEX in the schema script.
    if not skip_migrations:
        existing_tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if existing_tables:  # Only migrate if DB has tables (not fresh)
            _migrate(conn)
    schema = _read_schema()

    # Check if FTS5 is available (Chaquopy's Android SQLite may lack it)
    _fts5_available = True
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts5_probe")
    except sqlite3.OperationalError:
        _fts5_available = False
        logger.warning("SQLite FTS5 module not available -- FTS features disabled")

    if _fts5_available:
        conn.executescript(schema)
    else:
        # Execute schema statement-by-statement, skipping FTS5 statements
        for stmt in schema.split(";"):
            stmt = stmt.strip()
            if not stmt:
                continue
            if "fts5" in stmt.lower() or "_fts" in stmt.lower():
                logger.debug("Skipping FTS5 statement: %.60s...", stmt)
                continue
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                logger.debug("Schema statement skipped: %s", e)
        conn.commit()

    # Startup recovery: reset orphaned claimed work items and active arcs
    _recover_on_startup(conn)

    conn.close()

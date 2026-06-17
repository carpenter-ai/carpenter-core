"""Database migration logic for Carpenter.

Contains all schema migration functions, organized into logical phases.
These are called from db.init_db() after initial schema creation to handle
column additions and table modifications for schema evolution.
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)


def _migrate_basic_schema(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 1: Basic schema additions (messages, api_calls, conversations columns)."""
    # Add content_json column to messages
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "content_json" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN content_json TEXT")
        conn.commit()

    # Create api_calls table if missing (added after initial schema)
    if "api_calls" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS api_calls (
                id INTEGER PRIMARY KEY,
                conversation_id INTEGER REFERENCES conversations(id),
                model TEXT NOT NULL,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_creation_input_tokens INTEGER DEFAULT 0,
                cache_read_input_tokens INTEGER DEFAULT 0,
                stop_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_api_calls_conversation ON api_calls(conversation_id);
        """)
        conn.commit()

    # Add title, archived, and summary columns to conversations (multi-conversation support)
    conv_cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
    if "title" not in conv_cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN title TEXT")
        conn.commit()
    if "archived" not in conv_cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN archived BOOLEAN DEFAULT FALSE")
        conn.commit()
    if "summary" not in conv_cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN summary TEXT")
        conn.commit()


def _migrate_conversation_arcs(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 2: Multi-conversation support (conversation_arcs junction table)."""
    if "conversation_arcs" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversation_arcs (
                id INTEGER PRIMARY KEY,
                conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
                arc_id INTEGER REFERENCES arcs(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(conversation_id, arc_id)
            );
            CREATE INDEX IF NOT EXISTS idx_conversation_arcs_conv ON conversation_arcs(conversation_id);
        """)
        conn.commit()


def _migrate_execution_sessions(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 3: Security and execution session management."""
    # Create execution_sessions table if missing (security: platform-controlled session IDs)
    if "execution_sessions" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS execution_sessions (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL UNIQUE,
                code_file_id INTEGER REFERENCES code_files(id),
                execution_id INTEGER REFERENCES code_executions(id),
                reviewed BOOLEAN NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_execution_sessions_session_id
                ON execution_sessions(session_id, expires_at);
        """)
        conn.commit()

    # Add conversation_id column to execution_sessions
    es_cols = {row[1] for row in conn.execute("PRAGMA table_info(execution_sessions)").fetchall()}
    if "conversation_id" not in es_cols and "execution_sessions" in tables:
        conn.execute("ALTER TABLE execution_sessions ADD COLUMN conversation_id INTEGER REFERENCES conversations(id)")
        conn.commit()

    # Add execution_context column to execution_sessions (arc-step vs reviewed)
    if "execution_context" not in es_cols and "execution_sessions" in tables:
        conn.execute("ALTER TABLE execution_sessions ADD COLUMN execution_context TEXT DEFAULT 'reviewed'")
        conn.commit()


def _migrate_trust_boundary_system(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 4: Trust boundary system (integrity_level, audit log, review keys, performance counters)."""
    arc_cols = {row[1] for row in conn.execute("PRAGMA table_info(arcs)").fetchall()}

    # Migration: rename taint_level -> integrity_level
    if "taint_level" in arc_cols and "integrity_level" not in arc_cols:
        conn.execute("ALTER TABLE arcs RENAME COLUMN taint_level TO integrity_level")
        # Map old values to new: clean->trusted, tainted->untrusted, review->trusted
        conn.execute("UPDATE arcs SET integrity_level = 'trusted' WHERE integrity_level = 'clean'")
        conn.execute("UPDATE arcs SET integrity_level = 'untrusted' WHERE integrity_level = 'tainted'")
        conn.execute("UPDATE arcs SET integrity_level = 'trusted' WHERE integrity_level = 'review'")
        conn.commit()
    if "integrity_level" not in arc_cols and "taint_level" not in arc_cols:
        conn.execute("ALTER TABLE arcs ADD COLUMN integrity_level TEXT DEFAULT 'trusted'")
        conn.commit()

    # Add trust boundary arc columns
    if "output_type" not in arc_cols:
        conn.execute("ALTER TABLE arcs ADD COLUMN output_type TEXT DEFAULT 'python'")
        conn.commit()
    if "agent_type" not in arc_cols:
        conn.execute("ALTER TABLE arcs ADD COLUMN agent_type TEXT DEFAULT 'EXECUTOR'")
        conn.commit()
    if "template_mutable" not in arc_cols:
        conn.execute("ALTER TABLE arcs ADD COLUMN template_mutable BOOLEAN DEFAULT FALSE")
        conn.commit()

    # Add performance counter columns to arcs
    if "descendant_tokens" not in arc_cols:
        conn.execute("ALTER TABLE arcs ADD COLUMN descendant_tokens INTEGER DEFAULT 0")
        conn.commit()
    if "descendant_executions" not in arc_cols:
        conn.execute("ALTER TABLE arcs ADD COLUMN descendant_executions INTEGER DEFAULT 0")
        conn.commit()
    if "descendant_arc_count" not in arc_cols:
        conn.execute("ALTER TABLE arcs ADD COLUMN descendant_arc_count INTEGER DEFAULT 0")
        conn.commit()

    # Create trust audit log table if missing
    if "trust_audit_log" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trust_audit_log (
                id INTEGER PRIMARY KEY,
                arc_id INTEGER,
                event_type TEXT NOT NULL,
                details_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_trust_audit_arc ON trust_audit_log(arc_id);
            CREATE INDEX IF NOT EXISTS idx_trust_audit_event ON trust_audit_log(event_type);
        """)
        conn.commit()

    # Create review_keys table if missing
    if "review_keys" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS review_keys (
                id INTEGER PRIMARY KEY,
                target_arc_id INTEGER NOT NULL,
                reviewer_arc_id INTEGER NOT NULL,
                fernet_key_encrypted BLOB NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(target_arc_id, reviewer_arc_id)
            );
            CREATE INDEX IF NOT EXISTS idx_review_keys_target ON review_keys(target_arc_id);
        """)
        conn.commit()

    # Drop review_policies table (replaced by judge pattern)
    if "review_policies" in tables:
        conn.execute("DROP TABLE review_policies")
        conn.commit()

    # Create integrity_level index if missing (for existing DBs that got columns but not index)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_arcs_integrity_level ON arcs(integrity_level)")
        conn.commit()
    except sqlite3.Error as _exc:
        pass
    # Drop old index if present
    try:
        conn.execute("DROP INDEX IF EXISTS idx_arcs_taint_level")
        conn.commit()
    except sqlite3.Error as _exc:
        pass


def _migrate_fts_index(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 5: Full-text search index for conversations (memory recall)."""
    if "conversations_fts" not in tables:
        try:
            conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
                    title,
                    summary,
                    content='conversations',
                    content_rowid='id',
                    tokenize='porter unicode61'
                );

                CREATE TRIGGER IF NOT EXISTS conversations_fts_insert AFTER INSERT ON conversations
                BEGIN
                    INSERT INTO conversations_fts(rowid, title, summary)
                    VALUES (NEW.id, COALESCE(NEW.title, ''), COALESCE(NEW.summary, ''));
                END;

                CREATE TRIGGER IF NOT EXISTS conversations_fts_update AFTER UPDATE OF title, summary ON conversations
                BEGIN
                    INSERT INTO conversations_fts(conversations_fts, rowid, title, summary)
                    VALUES ('delete', OLD.id, COALESCE(OLD.title, ''), COALESCE(OLD.summary, ''));
                    INSERT INTO conversations_fts(rowid, title, summary)
                    VALUES (NEW.id, COALESCE(NEW.title, ''), COALESCE(NEW.summary, ''));
                END;

                CREATE TRIGGER IF NOT EXISTS conversations_fts_delete BEFORE DELETE ON conversations
                BEGIN
                    INSERT INTO conversations_fts(conversations_fts, rowid, title, summary)
                    VALUES ('delete', OLD.id, COALESCE(OLD.title, ''), COALESCE(OLD.summary, ''));
                END;
            """)

            # Backfill existing conversations into FTS index
            conn.execute(
                "INSERT INTO conversations_fts(rowid, title, summary) "
                "SELECT id, COALESCE(title, ''), COALESCE(summary, '') FROM conversations"
            )
            conn.commit()
            logger.info("Created FTS5 index for conversations and backfilled existing data")
        except sqlite3.Error as _exc:
            logger.exception("Failed to create FTS5 index (FTS5 extension may not be available)")


def _migrate_compaction_system(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 6: Context window compaction system (compaction_events table and message tracking)."""
    if "compaction_events" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS compaction_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                message_id_start INTEGER NOT NULL,
                message_id_end INTEGER NOT NULL,
                model TEXT,
                tokens_reclaimed INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            );
        """)
        conn.commit()

    # Add compaction_event_id column to messages
    msg_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "compaction_event_id" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN compaction_event_id INTEGER REFERENCES compaction_events(id)")
        conn.commit()


def _migrate_notifications(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 7: Notifications system."""
    if "notifications" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'normal',
                category TEXT,
                channel TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                batch_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status);
            CREATE INDEX IF NOT EXISTS idx_notifications_batch ON notifications(batch_id);
        """)
        conn.commit()


def _migrate_agent_configs(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 8: Agent configuration system.

    Historically this phase created the ``agent_configs`` table and added
    the ``arcs.agent_config_id`` column.  Both have been retired — see
    ``_drop_legacy_agent_configs`` (Phase 23) — and are no longer
    re-created on fresh DBs.  This function is a no-op kept only for
    sequencing; do not add new logic here.
    """
    return


def _migrate_scheduling_and_contracts(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 9: Scheduling, contracts, and verification support."""
    # Add one_shot column to cron_entries (one-shot scheduling support)
    cron_cols = {row[1] for row in conn.execute("PRAGMA table_info(cron_entries)").fetchall()}
    if "one_shot" not in cron_cols:
        conn.execute("ALTER TABLE cron_entries ADD COLUMN one_shot BOOLEAN NOT NULL DEFAULT FALSE")
        conn.commit()

    # Add arc scheduling and contract columns
    arc_cols = {row[1] for row in conn.execute("PRAGMA table_info(arcs)").fetchall()}
    if "wait_until" not in arc_cols:
        conn.execute("ALTER TABLE arcs ADD COLUMN wait_until TEXT")
        conn.commit()
    if "output_contract" not in arc_cols:
        conn.execute("ALTER TABLE arcs ADD COLUMN output_contract TEXT")
        conn.commit()
    if "arc_role" not in arc_cols:
        conn.execute("ALTER TABLE arcs ADD COLUMN arc_role TEXT DEFAULT 'worker'")
        conn.commit()
    if "verification_target_id" not in arc_cols:
        conn.execute("ALTER TABLE arcs ADD COLUMN verification_target_id INTEGER REFERENCES arcs(id)")
        conn.commit()


def _migrate_channel_connectors(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 10: Channel connector support (channel bindings and analytics)."""
    # Add channel_type column to conversations
    conv_cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
    if "channel_type" not in conv_cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN channel_type TEXT")
        conn.commit()

    # Create channel_bindings table if missing
    if "channel_bindings" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS channel_bindings (
                id INTEGER PRIMARY KEY,
                channel_type TEXT NOT NULL,
                channel_user_id TEXT NOT NULL,
                display_name TEXT,
                conversation_id INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(channel_type, channel_user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_channel_bindings_conv ON channel_bindings(conversation_id);
        """)
        conn.commit()

    # skill_loads resource_path migration removed (skills system deprecated)


def _migrate_code_execution_enhancements(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 11: Code execution safety and tracking enhancements."""
    if "code_executions" not in tables:
        return

    ce_cols = {row[1] for row in conn.execute("PRAGMA table_info(code_executions)").fetchall()}

    # Add command_hash column (PID-reuse-safe liveness checking)
    if "command_hash" not in ce_cols:
        conn.execute("ALTER TABLE code_executions ADD COLUMN command_hash TEXT")
        conn.commit()

    # Add taint_source column (taint leak fix)
    if "taint_source" not in ce_cols:
        conn.execute("ALTER TABLE code_executions ADD COLUMN taint_source TEXT")
        conn.commit()


def _migrate_arc_retry_system(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 12: Arc retry system with backoff and adaptive circuit breaker."""
    # Add scheduled_at column to work_queue (arc retry with backoff)
    if "work_queue" in tables:
        wq_cols = {row[1] for row in conn.execute("PRAGMA table_info(work_queue)").fetchall()}
        if "scheduled_at" not in wq_cols:
            conn.execute("ALTER TABLE work_queue ADD COLUMN scheduled_at TEXT")
            conn.commit()
            # Create index for scheduled queries
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_work_queue_scheduled "
                "ON work_queue(status, scheduled_at) WHERE status = 'pending'"
            )
            conn.commit()

    # Create model_calls table if missing (Phase 3: adaptive backoff / circuit breaker)
    if "model_calls" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS model_calls (
                id INTEGER PRIMARY KEY,
                model_id TEXT NOT NULL,
                success BOOLEAN NOT NULL,
                error_type TEXT,
                called_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_model_calls_model ON model_calls(model_id, called_at DESC);
        """)
        conn.commit()

    # Add provider column to model_calls (multi-provider failover)
    if "model_calls" in tables:
        mc_cols = {row[1] for row in conn.execute("PRAGMA table_info(model_calls)").fetchall()}
        if "provider" not in mc_cols:
            conn.execute("ALTER TABLE model_calls ADD COLUMN provider TEXT")
            # Backfill: extract provider from model_id (split on ':'), default to 'anthropic'
            conn.execute(
                "UPDATE model_calls SET provider = CASE "
                "WHEN INSTR(model_id, ':') > 0 THEN SUBSTR(model_id, 1, INSTR(model_id, ':') - 1) "
                "ELSE 'anthropic' END "
                "WHERE provider IS NULL"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_calls_provider "
                "ON model_calls(provider, called_at DESC)"
            )
            conn.commit()

    # Add latency_ms and arc_id columns to api_calls (model selection: latency tracking)
    if "api_calls" in tables:
        ac_cols = {row[1] for row in conn.execute("PRAGMA table_info(api_calls)").fetchall()}
        if "latency_ms" not in ac_cols:
            conn.execute("ALTER TABLE api_calls ADD COLUMN latency_ms INTEGER")
            conn.commit()
        if "arc_id" not in ac_cols:
            conn.execute("ALTER TABLE api_calls ADD COLUMN arc_id INTEGER")
            conn.commit()
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_calls_arc ON api_calls(arc_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_calls_model_latency "
                "ON api_calls(model, created_at DESC)"
            )
            conn.commit()


def _migrate_model_selection(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 13: Model selection system (model_policies and policy-based arc configuration)."""
    # Create model_policies table if missing (model selection: constraint+preference)
    if "model_policies" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS model_policies (
                id INTEGER PRIMARY KEY,
                name TEXT,
                model TEXT,
                agent_role TEXT,
                temperature REAL,
                max_tokens INTEGER,
                policy_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()

        # Seed model_policies from existing agent_configs rows (only if the
        # legacy table is still present — fresh DBs no longer ship it).
        if "agent_configs" in tables:
            conn.execute(
                "INSERT INTO model_policies (id, model, agent_role, temperature, max_tokens, created_at) "
                "SELECT id, model, agent_role, temperature, max_tokens, created_at FROM agent_configs"
            )
            conn.commit()

    # Add model_policy_id column to arcs (model selection)
    arc_cols = {row[1] for row in conn.execute("PRAGMA table_info(arcs)").fetchall()}
    if "model_policy_id" not in arc_cols:
        conn.execute("ALTER TABLE arcs ADD COLUMN model_policy_id INTEGER")
        conn.commit()
        # Backfill from agent_config_id (only if the legacy column is still
        # present on this DB — Phase 23 drops it).
        if "agent_config_id" in arc_cols:
            conn.execute(
                "UPDATE arcs SET model_policy_id = agent_config_id "
                "WHERE agent_config_id IS NOT NULL AND model_policy_id IS NULL"
            )
            conn.commit()


def _migrate_templates_and_sentinel(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 14: Template system and sentinel arc for conversation-level state."""
    # Create workflow_templates table if missing (template system)
    if "workflow_templates" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workflow_templates (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                yaml_path TEXT NOT NULL,
                required_for_json TEXT,
                steps_json TEXT NOT NULL,
                version INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_workflow_templates_name ON workflow_templates(name);
        """)
        conn.commit()

    # Create sentinel arc with id=0 for conversation-level state (escalation, etc.)
    sentinel_exists = conn.execute("SELECT 1 FROM arcs WHERE id = 0").fetchone()
    if not sentinel_exists:
        conn.execute(
            "INSERT INTO arcs (id, name, goal, status) VALUES (0, '_sentinel', 'Conversation-level state storage', 'completed')"
        )
        conn.commit()


def _drop_deprecated_skills_tables(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 15: Drop deprecated skills and skill_loads tables.

    The skills system has been fully replaced by KB entries under the
    skills/ path.  These tables are no longer read or written by any code.
    """
    for table in ("skill_loads", "skills"):
        if table in tables:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.commit()


def _drop_reflection_tables(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase D PR-C: Drop deprecated ``reflections`` and ``reflection_actions``.

    Reflections now persist only as KB entries under ``reflections/``.
    After PRs #252 (stop SQL writes) and #254 (delete legacy code paths),
    no code reads or writes either table.  Drop ``reflection_actions``
    first since it has a FK referencing ``reflections(id)``.
    """
    for table in ("reflection_actions", "reflections"):
        if table in tables:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.commit()


def _migrate_trigger_event_pipeline(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 16: Add trigger/event pipeline columns.

    - events table: add priority and idempotency_key columns
    - trigger_state table: created via schema.sql (IF NOT EXISTS)
    """
    if "events" in tables:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        if "priority" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN priority INTEGER DEFAULT 0")
        if "idempotency_key" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN idempotency_key TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency_key ON events(idempotency_key)")
        conn.commit()


def _migrate_kb_text_content(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 17: Add kb_text_content table for semantic search body cache.

    Also drops the unused kb_entries_fts virtual table if it exists.
    """
    if "kb_text_content" not in tables:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kb_text_content ("
            "path TEXT PRIMARY KEY, "
            "body TEXT NOT NULL DEFAULT ''"
            ")"
        )
        conn.commit()
    # Drop legacy FTS5 virtual table (unused since PR #148)
    if "kb_entries_fts" in tables:
        try:
            conn.execute("DROP TABLE IF EXISTS kb_entries_fts")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # FTS5 extension may not be available to drop it


def _migrate_hidden_messages(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 18: Add hidden column to messages for internal-only messages.

    Hidden messages are included in the LLM context but not rendered in
    the chat UI.  Used for arc completion notifications that the chat
    agent relays to the user in its own words.
    """
    if "messages" in tables:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "hidden" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN hidden BOOLEAN DEFAULT FALSE")
            conn.commit()


def _migrate_conversation_model_override(
    conn: sqlite3.Connection, tables: set[str]
) -> None:
    """Phase 19: Add ai_provider and model columns to conversations.

    Allows pinning a single conversation to a specific provider/model without
    mutating global config. When NULL, resolution falls back to the global
    model_roles/ai_provider chain. Useful for smoke-testing alternate backends
    (e.g. Ollama on a desktop) against a server whose default stays on
    Anthropic.
    """
    if "conversations" in tables:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(conversations)"
        ).fetchall()}
        if "ai_provider" not in cols:
            conn.execute(
                "ALTER TABLE conversations ADD COLUMN ai_provider TEXT"
            )
        if "model" not in cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN model TEXT")
        conn.commit()


def _migrate_priority_primitive(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 20: Add priority column to arcs and work_queue.

    Lower integer = higher priority (Unix-nice style). Default 100 preserves
    existing FIFO behaviour for all existing rows and new rows that don't
    explicitly pass a priority.
    """
    if "arcs" in tables:
        arc_cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(arcs)"
        ).fetchall()}
        if "priority" not in arc_cols:
            conn.execute(
                "ALTER TABLE arcs ADD COLUMN priority INTEGER NOT NULL DEFAULT 100"
            )
            conn.commit()

    if "work_queue" in tables:
        wq_cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(work_queue)"
        ).fetchall()}
        if "priority" not in wq_cols:
            conn.execute(
                "ALTER TABLE work_queue ADD COLUMN priority INTEGER NOT NULL DEFAULT 100"
            )
            conn.commit()
        # Replace the old (status, created_at) index with one that includes
        # priority so the claim() ORDER BY can be served from the index.
        try:
            conn.execute("DROP INDEX IF EXISTS idx_work_queue_status")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_work_queue_status "
                "ON work_queue(status, priority, created_at)"
            )
            conn.commit()
        except sqlite3.Error:
            pass


def _migrate_resources(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 21: Resource abstraction (resources + arc_resources tables).

    Resources are first-class rows for externally-sourced content (starting
    with web-fetched HTML).  Trust is DERIVED from provenance — a Resource
    is trusted iff it was produced by a template arc whose output a JUDGE
    approved.  Raw ingest (produced_by_template NULL) is forever untrusted.
    """
    if "resources" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS resources (
                id                   INTEGER PRIMARY KEY,
                content_type         TEXT NOT NULL,
                file_path            TEXT,
                byte_size            INTEGER,
                content_hash         TEXT,
                produced_by_arc_id   INTEGER REFERENCES arcs(id),
                produced_by_template TEXT,
                template_verdict     TEXT,
                source_descriptor    TEXT,
                pinned               INTEGER NOT NULL DEFAULT 0,
                retain_until         TIMESTAMP,
                created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deprecated_at        TIMESTAMP,
                deleted_at           TIMESTAMP,
                CHECK (template_verdict IS NULL OR template_verdict IN ('pending','approved','rejected'))
            );
            CREATE INDEX IF NOT EXISTS idx_resources_arc     ON resources(produced_by_arc_id);
            CREATE INDEX IF NOT EXISTS idx_resources_sweep   ON resources(deprecated_at, pinned, deleted_at);
            CREATE INDEX IF NOT EXISTS idx_resources_content ON resources(content_type);
        """)
        conn.commit()

    if "arc_resources" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS arc_resources (
                id            INTEGER PRIMARY KEY,
                arc_id        INTEGER NOT NULL REFERENCES arcs(id) ON DELETE CASCADE,
                resource_id   INTEGER NOT NULL REFERENCES resources(id),
                role          TEXT NOT NULL CHECK (role IN ('input','output')),
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(arc_id, resource_id, role)
            );
            CREATE INDEX IF NOT EXISTS idx_arc_resources_arc ON arc_resources(arc_id);
            CREATE INDEX IF NOT EXISTS idx_arc_resources_res ON arc_resources(resource_id);
        """)
        conn.commit()


def _drop_legacy_agent_configs(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 23: Drop the legacy ``agent_configs`` table and ``arcs.agent_config_id``.

    The ``agent_configs`` table has been fully superseded by ``model_policies``
    (see Phase 13).  By the time this phase runs:

    - ``model_policies`` is guaranteed to exist (created in Phase 13 on
      fresh DBs via schema.sql or in-migration; on existing DBs the
      seeding from ``agent_configs`` already happened).
    - ``arcs.model_policy_id`` is guaranteed to be backfilled from
      ``agent_config_id`` for any rows that had only the legacy column.

    Drop is idempotent: both the column drop and table drop are guarded
    by existence checks.  Fresh DBs (where schema.sql no longer creates
    either) silently no-op.

    Requires SQLite 3.35+ for ``ALTER TABLE DROP COLUMN``.
    """
    # Drop arcs.agent_config_id column if present
    arc_cols = {row[1] for row in conn.execute("PRAGMA table_info(arcs)").fetchall()}
    if "agent_config_id" in arc_cols:
        try:
            conn.execute("ALTER TABLE arcs DROP COLUMN agent_config_id")
            conn.commit()
        except sqlite3.OperationalError:
            # Older SQLite without DROP COLUMN support — leave the column in place;
            # all code now reads/writes only model_policy_id.
            logger.warning(
                "Could not drop arcs.agent_config_id column (SQLite < 3.35?); "
                "column will remain but is unused."
            )

    # Drop agent_configs table if present
    if "agent_configs" in tables:
        conn.execute("DROP INDEX IF EXISTS idx_agent_configs_dedup")
        conn.execute("DROP TABLE IF EXISTS agent_configs")
        conn.commit()


def _migrate_webhook_resource_wrap(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 22: Webhook -> Resource wrapping columns on webhook_subscriptions.

    Adds two optional columns (both SQLite-safe ALTER TABLE ADD COLUMN):

      resource_content_type TEXT         -- NULL = legacy behaviour
      auto_approve_verdict  INTEGER NOT NULL DEFAULT 0

    Subscriptions with ``resource_content_type`` set cause the webhook
    dispatch handler to wrap the payload as a raw-ingest Resource and
    spawn a template pipeline (REVIEWER[+JUDGE]) to process it.  Per the
    user's config-override directive ("nothing starts trusted"),
    ``auto_approve_verdict`` defaults to 0 — the JUDGE arc is what
    promotes derived trust.  Setting it to 1 is an explicit override:
    no JUDGE is spawned and the REVIEWER's completion auto-approves
    the derived Resource's template_verdict.
    """
    if "webhook_subscriptions" not in tables:
        return
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(webhook_subscriptions)"
    ).fetchall()}
    if "resource_content_type" not in cols:
        conn.execute(
            "ALTER TABLE webhook_subscriptions ADD COLUMN resource_content_type TEXT"
        )
        conn.commit()
    if "auto_approve_verdict" not in cols:
        conn.execute(
            "ALTER TABLE webhook_subscriptions "
            "ADD COLUMN auto_approve_verdict INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()


def _migrate_file_provenance(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 23: File provenance tracking for cross-trust file isolation.

    Per leadership decision D10 (2026-04-29): ``tool_backends/files.py``
    historically had NO path allowlist, NO per-arc scoping, and NO
    integrity_level check on read or write.  An untrusted arc could
    materialise prompt-injected content at an arbitrary path; a later
    trusted arc reading the same path would smuggle untrusted bytes into
    a trusting AI's context.

    This table records who wrote each path (resolved via realpath) and at
    what integrity level.  ``handle_read`` consults the row and refuses
    when a trusted reader would otherwise see content written by a
    non-trusted writer.  Per-path PRIMARY KEY means an overwrite by a
    more-tainted writer updates the row (the write-side audit trail
    lives in ``arc_history`` events of type ``file_written``).

    This migration is forward-looking: pre-existing files have no
    provenance row, so reads of them remain unrestricted.  That is the
    intended policy — the rule applies to writes that happen *after* the
    enforcement is deployed.
    """
    if "file_provenance" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS file_provenance (
                path                   TEXT PRIMARY KEY,
                writer_arc_id          INTEGER NOT NULL,
                writer_integrity_level TEXT NOT NULL,
                written_at             TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_file_provenance_writer
                ON file_provenance(writer_arc_id);
        """)
        conn.commit()


def _migrate_resources_kind(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 25: Add ``kind`` column to ``resources`` (D24 §11 / SD12).

    The ``kind`` column tags a Resource with the dataclass name that
    deserialises its bytes — the platform's JUDGE-dispatch wrapper reads
    it to resolve the right dataclass for REVIEWER → JUDGE handoffs that
    used to ride on the ``_extraction_output`` arc-state shortcut.
    Existing rows stay NULL (raw-ingest Resources have no kind, and the
    old kind-less derived Resources continue to dispatch via the legacy
    ``produced_by_template`` lookup paths that don't rely on kind).

    Idempotent: skips the ALTER and the index when both already exist.
    """
    if "resources" not in tables:
        return
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(resources)"
    ).fetchall()}
    if "kind" not in cols:
        conn.execute("ALTER TABLE resources ADD COLUMN kind TEXT")
        conn.commit()
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_resources_kind ON resources(kind)"
    )
    conn.commit()


def _migrate_installed_packages(
    conn: sqlite3.Connection, tables: set[str],
) -> None:
    """Phase 26: ``installed_packages`` + ``installed_packages_templates``
    (D24 stage 3a — copy-on-install + hash-pinning for capability packages).

    The ``installed_packages`` table records every package the operator has
    installed via ``install_package``: name (PRIMARY KEY), version, the
    deterministic SHA-256 hash of the source tree at install time, the
    source path the bytes were copied from, the install path, and the
    UTC timestamp.  ``installed_packages_templates`` records the (flat,
    unprefixed; SD7) template names each package shipped, so the
    uninstall code can refuse to remove a package that still has live
    arcs referencing its templates (SD9).

    Both tables are additive (no existing data touched) and idempotent
    (CREATE IF NOT EXISTS).  Migration is safe to re-run.

    The actual schema strings live in ``carpenter.packages.installer``
    so the install machinery and the migration agree.
    """
    from .packages.installer import ensure_installer_tables
    ensure_installer_tables(conn)


def _migrate_package_state(
    conn: sqlite3.Connection, tables: set[str],
) -> None:
    """Phase 27: ``package_state`` + ``package_state_archive`` (D24 / Phase 3a).

    Per-package mutable state primitive.  Each capability package gets
    its own (key, value_json) keyspace, isolated from every other
    package by the (package_name, key) primary key and the FK
    ON DELETE CASCADE to ``installed_packages.name``.  See the
    ``package_state`` definition in ``schema.sql`` for the full
    motivation.

    Both tables are additive (no existing data touched) and idempotent
    (CREATE IF NOT EXISTS).  Migration is safe to re-run; the actual
    schema strings are mirrored from ``schema.sql`` so the migration
    works on existing DBs that were initialised before this phase
    landed.
    """
    if "package_state" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS package_state (
                package_name TEXT NOT NULL,
                key          TEXT NOT NULL,
                value_json   TEXT NOT NULL,
                version      INTEGER NOT NULL DEFAULT 1,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (package_name, key),
                FOREIGN KEY (package_name) REFERENCES installed_packages(name)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_package_state_pkg
                ON package_state(package_name);
        """)
        conn.commit()
    if "package_state_archive" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS package_state_archive (
                package_name TEXT NOT NULL,
                key          TEXT NOT NULL,
                value_json   TEXT NOT NULL,
                version      INTEGER NOT NULL,
                archived_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (package_name, key)
            );
            CREATE INDEX IF NOT EXISTS idx_package_state_archive_pkg
                ON package_state_archive(package_name);
        """)
        conn.commit()


def _migrate_package_vectors(
    conn: sqlite3.Connection, tables: set[str],
) -> None:
    """Phase 28: ``package_vectors`` (D24 / Phase 2 PR-2 — D6).

    Per-package vector store primitive.  Each capability package gets
    its own (id, embedding, metadata) namespace, isolated from every
    other package by the (package_name, id) primary key and the FK
    ON DELETE CASCADE to ``installed_packages.name``.  See the
    ``package_vectors`` definition in ``schema.sql`` for the full
    motivation.

    The table is additive (no existing data touched) and idempotent
    (CREATE IF NOT EXISTS).  Migration is safe to re-run; the schema
    is mirrored from ``schema.sql`` so the migration works on existing
    DBs initialised before this phase landed.

    Vectors are derived data — D9 dictates they are wiped on uninstall
    via the FK cascade, never archived.  No companion archive table.
    """
    if "package_vectors" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS package_vectors (
                package_name   TEXT NOT NULL,
                id             TEXT NOT NULL,
                embedding      BLOB NOT NULL,
                model_identity TEXT NOT NULL,
                vector_dim     INTEGER NOT NULL,
                metadata_json  TEXT NOT NULL DEFAULT '{}',
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (package_name, id),
                FOREIGN KEY (package_name) REFERENCES installed_packages(name)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_package_vectors_pkg_model
                ON package_vectors(package_name, model_identity);
        """)
        conn.commit()


def _migrate_arc_step_role(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 24: Add step_role column on arcs (D2 PR-α — name → role refactor).

    Per leadership decision D18 (2026-04-29): the handler-registry dispatch
    path historically keyed on ``arcs.name``, the human-readable step label.
    The structural identifier should be the template step's ``role``. This
    phase adds a nullable ``step_role`` column on ``arcs`` and backfills it
    for existing template-instantiated arcs by joining through
    ``workflow_templates.steps_json`` (parsed in Python — SQLite has no
    JSON path operators we depend on).

    Backfill is fail-soft: arcs whose templates haven't declared roles
    keep ``step_role = NULL`` and dispatch via the name-fallback path.
    Idempotent: re-running the migration on a DB that already has the
    column is a no-op (only NULL rows are touched).
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(arcs)").fetchall()}
    if "step_role" not in cols:
        conn.execute("ALTER TABLE arcs ADD COLUMN step_role TEXT")
        conn.commit()

    # Index is created on schema and re-asserted here (idempotent).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_arcs_step_role "
        "ON arcs(template_id, step_role)"
    )
    conn.commit()

    # Backfill: for each arc with a template_id and NULL step_role, look up
    # the matching step in workflow_templates.steps_json by name and write
    # step.role if present.
    import json as _json

    # Gather all template steps once, keyed by template_id.
    template_steps: dict[int, dict[str, str]] = {}
    template_rows = conn.execute(
        "SELECT id, steps_json FROM workflow_templates"
    ).fetchall()
    for row in template_rows:
        try:
            steps = _json.loads(row[1])
        except (TypeError, ValueError):
            continue
        # steps may be a dict or list — accept both shapes.
        if isinstance(steps, dict):
            steps_iter = steps.get("steps") if "steps" in steps else []
        elif isinstance(steps, list):
            steps_iter = steps
        else:
            steps_iter = []
        if not isinstance(steps_iter, list):
            continue
        name_to_role: dict[str, str] = {}
        for step in steps_iter:
            if not isinstance(step, dict):
                continue
            step_name = step.get("name")
            step_role = step.get("role")
            if step_name and step_role:
                name_to_role[step_name] = step_role
        if name_to_role:
            template_steps[row[0]] = name_to_role

    if not template_steps:
        return

    # Update arcs in a single pass.
    arc_rows = conn.execute(
        "SELECT id, name, template_id FROM arcs "
        "WHERE template_id IS NOT NULL AND step_role IS NULL"
    ).fetchall()
    updates: list[tuple[str, int]] = []
    for row in arc_rows:
        arc_id, arc_name, tid = row[0], row[1], row[2]
        roles = template_steps.get(tid)
        if not roles:
            continue
        role = roles.get(arc_name)
        if role:
            updates.append((role, arc_id))

    if updates:
        conn.executemany(
            "UPDATE arcs SET step_role = ? WHERE id = ?",
            updates,
        )
        conn.commit()


def _migrate_template_owner_package(
    conn: sqlite3.Connection, tables: set[str]
) -> None:
    """Add ``owner_package`` column on workflow_templates.

    Records which capability package shipped a template. When set, the
    template's instantiation stamps the owning package's per-arc grant
    (``pkg.<owner>``) onto every step arc so the package's EXECUTOR can
    invoke the package's registered trusted capability verbs. Platform-
    shipped templates leave it NULL and receive no grant.

    Idempotent: only adds the column when absent.
    """
    if "workflow_templates" not in tables:
        return
    cols = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(workflow_templates)"
        ).fetchall()
    }
    if "owner_package" not in cols:
        conn.execute(
            "ALTER TABLE workflow_templates ADD COLUMN owner_package TEXT"
        )
        conn.commit()


def _migrate_arc_origin(conn: sqlite3.Connection, tables: set) -> None:
    """Add arc provenance columns: origin_kind + origin_ref.

    Records where a root arc tree came from (chat, trigger, schedule,
    webhook, reflection, arc, manual, cli) and a compact JSON reference
    to the originating entity.  Descendants inherit the root's origin at
    creation time, so background/trigger-spawned trees carry provenance
    even though they have no chat conversation link.
    """
    if "arcs" not in tables:
        return
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(arcs)").fetchall()
    }
    changed = False
    if "origin_kind" not in cols:
        conn.execute("ALTER TABLE arcs ADD COLUMN origin_kind TEXT")
        changed = True
    if "origin_ref" not in cols:
        conn.execute("ALTER TABLE arcs ADD COLUMN origin_ref TEXT")
        changed = True
    if changed:
        conn.commit()


def run_migrations(conn: sqlite3.Connection) -> None:
    """Run all data migrations for existing databases.

    Called after schema init. Handles column additions for schema evolution.
    Migrations are organized into logical phases for maintainability.
    """
    # Query all tables once to pass to migration functions
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    # Run migrations in order (must maintain sequence for proper schema evolution)
    _migrate_basic_schema(conn, tables)
    _migrate_conversation_arcs(conn, tables)
    _migrate_execution_sessions(conn, tables)
    _migrate_trust_boundary_system(conn, tables)
    _migrate_fts_index(conn, tables)
    _migrate_compaction_system(conn, tables)
    _migrate_notifications(conn, tables)
    _migrate_agent_configs(conn, tables)
    _migrate_scheduling_and_contracts(conn, tables)
    _migrate_channel_connectors(conn, tables)
    _migrate_code_execution_enhancements(conn, tables)
    _migrate_arc_retry_system(conn, tables)
    _migrate_model_selection(conn, tables)
    _migrate_templates_and_sentinel(conn, tables)
    _migrate_template_owner_package(conn, tables)
    _drop_deprecated_skills_tables(conn, tables)
    _drop_reflection_tables(conn, tables)
    _migrate_trigger_event_pipeline(conn, tables)
    _migrate_kb_text_content(conn, tables)
    _migrate_hidden_messages(conn, tables)
    _migrate_conversation_model_override(conn, tables)
    _migrate_priority_primitive(conn, tables)
    _migrate_resources(conn, tables)
    _migrate_webhook_resource_wrap(conn, tables)
    _migrate_file_provenance(conn, tables)
    _drop_legacy_agent_configs(conn, tables)
    _migrate_arc_step_role(conn, tables)
    _migrate_resources_kind(conn, tables)
    _migrate_installed_packages(conn, tables)
    # package_state must run after installed_packages (FK target).
    _migrate_package_state(conn, tables)
    # package_vectors must run after installed_packages (FK target).
    _migrate_package_vectors(conn, tables)
    _migrate_arc_origin(conn, tables)

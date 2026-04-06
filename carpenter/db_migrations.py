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


def _migrate_reflections_and_agent_configs(conn: sqlite3.Connection, tables: set[str]) -> None:
    """Phase 8: Reflection actions and agent configuration system."""
    # Create reflection_actions table if missing (auto-action from reflections)
    if "reflection_actions" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS reflection_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reflection_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                action_description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                review_mode TEXT,
                arc_id INTEGER,
                outcome TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (reflection_id) REFERENCES reflections(id)
            );
            CREATE INDEX IF NOT EXISTS idx_reflection_actions_reflection ON reflection_actions(reflection_id);
            CREATE INDEX IF NOT EXISTS idx_reflection_actions_status ON reflection_actions(status);
        """)
        conn.commit()

    # Create agent_configs table if missing (model roles consolidation)
    if "agent_configs" not in tables:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_configs (
                id INTEGER PRIMARY KEY,
                model TEXT NOT NULL,
                agent_role TEXT,
                temperature REAL,
                max_tokens INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_configs_dedup
                ON agent_configs(model, COALESCE(agent_role, ''), COALESCE(temperature, -1), COALESCE(max_tokens, -1));
        """)
        conn.commit()

    # Add agent_config_id column to arcs
    arc_cols = {row[1] for row in conn.execute("PRAGMA table_info(arcs)").fetchall()}
    if "agent_config_id" not in arc_cols:
        conn.execute("ALTER TABLE arcs ADD COLUMN agent_config_id INTEGER REFERENCES agent_configs(id)")
        conn.commit()


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

        # Seed model_policies from existing agent_configs rows
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
        # Backfill from agent_config_id
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
    _migrate_reflections_and_agent_configs(conn, tables)
    _migrate_scheduling_and_contracts(conn, tables)
    _migrate_channel_connectors(conn, tables)
    _migrate_code_execution_enhancements(conn, tables)
    _migrate_arc_retry_system(conn, tables)
    _migrate_model_selection(conn, tables)
    _migrate_templates_and_sentinel(conn, tables)
    _drop_deprecated_skills_tables(conn, tables)
    _migrate_trigger_event_pipeline(conn, tables)
    _migrate_kb_text_content(conn, tables)
    _migrate_hidden_messages(conn, tables)

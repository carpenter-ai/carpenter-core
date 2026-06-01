-- Carpenter Database Schema
-- All tables use IF NOT EXISTS for idempotent initialization.

-- Arcs: unified work nodes in a tree
CREATE TABLE IF NOT EXISTS arcs (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES arcs(id),
    name TEXT NOT NULL,
    goal TEXT,
    status TEXT DEFAULT 'pending',
    step_order INTEGER DEFAULT 0,
    depth INTEGER DEFAULT 0,
    code_file_id INTEGER REFERENCES code_files(id),
    template_id INTEGER REFERENCES workflow_templates(id),
    step_role TEXT,
    from_template BOOLEAN DEFAULT FALSE,
    template_mutable BOOLEAN DEFAULT FALSE,
    timeout_minutes INTEGER,
    disk_workspace TEXT,
    integrity_level TEXT DEFAULT 'trusted',
    output_type TEXT DEFAULT 'python',
    agent_type TEXT DEFAULT 'EXECUTOR',
    descendant_tokens INTEGER DEFAULT 0,
    descendant_executions INTEGER DEFAULT 0,
    descendant_arc_count INTEGER DEFAULT 0,
    model_policy_id INTEGER,
    wait_until TEXT,
    output_contract TEXT,
    arc_role TEXT DEFAULT 'worker',
    verification_target_id INTEGER REFERENCES arcs(id),
    priority INTEGER NOT NULL DEFAULT 100,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_arcs_parent ON arcs(parent_id);
CREATE INDEX IF NOT EXISTS idx_arcs_status ON arcs(status);
CREATE INDEX IF NOT EXISTS idx_arcs_integrity_level ON arcs(integrity_level);
CREATE INDEX IF NOT EXISTS idx_arcs_step_role ON arcs(template_id, step_role);

-- Arc activation conditions
CREATE TABLE IF NOT EXISTS arc_activations (
    id INTEGER PRIMARY KEY,
    arc_id INTEGER REFERENCES arcs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    filter_json TEXT,
    UNIQUE(arc_id, event_type, filter_json)
);
CREATE INDEX IF NOT EXISTS idx_arc_activations_event ON arc_activations(event_type);

-- Arc history: immutable log per arc
CREATE TABLE IF NOT EXISTS arc_history (
    id INTEGER PRIMARY KEY,
    arc_id INTEGER REFERENCES arcs(id) ON DELETE CASCADE,
    entry_type TEXT NOT NULL,
    content_json TEXT NOT NULL,
    code_file_id INTEGER REFERENCES code_files(id),
    actor TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_arc_history_arc ON arc_history(arc_id);

-- Code files: every piece of agent-generated Python
CREATE TABLE IF NOT EXISTS code_files (
    id INTEGER PRIMARY KEY,
    file_path TEXT NOT NULL,
    source TEXT NOT NULL,
    arc_id INTEGER REFERENCES arcs(id),
    trust_tier INTEGER DEFAULT 1,
    review_status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Code executions: tracking each run
CREATE TABLE IF NOT EXISTS code_executions (
    id INTEGER PRIMARY KEY,
    code_file_id INTEGER REFERENCES code_files(id),
    execution_status TEXT,
    exit_code INTEGER,
    result_summary TEXT,
    executor_type TEXT,
    pid_or_container TEXT,
    command_hash TEXT,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    log_file TEXT,
    taint_source TEXT
);

-- Execution sessions: platform-controlled session IDs for callback authentication
CREATE TABLE IF NOT EXISTS execution_sessions (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE,
    code_file_id INTEGER REFERENCES code_files(id),
    execution_id INTEGER REFERENCES code_executions(id),
    reviewed BOOLEAN NOT NULL,
    conversation_id INTEGER REFERENCES conversations(id),
    execution_context TEXT DEFAULT 'reviewed',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_sessions_session_id
    ON execution_sessions(session_id, expires_at);

-- Events: what happened (append-only)
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source TEXT,
    processed BOOLEAN DEFAULT FALSE,
    priority INTEGER DEFAULT 0,
    idempotency_key TEXT UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_events_type_processed ON events(event_type, processed);
CREATE INDEX IF NOT EXISTS idx_events_priority ON events(processed, priority DESC, created_at ASC);

-- Event matchers: dynamically registered by running arcs
CREATE TABLE IF NOT EXISTS event_matchers (
    id INTEGER PRIMARY KEY,
    arc_id INTEGER REFERENCES arcs(id),
    event_type TEXT NOT NULL,
    filter_json TEXT,
    timeout_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_event_matchers_type ON event_matchers(event_type);

-- Work queue: what needs to be done
CREATE TABLE IF NOT EXISTS work_queue (
    id INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    idempotency_key TEXT UNIQUE,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    claimed_at TIMESTAMP,
    completed_at TIMESTAMP,
    scheduled_at TEXT,
    priority INTEGER NOT NULL DEFAULT 100
);
CREATE INDEX IF NOT EXISTS idx_work_queue_status ON work_queue(status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_work_queue_scheduled ON work_queue(status, scheduled_at) WHERE status = 'pending';

-- Model health tracking: per-model success/failure history for adaptive backoff
CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY,
    model_id TEXT NOT NULL,
    success BOOLEAN NOT NULL,
    error_type TEXT,
    provider TEXT,
    called_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_model_calls_model ON model_calls(model_id, called_at DESC);
CREATE INDEX IF NOT EXISTS idx_model_calls_provider ON model_calls(provider, called_at DESC);

-- Cron entries: Python-native cron via croniter.
-- ``name`` is UNIQUE; ``trigger_manager.add_cron()`` / ``add_once()`` perform
-- an idempotent upsert on name conflict (re-adding the same name updates the
-- entry in place rather than raising IntegrityError).
CREATE TABLE IF NOT EXISTS cron_entries (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    cron_expr TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_payload_json TEXT,
    next_fire_at TIMESTAMP NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    one_shot BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Workflow templates: YAML-defined process constraints
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

-- Conversations: chat context tracking
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    title TEXT,
    summary TEXT,
    archived BOOLEAN DEFAULT FALSE,
    channel_type TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_message_at TIMESTAMP,
    context_tokens INTEGER DEFAULT 0,
    ai_provider TEXT,
    model TEXT
);

-- FTS5 full-text search index for conversation memory
CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
    title,
    summary,
    content='conversations',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Triggers to keep FTS index in sync with conversations table
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

-- Compaction events: context window compaction records
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

-- Messages: individual chat messages
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    content_json TEXT,
    arc_id INTEGER REFERENCES arcs(id),
    compaction_event_id INTEGER REFERENCES compaction_events(id),
    hidden BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, role, id);

-- Tool calls: audit trail for chat tool_use
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id),
    message_id INTEGER REFERENCES messages(id),
    tool_use_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    input_json TEXT NOT NULL,
    result_text TEXT,
    duration_ms INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Arc state: per-arc key-value persistent state
CREATE TABLE IF NOT EXISTS arc_state (
    id INTEGER PRIMARY KEY,
    arc_id INTEGER NOT NULL REFERENCES arcs(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(arc_id, key)
);
CREATE INDEX IF NOT EXISTS idx_arc_state_arc ON arc_state(arc_id);

-- Arc read grants: explicit cross-arc read permissions
CREATE TABLE IF NOT EXISTS arc_read_grants (
    id INTEGER PRIMARY KEY,
    reader_arc_id INTEGER NOT NULL REFERENCES arcs(id) ON DELETE CASCADE,
    target_arc_id INTEGER NOT NULL REFERENCES arcs(id) ON DELETE CASCADE,
    depth TEXT NOT NULL DEFAULT 'subtree',  -- 'self' or 'subtree'
    reason TEXT,
    granted_by TEXT,  -- 'platform', 'parent:<id>', 'chat:<conv_id>'
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(reader_arc_id, target_arc_id)
);
CREATE INDEX IF NOT EXISTS idx_arc_read_grants_reader ON arc_read_grants(reader_arc_id);
CREATE INDEX IF NOT EXISTS idx_arc_read_grants_target ON arc_read_grants(target_arc_id);

-- API calls: per-call token and cache metrics from Claude API
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id),
    model TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_creation_input_tokens INTEGER DEFAULT 0,
    cache_read_input_tokens INTEGER DEFAULT 0,
    stop_reason TEXT,
    latency_ms INTEGER,
    arc_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_api_calls_conversation ON api_calls(conversation_id);
CREATE INDEX IF NOT EXISTS idx_api_calls_arc ON api_calls(arc_id);
CREATE INDEX IF NOT EXISTS idx_api_calls_model_latency ON api_calls(model, created_at DESC);

-- Archived arcs: completed root arcs moved here after retention period
CREATE TABLE IF NOT EXISTS archived_arcs (
    id INTEGER PRIMARY KEY,
    original_id INTEGER NOT NULL,
    tree_json TEXT NOT NULL,
    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Conversation-arc links: which arcs are relevant to which conversations
CREATE TABLE IF NOT EXISTS conversation_arcs (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    arc_id INTEGER REFERENCES arcs(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(conversation_id, arc_id)
);
CREATE INDEX IF NOT EXISTS idx_conversation_arcs_conv ON conversation_arcs(conversation_id);

-- Conversation trust taint tracking
CREATE TABLE IF NOT EXISTS conversation_taint (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id),
    source_tool TEXT NOT NULL,
    tainted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_conversation_taint_conv ON conversation_taint(conversation_id);

-- Trust audit log: paper trail of all trust boundary decisions
CREATE TABLE IF NOT EXISTS trust_audit_log (
    id INTEGER PRIMARY KEY,
    arc_id INTEGER,
    event_type TEXT NOT NULL,
    details_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trust_audit_arc ON trust_audit_log(arc_id);
CREATE INDEX IF NOT EXISTS idx_trust_audit_event ON trust_audit_log(event_type);

-- Notifications: audit trail and delivery tracking for user notifications
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

-- Model policies: constraint+preference bundles for model selection
-- (Replaces the legacy agent_configs table; model = hard pin, policy_json = selector.)
CREATE TABLE IF NOT EXISTS model_policies (
    id INTEGER PRIMARY KEY,
    name TEXT,
    model TEXT,                    -- Hard pin (NULL = use selector)
    agent_role TEXT,
    temperature REAL,
    max_tokens INTEGER,
    policy_json TEXT,              -- {"constraints": {...}, "preference": [...]}
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Review keys: Fernet symmetric keys for encrypted untrusted output
CREATE TABLE IF NOT EXISTS review_keys (
    id INTEGER PRIMARY KEY,
    target_arc_id INTEGER NOT NULL,
    reviewer_arc_id INTEGER NOT NULL,
    fernet_key_encrypted BLOB NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(target_arc_id, reviewer_arc_id)
);
CREATE INDEX IF NOT EXISTS idx_review_keys_target ON review_keys(target_arc_id);

-- Security policies: default-deny allowlists for policy-typed literals
CREATE TABLE IF NOT EXISTS security_policies (
    id INTEGER PRIMARY KEY,
    policy_type TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(policy_type, value)
);
CREATE INDEX IF NOT EXISTS idx_security_policies_type ON security_policies(policy_type);

-- Channel bindings: maps external channel identities to conversations
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

-- Verified code hashes: trusted code that has passed flow analysis
CREATE TABLE IF NOT EXISTS verified_code_hashes (
    code_hash TEXT PRIMARY KEY,
    input_schemas_json TEXT,
    policy_version INTEGER DEFAULT 0,
    verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Webhook subscriptions: maps incoming webhooks to arc/work actions.
--
-- Phase B (PR B2) of the Resource refactor added two optional columns:
--   resource_content_type: when non-NULL, the incoming webhook payload
--     is wrapped as a raw-ingest Resource (content_type=<that value>)
--     and a REVIEWER+JUDGE template pipeline is spawned to process it.
--     When NULL, legacy behaviour applies (payload stored in arc_state /
--     work_queue).
--   auto_approve_verdict: user-configurable override. When set to 1,
--     no JUDGE arc is spawned; the REVIEWER's completion auto-marks the
--     derived Resource verdict as 'approved'. Default 0 honours the
--     "nothing starts trusted" invariant.
CREATE TABLE IF NOT EXISTS webhook_subscriptions (
    id INTEGER PRIMARY KEY,
    webhook_id TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    source_config TEXT NOT NULL DEFAULT '{}',
    event_filter TEXT NOT NULL DEFAULT '[]',
    action_type TEXT NOT NULL,
    action_config TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    conversation_id INTEGER,
    forge_hook_id INTEGER,
    resource_content_type TEXT,
    auto_approve_verdict INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_webhook_subscriptions_webhook ON webhook_subscriptions(webhook_id);

-- Knowledge Base entries: unified navigable graph of capabilities and knowledge
CREATE TABLE IF NOT EXISTS kb_entries (
    path TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    trust_level TEXT NOT NULL DEFAULT 'trusted',
    entry_type TEXT NOT NULL,
    auto_source TEXT,
    byte_count INTEGER NOT NULL DEFAULT 0,
    linked_byte_count INTEGER NOT NULL DEFAULT 0,
    last_accessed TIMESTAMP,
    access_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Knowledge Base links: directed edges between entries
CREATE TABLE IF NOT EXISTS kb_links (
    source_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    link_text TEXT,
    UNIQUE(source_path, target_path)
);
CREATE INDEX IF NOT EXISTS idx_kb_links_source ON kb_links(source_path);
CREATE INDEX IF NOT EXISTS idx_kb_links_target ON kb_links(target_path);

-- Knowledge Base access log: tracks entry reads for analytics
CREATE TABLE IF NOT EXISTS kb_access_log (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    arc_id INTEGER,
    conversation_id INTEGER,
    accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_kb_access_log_path ON kb_access_log(path);

-- Body text cache for KB entries (used by search reindex)
CREATE TABLE IF NOT EXISTS kb_text_content (
    path TEXT PRIMARY KEY,
    body TEXT NOT NULL DEFAULT ''
);

-- Knowledge Base embeddings for semantic search
CREATE TABLE IF NOT EXISTS kb_embeddings (
    path TEXT PRIMARY KEY,
    embedding BLOB NOT NULL,
    model TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- KB source file hashes for auto-generation change detection
CREATE TABLE IF NOT EXISTS kb_source_hashes (
    source_path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- File change processing queue for KB updates
CREATE TABLE IF NOT EXISTS kb_change_queue (
    id INTEGER PRIMARY KEY,
    file_path TEXT NOT NULL,
    change_type TEXT NOT NULL,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP,
    UNIQUE(file_path, detected_at)
);

-- Trigger state: persistent state for triggers (counters, last fired, etc.)
CREATE TABLE IF NOT EXISTS trigger_state (
    id INTEGER PRIMARY KEY,
    trigger_name TEXT NOT NULL UNIQUE,
    trigger_type TEXT NOT NULL,
    last_fired_at TIMESTAMP,
    counter INTEGER DEFAULT 0,
    metadata_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Resources: first-class rows for externally-sourced content (web fetches, etc.)
-- Trust is DERIVED from provenance, not stored.  resource_trust(r) = 'trusted'
-- iff produced_by_template IS NOT NULL AND template_verdict = 'approved'.
-- Otherwise untrusted.  Raw ingest (produced_by_template NULL) is forever
-- untrusted; the only way to become trusted is to be produced by a template
-- arc whose output was approved by a JUDGE.
CREATE TABLE IF NOT EXISTS resources (
    id                   INTEGER PRIMARY KEY,
    content_type         TEXT NOT NULL,
    file_path            TEXT,
    byte_size            INTEGER,
    content_hash         TEXT,
    produced_by_arc_id   INTEGER REFERENCES arcs(id),
    produced_by_template TEXT,
    template_verdict     TEXT,                      -- NULL, 'pending', 'approved', 'rejected'
    source_descriptor    TEXT,                      -- free-form JSON string
    kind                 TEXT,                      -- D24 SD12: dataclass-name dispatch tag for kind-typed handoffs
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
CREATE INDEX IF NOT EXISTS idx_resources_kind    ON resources(kind);

-- Arc-resource links: which Resources were consumed (input) or produced
-- (output) by which arcs.  Lineage is reconstructed via this join plus
-- resources.produced_by_arc_id.  Input role is enforced against the arc's
-- integrity_level (untrusted arcs cannot read Resources).
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

-- File provenance for cross-trust read isolation (D10, 2026-04-29).
-- handle_write records (path, writer_arc_id, writer_integrity_level) so
-- handle_read can refuse when a trusted arc would otherwise read a file
-- written by a non-trusted arc.  Path is the realpath; primary key by
-- path so an overwrite by a more-tainted writer updates the row (the
-- write-side audit trail lives in arc_history events of type
-- 'file_written').  See tool_backends/files.py for the enforcement.
CREATE TABLE IF NOT EXISTS file_provenance (
    path                   TEXT PRIMARY KEY,
    writer_arc_id          INTEGER NOT NULL,
    writer_integrity_level TEXT NOT NULL,
    written_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_provenance_writer ON file_provenance(writer_arc_id);

-- Per-package mutable state primitive (D24 / Phase 3a).
-- Each capability package gets its own (key, value_json) keyspace,
-- isolated from every other package by the (package_name, key) primary
-- key and the FK ON DELETE CASCADE to ``installed_packages.name``.
--
-- ``version`` is a monotonically-increasing integer used for
-- compare-and-swap (CAS) semantics — callers read (value, version) and
-- then write with the expected version; if a concurrent writer bumped
-- the row, the CAS fails and the caller retries.  This is the
-- contention guard used by GmailPollTrigger's ``poll_in_progress`` flag.
--
-- Isolation invariant (I9): a ``PackageStateHandle`` is bound to a
-- single package_name at construction; the methods only operate on
-- ``self.package_name``.  Packages never receive another package's
-- handle, and the SQL primary key precludes cross-package key access.
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
CREATE INDEX IF NOT EXISTS idx_package_state_pkg ON package_state(package_name);

-- Archive of per-package state preserved across uninstalls.  The
-- chat-tool uninstall flow can preserve state (via archive=True), in
-- which case the rows are copied here BEFORE the cascade delete on
-- ``installed_packages`` wipes ``package_state``.  This table has no
-- FK so it survives independently and can be restored on re-install.
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

-- Per-package vector store (D24 / Phase 2 PR-2 — D6).
-- Each capability package gets its own (id, embedding, metadata)
-- namespace in the ``package_vectors`` table, isolated from every
-- other package by the (package_name, id) primary key and the FK
-- ON DELETE CASCADE to ``installed_packages.name``.  Mirrors the
-- ``package_state`` isolation pattern.
--
-- ``embedding`` is the raw binary blob produced by
-- ``carpenter.embeddings.codec._serialize_embedding`` (little-endian
-- packed float32).  ``model_identity`` records the embedding service's
-- identity fingerprint at upsert time (e.g. ``local:all-MiniLM-L6-v2:384``)
-- so search-time mismatches (model swapped under the data) raise
-- ``EmbeddingModelMismatchError`` rather than silently returning
-- wrong-dim cosine scores.  ``vector_dim`` is denormalised for cheap
-- reads at deserialisation time.
--
-- Isolation invariant (I9): a ``PackageVectorStore`` is bound to a
-- single package_name at construction; no method on the handle takes
-- a package_name parameter.  Packages never receive another package's
-- handle, and the SQL primary key precludes cross-package id access.
-- Vector data is derived (rebuildable from source); D9 specifies it
-- is wiped on uninstall via FK cascade, never archived.
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

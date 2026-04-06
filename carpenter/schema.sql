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
    agent_config_id INTEGER REFERENCES agent_configs(id),
    model_policy_id INTEGER,
    wait_until TEXT,
    output_contract TEXT,
    arc_role TEXT DEFAULT 'worker',
    verification_target_id INTEGER REFERENCES arcs(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_arcs_parent ON arcs(parent_id);
CREATE INDEX IF NOT EXISTS idx_arcs_status ON arcs(status);
CREATE INDEX IF NOT EXISTS idx_arcs_integrity_level ON arcs(integrity_level);

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
    scheduled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_work_queue_status ON work_queue(status, created_at);
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

-- Cron entries: Python-native cron via croniter
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
    context_tokens INTEGER DEFAULT 0
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

-- Reflections: cadenced self-reflection entries (daily/weekly/monthly)
CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY,
    cadence TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    content TEXT NOT NULL,
    proposed_actions TEXT,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_reflections_cadence ON reflections(cadence, period_end);

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

-- Reflection actions: proposed actions from reflections that are auto-submitted
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

-- Agent configs: reusable model/role/parameter bundles for arcs
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

-- Model policies: constraint+preference bundles for model selection
CREATE TABLE IF NOT EXISTS model_policies (
    id INTEGER PRIMARY KEY,
    name TEXT,
    model TEXT,                    -- Hard pin (NULL = use selector)
    agent_role TEXT,               -- Preserved from agent_configs
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

-- Webhook subscriptions: maps incoming webhooks to arc/work actions
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

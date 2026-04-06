"""Shared test fixtures for the Carpenter test suite."""

import os

# Ensure pytest basetemp uses RAM-backed tmpfs (/dev/shm) instead of the SD
# card's /tmp.  This MUST happen before ``import tempfile`` because
# tempfile.gettempdir() caches its result on first call.  Without this, a
# 15-minute xdist run under /tmp is vulnerable to:
#   - No cleanup-lock protection (tmp_path_retention_count=0 skips locks)
#   - Concurrent pytest invocations deleting the basetemp mid-run
# The ~/bin/run-tests wrapper sets TMPDIR too, but this failsafe covers
# direct invocations and git-worktree paths the wrapper might not detect.
if "TMPDIR" not in os.environ and os.path.isdir("/dev/shm"):
    os.environ["TMPDIR"] = "/dev/shm"

import hashlib
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

# Ensure packages are importable
sys.path.insert(0, str(Path(__file__).parent.parent))
# data_models now lives inside config_seed/; add that to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "config_seed"))

# Session-scoped template database for fast test initialization
_template_db_dir = None
_template_db_lock = threading.Lock()

# Template DB retention policy (in seconds)
_TEMPLATE_DB_MAX_AGE = 86400  # 1 day


def _cleanup_old_template_dbs():
    """Remove old template databases from /dev/shm to prevent accumulation.

    Template DBs are created once per pytest session and persist after the
    session ends. Over time, they can fill up /dev/shm (tmpfs). This cleanup
    runs at session start and removes any template DBs older than 1 day.
    """
    tmpdir = Path(tempfile.gettempdir())
    if not tmpdir.exists():
        return

    now = time.time()
    deleted_count = 0
    reclaimed_mb = 0

    for template_dir in tmpdir.glob("carpenter_test_template_*"):
        if not template_dir.is_dir():
            continue

        try:
            # Check directory age via mtime
            dir_age = now - template_dir.stat().st_mtime
            if dir_age > _TEMPLATE_DB_MAX_AGE:
                # Calculate size before deletion
                dir_size = sum(
                    f.stat().st_size for f in template_dir.rglob("*") if f.is_file()
                )
                shutil.rmtree(template_dir)
                deleted_count += 1
                reclaimed_mb += dir_size / (1024 * 1024)
        except (OSError, PermissionError) as e:
            # Skip directories we can't access or delete
            pass

    if deleted_count > 0:
        print(
            f"  Cleaned up {deleted_count} old template DB(s), "
            f"reclaimed ~{reclaimed_mb:.1f} MB"
        )


@pytest.fixture(scope="session", autouse=True)
def _cleanup_old_templates():
    """Clean up old template DBs at session start (before any tests run)."""
    _cleanup_old_template_dbs()


def _schema_hash() -> str:
    """Short hash of schema.sql so template DB auto-invalidates on schema changes."""
    schema_path = Path(__file__).parent.parent / "carpenter" / "schema.sql"
    content = schema_path.read_bytes() if schema_path.exists() else b""
    return hashlib.sha256(content).hexdigest()[:12]


def _get_template_db():
    """Create or return the cached template database directory."""
    global _template_db_dir
    with _template_db_lock:
        if _template_db_dir is not None:
            return _template_db_dir

        # Use a local variable until initialization fully succeeds.
        # If anything below raises, _template_db_dir stays None so the next
        # call retries from scratch rather than returning a broken directory.
        temp_dir = tempfile.mkdtemp(prefix=f"carpenter_test_template_{_schema_hash()}_")
        db_path = str(Path(temp_dir) / "template.db")
        log_dir = str(Path(temp_dir) / "logs")
        code_dir = str(Path(temp_dir) / "code")
        workspaces_dir = str(Path(temp_dir) / "workspaces")
        data_models_dir = str(Path(temp_dir) / "data_models")

        # Create template config
        kb_dir = str(Path(temp_dir) / "kb")

        template_config = {
            "base_dir": temp_dir,
            "database_path": db_path,
            "log_dir": log_dir,
            "code_dir": code_dir,
            "workspaces_dir": workspaces_dir,
            "templates_dir": str(Path(temp_dir) / "templates"),
            "tools_dir": str(Path(temp_dir) / "tools"),
            "data_models_dir": data_models_dir,
            "kb": {
                "enabled": True,
                "dir": kb_dir,
                "max_entry_bytes": 6000,
                "search_backend": "embedding",
            },
            "host": "127.0.0.1",
            "port": 7842,
            "ui_token": "",
            "allow_insecure_bind": False,
            "executor_type": "restricted",
            "retry_max_attempts": 3,
            "retry_base_delay": 0.01,
            "circuit_breaker_threshold": 5,
            "circuit_breaker_recovery_seconds": 60,
            "executor_memory_limit_mb": 0,
            "compaction_threshold": 0.8,
            "compaction_threshold_tokens": 0,
            "compaction_preserve_recent": 8,
            "context_compaction_hours": 6,
            "workspace_retention_days": 14,
            "workspace_retention_count": 100,
            "arc_archive_days": 7,
            "mechanical_retry_max": 4,
            "agentic_iteration_budget": 10,
            "agentic_iteration_cap": 256,
            "heartbeat_seconds": 5,
            "max_concurrent_handlers": 4,
            "execution_session_expiry_hours": 1,
            "shutdown_timeout": 25,
            "executor_grace_seconds": 5,
            "sandbox": {"method": "none"},
            "connectors": {},
            "connector_retention_days": 7,
            "plugin_shared_base": str(Path(temp_dir) / "plugin_shared"),
            "plugins_config": str(Path(temp_dir) / "plugins.json"),
            "plugin_retention_days": 7,
            "models": {
                "opus": {
                    "provider": "anthropic",
                    "model_id": "claude-opus-4-20250514",
                    "description": "Most capable model.",
                    "cost_tier": "high",
                    "context_window": 200000,
                    "roles": ["planning", "review", "implementation"],
                },
                "sonnet": {
                    "provider": "anthropic",
                    "model_id": "claude-sonnet-4-20250514",
                    "description": "Balanced capability and cost.",
                    "cost_tier": "medium",
                    "context_window": 200000,
                    "roles": ["planning", "review", "implementation", "documentation"],
                },
                "haiku": {
                    "provider": "anthropic",
                    "model_id": "claude-haiku-4-5-20251001",
                    "description": "Fast and cheap.",
                    "cost_tier": "low",
                    "context_window": 200000,
                    "roles": ["summarization", "documentation"],
                },
            },
            "model_roles": {
                "default": "", "chat": "", "default_step": "",
                "title": "", "summary": "", "compaction": "",
                "code_review": "", "review_judge": "",
                "reflection_daily": "", "reflection_weekly": "", "reflection_monthly": "",
            },
            "memory_recent_hints": 3,
            "reflection": {
                "enabled": False,
                "min_daily_conversations": 1,
                "daily_cron": "0 23 * * *",
                "weekly_cron": "0 23 * * 0",
                "monthly_cron": "0 23 1 * *",
                "auto_action": False,
                "review_mode": "auto",
                "tainted_review_mode": "human",
                "max_actions_per_reflection": 10,
                "max_actions_per_day": 50,
            },
            "tool_output_max_bytes": 32768,
            "tool_output_head_lines": 50,
            "tool_output_tail_lines": 20,
            "notifications": {
                "email": {
                    "enabled": False,
                    "mode": "smtp",
                    "smtp_host": "",
                    "smtp_port": 587,
                    "smtp_from": "",
                    "smtp_to": "",
                    "smtp_username": "",
                    "smtp_password": "",
                    "smtp_tls": True,
                    "command": "",
                },
                "batch_window": 0,
                "routing": {},
            },
            "review": {
                "adversarial_mode": False,
                "adversarial_min_findings": 1,
            },
            "agent_roles": {
                "security-reviewer": {
                    "system_prompt": "You are a security reviewer.",
                    "auto_review_output_types": ["python"],
                    "temperature": 0.2,
                },
                "judge": {
                    "system_prompt": "You are the final judge.",
                    "auto_review_output_types": [],
                    "temperature": 0.1,
                },
            },
        }

        # Temporarily set config to initialize template DB.
        # The try/finally guarantees CONFIG is always restored even if
        # init_db() or the sentinel insert raises.
        import carpenter.config
        original_config = carpenter.config.CONFIG
        try:
            carpenter.config.CONFIG = template_config

            from carpenter.db import init_db, get_db
            init_db(skip_migrations=True)

            # Create sentinel arc in template
            conn = get_db()
            try:
                sentinel_exists = conn.execute(
                    "SELECT 1 FROM arcs WHERE id = 0"
                ).fetchone()
                if not sentinel_exists:
                    conn.execute(
                        "INSERT INTO arcs (id, name, goal, status) "
                        "VALUES (0, '_sentinel', 'Conversation-level state storage', 'completed')"
                    )
                    conn.commit()
            finally:
                conn.close()
        finally:
            carpenter.config.CONFIG = original_config

        # Install prompt and tool defaults once for the session.
        # Per-test fixtures symlink to these shared directories.
        from carpenter.prompts import install_prompt_defaults, install_coding_prompt_defaults
        from carpenter.tool_loader import install_coding_tool_defaults
        from carpenter.chat_tool_loader import install_chat_tool_defaults
        install_prompt_defaults(str(Path(temp_dir) / "prompts"))
        install_coding_prompt_defaults(str(Path(temp_dir) / "coding-prompts"))
        install_coding_tool_defaults(str(Path(temp_dir) / "coding-tools"))
        install_chat_tool_defaults(str(Path(temp_dir) / "chat_tools"))

        # Only promote to global after full successful initialization.
        _template_db_dir = temp_dir

    return _template_db_dir


@pytest.fixture(autouse=True)
def test_db(tmp_path, monkeypatch):
    """Set up a fresh test database for each test.

    Uses a cached template database for fast initialization (copy vs full init).
    Every test gets an isolated database.
    """
    # Get template database directory
    template_dir = _get_template_db()
    template_db_path = Path(template_dir) / "template.db"

    # Copy template database to test directory
    db_path = str(tmp_path / "test.db")
    shutil.copy2(template_db_path, db_path)

    # Create other directories
    log_dir = str(tmp_path / "logs")
    code_dir = str(tmp_path / "code")
    workspaces_dir = str(tmp_path / "workspaces")
    data_models_dir = str(tmp_path / "data_models")
    kb_dir = str(tmp_path / "kb")

    # Ensure directories exist (some tests expect this)
    Path(log_dir).mkdir(exist_ok=True)
    Path(code_dir).mkdir(exist_ok=True)
    Path(workspaces_dir).mkdir(exist_ok=True)
    Path(data_models_dir).mkdir(exist_ok=True)
    Path(kb_dir).mkdir(exist_ok=True)

    # Point to shared prompt/tool defaults from the template directory.
    # These are installed once per session (in _get_template_db) and reused
    # by all tests via symlinks to avoid per-test file copying overhead.
    template_dir_path = Path(template_dir)
    for src_name, dst_name in [
        ("prompts", "prompts"),
        ("coding-prompts", "coding-prompts"),
        ("coding-tools", "coding-tools"),
        ("chat_tools", "chat_tools"),
    ]:
        src = template_dir_path / src_name
        dst = tmp_path / dst_name
        if src.is_dir() and not dst.exists():
            dst.symlink_to(src)

    monkeypatch.setattr("carpenter.config.CONFIG", {
        "base_dir": str(tmp_path),
        "database_path": db_path,
        "log_dir": log_dir,
        "code_dir": code_dir,
        "workspaces_dir": workspaces_dir,
        "templates_dir": str(tmp_path / "templates"),
        "tools_dir": str(tmp_path / "tools"),
        "data_models_dir": data_models_dir,
        "kb": {
            "enabled": True,
            "dir": kb_dir,
            "max_entry_bytes": 6000,
            "search_backend": "embedding",
        },
        "host": "127.0.0.1",
        "port": 7842,
        "ui_token": "",
        "allow_insecure_bind": False,
        "executor_type": "restricted",
        "retry_max_attempts": 3,
        "retry_base_delay": 0.01,
        "circuit_breaker_threshold": 5,
        "circuit_breaker_recovery_seconds": 60,
        "executor_memory_limit_mb": 0,
        "compaction_threshold": 0.8,
        "compaction_threshold_tokens": 0,
        "compaction_preserve_recent": 8,
        "context_compaction_hours": 6,
        "workspace_retention_days": 14,
        "workspace_retention_count": 100,
        "arc_archive_days": 7,
        "mechanical_retry_max": 4,
        "agentic_iteration_budget": 10,
        "agentic_iteration_cap": 256,
        "heartbeat_seconds": 5,
        "max_concurrent_handlers": 4,
        "execution_session_expiry_hours": 1,
        "shutdown_timeout": 25,
        "executor_grace_seconds": 5,
        "sandbox": {"method": "none"},
        "connectors": {},
        "connector_retention_days": 7,
        "prompts_dir": str(tmp_path / "prompts"),
        "coding_prompts_dir": str(tmp_path / "coding-prompts"),
        "coding_tools_dir": str(tmp_path / "coding-tools"),
        "chat_tools_dir": str(tmp_path / "chat_tools"),
        "plugin_shared_base": str(tmp_path / "plugin_shared"),
        "plugins_config": str(tmp_path / "plugins.json"),
        "plugin_retention_days": 7,
        "models": {
            "opus": {
                "provider": "anthropic",
                "model_id": "claude-opus-4-20250514",
                "description": "Most capable model.",
                "cost_tier": "high",
                "context_window": 200000,
                "roles": ["planning", "review", "implementation"],
            },
            "sonnet": {
                "provider": "anthropic",
                "model_id": "claude-sonnet-4-20250514",
                "description": "Balanced capability and cost.",
                "cost_tier": "medium",
                "context_window": 200000,
                "roles": ["planning", "review", "implementation", "documentation"],
            },
            "haiku": {
                "provider": "anthropic",
                "model_id": "claude-haiku-4-5-20251001",
                "description": "Fast and cheap.",
                "cost_tier": "low",
                "context_window": 200000,
                "roles": ["summarization", "documentation"],
            },
        },
        "model_roles": {
            "default": "", "chat": "", "default_step": "",
            "title": "", "summary": "", "compaction": "",
            "code_review": "", "review_judge": "",
            "reflection_daily": "", "reflection_weekly": "", "reflection_monthly": "",
        },
        "memory_recent_hints": 3,
        "reflection": {
            "enabled": False,
            "min_daily_conversations": 1,
            "daily_cron": "0 23 * * *",
            "weekly_cron": "0 23 * * 0",
            "monthly_cron": "0 23 1 * *",
        },
        "tool_output_max_bytes": 32768,
        "tool_output_head_lines": 50,
        "tool_output_tail_lines": 20,
        "encryption": {
            "enforce": True,
        },
        "egress_policy": "none",
        "egress_enforce": False,
        "tls_enabled": False,
        "tls_cert_path": "",
        "tls_key_path": "",
        "tls_domain": "",
        "tls_ca_path": "",
        "notifications": {
            "email": {
                "enabled": False,
                "mode": "smtp",
                "smtp_host": "",
                "smtp_port": 587,
                "smtp_from": "",
                "smtp_to": "",
                "smtp_username": "",
                "smtp_password": "",
                "smtp_tls": True,
                "command": "",
            },
            "batch_window": 0,
            "routing": {},
        },
        "review": {
            "adversarial_mode": False,
            "adversarial_min_findings": 1,
        },
        "verification": {
            "enabled": False,  # Disabled by default in tests; enabled in tests/verify/
        },
        "agent_roles": {
            "security-reviewer": {
                "system_prompt": "You are a security reviewer.",
                "auto_review_output_types": ["python"],
                "temperature": 0.2,
            },
            "judge": {
                "system_prompt": "You are the final judge.",
                "auto_review_output_types": [],
                "temperature": 0.1,
            },
        },
    })

    return db_path


@pytest.fixture(autouse=True)
def _no_title_generation(monkeypatch):
    """Prevent background title generation threads from racing across tests.

    generate_title spawns a daemon thread that imports claude_client
    directly and can outlive individual test mocks, causing flaky failures
    when the thread writes to a conversation that a later test reuses.
    """
    monkeypatch.setattr(
        "carpenter.agent.conversation.generate_title",
        lambda conversation_id: None,
    )


@pytest.fixture(autouse=True)
def _no_summary_generation(monkeypatch):
    """Prevent background summary generation threads from racing across tests.

    generate_summary spawns a daemon thread at conversation boundaries
    that can outlive individual test mocks.
    """
    monkeypatch.setattr(
        "carpenter.agent.conversation.generate_summary",
        lambda conversation_id: None,
    )


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """Skip retry backoff delays during tests to speed up test suite.

    API retry logic uses exponential backoff (1s, 2s, 4s, 8s) which adds
    significant delay to tests that mock API failures. This fixture
    replaces time.sleep to skip delays > 1.5s while preserving sleeps
    needed for test coordination (<= 1.5s).

    The threshold of 1.5s allows:
    - Small coordination sleeps (0.05s, 0.1s, 0.2s)
    - The one 1s sleep for thread sync in test_poll_picks_up_trigger
    - Skips longer retry delays (2s, 4s, 8s)

    Saves ~15-30 seconds across the test suite.
    """
    import time
    original_sleep = time.sleep

    def fast_sleep(seconds):
        # Allow sleeps <= 1.5s, skip longer retry backoffs
        if seconds <= 1.5:
            original_sleep(seconds)
        # else: skip longer sleeps (2s, 4s, 8s retry delays)

    monkeypatch.setattr(time, "sleep", fast_sleep)


@pytest.fixture(autouse=True)
def _init_thread_pools():
    """Ensure thread pools are available for tests that call run_in_work_pool.

    The coordinator normally calls init_pools() at startup, but tests don't
    go through coordinator startup. This fixture initialises (and tears down)
    the pools for every test so that handlers exercised in tests don't crash
    with "Thread pools not initialised".
    """
    from carpenter import thread_pools
    thread_pools.init_pools()
    yield
    thread_pools.shutdown_pools()


@pytest.fixture(autouse=True)
def _reset_circuit_breakers():
    """Reset AI provider circuit breakers between tests.

    Circuit breakers are module-level singletons shared across all tests
    in the same worker process.  If a test makes an unexpected real API
    call (e.g. a missing mock), the failures are recorded in the breaker.
    Enough failures open the circuit and cause subsequent tests to get
    fast-fail errors unrelated to what they are testing.

    Resetting before (and after) every test keeps state isolated.
    """
    from carpenter.agent import circuit_breaker
    circuit_breaker.reset()
    yield
    circuit_breaker.reset()


@pytest.fixture(autouse=True)
def _reset_invocation_tracker():
    """Reset the channel invocation tracker between tests.

    InvocationTracker is a module-level singleton shared across tests.
    Clear it before and after each test to prevent cross-test leakage.
    """
    from carpenter.channels.channel import get_invocation_tracker
    tracker = get_invocation_tracker()
    tracker.clear()
    yield
    tracker.clear()


@pytest.fixture(autouse=True)
def _reset_review_cache():
    """Clear the review pipeline approval cache between tests.

    _approval_cache is a module-level dict keyed by conversation_id.
    Because test databases are isolated but worker processes are reused,
    the cache can accumulate entries from previous tests in the same
    worker.  A stale cache hit would skip the reviewer mock entirely,
    causing an apparent pass while hiding that the mock was never called.
    """
    from carpenter.review.pipeline import clear_cache
    clear_cache()
    yield
    clear_cache()


@pytest.fixture(autouse=True)
def _reset_kb_store():
    """Reset the KB store singleton between tests."""
    import carpenter.kb as kb_module
    kb_module._store = None
    yield
    kb_module._store = None


@pytest.fixture(autouse=True)
def _mock_local_embed(monkeypatch):
    """Provide deterministic fake embeddings for all tests.

    The EmbeddingBackend needs a model file (safetensors or ONNX) to
    produce real embeddings.  In tests, we use keyword-based fake
    embeddings so tests run without model files while still exercising
    the full embedding → cosine similarity pipeline.
    """
    import math as _math

    _dim = 384
    _keywords = [
        "schedule", "cron", "timer", "message", "chat", "email",
        "python", "code", "test", "greeting", "work", "reflection",
        "conversation", "daily", "review",
    ]

    def _fake_embed(texts):
        vectors = []
        for text in texts:
            low = text.lower()
            vec = [0.0] * _dim
            for i, kw in enumerate(_keywords):
                if kw in low:
                    vec[i] = 1.0
            # Hash each word into a vector position so unknown words
            # still produce unique, distinguishable embeddings.
            for word in low.split():
                if word not in _keywords:
                    idx = hash(word) % (_dim - len(_keywords) - 1) + len(_keywords)
                    vec[idx] += 1.0
            norm = _math.sqrt(sum(x * x for x in vec))
            if norm > 0:
                vec = [x / norm for x in vec]
            else:
                vec[0] = 1.0
            vectors.append(vec)
        return vectors

    monkeypatch.setattr("carpenter.kb.search._local_embed", _fake_embed)


@pytest.fixture(autouse=True)
def _load_chat_tools(test_db):
    """Load chat tools for each test so dispatch works.

    Uses the chat_tools_dir from CONFIG (set by test_db fixture).
    Resets module-level state after each test.
    """
    import carpenter.config
    import carpenter.chat_tool_loader as loader
    chat_tools_dir = carpenter.config.CONFIG.get("chat_tools_dir", "")
    if chat_tools_dir:
        try:
            loader.load_chat_tools(chat_tools_dir)
        except (RuntimeError, OSError):
            pass  # Some tests may not have chat_tools dir
    yield
    loader._loaded_tools = {}
    loader._mtimes = {}
    loader._chat_tools_dir = ""




@pytest.fixture(autouse=True)
def _inject_mock_platform():
    """Inject a mock platform for all tests.

    Since LinuxPlatform moved to carpenter-linux, core tests need a mock
    so that get_platform() doesn't raise RuntimeError.
    """
    import carpenter.platform as platform_mod

    class _MockPlatform:
        name = "mock"

        def restart_process(self):
            pass

        def protect_file(self, path):
            import os
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

        def generate_service(self, name, command, description, **kw):
            return None

        def install_service(self, name, service_content):
            return False

        def graceful_kill(self, proc, grace_seconds=5):
            pass

    old = platform_mod._instance
    platform_mod.set_platform(_MockPlatform())
    yield
    platform_mod._instance = old


@pytest.fixture
def db():
    """Get a database connection for direct access in tests."""
    from carpenter.db import get_db
    conn = get_db()
    yield conn
    conn.close()

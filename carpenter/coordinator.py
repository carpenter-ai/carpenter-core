"""Platform coordinator — owns the event loop, handlers, and lifecycle.

Independent of HTTP. The FastAPI server is one possible frontend;
channel connectors, embedded mode, and headless mode are others.
"""

import asyncio
import logging
import os
import sqlite3
import sys
from pathlib import Path

from . import config
from . import thread_pools
from .db import init_db
from .core.engine import main_loop
from .core.workflows import coding_change_handler

logger = logging.getLogger(__name__)

class Coordinator:
    """Platform coordinator — owns startup/shutdown lifecycle.

    Can be used standalone (no HTTP) or embedded in a FastAPI lifespan.
    """

    def __init__(self):
        self._loop_task: asyncio.Task | None = None
        self._shutdown_event: asyncio.Event | None = None

    # ── Startup phases ──────────────────────────────────────────────

    def _init_thread_pools(self) -> None:
        """Initialise dedicated thread pools before any blocking work."""
        thread_pools.init_pools()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(thread_pools.get_default_pool())

    def _init_database(self) -> None:
        """Run DB migrations and ensure schema is current."""
        init_db()

    def _load_workflow_templates(self) -> None:
        """Load workflow templates from YAML seed files."""
        from .core.engine import template_manager
        from pathlib import Path
        templates_dir = Path(__file__).parent.parent / "config_seed" / "templates"
        if templates_dir.exists():
            try:
                loaded_count = template_manager.load_templates_from_dir(str(templates_dir))
                logger.info("Loaded %d workflow template(s) from %s", loaded_count, templates_dir)
            except (OSError, ValueError, TypeError) as _exc:
                logger.exception("Failed to load workflow templates")
        else:
            logger.warning("Templates directory not found: %s", templates_dir)

    def _ensure_model_policy_presets(self) -> None:
        """Insert model policy presets into DB if they don't already exist."""
        from .core.models.selector import get_presets
        from .db import get_db, db_transaction
        with db_transaction() as db:
            try:
                for name, policy in get_presets().items():
                    # Insert if doesn't exist (ON CONFLICT DO NOTHING)
                    db.execute(
                        "INSERT OR IGNORE INTO model_policies (name, policy_json) VALUES (?, ?)",
                        (name, policy.to_policy_json())
                    )
                logger.info("Model policy presets ensured in database")
            except (sqlite3.Error, ValueError, TypeError) as _exc:
                logger.exception("Failed to ensure model policy presets")

    def _validate_tools(self) -> None:
        """Validate tool metadata and dispatch classification."""
        from .api.callbacks import validate_tool_classification
        validate_tool_classification()
        from carpenter_tools.tool_meta import validate_package
        import carpenter_tools.read as _read_pkg, carpenter_tools.act as _act_pkg
        _tool_errors = validate_package(_read_pkg, expected_safe=True) + \
            validate_package(_act_pkg, expected_safe=False)
        if _tool_errors:
            for _e in _tool_errors:
                logger.error("Tool validation: %s", _e)
            raise RuntimeError(
                f"Tool package validation failed: {_tool_errors}"
            )
        logger.info("Tool metadata and dispatch classification validated")

    def _load_chat_tools(self, base_dir: str) -> None:
        """Install defaults and load user-configurable chat tools."""
        if base_dir:
            chat_tools_dir = config.CONFIG.get("chat_tools_dir", "") or os.path.join(base_dir, "config", "chat_tools")
            from .chat_tool_loader import install_chat_tool_defaults, load_chat_tools, register_reload_hook
            chat_tool_result = install_chat_tool_defaults(chat_tools_dir)
            if chat_tool_result.get("status") == "installed":
                logger.info(
                    "Chat tool defaults installed: %d files", chat_tool_result["copied"],
                )
            load_chat_tools(chat_tools_dir)
            register_reload_hook(chat_tools_dir)
        logger.info("Chat tool trust boundaries validated")

    def _discover_capability_packages(self) -> None:
        """Discover and register Phase A capability packages.

        Runs after ``_load_chat_tools`` so that
        ``register_extension_tool`` has the loaded-tools dict to merge
        into.  Search paths come from
        ``config["capability_packages"]["search_paths"]`` when set,
        otherwise the conventional defaults (per D22 — zero-action
        discovery when ``carpenter-packages`` is cloned alongside
        ``carpenter-core``).

        Failures are logged and swallowed: a misbehaving package must
        not block the daemon from starting.
        """
        from pathlib import Path
        from .packages import discover_and_register

        cfg = config.CONFIG.get("capability_packages", {}) or {}
        configured = cfg.get("search_paths")
        search_paths: list[Path] | None = None
        if configured:
            if not isinstance(configured, list):
                logger.error(
                    "config['capability_packages']['search_paths'] must be "
                    "a list; falling back to defaults",
                )
            else:
                search_paths = [
                    Path(os.path.expanduser(str(p))) for p in configured
                ]

        try:
            from .db import db_transaction
            with db_transaction() as db:
                discover_and_register(
                    search_paths=search_paths, db_conn=db,
                )
        except Exception:
            logger.exception(
                "Capability package discovery failed; "
                "continuing without packages",
            )

    def _recover_review_links(self) -> None:
        """Recover review links from previous session.

        Runs the in-memory recovery from ``arc_state`` first, then a
        one-shot backfill that mints fresh arc-approval reviews for any
        stranded ``_review_mode='human'`` arcs missing recovery data.
        Both steps are idempotent.
        """
        from .api.review import (
            backfill_arc_approval_reviews,
            migrate_review_urls_to_absolute,
            recover_review_links,
        )
        recover_review_links()
        backfill_arc_approval_reviews()
        migrate_review_urls_to_absolute()

    def _register_work_handlers(self) -> None:
        """Register all work-queue event handlers and heartbeat hooks."""
        coding_change_handler.register_handlers(main_loop.register_handler)
        logger.info("Coding-change handlers registered")

        from .core.workflows import external_coding_change_handler
        external_coding_change_handler.register_handlers(main_loop.register_handler)
        logger.info("External coding-change handlers registered")

        from .core.workflows import platform_handler
        platform_handler.register_handlers(main_loop.register_handler)
        logger.info("Platform handler registered")

        from .core.arcs import child_failure_handler
        child_failure_handler.register_handlers(main_loop.register_handler)
        logger.info("Child failure handler registered")

        from .core.arcs import supervisor_wake_handler
        supervisor_wake_handler.register_handlers(main_loop.register_handler)
        logger.info("Supervisor wake handler registered")

        from .core.arcs import dispatch_handler as arc_dispatch_handler
        arc_dispatch_handler.register_handlers(main_loop.register_handler)

        # PR 7 close-out: deterministic Python-only verification steps for
        # the yaml-change / kb-change workflows.  These swap the LLM
        # REVIEWER correctness arc in ``create_verification_arcs`` for a
        # Python step, which dispatch_handler routes here via the
        # (template_name, step_name/step_role) lookup.  Register both the
        # step_name and step_role keys so dispatch's role-first lookup
        # also resolves.
        from .core.engine import handler_registry as _handler_registry
        from .core.workflows.yaml_lint_handler import handle_lint_yaml_step
        from .core.workflows.kb_format_handler import handle_verify_kb_format_step
        _handler_registry.register_step_handler(
            "yaml-change", "lint-yaml", handle_lint_yaml_step,
        )
        _handler_registry.register_step_handler(
            "yaml-change", "verifier-lint-yaml", handle_lint_yaml_step,
        )
        _handler_registry.register_step_handler(
            "kb-change", "verify-kb-format", handle_verify_kb_format_step,
        )
        _handler_registry.register_step_handler(
            "kb-change", "verifier-kb-format", handle_verify_kb_format_step,
        )
        logger.info(
            "Deterministic verification step handlers registered "
            "(yaml-change.lint-yaml, kb-change.verify-kb-format)"
        )

        from .core.models import monitor as health_monitor
        main_loop.register_heartbeat_hook(health_monitor.check_health)
        logger.info("Health monitor heartbeat hook registered")

        from .core.models.health import cleanup_old_calls
        _last_model_calls_cleanup = [0.0]  # mutable container for closure

        def _model_calls_cleanup_hook():
            import time
            now = time.time()
            if now - _last_model_calls_cleanup[0] < 86400:  # once per day
                return
            _last_model_calls_cleanup[0] = now
            try:
                deleted = cleanup_old_calls(days=7)
                if deleted:
                    logger.info("Model calls cleanup: removed %d old records", deleted)
            except sqlite3.Error as _exc:
                logger.debug("Model calls cleanup failed", exc_info=True)

        main_loop.register_heartbeat_hook(_model_calls_cleanup_hook)
        logger.info("Model calls cleanup hook registered")

        from .core.workflows import webhook_dispatch_handler
        webhook_dispatch_handler.register_handlers(main_loop.register_handler)
        logger.info("Webhook dispatch handler registered")
        logger.info("Arc dispatch handler registered")

        from .core.workflows import pr_review_handler
        pr_review_handler.register_handlers(main_loop.register_handler)
        logger.info("PR review handler registered")

        from .core.workflows import merge_handler
        merge_handler.register_handlers(main_loop.register_handler)
        logger.info("Merge resolution handler registered")

        from .core.workflows import arc_notify_handler
        arc_notify_handler.register_handlers(main_loop.register_handler)
        logger.info("Arc chat notification handler registered")

    async def _init_trigger_subscription_pipeline(self, base_dir: str, app) -> None:
        """Set up triggers, subscriptions, and endpoint routes."""
        from .core.engine.triggers import registry as trigger_registry
        from .core.engine.triggers.timer import TimerTrigger
        from .core.engine.triggers.counter import CounterTrigger
        from .core.engine.triggers.webhook import WebhookTrigger
        from .core.engine import subscriptions

        # Register built-in trigger types
        trigger_registry.register_trigger_type(TimerTrigger)
        trigger_registry.register_trigger_type(CounterTrigger)
        trigger_registry.register_trigger_type(WebhookTrigger)

        # Load user-defined trigger plugins
        trigger_plugins_dir = config.CONFIG.get("trigger_plugins_dir", "")
        if trigger_plugins_dir and base_dir:
            import os as _os
            if not _os.path.isabs(trigger_plugins_dir):
                trigger_plugins_dir = _os.path.join(base_dir, trigger_plugins_dir)
            trigger_registry.load_user_triggers(trigger_plugins_dir)

        # Config-driven triggers (no feature-specific wiring here —
        # template packages declare their own triggers via the
        # ``triggers:`` section, loaded below by
        # ``load_template_triggers``).
        import copy
        trigger_configs = copy.deepcopy(config.CONFIG.get("triggers", []))

        # Load triggers from config
        if trigger_configs:
            trigger_registry.load_triggers(trigger_configs)
            trigger_registry.start_all()
            logger.info(
                "Trigger pipeline: %d trigger(s) loaded (%d pollable, %d endpoint)",
                len(trigger_registry.get_trigger_instances()),
                len(trigger_registry.get_pollable_triggers()),
                len(trigger_registry.get_endpoint_triggers()),
            )

        # Load built-in subscriptions (timer forwarding, webhook dispatch, etc.)
        subscriptions.load_builtin_subscriptions()

        # Register the weekly Resource sweep.  Seeds a cron entry that
        # emits ``resources.sweep`` via the generic timer.fired pipeline,
        # plus a work-item handler that runs :func:`run_sweep`.
        from .core.resources import sweep as _resource_sweep
        _resource_sweep.register_weekly_sweep(main_loop.register_handler)

        # Load subscriptions from config
        sub_configs = config.CONFIG.get("subscriptions", [])
        if sub_configs:
            subscriptions.load_subscriptions(sub_configs)

        # Load subscriptions declared by loaded templates' `triggers:` sections.
        from .core.engine import template_manager as _tmpl_mgr
        tmpl_sub_count = _tmpl_mgr.load_template_triggers()
        if tmpl_sub_count:
            logger.info(
                "Loaded %d subscription(s) from template triggers", tmpl_sub_count,
            )

        # B-full (D24): re-register trigger subscriptions that installed
        # capability packages contributed at install time.  The on-disk
        # ``_subscriptions.json`` files under each install dir are the
        # source of truth — install-time in-memory registrations don't
        # survive a restart, but the JSON record does.
        try:
            from .packages.installer import list_install_records as _list
            from .db import db_connection as _db_connection
            import json as _json
            with _db_connection() as _db:
                pkg_records = _list(_db)
            sub_records: list[tuple[str, list[dict]]] = []
            for rec in pkg_records:
                ip = Path(rec["install_path"]) / "_subscriptions.json"
                if not ip.is_file():
                    continue
                try:
                    entries = _json.loads(ip.read_text())
                except (ValueError, OSError):
                    logger.warning(
                        "Could not read package subscriptions for %r at %s",
                        rec["name"], ip,
                    )
                    continue
                if isinstance(entries, list):
                    sub_records.append((rec["name"], entries))
            if sub_records:
                subscriptions.load_package_subscriptions(sub_records)
        except Exception:  # noqa: BLE001 — best-effort startup wiring
            logger.exception(
                "Failed to load capability-package subscriptions at startup",
            )

        total_subs = len(subscriptions.get_subscriptions())
        if total_subs:
            logger.info(
                "Subscription pipeline: %d subscription(s) loaded",
                total_subs,
            )

        # Register endpoint triggers with the HTTP app
        endpoint_triggers = trigger_registry.get_endpoint_triggers()
        if endpoint_triggers and app is not None:
            from starlette.requests import Request
            from starlette.responses import JSONResponse
            from starlette.routing import Route

            trigger_routes = []
            for trigger in endpoint_triggers:
                async def _make_handler(t):
                    async def _handler(request: Request):
                        result = await t.handle_request(request)
                        return JSONResponse(content=result)
                    return _handler

                handler = await _make_handler(trigger)
                trigger_routes.append(
                    Route(trigger.path, handler, methods=["POST"])
                )

            if trigger_routes:
                app.routes.extend(trigger_routes)
                logger.info("Registered %d endpoint trigger route(s)", len(trigger_routes))

        # Register subscription action handlers
        async def _handle_subscription_notification(work_id, payload):
            """Handle notification actions from subscription processing."""
            from .core import notifications
            message = payload.get("message", "Subscription notification")
            priority = payload.get("priority", "normal")
            category = payload.get("category", "subscription")
            notifications.notify(message, priority=priority, category=category)

        main_loop.register_handler(
            "subscription.notification", _handle_subscription_notification,
        )

        async def _handle_subscription_create_arc(work_id, payload):
            """Create an arc (optionally from a template) for a subscription."""
            from .core.engine import subscriptions as _subs
            _subs.handle_subscription_create_arc(payload)

        main_loop.register_handler(
            "subscription.create_arc", _handle_subscription_create_arc,
        )

        # B-full (D24): package_dispatch action enqueues ``package.dispatch``
        # work items that route to the package's manifest-declared handler.
        from .packages.subscription_handler import dispatch_package_handler
        main_loop.register_handler(
            "package.dispatch", dispatch_package_handler,
        )
        logger.info("Trigger and subscription pipeline initialized")

    def _install_config_seeds(self, base_dir: str) -> dict:
        """Install all config_seed/ targets via the unified seed installer.

        Replaces what used to be three separate per-target installer calls
        (prompts, coding-prompts, data_models, kb). Honors the existing
        config overrides so custom paths still work.

        Returns the per-target result dict so other init steps (e.g. KB)
        can re-read the outcome without re-invoking the installers.
        """
        if not base_dir:
            return {}

        from .seed import install_config_seed, SEED_MANIFEST

        # Honor explicit overrides from config for backward compat.
        overrides: dict[str, Path] = {}
        for entry in SEED_MANIFEST:
            cfg_key = {
                "prompts": "prompts_dir",
                "coding-prompts": "coding_prompts_dir",
                "kb": None,  # kb dir is resolved later in _init_knowledge_base
                "data_models": "data_models_dir",
            }.get(entry.name)
            if cfg_key:
                override = config.CONFIG.get(cfg_key, "")
                if override:
                    overrides[entry.name] = Path(override)

        # KB dir override from kb config (parity with prior _init_knowledge_base).
        kb_cfg = config.CONFIG.get("kb", {}) or {}
        kb_dir_override = kb_cfg.get("dir", "")
        if kb_dir_override:
            overrides["kb"] = Path(kb_dir_override)

        results = install_config_seed(base_dir, overrides=overrides)

        self._install_data_models_syspath()

        # Preserve the prior per-target log messages.
        log_labels = {
            "prompts": "Prompt defaults installed: %d files",
            "coding-prompts": "Coding prompt defaults installed: %d files",
            "data_models": "Data model defaults installed: %d files",
            "kb": "KB seed installed: %d entries",
        }
        for name, result in results.items():
            if result.get("status") == "installed":
                logger.info(log_labels.get(name, name + ": %d files"), result["copied"])

        return results

    def _install_data_models_syspath(self) -> None:
        # data_models is a top-level package on disk; its parent dir must be on
        # sys.path so handler code can `from data_models.X import Y` directly,
        # not just via the lazy path in verify/_schema._load_model_class.
        data_models_dir = config.CONFIG.get("data_models_dir", "")
        if not data_models_dir:
            return
        parent = os.path.dirname(data_models_dir.rstrip("/"))
        if parent and parent not in sys.path:
            sys.path.insert(0, parent)

    def _install_coding_tool_defaults(self, base_dir: str) -> None:
        """Install coding tool defaults (not part of config_seed/ manifest).

        coding-tools is sourced from a separate seed location and has its
        own installer; it stays outside the unified install_config_seed()
        flow so we don't accidentally change its on-disk layout here.
        """
        if not base_dir:
            return
        coding_tools_dir = config.CONFIG.get("coding_tools_dir", "") or os.path.join(base_dir, "config", "coding-tools")
        from .tool_loader import install_coding_tool_defaults
        coding_tool_result = install_coding_tool_defaults(coding_tools_dir)
        if coding_tool_result.get("status") == "installed":
            logger.info(
                "Coding tool defaults installed: %d files", coding_tool_result["copied"],
            )

    def _init_knowledge_base(self, base_dir: str) -> None:
        """Initialize Knowledge Base: seed, sync, autogen, handlers, backfill."""
        kb_config = config.CONFIG.get("kb", {})
        if not kb_config.get("enabled", True):
            return

        # Phase 2 PR-1: warm up the process-wide embedding service so the
        # first user query doesn't pay the ~1.5–3s ONNX session load cost.
        # Failures are non-fatal — the first real query will retry.
        try:
            from .embeddings import get_embedding_service
            get_embedding_service().warm_up()
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "Embedding service warm-up raised unexpectedly; "
                "daemon will continue, first query may be slow",
                exc_info=True,
            )

        from .kb import get_store
        kb_dir = kb_config.get("dir", "")
        if not kb_dir:
            kb_dir = os.path.join(base_dir, "config", "kb") if base_dir else ""
        if not kb_dir:
            return

        # KB seed install is now handled up front by _install_config_seeds().
        store = get_store(kb_dir)
        sync_result = store.sync_from_filesystem()
        if sync_result["added"] or sync_result["updated"]:
            logger.info(
                "KB sync: %d added, %d updated",
                sync_result["added"], sync_result["updated"],
            )
        # Auto-generate tool/config/template reference entries
        from .kb.autogen import run_autogen, register_change_hook
        autogen_result = run_autogen(store)
        if autogen_result["generated"]:
            logger.info(
                "KB autogen: %d entries generated",
                autogen_result["generated"],
            )
        # Register heartbeat hook for file change detection
        register_change_hook()

        # Register work history handler
        kb_work_config = kb_config.get("work_history_enabled", True)
        if kb_work_config:
            async def _handle_work_summary(work_id, payload):
                from .kb.work_history import should_summarize, create_work_entry
                from .kb import get_store
                arc_id = payload["arc_id"]
                if should_summarize(arc_id):
                    create_work_entry(arc_id, get_store())

            main_loop.register_handler(
                "kb.work_summary", _handle_work_summary,
            )
            logger.info("KB work history handler registered")

        # Register conversation summary -> KB handler
        async def _handle_conversation_summary(work_id, payload):
            from .kb.conversation_kb import create_conversation_entry
            from .kb import get_store
            create_conversation_entry(payload["conversation_id"], get_store())

        main_loop.register_handler(
            "kb.conversation_summary", _handle_conversation_summary,
        )

        # Register generic KB write handler. Feature packages construct
        # the fully-formatted entry (path, frontmatter+body content,
        # description, entry_type) and enqueue a ``kb.write_entry`` work
        # item; this handler performs the store write with no knowledge
        # of the feature that produced the entry.
        async def _handle_kb_write_entry(work_id, payload):
            from .kb import get_store
            path = payload.get("kb_path") or payload.get("path")
            content = payload.get("content")
            if not path or not content:
                logger.warning(
                    "kb.write_entry work %s: missing required "
                    "fields (kb_path, content); skipping",
                    work_id,
                )
                return
            # Content-hash dedupe is now enforced inside KBStore.write_entry
            # itself — a byte-identical rewrite short-circuits with an
            # "unchanged" success and no I/O. Every KB writer benefits
            # (chat kb.edit, package installers, reflection dispatch),
            # not just this coordinator handler.
            store = get_store()
            store.write_entry(
                path=path,
                content=content,
                description=payload.get("description") or "",
                entry_type=payload.get("entry_type", "knowledge"),
                trust_level=payload.get("trust_level", "trusted"),
                validate_links=payload.get("validate_links", False),
            )

        main_loop.register_handler(
            "kb.write_entry", _handle_kb_write_entry,
        )
        logger.info("KB conversation/write-entry handlers registered")

        # One-time backfill of existing conversations.
        from .kb.conversation_kb import backfill_conversations
        conv_count = backfill_conversations(store)
        if conv_count:
            logger.info("KB backfill: %d conversations", conv_count)

    def _register_cron_message_handler(self) -> None:
        """Register cron.message handler for recurring message delivery."""
        async def _handle_cron_message(work_id, payload):
            from .tool_backends.messaging import handle_send
            # Cron wraps event_payload inside metadata; unwrap it
            inner = payload.get("event_payload") or payload
            # Normalize: accept both "content" and "message" keys for
            # the message text (agents sometimes use "content" instead).
            if "message" not in inner and "content" in inner:
                inner = dict(inner)
                inner["message"] = inner.pop("content")
            handle_send(inner)

        main_loop.register_handler("cron.message", _handle_cron_message)

    async def _init_connector_registry(self, app) -> None:
        """Initialize channel connector registry (registers heartbeat hooks)."""
        from .channels.registry import initialize_connector_registry
        await initialize_connector_registry(app=app)

    def _start_main_loop(self) -> None:
        """Create the shutdown event and launch the main event loop task."""
        self._shutdown_event = asyncio.Event()
        self._loop_task = asyncio.create_task(
            main_loop.run_loop(shutdown_event=self._shutdown_event)
        )

    # ── Public API ──────────────────────────────────────────────────

    async def start(self, *, app=None) -> None:
        """Initialize DB, validate tools, register handlers, start main loop.

        Args:
            app: Optional FastAPI app instance for mounting webhook routers.
                 Pass None for headless/embedded mode.
        """
        base_dir = config.CONFIG.get("base_dir", "")

        self._init_thread_pools()
        self._init_database()
        self._load_workflow_templates()
        self._ensure_model_policy_presets()
        self._validate_tools()
        self._load_chat_tools(base_dir)
        self._discover_capability_packages()
        self._recover_review_links()

        logger.info("Coordinator started")

        self._register_work_handlers()
        await self._init_trigger_subscription_pipeline(base_dir, app)
        self._install_config_seeds(base_dir)
        self._install_coding_tool_defaults(base_dir)
        self._init_knowledge_base(base_dir)
        self._register_cron_message_handler()
        await self._init_connector_registry(app)
        self._start_main_loop()

    async def submit_chat(self, text: str, *,
                          conversation_id: int | None = None,
                          user: str = "default",
                          channel_type: str = "embedded") -> dict:
        """Submit a chat message without HTTP.

        Uses the unified ChannelConnector inbound path: identity resolution,
        conversation management, message persistence, and async AI invocation.

        The AI response is persisted to the conversation in the DB. Callers
        can poll ``conversation.get_messages(conversation_id)`` for the result
        or await the invocation tracker.

        Args:
            text: User message text.
            conversation_id: Explicit conversation ID. None = auto-resolve
                from channel binding or create new.
            user: User identity string for channel_bindings.
            channel_type: Channel type label (default "embedded").

        Returns:
            Dict with ``conversation_id``.
        """
        from .channels.channel import ChannelConnector

        class _EmbeddedConnector(ChannelConnector):
            channel_type_val = channel_type

            def __init__(self):
                self.name = "embedded"
                self.enabled = True
                # Set channel_type on the instance
                self.channel_type = self.channel_type_val

            async def start(self, config): pass
            async def stop(self): pass
            async def health_check(self):
                from .channels.base import HealthStatus
                return HealthStatus(healthy=True, detail="embedded")
            async def send_message(self, conv_id, text, metadata=None):
                return True  # no-op — caller retrieves response from DB

        connector = _EmbeddedConnector()
        conv_id = await connector.deliver_inbound(
            channel_user_id=user,
            text=text,
            conversation_id=conversation_id,
        )
        return {"conversation_id": conv_id}

    async def stop(self) -> None:
        """Drain work, stop executors, flush notifications."""
        from .agent.coding_agent import _shutdown as coding_shutdown
        from .agent.rate_limiter import shutdown as rl_shutdown
        from .executor import process_registry
        from .core import notifications

        shutdown_timeout = config.CONFIG.get("shutdown_timeout", 25)

        # 1. Stop accepting new work
        coding_shutdown.set()
        rl_shutdown()

        # 2. Stop main loop from claiming new work items
        if self._shutdown_event:
            self._shutdown_event.set()
        main_loop.wake_signal.set()

        # 3. SIGTERM all running executor processes (non-blocking)
        proc_count = process_registry.count()
        if proc_count:
            logger.info("Sending SIGTERM to %d executor process(es)", proc_count)
            process_registry.signal_all()

        # 4. Wait for main loop to drain
        if self._loop_task:
            try:
                await asyncio.wait_for(self._loop_task, timeout=shutdown_timeout)
            except asyncio.TimeoutError:
                logger.warning("Main loop drain timed out, force-killing executor processes")
                process_registry.kill_all()
                self._loop_task.cancel()
                try:
                    await self._loop_task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass

        # 5. Flush any batched notifications
        notifications.flush_now()

        # 6. Shut down thread pools
        thread_pools.shutdown_pools()

        logger.info("Coordinator shutting down")

        # 7. If a graceful restart was requested, replace the process now
        if main_loop._restart_pending and main_loop._restart_mode == "graceful":
            main_loop._do_restart()

"""Platform coordinator — owns the event loop, handlers, and lifecycle.

Independent of HTTP. The FastAPI server is one possible frontend;
channel connectors, embedded mode, and headless mode are others.
"""

import asyncio
import logging
import os
import sqlite3

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

    def _start_local_inference_server(self) -> None:
        """Start local inference server if ai_provider is 'local'."""
        if config.CONFIG.get("ai_provider") == "local":
            try:
                from .inference import get_inference_server
                server = get_inference_server()
                if server.start():
                    logger.info("Local inference server started")
                else:
                    logger.error(
                        "Local inference server failed to start — "
                        "circuit breaker will handle connection failures"
                    )
            except (ImportError, OSError, RuntimeError) as _exc:
                logger.exception("Failed to start local inference server")

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

    def _recover_review_links(self) -> None:
        """Recover review links from previous session."""
        from .api.review import recover_review_links
        recover_review_links()

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

        from .core.arcs import dispatch_handler as arc_dispatch_handler
        arc_dispatch_handler.register_handlers(main_loop.register_handler)

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

        # Skill-KB review handler has no custom event types (uses arc.dispatch
        # intercepts), but import to validate the module loads cleanly.
        from .core.workflows import skill_kb_review_handler  # noqa: F401
        logger.info("Skill-KB review handler loaded")

    async def _init_trigger_subscription_pipeline(self, base_dir: str, app) -> dict:
        """Set up triggers, subscriptions, and endpoint routes.

        Returns:
            The reflection_config dict (needed by later phases).
        """
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

        # Activate reflection triggers when reflection.enabled is True.
        # Deep-copy to avoid mutating config.DEFAULTS (shallow dict copy
        # in CONFIG shares list/dict references with DEFAULTS).
        import copy
        trigger_configs = copy.deepcopy(config.CONFIG.get("triggers", []))
        reflection_config = config.CONFIG.get("reflection", {})
        if reflection_config.get("enabled", False):
            _REFLECTION_CRON_MAP = {
                "daily-reflection": "daily_cron",
                "weekly-reflection": "weekly_cron",
                "monthly-reflection": "monthly_cron",
            }
            for tcfg in trigger_configs:
                tname = tcfg.get("name", "")
                if tname in _REFLECTION_CRON_MAP:
                    tcfg["enabled"] = True
                    # Apply per-cadence cron overrides from reflection config
                    cron_key = _REFLECTION_CRON_MAP[tname]
                    override = reflection_config.get(cron_key)
                    if override:
                        tcfg["schedule"] = override
            logger.info("Reflection triggers activated via reflection.enabled")

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

        # Load subscriptions from config
        sub_configs = config.CONFIG.get("subscriptions", [])
        if sub_configs:
            subscriptions.load_subscriptions(sub_configs)

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
        logger.info("Trigger and subscription pipeline initialized")

        return reflection_config

    def _register_reflection_handler(self, reflection_config: dict) -> None:
        """Register reflection template handler if reflections are enabled."""
        if reflection_config.get("enabled", False):
            from .core.workflows import reflection_template_handler
            reflection_template_handler.register_handlers(main_loop.register_handler)
            logger.info("Reflection template handler registered")

    def _install_prompt_and_tool_defaults(self, base_dir: str) -> None:
        """Install prompt templates, coding prompts, and coding tool defaults."""
        if not base_dir:
            return

        # Install prompt template defaults
        prompts_dir = config.CONFIG.get("prompts_dir", "") or os.path.join(base_dir, "config", "prompts")
        from .prompts import install_prompt_defaults
        prompt_result = install_prompt_defaults(prompts_dir)
        if prompt_result.get("status") == "installed":
            logger.info(
                "Prompt defaults installed: %d files", prompt_result["copied"],
            )

        # Install coding agent prompt defaults
        coding_prompts_dir = config.CONFIG.get("coding_prompts_dir", "") or os.path.join(base_dir, "config", "coding-prompts")
        from .prompts import install_coding_prompt_defaults
        coding_prompt_result = install_coding_prompt_defaults(coding_prompts_dir)
        if coding_prompt_result.get("status") == "installed":
            logger.info(
                "Coding prompt defaults installed: %d files", coding_prompt_result["copied"],
            )

        # Install coding tool defaults
        coding_tools_dir = config.CONFIG.get("coding_tools_dir", "") or os.path.join(base_dir, "config", "coding-tools")
        from .tool_loader import install_coding_tool_defaults
        coding_tool_result = install_coding_tool_defaults(coding_tools_dir)
        if coding_tool_result.get("status") == "installed":
            logger.info(
                "Coding tool defaults installed: %d files", coding_tool_result["copied"],
            )

    def _install_data_model_defaults(self, base_dir: str) -> None:
        """Install data_models seed files."""
        if not base_dir:
            return
        data_models_dir = config.CONFIG.get("data_models_dir", "") or os.path.join(base_dir, "config", "data_models")
        from .db import install_data_models_defaults
        dm_result = install_data_models_defaults(data_models_dir)
        if dm_result.get("status") == "installed":
            logger.info(
                "Data model defaults installed: %d files", dm_result["copied"],
            )

    def _init_knowledge_base(self, base_dir: str) -> None:
        """Initialize Knowledge Base: seed, sync, autogen, handlers, backfill."""
        kb_config = config.CONFIG.get("kb", {})
        if not kb_config.get("enabled", True):
            return

        from .kb import install_seed, get_store
        kb_dir = kb_config.get("dir", "")
        if not kb_dir:
            kb_dir = os.path.join(base_dir, "config", "kb") if base_dir else ""
        if not kb_dir:
            return

        seed_result = install_seed(kb_dir)
        if seed_result.get("status") == "installed":
            logger.info(
                "KB seed installed: %d entries", seed_result["copied"],
            )
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

        # Register reflection -> KB handler
        async def _handle_reflection_summary(work_id, payload):
            from .kb.reflection_kb import create_reflection_entry
            from .kb import get_store
            create_reflection_entry(payload["reflection_id"], get_store())

        main_loop.register_handler(
            "kb.reflection_summary", _handle_reflection_summary,
        )
        logger.info("KB conversation/reflection handlers registered")

        # One-time backfill of existing conversations and reflections
        from .kb.conversation_kb import backfill_conversations
        from .kb.reflection_kb import backfill_reflections
        conv_count = backfill_conversations(store)
        refl_count = backfill_reflections(store)
        if conv_count or refl_count:
            logger.info(
                "KB backfill: %d conversations, %d reflections",
                conv_count, refl_count,
            )

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
        self._start_local_inference_server()
        self._validate_tools()
        self._load_chat_tools(base_dir)
        self._recover_review_links()

        logger.info("Coordinator started")

        self._register_work_handlers()
        reflection_config = await self._init_trigger_subscription_pipeline(base_dir, app)
        self._register_reflection_handler(reflection_config)
        self._install_prompt_and_tool_defaults(base_dir)
        self._install_data_model_defaults(base_dir)
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

        # 0. Stop local inference server if running
        if config.CONFIG.get("ai_provider") == "local":
            try:
                from .inference import get_inference_server
                server = get_inference_server()
                if server.running:
                    server.stop()
                    logger.info("Local inference server stopped")
            except (ImportError, OSError, RuntimeError) as _exc:
                logger.exception("Error stopping local inference server")

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

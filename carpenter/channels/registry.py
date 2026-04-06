"""Connector registry — manages all connector instances.

Provides a unified registry for tool and channel connectors with
lifecycle management, health checking, and config change detection.
"""

import logging
from contextlib import asynccontextmanager
from typing import Callable, Optional

from .. import config
from .base import Connector

logger = logging.getLogger(__name__)


# Factory registry: (kind, transport) -> constructor
# Each factory takes (name, connector_config) and returns a Connector
_CONNECTOR_FACTORIES: dict[tuple[str, str], Callable[[str, dict], Connector]] = {}


def register_factory(kind: str, transport: str, factory: Callable[[str, dict], Connector]) -> None:
    """Register a connector factory for a (kind, transport) pair."""
    _CONNECTOR_FACTORIES[(kind, transport)] = factory


def _register_builtin_factories():
    """Register the built-in connector factories."""
    from .tool_connector import FileWatchToolConnector
    register_factory("tool", "file_watch", FileWatchToolConnector)

    # Channel connector factories use lazy wrapper to avoid import failures
    # when the module hasn't been created yet (supports incremental development)
    def _telegram_factory(name, config):
        from .telegram_channel import TelegramChannelConnector
        return TelegramChannelConnector(name, config)

    def _signal_factory(name, config):
        from .signal_channel import SignalChannelConnector
        return SignalChannelConnector(name, config)

    register_factory("channel", "telegram", _telegram_factory)
    register_factory("channel", "signal", _signal_factory)


# Register on import
_register_builtin_factories()


class ConnectorRegistry:
    """Central registry for all connectors.

    Manages lifecycle (start/stop), health checks, and config change
    detection for all registered connectors.
    """

    def __init__(self, connectors_config: dict):
        self._config_snapshot = dict(connectors_config)
        self.connectors: dict[str, Connector] = {}
        self._build_connectors(connectors_config)

    def _build_connectors(self, connectors_config: dict) -> None:
        """Build connector instances from config via factory pattern."""
        for name, cc in connectors_config.items():
            kind = cc.get("kind", "tool")
            transport = cc.get("transport", "file_watch")

            factory = _CONNECTOR_FACTORIES.get((kind, transport))
            if factory is None:
                logger.error(
                    "No factory for connector %s (kind=%s, transport=%s)",
                    name, kind, transport,
                )
                continue

            try:
                connector = factory(name, cc)
                if connector.enabled:
                    self.connectors[name] = connector
                    logger.info("Built connector: %s (kind=%s)", name, kind)
                else:
                    logger.debug("Skipping disabled connector: %s", name)
            except Exception:
                logger.exception("Failed to build connector: %s", name)

    def get(self, name: str) -> Optional[Connector]:
        """Get a connector by name, or None if not found/disabled."""
        return self.connectors.get(name)

    def list_connectors(self, kind: str | None = None) -> list[Connector]:
        """List all enabled connectors, optionally filtered by kind."""
        if kind is None:
            return list(self.connectors.values())
        return [c for c in self.connectors.values() if c.kind == kind]

    async def start_all(self) -> None:
        """Start all enabled connectors."""
        cfg = config.CONFIG
        for name, connector in self.connectors.items():
            try:
                await connector.start(cfg)
                logger.info("Started connector: %s", name)
            except Exception:
                logger.exception("Failed to start connector: %s", name)

    async def stop_all(self) -> None:
        """Stop all connectors."""
        for name, connector in self.connectors.items():
            try:
                await connector.stop()
                logger.debug("Stopped connector: %s", name)
            except Exception:
                logger.exception("Failed to stop connector: %s", name)

    def run_health_checks(self) -> dict[str, dict]:
        """Run health checks on all connectors (sync, for heartbeat)."""
        import asyncio
        results = {}
        for name, connector in self.connectors.items():
            try:
                try:
                    asyncio.get_running_loop()
                    loop_running = True
                except RuntimeError:
                    loop_running = False

                if loop_running:
                    # We're in the heartbeat — use a future
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        status = pool.submit(
                            asyncio.run, connector.health_check()
                        ).result(timeout=5)
                else:
                    status = asyncio.run(connector.health_check())
                results[name] = {"healthy": status.healthy, "detail": status.detail}
            except Exception as e:
                results[name] = {"healthy": False, "detail": str(e)}
        return results

    def check_for_config_changes(self) -> None:
        """Check if connector config has changed and rebuild if needed.

        Called from the main loop heartbeat (every ~5 seconds).
        """
        current = config.CONFIG.get("connectors", {})
        if current != self._config_snapshot:
            logger.info("Connector config changed, rebuilding")
            old_names = set(self.connectors.keys())
            self._config_snapshot = dict(current)
            self.connectors = {}
            self._build_connectors(current)
            new_names = set(self.connectors.keys())

            added = new_names - old_names
            removed = old_names - new_names
            if added:
                logger.info("Added connectors: %s", ", ".join(added))
            if removed:
                logger.info("Removed connectors: %s", ", ".join(removed))

    @asynccontextmanager
    async def managed(self):
        """Async context manager for start/stop lifecycle."""
        await self.start_all()
        try:
            yield self
        finally:
            await self.stop_all()


# Module-level singleton
_registry: Optional[ConnectorRegistry] = None


async def initialize_connector_registry(app=None) -> None:
    """Initialize the global connector registry.

    Called during server lifespan startup. If connectors config is empty,
    auto-migrates from plugins.json.

    Args:
        app: Optional FastAPI app instance for mounting webhook routers.
    """
    global _registry

    cfg = config.CONFIG
    connectors_config = cfg.get("connectors", {})

    # Auto-migrate from plugins.json if connectors is empty
    if not connectors_config:
        from .migration import migrate_plugins_json
        connectors_config = migrate_plugins_json(cfg)
        if connectors_config:
            cfg["connectors"] = connectors_config

    _registry = ConnectorRegistry(connectors_config)

    # Auto-register web channel connector if not explicitly configured
    if "web" not in _registry.connectors:
        from .web_channel import WebChannelConnector
        web = WebChannelConnector()
        _registry.connectors["web"] = web

    await _registry.start_all()

    # Mount webhook routes from channel connectors
    if app is not None:
        for connector in _registry.connectors.values():
            connector_routes = getattr(connector, "routes", None)
            if connector_routes:
                app.routes.extend(connector_routes)
                logger.info("Mounted webhook routes for connector: %s", connector.name)

    # Register heartbeat hooks
    from ..core.engine.main_loop import register_heartbeat_hook
    register_heartbeat_hook(_registry.check_for_config_changes)

    # Register retention cleanup hook
    from .retention import create_retention_hook
    retention_hook = create_retention_hook()
    if retention_hook:
        register_heartbeat_hook(retention_hook)

    connector_count = len(_registry.connectors)
    logger.info("Connector registry initialized: %d connector(s) loaded", connector_count)


def get_connector_registry() -> Optional[ConnectorRegistry]:
    """Get the global connector registry instance, or None if not initialized."""
    return _registry

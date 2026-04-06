"""Trigger registry — manages trigger type registration and instance lifecycle.

Trigger types are registered by class; instances are created from config.
The registry provides access to pollable and endpoint triggers for the
main loop and HTTP server respectively.
"""

import importlib
import importlib.util
import logging
import os
from pathlib import Path

from .base import Trigger, PollableTrigger, EndpointTrigger

logger = logging.getLogger(__name__)

# Type registry: trigger_type string → Trigger subclass
_trigger_types: dict[str, type[Trigger]] = {}

# Active trigger instances
_instances: list[Trigger] = []


def register_trigger_type(cls: type[Trigger]) -> None:
    """Register a trigger class by its trigger_type() string.

    Raises:
        TypeError: If cls is not a Trigger subclass.
        ValueError: If trigger_type() is already registered.
    """
    if not (isinstance(cls, type) and issubclass(cls, Trigger)):
        raise TypeError(f"{cls} is not a Trigger subclass")

    type_name = cls.trigger_type()
    if type_name in _trigger_types:
        existing = _trigger_types[type_name]
        if existing is cls:
            return  # idempotent
        raise ValueError(
            f"Trigger type {type_name!r} already registered by {existing.__name__}"
        )

    _trigger_types[type_name] = cls
    logger.debug("Registered trigger type: %s (%s)", type_name, cls.__name__)


def get_trigger_type(type_name: str) -> type[Trigger] | None:
    """Look up a registered trigger class by type name."""
    return _trigger_types.get(type_name)


def load_triggers(trigger_configs: list[dict]) -> list[Trigger]:
    """Instantiate triggers from config dicts.

    Each config dict must have:
        - name: unique trigger name
        - type: registered trigger type string
        - enabled: bool (default True)
        - ... additional type-specific config

    Returns list of instantiated triggers (only enabled ones).
    Appends to the global instance list.
    """
    triggers = []
    for cfg in trigger_configs:
        name = cfg.get("name")
        type_name = cfg.get("type")
        enabled = cfg.get("enabled", True)

        if not enabled:
            logger.debug("Skipping disabled trigger: %s", name)
            continue

        if not name or not type_name:
            logger.warning("Trigger config missing name or type: %s", cfg)
            continue

        cls = _trigger_types.get(type_name)
        if cls is None:
            logger.warning(
                "Unknown trigger type %r for trigger %r (registered: %s)",
                type_name, name, sorted(_trigger_types.keys()),
            )
            continue

        try:
            instance = cls(name=name, config=cfg)
            triggers.append(instance)
            _instances.append(instance)
            logger.info("Loaded trigger: %s (type=%s)", name, type_name)
        except Exception:
            logger.exception("Failed to instantiate trigger %s (type=%s)", name, type_name)

    return triggers


def load_user_triggers(directory: str) -> int:
    """Scan a directory for Python files defining custom trigger subclasses.

    Each .py file is imported. Any Trigger subclass with a trigger_type()
    method is automatically registered.

    Args:
        directory: Path to scan for trigger plugin files.

    Returns:
        Number of trigger types registered.
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        logger.debug("Trigger plugins directory does not exist: %s", directory)
        return 0

    registered = 0
    for py_file in sorted(dir_path.glob("*.py")):
        if py_file.name.startswith("_"):
            continue

        module_name = f"carpenter_trigger_plugin_{py_file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, str(py_file))
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Find and register Trigger subclasses
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, Trigger)
                    and attr not in (Trigger, PollableTrigger, EndpointTrigger)
                    and hasattr(attr, "trigger_type")
                    and not getattr(attr.trigger_type, "__isabstractmethod__", False)
                ):
                    try:
                        register_trigger_type(attr)
                        registered += 1
                    except (TypeError, ValueError) as exc:
                        logger.warning("Could not register %s from %s: %s", attr_name, py_file, exc)

        except Exception:
            logger.exception("Failed to load trigger plugin: %s", py_file)

    if registered:
        logger.info("Loaded %d user trigger type(s) from %s", registered, directory)
    return registered


def get_trigger_instances() -> list[Trigger]:
    """Return all active trigger instances."""
    return list(_instances)


def get_pollable_triggers() -> list[PollableTrigger]:
    """Return all PollableTrigger instances."""
    return [t for t in _instances if isinstance(t, PollableTrigger)]


def get_endpoint_triggers() -> list[EndpointTrigger]:
    """Return all EndpointTrigger instances."""
    return [t for t in _instances if isinstance(t, EndpointTrigger)]


def start_all() -> None:
    """Call start() on all trigger instances."""
    for trigger in _instances:
        try:
            trigger.start()
        except Exception:
            logger.exception("Failed to start trigger: %s", trigger.name)


def stop_all() -> None:
    """Call stop() on all trigger instances."""
    for trigger in _instances:
        try:
            trigger.stop()
        except Exception:
            logger.exception("Failed to stop trigger: %s", trigger.name)


def check_pollable_triggers() -> int:
    """Call check() on all PollableTrigger instances.

    Returns the number of triggers checked.
    """
    checked = 0
    for trigger in get_pollable_triggers():
        try:
            trigger.check()
            checked += 1
        except Exception:
            logger.exception("Error in pollable trigger check: %s", trigger.name)
    return checked


def reset() -> None:
    """Clear all registrations and instances. For testing only."""
    _trigger_types.clear()
    _instances.clear()

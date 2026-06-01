"""Forge provider registry — abstraction over git-forge backends.

Provides :func:`register_forge_provider` and :func:`get_forge_provider`.
Forgejo ships with core and is registered eagerly at module-import time
(see bottom of file).  External providers (e.g. GitHub) call
``register_forge_provider("github", GitHubProvider())`` at startup.

``get_forge_provider(None)`` returns the provider named by
``config.CONFIG["forge"]``, defaulting to ``"forgejo"`` when the key is
absent.
"""
from __future__ import annotations

import logging
from typing import Optional

from .protocol import ForgeEvent, ForgeProvider

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, ForgeProvider] = {}


def register_forge_provider(name: str, impl: ForgeProvider) -> None:
    """Register a forge provider implementation under ``name``.

    Re-registering an existing name overwrites the previous entry — this
    is intentional so that tests can swap in fakes via
    ``register_forge_provider("forgejo", FakeProvider())``.
    """
    _REGISTRY[name] = impl
    logger.debug("Registered forge provider %r: %s", name, impl.__class__.__name__)


def get_forge_provider(name: Optional[str] = None) -> Optional[ForgeProvider]:
    """Resolve a forge provider by name.

    If ``name`` is ``None``, falls back to ``config.CONFIG["forge"]``,
    defaulting to ``"forgejo"`` when neither is set.

    Returns ``None`` when no provider is registered under the resolved
    name (callers should check and log/skip).
    """
    if name is None:
        # Late import to avoid circular dependency with config module.
        from .. import config as _config
        name = _config.CONFIG.get("forge", "forgejo") or "forgejo"
    return _REGISTRY.get(name)


# Eager registration of the built-in Forgejo provider.  Mirrors the
# platform-injection pattern but is simpler — Forgejo ships with core,
# and external providers can call register_forge_provider later without
# touching core startup.
from .forgejo import ForgejoProvider  # noqa: E402  (after registry definition)

register_forge_provider("forgejo", ForgejoProvider())


__all__ = [
    "ForgeEvent",
    "ForgeProvider",
    "register_forge_provider",
    "get_forge_provider",
]

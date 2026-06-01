"""Knowledge Base — unified navigable graph of capabilities and knowledge.

Public API:
    get_store() -> KBStore: Get the singleton KB store instance.
    install_seed(kb_dir) -> dict: Copy seed KB entries on first install.
"""

import logging

from .store import KBStore

logger = logging.getLogger(__name__)

_store: KBStore | None = None


def get_store(kb_dir: str | None = None) -> KBStore:
    """Get the singleton KBStore instance."""
    global _store
    if _store is None or (kb_dir and _store.kb_dir != kb_dir):
        _store = KBStore(kb_dir=kb_dir)
    return _store


def install_seed(kb_dir: str) -> dict:
    """Copy seed KB entries to kb_dir on first install.

    Thin wrapper that delegates to :func:`carpenter.seed.install_single_target`.
    Only copies if kb_dir does not yet exist. Returns change summary.
    """
    from ..seed import install_single_target
    return install_single_target("kb", kb_dir)

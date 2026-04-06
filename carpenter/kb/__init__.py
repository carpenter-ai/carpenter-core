"""Knowledge Base — unified navigable graph of capabilities and knowledge.

Public API:
    get_store() -> KBStore: Get the singleton KB store instance.
    install_seed(kb_dir) -> dict: Copy seed KB entries on first install.
"""

import logging
import os
import shutil
from pathlib import Path

from .store import KBStore

logger = logging.getLogger(__name__)

_store: KBStore | None = None

# Seed KB entries ship in the config_seed/kb/ directory at the repository root.
_SEED_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config_seed", "kb",
)


def get_store(kb_dir: str | None = None) -> KBStore:
    """Get the singleton KBStore instance."""
    global _store
    if _store is None or (kb_dir and _store.kb_dir != kb_dir):
        _store = KBStore(kb_dir=kb_dir)
    return _store


def install_seed(kb_dir: str) -> dict:
    """Copy seed KB entries to kb_dir on first install.

    Only copies if kb_dir does not yet exist. Returns change summary.
    """
    if os.path.isdir(kb_dir):
        return {"status": "exists", "copied": 0}

    if not os.path.isdir(_SEED_DIR):
        logger.warning("KB seed directory not found: %s", _SEED_DIR)
        return {"status": "no_seed", "copied": 0}

    try:
        shutil.copytree(_SEED_DIR, kb_dir)
        # Count copied files
        count = sum(1 for _ in Path(kb_dir).rglob("*.md"))
        logger.info("Installed KB seed: %d entries to %s", count, kb_dir)
        return {"status": "installed", "copied": count}
    except OSError as e:
        logger.error("Failed to install KB seed: %s", e)
        return {"status": "error", "error": str(e), "copied": 0}

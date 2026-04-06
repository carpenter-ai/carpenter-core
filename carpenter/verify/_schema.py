"""Attrs schema-based policy type resolution for verified flow analysis.

When taint analysis cannot detect the policy type from comparison context
(no C == T(PolicyLiteral) pattern), this module attempts to resolve the
type from the source arc's output_contract attrs schema.

Convention for annotating policy types on attrs model fields:

    import attrs

    @attrs.define
    class MyOutput:
        email: str = attrs.field(metadata={"policy_type": "email"})
        count: int = attrs.field(metadata={"policy_type": "int_range"})
        name: str  # no policy_type → stays None
"""

from __future__ import annotations

import importlib
import logging
import sqlite3
import sys
from typing import Any

logger = logging.getLogger(__name__)

# Valid policy types that can appear in field metadata
_VALID_POLICY_TYPES = frozenset({
    "email", "domain", "url", "filepath", "command",
    "int_range", "enum", "bool", "pattern",
})


def resolve_policy_type(arc_id: int, key: str) -> str | None:
    """Resolve the policy type for a state key from the arc's output_contract.

    Args:
        arc_id: The arc whose output schema to check.
        key: The state key being read (maps to a field name on the model).

    Returns:
        Policy type string (e.g. "email") or None if unresolvable.
    """
    contract = _get_output_contract(arc_id)
    if contract is None:
        return None

    model_class = _load_model_class(contract)
    if model_class is None:
        return None

    return _get_field_policy_type(model_class, key)


def _get_output_contract(arc_id: int) -> str | None:
    """Look up the output_contract for an arc from the database."""
    try:
        from ..db import get_db, db_connection
        with db_connection() as db:
            row = db.execute(
                "SELECT output_contract FROM arcs WHERE id = ?", (arc_id,)
            ).fetchone()
            if row is not None:
                return row["output_contract"]
    except Exception as _exc:  # broad catch: DB may not be available during verification
        logger.debug("Could not look up output_contract for arc %s", arc_id)
    return None


def _load_model_class(contract: str) -> Any:
    """Load a Pydantic model class from a 'module:ClassName' contract string.

    The module is resolved relative to the data_models_dir directory.
    E.g. 'dark_factory:DevelopmentSpec' loads DevelopmentSpec from
    config_seed/data_models/dark_factory.py (seed) or {base_dir}/config/data_models/.
    """
    if ":" not in contract:
        logger.debug("Invalid output_contract format (no ':'): %s", contract)
        return None

    module_name, class_name = contract.split(":", 1)

    # Try importing from data_models package
    full_module = f"data_models.{module_name}" if not module_name.startswith("data_models") else module_name

    try:
        # Ensure data_models_dir is on sys.path
        from .. import config as config_mod
        data_models_dir = config_mod.CONFIG.get("data_models_dir", "")
        if data_models_dir:
            import os
            parent = os.path.dirname(data_models_dir.rstrip("/"))
            if parent and parent not in sys.path:
                sys.path.insert(0, parent)

        mod = importlib.import_module(full_module)
        cls = getattr(mod, class_name, None)
        if cls is None:
            logger.debug("Class %s not found in module %s", class_name, full_module)
            return None
        return cls
    except ImportError:
        logger.debug("Could not import module %s for contract %s", full_module, contract)
        return None
    except (KeyError, ValueError, TypeError) as e:
        logger.debug("Error loading contract %s: %s", contract, e)
        return None


def _get_field_policy_type(model_class: Any, field_name: str) -> str | None:
    """Extract the policy_type from an attrs model field's metadata.

    Looks for metadata={"policy_type": "email"} on the field.
    """
    import attrs

    try:
        fields = attrs.fields(model_class)
    except (attrs.exceptions.NotAnAttrsClassError, TypeError):
        return None

    field_info = None
    for f in fields:
        if f.name == field_name:
            field_info = f
            break
    if field_info is None:
        return None

    # Check attrs field metadata (mappingproxy or dict)
    meta = field_info.metadata
    if hasattr(meta, "get"):
        policy_type = meta.get("policy_type")
        if policy_type in _VALID_POLICY_TYPES:
            return policy_type

    return None

"""Arc subsystem — arc lifecycle, dispatch, retry, verification.

Re-exports key public symbols::

    from carpenter.core.arcs import create_arc, get_arc, dispatch_arc

Note: dispatch_handler and child_failure_handler are NOT eagerly imported
here to avoid circular imports (they trigger engine/__init__.py which
tries to import arc_manager back). They are imported where needed.
"""

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CODING_CHANGE_PREFIX = "coding-change"
"""Name prefix for coding-change arcs (used in SQL LIKE and startswith checks)."""

# Manager (arc lifecycle CRUD)
from .manager import (  # noqa: F401
    VALID_STATUSES, create_arc, add_child, get_arc, get_children, get_subtree,
    update_status, cancel_arc, add_history, get_history, check_dependencies,
    check_activation, dispatch_arc, freeze_arc, is_frozen,
    increment_ancestor_arc_count, increment_ancestor_executions,
    increment_ancestor_tokens, get_or_create_agent_config, get_agent_config,
    get_or_create_model_policy, get_model_policy, grant_read_access,
    has_read_grant, list_read_grants, get_policy_id_by_name, get_policy_by_name,
    update_arc_counters, check_dependencies_detailed,
)

# Retry
from .retry import RetryDecision, should_retry_arc, initialize_retry_state, calculate_backoff, record_retry_attempt, get_retry_state  # noqa: F401

# Verification
from .verification import create_verification_arcs, should_create_verification_arcs, is_coding_arc  # noqa: F401

# Data model validation
from .data_model_validation import parse_contract_ref, validate_contract, load_model_class  # noqa: F401

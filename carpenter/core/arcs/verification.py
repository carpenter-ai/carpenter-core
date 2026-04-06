"""Verification sibling arc pattern for coding arcs.

When a coding arc (one that produces persistent code changes) reaches
the generate-review step, the platform auto-creates verification sibling
arcs to check the work BEFORE human approval. This is separation of
powers: the implementer cannot verify its own work.

Verification arcs created (in order):
1. Quality check (verifier) -- static code review, no execution (platform/tool code only)
2. Correctness check (verifier) -- runs tests, validates behavior
3. Judge (Python-only) -- boolean aggregation of check results, no AI
4. Documentation (worker) -- after judge passes, writes docstrings + summary

Flow: quality → correctness → judge → docs → [human approval]
If judge fails, the coding agent is re-invoked with verification feedback.
"""

import json
import logging
import os
import sqlite3

from ... import config
from ...db import get_db, db_connection
from . import manager as arc_manager
from ..engine import template_executor
from ..workflows._arc_state import set_arc_state as _set_arc_state

logger = logging.getLogger(__name__)

# Built-in arc names used by the verification pattern.
# These serve as defaults; users can override via config.yaml
# under verification.arc_names.
_BUILTIN_CORRECTNESS_CHECK = "verify-correctness"
_BUILTIN_QUALITY_CHECK = "verify-quality"
_BUILTIN_JUDGE_VERIFICATION = "judge-verification"
_BUILTIN_DOCUMENTATION_ARC = "post-verification-docs"


def _get_verification_config() -> dict:
    """Return the verification section of the config, with defaults."""
    return config.CONFIG.get("verification", {})


def get_arc_name(key: str) -> str:
    """Return the configured verification arc name for *key*.

    Valid keys: ``correctness_check``, ``quality_check``, ``judge``,
    ``documentation``.
    """
    builtins = {
        "correctness_check": _BUILTIN_CORRECTNESS_CHECK,
        "quality_check": _BUILTIN_QUALITY_CHECK,
        "judge": _BUILTIN_JUDGE_VERIFICATION,
        "documentation": _BUILTIN_DOCUMENTATION_ARC,
    }
    arc_names = _get_verification_config().get("arc_names", {})
    return arc_names.get(key, builtins[key])


# Deprecated: use get_arc_name('correctness_check') etc. instead.
# These module-level aliases will be removed in a future release.
CORRECTNESS_CHECK = _BUILTIN_CORRECTNESS_CHECK
QUALITY_CHECK = _BUILTIN_QUALITY_CHECK
JUDGE_VERIFICATION = _BUILTIN_JUDGE_VERIFICATION
DOCUMENTATION_ARC = _BUILTIN_DOCUMENTATION_ARC


def _get_model_policy_fallback() -> str:
    """Return the configured model-policy fallback for verification steps."""
    return _get_verification_config().get("model_policy_fallback", "careful-coding")


def _get_model_policy_for_verification_step(
    template_id: int | None,
    step_name: str,
) -> int | None:
    """Get model_policy_id for a verification step from template.

    Falls back to the configured model_policy_fallback (default:
    ``careful-coding``) if the template doesn't specify.
    """
    return template_executor.get_model_policy_for_step(
        template_id, step_name, fallback=_get_model_policy_fallback()
    )


def _inherit_source_category(parent_arc_id: int, child_arc_id: int) -> None:
    """Copy source_category from parent arc to child arc in arc_state.

    This ensures verification arcs use the same model policies as their parent.
    """
    with db_connection() as db:
        # Read source_category from parent
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = 'source_category'",
            (parent_arc_id,)
        ).fetchone()

        if row:
            source_category = row["value_json"]
            # Write to child
            db.execute(
                "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?) "
                "ON CONFLICT(arc_id, key) DO UPDATE SET value_json = excluded.value_json, "
                "updated_at = CURRENT_TIMESTAMP",
                (child_arc_id, "source_category", source_category)
            )
            db.commit()


def is_coding_arc(arc: dict) -> bool:
    """Return True if the arc represents a coding change that produces persistent code.

    Checks for coding-change arcs (the main coding workflow) and
    external-coding-change arcs.
    """
    from . import CODING_CHANGE_PREFIX
    name = arc.get("name", "")
    return (
        name.startswith(CODING_CHANGE_PREFIX)
        or name.startswith(f"external-{CODING_CHANGE_PREFIX}")
    )


def is_platform_or_tool_code(arc: dict) -> bool:
    """Return True if the arc modifies platform or tool code.

    Checks arc goal/name for platform/tool indicators.
    """
    # Check goal text for platform/tool indicators
    goal = arc.get("goal", "") or ""
    name = arc.get("name", "") or ""

    if "platform" in name.lower() or "platform" in goal.lower():
        return True
    if "carpenter" in goal or "carpenter_tools" in goal:
        return True

    return False


def should_create_verification_arcs(arc: dict, *, require_completed: bool = True) -> bool:
    """Return True if verification arcs should be created for this arc.

    Conditions:
    - Arc is a coding arc (produces persistent changes)
    - Arc completed successfully (unless require_completed=False)
    - Verification is enabled in config
    - Arc is not itself a verifier (no recursive verification)

    Args:
        require_completed: If True (default), require status == "completed".
            Pass False when creating verification arcs pre-approval (e.g.
            from generate-review, where the arc is still active).
    """
    if not is_coding_arc(arc):
        return False
    if require_completed and arc.get("status") != "completed":
        return False
    if arc.get("arc_role") == "verifier":
        return False

    verification_config = config.CONFIG.get("verification", {})
    return verification_config.get("enabled", False)


def create_verification_arcs(
    implementation_arc_id: int,
    *,
    require_completed: bool = True,
) -> list[int]:
    """Create verification sibling arcs for a coding arc.

    Creates (in order):
    1. Quality check (verifier, static review -- only for platform/tool code)
    2. Correctness check (verifier, runs tests -- after quality for security)
    3. Judge (Python-only aggregation, no AI -- depends on checks)
    4. Documentation (worker, depends on judge)

    Quality gates correctness: static review runs first so that obviously
    malicious code is caught before any test execution occurs.

    All verification arcs share the same parent as the implementation arc
    and have verification_target_id pointing to the implementation arc.

    Args:
        implementation_arc_id: The coding arc to verify.
        require_completed: If True (default), require the arc to be
            completed. Pass False for pre-approval verification (called
            from generate-review while the arc is still active).

    Returns:
        List of created verification arc IDs.

    Raises:
        ValueError: If the implementation arc is not found or not in
            the expected status.
    """
    impl_arc = arc_manager.get_arc(implementation_arc_id)
    if impl_arc is None:
        raise ValueError(f"Implementation arc {implementation_arc_id} not found")
    if require_completed and impl_arc["status"] != "completed":
        raise ValueError(
            f"Implementation arc {implementation_arc_id} has status "
            f"'{impl_arc['status']}', expected 'completed'"
        )

    parent_id = impl_arc["parent_id"]
    needs_quality = is_platform_or_tool_code(impl_arc)
    impl_name = impl_arc.get("name", f"arc-{implementation_arc_id}")

    created_ids = []

    # Determine step_order for verification arcs (after all existing siblings)
    with db_connection() as db:
        row = db.execute(
            "SELECT COALESCE(MAX(step_order), -1) AS max_order "
            "FROM arcs WHERE parent_id = ?",
            (parent_id,) if parent_id is not None else (None,),
        ).fetchone()
        base_order = (row["max_order"] + 1) if row else 0

    # Get template_id from implementation arc for model policy lookup
    template_id = impl_arc.get("template_id")

    # Resolve arc names from config (allows renaming without code changes)
    quality_name = get_arc_name("quality_check")
    correctness_name = get_arc_name("correctness_check")
    judge_name = get_arc_name("judge")
    docs_name = get_arc_name("documentation")

    # 1. Quality check (only for platform/tool code -- runs FIRST for security)
    quality_id = None
    if needs_quality:
        quality_policy_id = _get_model_policy_for_verification_step(
            template_id, "verify-quality"
        )
        quality_id = arc_manager.create_arc(
            name=quality_name,
            goal=(
                f"Verify code quality of '{impl_name}' (arc #{implementation_arc_id}): "
                f"check style, naming, structure, error handling for platform/tool code."
            ),
            parent_id=parent_id,
            step_order=base_order,
            arc_role="verifier",
            verification_target_id=implementation_arc_id,
            agent_type="REVIEWER",
            integrity_level="trusted",
            model_policy_id=quality_policy_id,  # From template
        )
        created_ids.append(quality_id)
        _inherit_source_category(implementation_arc_id, quality_id)

        arc_manager.add_history(
            implementation_arc_id,
            "verification_arc_created",
            {"verification_arc_id": quality_id, "check_type": "quality"},
        )

    # 2. Correctness check (after quality -- quality gates correctness)
    correctness_policy_id = _get_model_policy_for_verification_step(
        template_id, "verify-correctness"
    )
    correctness_id = arc_manager.create_arc(
        name=correctness_name,
        goal=(
            f"Verify correctness of '{impl_name}' (arc #{implementation_arc_id}): "
            f"run tests, check behavior matches spec, validate no regressions."
        ),
        parent_id=parent_id,
        step_order=base_order + (1 if needs_quality else 0),
        arc_role="verifier",
        verification_target_id=implementation_arc_id,
        agent_type="REVIEWER",
        integrity_level="trusted",
        model_policy_id=correctness_policy_id,  # From template
    )
    created_ids.append(correctness_id)
    _inherit_source_category(implementation_arc_id, correctness_id)

    arc_manager.add_history(
        implementation_arc_id,
        "verification_arc_created",
        {"verification_arc_id": correctness_id, "check_type": "correctness"},
    )

    # 3. Judge -- Python-only boolean aggregation (no AI agent)
    # The dedicated handler in arc_dispatch_handler reads sibling statuses
    # and produces a pass/fail verdict without invoking an LLM.
    judge_step = base_order + (2 if needs_quality else 1)
    judge_id = arc_manager.create_arc(
        name=judge_name,
        goal=(
            f"Aggregate verification results for '{impl_name}' "
            f"(arc #{implementation_arc_id}): "
            + ("quality and " if needs_quality else "")
            + f"correctness check → boolean pass/fail."
        ),
        parent_id=parent_id,
        step_order=judge_step,
        arc_role="verifier",
        verification_target_id=implementation_arc_id,
        # No agent_type="REVIEWER" — handled by dedicated Python code
        agent_type="EXECUTOR",
        integrity_level="trusted",
    )
    created_ids.append(judge_id)

    arc_manager.add_history(
        implementation_arc_id,
        "verification_arc_created",
        {"verification_arc_id": judge_id, "check_type": "judge"},
    )

    # 4. Documentation arc (worker, depends on judge)
    docs_policy_id = _get_model_policy_for_verification_step(
        template_id, "post-verification-docs"
    )
    docs_id = arc_manager.create_arc(
        name=docs_name,
        goal=(
            f"Post-verification documentation for '{impl_name}' "
            f"(arc #{implementation_arc_id}): read the reviewed code, "
            f"write docstrings and arc summary."
        ),
        parent_id=parent_id,
        step_order=judge_step + 1,
        arc_role="worker",
        verification_target_id=implementation_arc_id,
        agent_type="EXECUTOR",
        integrity_level="trusted",
        model_policy_id=docs_policy_id,  # From template (background-batch)
    )
    created_ids.append(docs_id)
    _inherit_source_category(implementation_arc_id, docs_id)

    arc_manager.add_history(
        implementation_arc_id,
        "verification_arc_created",
        {"verification_arc_id": docs_id, "check_type": "documentation"},
    )

    logger.info(
        "Created %d verification arcs for implementation arc %d: %s",
        len(created_ids), implementation_arc_id, created_ids,
    )

    return created_ids


def try_create_verification_arcs(
    arc_id: int,
    *,
    label: str = "arc",
) -> bool:
    """Attempt to create pre-approval verification arcs for a coding arc.

    This is a convenience wrapper used by workflow handlers to create
    verification arcs with consistent error handling and arc-state updates.
    On success it stores ``_verification_arc_ids`` and sets
    ``_verification_pending = True`` in the arc's state.

    Args:
        arc_id: The coding arc to verify.
        label: Human-readable label for log messages (e.g.
            ``"arc"`` or ``"external-coding-change"``).

    Returns:
        True if verification arcs were created, False otherwise
        (disabled, not applicable, or an error occurred).
    """
    try:
        arc_info = arc_manager.get_arc(arc_id)
        if arc_info and should_create_verification_arcs(
            arc_info, require_completed=False,
        ):
            v_ids = create_verification_arcs(arc_id, require_completed=False)
            _set_arc_state(arc_id, "_verification_arc_ids", v_ids)
            _set_arc_state(arc_id, "_verification_pending", True)
            logger.info(
                "Created %d verification arcs for %s %d",
                len(v_ids), label, arc_id,
            )
            return True
    except (ImportError, ValueError, sqlite3.Error):
        logger.exception(
            "Failed to create verification arcs for %s %d",
            label, arc_id,
        )
    return False

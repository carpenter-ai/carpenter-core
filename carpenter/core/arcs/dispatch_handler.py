"""Arc dispatch handler for automatic execution of ready arcs.

Handles the arc.dispatch event type — transitions arcs to active and
executes them via dispatch_arc(). Also provides a heartbeat hook to
scan for ready children after parent arcs complete.
"""

import asyncio
import json
import logging
import sqlite3
import time

from ...db import get_db, db_connection, db_transaction
from . import CODING_CHANGE_PREFIX, manager as arc_manager, retry as arc_retry
from ..models import health as model_health
from .. import notifications
from ..engine import work_queue, main_loop, event_bus
from ...agent import error_classifier

logger = logging.getLogger(__name__)


async def handle_arc_dispatch(work_id: int, payload: dict):
    """Handle arc.dispatch work item.

    Payload: {arc_id: int}

    When dispatched via cron, check_cron() wraps the event_payload inside
    its own metadata dict: {cron_id, cron_name, fire_time, event_payload: {arc_id, conversation_id}}

    Calls arc_manager.dispatch_arc() which:
    - Transitions arc from pending -> active
    - If arc has code_file_id, executes via code_manager
    - Otherwise returns invoke_agent action
    """
    arc_id = payload.get("arc_id")
    event_payload = payload.get("event_payload") or {}
    is_cron_triggered = "cron_id" in payload

    # When dispatched via cron, extract arc_id from nested event_payload
    if arc_id is None:
        arc_id = event_payload.get("arc_id")

    if arc_id is None:
        logger.error(
            "arc.dispatch: missing arc_id in payload. "
            'Expected event_payload={"arc_id": <int>}. Got: %s',
            payload,
        )
        return

    # Extract conversation_id from cron event_payload and link the arc.
    # Without this link, _find_arc_conversation() can't route arc output
    # back to the user's conversation.
    cron_conversation_id = event_payload.get("conversation_id") if is_cron_triggered else None
    if cron_conversation_id:
        from ...agent.conversation import link_arc_to_conversation
        link_arc_to_conversation(cron_conversation_id, arc_id)

    # Check arc exists and is in a dispatchable state before proceeding.
    # Arcs may have been cancelled, completed, or failed between enqueue and
    # processing — skip gracefully to avoid ValueError from dispatch_arc().
    # "waiting" is included for retry scenarios (arc waiting for backoff timer).
    arc_info_early = arc_manager.get_arc(arc_id)
    if not arc_info_early:
        logger.debug("arc.dispatch: arc %d not found, skipping", arc_id)
        return
    early_status = arc_info_early.get("status")
    if early_status not in ("pending", "active", "waiting"):
        # Recurring cron fires may reference an arc that already completed.
        # Clone it as a fresh standalone arc so the recurring work continues.
        if is_cron_triggered and early_status in ("completed", "failed", "frozen"):
            arc_id = _clone_arc_for_cron(arc_id, arc_info_early, cron_conversation_id)
            if arc_id is None:
                return
            # Re-fetch the fresh arc info
            arc_info_early = arc_manager.get_arc(arc_id)
        else:
            logger.debug(
                "arc.dispatch: skipping arc %d (status: %s)", arc_id, early_status
            )
            return

    # Coding-change arcs are managed exclusively by coding_change_handler via
    # its own work queue events (coding-change.invoke-agent etc.). Skip them
    # here to avoid racing with that handler and prematurely completing the arc.
    if arc_info_early.get("name", "").startswith(CODING_CHANGE_PREFIX):
        logger.debug("arc.dispatch: skipping coding-change arc %d", arc_id)
        return

    # Judge-verification arcs: Python-only boolean aggregation, no AI agent.
    from .verification import get_arc_name as _get_varc_name
    if arc_info_early.get("name") == _get_varc_name("judge"):
        await _handle_judge_verification(arc_id, arc_info_early)
        return

    # Skill-KB review: Python-only steps (classify-source, text-review,
    # human-escalation).
    from ..workflows import skill_kb_review_handler
    if skill_kb_review_handler.is_skill_kb_review_step(arc_info_early):
        arc_name = arc_info_early.get("name")
        if arc_name == "classify-source":
            await skill_kb_review_handler.handle_classify_source(arc_id, arc_info_early)
        elif arc_name == "text-review":
            await skill_kb_review_handler.handle_text_review(arc_id, arc_info_early)
        elif arc_name == "human-escalation":
            await skill_kb_review_handler.handle_human_escalation(arc_id, arc_info_early)
        return

    # Reflection template save step: Python-only save and post-processing
    from ..workflows import reflection_template_handler
    if reflection_template_handler.is_reflection_save_step(arc_info_early):
        await reflection_template_handler.handle_save_reflection(arc_id, arc_info_early)
        return

    # Track fallback models from policy-based selection for model failover
    _fallback_models = []  # list of SelectionResult, remaining alternatives
    _selected_model_id = None  # model_id of the model we're currently trying

    try:
        result = arc_manager.dispatch_arc(arc_id)
        action = result.get("action")

        if action == "invoke_agent":
            arc_info = arc_manager.get_arc(arc_id)
            agent_type = (arc_info.get("agent_type", "EXECUTOR") if arc_info else "EXECUTOR")
            goal = (arc_info.get("goal") or arc_info.get("name") or f"Arc #{arc_id}") if arc_info else f"Arc #{arc_id}"
            config_id = arc_info.get("agent_config_id") if arc_info else None

            # JUDGE arcs: run deterministic platform code, not LLM agents
            if agent_type == "JUDGE":
                await _run_judge_checks(arc_id)
                arc_manager.freeze_arc(arc_id)
                _propagate_completion(arc_id)
                logger.info("Arc %d dispatched successfully (action: judge_policy_checks)", arc_id)
                return

            # Resolve model config — prefer model_policy_id, fall back to agent_config_id
            policy_id = arc_info.get("model_policy_id") if arc_info else None
            agent_config = None

            if policy_id is not None:
                policy_row = arc_manager.get_model_policy(policy_id)
                if policy_row:
                    policy_json = policy_row.get("policy_json")
                    if policy_json and not policy_row.get("model"):
                        # Policy-based selection — get ranked list for fallback
                        try:
                            from ..models.selector import ModelPolicy, select_models
                            policy = ModelPolicy.from_db_row(policy_row)
                            ranked = select_models(policy)
                            if not ranked:
                                _fire_connectivity_degraded(arc_id)
                                return
                            # Use top-ranked model, keep rest as fallbacks
                            top = ranked[0]
                            _fallback_models = ranked[1:]
                            _selected_model_id = top.model_id
                            # Build agent_config-compatible dict from selection
                            agent_config = {
                                "model": top.model_id,
                                "agent_role": policy_row.get("agent_role"),
                                "temperature": policy_row.get("temperature"),
                                "max_tokens": policy_row.get("max_tokens"),
                            }
                            logger.info(
                                "Arc %d: model selector chose %s (%s), %d fallback(s)",
                                arc_id, top.model_key, top.reason,
                                len(_fallback_models),
                            )
                        except (ImportError, KeyError, ValueError, TypeError) as _exc:
                            logger.exception("Arc %d: model selector failed, falling back", arc_id)
                            agent_config = dict(policy_row)
                    else:
                        # Hard-pinned model in policy row
                        agent_config = dict(policy_row)

            if agent_config is None and config_id is not None:
                agent_config = arc_manager.get_agent_config(config_id)

            # Decide whether to invoke an agent:
            # - If arc has agent_config_id or model_policy_id: invoke with that model
            # - If EXECUTOR without config: invoke with default model (backward compat)
            # - Otherwise (PLANNER/REVIEWER without config): freeze immediately
            should_invoke = agent_config is not None or config_id is not None or agent_type == "EXECUTOR"

            if should_invoke:
                conv_id = _find_arc_conversation(arc_id)
                if conv_id:
                    logger.info(
                        "Arc %d (%s): invoking agent for goal: %s",
                        arc_id, agent_type, goal[:80],
                    )
                    await _run_arc_agent(arc_id, goal, conv_id, agent_config=agent_config)
                else:
                    logger.warning(
                        "Arc %d: no conversation found for invoke_agent, "
                        "freezing without execution",
                        arc_id,
                    )
            else:
                # PLANNER/REVIEWER/JUDGE arcs without config or code: freeze immediately
                logger.info(
                    "Arc %d (%s): no code file or agent config, freezing immediately",
                    arc_id, agent_type,
                )

            arc_manager.freeze_arc(arc_id)
            _propagate_completion(arc_id)

        elif action == "execute_code":
            # Code execution happened, now freeze the arc
            arc_manager.freeze_arc(arc_id)
            _propagate_completion(arc_id)

        # Post-verification-docs completion: transition the target coding-change
        # arc to "waiting" for human approval now that all verification + docs
        # are done.
        arc_info_post = arc_manager.get_arc(arc_id)
        if arc_info_post and arc_info_post.get("name") == _get_varc_name("documentation"):
            _handle_docs_completed(arc_id, arc_info_post)

        # Record successful model call (if model was used)
        if _selected_model_id:
            try:
                model_health.record_model_call(
                    model_id=_selected_model_id,
                    success=True,
                )
            except (ImportError, KeyError, ValueError) as _exc:
                pass  # Don't fail dispatch over health tracking
        else:
            arc_info = arc_manager.get_arc(arc_id)
            if arc_info and arc_info.get("agent_config_id"):
                try:
                    agent_config = arc_manager.get_agent_config(arc_info["agent_config_id"])
                    if agent_config and agent_config.get("model"):
                        model_health.record_model_call(
                            model_id=agent_config["model"],
                            success=True,
                        )
                except (ImportError, KeyError, ValueError) as _exc:
                    pass  # Don't fail dispatch over health tracking

        logger.info("Arc %d dispatched successfully (action: %s)", arc_id, action)
    except Exception as e:  # broad catch: dispatch involves AI/DB/subprocess
        logger.exception("Failed to dispatch arc %d", arc_id)

        # Try to extract ErrorInfo from exception or messages
        error_info = _extract_error_info(arc_id, e)

        # Record failed model call (if model was used)
        failed_model_id = _selected_model_id or error_info.model
        if failed_model_id:
            try:
                model_health.record_model_call(
                    model_id=failed_model_id,
                    success=False,
                    error_type=error_info.type,
                )
            except (ImportError, KeyError, ValueError) as _exc:
                pass  # Don't fail dispatch over health tracking

        # ── Model failover: try next-best model before retry/escalation ──
        # If we have fallback models from policy-based selection and the
        # error looks like a provider connectivity issue, immediately retry
        # with the next model instead of entering the backoff retry loop.
        if _fallback_models and _is_provider_error(error_info):
            fallback_succeeded = await _try_fallback_models(
                arc_id, _fallback_models, agent_config, error_info,
            )
            if fallback_succeeded:
                return

        # Check circuit breaker status
        if error_info.model and model_health.should_circuit_break(error_info.model):
            logger.warning(
                "Arc %d: model %s circuit breaker is OPEN, escalating immediately",
                arc_id, error_info.model
            )
            try:
                arc_manager._escalate_arc(arc_id)
            except (ValueError, sqlite3.Error) as _exc:
                logger.exception("Failed to escalate arc %d after circuit break", arc_id)
                arc_manager.update_status(arc_id, "failed")
            return

        # Decide if should retry
        decision = arc_retry.should_retry_arc(arc_id, error_info)

        if decision.should_retry:
            # Record retry attempt
            arc_retry.record_retry_attempt(arc_id, error_info, decision.backoff_seconds)

            # Set arc to waiting status (waiting for retry backoff)
            try:
                arc_manager.update_status(arc_id, "waiting")
            except (ValueError, sqlite3.Error) as _exc:
                logger.exception("Failed to set arc %d to waiting for retry", arc_id)

            # Re-enqueue with backoff
            _reenqueue_arc_dispatch(arc_id, backoff_seconds=decision.backoff_seconds)

            # Get retry state for logging
            retry_state = arc_retry.get_retry_state(arc_id)
            logger.info(
                "Arc %d retry scheduled (attempt %d/%d, backoff %.1fs): %s",
                arc_id, retry_state.get("_retry_count", 0),
                retry_state.get("_max_retries", 0),
                decision.backoff_seconds, decision.reason
            )
        else:
            # Retries exhausted or non-retriable error
            notifications.notify(
                f"Arc {arc_id} failed after retries exhausted: {decision.reason}",
                priority="normal",
                category="retry_exhausted",
            )

            if decision.escalate_on_exhaust:
                try:
                    arc_manager._escalate_arc(arc_id)
                except (ValueError, sqlite3.Error) as _exc:
                    logger.exception("Failed to escalate arc %d after retry exhaust", arc_id)
                    arc_manager.update_status(arc_id, "failed")
                _handle_failed_docs_arc(arc_id)
            else:
                arc_manager.update_status(arc_id, "failed")
                _handle_failed_docs_arc(arc_id)

            logger.warning(
                "Arc %d failed, not retrying: %s", arc_id, decision.reason
            )

        # Don't re-raise — retry logic has handled the failure
        return


# ── Provider error detection and model failover ──────────────────


# Error types that indicate provider unavailability (transient/connectivity).
# These map to error_classifier.classify_error() output types.
_PROVIDER_ERROR_TYPES = frozenset({
    "NetworkError",       # ConnectError, TimeoutException (from error_classifier)
    "APIOutageError",     # HTTP 5xx, CircuitBreakerError (from error_classifier)
    "ConnectionError",    # Raw exception type name
    "ConnectTimeout",     # Raw exception type name
    "TimeoutError",       # Raw exception type name
    "ConnectError",       # Raw exception type name (httpx)
    "ServerError",        # Generic server error
    "ServiceUnavailable", # HTTP 503
})


def _is_provider_error(error_info) -> bool:
    """Check if an error indicates provider unavailability.

    Returns True for connection errors, timeouts, and server errors
    that suggest the provider is offline or unreachable. Returns False
    for client errors (4xx), rate limits, auth failures, etc. that
    would likely affect all providers or are not transient.
    """
    if error_info is None:
        return False
    error_type = getattr(error_info, "type", "") or ""
    # Direct match against known provider error types
    if error_type in _PROVIDER_ERROR_TYPES:
        return True
    # Heuristic: check for connection/timeout in the error type name
    lower = error_type.lower()
    return any(kw in lower for kw in ("connect", "timeout", "unavailable", "unreachable"))


async def _try_fallback_models(
    arc_id: int,
    fallback_models: list,
    original_agent_config: dict | None,
    original_error_info,
) -> bool:
    """Try fallback models after primary model fails with a provider error.

    Iterates through the remaining ranked models. For each, attempts to
    invoke the agent. On success, records the model call as successful
    and completes the arc. On provider error, records the failure and
    continues to the next fallback.

    Args:
        arc_id: The arc being dispatched.
        fallback_models: List of SelectionResult alternatives (score-descending).
        original_agent_config: The agent_config dict from the primary attempt.
        original_error_info: ErrorInfo from the primary failure.

    Returns:
        True if a fallback model succeeded, False if all fallbacks failed.
    """
    if not fallback_models or original_agent_config is None:
        return False

    arc_info = arc_manager.get_arc(arc_id)
    if not arc_info:
        return False

    goal = arc_info.get("goal") or arc_info.get("name") or f"Arc #{arc_id}"
    conv_id = _find_arc_conversation(arc_id)
    if not conv_id:
        return False

    for fallback in fallback_models:
        fallback_config = dict(original_agent_config)
        fallback_config["model"] = fallback.model_id

        logger.info(
            "Arc %d: trying fallback model %s (%s)",
            arc_id, fallback.model_key, fallback.reason,
        )

        try:
            await _run_arc_agent(
                arc_id, goal, conv_id, agent_config=fallback_config,
            )

            # Fallback succeeded — record success and complete the arc
            try:
                model_health.record_model_call(
                    model_id=fallback.model_id, success=True,
                )
            except (ImportError, KeyError, ValueError):
                pass

            arc_manager.freeze_arc(arc_id)
            _propagate_completion(arc_id)

            logger.info(
                "Arc %d: fallback model %s succeeded",
                arc_id, fallback.model_key,
            )
            return True

        except Exception as fb_exc:
            fb_error = _extract_error_info(arc_id, fb_exc)

            # Record the fallback model failure
            try:
                model_health.record_model_call(
                    model_id=fallback.model_id,
                    success=False,
                    error_type=fb_error.type,
                )
            except (ImportError, KeyError, ValueError):
                pass

            if _is_provider_error(fb_error):
                logger.warning(
                    "Arc %d: fallback model %s also failed (provider error: %s), "
                    "trying next",
                    arc_id, fallback.model_key, fb_error.type,
                )
                continue
            else:
                # Non-provider error (e.g., auth, rate limit, content filter)
                # — don't continue failover, let normal retry logic handle it
                logger.warning(
                    "Arc %d: fallback model %s failed with non-provider error "
                    "(%s), stopping failover",
                    arc_id, fallback.model_key, fb_error.type,
                )
                return False

    logger.warning(
        "Arc %d: all %d fallback models exhausted",
        arc_id, len(fallback_models),
    )
    return False


async def _handle_judge_verification(arc_id: int, arc_info: dict) -> None:
    """Handle judge-verification arc with Python-only boolean aggregation.

    Reads sibling verification arcs (verify-quality, verify-correctness),
    aggregates their statuses, and produces a pass/fail verdict without
    invoking an AI agent.

    On PASS: marks judge completed, lets docs arc run via standard propagation.
    On FAIL: marks judge completed, cancels docs arc, re-invokes the coding
        agent with verification feedback (up to a configurable limit).
    """
    from .verification import (
        get_arc_name as _get_vname, _get_verification_config,
    )
    from ..workflows.coding_change_handler import (
        _get_arc_state, _set_arc_state, _notify_chat,
    )

    # Load truncation limits from config
    _vcfg = _get_verification_config()
    reason_max = _vcfg.get("feedback_reason_max_length", 300)
    summary_max = _vcfg.get("feedback_summary_max_length", 500)

    verification_target_id = arc_info.get("verification_target_id")
    parent_id = arc_info.get("parent_id")

    # Activate the arc (pending -> active)
    if arc_info.get("status") == "pending":
        arc_manager.update_status(arc_id, "active")

    if verification_target_id is None:
        logger.error("judge-verification arc %d has no verification_target_id", arc_id)
        arc_manager.update_status(arc_id, "failed")
        return

    # Find sibling verification arcs via parent + verification_target_id
    with db_connection() as db:
        siblings = db.execute(
            "SELECT id, name, status FROM arcs "
            "WHERE parent_id = ? AND verification_target_id = ? "
            "AND id != ?",
            (parent_id, verification_target_id, arc_id),
        ).fetchall()

    # Collect check results
    check_results = []
    all_passed = True
    feedback_parts = []

    for sib in siblings:
        sib_name = sib["name"]
        sib_status = sib["status"]

        # Skip docs arc — it depends on the judge, not the other way around
        if sib_name == _get_vname("documentation"):
            continue

        if sib_name in (_get_vname("correctness_check"), _get_vname("quality_check")):
            passed = sib_status in ("completed", "frozen")
            check_results.append({
                "name": sib_name,
                "arc_id": sib["id"],
                "status": sib_status,
                "passed": passed,
            })
            if not passed:
                all_passed = False
                # Try to get failure reason from arc history
                history = arc_manager.get_history(sib["id"])
                error_entries = [
                    h for h in history
                    if h["entry_type"] in ("error", "failed", "verdict")
                ]
                reason = ""
                if error_entries:
                    last = error_entries[-1]
                    data = last.get("data_json")
                    if isinstance(data, str):
                        try:
                            data = json.loads(data)
                        except (json.JSONDecodeError, TypeError):
                            data = {}
                    elif data is None:
                        data = {}
                    reason = data.get("message", "") or data.get("reason", "") or str(data)
                feedback_parts.append(
                    f"- {sib_name} (arc #{sib['id']}): FAILED ({sib_status})"
                    + (f" — {reason[:reason_max]}" if reason else "")
                )

    verdict = "pass" if all_passed else "fail"
    summary = {
        "verdict": verdict,
        "checks": check_results,
        "feedback": "\n".join(feedback_parts) if feedback_parts else "",
    }

    # Store verdict in judge arc state and history
    with db_transaction() as db:
        db.execute(
            "INSERT INTO arc_state (arc_id, key, value_json) VALUES (?, ?, ?) "
            "ON CONFLICT(arc_id, key) DO UPDATE SET value_json = excluded.value_json, "
            "updated_at = CURRENT_TIMESTAMP",
            (arc_id, "verdict", json.dumps(summary)),
        )

    arc_manager.add_history(
        arc_id, "judge_verdict",
        {"verdict": verdict, "checks": check_results},
    )

    # Also store verification summary on the target coding-change arc
    _set_arc_state(verification_target_id, "_verification_summary", summary)

    if all_passed:
        logger.info(
            "judge-verification arc %d: PASS (%d checks passed) for target %d",
            arc_id, len(check_results), verification_target_id,
        )
        # Mark judge completed — standard propagation will dispatch docs arc
        arc_manager.update_status(arc_id, "completed")
        arc_manager.freeze_arc(arc_id)
        _propagate_completion(arc_id)
    else:
        logger.info(
            "judge-verification arc %d: FAIL for target %d: %s",
            arc_id, verification_target_id, feedback_parts,
        )

        # Use arc_retry to decide whether to rework
        verification_feedback = "\n".join(feedback_parts)
        error_info = error_classifier.ErrorInfo(
            type="VerificationError",
            retry_count=0,
            source_location="arc_dispatch_handler._handle_judge_verification",
            message=f"Verification failed: {verification_feedback[:summary_max]}",
        )
        decision = arc_retry.should_retry_arc(verification_target_id, error_info)

        if decision.should_retry:
            # Record retry attempt (increments _retry_count)
            arc_retry.record_retry_attempt(
                verification_target_id, error_info, decision.backoff_seconds,
            )
            retry_state = arc_retry.get_retry_state(verification_target_id)
            retry_count = retry_state.get("_retry_count", 1)

            # Cancel the docs arc (not needed yet)
            for sib in siblings:
                if sib["name"] == _get_vname("documentation") and sib["status"] == "pending":
                    arc_manager.update_status(sib["id"], "cancelled")

            # Also bump rework_count so workspace is reused
            rework_count = _get_arc_state(verification_target_id, "rework_count", 0)
            _set_arc_state(verification_target_id, "rework_count", rework_count + 1)

            # Determine max retries for display
            max_retries = retry_state.get("_max_retries", 2)

            # Build verification feedback prompt
            from ...agent import templates
            original_prompt = _get_arc_state(
                verification_target_id, "original_prompt", "",
            )
            source_dir = _get_arc_state(verification_target_id, "source_dir", "")
            revised_prompt = templates.render(
                "verification_feedback",
                original_prompt=original_prompt,
                retry_count=retry_count,
                max_retries=max_retries,
                verification_feedback=verification_feedback,
            )

            # Clear verification pending flag (new cycle will set it again)
            _set_arc_state(verification_target_id, "_verification_pending", False)

            # Determine the correct invoke-agent event type based on target arc
            target_arc = arc_manager.get_arc(verification_target_id)
            target_name = target_arc.get("name", "") if target_arc else ""
            if target_name.startswith(f"external-{CODING_CHANGE_PREFIX}"):
                invoke_event = f"external-{CODING_CHANGE_PREFIX}.invoke-agent"
            else:
                invoke_event = f"{CODING_CHANGE_PREFIX}.invoke-agent"

            # Re-enqueue coding agent
            work_queue.enqueue(
                invoke_event,
                {
                    "arc_id": verification_target_id,
                    "source_dir": source_dir,
                    "prompt": revised_prompt,
                    "coding_agent": _get_arc_state(
                        verification_target_id, "coding_agent",
                    ),
                },
                idempotency_key=f"{CODING_CHANGE_PREFIX}-vrework-{verification_target_id}-{int(time.time())}",
                max_retries=work_queue.SINGLE_ATTEMPT,
            )

            _notify_chat(
                verification_target_id,
                f"Verification failed (attempt {retry_count}/{max_retries}). "
                f"Re-invoking coding agent with feedback...",
            )

            # Mark judge completed
            arc_manager.update_status(arc_id, "completed")
            arc_manager.freeze_arc(arc_id)

            logger.info(
                "Verification rework %d/%d for target arc %d",
                retry_count, max_retries, verification_target_id,
            )
        else:
            # Rework limit reached — proceed to human review with failure noted
            retry_state = arc_retry.get_retry_state(verification_target_id)
            retry_count = retry_state.get("_retry_count", 0)
            logger.warning(
                "Verification rework limit reached for target arc %d "
                "(%d attempts). Proceeding to human review.",
                verification_target_id, retry_count,
            )

            # Cancel the docs arc (verification failed, skip docs)
            for sib in siblings:
                if sib["name"] == _get_vname("documentation") and sib["status"] == "pending":
                    arc_manager.update_status(sib["id"], "cancelled")

            # Clear verification pending flag
            _set_arc_state(verification_target_id, "_verification_pending", False)

            # Transition coding-change arc to waiting for human review
            target_arc = arc_manager.get_arc(verification_target_id)
            if target_arc and target_arc["status"] == "active":
                arc_manager.update_status(verification_target_id, "waiting")

            _notify_chat(
                verification_target_id,
                f"AI verification failed after {retry_count} rework attempts. "
                f"Proceeding to human review.\n\n"
                f"Verification issues:\n{chr(10).join(feedback_parts)}",
            )

            # Mark judge completed
            arc_manager.update_status(arc_id, "completed")
            arc_manager.freeze_arc(arc_id)


def _handle_failed_docs_arc(arc_id: int) -> None:
    """Handle a failed post-verification-docs arc.

    When the docs arc fails, unblock the target arc so the pipeline
    doesn't deadlock. Documentation is non-blocking — the target proceeds
    to human review (or external post-verification) without docs.
    """
    from ..workflows.coding_change_handler import _set_arc_state, _notify_chat
    from .verification import get_arc_name as _get_vname_docs

    arc_info = arc_manager.get_arc(arc_id)
    if arc_info is None:
        return
    if arc_info.get("name") != _get_vname_docs("documentation"):
        return

    verification_target_id = arc_info.get("verification_target_id")
    if verification_target_id is None:
        return

    target_arc = arc_manager.get_arc(verification_target_id)
    if target_arc is None:
        return

    # Clear verification pending flag
    _set_arc_state(verification_target_id, "_verification_pending", False)

    target_name = target_arc.get("name", "")

    if target_name.startswith(f"external-{CODING_CHANGE_PREFIX}"):
        # External target: enqueue post-verification step directly
        _enqueue_ext_post_verification(verification_target_id, target_arc)
    elif target_arc["status"] == "active":
        # Standard coding-change: transition to waiting for human review
        arc_manager.update_status(verification_target_id, "waiting")

    _notify_chat(
        verification_target_id,
        "Documentation generation failed (non-blocking). "
        "Proceeding to human review.",
    )
    logger.info(
        "post-verification-docs arc %d failed — unblocking target arc %d",
        arc_id, verification_target_id,
    )


def _enqueue_ext_post_verification(arc_id: int, arc_info: dict) -> None:
    """Enqueue the next step for an external-coding-change after verification.

    Based on the local_review arc_state flag, enqueue either local-review
    or push-and-pr.
    """
    from ..workflows.coding_change_handler import _get_arc_state

    local_review = _get_arc_state(arc_id, "local_review", False)
    if local_review:
        event_type = f"external-{CODING_CHANGE_PREFIX}.local-review"
    else:
        event_type = f"external-{CODING_CHANGE_PREFIX}.push-and-pr"

    work_queue.enqueue(
        event_type,
        {"arc_id": arc_id},
        idempotency_key=f"ext-cc-post-verify-{arc_id}-{int(time.time())}",
    )
    logger.info(
        "Enqueued %s for external-coding-change arc %d after verification",
        event_type, arc_id,
    )


def _handle_docs_completed(arc_id: int, arc_info: dict) -> None:
    """Handle post-verification-docs arc completion.

    After docs finishes, transition the target coding-change arc to "waiting"
    for human approval, and clear the verification_pending flag.

    For external-coding-change targets, enqueue the post-verification step
    instead of transitioning to "waiting".
    """
    from ..workflows.coding_change_handler import _set_arc_state, _notify_chat

    verification_target_id = arc_info.get("verification_target_id")
    if verification_target_id is None:
        return

    target_arc = arc_manager.get_arc(verification_target_id)
    if target_arc is None:
        return

    # Clear verification pending flag
    _set_arc_state(verification_target_id, "_verification_pending", False)

    target_name = target_arc.get("name", "")

    # External-coding-change targets: enqueue post-verification step
    if target_name.startswith(f"external-{CODING_CHANGE_PREFIX}"):
        _enqueue_ext_post_verification(verification_target_id, target_arc)
        _notify_chat(
            verification_target_id,
            "AI verification and documentation complete. "
            "Proceeding to push and PR.",
        )
        logger.info(
            "post-verification-docs completed for external target arc %d — "
            "enqueued post-verification step",
            verification_target_id,
        )
        return

    # Standard coding-change: transition to waiting for human approval
    if target_arc["status"] == "active":
        arc_manager.update_status(verification_target_id, "waiting")
        review_url = None
        with db_connection() as db:
            row = db.execute(
                "SELECT value_json FROM arc_state "
                "WHERE arc_id = ? AND key = 'review_url'",
                (verification_target_id,),
            ).fetchone()
            if row:
                review_url = json.loads(row["value_json"])

        _notify_chat(
            verification_target_id,
            "AI verification and documentation complete. "
            "Ready for human review."
            + (f"\nReview: {review_url}" if review_url else ""),
        )
        logger.info(
            "post-verification-docs completed for target arc %d — "
            "transitioned to waiting for human approval",
            verification_target_id,
        )


def _clone_arc_for_cron(
    original_arc_id: int, original_info: dict, conversation_id: int | None
) -> int | None:
    """Create a fresh copy of a completed arc for a recurring cron dispatch.

    Recurring crons reference a static arc_id. After the first execution the
    arc is completed, so subsequent fires need a fresh arc with the same
    goal and configuration. The clone is standalone (no parent) to avoid
    entanglement with the original arc's completed parent.

    If the original arc was a PLANNER with children, we also clone the
    children so the recurring dispatch actually executes them.
    """
    try:
        new_arc_id = arc_manager.create_arc(
            name=original_info.get("name", f"cron-repeat-{original_arc_id}"),
            goal=original_info.get("goal"),
            parent_id=None,  # Standalone — original parent may be completed
            integrity_level=original_info.get("integrity_level", "trusted"),
            output_type=original_info.get("output_type", "python"),
            agent_type=original_info.get("agent_type", "EXECUTOR"),
            agent_config_id=original_info.get("agent_config_id"),
            timeout_minutes=original_info.get("timeout_minutes"),
            _allow_tainted=True,  # Cron-spawned arcs may need untrusted level
        )
        if conversation_id:
            from ...agent.conversation import link_arc_to_conversation
            link_arc_to_conversation(conversation_id, new_arc_id)

        # If the original was a PLANNER with children, clone the children too.
        # Without this, cloned PLANNERs have no children and freeze immediately
        # without doing any work.
        original_children = arc_manager.get_children(original_arc_id)
        if original_children:
            for child in original_children:
                child_id = arc_manager.create_arc(
                    name=child.get("name", "cloned-child"),
                    goal=child.get("goal"),
                    parent_id=new_arc_id,
                    integrity_level=child.get("integrity_level", "trusted"),
                    output_type=child.get("output_type", "python"),
                    agent_type=child.get("agent_type", "EXECUTOR"),
                    agent_config_id=child.get("agent_config_id"),
                    timeout_minutes=child.get("timeout_minutes"),
                    _allow_tainted=True,
                )
                if conversation_id:
                    link_arc_to_conversation(conversation_id, child_id)

        logger.info(
            "Cloned completed arc %d -> new arc %d for cron repeat "
            "(%d children cloned)",
            original_arc_id, new_arc_id, len(original_children),
        )
        return new_arc_id
    except (ValueError, sqlite3.Error, KeyError) as _exc:
        logger.exception(
            "Failed to clone arc %d for cron repeat", original_arc_id
        )
        return None


def _try_inject_fetched_content(reviewer_arc_id: int) -> bool:
    """Check if a REVIEWER arc's review target has fetched web content.

    If the target EXECUTOR arc has encrypted ``fetched_content`` in its state,
    decrypt it and store the web page content as ``_agent_response`` on the
    REVIEWER arc.  This allows the fetch pipeline to bypass the LLM-based
    review step (which cannot access cross-arc encrypted state from the
    sandbox).

    Returns True if content was injected (caller should skip LLM invocation).
    """
    from ..workflows._arc_state import get_arc_state, set_arc_state

    review_target = get_arc_state(reviewer_arc_id, "_review_target")
    if review_target is None:
        return False

    # Read the raw value_json to check for encryption marker
    with db_connection() as db:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = ?",
            (review_target, "fetched_content"),
        ).fetchone()

    if row is None:
        return False

    value_json = row["value_json"]
    encrypted_marker = "__encrypted__:"

    # Check if it's encrypted
    if not value_json.startswith(f'"{encrypted_marker}'):
        # Plaintext — just read the value directly
        try:
            content_obj = json.loads(value_json)
        except (json.JSONDecodeError, TypeError):
            return False
    else:
        # Encrypted — decrypt using the reviewer's key
        try:
            raw = json.loads(value_json)  # -> "__encrypted__:BASE64..."
            ciphertext = raw[len(encrypted_marker):].encode("ascii")
            from ..trust.encryption import decrypt_for_reviewer
            decrypted_json = decrypt_for_reviewer(
                reviewer_arc_id, review_target, ciphertext,
            )
            content_obj = json.loads(decrypted_json)
        except (PermissionError, json.JSONDecodeError, ImportError,
                TypeError, ValueError, RuntimeError) as exc:
            logger.warning(
                "Arc %d: failed to decrypt fetched_content from target %d: %s",
                reviewer_arc_id, review_target, exc,
            )
            return False

    # Extract the web page content from the fetch result.
    # Store the FULL content — the notification handler truncates for
    # display and nudges the chat agent to use read_arc_result() for
    # the complete output.
    if isinstance(content_obj, dict):
        page_content = content_obj.get("content", "")
        url = content_obj.get("url", "")
        if url:
            result = f"Fetched from {url}:\n{page_content}"
        else:
            result = page_content
    elif isinstance(content_obj, str):
        result = content_obj
    else:
        result = str(content_obj)

    set_arc_state(reviewer_arc_id, "_agent_response", result)
    logger.info(
        "Arc %d: injected %d chars of fetched content from target arc %d",
        reviewer_arc_id, len(result), review_target,
    )
    return True


def _find_arc_conversation(arc_id: int, _depth: int = 0) -> int | None:
    """Walk up the arc family tree to find a linked conversation.

    Checks conversation_arcs for the arc and its ancestors (up to depth 10).

    Joins against the conversations table to ensure the referenced
    conversation still exists (orphaned conversation_arcs rows are
    skipped).  When multiple links exist, the most recently created
    link is preferred -- this handles arcs that were re-linked to a
    new conversation after an earlier link became stale.
    """
    if _depth > 10:
        return None
    with db_connection() as db:
        row = db.execute(
            "SELECT ca.conversation_id "
            "FROM conversation_arcs ca "
            "JOIN conversations c ON c.id = ca.conversation_id "
            "WHERE ca.arc_id = ? "
            "ORDER BY ca.created_at DESC LIMIT 1",
            (arc_id,),
        ).fetchone()
        if row:
            return row["conversation_id"]
        # Walk up to parent
        arc = db.execute(
            "SELECT parent_id FROM arcs WHERE id = ?", (arc_id,)
        ).fetchone()
        parent_id = arc["parent_id"] if arc else None

    if parent_id:
        return _find_arc_conversation(parent_id, _depth + 1)
    return None


async def _run_judge_checks(arc_id: int) -> None:
    """Run deterministic policy checks for a JUDGE arc.

    Reads the reviewer's structured output, runs policy validations,
    and promotes or rejects the target arc based on results.
    """
    from ...security.judge import run_policy_checks
    from ..workflows import review_manager

    try:
        result = run_policy_checks(arc_id)

        # Find the review target
        from ...security.judge import _get_review_target
        target_arc_id = _get_review_target(arc_id)

        if target_arc_id is None:
            logger.warning("JUDGE arc %d has no review target", arc_id)
            return

        if result.approved:
            logger.info(
                "JUDGE arc %d approved target %d (%d checks passed)",
                arc_id, target_arc_id, len(result.checks),
            )
            # Submit approval verdict through review manager
            review_manager.submit_verdict(
                reviewer_arc_id=arc_id,
                target_arc_id=target_arc_id,
                decision="approve",
                reason=result.reason or "All policy checks passed",
            )
        else:
            logger.info(
                "JUDGE arc %d rejected target %d: %s (%d/%d checks failed)",
                arc_id, target_arc_id, result.reason,
                len(result.failed_checks), len(result.checks),
            )
            # Submit rejection verdict
            review_manager.submit_verdict(
                reviewer_arc_id=arc_id,
                target_arc_id=target_arc_id,
                decision="reject",
                reason=result.reason or "Policy check(s) failed",
            )
    except Exception:  # broad catch: policy checks may involve plugin code
        logger.exception("JUDGE policy checks failed for arc %d", arc_id)


async def _run_arc_agent(
    arc_id: int, goal: str, source_conv_id: int,
    agent_config: dict | None = None,
) -> None:
    """Invoke the chat agent to execute an arc's goal.

    Creates an ephemeral conversation, invokes the agent with the arc's
    goal and execution context (arc_id + target conversation_id), then
    archives the ephemeral conversation.

    Args:
        agent_config: Optional agent_configs row dict with 'model', 'agent_role',
                      'temperature', 'max_tokens'. When present, overrides the
                      default model for this invocation.
    """
    from ...agent import conversation as conv_module, invocation, templates

    arc_conv_id = conv_module.create_conversation()
    conv_module.set_conversation_title(
        arc_conv_id, f"[Arc #{arc_id}] {goal[:50]}"
    )

    message = templates.render(
        "arc_execute",
        arc_id=arc_id,
        goal=goal,
        source_conv_id=source_conv_id,
    )
    conv_module.add_message(arc_conv_id, "user", message)

    # Extract model override from agent config
    model_override = agent_config.get("model") if agent_config else None

    from ... import thread_pools
    try:
        result = await thread_pools.run_in_work_pool(
            invocation.invoke_for_chat,
            message,
            conversation_id=arc_conv_id,
            _message_already_saved=True,
            _system_triggered=True,
            _executor_arc_id=arc_id,
            _executor_conv_id=source_conv_id,
            _model_override=model_override,
        )
        # Store agent response for downstream steps to access
        response_text = ""
        if isinstance(result, dict):
            response_text = result.get("response_text", "")
        if response_text:
            _db = get_db()
            try:
                _db.execute(
                    "INSERT OR REPLACE INTO arc_state (arc_id, key, value_json) "
                    "VALUES (?, ?, ?)",
                    (arc_id, "_agent_response", json.dumps(response_text)),
                )
                _db.commit()
            finally:
                _db.close()
    except Exception:  # broad catch: AI agent invocation may raise anything
        logger.exception("Arc agent invocation failed for arc %d", arc_id)
    finally:
        conv_module.archive_conversation(arc_conv_id)


def _propagate_completion(arc_id: int) -> None:
    """After arc_id finishes, enqueue its next ready sibling and complete parent if done.

    For leaf arcs (executors), this looks up the parent and:
    1. Enqueues the next pending sibling whose dependencies are now met.
    2. Calls freeze_arc(parent_id) so the parent transitions to completed/failed
       once all its children are frozen.

    For arcs that themselves have children (planners just frozen), also scan
    their own children for any that are ready.
    """
    arc_info = arc_manager.get_arc(arc_id)
    parent_id = arc_info.get("parent_id") if arc_info else None

    if parent_id is not None:
        # Enqueue the next ready sibling under the same parent
        _enqueue_ready_children(parent_id)
        # Potentially complete (or fail) the parent now that a child has finished
        try:
            arc_manager.freeze_arc(parent_id)
        except (ValueError, sqlite3.Error) as _exc:
            logger.exception(
                "Failed to re-evaluate parent arc %d after child %d completed",
                parent_id, arc_id,
            )
    else:
        # Top-level arc — scan its own children
        _enqueue_ready_children(arc_id)


def _check_review_verdicts(child: dict, all_siblings: list[dict]) -> bool:
    """Check if a child arc is blocked by a failed required_pass sibling.

    For each preceding completed sibling with _required_pass=True in arc_state,
    checks the _verdict arc_state key. If the verdict is not "pass", the child
    is blocked.

    A required_pass step that completed without setting _verdict defaults to
    pass (successful completion implies pass).

    Args:
        child: The candidate child arc to check.
        all_siblings: All sibling arcs under the same parent.

    Returns:
        True if the child is allowed to proceed, False if blocked.
    """
    child_order = child.get("step_order", 0)

    for sib in all_siblings:
        sib_order = sib.get("step_order", 0)
        if sib_order >= child_order:
            continue
        if sib.get("status") not in ("completed", "frozen"):
            continue

        # Check if this sibling has _required_pass
        with db_connection() as db:
            row = db.execute(
                "SELECT value_json FROM arc_state "
                "WHERE arc_id = ? AND key = '_required_pass'",
                (sib["id"],),
            ).fetchone()

        if not row:
            continue

        required_pass = json.loads(row["value_json"])
        if not required_pass:
            continue

        # Check verdict — default to "pass" if not set (completion implies pass)
        with db_connection() as db:
            verdict_row = db.execute(
                "SELECT value_json FROM arc_state "
                "WHERE arc_id = ? AND key = '_verdict'",
                (sib["id"],),
            ).fetchone()

        if verdict_row:
            verdict_data = json.loads(verdict_row["value_json"])
            # verdict_data may be a string or a dict with "verdict" key
            if isinstance(verdict_data, dict):
                verdict = verdict_data.get("verdict", "pass")
            else:
                verdict = str(verdict_data)

            if verdict != "pass":
                logger.info(
                    "Arc %d blocked: preceding required_pass sibling %d "
                    "(step_order=%d) has verdict=%r",
                    child["id"], sib["id"], sib_order, verdict,
                )
                return False

    return True


def _enqueue_ready_children(parent_id: int):
    """Check children of the given parent and enqueue any that are ready.

    A child is ready if:
    - Status is 'pending'
    - All preceding siblings (lower step_order) are completed
    - Activation conditions are met (if any)
    - wait_until is not set or is in the past
    - No preceding required_pass sibling has a non-pass verdict
    """
    from datetime import datetime, timezone

    children = arc_manager.get_children(parent_id)

    for child in children:
        if child["status"] != "pending":
            continue

        # Skip children with a future wait_until — heartbeat will catch them later
        wait_until = child.get("wait_until")
        if wait_until:
            try:
                wait_dt = datetime.fromisoformat(wait_until)
                if wait_dt.tzinfo is None:
                    wait_dt = wait_dt.replace(tzinfo=timezone.utc)
                if wait_dt > datetime.now(timezone.utc):
                    continue
            except (ValueError, TypeError):
                pass

        # Check dependencies
        if not arc_manager.check_dependencies(child["id"]):
            continue

        # Check activation conditions
        if not arc_manager.check_activation(child["id"]):
            continue

        # Check review verdicts — block if a required_pass sibling failed
        if not _check_review_verdicts(child, children):
            continue

        # This child is ready — enqueue for dispatch
        work_queue.enqueue(
            "arc.dispatch",
            {"arc_id": child["id"]},
            idempotency_key=f"arc_dispatch:{child['id']}",
        )
        logger.info("Enqueued arc %d for dispatch (child of %d)", child["id"], parent_id)

        # Wake the main loop for faster processing
        main_loop.wake_signal.set()


def scan_for_ready_arcs():
    """Heartbeat hook: scan for pending arcs whose dependencies are satisfied.

    This is a safety net in case enqueue_ready_children misses any arcs.
    Checks all pending arcs and enqueues those that are ready.

    Called every ~5 seconds from main_loop heartbeat.
    """
    with db_connection() as db:
        # Find all pending arcs — exclude coding-change arcs, which are managed
        # exclusively by coding_change_handler via its own work queue events.
        # Also skip arcs with a future wait_until timestamp (heartbeat guard).
        rows = db.execute(
            "SELECT id, parent_id FROM arcs "
            "WHERE status = 'pending' "
            f"AND (name IS NULL OR name NOT LIKE '{CODING_CHANGE_PREFIX}%') "
            "AND (wait_until IS NULL OR wait_until <= datetime('now'))"
        ).fetchall()

    for row in rows:
        arc_id = row["id"]

        # Check if arc is already enqueued
        with db_connection() as db:
            existing = db.execute(
                "SELECT id FROM work_queue "
                "WHERE event_type = 'arc.dispatch' "
                "AND payload_json = ? "
                "AND status IN ('pending', 'claimed')",
                (json.dumps({"arc_id": arc_id}),),
            ).fetchone()

        if existing:
            continue  # Already enqueued

        # Check if dependencies are satisfied
        if not arc_manager.check_dependencies(arc_id):
            continue

        # Check activation conditions
        if not arc_manager.check_activation(arc_id):
            continue

        # This arc is ready but not enqueued — enqueue it
        work_queue.enqueue(
            "arc.dispatch",
            {"arc_id": arc_id},
            idempotency_key=f"arc_dispatch:{arc_id}",
        )
        logger.info("Heartbeat: enqueued ready arc %d for dispatch", arc_id)


def _extract_error_info(arc_id: int, exception: Exception) -> error_classifier.ErrorInfo:
    """Extract ErrorInfo from arc's conversation messages or exception.

    1. Query messages WHERE arc_id = ? AND role = 'system'
    2. Parse content_json for error_info
    3. If found: deserialize to ErrorInfo
    4. If not: classify exception directly
    5. Return ErrorInfo for retry decision

    Args:
        arc_id: The arc that failed
        exception: The exception that was caught

    Returns:
        ErrorInfo for retry decision logic
    """
    with db_connection() as db:
        # Try to find error message in conversation
        conv_id = db.execute(
            "SELECT conversation_id FROM conversation_arcs WHERE arc_id = ?",
            (arc_id,)
        ).fetchone()

        if conv_id:
            error_msg = db.execute(
                "SELECT content_json FROM messages "
                "WHERE conversation_id = ? AND role = 'system' "
                "AND content_json LIKE '%error_info%' "
                "ORDER BY created_at DESC LIMIT 1",
                (conv_id[0],)
            ).fetchone()

            if error_msg and error_msg[0]:
                try:
                    data = json.loads(error_msg[0])
                    if "error_info" in data:
                        # Reconstruct ErrorInfo from JSON
                        return error_classifier.ErrorInfo(**data["error_info"])
                except (json.JSONDecodeError, TypeError, KeyError):
                    pass  # Fall through to exception classification

    # Fallback: classify exception directly
    # Try to get retry count from arc state
    retry_state = arc_retry.get_retry_state(arc_id)
    retry_count = retry_state.get("_retry_count", 0) + 1

    return error_classifier.classify_error(
        exception,
        retry_count=retry_count,
        model=None,
        provider=None,
    )


def _reenqueue_arc_dispatch(arc_id: int, backoff_seconds: float) -> None:
    """Create new arc.dispatch work item with delay.

    Uses work_queue.enqueue() with scheduled execution time.

    Args:
        arc_id: The arc to retry
        backoff_seconds: How long to wait before retrying
    """
    from datetime import datetime, timedelta, timezone

    # Schedule for future execution
    scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)

    work_queue.enqueue(
        event_type="arc.dispatch",
        payload={"arc_id": arc_id},
        idempotency_key=f"arc_dispatch_{arc_id}_{int(time.time())}",  # Unique per attempt
        max_retries=work_queue.SINGLE_ATTEMPT,
        scheduled_at=scheduled_at.isoformat(),
    )


def _fire_connectivity_degraded(arc_id: int) -> None:
    """Fire connectivity degraded event and set arc to waiting for retry."""
    try:
        event_bus.record_event("system.connectivity_degraded", {"arc_id": arc_id})
    except (sqlite3.Error, ValueError) as _exc:
        pass
    try:
        notifications.notify(
            f"No capable models available for arc #{arc_id}. Will retry automatically.",
            priority="normal",
            category="connectivity",
        )
    except Exception:  # broad catch: notification delivery may raise anything
        pass
    try:
        arc_manager.update_status(arc_id, "waiting")
    except (ValueError, sqlite3.Error) as _exc:
        logger.exception("Failed to set arc %d to waiting after connectivity degraded", arc_id)


def register_handlers(register_fn):
    """Register arc dispatch handlers and heartbeat hook.

    Args:
        register_fn: The main_loop.register_handler function.
    """
    register_fn("arc.dispatch", handle_arc_dispatch)
    main_loop.register_heartbeat_hook(scan_for_ready_arcs)
    logger.info("Arc dispatch handler and heartbeat hook registered")

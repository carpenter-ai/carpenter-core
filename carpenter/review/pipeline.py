"""Security review pipeline for submitted code.

Orchestrates: verification check -> hash check -> AST parse -> injection scan ->
sanitize -> reviewer AI call. Verification is the sole machine authority;
LLM reviewer runs for all code as advisory defense-in-depth.
"""

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from enum import Enum

from .profiles import ReviewProfile, PROFILE_STEP
from .static_analyzer import (
    validate_syntax,
    extract_comments_and_strings,
    check_import_star,
)
from .injection_defense import (
    analyze_injection_risk,
    analyze_histogram_with_llm,
    run_progressive_text_review,
)
from ..verify.string_declarations import extract_unstructured_text_values
from .code_sanitizer import sanitize_for_review
from .code_reviewer import review_code, review_code_adversarial, review_code_for_intent, ReviewResult
from .. import config as config_mod
from ..agent import conversation as conversation_mod

logger = logging.getLogger(__name__)


class ReviewOutcome(Enum):
    """Standardized review outcomes."""

    APPROVE = "approve"  # Code is safe, proceed with execution
    REWORK = "rework"  # Fixable issues (syntax, logic, style, medium-risk), agent fixes (max 3 attempts)
    MAJOR = "major"  # Security concern or significant deviation, requires human decision
    REJECTED = "rejected"  # Policy violation (e.g., import *), no retry allowed


@dataclass
class PipelineResult:
    """Result from the full review pipeline."""

    status: str  # "approved", "minor_concern", "major_alert", "syntax_error", "cached_approval", "rejected"
    reason: str  # Empty for approved/cached, explanation otherwise
    sanitized_code: str  # Sanitized code shown to reviewer (empty for cached/syntax_error)
    advisory_flags: list[str]  # Injection defense findings
    outcome: ReviewOutcome | None = None  # Standardized outcome (None for legacy/cached)
    review_result: ReviewResult | None = None  # Full reviewer result (includes findings in adversarial mode)


# Per-conversation approval cache: conv_id -> set of approved code hashes.
# In-memory only — re-review after server restart is acceptable.
_approval_cache: dict[int, set[str]] = {}


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def is_previously_approved(conversation_id: int, code: str) -> bool:
    """Check if identical code was already approved in this conversation."""
    return _code_hash(code) in _approval_cache.get(conversation_id, set())


def record_approval(conversation_id: int, code: str) -> None:
    """Record an approved code hash for this conversation."""
    _approval_cache.setdefault(conversation_id, set()).add(_code_hash(code))


def clear_cache(conversation_id: int | None = None) -> None:
    """Clear approval cache. If conversation_id given, clear only that conversation."""
    if conversation_id is not None:
        _approval_cache.pop(conversation_id, None)
    else:
        _approval_cache.clear()


def _post_advisory_warning(conversation_id: int, review_result: ReviewResult) -> None:
    """Post LLM MAJOR findings to chat when verified code has LLM concerns.

    This is defense-in-depth: the code is machine-verified but the LLM
    reviewer flagged something worth human attention.
    """
    try:
        msg = (
            f"[Advisory] Verified code had LLM reviewer concerns: "
            f"{review_result.reason}"
        )
        conversation_mod.add_message(conversation_id, "system", msg)
        logger.info("Posted advisory warning for verified code: %s", review_result.reason)
    except Exception:  # broad catch: review pipeline may raise anything
        logger.debug("Could not post advisory warning", exc_info=True)


def _post_advisory_summary(conversation_id: int, review_result: ReviewResult) -> None:
    """Post LLM review summary for human reviewer context."""
    try:
        msg = (
            f"[Review Summary] LLM reviewer verdict: {review_result.status}. "
            f"{review_result.reason}"
        )
        conversation_mod.add_message(conversation_id, "system", msg)
    except (sqlite3.Error, ValueError) as _exc:
        logger.debug("Could not post advisory summary", exc_info=True)


def run_review_pipeline(
    code: str,
    conversation_id: int,
    profile: ReviewProfile = PROFILE_STEP,
    arc_id: int | None = None,
) -> PipelineResult:
    """Execute the review pipeline on submitted code.

    Which stages run is determined by ``profile``.  Two built-in profiles:

    PROFILE_PLANNER (chat agent / planner orchestration code)
        Lightweight: syntax check, import-star check, intent-alignment LLM review.
        Formal verification is skipped — planner string literals are T by definition.

    PROFILE_STEP (arc step agent or tainted-context code)
        Full pipeline: formal verification (whitelist, string declarations, taint
        analysis, dry-run) then security-focused LLM review with sanitisation.

    Steps run in both profiles:
    1. Hash check — skip review if identical code was already approved
    2. Import-star check — auto-reject policy violations  (profile.check_import_star)
    3. Syntax validation                                  (profile.check_syntax)

    PROFILE_STEP only:
    1b. Verified flow analysis (whitelist / string declarations / taint / dry-run)

    PROFILE_PLANNER (intent_review_only=True):
    4. Intent-alignment LLM review — is the code doing what was asked?

    PROFILE_STEP (intent_review_only=False):
    4. Injection pattern scan — advisory flags (non-blocking)
    5. Code sanitisation — strip strings, rename variables
    6. Security-focused LLM review

    Both profiles:
    7. Determine standardised outcome from verification + LLM results.

    Args:
        code: Python source code to review.
        conversation_id: Conversation ID for context and cache scoping.
        profile: Which checks to run.  Defaults to PROFILE_STEP (full pipeline).
        arc_id: Optional arc ID for context-aware reviewer model selection.

    Returns:
        PipelineResult with status and standardised outcome.
    """
    # Step 1: Hash check for previously approved code
    if is_previously_approved(conversation_id, code):
        logger.info("Code hash matches previous approval — skipping review")
        return PipelineResult(
            status="cached_approval",
            reason="",
            sanitized_code="",
            advisory_flags=[],
            outcome=None,  # Cached approvals don't have standardized outcomes
        )

    # Step 1b: Formal verification (CaMeL whitelist / string declarations / taint).
    # Only runs for profiles that declare run_formal_verification=True (e.g.
    # PROFILE_STEP).  Planner code skips this entirely — its string literals are
    # T by definition and require no CaMeL taint analysis.
    verification_cfg = config_mod.CONFIG.get("verification", {})
    verification_result = None
    if profile.run_formal_verification and verification_cfg.get("enabled", True):
        try:
            from ..verify import verify_code
            verification_result = verify_code(code)

            if verification_result.hard_reject:
                # Code fails CaMeL verification and must be restructured.
                return PipelineResult(
                    status="rejected",
                    reason=verification_result.reason,
                    sanitized_code="",
                    advisory_flags=verification_result.violations,
                    outcome=ReviewOutcome.REJECTED,
                )
        except Exception:  # broad catch: verification involves imports + AST analysis
            logger.exception("Verification system error — failing closed to human review")
            # verification_result stays None — treated as "not verifiable"

    # Step 2: Import star check (policy violation - auto-reject)
    import_star_result = check_import_star(code)
    if import_star_result["violation"]:
        violations = "; ".join(
            f"line {f['line']}: {f['message']}"
            for f in import_star_result["findings"]
        )
        return PipelineResult(
            status="rejected",
            reason=f"Policy violation: {violations}",
            sanitized_code="",
            advisory_flags=[],
            outcome=ReviewOutcome.REJECTED,
        )

    # Step 3: Syntax validation
    syntax_result = validate_syntax(code)
    if not syntax_result["valid"]:
        errors = "; ".join(
            f"line {e['line']}: {e['message']}" for e in syntax_result["errors"]
        )
        return PipelineResult(
            status="minor_concern",  # Syntax errors are fixable → REWORK
            reason=f"Syntax errors: {errors}",
            sanitized_code="",
            advisory_flags=[],
            outcome=ReviewOutcome.REWORK,
        )

    messages = conversation_mod.get_messages(conversation_id)

    if profile.intent_review_only:
        # Intent-alignment path (PROFILE_PLANNER and similar).
        # Skip injection scanning and sanitisation; reviewer sees the raw source
        # and focuses only on intent alignment — is the code doing what was asked?
        injection_flags: list = []
        advisory_flags: list[str] = []
        sanitized_code = code  # "sanitized" == original for interface compat
        review_result: ReviewResult = review_code_for_intent(code, messages, arc_id=arc_id)
    else:
        # Full security review path (PROFILE_STEP and similar).

        # Step 4: Injection pattern scan (advisory, not blocking)
        extracted = extract_comments_and_strings(code)
        injection_result = analyze_injection_risk(code, extracted)
        injection_flags = injection_result.get("findings", [])
        advisory_flags = [
            f"[{f['severity']}] {f['description']} (in {f['source']})"
            for f in injection_flags
        ]

        # Step 4b: Histogram LLM analysis (advisory, non-blocking)
        histogram_flags = analyze_histogram_with_llm(extracted)
        advisory_flags.extend(histogram_flags)

        # Step 4c: Progressive text review (only when UnstructuredText strings exist)
        ptr_cfg = config_mod.CONFIG.get("review", {}).get("progressive_text_review", {})
        if ptr_cfg.get("enabled", True):
            unstructured_texts = extract_unstructured_text_values(code)
            if unstructured_texts:
                ptr_escalate, ptr_flags = run_progressive_text_review(unstructured_texts)
                advisory_flags.extend(ptr_flags)
                if ptr_escalate:
                    return PipelineResult(
                        status="major_alert",
                        reason=(
                            "Progressive text review flagged suspected prompt injection "
                            "in UnstructuredText strings — human review required."
                        ),
                        sanitized_code="",
                        advisory_flags=advisory_flags,
                        outcome=ReviewOutcome.MAJOR,
                    )

        # Step 5: Sanitize code for reviewer
        try:
            sanitized_code, sanitizer_notes = sanitize_for_review(code)
        except Exception as e:  # broad catch: sanitizer involves AST transforms
            logger.exception("Code sanitization failed")
            return PipelineResult(
                status="minor_concern",
                reason=f"Code sanitization failed: {e}",
                sanitized_code="",
                advisory_flags=advisory_flags,
                outcome=ReviewOutcome.REWORK,
            )

        if sanitizer_notes:
            advisory_flags.extend(f"[sanitizer] {note}" for note in sanitizer_notes)

        # Step 6: Reviewer AI call (standard or adversarial mode)
        review_config = config_mod.CONFIG.get("review", {})
        adversarial = review_config.get("adversarial_mode", False)

        if adversarial:
            review_result = review_code_adversarial(
                sanitized_code, messages, advisory_flags, arc_id=arc_id,
            )
        else:
            review_result = review_code(
                sanitized_code, messages, advisory_flags, arc_id=arc_id,
            )

    # Step 7: Determine outcome using verification + LLM review
    llm_outcome = determine_outcome(
        syntax_valid=True,  # We already checked this above
        import_star_violation=False,  # Already checked
        injection_flags=injection_flags,
        ai_review_result=review_result.status,
        ai_review_reason=review_result.reason,
    )

    # Verification determines the final outcome; LLM is advisory
    if verification_result is not None and verification_result.verified:
        # Machine-verified: auto-approve regardless of LLM verdict
        if llm_outcome == ReviewOutcome.MAJOR:
            _post_advisory_warning(conversation_id, review_result)
        record_approval(conversation_id, code)
        return PipelineResult(
            status="approved",
            reason="",
            sanitized_code=sanitized_code,
            advisory_flags=advisory_flags,
            outcome=ReviewOutcome.APPROVE,
            review_result=review_result,
        )

    # Not verifiable OR verification disabled: use LLM outcome but force
    # human review (MAJOR) for anything that isn't REJECTED
    if verification_result is not None and not verification_result.verified:
        # Verification ran but code is not verifiable — human review required
        _post_advisory_summary(conversation_id, review_result)
        if llm_outcome == ReviewOutcome.REJECTED:
            return PipelineResult(
                status="rejected",
                reason=review_result.reason,
                sanitized_code=sanitized_code,
                advisory_flags=advisory_flags,
                outcome=ReviewOutcome.REJECTED,
                review_result=review_result,
            )
        return PipelineResult(
            status="major_alert",
            reason=review_result.reason or "Code not verifiable — human review required",
            sanitized_code=sanitized_code,
            advisory_flags=advisory_flags,
            outcome=ReviewOutcome.MAJOR,
            review_result=review_result,
        )

    # verification_result is None (disabled or error) — fall back to LLM outcome
    outcome = llm_outcome

    # Map outcome to legacy status for backward compatibility
    if outcome == ReviewOutcome.APPROVE:
        record_approval(conversation_id, code)
        return PipelineResult(
            status="approved",
            reason="",
            sanitized_code=sanitized_code,
            advisory_flags=advisory_flags,
            outcome=outcome,
            review_result=review_result,
        )
    elif outcome == ReviewOutcome.REWORK:
        return PipelineResult(
            status="minor_concern",
            reason=review_result.reason,
            sanitized_code=sanitized_code,
            advisory_flags=advisory_flags,
            outcome=outcome,
            review_result=review_result,
        )
    elif outcome == ReviewOutcome.MAJOR:
        return PipelineResult(
            status="major_alert",
            reason=review_result.reason,
            sanitized_code=sanitized_code,
            advisory_flags=advisory_flags,
            outcome=outcome,
            review_result=review_result,
        )
    else:  # REJECTED
        return PipelineResult(
            status="rejected",
            reason=review_result.reason,
            sanitized_code=sanitized_code,
            advisory_flags=advisory_flags,
            outcome=outcome,
            review_result=review_result,
        )


def determine_outcome(
    syntax_valid: bool,
    import_star_violation: bool,
    injection_flags: list,
    ai_review_result: str,
    ai_review_reason: str = "",
) -> ReviewOutcome:
    """Determine standardized review outcome from all analysis results.

    Args:
        syntax_valid: Whether code has valid Python syntax
        import_star_violation: Whether code contains 'from X import *'
        injection_flags: List of injection pattern findings (from injection_defense)
        ai_review_result: AI reviewer status ("approve", "minor", "major")
        ai_review_reason: AI reviewer reason string

    Returns:
        ReviewOutcome enum value
    """
    # Import * violation → immediate rejection (no retry)
    if import_star_violation:
        return ReviewOutcome.REJECTED

    # Syntax error → REWORK (agent can fix with feedback)
    if not syntax_valid:
        return ReviewOutcome.REWORK

    # High-risk injection patterns → MAJOR
    high_severity_injection = any(
        f.get("severity") == "HIGH" for f in injection_flags
    )
    if high_severity_injection:
        return ReviewOutcome.MAJOR

    # AI reviewer found major issues → MAJOR
    if ai_review_result == "major":
        return ReviewOutcome.MAJOR

    # AI reviewer found minor issues → REWORK
    if ai_review_result == "minor":
        return ReviewOutcome.REWORK

    # Otherwise approve
    return ReviewOutcome.APPROVE

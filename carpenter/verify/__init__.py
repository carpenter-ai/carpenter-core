"""Verified flow analysis for Carpenter.

Public API: verify_code() orchestrates whitelist check, static taint
analysis, and dry-run verification. Returns a VerificationResult that
the review pipeline uses to determine the outcome.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field

from .whitelist import check_whitelist
from .string_declarations import check_string_declarations
from .tool_arg_types import check_tool_arg_types
from .taint import analyze_taint
from .dry_run import run_dry_run
from .hash_store import compute_code_hash, check_verified_hash, add_verified_hash

from .. import config as config_mod

logger = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    """Result of code verification."""

    verified: bool  # True = auto-approve
    hard_reject: bool  # True = code must be restructured (C in conditions + fails)
    reason: str
    violations: list[str] = field(default_factory=list)
    code_hash: str = ""
    policy_version: int = 0
    input_combinations: int = 0


def verify_code(code: str, arc_id: int | None = None) -> VerificationResult:
    """Verify code through whitelist, taint analysis, and dry-run.

    Steps:
    1. Hash check (fast path — previously verified + current policy)
    2. Whitelist check — fails: not verifiable (human review), NOT hard reject
    2b. String declaration check — all strings must be typed
    3. Static taint analysis
    4. If all trusted: store hash, return verified
    5. If C in conditions: enumerate inputs, check threshold
    6. Dry-run — passes: store hash, verified; fails: hard reject

    Args:
        code: Python source code to verify.
        arc_id: Arc submitting this code (for state.get label resolution).

    Returns:
        VerificationResult with verification status.
    """
    code_hash = compute_code_hash(code)
    verification_cfg = config_mod.CONFIG.get("verification", {})
    threshold = verification_cfg.get("threshold", 150)

    # Step 1: Hash check (fast path)
    cached = check_verified_hash(code_hash)
    if cached is not None:
        logger.debug("Code hash %s is verified (cached)", code_hash[:12])
        return VerificationResult(
            verified=True,
            hard_reject=False,
            reason="Previously verified (hash match)",
            code_hash=code_hash,
            policy_version=cached["policy_version"],
        )

    # Step 2: Whitelist check
    whitelist_result = check_whitelist(code)
    if not whitelist_result.passed:
        logger.debug("Whitelist check failed: %s", whitelist_result.violations)
        return VerificationResult(
            verified=False,
            hard_reject=False,  # Not verifiable, needs human review — NOT a hard reject
            reason="Code uses constructs outside the verifiable subset",
            violations=whitelist_result.violations,
            code_hash=code_hash,
        )

    # Step 2b: String declaration check — all strings must be typed
    decl_result = check_string_declarations(code)
    if not decl_result.passed:
        logger.debug("String declaration check failed: %s", decl_result.violations)
        return VerificationResult(
            verified=False,
            hard_reject=True,
            reason="Untyped string literals found",
            violations=decl_result.violations,
            code_hash=code_hash,
        )

    # Step 2c: Tool argument type check
    arg_type_result = check_tool_arg_types(code)
    if not arg_type_result.passed:
        logger.debug("Tool arg type check failed: %s", arg_type_result.violations)
        return VerificationResult(
            verified=False,
            hard_reject=True,
            reason="Tool arguments use wrong SecurityType",
            violations=arg_type_result.violations,
            code_hash=code_hash,
        )

    # Step 3: Static taint analysis
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return VerificationResult(
            verified=False,
            hard_reject=False,
            reason=f"Syntax error: {e}",
            code_hash=code_hash,
        )

    taint_result = analyze_taint(tree, arc_id=arc_id)

    if taint_result.violations:
        return VerificationResult(
            verified=False,
            hard_reject=True,
            reason="Taint analysis found policy violations",
            violations=taint_result.violations,
            code_hash=code_hash,
        )

    # Step 4: If all trusted — no C data in conditions
    if taint_result.all_trusted:
        from .hash_store import add_verified_hash as store_hash
        from ..security.policy_store import get_policy_version
        policy_version = get_policy_version()
        store_hash(code_hash, "[]", policy_version)
        logger.info("Code verified (all-trusted, no C in conditions)")
        return VerificationResult(
            verified=True,
            hard_reject=False,
            reason="All data in conditions is TRUSTED",
            code_hash=code_hash,
            policy_version=policy_version,
        )

    # Step 4b: Schema fallback — resolve untyped inputs from output_contract
    _resolve_untyped_inputs(taint_result.constrained_inputs)

    # Step 5-6: C data in conditions — dry-run verification
    constrained_inputs = [
        {
            "key": inp.key,
            "arc_id": inp.arc_id,
            "integrity_level": inp.integrity_level,
            "detected_type": inp.detected_type,
            "is_iterated": inp.is_iterated,
            "has_accumulator": inp.has_accumulator,
        }
        for inp in taint_result.constrained_inputs
    ]

    dry_result = run_dry_run(code, constrained_inputs, threshold=threshold)

    if dry_result.passed:
        from ..security.policy_store import get_policy_version
        policy_version = get_policy_version()
        add_verified_hash(code_hash, "[]", policy_version)
        logger.info(
            "Code verified via dry-run (%d combinations)",
            dry_result.input_combinations,
        )
        return VerificationResult(
            verified=True,
            hard_reject=False,
            reason=f"Dry-run passed ({dry_result.input_combinations} combinations)",
            code_hash=code_hash,
            policy_version=policy_version,
            input_combinations=dry_result.input_combinations,
        )
    else:
        return VerificationResult(
            verified=False,
            hard_reject=True,
            reason=dry_result.reason,
            violations=[dry_result.error_detail] if dry_result.error_detail else [],
            code_hash=code_hash,
            input_combinations=dry_result.input_combinations,
        )


def _resolve_untyped_inputs(constrained_inputs: list) -> None:
    """Attempt to resolve detected_type from Pydantic schema for untyped inputs.

    For each InputSpec with detected_type=None and a known arc_id, looks up
    the source arc's output_contract and checks the field metadata for
    policy_type annotations.

    Modifies InputSpec objects in place.
    """
    from ._schema import resolve_policy_type

    for inp in constrained_inputs:
        if inp.detected_type is not None:
            continue  # already resolved from comparison context
        if inp.arc_id is None:
            continue
        try:
            policy_type = resolve_policy_type(inp.arc_id, inp.key)
            if policy_type is not None:
                inp.detected_type = policy_type
                logger.debug(
                    "Resolved detected_type=%s for %s from arc %s output_contract",
                    policy_type, inp.key, inp.arc_id,
                )
        except Exception as _exc:  # broad catch: schema resolution involves DB + imports
            logger.debug("Could not resolve schema type for %s (arc %s)", inp.key, inp.arc_id)

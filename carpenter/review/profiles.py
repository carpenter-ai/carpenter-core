"""Review profiles — which pipeline stages run for each code submission context.

Two profiles ship with Carpenter:

PROFILE_PLANNER
    Used when the main chat agent (planner) submits orchestration code such
    as arc creation and workflow setup.  String literals in planner code are
    T by definition (the AI's own generation, not external data), so formal
    CaMeL verification adds no security value there.  This profile runs only
    the fast static checks (syntax, import-star) plus an intent-alignment
    LLM review.

PROFILE_STEP
    Used for arc step agents and any code submitted by a tainted (externally
    influenced) context.  The full pipeline applies: formal verification
    (whitelist, string declarations, taint analysis, dry-run) plus a
    security-focused LLM review with code sanitisation.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewProfile:
    """Declares which review pipeline stages run for a given submission context."""

    name: str

    # ── Static checks (fast, always applicable) ───────────────────────────────
    check_import_star: bool = True   # Reject `from x import *` policy violations
    check_syntax: bool = True        # Reject / rework on Python syntax errors

    # ── Formal verification (CaMeL taint / string declarations / dry-run) ────
    # Only meaningful for code that may handle externally-sourced data.
    # When False the verification block is skipped entirely and the LLM review
    # result determines the outcome.
    run_formal_verification: bool = False

    # ── LLM review mode ───────────────────────────────────────────────────────
    # True  → intent-alignment only: no sanitisation, reviewer sees raw source.
    # False → full security review: injection scan, sanitise, security-focused LLM.
    intent_review_only: bool = True


# ── Named profiles ─────────────────────────────────────────────────────────────

# Chat agent / planner submitting orchestration code (arc creation, scheduling…).
# All string literals are T (the AI's own generation); no external data is in
# scope.  Lightweight static checks + intent-alignment LLM review only.
PROFILE_PLANNER = ReviewProfile(
    name="planner",
    check_import_star=True,
    check_syntax=True,
    run_formal_verification=False,
    intent_review_only=True,
)

# Arc step agent or code submitted in a tainted (externally influenced) context.
# May process data that originated outside the platform, so the full CaMeL
# verification pipeline and a security-focused LLM review apply.
PROFILE_STEP = ReviewProfile(
    name="step",
    check_import_star=True,
    check_syntax=True,
    run_formal_verification=True,
    intent_review_only=False,
)

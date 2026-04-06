"""Review pipeline for Carpenter.

Multi-phase code review pipeline:
1. File type inspection (deterministic)
2. Syntax validation (deterministic)
3. AST flag analysis (deterministic, uses code_manager.ast_check)
4. Injection risk analysis (deterministic)
5. (Optional) Human review link creation
"""

from .static_analyzer import analyze_file_type, validate_syntax, extract_comments_and_strings
from .injection_defense import analyze_injection_risk
from ..core.code_manager import ast_check
from ..db import get_db, db_transaction


def run_review_pipeline(
    code_file_id: int,
    code: str,
    trust_tier: int = 1,
    arc_id: int | None = None,
) -> dict:
    """Execute the full review pipeline.

    Args:
        code_file_id: ID of code file being reviewed.
        code: Python source code to review.
        trust_tier: 1=standard, 2=self-modification, 3=user-trusted.
        arc_id: Optional arc ID for audit trail.

    Returns:
        Dict with:
            code_file_id: int
            risk_level: "low" | "medium" | "high"
            requires_human_review: bool
            phases: dict of each phase result
            recommendation: str
    """
    phases = {}

    # Phase 1: File type inspection
    phases["file_type"] = analyze_file_type(code)

    # Phase 2: Syntax validation
    phases["syntax"] = validate_syntax(code)

    # Phase 3: AST flag analysis (reuses code_manager.ast_check)
    ast_findings = ast_check(code)
    phases["ast_flags"] = ast_findings

    # Phase 4: Comment/string extraction + injection analysis
    extracted = extract_comments_and_strings(code)
    phases["extracted"] = extracted
    injection_result = analyze_injection_risk(code, extracted)
    phases["injection_risk"] = injection_result

    # Determine overall risk level
    risk_level = _compute_risk_level(phases)

    # Determine if human review needed
    requires_human = (
        risk_level == "high"
        or trust_tier >= 2  # self-modification always needs human review
        or not phases["syntax"]["valid"]
    )

    recommendation = _make_recommendation(risk_level, requires_human, phases)

    # Update code_files.review_status
    new_status = "pending_review" if requires_human else ("approved" if risk_level == "low" else "pending_review")
    with db_transaction() as db:
        db.execute(
            "UPDATE code_files SET review_status = ? WHERE id = ?",
            (new_status, code_file_id),
        )

    return {
        "code_file_id": code_file_id,
        "risk_level": risk_level,
        "requires_human_review": requires_human,
        "phases": phases,
        "recommendation": recommendation,
    }


def _compute_risk_level(phases: dict) -> str:
    """Compute overall risk from phase results."""
    # Syntax errors = high risk
    if not phases["syntax"]["valid"]:
        return "high"

    # AST flags
    flag_count = len(phases["ast_flags"])
    has_error_flags = any(f["level"] == "error" for f in phases["ast_flags"])
    has_warning_flags = any(f["level"] == "warning" for f in phases["ast_flags"])
    has_flag_flags = any(f["level"] == "flag" for f in phases["ast_flags"])

    # Injection risk
    injection_risk = phases["injection_risk"]["risk_level"]

    if has_error_flags or injection_risk == "high":
        return "high"
    if has_warning_flags or has_flag_flags or injection_risk == "medium" or flag_count > 2:
        return "medium"
    return "low"


def _make_recommendation(risk_level: str, requires_human: bool, phases: dict) -> str:
    """Generate a human-readable recommendation."""
    if not phases["syntax"]["valid"]:
        return "Code has syntax errors and should not be executed."
    if risk_level == "high":
        return "High risk patterns detected. Human review required before execution."
    if risk_level == "medium":
        return "Some suspicious patterns detected. Review recommended."
    if requires_human:
        return "Self-modification code requires human approval."
    return "Code appears safe for execution."

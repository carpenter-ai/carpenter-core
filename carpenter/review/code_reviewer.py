"""AI-based code reviewer for the review pipeline.

Calls a configurable reviewer model with sanitized code and user-visible
conversation context. The reviewer checks whether the code's actions
align with the user's stated intent.

Supports two review modes:
- Standard: zero findings = APPROVE (normal behavior)
- Adversarial: zero findings = insufficient review. Re-runs with escalated
  scrutiny prompt, then escalates to a stronger model or flags for human
  attention. Particularly valuable for reviews presented to a human judge.
"""

import json
import logging
from dataclasses import dataclass, field

from .. import config

logger = logging.getLogger(__name__)


REVIEWER_SYSTEM_PROMPT = """\
You are a code security reviewer. Your job is to determine whether
submitted Python code faithfully implements the user's stated intent.

The code has been sanitized to prevent prompt injection:
- All string literals replaced with S1, S2, S3, etc.
- All user-defined variable/function/class names replaced with a, b, c, etc.
- All comments and docstrings removed
- For multi-file changes: filenames obfuscated (file_a.py, file_b.py, etc.)

You CANNOT see the actual string values or original variable names.
Focus ONLY on the STRUCTURE and ACTIONS of the code:
- What tools/APIs does it call?
- What data does it read vs write?
- Does it make external network requests?
- Does it access data beyond what's needed?

Compare the code's actions against the user's conversation to determine
whether the code does what the user asked for — nothing more, nothing less.

Guidelines:
- APPROVE if the code's actions match the user's intent
- MINOR if the code seems to deviate in non-dangerous ways (wrong scope,
  extra file reads, unnecessary state writes). User will be asked to clarify.
- MAJOR if the code performs actions clearly not requested (external network
  calls when user asked for local work, accessing unrelated files, writing to
  unexpected locations). This halts execution and alerts the user.

Policy Violations (auto-rejected before you see them):
- Wildcard imports (from X import *) are prohibited - you won't see code with these
- These violations are rejected immediately with no review needed

Arc Context:
Arcs are work units organized in a tree. Code may create, cancel, or modify
Arcs via the arc module. Arc manipulation is a privileged operation — creating
Arcs schedules future work, cancelling Arcs stops planned work, and modifying
Arc status changes workflow progression. Verify that any arc operations match
the user's stated intent.

IMPORTANT: Workflows naturally require a parent PLANNER arc plus N child arcs
for N steps. So a "3-step workflow" requires creating 4 arcs total: 1 parent +
3 children. This is the correct and expected pattern — do NOT flag it as
creating "extra" arcs. Similarly, code that creates a parent arc first and then
adds children in a second submission is normal multi-step setup — do NOT reject
a submission for "only creating one arc" if it is clearly setting up a parent
for subsequent children. Code that only uses arc creation and messaging.send is
structural workflow setup and should generally be APPROVED.

Multi-Step Attack Risk:
A single code submission may look benign in isolation but could be part of a
sequence designed to achieve an unauthorized goal. Watch for:
- State or arc setup that creates conditions exploitable by future submissions
- Code that writes configuration or credentials to be read later
- Arc creation that schedules follow-up work not discussed with the user
- Seemingly innocuous data staging (writing keys, tokens, or URLs to state)
Flag suspicious staging patterns even if the individual submission seems safe.

Config Operations:
When the user asks to change a configuration value, the expected code pattern is
exactly `config.set_value(S1, S2)` optionally followed by `config.reload()`.
Because string literals are always hidden by sanitization, you cannot see which
config key or value is being set — that is by design. Focus on whether the
operation type matches the user's request (read vs write, expected vs unexpected
API). Do NOT flag `config.set_value` / `config.reload` calls as suspicious
solely because you cannot verify the specific key; if the user asked to change a
config setting and the code only calls config.set_value + config.reload (and
nothing else unexpected), APPROVE it.

Cross-File Analysis (for multi-file changes):
When reviewing multiple files together:
- Verify imports between files make sense for the stated task
- Watch for split functionality that could hide malicious intent
- Check that file interactions align with the user's request
- Ensure no file is doing work unrelated to its apparent purpose

You MUST call the submit_verdict tool to deliver your verdict.
Do NOT write your verdict as plain text."""


INTENT_REVIEWER_SYSTEM_PROMPT = """\
You are a code intent reviewer. Your job is to verify that submitted Python
code faithfully implements what the user asked for — nothing more, nothing less.

This code comes from a clean, trusted planner operating on behalf of the user.
You are reviewing the ORIGINAL code (not sanitized) — you can read all variable
names and string literals.

Focus on intent alignment:
- Does the code perform the actions the user requested?
- Does it stay within the expected scope (right files, right arcs, right data)?
- Does it do anything destructive or irreversible that the user did not ask for?
- Does it access, copy, or transmit data beyond what is needed for the task?

Guidelines:
- APPROVE if the code faithfully and safely implements the user's request
- MINOR if the code deviates in non-dangerous ways (wrong scope, unnecessary
  extra reads/writes, overly broad operations). User will be asked to clarify.
- MAJOR if the code performs actions clearly not requested (unexpected external
  calls, writing to unrelated locations, destructive operations not discussed,
  creating arcs or scheduling work not discussed with the user).

Arc Context:
Arcs are work units organized in a tree. Workflows naturally require a parent
PLANNER arc plus N child arcs for N steps — this is correct and expected.
Do NOT flag multi-arc scaffolding as suspicious when it matches the user's request.

You MUST call the submit_verdict tool to deliver your verdict.
Do NOT write your verdict as plain text."""


@dataclass
class Finding:
    """A single finding from the reviewer."""

    location: str  # e.g., "line 5", "function a()", "global scope"
    severity: str  # "critical", "warning", "note"
    description: str  # What was found
    remediation: str  # Suggested fix


@dataclass
class ReviewResult:
    """Result from the AI code reviewer."""

    status: str  # "approve", "minor", "major"
    reason: str  # Empty for approve, explanation for minor/major
    sanitized_code: str  # The sanitized code that was reviewed
    findings: list[Finding] = field(default_factory=list)
    review_pass: int = 1  # Which pass produced this result (1=first, 2=escalated, etc.)
    adversarial_escalated: bool = False  # Whether this result was from adversarial escalation


SUBMIT_VERDICT_TOOL = {
    "name": "submit_verdict",
    "description": "Submit your review verdict. You MUST call this tool.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["APPROVE", "MINOR", "MAJOR"],
            },
            "reasoning": {
                "type": "string",
                "description": "One sentence explaining your decision.",
            },
        },
        "required": ["status", "reasoning"],
    },
}


SUBMIT_VERDICT_ADVERSARIAL_TOOL = {
    "name": "submit_verdict",
    "description": (
        "Submit your review verdict with structured findings. "
        "You MUST report at least one finding. Zero findings means "
        "insufficient review, not clean code."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["APPROVE", "MINOR", "MAJOR"],
            },
            "reasoning": {
                "type": "string",
                "description": "One sentence explaining your overall assessment.",
            },
            "findings": {
                "type": "array",
                "description": (
                    "List of findings. Every review MUST include at least one "
                    "finding, even for code that will be approved. Notes about "
                    "implicit assumptions, edge cases, or minor style issues "
                    "all count as valid findings."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": (
                                "Where in the code (e.g., 'line 5', "
                                "'function a()', 'global scope')"
                            ),
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "warning", "note"],
                            "description": (
                                "critical = security/correctness issue, "
                                "warning = potential problem, "
                                "note = observation or style"
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": "What was found.",
                        },
                        "remediation": {
                            "type": "string",
                            "description": "Suggested fix or action.",
                        },
                    },
                    "required": ["location", "severity", "description", "remediation"],
                },
            },
        },
        "required": ["status", "reasoning", "findings"],
    },
}


ADVERSARIAL_ESCALATION_PROMPT = """\
## ESCALATED REVIEW — Second Pass

Your previous review of this code found NO findings. This is statistically
unlikely for non-trivial code. Every code submission has implicit assumptions,
edge cases, or boundary conditions worth noting.

Review the code again with particular attention to:
- Edge cases and boundary conditions (empty inputs, overflow, type mismatches)
- Implicit assumptions about the execution environment
- Security boundaries (what can the code access vs what it needs?)
- Error handling gaps (what happens when calls fail?)
- Resource leaks (unclosed files, connections, unfinished async operations)
- Race conditions or ordering dependencies
- Whether the code does EXACTLY what the user asked — no more, no less
- Potential for this code to be part of a multi-step attack sequence

You MUST report at least one finding. Even approved code has assumptions
and edge cases worth documenting for the human judge.
"""


def _extract_findings_from_tool_call(response: dict) -> list[Finding]:
    """Extract structured findings from a submit_verdict tool call.

    Returns a list of Finding objects, possibly empty.
    """
    content = response.get("content", [])
    findings = []
    for block in content:
        if block.get("type") != "tool_use":
            continue
        if block.get("name") != "submit_verdict":
            continue
        inp = block.get("input", {})
        raw_findings = inp.get("findings", [])
        if not isinstance(raw_findings, list):
            continue
        for f in raw_findings:
            if not isinstance(f, dict):
                continue
            findings.append(Finding(
                location=str(f.get("location", "unknown")),
                severity=str(f.get("severity", "note")),
                description=str(f.get("description", "")),
                remediation=str(f.get("remediation", "")),
            ))
    return findings


def _extract_verdict_from_tool_call(
    response: dict, sanitized_code: str,
) -> ReviewResult | None:
    """Extract verdict from a tool_use block in the API response.

    Returns ReviewResult if a valid submit_verdict tool call is found,
    None otherwise (caller should fall back to text parsing).
    """
    content = response.get("content", [])
    for block in content:
        if block.get("type") != "tool_use":
            continue
        if block.get("name") != "submit_verdict":
            continue
        inp = block.get("input", {})
        raw_status = inp.get("status", "").upper()
        reasoning = inp.get("reasoning", "")
        if raw_status == "APPROVE":
            return ReviewResult(
                status="approve", reason="", sanitized_code=sanitized_code,
            )
        if raw_status == "MINOR":
            return ReviewResult(
                status="minor", reason=reasoning, sanitized_code=sanitized_code,
            )
        if raw_status == "MAJOR":
            return ReviewResult(
                status="major", reason=reasoning, sanitized_code=sanitized_code,
            )
        # Invalid status value — fail closed (reject)
        logger.warning("submit_verdict called with invalid status: %s", raw_status)
        return ReviewResult(
            status="major",
            reason=f"Reviewer gave unclear verdict status: {raw_status}",
            sanitized_code=sanitized_code,
        )
    return None


def get_reviewer_model(arc_id: int | None = None) -> str:
    """Get the configured reviewer model string.

    If arc_id is provided, looks up source_category from arc_state and
    uses the reviewer_policy for that category. Otherwise falls back to
    role-based model selection.

    Args:
        arc_id: Optional arc ID to determine context-aware policy.

    Returns:
        Model string in "provider:model" format.
    """
    from ..agent.model_resolver import get_model_for_role

    # Try context-aware selection if arc_id provided
    if arc_id is not None:
        try:
            from ..db import get_db, db_connection
            from ..core.source_classifier import get_policy_for_category
            from ..core.arcs import manager as arc_manager
            import json

            # Look up source_category from arc_state
            with db_connection() as db:
                row = db.execute(
                    "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = 'source_category'",
                    (arc_id,),
                ).fetchone()

            if row:
                source_category = json.loads(row["value_json"])
                # Get reviewer policy for this category
                reviewer_policy_name = get_policy_for_category(source_category, "reviewer_policy")
                # Get policy details and extract model
                policy = arc_manager.get_policy_by_name(reviewer_policy_name)
                if policy and policy.get("model"):
                    logger.debug(
                        "Using context-aware reviewer model for arc %d (category=%s, policy=%s): %s",
                        arc_id, source_category, reviewer_policy_name, policy["model"]
                    )
                    return policy["model"]
        except Exception:  # broad catch: AI review may raise anything
            logger.debug("Could not load context-aware reviewer model, falling back to role-based", exc_info=True)

    # Fallback to role-based selection
    return get_model_for_role("code_review")


def extract_conversation_text(messages: list[dict]) -> str:
    """Extract user-visible conversation text for reviewer context.

    Includes only user and assistant messages with plain text content.
    Excludes system messages, tool_use blocks, and tool_result blocks
    to prevent fetched data from reaching the reviewer.

    Args:
        messages: List of message dicts from conversation.get_messages().

    Returns:
        Formatted conversation text.
    """
    lines = []
    for msg in messages:
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue
        # Skip structured content (tool_use / tool_result blocks)
        if msg.get("content_json"):
            continue
        content = msg.get("content", "")
        if not content:
            continue
        label = "User" if role == "user" else "Assistant"
        lines.append(f"**{label}:** {content}")
    return "\n\n".join(lines)


def review_code(
    sanitized_code: str,
    conversation_messages: list[dict],
    advisory_flags: list[str],
    arc_id: int | None = None,
) -> ReviewResult:
    """Run AI code review on sanitized code.

    Args:
        sanitized_code: Code after stripping strings and renaming variables.
        conversation_messages: Raw messages from the conversation (will be
            filtered to user-visible content only).
        advisory_flags: Advisory findings from injection defense analysis.
        arc_id: Optional arc ID for context-aware model selection.

    Returns:
        ReviewResult with status, reason, and sanitized_code.
    """
    from ..agent import model_resolver

    conversation_text = extract_conversation_text(conversation_messages)
    model_str = get_reviewer_model(arc_id)

    provider, model_name = model_resolver.parse_model_string(model_str)
    client = model_resolver.create_client_for_model(model_str)

    # Build the reviewer's input
    user_content = _build_reviewer_content(
        sanitized_code, conversation_text, advisory_flags,
    )
    messages = [{"role": "user", "content": user_content}]

    # Call the reviewer
    review_cfg = config.CONFIG.get("review", {})
    api_key = config.CONFIG.get("claude_api_key") if provider == "anthropic" else None
    kwargs = {
        "model": model_name,
        "temperature": review_cfg.get("reviewer_temperature", 0.0),
        "max_tokens": review_cfg.get("reviewer_max_tokens", 200),
    }
    if provider == "anthropic":
        kwargs["api_key"] = api_key
        kwargs["tools"] = [SUBMIT_VERDICT_TOOL]
        kwargs["tool_choice"] = {"type": "tool", "name": "submit_verdict"}

    response = client.call(REVIEWER_SYSTEM_PROMPT, messages, **kwargs)

    # Try structured tool_use extraction first (Anthropic with tools)
    verdict = _extract_verdict_from_tool_call(response, sanitized_code)
    if verdict is not None:
        return verdict

    # Fall back to text parsing (Ollama, or Anthropic edge case)
    response_text = client.extract_text(response).strip()
    return _parse_review_response(response_text, sanitized_code)


def review_code_for_intent(
    code: str,
    conversation_messages: list[dict],
    arc_id: int | None = None,
) -> ReviewResult:
    """Run lightweight intent-alignment review on trusted code.

    Used when code comes from a clean (non-tainted) conversation.  The
    reviewer sees the ORIGINAL code — no sanitization applied.  The focus
    is purely on whether the code faithfully implements what the user asked
    for, not on injection defence.

    Args:
        code: Original (unsanitized) Python source code.
        conversation_messages: Raw messages from the conversation.
        arc_id: Optional arc ID for context-aware model selection.

    Returns:
        ReviewResult with status, reason, and the original code stored in
        the sanitized_code field for interface compatibility.
    """
    from ..agent import model_resolver

    conversation_text = extract_conversation_text(conversation_messages)
    model_str = get_reviewer_model(arc_id)

    provider, model_name = model_resolver.parse_model_string(model_str)
    client = model_resolver.create_client_for_model(model_str)

    parts = []
    parts.append("## Conversation\n")
    if conversation_text:
        parts.append(conversation_text)
    else:
        parts.append("(No conversation context available)")
    parts.append("\n\n## Code\n")
    parts.append(f"```python\n{code}\n```")
    user_content = "\n".join(parts)

    messages = [{"role": "user", "content": user_content}]

    review_cfg = config.CONFIG.get("review", {})
    api_key = config.CONFIG.get("claude_api_key") if provider == "anthropic" else None
    kwargs = {
        "model": model_name,
        "temperature": review_cfg.get("reviewer_temperature", 0.0),
        "max_tokens": review_cfg.get("reviewer_max_tokens", 200),
    }
    if provider == "anthropic":
        kwargs["api_key"] = api_key
        kwargs["tools"] = [SUBMIT_VERDICT_TOOL]
        kwargs["tool_choice"] = {"type": "tool", "name": "submit_verdict"}

    response = client.call(INTENT_REVIEWER_SYSTEM_PROMPT, messages, **kwargs)

    verdict = _extract_verdict_from_tool_call(response, code)
    if verdict is not None:
        return verdict

    response_text = client.extract_text(response).strip()
    return _parse_review_response(response_text, code)


def review_code_adversarial(
    sanitized_code: str,
    conversation_messages: list[dict],
    advisory_flags: list[str],
    arc_id: int | None = None,
) -> ReviewResult:
    """Run adversarial AI code review: zero findings = insufficient review.

    This mode requires the reviewer to produce structured findings. If the
    first pass returns zero findings, it re-runs with an escalated scrutiny
    prompt. If the second pass also returns zero findings, it either escalates
    to a stronger model (if configured) or returns MAJOR to flag for human
    attention.

    Args:
        sanitized_code: Code after stripping strings and renaming variables.
        conversation_messages: Raw messages from the conversation.
        advisory_flags: Advisory findings from injection defense analysis.
        arc_id: Optional arc ID for context-aware model selection.

    Returns:
        ReviewResult with status, reason, findings, and escalation metadata.
    """
    review_config = config.CONFIG.get("review", {})
    min_findings = review_config.get("adversarial_min_findings", 1)

    # Pass 1: Standard adversarial review (with findings tool)
    result = _run_adversarial_pass(
        sanitized_code, conversation_messages, advisory_flags,
        escalation_context=None,
        pass_number=1,
        arc_id=arc_id,
    )

    if len(result.findings) >= min_findings:
        return result

    logger.info(
        "Adversarial review pass 1 returned %d findings (minimum: %d) — "
        "re-running with escalated scrutiny",
        len(result.findings), min_findings,
    )

    # Pass 2: Escalated scrutiny prompt
    result = _run_adversarial_pass(
        sanitized_code, conversation_messages, advisory_flags,
        escalation_context=ADVERSARIAL_ESCALATION_PROMPT,
        pass_number=2,
        arc_id=arc_id,
    )

    if len(result.findings) >= min_findings:
        result.adversarial_escalated = True
        return result

    logger.warning(
        "Adversarial review pass 2 also returned %d findings (minimum: %d) — "
        "escalating to stronger model or flagging for human review",
        len(result.findings), min_findings,
    )

    # Pass 3: Try model escalation, or flag as MAJOR
    escalated_result = _try_model_escalation(
        sanitized_code, conversation_messages, advisory_flags,
        min_findings=min_findings,
        arc_id=arc_id,
    )
    if escalated_result is not None:
        return escalated_result

    # No escalation available — force MAJOR for human attention
    return ReviewResult(
        status="major",
        reason=(
            "Adversarial review: reviewer found no findings after two passes "
            "and no escalation model is available. Flagging for human review."
        ),
        sanitized_code=sanitized_code,
        findings=[],
        review_pass=2,
        adversarial_escalated=True,
    )


def _run_adversarial_pass(
    sanitized_code: str,
    conversation_messages: list[dict],
    advisory_flags: list[str],
    escalation_context: str | None,
    pass_number: int,
    model_override: str | None = None,
    arc_id: int | None = None,
) -> ReviewResult:
    """Execute a single adversarial review pass.

    Args:
        sanitized_code: Sanitized code to review.
        conversation_messages: Conversation context.
        advisory_flags: Advisory flags from earlier pipeline stages.
        escalation_context: Optional additional prompt for escalated passes.
        pass_number: Which review pass this is (1, 2, 3).
        model_override: Optional model string to use instead of configured reviewer.
        arc_id: Optional arc ID for context-aware model selection.

    Returns:
        ReviewResult with parsed findings.
    """
    from ..agent import model_resolver

    conversation_text = extract_conversation_text(conversation_messages)
    model_str = model_override or get_reviewer_model(arc_id)

    provider, model_name = model_resolver.parse_model_string(model_str)
    client = model_resolver.create_client_for_model(model_str)

    # Build reviewer content
    user_content = _build_reviewer_content(
        sanitized_code, conversation_text, advisory_flags,
    )
    if escalation_context:
        user_content = escalation_context + "\n\n" + user_content

    messages = [{"role": "user", "content": user_content}]

    # Build the system prompt with adversarial instructions
    system_prompt = REVIEWER_SYSTEM_PROMPT + (
        "\n\nADVERSARIAL MODE: You MUST report at least one finding using the "
        "submit_verdict tool's findings array. Zero findings is not acceptable. "
        "Even approved code has implicit assumptions and edge cases worth noting."
    )

    # Call the reviewer with adversarial tool
    review_cfg = config.CONFIG.get("review", {})
    api_key = config.CONFIG.get("claude_api_key") if provider == "anthropic" else None
    kwargs = {
        "model": model_name,
        "temperature": review_cfg.get("reviewer_temperature", 0.0),
        "max_tokens": review_cfg.get("adversarial_max_tokens", 1000),
    }
    if provider == "anthropic":
        kwargs["api_key"] = api_key
        kwargs["tools"] = [SUBMIT_VERDICT_ADVERSARIAL_TOOL]
        kwargs["tool_choice"] = {"type": "tool", "name": "submit_verdict"}

    response = client.call(system_prompt, messages, **kwargs)

    # Extract findings
    findings = _extract_findings_from_tool_call(response)

    # Extract verdict
    verdict = _extract_verdict_from_tool_call(response, sanitized_code)
    if verdict is not None:
        verdict.findings = findings
        verdict.review_pass = pass_number
        return verdict

    # Fall back to text parsing
    response_text = client.extract_text(response).strip()
    result = _parse_review_response(response_text, sanitized_code)
    result.findings = findings
    result.review_pass = pass_number
    return result


def _try_model_escalation(
    sanitized_code: str,
    conversation_messages: list[dict],
    advisory_flags: list[str],
    min_findings: int,
    arc_id: int | None = None,
) -> ReviewResult | None:
    """Attempt to escalate to a stronger model for adversarial review.

    Uses the model escalation stack (task_type="review") to find a
    stronger model. If no escalation stack is configured, returns None.

    Args:
        sanitized_code: Sanitized code to review.
        conversation_messages: Conversation context.
        advisory_flags: Advisory flags.
        min_findings: Minimum findings required.
        arc_id: Optional arc ID for context-aware model selection.

    Returns:
        ReviewResult from the escalated model, or None if no escalation available.
    """
    from ..agent import model_resolver

    current_model = get_reviewer_model(arc_id)
    next_model = model_resolver.get_next_model(current_model, "review")

    if next_model is None:
        logger.info("No escalation model available for adversarial review")
        return None

    logger.info(
        "Adversarial review: escalating from %s to %s",
        current_model, next_model,
    )

    result = _run_adversarial_pass(
        sanitized_code, conversation_messages, advisory_flags,
        escalation_context=ADVERSARIAL_ESCALATION_PROMPT,
        pass_number=3,
        model_override=next_model,
    )
    result.adversarial_escalated = True

    if len(result.findings) >= min_findings:
        return result

    # Even the escalated model found nothing — force MAJOR
    logger.warning(
        "Escalated model %s also returned insufficient findings — "
        "flagging for human review",
        next_model,
    )
    return ReviewResult(
        status="major",
        reason=(
            f"Adversarial review: escalated model ({next_model}) also found no "
            f"findings after scrutiny pass. Flagging for human review."
        ),
        sanitized_code=sanitized_code,
        findings=result.findings,
        review_pass=3,
        adversarial_escalated=True,
    )


def format_findings_for_human(findings: list[Finding]) -> str:
    """Format a list of findings for human consumption.

    Args:
        findings: List of Finding objects.

    Returns:
        Human-readable string summarizing all findings.
    """
    if not findings:
        return "(No findings)"

    lines = []
    for i, f in enumerate(findings, 1):
        severity_label = {
            "critical": "CRITICAL",
            "warning": "WARNING",
            "note": "NOTE",
        }.get(f.severity, f.severity.upper())

        lines.append(f"### Finding {i} [{severity_label}]")
        lines.append(f"**Location:** {f.location}")
        lines.append(f"**Issue:** {f.description}")
        lines.append(f"**Remediation:** {f.remediation}")
        lines.append("")

    return "\n".join(lines)


def _build_reviewer_content(
    sanitized_code: str,
    conversation_text: str,
    advisory_flags: list[str],
) -> str:
    """Build the user message content for the reviewer."""
    parts = []

    parts.append("## Conversation\n")
    if conversation_text:
        parts.append(conversation_text)
    else:
        parts.append("(No conversation context available)")

    parts.append("\n\n## Sanitized Code\n")
    parts.append(f"```python\n{sanitized_code}\n```")

    if advisory_flags:
        parts.append("\n\n## Advisory Flags\n")
        for flag in advisory_flags:
            parts.append(f"- {flag}")

    return "\n".join(parts)


def _parse_review_response(text: str, sanitized_code: str) -> ReviewResult:
    """Parse the reviewer's response into a structured result.

    Checks the first line for the verdict. Models sometimes add
    explanatory text after the verdict — we only use the first line.
    """
    first_line = text.strip().split("\n")[0].strip()

    if first_line == "APPROVE":
        return ReviewResult(
            status="approve", reason="", sanitized_code=sanitized_code,
        )

    if first_line.startswith("MINOR:"):
        return ReviewResult(
            status="minor",
            reason=first_line[6:].strip(),
            sanitized_code=sanitized_code,
        )

    if first_line.startswith("MAJOR:"):
        return ReviewResult(
            status="major",
            reason=first_line[6:].strip(),
            sanitized_code=sanitized_code,
        )

    # Malformed response — fail closed (reject rather than accept)
    logger.warning("Malformed reviewer response: %s", text[:200])
    return ReviewResult(
        status="major",
        reason=f"Reviewer gave unparseable response: {text[:200]}",
        sanitized_code=sanitized_code,
    )

"""Quarantined Quality Reviewer (QQR).

A second LLM reviewer that runs in a strict quarantine envelope:

- sees ONLY the sanitised code and a deterministic, T-only distilled
  summary of the user's request (from :mod:`carpenter.review._summarize`);
- does NOT see the raw conversation history;
- does NOT see prior tool outputs;
- has NO tool access (text in, text out);
- uses a **fixed Python-constant system prompt** (:data:`QQR_PROMPT`);
- is constrained to a vetted allowlist of models (config
  ``review.qqr.allowed_models``).

QQR runs at trusted (T) integrity — its verdict influences a control-flow
decision — but its inputs are bounded T-only by construction. This is a
T-on-T defence-in-depth check, NOT a U→T promotion mechanism (only
deterministic JUDGE arcs sit on the U→T boundary).

Composition (see ``determine_outcome`` in ``pipeline.py``):

* When ``verify_code`` ``verified`` is True, QQR is advisory only — APPROVE
  is preserved (today's behaviour).
* When verification ran but did not verify, MAJOR is forced regardless of
  QQR (today's behaviour preserved).
* When verification is **disabled or errored**, today's behaviour is the
  main reviewer alone. Under QQR, BOTH reviewers must concur for APPROVE;
  either MAJOR is MAJOR. ABSTAIN falls back to today's main-reviewer-only
  behaviour with an audit-log entry.

The composition narrows the auto-approve surface; it never expands it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .. import config as config_mod

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class QqrVerdict(str, Enum):
    """The four possible QQR outcomes.

    APPROVE/MINOR/MAJOR mirror the main reviewer. ABSTAIN is the fail-closed
    sentinel returned when QQR is unreachable, malformed, or disabled —
    composition then falls back to today's main-reviewer-only path.
    """

    APPROVE = "approve"
    MINOR = "minor"
    MAJOR = "major"
    ABSTAIN = "abstain"


_VALID_CATEGORIES = frozenset({"safety", "correctness", "scope", "style", "none"})
_VALID_CONFIDENCE = frozenset({"low", "medium", "high"})

# Hard cap for the UI-only reason field. The reason is *not* fed back to
# any other LLM; it is HTML-escaped before display.
_REASON_HTML_MAX_CHARS = 400


@dataclass(frozen=True)
class QqrSignal:
    """Result of a Quarantined Quality Reviewer call.

    ``reason_html`` is an HTML-escaped, truncated, ``[QQR]``-prefixed string
    intended for the human-review panel only. Other LLMs MUST NOT receive
    this field — only the structured fields (``verdict``, ``category``,
    ``confidence``).
    """

    verdict: QqrVerdict
    category: str  # one of _VALID_CATEGORIES
    confidence: str  # one of _VALID_CONFIDENCE
    reason_html: str  # UI-only; never fed to another LLM
    abstain_reason: str = ""  # internal, for audit log; empty unless ABSTAIN

    @classmethod
    def abstain(cls, reason: str) -> "QqrSignal":
        return cls(
            verdict=QqrVerdict.ABSTAIN,
            category="none",
            confidence="low",
            reason_html=_escape_html_for_panel("[QQR] abstained: " + reason),
            abstain_reason=reason,
        )

    @classmethod
    def from_model_output(cls, raw: str) -> "QqrSignal":
        """Parse a strict JSON response. Fail-closed → ABSTAIN on any error.

        Expected shape::

            {"verdict": "APPROVE|MINOR|MAJOR",
             "category": "safety|correctness|scope|style|none",
             "confidence": "low|medium|high",
             "reason": "one short sentence"}
        """
        if not isinstance(raw, str) or not raw.strip():
            return cls.abstain("empty model output")

        # Tolerate models that wrap JSON in code fences.
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            # Drop opening fence (with optional language) and trailing fence.
            first_newline = cleaned.find("\n")
            if first_newline != -1:
                cleaned = cleaned[first_newline + 1 :]
            if cleaned.endswith("```"):
                cleaned = cleaned[: -3]
            cleaned = cleaned.strip()

        try:
            parsed = json.loads(cleaned)
        except (ValueError, TypeError):
            return cls.abstain("malformed JSON")
        if not isinstance(parsed, dict):
            return cls.abstain("model output not an object")

        verdict_raw = str(parsed.get("verdict", "")).strip().upper()
        if verdict_raw == "APPROVE":
            verdict = QqrVerdict.APPROVE
        elif verdict_raw == "MINOR":
            verdict = QqrVerdict.MINOR
        elif verdict_raw == "MAJOR":
            verdict = QqrVerdict.MAJOR
        else:
            return cls.abstain(f"invalid verdict: {verdict_raw!r}")

        category = str(parsed.get("category", "none")).strip().lower()
        if category not in _VALID_CATEGORIES:
            category = "none"

        confidence = str(parsed.get("confidence", "low")).strip().lower()
        if confidence not in _VALID_CONFIDENCE:
            confidence = "low"

        reason = parsed.get("reason", "")
        if not isinstance(reason, str):
            reason = ""
        reason_html = _escape_html_for_panel("[QQR] " + reason) if reason else "[QQR]"

        return cls(
            verdict=verdict,
            category=category,
            confidence=confidence,
            reason_html=reason_html,
        )


# ---------------------------------------------------------------------------
# Fixed system prompt — Python constant by design (mitigation for I9).
# DO NOT interpolate runtime data into this string.
# ---------------------------------------------------------------------------

QQR_PROMPT = """\
You are the Quarantined Quality Reviewer (QQR).

You see exactly two pieces of information:

1. Sanitised Python code submitted for review. All string literals have
   been replaced with placeholders (S1, S2, …), all user-defined names
   replaced with single letters (a, b, c, …), and all comments and
   docstrings stripped. You CANNOT see the original strings or names.
2. A short trusted summary of what the user asked for, prefixed with
   `[trusted-request]`. This summary is produced by deterministic
   platform code from the user's typed message or the arc's declared
   goal. It is NOT chat history.

You are a defence-in-depth check. A separate reviewer already inspected
the code with full conversation context. Your job is to evaluate the
SAME code WITHOUT that context, so that prompt-injection bytes hiding in
the conversation history cannot steer your verdict.

What to look for (in order):

- SAFETY: external network calls, writes outside the requested scope,
  destructive operations, credential or secret access, arc creation or
  scheduling that the trusted-request does not justify.
- CORRECTNESS: the code's actions plainly cannot produce what the
  trusted-request asks for.
- SCOPE: the code does materially more than the trusted-request asks
  for (e.g. extra arcs, extra files, extra network endpoints).

What NOT to flag:

- Stylistic preferences. Style is not a safety issue.
- Simplicity. A short direct solution to a simple request is correct;
  do not demand elaboration.
- Missing error handling, logging, comments, or type hints the
  trusted-request did not request.

Output FORMAT — RESPOND WITH A SINGLE JSON OBJECT, NOTHING ELSE:

{"verdict": "APPROVE" | "MINOR" | "MAJOR",
 "category": "safety" | "correctness" | "scope" | "style" | "none",
 "confidence": "low" | "medium" | "high",
 "reason": "one short sentence"}

Reserve MAJOR for clear safety or out-of-scope action concerns.
Reserve MINOR for clear correctness gaps.
Default to APPROVE when the code matches the trusted-request and shows
no concrete safety problem.

If the trusted-request is empty or only the placeholder
"(no trusted request available)", err on the side of MINOR for any
non-trivial code (you have insufficient context to APPROVE) and MAJOR
only for code that is unsafe regardless of intent.
"""


# ---------------------------------------------------------------------------
# Defaults (mirror config.py keys; used when config is not yet loaded)
# ---------------------------------------------------------------------------

_DEFAULT_ALLOWED_MODELS = (
    "anthropic:claude-haiku-4-5",
    "anthropic:claude-sonnet-4-6",
)
_DEFAULT_QQR_MODEL = "anthropic:claude-haiku-4-5"
_QQR_MAX_TOKENS = 250
_QQR_TEMPERATURE = 0.0


def _qqr_config() -> dict:
    return config_mod.CONFIG.get("review", {}).get("qqr", {}) or {}


def is_enabled() -> bool:
    """Whether QQR is enabled in config (default True)."""
    return bool(_qqr_config().get("enabled", True))


def _select_model() -> str:
    cfg = _qqr_config()
    allowed = tuple(cfg.get("allowed_models") or _DEFAULT_ALLOWED_MODELS)
    if not allowed:
        return _DEFAULT_QQR_MODEL

    # If a model_policy_id is provided, look it up; otherwise use the first
    # allowed model. The selected model MUST be in the allowlist — if a
    # policy resolves to something outside the allowlist, fall back to
    # the first allowed entry rather than honouring the policy (defence
    # against config drift).
    policy_id = cfg.get("model_policy_id")
    if policy_id:
        try:
            from ..core.arcs import manager as arc_manager
            policy = arc_manager.get_policy_by_name(policy_id)
            if policy and policy.get("model") and policy["model"] in allowed:
                return policy["model"]
        except Exception:  # broad: arc manager init may fail in tests
            logger.debug("QQR: model policy lookup failed", exc_info=True)
    return allowed[0]


def _escape_html_for_panel(text: str) -> str:
    """HTML-escape and length-cap text for the human review panel."""
    if not isinstance(text, str):
        text = str(text)
    text = text[:_REASON_HTML_MAX_CHARS]
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def _fail_closed_default() -> bool:
    return bool(_qqr_config().get("fail_closed", True))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_qqr(
    sanitized_code: str,
    trusted_summary: str,
    advisory_severities: Optional[list[str]] = None,
) -> QqrSignal:
    """Run the Quarantined Quality Reviewer on sanitised code.

    Args:
        sanitized_code: Code produced by ``sanitize_for_review``.
        trusted_summary: Output of
            :func:`carpenter.review._summarize.summarize_trusted_request`.
            MUST already be T-only and truncated.
        advisory_severities: Optional list of severity strings (e.g.
            ``["HIGH", "MEDIUM"]``) from earlier pipeline stages. Only
            severity *labels* are admitted — flag descriptions are dropped
            because their text could itself carry injected prose.

    Returns:
        A :class:`QqrSignal`. On any error (timeout, network failure,
        malformed JSON, disabled-by-config), an ``ABSTAIN`` signal is
        returned and the caller is responsible for the fail-closed
        composition rule.
    """
    if not is_enabled():
        return QqrSignal.abstain("disabled by config")

    # Build the user content. CRITICAL: the only string-formatted runtime
    # data here is `sanitized_code` (already structurally scrubbed) and
    # `trusted_summary` (already T-only, truncated, and prefixed with
    # `[trusted-request]`). No conversation history, no advisory flag
    # *descriptions*, and the system prompt is the Python constant
    # QQR_PROMPT — never config-derived, never interpolated.
    severities = []
    for s in advisory_severities or []:
        if not isinstance(s, str):
            continue
        s_norm = s.strip().upper()
        if s_norm in ("HIGH", "MEDIUM", "LOW", "INFO"):
            severities.append(s_norm)

    parts = ["## Sanitised Code\n", "```python\n", sanitized_code, "\n```\n"]
    parts.append("\n## Trusted Request\n")
    parts.append(trusted_summary)
    if severities:
        parts.append("\n\n## Advisory Severities\n")
        parts.append(", ".join(severities))
    user_content = "".join(parts)

    model_str = _select_model()

    try:
        from ..agent import model_resolver
    except Exception:
        logger.exception("QQR: model_resolver import failed — abstaining")
        return QqrSignal.abstain("model resolver unavailable")

    try:
        provider, model_name = model_resolver.parse_model_string(model_str)
        client = model_resolver.create_client_for_model(model_str)
    except Exception:
        logger.exception("QQR: client construction failed — abstaining")
        return QqrSignal.abstain("client construction failed")

    api_key = (
        config_mod.CONFIG.get("claude_api_key") if provider == "anthropic" else None
    )
    kwargs = {
        "model": model_name,
        "temperature": _QQR_TEMPERATURE,
        "max_tokens": _QQR_MAX_TOKENS,
    }
    if provider == "anthropic":
        kwargs["api_key"] = api_key
        # No tools — text in, text out (forced JSON in the prompt).

    messages = [{"role": "user", "content": user_content}]

    try:
        response = client.call(QQR_PROMPT, messages, **kwargs)
    except Exception:
        logger.warning("QQR: model call failed — abstaining", exc_info=True)
        return QqrSignal.abstain("model call failed")

    try:
        text = client.extract_text(response).strip()
    except Exception:
        logger.warning("QQR: response extraction failed — abstaining", exc_info=True)
        return QqrSignal.abstain("response extraction failed")

    return QqrSignal.from_model_output(text)

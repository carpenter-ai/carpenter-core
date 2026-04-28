"""Verifier wrapper for Python arc-step code.

Thin adapter that lets the existing ``verify_code`` pipeline (whitelist,
typed-string check, taint analysis, dry-run) flow through the same
``carpenter.verify.registry.verify(...)`` surface as the YAML template
verifier.  The goal is not to reimplement anything — only to translate
the existing ``VerificationResult`` (from ``carpenter.verify``) into the
common framework's ``VerificationResult`` (from ``.registry``) so future
content types plug in cleanly.

The two ``VerificationResult`` types live in different modules on
purpose: the existing one is rich with code-specific fields
(``input_combinations``, ``code_hash``, ``policy_version``…); the
framework one is the lowest-common-denominator that every content type
can produce.
"""

from __future__ import annotations

from typing import Optional

from .registry import VerificationFinding, VerificationResult


def verify_python_arc_step(
    content: str,
    context: Optional[dict] = None,
) -> VerificationResult:
    """Run the existing Python verification pipeline as a registry verifier.

    Args:
        content: Python source code submitted by an agent.
        context: Optional dict; ``arc_id`` if present is forwarded to
            the inner pipeline so taint analysis can resolve
            ``state.get`` labels.

    Returns:
        A framework-level ``VerificationResult`` with one finding per
        violation reported by the inner pipeline.  Non-verifiable code
        (whitelist failure that needs human review, not hard reject)
        produces ``warning`` findings rather than ``error`` findings —
        the agent should not be told "you must fix this and retry" when
        the platform's answer is "this needs a human".
    """
    # Imported lazily so importing the registry does not pull in the
    # full taint / dry-run subsystem at module load.
    from . import verify_code

    arc_id = None
    if context:
        arc_id = context.get("arc_id")

    inner = verify_code(content, arc_id=arc_id)

    if inner.verified:
        return VerificationResult.passing()

    severity = "error" if inner.hard_reject else "warning"
    findings: list[VerificationFinding] = []

    if inner.violations:
        for v in inner.violations:
            line = _extract_line(v)
            findings.append(
                VerificationFinding(
                    severity=severity,
                    line=line,
                    message=v,
                    fix_hint=inner.reason,
                )
            )
    else:
        findings.append(
            VerificationFinding(
                severity=severity,
                line=None,
                message=inner.reason,
                fix_hint=(
                    "Restructure the submission so it parses cleanly "
                    "and uses only whitelisted constructs "
                    "(see KB: security/typed-declarations)."
                ),
            )
        )

    return VerificationResult.from_findings(findings)


def _extract_line(violation: str) -> Optional[int]:
    """Pull the first ``Line N:`` integer out of a violation string.

    The existing pipeline emits messages like ``"Line 14: untyped string
    literal …"``; we surface that line number on the framework finding
    so editors / coding agents can jump straight to it.  Non-matching
    strings yield ``None``.
    """
    import re
    m = re.search(r"Line\s+(\d+)\b", violation)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None

"""Content-type-keyed verifier registry.

This is the framework that lets every content type the platform cares
about (Python arc steps, YAML templates, KB articles, prompt files, …)
flow through the same pre-flight check pipeline.

A *verifier* is a callable ``(content: str, context: dict | None) ->
VerificationResult``.  It returns structured findings — each finding
carries enough information that a coding agent can iterate without
human help: the line in the file, what rule was broken, and a fix
hint pointing to the canonical KB article (mirrors the tone of
``carpenter.verify.string_declarations`` which the user explicitly
cited as the model).

Usage::

    from carpenter.verify.registry import verify, register_verifier

    register_verifier("yaml-template", verify_yaml_template)
    result = verify("yaml-template", text)
    if not result.ok:
        for finding in result.findings:
            print(finding.line, finding.message, finding.fix_hint)

Unknown content types pass through (``ok=True``) — registration is
opt-in, so adding a new template directory does not break the agent's
finalization until somebody writes the verifier for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Optional


Severity = Literal["error", "warning"]


@dataclass
class VerificationFinding:
    """A single rule violation surfaced by a verifier.

    Attributes:
        severity: ``"error"`` blocks finalization; ``"warning"`` is
            informational only and does not flip ``VerificationResult.ok``
            to ``False``.
        line: 1-based line number in the verified content, or ``None``
            if the rule is file-global (e.g. missing top-level key).
        message: One-sentence statement of what is wrong.
        fix_hint: Concrete next step for the agent — what to add /
            remove / change.  Mirrors the ``(see KB: …)`` format used
            in ``string_declarations.py`` error messages.
    """

    severity: Severity
    line: Optional[int]
    message: str
    fix_hint: str


@dataclass
class VerificationResult:
    """Aggregate result of running a verifier on one piece of content."""

    ok: bool
    findings: list[VerificationFinding] = field(default_factory=list)

    @classmethod
    def passing(cls) -> "VerificationResult":
        return cls(ok=True, findings=[])

    @classmethod
    def from_findings(
        cls, findings: list[VerificationFinding]
    ) -> "VerificationResult":
        """Build a result; ``ok`` is False iff any finding is an error."""
        has_error = any(f.severity == "error" for f in findings)
        return cls(ok=not has_error, findings=list(findings))


# Verifier signature: takes the content as text plus optional context
# (e.g. file path) and returns a VerificationResult.
Verifier = Callable[[str, Optional[dict]], VerificationResult]


_REGISTRY: dict[str, Verifier] = {}


def register_verifier(content_type: str, verifier: Verifier) -> None:
    """Register a verifier for ``content_type``.

    Re-registering overwrites the existing entry (useful in tests).
    """
    _REGISTRY[content_type] = verifier


def unregister_verifier(content_type: str) -> None:
    """Remove the verifier for ``content_type`` if registered.

    Used in tests to restore a clean registry between cases.
    """
    _REGISTRY.pop(content_type, None)


def list_content_types() -> list[str]:
    """Return the names of all registered content types, sorted."""
    _ensure_default_verifiers()
    return sorted(_REGISTRY)


def verify(
    content_type: str,
    content: str,
    context: dict | None = None,
) -> VerificationResult:
    """Run the registered verifier for ``content_type`` against ``content``.

    If no verifier is registered for ``content_type``, returns a passing
    result with no findings — the framework is opt-in by design so that
    files without a registered type (random Python utility modules,
    docs, …) pass through the agent-finalization hook untouched.
    """
    _ensure_default_verifiers()
    verifier = _REGISTRY.get(content_type)
    if verifier is None:
        return VerificationResult.passing()
    return verifier(content, context)


# ---------------------------------------------------------------------------
# Filename-based content-type detection
# ---------------------------------------------------------------------------
#
# The agent-finalization hook calls ``detect_content_type(path)`` so it
# does not have to require an explicit declaration on every file.
# Detection is a pure function of the path — it looks at directory +
# extension, not at the file content.

def detect_content_type(path: str) -> str | None:
    """Map a file path to a registered content type, or None.

    Current rules:

    - ``config_seed/templates/*.yaml`` → ``"yaml-template"``
    - everything else → ``None`` (no verifier runs)

    Extending: add another branch here when a new content type lands.
    """
    # Normalise separators so both POSIX and Windows-style paths resolve.
    normalised = path.replace("\\", "/")
    is_yaml = normalised.endswith(".yaml") or normalised.endswith(".yml")
    if is_yaml and (
        "/config_seed/templates/" in normalised
        or normalised.startswith("config_seed/templates/")
    ):
        return "yaml-template"
    return None


# ---------------------------------------------------------------------------
# Built-in registrations
# ---------------------------------------------------------------------------
#
# Imports are deferred to avoid an import cycle: ``yaml_template`` may
# pull in ``ast`` / ``yaml`` at module load and we want this module to
# stay importable even if those subsystems break.

def _install_default_verifiers() -> None:
    """Register the verifiers shipped with the package.

    Each import is wrapped so that a partially-initialised submodule
    (e.g. when ``python_arc_step`` is imported first and triggers the
    registry side-effects mid-init) does not raise — we'll just retry
    on the next ``verify`` call via ``_ensure_default_verifiers``.
    """
    try:
        from .yaml_template import verify_yaml_template
        register_verifier("yaml-template", verify_yaml_template)
    except ImportError:
        pass
    try:
        from .python_arc_step import verify_python_arc_step
        register_verifier("python-arc-step", verify_python_arc_step)
    except ImportError:
        pass


_install_default_verifiers()


def _ensure_default_verifiers() -> None:
    """Re-run default registration if a circular import skipped one.

    Cheap enough to call on every ``verify`` — the registrations are
    idempotent and the imports are cached after first success.
    """
    if "yaml-template" not in _REGISTRY or "python-arc-step" not in _REGISTRY:
        _install_default_verifiers()

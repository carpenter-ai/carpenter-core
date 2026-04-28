"""Pre-flight verifier for ``config_seed/templates/*.yaml`` content.

Historically, raw ``integrity_level: untrusted`` in a YAML step was
unsafe because the template loader could not tell at parse time
whether the step authored an untrusted EXECUTOR without the mandatory
REVIEWER + JUDGE siblings, the right ``output_type``, and a registered
reviewer profile.  A parallel Python-only "shape registry" provided
the only audited path until this verifier closed the gap; the
registry has since been removed (Phase 2 — see ``fetch_web.yaml``).

This verifier runs at coding time on the YAML *as text* (so line
numbers in findings match what the agent sees in its editor) and
enforces:

1.  **Schema**: top-level keys are recognised; ``steps`` is a list;
    each step has the required fields.
2.  **Trust topology**: every step with ``integrity_level: untrusted``
    has downstream sibling steps in the same template that form an
    EXECUTOR (untrusted) → REVIEWER (trusted) → JUDGE (trusted) chain
    in correct ``order``.  The REVIEWER's profile must be a registered
    reviewer (e.g. ``security-reviewer``); the JUDGE's profile must be
    ``judge``.
3.  **Output type**: untrusted EXECUTOR steps must pin
    ``output_type: json`` — same invariant the runtime
    ``create_untrusted_batch`` code path relies on.
4.  **Agent-type / integrity-level compatibility**: a JUDGE step
    cannot be untrusted; an EXECUTOR cannot also be a REVIEWER (the
    ``agent_type`` enum already prevents that, but we surface a
    cleaner finding here).
5.  **Goal-placeholder safety**: ``$placeholder`` substitution in a
    step's ``description``/``goal`` is allowed but flagged-with-context
    if it lands inside a triple-backtick fenced block.  A ``$`` inside
    fenced code can be rebound by the caller's bindings dict and
    inject text into the agent's instructions; flagging surfaces the
    risk without blocking authors who have validated their bindings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

import yaml

from .registry import VerificationFinding, VerificationResult


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

_VALID_TOP_LEVEL_KEYS = frozenset({
    "name", "description", "steps", "required_for", "capabilities",
})

_VALID_STEP_KEYS = frozenset({
    "name", "description", "goal", "order", "step_order",
    "agent_type", "integrity_level", "output_type",
    "agent_role", "arc_role", "reviewer_profile",
    "model", "model_role", "agent_model", "model_policy", "model_policy_id",
    "model_min_tier", "mutable", "template_mutable",
    "capabilities", "activation_event", "required_pass",
})

_VALID_AGENT_TYPES = frozenset({
    "PLANNER", "EXECUTOR", "REVIEWER", "JUDGE", "CHAT",
})

_VALID_INTEGRITY_LEVELS = frozenset({
    "trusted", "constrained", "untrusted",
})

_NON_TRUSTED_LEVELS = frozenset({"constrained", "untrusted"})

_VALID_OUTPUT_TYPES = frozenset({"python", "text", "json", "unknown"})

# Reviewer profiles that satisfy the "REVIEWER must be a security-equivalent
# reviewer" rule.  Kept as a small whitelist so the verifier does not need
# a live config — config can add more entries via override but these are
# the platform defaults from ``carpenter/config.py``.
_VALID_REVIEWER_PROFILES = frozenset({
    "security-reviewer", "ux-reviewer",
})

_VALID_JUDGE_PROFILES = frozenset({"judge"})


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def verify_yaml_template(
    content: str,
    context: Optional[dict] = None,
) -> VerificationResult:
    """Verify a YAML workflow template's text.

    Args:
        content: YAML source as text.  Accepted as text rather than a
            pre-parsed dict so we can attach line numbers to every
            finding.
        context: Reserved for future use (e.g. a config snapshot to
            cross-check ``model_policy`` names).

    Returns:
        ``VerificationResult.ok=True`` iff no error-severity findings.
    """
    findings: list[VerificationFinding] = []

    # --- Step 1: parse with line tracking -----------------------------------
    parsed = _parse_with_lines(content)
    if isinstance(parsed, _ParseError):
        findings.append(parsed.to_finding())
        return VerificationResult.from_findings(findings)

    data, line_map = parsed

    # --- Step 2: schema-level checks ----------------------------------------
    findings.extend(_check_top_level_schema(data, line_map))

    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        # Already reported by _check_top_level_schema, but bail before
        # trying to iterate something non-iterable.
        return VerificationResult.from_findings(findings)

    # --- Step 3: per-step checks --------------------------------------------
    for idx, step in enumerate(steps):
        step_line = line_map.get(("steps", idx))
        if not isinstance(step, dict):
            findings.append(VerificationFinding(
                severity="error",
                line=step_line,
                message=f"Step {idx} is not a mapping/dict",
                fix_hint=(
                    "Each step must be a YAML mapping with at least a "
                    "'name' key (see KB: workflows/template-schema)."
                ),
            ))
            continue
        findings.extend(_check_step_schema(step, idx, line_map))
        findings.extend(_check_agent_type_compatibility(step, idx, line_map))
        findings.extend(_check_goal_placeholder_safety(step, idx, line_map))

    # --- Step 4: trust-topology checks (cross-step) -------------------------
    findings.extend(_check_trust_topology(steps, line_map))

    return VerificationResult.from_findings(findings)


# ---------------------------------------------------------------------------
# YAML parsing with line-number capture
# ---------------------------------------------------------------------------

@dataclass
class _ParseError:
    """Internal failure representation for malformed YAML."""

    line: Optional[int]
    message: str

    def to_finding(self) -> VerificationFinding:
        return VerificationFinding(
            severity="error",
            line=self.line,
            message=f"YAML parse error: {self.message}",
            fix_hint=(
                "Fix the YAML syntax error before any other rules can be "
                "checked (see KB: workflows/template-schema)."
            ),
        )


def _parse_with_lines(
    content: str,
) -> tuple[Any, dict] | _ParseError:
    """Parse YAML and return (data, line_map).

    ``line_map`` maps a path tuple to the 1-based line number where the
    *value* (not the key) starts.  Recognised paths:

    - ``("name",)``, ``("description",)`` … for top-level scalars
    - ``("steps", i)`` for the i-th step (its first key)
    - ``("steps", i, "name")``, ``("steps", i, "agent_type")`` … for
      nested step fields

    PyYAML's loader exposes ``Mark`` objects on every parsed node when
    we use ``Loader.compose`` instead of ``yaml.safe_load``.  We walk
    the resulting node tree once to build both the plain Python value
    and the line map.
    """
    try:
        loader = yaml.SafeLoader(content)
        try:
            root_node = loader.get_single_node()
        finally:
            loader.dispose()
    except yaml.YAMLError as exc:
        line = None
        if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
            line = exc.problem_mark.line + 1
        return _ParseError(line=line, message=str(exc))

    if root_node is None:
        return _ParseError(
            line=1,
            message="empty document — at minimum 'name', 'description', "
                    "and 'steps' are required",
        )

    line_map: dict[tuple, int] = {}
    data = _node_to_python(root_node, path=(), line_map=line_map)
    return data, line_map


def _node_to_python(
    node: yaml.Node,
    *,
    path: tuple,
    line_map: dict[tuple, int],
) -> Any:
    """Recursively convert a YAML node to Python, capturing line numbers."""
    if isinstance(node, yaml.MappingNode):
        result: dict = {}
        for key_node, value_node in node.value:
            # Keys that aren't scalar strings get coerced via SafeLoader's
            # default constructor below — preserve the original semantics
            # but skip line tracking for non-string keys.
            try:
                key = yaml.SafeLoader(b"").construct_object(key_node, deep=True)  # type: ignore
            except Exception:  # pragma: no cover - defensive
                key = str(key_node.value)
            sub_path = path + (key,)
            line_map[sub_path] = (
                value_node.start_mark.line + 1
                if value_node.start_mark else None
            )
            result[key] = _node_to_python(
                value_node, path=sub_path, line_map=line_map,
            )
        return result

    if isinstance(node, yaml.SequenceNode):
        result_list: list = []
        for i, item_node in enumerate(node.value):
            sub_path = path + (i,)
            line_map[sub_path] = (
                item_node.start_mark.line + 1
                if item_node.start_mark else None
            )
            result_list.append(_node_to_python(
                item_node, path=sub_path, line_map=line_map,
            ))
        return result_list

    if isinstance(node, yaml.ScalarNode):
        # Use SafeLoader's normal scalar resolution so 'true', '42', etc.
        # behave identically to yaml.safe_load.
        loader = yaml.SafeLoader(b"")
        try:
            return loader.construct_object(node, deep=True)
        finally:
            loader.dispose()

    return None  # pragma: no cover - PyYAML emits only the three node kinds


# ---------------------------------------------------------------------------
# Schema checks
# ---------------------------------------------------------------------------

def _check_top_level_schema(
    data: Any,
    line_map: dict[tuple, int],
) -> list[VerificationFinding]:
    findings: list[VerificationFinding] = []

    if not isinstance(data, dict):
        findings.append(VerificationFinding(
            severity="error",
            line=1,
            message="Template root must be a mapping (dict) of fields",
            fix_hint=(
                "Top-level YAML must declare 'name', 'description', and "
                "'steps' (see KB: workflows/template-schema)."
            ),
        ))
        return findings

    for required in ("name", "description", "steps"):
        if required not in data:
            findings.append(VerificationFinding(
                severity="error",
                line=None,
                message=f"Missing required top-level key {required!r}",
                fix_hint=(
                    f"Add a '{required}:' entry at the top level "
                    "(see KB: workflows/template-schema)."
                ),
            ))

    for key in data:
        if key not in _VALID_TOP_LEVEL_KEYS:
            findings.append(VerificationFinding(
                severity="warning",
                line=line_map.get((key,)),
                message=f"Unknown top-level key {key!r}",
                fix_hint=(
                    "Recognised top-level keys: "
                    f"{sorted(_VALID_TOP_LEVEL_KEYS)} "
                    "(see KB: workflows/template-schema)."
                ),
            ))

    if "steps" in data and not isinstance(data["steps"], list):
        findings.append(VerificationFinding(
            severity="error",
            line=line_map.get(("steps",)),
            message="'steps' must be a list",
            fix_hint=(
                "Format 'steps' as a YAML sequence: each step is a "
                "'- name: …' block (see KB: workflows/template-schema)."
            ),
        ))

    return findings


def _check_step_schema(
    step: dict,
    idx: int,
    line_map: dict[tuple, int],
) -> list[VerificationFinding]:
    findings: list[VerificationFinding] = []
    step_line = line_map.get(("steps", idx))

    if "name" not in step:
        findings.append(VerificationFinding(
            severity="error",
            line=step_line,
            message=f"Step {idx} is missing required field 'name'",
            fix_hint=(
                "Every step needs a 'name' for logging and arc tracking "
                "(see KB: workflows/template-schema)."
            ),
        ))

    for key in step:
        if key not in _VALID_STEP_KEYS:
            findings.append(VerificationFinding(
                severity="warning",
                line=line_map.get(("steps", idx, key)),
                message=(
                    f"Step {step.get('name', idx)!r}: unknown field {key!r}"
                ),
                fix_hint=(
                    "Recognised step fields: "
                    f"{sorted(_VALID_STEP_KEYS)} "
                    "(see KB: workflows/template-schema)."
                ),
            ))

    agent_type = step.get("agent_type")
    if agent_type is not None and agent_type not in _VALID_AGENT_TYPES:
        findings.append(VerificationFinding(
            severity="error",
            line=line_map.get(("steps", idx, "agent_type")),
            message=(
                f"Step {step.get('name', idx)!r}: invalid agent_type "
                f"{agent_type!r}"
            ),
            fix_hint=(
                f"Valid agent_types: {sorted(_VALID_AGENT_TYPES)} "
                "(see KB: trust/agent-types)."
            ),
        ))

    integrity = step.get("integrity_level")
    if integrity is not None and integrity not in _VALID_INTEGRITY_LEVELS:
        findings.append(VerificationFinding(
            severity="error",
            line=line_map.get(("steps", idx, "integrity_level")),
            message=(
                f"Step {step.get('name', idx)!r}: invalid integrity_level "
                f"{integrity!r}"
            ),
            fix_hint=(
                f"Valid integrity levels: {sorted(_VALID_INTEGRITY_LEVELS)} "
                "(see KB: trust/integrity-levels)."
            ),
        ))

    output_type = step.get("output_type")
    if output_type is not None and output_type not in _VALID_OUTPUT_TYPES:
        findings.append(VerificationFinding(
            severity="error",
            line=line_map.get(("steps", idx, "output_type")),
            message=(
                f"Step {step.get('name', idx)!r}: invalid output_type "
                f"{output_type!r}"
            ),
            fix_hint=(
                f"Valid output_types: {sorted(_VALID_OUTPUT_TYPES)} "
                "(see KB: trust/output-types)."
            ),
        ))

    return findings


def _check_agent_type_compatibility(
    step: dict,
    idx: int,
    line_map: dict[tuple, int],
) -> list[VerificationFinding]:
    """Catch impossible (agent_type, integrity_level) combinations."""
    findings: list[VerificationFinding] = []
    agent_type = step.get("agent_type")
    integrity = step.get("integrity_level", "trusted")
    name = step.get("name", idx)

    # JUDGE arcs run deterministic platform code against tainted output —
    # they themselves must be trusted.  See ``carpenter/core/trust/types.py``
    # ``_DEFAULT_AGENT_CAPABILITIES[JUDGE]``.
    if agent_type == "JUDGE" and integrity in _NON_TRUSTED_LEVELS:
        findings.append(VerificationFinding(
            severity="error",
            line=line_map.get(("steps", idx, "integrity_level")),
            message=(
                f"Step {name!r}: JUDGE arcs must be trusted, not "
                f"integrity_level={integrity!r}"
            ),
            fix_hint=(
                "Remove 'integrity_level: " + integrity + "' from this "
                "JUDGE step, or change agent_type to EXECUTOR "
                "(see KB: trust/agent-types)."
            ),
        ))

    # REVIEWER arcs read tainted output but must themselves be trusted.
    if agent_type == "REVIEWER" and integrity in _NON_TRUSTED_LEVELS:
        findings.append(VerificationFinding(
            severity="error",
            line=line_map.get(("steps", idx, "integrity_level")),
            message=(
                f"Step {name!r}: REVIEWER arcs must be trusted, not "
                f"integrity_level={integrity!r}"
            ),
            fix_hint=(
                "REVIEWER arcs cross trust boundaries — they read "
                "untrusted output but must run with trusted authority "
                "(see KB: trust/agent-types)."
            ),
        ))

    return findings


# ---------------------------------------------------------------------------
# Trust-topology cross-step check
# ---------------------------------------------------------------------------

def _check_trust_topology(
    steps: list,
    line_map: dict[tuple, int],
) -> list[VerificationFinding]:
    """For each untrusted step, verify the EXECUTOR/REVIEWER/JUDGE chain.

    Mirrors the invariants enforced at runtime by
    ``carpenter.core.trust.batch._validate_batch``:

    -   At least one REVIEWER and one JUDGE downstream of the untrusted
        EXECUTOR (in the same template).
    -   JUDGE has the highest ``order`` among reviewer-style steps.
    -   ``output_type: json`` on the untrusted EXECUTOR.
    -   Reviewer profile is in ``_VALID_REVIEWER_PROFILES``;
        judge profile is in ``_VALID_JUDGE_PROFILES``.
    """
    findings: list[VerificationFinding] = []

    untrusted_steps = [
        (i, s) for i, s in enumerate(steps)
        if isinstance(s, dict)
        and s.get("integrity_level") in _NON_TRUSTED_LEVELS
        # JUDGE/REVIEWER misuse already flagged above; skip them so we
        # don't double-report.
        and s.get("agent_type", "EXECUTOR") == "EXECUTOR"
    ]

    for idx, step in untrusted_steps:
        name = step.get("name", idx)
        step_line = line_map.get(("steps", idx))
        order = _step_order(step, idx)

        # Output type must be JSON (matches create_untrusted_batch).
        if step.get("output_type") != "json":
            findings.append(VerificationFinding(
                severity="error",
                line=line_map.get(("steps", idx, "output_type"))
                     or step_line,
                message=(
                    f"Step {name!r}: untrusted EXECUTOR must declare "
                    "output_type: json"
                ),
                fix_hint=(
                    "Add 'output_type: json' to this step — runtime "
                    "trust enforcement requires structured output for "
                    "untrusted arcs (see KB: trust/output-types)."
                ),
            ))

        # Find downstream reviewer-style siblings.
        downstream = [
            (j, s) for j, s in enumerate(steps)
            if isinstance(s, dict)
            and _step_order(s, j) > order
            and s.get("agent_type") in ("REVIEWER", "JUDGE")
        ]
        reviewers = [(j, s) for j, s in downstream
                     if s.get("agent_type") == "REVIEWER"]
        judges = [(j, s) for j, s in downstream
                  if s.get("agent_type") == "JUDGE"]

        if not reviewers:
            findings.append(VerificationFinding(
                severity="error",
                line=step_line,
                message=(
                    f"Step {name!r}: untrusted EXECUTOR has no "
                    "downstream REVIEWER step in the same template"
                ),
                fix_hint=(
                    "Add a step with agent_type: REVIEWER and "
                    "agent_role: security-reviewer (or equivalent) "
                    f"with order > {order} (see KB: trust/untrusted-arc-shape)."
                ),
            ))

        if not judges:
            findings.append(VerificationFinding(
                severity="error",
                line=step_line,
                message=(
                    f"Step {name!r}: untrusted EXECUTOR has no "
                    "downstream JUDGE step in the same template"
                ),
                fix_hint=(
                    "Add a step with agent_type: JUDGE and "
                    "agent_role: judge as the final reviewer-style "
                    f"step (order > all REVIEWERs, see KB: "
                    "trust/untrusted-arc-shape)."
                ),
            ))

        # Reviewer profiles must be valid.
        for j, rev in reviewers:
            profile = (rev.get("agent_role")
                       or rev.get("reviewer_profile"))
            if profile is None:
                findings.append(VerificationFinding(
                    severity="error",
                    line=line_map.get(("steps", j)),
                    message=(
                        f"Step {rev.get('name', j)!r}: REVIEWER for "
                        f"untrusted EXECUTOR {name!r} has no agent_role "
                        "(or reviewer_profile)"
                    ),
                    fix_hint=(
                        "Add 'agent_role: security-reviewer' (or "
                        "another registered reviewer profile) "
                        "(see KB: trust/agent-types)."
                    ),
                ))
            elif profile not in _VALID_REVIEWER_PROFILES:
                findings.append(VerificationFinding(
                    severity="error",
                    line=line_map.get(("steps", j, "agent_role"))
                         or line_map.get(("steps", j, "reviewer_profile"))
                         or line_map.get(("steps", j)),
                    message=(
                        f"Step {rev.get('name', j)!r}: REVIEWER profile "
                        f"{profile!r} is not a registered reviewer"
                    ),
                    fix_hint=(
                        f"Use one of {sorted(_VALID_REVIEWER_PROFILES)} "
                        "or register a new agent_role in config.yaml "
                        "(see KB: trust/agent-types)."
                    ),
                ))

        # Judge profile must be 'judge' (or equivalent).
        for j, jdg in judges:
            profile = (jdg.get("agent_role")
                       or jdg.get("reviewer_profile"))
            if profile is None:
                findings.append(VerificationFinding(
                    severity="error",
                    line=line_map.get(("steps", j)),
                    message=(
                        f"Step {jdg.get('name', j)!r}: JUDGE for "
                        f"untrusted EXECUTOR {name!r} has no agent_role"
                    ),
                    fix_hint=(
                        "Add 'agent_role: judge' "
                        "(see KB: trust/agent-types)."
                    ),
                ))
            elif profile not in _VALID_JUDGE_PROFILES:
                findings.append(VerificationFinding(
                    severity="error",
                    line=line_map.get(("steps", j, "agent_role"))
                         or line_map.get(("steps", j, "reviewer_profile"))
                         or line_map.get(("steps", j)),
                    message=(
                        f"Step {jdg.get('name', j)!r}: JUDGE profile "
                        f"{profile!r} is not the registered 'judge' role"
                    ),
                    fix_hint=(
                        f"Use one of {sorted(_VALID_JUDGE_PROFILES)} "
                        "(see KB: trust/agent-types)."
                    ),
                ))

        # JUDGE must have highest order among reviewer-style steps.
        if judges and reviewers:
            judge_orders = [_step_order(j[1], j[0]) for j in judges]
            reviewer_orders = [_step_order(r[1], r[0]) for r in reviewers]
            if judge_orders and reviewer_orders and (
                min(judge_orders) <= max(reviewer_orders)
            ):
                j_idx, j_step = judges[0]
                findings.append(VerificationFinding(
                    severity="error",
                    line=line_map.get(("steps", j_idx, "order"))
                         or line_map.get(("steps", j_idx)),
                    message=(
                        f"Step {j_step.get('name', j_idx)!r}: JUDGE "
                        "must have the highest 'order' among "
                        "reviewer-style steps for untrusted batch "
                        f"{name!r}"
                    ),
                    fix_hint=(
                        "Set this JUDGE's 'order' higher than every "
                        "REVIEWER's 'order' (see KB: "
                        "trust/untrusted-arc-shape)."
                    ),
                ))

    return findings


def _step_order(step: dict, fallback: int) -> int:
    """Return the step's declared ``order`` / ``step_order``, or fallback.

    Templates use ``order``; the trust batch module uses ``step_order``.
    Accept either so we can verify content destined for either path.
    """
    val = step.get("order")
    if val is None:
        val = step.get("step_order")
    if isinstance(val, int):
        return val
    return fallback


# ---------------------------------------------------------------------------
# Goal-placeholder safety
# ---------------------------------------------------------------------------

# A ``$word`` or ``${word}`` style placeholder, matching string.Template's
# default pattern.  We deliberately do NOT match a lone ``$`` or ``$$``.
_PLACEHOLDER_RE = re.compile(r"\$(\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)")

# A triple-backtick fence, matching the convention in
# ``invocation._FETCH_SCRIPT`` and ``fetch_web.yaml``.
_FENCE_RE = re.compile(r"```")


def _check_goal_placeholder_safety(
    step: dict,
    idx: int,
    line_map: dict[tuple, int],
) -> list[VerificationFinding]:
    """Flag ``$placeholder`` substitutions that land inside fenced code.

    Same risk as ``render_shape``: a binding value containing
    backticks or further placeholders could re-bind text inside what
    looks like literal code, injecting tool-call instructions.  At
    coding time we cannot know the binding values, so we surface a
    warning whenever a ``$placeholder`` lives inside a triple-backtick
    fenced block.
    """
    findings: list[VerificationFinding] = []
    name = step.get("name", idx)

    for field_name in ("description", "goal"):
        text = step.get(field_name)
        if not isinstance(text, str):
            continue
        for match in _PLACEHOLDER_RE.finditer(text):
            if _is_inside_fenced_block(text, match.start()):
                findings.append(VerificationFinding(
                    severity="warning",
                    line=line_map.get(("steps", idx, field_name)),
                    message=(
                        f"Step {name!r}: placeholder {match.group(0)!r} "
                        f"appears inside a fenced code block in "
                        f"'{field_name}' — binding values can rebind "
                        "the surrounding text"
                    ),
                    fix_hint=(
                        "Move the placeholder outside the ``` code "
                        "fence, or pre-validate the binding value "
                        "before rendering "
                        "(see KB: trust/goal-injection)."
                    ),
                ))

    return findings


def _is_inside_fenced_block(text: str, offset: int) -> bool:
    """Return True if ``offset`` falls between an odd-numbered set of fences.

    The check is line-naive on purpose: we count ``\\`\\`\\``` occurrences
    before ``offset``; an odd count means we're between an opening and
    closing fence.
    """
    fences_before = sum(1 for _ in _FENCE_RE.finditer(text, 0, offset))
    return fences_before % 2 == 1

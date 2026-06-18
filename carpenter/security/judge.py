# TRIPWIRE: This module is deterministic platform code. It MUST NOT import or
# call any LLM client (anthropic, openai, ollama, carpenter.agent.llm, etc.).
# Reason: the JUDGE is the sole mechanism that promotes a Resource from
# untrusted to trusted (trust-invariants I3). Routing any part of that
# decision through a model — even advisory — converts a deterministic policy
# check into a model-influenced one, collapsing the trust boundary.
# Related: coding-invariants I6 (deterministic checks on hard boundaries),
# trust-invariants I3.

"""Deterministic JUDGE for Carpenter.

JUDGE arcs run platform-level deterministic policy checks instead of
LLM agents.  This module reads the REVIEWER's structured output through
the Resources pipeline (D24 SD11/SD12), runs policy validations against
the security allowlists, and returns a pass/fail result with a detailed
check log.

Flow:
1. Dispatch handler calls ``run_policy_checks(judge_arc_id)``.
2. This module locates the pending Resource produced by a REVIEWER arc
   targeting the JUDGE's review target.  The Resource bytes are read via
   ``read_resource_content(caller_arc_id=None)`` — the JUDGE-dispatch
   wrapper is platform code with no arc context, so the
   platform-introspection path is the right one (passing the JUDGE
   arc's id would fail the I2 defence-in-depth gate, since JUDGE arcs
   are ``integrity_level='trusted'`` and the pending Resource has
   derived trust ``'untrusted'`` until this very call approves it).
3. The bytes are decoded as JSON and (when a ``kind`` column tag is
   present) deserialised into a registered dataclass.  Platform-shipped
   templates use ``PolicyCheckList`` defined below; package-shipped
   templates supply their own kinds.
4. Each policy-typed field is validated against ``SecurityPolicies``
   in-process via ``get_policies().validate()`` — NOT through the
   executor RPC path.  The wrapper IS the platform.
5. On approve/reject, the Resource's ``template_verdict`` is flipped
   via ``mark_template_verdict()``.

The legacy ``_extraction_output``/``_judge_policy_checks`` arc-state
shortcut described in earlier revisions of this docstring has been
replaced by the Resources path above.  Per D24 §11.3, no in-flight data
needs migrating: the old keys were transient and consumed by the JUDGE
in the same arc lifecycle that produced them.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..db import db_connection
from ..core.arcs import manager as arc_manager
from ..core.resources import (
    mark_template_verdict,
    read_resource_content,
)
from ..core.trust.audit import log_trust_event
from .policies import get_policies
from .exceptions import PolicyValidationError

logger = logging.getLogger(__name__)


@dataclass
class PolicyCheck:
    """Result of a single policy check."""
    field_name: str
    policy_type: str
    value: str
    passed: bool
    reason: str = ""


@dataclass
class JudgeResult:
    """Result of deterministic judge evaluation."""
    approved: bool
    checks: list[PolicyCheck] = field(default_factory=list)
    reason: str = ""

    @property
    def failed_checks(self) -> list[PolicyCheck]:
        return [c for c in self.checks if not c.passed]


# ---------------------------------------------------------------------------
# Platform-shipped extract kinds (D24 SD11)
# ---------------------------------------------------------------------------


@dataclass
class PolicyCheckList:
    """Platform extract kind: a flat list of {field, policy_type, value} checks.

    This is the structured shape the platform's deterministic JUDGE
    consumes.  It captures the same intent as the legacy
    ``_extraction_output`` arc-state list — REVIEWER arcs assert "here
    is the constrained data and the policy types each field must
    validate against" and the JUDGE deterministically validates each
    one against the platform's allowlists.

    Serialised form: the Resource's bytes are a JSON document of the
    form ``{"checks": [{"field": str, "policy_type": str, "value": Any}, ...]}``
    with ``policy_type`` optional (omit / empty string => no policy
    constraint, the field auto-passes).
    """

    checks: list[dict] = field(default_factory=list)


_PLATFORM_KINDS: dict[str, type] = {
    "PolicyCheckList": PolicyCheckList,
}


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def run_policy_checks(judge_arc_id: int) -> JudgeResult:
    """Run deterministic policy checks for a JUDGE arc.

    Reads the pending Resource produced by a REVIEWER arc targeting the
    JUDGE's review target, deserialises it (kind dispatch when present),
    and validates each policy-typed field against ``SecurityPolicies``.

    Args:
        judge_arc_id: The JUDGE arc ID.

    Returns:
        JudgeResult with approval status and check details.  The
        Resource's ``template_verdict`` is flipped to ``'approved'`` or
        ``'rejected'`` to match the result.
    """
    judge_arc = arc_manager.get_arc(judge_arc_id)
    if not judge_arc:
        return JudgeResult(approved=False, reason=f"Judge arc {judge_arc_id} not found")

    # Find the review target
    target_arc_id = _get_review_target(judge_arc_id)
    if target_arc_id is None:
        return JudgeResult(approved=False, reason="No review target found for judge")

    # Locate the pending Resource produced by the REVIEWER (D24 SD11).
    try:
        pending = _find_pending_extraction_resource(target_arc_id)
    except ValueError as exc:
        # Multi-row collision on a single REVIEWER — surface as JUDGE error.
        return JudgeResult(approved=False, reason=str(exc))

    if pending is None:
        # No structured extraction Resource — approve by default (no policy
        # constraints).  This matches the historical behaviour for
        # templates that simply don't emit extraction data; their JUDGE
        # arc trivially approves.
        log_trust_event(judge_arc_id, "judge_auto_approve", {
            "target_arc_id": target_arc_id,
            "reason": "no_extraction_resource",
        })
        return JudgeResult(
            approved=True,
            reason="No structured extraction data to validate; approved by default",
        )

    resource_id = int(pending["id"])

    # Deserialise the Resource bytes.
    try:
        extraction = _load_extraction_resource(pending)
    except (ValueError, json.JSONDecodeError, FileNotFoundError) as exc:
        log_trust_event(judge_arc_id, "judge_decode_failure", {
            "target_arc_id": target_arc_id,
            "resource_id": resource_id,
            "reason": str(exc),
        })
        try:
            mark_template_verdict(resource_id, "rejected")
        except Exception:  # noqa: BLE001 — verdict flip failure shouldn't mask
            logger.exception(
                "Failed to flip Resource %d to rejected after decode error",
                resource_id,
            )
        return JudgeResult(
            approved=False,
            reason=f"Failed to decode extraction Resource {resource_id}: {exc}",
        )

    # D24 stage 3b: package-shipped JUDGE dispatch.
    #
    # When the Resource was produced by a template a capability package
    # owns, the package's deterministic JUDGE handler is *the* JUDGE
    # for that template (Q2).  We hand the handler the typed dataclass
    # — never raw bytes, never raw arc state, never a DB handle —
    # mirroring the trust contract the platform JUDGE wrapper enforces
    # for its own handlers (I3).  Policy-typed fields on the extract
    # dataclass have already been validated by ``_validate_policy_fields``
    # below; the package handler does only the structural / cross-field
    # checks the dataclass type system can't express.
    package_result = _try_package_judge(
        judge_arc_id=judge_arc_id,
        target_arc_id=target_arc_id,
        resource_row=pending,
        extract=extraction,
    )
    if package_result is not None:
        try:
            mark_template_verdict(
                resource_id,
                "approved" if package_result.approved else "rejected",
            )
        except Exception:
            logger.exception(
                "Failed to update Resource %d verdict via package JUDGE",
                resource_id,
            )
        log_trust_event(judge_arc_id, "judge_policy_result", {
            "target_arc_id": target_arc_id,
            "resource_id": resource_id,
            "approved": package_result.approved,
            "source": "package_judge",
            "total_checks": len(package_result.checks),
            "failed_checks": len(package_result.failed_checks),
        })
        return package_result

    try:
        checks_list = _extraction_to_checks(extraction)
    except (ValueError, json.JSONDecodeError, FileNotFoundError) as exc:
        log_trust_event(judge_arc_id, "judge_decode_failure", {
            "target_arc_id": target_arc_id,
            "resource_id": resource_id,
            "reason": str(exc),
        })
        try:
            mark_template_verdict(resource_id, "rejected")
        except Exception:  # noqa: BLE001 — verdict flip failure shouldn't mask
            logger.exception(
                "Failed to flip Resource %d to rejected after decode error",
                resource_id,
            )
        return JudgeResult(
            approved=False,
            reason=f"Failed to decode extraction Resource {resource_id}: {exc}",
        )

    # Run policy checks on each field (in-process; the wrapper IS the platform).
    policies = get_policies()
    checks: list[PolicyCheck] = []

    for field_spec in checks_list:
        if not isinstance(field_spec, dict):
            checks.append(PolicyCheck(
                field_name="<malformed>",
                policy_type="none",
                value=str(field_spec),
                passed=False,
                reason=f"Expected dict, got {type(field_spec).__name__}",
            ))
            continue

        field_name = field_spec.get("field", "unknown")
        policy_type = field_spec.get("policy_type", "")
        value = field_spec.get("value", "")

        if not policy_type:
            checks.append(PolicyCheck(
                field_name=field_name,
                policy_type="none",
                value=str(value),
                passed=True,
                reason="No policy constraint",
            ))
            continue

        try:
            policies.validate(policy_type, value)
            checks.append(PolicyCheck(
                field_name=field_name,
                policy_type=policy_type,
                value=str(value),
                passed=True,
            ))
        except (PolicyValidationError, ValueError) as e:
            checks.append(PolicyCheck(
                field_name=field_name,
                policy_type=policy_type,
                value=str(value),
                passed=False,
                reason=str(e),
            ))

    approved = all(c.passed for c in checks)
    result = JudgeResult(
        approved=approved,
        checks=checks,
        reason="" if approved else "Policy check(s) failed",
    )

    # Flip the Resource verdict to match.  Idempotent on the same value;
    # mark_template_verdict refuses approved <-> rejected flips, but a
    # JUDGE arc only runs once per Resource so terminal-flip protection
    # is a backstop, not a hot path.
    try:
        mark_template_verdict(resource_id, "approved" if approved else "rejected")
    except Exception:
        logger.exception(
            "Failed to update Resource %d verdict (judge_arc=%d, target=%d)",
            resource_id, judge_arc_id, target_arc_id,
        )

    # Audit log
    log_trust_event(judge_arc_id, "judge_policy_result", {
        "target_arc_id": target_arc_id,
        "resource_id": resource_id,
        "approved": result.approved,
        "total_checks": len(checks),
        "failed_checks": len(result.failed_checks),
        "failures": [
            {"field": c.field_name, "policy_type": c.policy_type, "reason": c.reason}
            for c in result.failed_checks
        ],
    })

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_review_target(judge_arc_id: int) -> int | None:
    """Get the target arc ID for a judge arc."""
    with db_connection() as db:
        row = db.execute(
            "SELECT value_json FROM arc_state WHERE arc_id = ? AND key = '_review_target'",
            (judge_arc_id,),
        ).fetchone()
        if row:
            return json.loads(row["value_json"])
        return None


def _find_pending_extraction_resource(target_arc_id: int) -> dict | None:
    """Find the pending extraction Resource for the JUDGE's review target.

    Walks every REVIEWER arc whose ``_review_target`` points at
    ``target_arc_id``, looks up Resources they produced as ``role='output'``
    with ``template_verdict='pending'`` and a non-NULL
    ``produced_by_template``, and returns the first match.

    Surfaces multi-row collisions on a single REVIEWER arc as a JUDGE-time
    error: the contract per D24 §11.2 step 2 is one pending extraction
    Resource per REVIEWER arc per template.  Multiple REVIEWERs on the
    same target each contribute one Resource, and the JUDGE consumes
    the first it finds — matching the historical "first hit wins"
    semantics of the legacy ``_get_extraction_data``.

    Returns the Resource row dict, or None when no pending Resource is
    linked to any reviewer of the target.
    """
    with db_connection() as db:
        reviewer_rows = db.execute(
            "SELECT arc_id FROM arc_state "
            "WHERE key = '_review_target' AND value_json = ?",
            (json.dumps(target_arc_id),),
        ).fetchall()
        for reviewer_row in reviewer_rows:
            reviewer_id = reviewer_row["arc_id"]
            # Skip JUDGE arcs themselves — they may carry _review_target
            # too but never produce extraction Resources.
            arc = arc_manager.get_arc(reviewer_id)
            if not arc or arc.get("agent_type") != "REVIEWER":
                continue
            res_rows = db.execute(
                "SELECT r.* FROM resources r "
                "JOIN arc_resources ar ON ar.resource_id = r.id "
                "WHERE ar.arc_id = ? AND ar.role = 'output' "
                "  AND r.produced_by_template IS NOT NULL "
                "  AND r.template_verdict = 'pending' "
                "  AND r.deleted_at IS NULL",
                (reviewer_id,),
            ).fetchall()
            if not res_rows:
                continue
            if len(res_rows) > 1:
                raise ValueError(
                    f"REVIEWER arc {reviewer_id} produced "
                    f"{len(res_rows)} pending extraction Resources; "
                    "expected at most one per template per reviewer"
                )
            return {k: res_rows[0][k] for k in res_rows[0].keys()}
        return None


def _load_extraction_resource(resource_row: dict) -> Any:
    """Read and decode the bytes of a pending extraction Resource.

    Reads via ``read_resource_content(caller_arc_id=None)`` — the
    platform-introspection path.  This is correct here even though the
    Resource's derived trust is ``'untrusted'`` (verdict='pending')
    because:

      1. The JUDGE-dispatch wrapper runs in platform code with no arc
         dispatch context.  Passing the JUDGE arc's id would invoke
         the I2 defence-in-depth gate in ``read_resource_content``,
         which refuses *trusted* arcs reading *untrusted* Resources —
         JUDGE arcs are ``integrity_level='trusted'``, so that path
         raises ``PermissionError`` by construction.
      2. The JUDGE handler is *the* mechanism that promotes the
         Resource to trusted.  Reading the bytes to make the verdict
         decision is a privileged platform operation, not a tainted
         downstream read.  This matches I3 (only path from untrusted
         to trusted is JUDGE approval): the JUDGE *must* see the bytes
         to decide.

    When the Resource declares a ``kind``, dispatch deserialisation
    against the platform's kinds registry (``_PLATFORM_KINDS``) and
    return a dataclass instance.  When ``kind`` is NULL, return the
    decoded JSON (legacy path retained for kind-less platform-shipped
    Resources; B-min package work will phase this out as templates
    declare ``extract_kind``).
    """
    resource_id = int(resource_row["id"])
    kind = resource_row.get("kind")

    # TRIPWIRE: caller_arc_id MUST be None here. Passing the JUDGE arc id would
    # trigger the I2 gate (trusted arc reading untrusted Resource) and raise
    # PermissionError — JUDGE arcs are integrity_level='trusted' and the pending
    # Resource is derived 'untrusted' until this very call approves it.
    # Related: trust-invariants I2, I3 (JUDGE is the promotion path).
    text = read_resource_content(resource_id, caller_arc_id=None)
    payload = json.loads(text)

    if kind is None:
        # Legacy / kind-less path: hand back the raw decoded JSON.
        return payload

    cls = _PLATFORM_KINDS.get(kind)
    if cls is None:
        # D24 stage 3b: consult the package-shipped kind registry.
        # Platform kinds always win (we already checked above), so this
        # path only runs for kinds a capability package contributed via
        # its ``data_models`` manifest entry.  The package's loader
        # registered the dataclass against the same unprefixed name.
        # ImportError is the only soft-fallback case (e.g. a stripped
        # build with no packages subsystem); any other exception from
        # ``lookup_kind`` is a registry-corruption bug that should
        # surface to the caller, not be swallowed into a confusing
        # "unknown kind" rejection (PR #306 followup: the prior broad
        # ``except Exception`` here masked exactly that class of bug).
        try:
            from ..packages.handler_registry import get_handler_registry
            cls = get_handler_registry().lookup_kind(kind)
        except ImportError:
            logger.warning(
                "packages.handler_registry unavailable; cannot resolve "
                "package-shipped kind %r on Resource %d",
                kind, resource_id,
            )
            cls = None
    if cls is None:
        from ..packages.handler_registry import get_handler_registry
        pkg_kinds = [k for k, _ in get_handler_registry().list_kinds()]
        raise ValueError(
            f"Unknown extraction kind {kind!r} on Resource {resource_id}; "
            f"platform kinds: {sorted(_PLATFORM_KINDS)}; "
            f"package kinds: {sorted(pkg_kinds)}"
        )
    if not isinstance(payload, dict):
        raise ValueError(
            f"Resource {resource_id} kind={kind!r}: expected JSON object "
            f"to deserialise as dataclass, got {type(payload).__name__}"
        )
    return _construct_dataclass(cls, payload)


def _construct_dataclass(cls: type, payload: dict) -> Any:
    """Construct a dataclass from a JSON-decoded ``payload`` dict.

    JSON has no tuple type, so every dataclass ``tuple[...]`` field
    arrives as a ``list`` and every nested dataclass field arrives as a
    plain ``dict``.  A naive ``cls(**payload)`` would therefore store the
    wrong runtime types, and the package/platform JUDGE handlers — which
    assert ``isinstance(x, tuple)`` and ``isinstance(x, SomeDataclass)``
    as structural gates — would reject otherwise-valid extracts.

    This helper makes the REVIEWER -> JUDGE handoff reliable-by-default:
    a REVIEWER that persists its extract as a JSON object (the only shape
    ``resource.write`` can store) is decoded back into the exact runtime
    types the dataclass declares.  It is type-driven (uses the field
    annotations), recursion-safe for nested dataclasses, and conservative
    — it only coerces ``list -> tuple`` for tuple-annotated fields and
    recurses into dataclass-annotated fields; anything else is passed
    through untouched so this never silently reshapes a value the JUDGE
    means to inspect.

    Unknown payload keys and missing fields are left to ``cls(**...)`` to
    surface (a ``TypeError`` the caller already maps to a decode failure
    / rejection).
    """
    import dataclasses
    import typing

    if not dataclasses.is_dataclass(cls):
        return cls(**payload) if isinstance(payload, dict) else payload

    try:
        hints = typing.get_type_hints(cls)
    except Exception:  # noqa: BLE001 — fall back to raw annotations
        hints = {f.name: f.type for f in dataclasses.fields(cls)}

    field_names = {f.name for f in dataclasses.fields(cls)}
    kwargs: dict = {}
    for key, value in payload.items():
        if key not in field_names:
            # Preserve the original behaviour: an unexpected key is a
            # TypeError at construction, which the caller treats as a
            # decode failure.  Keep it so smuggled extra fields reject.
            kwargs[key] = value
            continue
        kwargs[key] = _coerce_field_value(hints.get(key), value)
    return cls(**kwargs)


def _coerce_field_value(annotation: Any, value: Any) -> Any:
    """Coerce one JSON-decoded ``value`` to match a dataclass field type.

    Handles the two shapes JSON loses and the JUDGE handlers care about:

    * ``tuple[X, ...]`` (or ``Tuple[...]``): a JSON ``list`` becomes a
      ``tuple``; each element is coerced against the element annotation
      (so ``tuple[AttachmentMetadata, ...]`` rebuilds the nested
      dataclasses).
    * a nested dataclass annotation: a JSON ``dict`` becomes an instance
      of that dataclass (recursively).

    Everything else (str / int / bool / PolicyLiteral fields, which the
    dataclass or dispatch wrapper handle) is returned unchanged.
    """
    import dataclasses
    import typing

    if annotation is None:
        return value

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    # tuple[...] field: JSON list -> tuple, coercing each element.
    if origin in (tuple,) and isinstance(value, list):
        if args and args[-1] is Ellipsis:
            elem_ann = args[0]
            return tuple(_coerce_field_value(elem_ann, v) for v in value)
        if args:
            return tuple(
                _coerce_field_value(args[i] if i < len(args) else None, v)
                for i, v in enumerate(value)
            )
        return tuple(value)

    # Nested dataclass field: JSON dict -> dataclass instance.
    if (
        isinstance(annotation, type)
        and dataclasses.is_dataclass(annotation)
        and isinstance(value, dict)
    ):
        return _construct_dataclass(annotation, value)

    # PolicyLiteral field (EmailPolicy, Url, Domain, ...): JSON primitive
    # -> PolicyLiteral instance.  This is SECURITY-LOAD-BEARING: the
    # in-process policy-field validation (_validate_policy_fields) only
    # runs on values that are ``isinstance(.., PolicyLiteral)``.  A JSON
    # string would otherwise stay a plain ``str``, the allowlist check
    # (e.g. from_address must be allowlisted) would be silently skipped,
    # and the trust gate would weaken.  Reconstructing the literal here
    # ensures every declared-PolicyLiteral field is actually validated.
    if (
        isinstance(annotation, type)
        and not isinstance(value, (dict, list))
        and _is_policy_literal_cls(annotation)
    ):
        try:
            return annotation(value)
        except Exception:  # noqa: BLE001 — let cls(**) surface a type error
            return value

    return value


def _is_policy_literal_cls(annotation: Any) -> bool:
    """Return True if ``annotation`` is a ``PolicyLiteral`` subclass.

    Imports ``PolicyLiteral`` lazily so the JUDGE module stays importable
    in stripped test environments where ``carpenter_tools`` is absent;
    there we simply skip literal reconstruction (the package handler's
    own structural checks still run).
    """
    try:
        from carpenter_tools.policy.types import PolicyLiteral
    except ImportError:
        return False
    try:
        return issubclass(annotation, PolicyLiteral)
    except TypeError:
        return False


def _resolve_package_template(resource_row: dict) -> tuple[str, str] | None:
    """Return ``(template_name, package_name)`` if the Resource came from a package.

    The Resource's ``produced_by_template`` column holds the **template
    name** directly (TEXT, per ``schema.sql`` line ~540 and
    ``derive_resource``'s ``produced_by_template`` argument).  We look it
    up in the ``installed_packages_templates`` join table; if the
    template was contributed by a package, return the pair so the caller
    can dispatch to the package's JUDGE.
    """
    import sqlite3

    produced_by = resource_row.get("produced_by_template")
    if not produced_by:
        return None
    template_name = str(produced_by)
    # No broad ``except Exception`` — sqlite3.Error / AttributeError
    # would represent a real corruption / programmer-error condition that
    # should NOT be silently swallowed.  The historical broad catch here
    # was masking the int(produced_by) bug (PR #306 followup): every
    # package-shipped Resource raised ValueError, the catch returned
    # None, and the package handler was never invoked.
    #
    # The single exception we tolerate is ``sqlite3.OperationalError``
    # for "no such table: installed_packages_templates": this happens
    # in test DBs built with ``init_db(skip_migrations=True)`` and means
    # "no packages installed", which is functionally equivalent to a
    # miss against the table.  We re-raise any other OperationalError.
    try:
        with db_connection() as db:
            pkg_row = db.execute(
                "SELECT package_name FROM installed_packages_templates "
                "WHERE template_name = ? LIMIT 1",
                (template_name,),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    if not pkg_row:
        return None
    return template_name, pkg_row["package_name"]


def _policy_value_for_validation(value: Any) -> str:
    """Extract the string representation a PolicyLiteral feeds to ``validate()``.

    Mirrors :py:meth:`PolicyLiteral._serialized_value` so the in-process
    validation path runs the same string the verification-mode constructor
    would.  Subclasses (IntRange, Bool) override the format; we honour
    that override when present.
    """
    serialised = getattr(value, "_serialized_value", None)
    if callable(serialised):
        try:
            return serialised()
        except Exception:  # noqa: BLE001 — fall through to .value/str
            pass
    inner = getattr(value, "value", None)
    if inner is not None:
        return str(inner)
    return str(value)


def _validate_policy_value(
    *,
    field_name: str,
    value: Any,
    policy_type: str,
    policies: Any,
) -> PolicyCheck:
    """Run a single allowlist validation and produce a PolicyCheck row."""
    v_str = _policy_value_for_validation(value)
    try:
        policies.validate(policy_type, v_str)
        return PolicyCheck(
            field_name=field_name,
            policy_type=policy_type,
            value=str(v_str),
            passed=True,
        )
    except (PolicyValidationError, ValueError) as exc:
        return PolicyCheck(
            field_name=field_name,
            policy_type=policy_type,
            value=str(v_str),
            passed=False,
            reason=str(exc),
        )


def _walk_value_for_policy_checks(
    *,
    field_name: str,
    value: Any,
    policy_literal_cls: type,
    policies: Any,
) -> list[PolicyCheck]:
    """Recursively validate any ``PolicyLiteral`` instances under ``value``.

    Walks lists/tuples (validating each element), dicts (validating
    values; keys are not policy-typed by convention), nested dataclasses
    (recursing into their fields), and bare ``PolicyLiteral`` instances.
    Anything else is ignored — the package JUDGE handler is responsible
    for structural / cross-field checks the type system can't express.

    The ``policy_type`` recorded on the resulting :class:`PolicyCheck`
    comes from the literal instance's own ``_policy_type`` class
    attribute, so all 9 ``PolicyLiteral`` subclasses (EmailPolicy,
    Domain, Url, FilePath, Command, IntRange, Enum, Bool, Pattern) are
    handled uniformly.
    """
    if value is None:
        return []
    if isinstance(value, policy_literal_cls):
        ptype = getattr(value, "_policy_type", "") or ""
        if not ptype:
            # Defensive: a PolicyLiteral subclass without _policy_type is
            # malformed; skip rather than synthesise a bogus check.
            return []
        return [_validate_policy_value(
            field_name=field_name,
            value=value,
            policy_type=ptype,
            policies=policies,
        )]
    # Strings/bytes are iterable but never policy-typed containers.
    if isinstance(value, (str, bytes, bytearray)):
        return []
    # list / tuple / set: validate each element under an indexed name.
    if isinstance(value, (list, tuple, set, frozenset)):
        out: list[PolicyCheck] = []
        for i, elem in enumerate(value):
            out.extend(_walk_value_for_policy_checks(
                field_name=f"{field_name}[{i}]",
                value=elem,
                policy_literal_cls=policy_literal_cls,
                policies=policies,
            ))
        return out
    # dict: validate values (keys aren't conventionally policy-typed).
    if isinstance(value, dict):
        out_d: list[PolicyCheck] = []
        for k, v in value.items():
            out_d.extend(_walk_value_for_policy_checks(
                field_name=f"{field_name}[{k!r}]",
                value=v,
                policy_literal_cls=policy_literal_cls,
                policies=policies,
            ))
        return out_d
    # Nested dataclass: recurse into its fields.
    if hasattr(value, "__dataclass_fields__"):
        out_dc: list[PolicyCheck] = []
        for sub_fname in value.__dataclass_fields__:
            sub_v = getattr(value, sub_fname, None)
            out_dc.extend(_walk_value_for_policy_checks(
                field_name=f"{field_name}.{sub_fname}",
                value=sub_v,
                policy_literal_cls=policy_literal_cls,
                policies=policies,
            ))
        return out_dc
    return []


def _validate_policy_fields(extract: Any) -> list[PolicyCheck]:
    """Validate every ``PolicyLiteral`` instance reachable from an extract.

    Per D24 §3.6 step 3, "every field whose declared type is a
    ``PolicyLiteral`` subclass" must be validated.  We don't trust
    declared types alone (annotations may be string-form under
    ``from __future__ import annotations``, generic args may be lazy);
    instead we walk the *runtime values* and validate every
    ``PolicyLiteral`` instance we find via :func:`isinstance`, which
    covers all 9 subclasses (EmailPolicy, Domain, Url, FilePath, Command,
    IntRange, Enum, Bool, Pattern) uniformly.

    Container handling (D24 spec example: ``list[EmailPolicy]``):

    * ``list``/``tuple``/``set``/``frozenset``: every element is
      validated; the field name is suffixed ``[i]``.
    * ``dict``: every value is validated; the field name is suffixed
      ``[<key!r>]``.  Dict keys are not validated (convention: keys are
      structural tags, not constrained data).
    * Nested ``@dataclass``: recurse into the nested dataclass's fields,
      suffixing the field name ``.subfield``.

    Returns the list of :class:`PolicyCheck` results.  An empty list
    means no ``PolicyLiteral`` instances were reachable from the extract.
    """
    if not hasattr(extract, "__dataclass_fields__"):
        return []
    try:
        from carpenter_tools.policy.types import PolicyLiteral
    except ImportError:
        # carpenter_tools may not be importable in some test envs.  No
        # validation happens here, but the package JUDGE handler still
        # runs and is responsible for whatever checks it can perform.
        # This is the only exception we deliberately swallow: ImportError
        # is a deployment / packaging condition, not a runtime bug.
        logger.warning(
            "carpenter_tools.policy.types unavailable; skipping in-process "
            "policy-field validation for %s",
            type(extract).__name__,
        )
        return []

    policies = get_policies()
    checks: list[PolicyCheck] = []
    for fname in extract.__dataclass_fields__:
        value = getattr(extract, fname, None)
        checks.extend(_walk_value_for_policy_checks(
            field_name=fname,
            value=value,
            policy_literal_cls=PolicyLiteral,
            policies=policies,
        ))
    return checks


def _try_package_judge(
    *,
    judge_arc_id: int,
    target_arc_id: int,
    resource_row: dict,
    extract: Any,
) -> "JudgeResult | None":
    """Dispatch to a package-registered JUDGE if one exists for this template.

    Returns ``None`` when no package JUDGE is registered (the platform
    default path runs).  Returns a :class:`JudgeResult` when the package
    handler ran (whether or not it approved).

    Trust-model invariants enforced here:

    1. Policy-typed fields on the extract are validated against
       ``SecurityPolicies`` *before* the handler runs.  If any field
       fails validation, the JUDGE result is `not approved` and the
       package handler is NOT called — by construction it cannot see
       out-of-policy data.
    2. The handler is invoked with exactly one positional argument:
       the deserialised dataclass.  No DB handle, no arc state, no raw
       bytes.  The dispatch wrapper IS the trust gate.
    3. Handler exceptions are caught and converted to a rejection.
       A package JUDGE that crashes does not crash the JUDGE arc and
       does not silently approve.
    """
    pair = _resolve_package_template(resource_row)
    if pair is None:
        return None
    template_name, package_name = pair

    from ..packages.handler_registry import get_handler_registry
    handler = get_handler_registry().lookup_judge(template_name)
    if handler is None:
        return None

    # Step 1: policy-typed-field validation in-process.
    field_checks = _validate_policy_fields(extract)
    if any(not c.passed for c in field_checks):
        return JudgeResult(
            approved=False,
            checks=field_checks,
            reason=(
                f"Package {package_name!r} JUDGE for template "
                f"{template_name!r}: policy-typed field(s) failed "
                f"in-process validation; handler not invoked"
            ),
        )

    # Step 2: invoke the package handler with the dataclass.
    try:
        raw_result = handler(extract)
    except Exception as exc:  # noqa: BLE001 — package code is fallible
        logger.exception(
            "Package JUDGE handler raised for template %r (package %r, "
            "judge_arc=%d)",
            template_name, package_name, judge_arc_id,
        )
        return JudgeResult(
            approved=False,
            checks=field_checks,
            reason=(
                f"Package {package_name!r} JUDGE for template "
                f"{template_name!r} raised: {type(exc).__name__}: {exc}"
            ),
        )

    # Step 3: coerce duck-typed result into a JudgeResult.
    approved = bool(getattr(raw_result, "approved", False))
    reason = str(getattr(raw_result, "reason", "") or "")
    extra_checks = list(getattr(raw_result, "checks", []) or [])
    return JudgeResult(
        approved=approved,
        checks=field_checks + [
            c for c in extra_checks if isinstance(c, PolicyCheck)
        ],
        reason=reason or (
            "" if approved else
            f"Package JUDGE rejected (template={template_name!r})"
        ),
    )


def _extraction_to_checks(extraction: Any) -> list[dict]:
    """Coerce a deserialised extraction payload to the ``[{field,...}]`` list.

    Accepts:
      - ``PolicyCheckList`` instance (preferred, kind-typed path).
      - bare ``list`` (legacy raw-JSON path; will go away once all
        REVIEWERs declare a kind).
      - ``dict`` with a ``"checks"`` key (raw JSON of PolicyCheckList shape).

    Raises ``ValueError`` on any other shape.
    """
    if isinstance(extraction, PolicyCheckList):
        return list(extraction.checks)
    if isinstance(extraction, list):
        return extraction
    if isinstance(extraction, dict) and isinstance(extraction.get("checks"), list):
        return list(extraction["checks"])
    raise ValueError(
        f"Cannot interpret extraction payload as a check list: "
        f"got {type(extraction).__name__}"
    )

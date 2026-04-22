"""Registry of canonical untrusted-arc shapes.

A "shape" is a reusable recipe for spawning an untrusted EXECUTOR arc
together with its mandatory trusted REVIEWER(s) and JUDGE — the same
structure that ``carpenter.agent.invocation._handle_fetch_web_content``
creates by hand today.

YAML workflow templates can declare ``untrusted_shape: <preset_name>``
on a step. ``template_manager.instantiate_template`` resolves the name
against this registry and expands the step into the canonical child
arcs via :func:`carpenter.core.trust.batch.create_untrusted_batch`.

Shapes are intentionally **Python-only**. We do not let YAML spell out
raw ``integrity_level: untrusted`` — authorship of new untrusted flows
stays with platform code so that review coverage and output typing are
centrally audited.

To register a new shape, add a dict to ``_REGISTRY`` keyed by the
preset name.  Each entry must contain ``specs``: a list of arc-spec
dicts in the same format accepted by ``create_untrusted_batch``.
Shell-style placeholders like ``$goal`` in any string value get
substituted by :func:`render_shape` from the step's ``bindings`` dict
(we use ``string.Template`` rather than ``str.format`` because the
fetch script contains literal ``{`` characters).
"""

from __future__ import annotations

import copy
from string import Template
from typing import Any


class UnknownShapeError(ValueError):
    """Raised when a YAML step references a shape that is not registered."""


# ---------------------------------------------------------------------------
# fetch_web preset
# ---------------------------------------------------------------------------
#
# Mirrors ``_handle_fetch_web_content`` exactly:
#   child 0: EXECUTOR (untrusted, json) — runs the pre-verified fetch script
#   child 1: REVIEWER (trusted) — extracts relevant info, writes to
#            arc_state['_agent_response']
#   child 2: JUDGE   (trusted) — validates and finalises the response
#
# ``{goal}`` is populated from the step bindings so the reviewer knows
# what information to extract.  URL injection happens at runtime by
# setting ``fetch_url`` on the EXECUTOR's arc_state; shapes do not know
# about runtime values.

_FETCH_SCRIPT = """\
from carpenter_tools.declarations import Label
url_result = dispatch(Label("state.get"), {"key": Label("fetch_url")})
url = url_result[Label("value")]
result = dispatch(Label("web.fetch_webpage"), {"url": url})
dispatch(Label("state.set"), {"key": Label("fetched_content"), "value": result})
"""


_REGISTRY: dict[str, dict[str, Any]] = {
    "fetch_web": {
        "description": (
            "Fetch a URL in a sandboxed untrusted EXECUTOR; extract "
            "relevant content in a trusted REVIEWER; finalise in a "
            "trusted JUDGE."
        ),
        "specs": [
            {
                "name": "Fetch web content",
                "goal": (
                    "Submit this EXACT code via submit_code "
                    "(do not modify it):\n"
                    "```python\n" + _FETCH_SCRIPT + "```\n"
                    "The URL has been pre-set in arc state as 'fetch_url'."
                ),
                "integrity_level": "untrusted",
                "output_type": "json",
                "agent_type": "EXECUTOR",
                "step_order": 0,
            },
            {
                "name": "Review fetched content",
                "goal": (
                    "Read the untrusted output from the fetch arc. "
                    "Extract the relevant information the user wanted: "
                    "$goal. Store a clean summary in arc state under key "
                    "'_agent_response'."
                ),
                "agent_type": "REVIEWER",
                "integrity_level": "trusted",
                "reviewer_profile": "security-reviewer",
                "model_policy": "fast-chat",
                "step_order": 1,
            },
            {
                "name": "Validate review",
                "goal": (
                    "Validate that the reviewer's extraction is accurate "
                    "and complete. Copy the final answer to arc state key "
                    "'_agent_response'."
                ),
                "agent_type": "JUDGE",
                "integrity_level": "trusted",
                "reviewer_profile": "judge",
                "step_order": 2,
            },
        ],
    },
}


def list_shapes() -> list[str]:
    """Return the names of all registered shapes."""
    return sorted(_REGISTRY)


def get_shape(name: str) -> dict[str, Any]:
    """Return the raw shape definition for ``name``.

    Raises:
        UnknownShapeError: if ``name`` is not registered.
    """
    if name not in _REGISTRY:
        available = ", ".join(list_shapes()) or "(none)"
        raise UnknownShapeError(
            f"Unknown untrusted_shape {name!r}. "
            f"Registered shapes: {available}."
        )
    return _REGISTRY[name]


def render_shape(
    name: str,
    bindings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return a deep-copied spec list with ``$name`` placeholders filled.

    Only string values in the spec are substituted, using
    :meth:`string.Template.safe_substitute` so that unknown
    placeholders (and stray ``$`` or ``{`` characters inside embedded
    code snippets) render as-is rather than raising.  Non-string
    values are passed through untouched.

    Args:
        name: Registered shape name.
        bindings: Mapping of placeholder → value. Missing keys leave
            the ``$key`` literal in place.

    Returns:
        A fresh list of spec dicts suitable for passing to
        :func:`carpenter.core.trust.batch.create_untrusted_batch`.
    """
    shape = get_shape(name)
    bindings = bindings or {}

    def _sub(value):
        if isinstance(value, str) and "$" in value:
            return Template(value).safe_substitute(bindings)
        return value

    specs = copy.deepcopy(shape["specs"])
    for spec in specs:
        for key, value in list(spec.items()):
            spec[key] = _sub(value)
    return specs


# ---------------------------------------------------------------------------
# YAML-step validation
# ---------------------------------------------------------------------------

# Step-level fields that conflict with a shape (the shape owns them).
_CONFLICTING_STEP_FIELDS = (
    "agent_type",
    "integrity_level",
    "reviewer_profile",
    "output_type",
)


def validate_step_against_shape(step: dict[str, Any]) -> None:
    """Validate that a YAML step's ``untrusted_shape`` is usable.

    Called by ``template_manager.load_template`` so authors get an
    error at load time rather than at instantiation.

    Raises:
        UnknownShapeError: if the shape name is unknown.
        ValueError: if the step mixes ``untrusted_shape`` with
            conflicting overrides.
    """
    name = step.get("untrusted_shape")
    if not name:
        return
    # Force a lookup so unknown names fail fast.
    get_shape(name)

    conflicting = [f for f in _CONFLICTING_STEP_FIELDS if f in step]
    if conflicting:
        raise ValueError(
            f"Step {step.get('name')!r} declares "
            f"untrusted_shape={name!r} and also sets "
            f"{conflicting!r}; these fields are owned by the shape "
            "and cannot be overridden on the step."
        )

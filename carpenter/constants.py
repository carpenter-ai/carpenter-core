"""Cross-module constants and config-backed shims.

Two flavors live here:

1. **Plain module-level constants** for values that are not user-tunable
   and that more than one module needs to agree on. Importing them by
   name keeps the cross-module contract explicit and gives `grep` a
   single home for these literals.

2. **Config-backed shims** (``_CONSTANTS`` dict + ``__getattr__``) for
   values that are configurable via ``config.yaml``. Setting the
   lowercase config key overrides the default::

       arc_state_value_max_length: 500
       arc_log_output_max_length: 16000

   See ``carpenter/config.py`` DEFAULTS for the full list.
"""

from . import config as _config

# ── Plain cross-module constants ────────────────────────────────────────

#: Default API/UI port used when no ``port`` key is configured. The
#: value also lives in ``config.DEFAULTS["port"]``; this constant exists
#: so callers that need a port literal at import time (or when config
#: is unavailable) don't sprinkle ``7842`` throughout the tree.
DEFAULT_API_PORT = 7842

#: Maximum number of REWORK attempts the coding-change handler permits
#: before auto-escalating to MAJOR. Mirrored in user-facing review UI
#: copy ("Attempt N/3"). Treat as hardcoded — security-relevant logic
#: in ``review/pipeline.py`` and ``core/workflows/coding_change_handler.py``
#: is documented against this limit.
MAX_REWORK_RETRIES = 3

#: Truncation length used for human-readable previews of opaque blobs
#: (tool-result snippets in compaction text, malformed-JSON dumps in
#: log messages, etc.). Not a security boundary — purely a log-volume
#: knob.
LOG_PREVIEW_TRUNCATION = 200


# ── Config-backed shims ────────────────────────────────────────────────

# Mapping: UPPER_CASE attribute name -> (config key, default value)
_CONSTANTS = {
    "ARC_STATE_VALUE_MAX_LENGTH": ("arc_state_value_max_length", 300),
    "ARC_LOG_OUTPUT_MAX_LENGTH": ("arc_log_output_max_length", 8000),
    "CONVERSATION_SUMMARY_MAX_LENGTH": ("conversation_summary_max_length", 6000),
    "CONVERSATION_SUMMARY_MIN_REMAINING": ("conversation_summary_min_remaining", 50),
    "PR_REVIEW_SUMMARY_MAX_LENGTH": ("pr_review_summary_max_length", 200),
    "ARC_PARENT_CHAIN_MAX_DEPTH": ("arc_parent_chain_max_depth", 100),
}


def __getattr__(name: str):
    """Provide backward-compatible access to constants via config."""
    if name in _CONSTANTS:
        key, default = _CONSTANTS[name]
        return _config.get_config(key, default)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

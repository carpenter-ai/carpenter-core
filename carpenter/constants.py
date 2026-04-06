"""Backward-compatible shim for constants moved to the config system.

All values formerly defined here are now configurable via config.yaml
(or environment). This module provides read-only attribute access that
delegates to ``config.get_config()`` so existing imports continue to
work without changes.

To override any value, set the lowercase config key in config.yaml::

    arc_state_value_max_length: 500
    arc_log_output_max_length: 16000

See ``carpenter/config.py`` DEFAULTS for the full list.
"""

from . import config as _config

# Mapping: UPPER_CASE attribute name -> (config key, default value)
_CONSTANTS = {
    "ARC_STATE_VALUE_MAX_LENGTH": ("arc_state_value_max_length", 300),
    "ARC_LOG_OUTPUT_MAX_LENGTH": ("arc_log_output_max_length", 8000),
    "CONVERSATION_SUMMARY_MAX_LENGTH": ("conversation_summary_max_length", 6000),
    "CONVERSATION_SUMMARY_MIN_REMAINING": ("conversation_summary_min_remaining", 50),
    "PR_REVIEW_SUMMARY_MAX_LENGTH": ("pr_review_summary_max_length", 200),
    "ARC_PARENT_CHAIN_MAX_DEPTH": ("arc_parent_chain_max_depth", 100),
    "INFERENCE_SERVER_HEALTH_CHECK_INTERVAL": ("inference_server_health_check_interval", 1),
}


def __getattr__(name: str):
    """Provide backward-compatible access to constants via config."""
    if name in _CONSTANTS:
        key, default = _CONSTANTS[name]
        return _config.get_config(key, default)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

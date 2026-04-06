"""Model subsystem — registry, selector, health tracking, and monitoring.

Re-exports key public symbols for convenient access::

    from carpenter.core.models import ModelEntry, select_model, ModelHealth
"""

# Registry
from .registry import ModelEntry, get_registry, load_registry, get_entry, get_entry_by_model_id, reload_registry, update_measured_speed, get_local_downloadable_models  # noqa: F401

# Selector
from .selector import PolicyConstraints, ModelPolicy, SelectionResult, select_model, select_models, get_presets, PRESETS  # noqa: F401

# Health
from .health import (  # noqa: F401
    ModelHealth,
    ModelHealthState,
    ProviderHealthState,
    record_model_call,
    get_model_health,
    get_backoff_multiplier,
    should_circuit_break,
    all_cloud_models_circuit_open,
    get_provider_health,
    get_all_provider_health,
    get_all_model_health,
    reset_circuit_breaker,
    cleanup_old_calls,
)

# Monitor
from .monitor import check_health, reset  # noqa: F401

# Speed tracker
from .speed_tracker import compute_measured_speeds, update_registry_speeds  # noqa: F401

"""Engine subsystem — work queue, event bus, main loop, triggers, subscriptions, templates.

Re-exports key public symbols::

    from carpenter.core.engine import enqueue, run_loop, record_event
"""

# Work queue
from .work_queue import enqueue, claim, complete, fail, get_item, get_dead_letter_items  # noqa: F401

# Event bus
from .event_bus import record_event, register_matcher, process_events, check_timeouts, get_event, get_matchers  # noqa: F401

# Trigger manager
from .trigger_manager import add_cron, add_once, remove_cron, enable_cron, check_cron, get_cron, list_cron  # noqa: F401

# Subscriptions
from .subscriptions import load_subscriptions, load_builtin_subscriptions, process_subscriptions, get_subscriptions  # noqa: F401

# Main loop
from .main_loop import run_loop, wake_signal, register_handler, get_handler, register_heartbeat_hook, set_restart_pending  # noqa: F401

# Template manager
from .template_manager import load_template, get_template, get_template_by_name, list_templates, find_template_for_resource, instantiate_template, validate_template_rigidity, load_templates_from_dir, collect_template_triggers, load_template_triggers  # noqa: F401

# Template executor
from .template_executor import get_template_for_workflow, get_step_config, get_verification_steps, get_model_policy_for_step  # noqa: F401

# Handler registry (template-supplied Python step handlers)
from .handler_registry import register_step_handler, lookup_step_handler, unregister_step_handler, clear_registry, registered_handlers  # noqa: F401

# Arc outputs (named outputs with sibling-by-role resolution)
from .arc_outputs import set_arc_output, get_arc_output, list_arc_outputs, find_sibling_arc_id, get_sibling_output  # noqa: F401

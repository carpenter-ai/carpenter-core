"""Resource abstraction: first-class rows for externally-sourced content.

Trust is DERIVED from provenance, not stored.  A Resource is ``'trusted'``
iff it was produced by a template arc whose output a JUDGE approved.
Raw ingest (``produced_by_template`` NULL) is forever ``'untrusted'``.

Untrusted arcs cannot *read* Resources (input role); they may *produce*
Resources (output role).  Enforcement lives in ``link_arc_resource``.

This PR (PR1) provides the schema, CRUD, lineage, and derived trust.
Integration with ``fetch_web_content``, the template registry, JUDGE
verdict wiring, the chat surface, and the sweep job happens in later
PRs.
"""

from .manager import (
    create_resource,
    derive_resource,
    mark_template_verdict,
    link_arc_resource,
    deprecate_resource,
    deprecate_inputs_of_arc,
    get_resource,
    list_resources_for_arc,
    read_resource_content,
    pin,
    unpin,
    set_retain_until,
    get_lineage,
    resource_storage_dir,
    resource_storage_path,
    hash_file,
    update_resource_content_stats,
    set_resource_file_path,
)
from .trust import resource_trust, is_trusted
from .registry import (
    get_template_for,
    get_template_by_name,
    list_templates,
    reload_templates,
)
from .sweep import (
    run_sweep,
    register_weekly_sweep,
    handle_sweep_work_item,
    SWEEP_EVENT_TYPE,
    SWEEP_COMPLETED_EVENT_TYPE,
    SWEEP_CRON_SCHEDULE,
    SWEEP_TRIGGER_NAME,
)

__all__ = [
    "create_resource",
    "derive_resource",
    "mark_template_verdict",
    "link_arc_resource",
    "deprecate_resource",
    "deprecate_inputs_of_arc",
    "get_resource",
    "list_resources_for_arc",
    "read_resource_content",
    "pin",
    "unpin",
    "set_retain_until",
    "get_lineage",
    "resource_storage_dir",
    "resource_storage_path",
    "hash_file",
    "update_resource_content_stats",
    "set_resource_file_path",
    "resource_trust",
    "is_trusted",
    "get_template_for",
    "get_template_by_name",
    "list_templates",
    "reload_templates",
    "run_sweep",
    "register_weekly_sweep",
    "handle_sweep_work_item",
    "SWEEP_EVENT_TYPE",
    "SWEEP_COMPLETED_EVENT_TYPE",
    "SWEEP_CRON_SCHEDULE",
    "SWEEP_TRIGGER_NAME",
]

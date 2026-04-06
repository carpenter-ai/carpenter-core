"""Workflow handlers — coding changes, merges, reviews, webhooks.

Re-exports key public symbols::

    from carpenter.core.workflows import handle_coding_change

Note: Most symbols are handler functions registered with the main loop.
Import individual modules for specific handlers.
"""

# Review templates (no dependencies, safe to import eagerly)
from .review_templates import has_automated_review, get_review_template  # noqa: F401

# Review manager
from .review_manager import create_review_arc, submit_verdict  # noqa: F401

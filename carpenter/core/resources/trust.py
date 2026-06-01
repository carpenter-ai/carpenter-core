"""Derived trust for Resources.

Trust is NOT stored on the row.  It is computed from provenance:

    resource_trust(r) = 'trusted'
        iff r['produced_by_template'] is not None
        AND r['template_verdict'] == 'approved'
    else 'untrusted'

This means the ONLY write path that can flip a Resource from untrusted to
trusted is ``mark_template_verdict(resource_id, 'approved')`` on a row
whose ``produced_by_template`` is already set at insertion time.  Raw
ingest (``create_resource``) never sets ``produced_by_template``, so raw
Resources are forever untrusted — which is the whole point of deriving
trust from provenance instead of a mutable flag.
"""

from __future__ import annotations

from .manager import get_resource


def resource_trust(resource: dict) -> str:
    """Return ``'trusted'`` or ``'untrusted'`` for a resource row.

    Args:
        resource: dict as returned by :func:`get_resource`.
    """
    if not resource:
        return "untrusted"
    if resource.get("produced_by_template") is None:
        return "untrusted"
    if resource.get("template_verdict") != "approved":
        return "untrusted"
    return "trusted"


def is_trusted(resource_id: int) -> bool:
    """Convenience — fetch resource and derive trust."""
    row = get_resource(resource_id)
    if row is None:
        return False
    return resource_trust(row) == "trusted"

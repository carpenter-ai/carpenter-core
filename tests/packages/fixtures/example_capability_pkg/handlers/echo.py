"""Trivial example capability handler.

Exercises the platform-capability framework end to end without doing any
real network I/O.  The handler is invoked as ``handle_echo(params, ctx)``
by the dispatch path; ``ctx`` is a :class:`carpenter.packages.capabilities.CapabilityContext`
bound to the operator-confirmed egress grant.

It returns a JSON-serialisable dict echoing the params plus the confirmed
scope (host/port/protocol) and — to prove credentials resolve PLATFORM-SIDE
— a boolean indicating whether the package's PASSWORD credential is
resolvable via ``ctx.secret``.  It NEVER returns the secret value.
"""

from __future__ import annotations


def handle_echo(params: dict, ctx) -> dict:
    # Credentials resolve platform-side via the scoped context.  We resolve
    # but do NOT return the value — just confirm it was reachable.
    has_password = False
    try:
        pw = ctx.secret("PASSWORD")
        has_password = bool(pw)
    except Exception:
        has_password = False
    return {
        "echo": params,
        "host": ctx.host,
        "port": ctx.port,
        "protocol": ctx.protocol,
        "package": ctx.package_name,
        "verb": ctx.verb,
        "has_password": has_password,
    }

"""Destination guard for web tool egress.

The ``web.*`` tool backends make HTTP requests from the platform process
on behalf of arc code.  Without a destination check, that code can reach
anything the platform host can reach: the platform's own HTTP API on
loopback, other services bound to loopback, the local network, and
cloud metadata endpoints.  Those destinations often trust network
location instead of credentials, so a request from the platform process
carries authority the calling arc was never granted.

This module refuses any destination that is not globally routable
unless the operator allowlists it explicitly::

    web_egress_allowlist:
      - "printer.lan"        # exact hostname (case-insensitive)
      - "192.168.1.0/24"     # CIDR, matched against resolved addresses
      - "10.0.0.5"           # single address

A hostname entry allows that host whatever it resolves to.  A CIDR entry
allows resolved addresses inside it.  ``0.0.0.0/0`` and ``::/0`` restore
the old unrestricted behaviour.

The check resolves the hostname and inspects every address it resolves
to, so numeric forms such as ``http://2130706433/`` are caught.  It is
not a defence against DNS rebinding: the HTTP client resolves the name
again when it connects.  Closing that gap needs the connection to be
pinned to the checked address, or egress filtering below the
application (a network namespace or firewall rule for the platform's
outbound traffic).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlsplit

from .. import config

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = ("http", "https")
_DEFAULT_PORTS = {"http": 80, "https": 443}

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class EgressDenied(PermissionError):
    """Raised when a web tool destination is refused by the egress guard."""


def _system_resolve_host(host: str, port: int) -> list[str]:
    """Return every address ``host`` resolves to (numeric strings)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


# Indirection so tests can substitute a hermetic resolver.
_resolve_host = _system_resolve_host


def _parse_address(raw: str) -> IPAddress:
    """Parse a numeric address, dropping any IPv6 zone index."""
    return ipaddress.ip_address(raw.split("%", 1)[0])


def _effective_address(addr: IPAddress) -> IPAddress:
    """Unwrap IPv4 addresses embedded in IPv6 forms that route to them."""
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return addr.ipv4_mapped
        if addr.sixtofour is not None:
            return addr.sixtofour
        if addr.teredo is not None:
            return addr.teredo[1]
    return addr


def _is_public(addr: IPAddress) -> bool:
    """True if ``addr`` is globally routable unicast."""
    addr = _effective_address(addr)
    return addr.is_global and not addr.is_multicast


def _load_allowlist() -> tuple[set[str], list[IPNetwork]]:
    """Split ``web_egress_allowlist`` into hostnames and networks."""
    raw = config.get_config("web_egress_allowlist", []) or []
    if isinstance(raw, str):
        raw = [raw]
    hosts: set[str] = set()
    networks: list[IPNetwork] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            continue
        entry = entry.strip()
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            hosts.add(entry.lower().rstrip("."))
    return hosts, networks


def check_url(url: str) -> None:
    """Refuse ``url`` unless its destination is public or allowlisted.

    Raises:
        EgressDenied: if the URL is malformed, uses a scheme other than
            http/https, cannot be resolved, or resolves to any address
            that is neither globally routable nor allowlisted.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise EgressDenied(f"Invalid URL: {exc}") from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise EgressDenied("Only HTTP and HTTPS URLs are supported")

    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise EgressDenied("URL has no host")
    if port is None:
        port = _DEFAULT_PORTS[scheme]

    allowed_hosts, allowed_networks = _load_allowlist()
    if host.lower() in allowed_hosts:
        return

    try:
        addresses = [_parse_address(host)]
    except ValueError:
        try:
            addresses = [_parse_address(a) for a in _resolve_host(host, port)]
        except (OSError, UnicodeError, ValueError) as exc:
            raise EgressDenied(f"Could not resolve host {host!r}: {exc}") from exc
    if not addresses:
        raise EgressDenied(f"Host {host!r} did not resolve to any address")

    for addr in addresses:
        if _is_public(addr):
            continue
        effective = _effective_address(addr)
        if any(effective in net or addr in net for net in allowed_networks):
            continue
        logger.warning(
            "web egress refused: %s resolves to non-public address %s", host, addr,
        )
        raise EgressDenied(
            f"Destination {host!r} resolves to {addr}, which is not a public "
            "address. Web tools refuse loopback, private, link-local and other "
            "non-global destinations unless listed in web_egress_allowlist."
        )


def check_redirect(response, max_redirects: int, hops: int) -> str | None:
    """Return the next URL if ``response`` is a redirect, after checking it.

    Args:
        response: An httpx response for the current hop.
        max_redirects: Hop limit.
        hops: Hops already taken.

    Returns:
        The absolute URL of the redirect target, or None if ``response``
        is not a redirect.

    Raises:
        EgressDenied: if the target is refused or the hop limit is hit.
    """
    status = getattr(response, "status_code", None)
    if not isinstance(status, int) or not (300 <= status < 400):
        return None
    headers = getattr(response, "headers", None) or {}
    try:
        location = headers.get("location")
    except AttributeError:
        return None
    if not isinstance(location, str) or not location:
        return None
    if hops >= max_redirects:
        raise EgressDenied(f"Too many redirects (limit {max_redirects})")
    base = str(getattr(response, "url", "") or "")
    try:
        import httpx
        target = str(httpx.URL(base).join(location)) if base else location
    except Exception:  # broad catch: malformed Location header
        target = location
    check_url(target)
    return target

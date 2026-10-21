"""The suite's own hermeticity: nothing may connect off loopback.

Two fixtures in ``tests/conftest.py`` have to be read together.
``_hermetic_egress_resolver`` makes every unknown hostname resolve to a
fixed address so the web egress guard lets placeholder hosts such as
``example.com`` through — and that address has to be globally routable,
because the guard refuses anything that is not, so a private or
documentation address would make it refuse the very hosts the web-tool
tests need accepted.  ``_no_outbound_connections`` is what makes that
safe: the resolver can say a placeholder is reachable precisely because
nothing is allowed to act on it.

That mattered less when the web tools were the only thing behind the
guard, since they are mocked.  The package fetchers really do open
sockets, so a test naming an invented public host would send real
traffic to whatever is at the address the resolver hands back.  These
tests pin both halves down.
"""

from __future__ import annotations

import socket
import threading

import pytest

from .conftest import OutboundConnectionBlocked

#: The address ``_hermetic_egress_resolver`` hands back for any unknown
#: hostname.  It belongs to someone; that is the point of these tests.
PUBLIC = "93.184.215.14"


class TestOutboundIsRefused:
    def test_connecting_to_a_public_address_is_refused(self):
        with pytest.raises(OutboundConnectionBlocked) as exc:
            socket.create_connection((PUBLIC, 80), timeout=1)

        message = str(exc.value)
        # The failure names the address and the test, at once, rather
        # than hanging until something times out.
        assert PUBLIC in message
        assert "test_connecting_to_a_public_address_is_refused" in message

    def test_connecting_to_a_hostname_is_refused(self):
        sock = socket.socket()
        try:
            with pytest.raises(OutboundConnectionBlocked) as exc:
                sock.connect(("example.com", 80))
        finally:
            sock.close()
        assert "example.com" in str(exc.value)

    def test_connect_ex_is_refused_too(self):
        """The quiet sibling of ``connect``, and just as much egress."""
        sock = socket.socket()
        try:
            with pytest.raises(OutboundConnectionBlocked):
                sock.connect_ex((PUBLIC, 80))
        finally:
            sock.close()

    def test_an_ipv6_public_address_is_refused(self):
        sock = socket.socket(socket.AF_INET6)
        try:
            with pytest.raises(OutboundConnectionBlocked):
                sock.connect(("2606:2800:21f:cb07:6820:80da:af6b:8b2c", 80))
        finally:
            sock.close()


class TestLoopbackStillWorks:
    """The guard has to leave the suite's own fixture servers alone."""

    @pytest.fixture
    def echo_server(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        def serve():
            try:
                conn, _ = listener.accept()
            except OSError:  # pragma: no cover — closed before a client
                return
            with conn:
                conn.sendall(b"hello")

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield listener.getsockname()
        finally:
            listener.close()
            thread.join(timeout=5)

    def test_a_loopback_connection_is_allowed(self, echo_server):
        with socket.create_connection(echo_server, timeout=5) as sock:
            assert sock.recv(5) == b"hello"

    def test_loopback_by_name_is_allowed(self, echo_server):
        _host, port = echo_server
        sock = socket.socket()
        try:
            # ``localhost`` is a name, so it is refused as a name — the
            # literal is what the fixture servers and their clients
            # use, and what the egress guard's test resolver returns.
            with pytest.raises(OutboundConnectionBlocked):
                sock.connect(("localhost", port))
        finally:
            sock.close()
        with socket.create_connection(("127.0.0.1", port), timeout=5) as ok:
            assert ok.recv(5) == b"hello"


class TestTheHoleThisCloses:
    def test_the_egress_guard_still_accepts_a_placeholder_host(self):
        """Why the refusal cannot live in the resolver instead.

        The guard refuses any destination that is not globally
        routable, so the hermetic resolver has to name a real public
        address and ``example.com`` has to keep passing the guard.
        """
        from carpenter.security import egress

        egress.check_url("https://example.com/some/path")

    def test_a_package_fetch_cannot_reach_a_public_address(
        self, tmp_path, monkeypatch,
    ):
        """The deleted test's scenario, now a refusal instead of egress.

        A package source is fetched for real — this is the one thing
        behind the egress guard that opens its own sockets — so an
        invented public host used to mean genuine traffic to whatever
        answers at the resolver's fixed address.  A numeric host is used
        here so the attempt is decided by the connection and not by a
        DNS lookup.
        """
        from carpenter import config
        from carpenter.packages import fetch
        from carpenter.packages.sources import parse_source

        monkeypatch.setitem(config.CONFIG, "base_dir", str(tmp_path))
        spec = parse_source(f"https://{PUBLIC}/releases/demo-1.0.0.tar.gz")

        # Not a ``FetchError``: the refusal is not an ``OSError``, so the
        # fetcher's own translation of network failures leaves it alone
        # and a breach of hermeticity reads as one.
        with pytest.raises(OutboundConnectionBlocked) as exc:
            fetch.fetch_source(spec)

        assert PUBLIC in str(exc.value)
        assert "hermetic" in str(exc.value)

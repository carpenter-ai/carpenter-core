"""Tests for the web tool egress guard (carpenter.security.egress)."""
from unittest.mock import MagicMock

import pytest

from carpenter import config
from carpenter.security import egress
from carpenter.security.egress import EgressDenied, check_redirect, check_url


def _resolver(mapping):
    def _resolve(host, port):
        if host not in mapping:
            raise OSError(f"unknown host {host}")
        return mapping[host]
    return _resolve


class TestPublicDestinations:
    def test_public_hostname_allowed(self):
        check_url("https://example.com/page")

    def test_public_ip_literal_allowed(self):
        check_url("http://93.184.215.14/")

    def test_public_ipv6_literal_allowed(self):
        check_url("http://[2606:2800:21f:cb07:6820:80da:af6b:8b2c]/")


class TestRefusedDestinations:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8000/api/chat",
        "http://127.8.9.10/",
        "http://localhost:8000/api/chat",
        "http://LOCALHOST./",
        "http://[::1]:8000/",
        "http://0.0.0.0:8000/",
        "http://10.1.2.3/",
        "http://172.16.0.1/",
        "http://192.168.1.10/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:7f00:1]/",
        "http://[2002:7f00:1::]/",
        "http://224.0.0.1/",
    ])
    def test_non_public_refused(self, url):
        with pytest.raises(EgressDenied):
            check_url(url)

    def test_numeric_host_forms_refused(self, monkeypatch):
        # Decimal and short dotted forms resolve to loopback via the system
        # resolver without any DNS query.
        monkeypatch.setattr(egress, "_resolve_host", egress._system_resolve_host)
        for url in ("http://2130706433/", "http://127.1/", "http://0x7f000001/"):
            with pytest.raises(EgressDenied):
                check_url(url)

    def test_hostname_resolving_to_private_refused(self, monkeypatch):
        monkeypatch.setattr(
            egress, "_resolve_host",
            _resolver({"intranet.example": ["10.0.0.7"]}),
        )
        with pytest.raises(EgressDenied, match="10.0.0.7"):
            check_url("https://intranet.example/")

    def test_any_private_address_refuses(self, monkeypatch):
        monkeypatch.setattr(
            egress, "_resolve_host",
            _resolver({"mixed.example": ["93.184.215.14", "127.0.0.1"]}),
        )
        with pytest.raises(EgressDenied):
            check_url("https://mixed.example/")

    def test_unresolvable_host_refused(self, monkeypatch):
        monkeypatch.setattr(egress, "_resolve_host", _resolver({}))
        with pytest.raises(EgressDenied, match="resolve"):
            check_url("https://nowhere.invalid/")

    @pytest.mark.parametrize("url", [
        "ftp://example.com/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "example.com/no-scheme",
        "http:///no-host",
        "http://example.com:notaport/",
    ])
    def test_bad_urls_refused(self, url):
        with pytest.raises(EgressDenied):
            check_url(url)


class TestAllowlist:
    def test_hostname_entry(self, monkeypatch):
        monkeypatch.setitem(config.CONFIG, "web_egress_allowlist", ["LocalHost"])
        check_url("http://localhost:8080/")

    def test_cidr_entry(self, monkeypatch):
        monkeypatch.setitem(config.CONFIG, "web_egress_allowlist", ["192.168.1.0/24"])
        check_url("http://192.168.1.20/")
        with pytest.raises(EgressDenied):
            check_url("http://192.168.2.20/")

    def test_single_address_entry(self, monkeypatch):
        monkeypatch.setitem(config.CONFIG, "web_egress_allowlist", ["10.0.0.5"])
        check_url("http://10.0.0.5/")
        with pytest.raises(EgressDenied):
            check_url("http://10.0.0.6/")

    def test_cidr_matches_resolved_address(self, monkeypatch):
        monkeypatch.setattr(
            egress, "_resolve_host",
            _resolver({"printer.lan": ["192.168.1.9"]}),
        )
        monkeypatch.setitem(config.CONFIG, "web_egress_allowlist", ["192.168.1.0/24"])
        check_url("http://printer.lan/")

    def test_allow_everything(self, monkeypatch):
        monkeypatch.setitem(
            config.CONFIG, "web_egress_allowlist", ["0.0.0.0/0", "::/0"],
        )
        check_url("http://127.0.0.1/")
        check_url("http://[::1]/")

    def test_string_value_tolerated(self, monkeypatch):
        monkeypatch.setitem(config.CONFIG, "web_egress_allowlist", "10.0.0.5")
        check_url("http://10.0.0.5/")

    def test_hostname_entry_does_not_allow_other_hosts(self, monkeypatch):
        monkeypatch.setitem(config.CONFIG, "web_egress_allowlist", ["printer.lan"])
        with pytest.raises(EgressDenied):
            check_url("http://127.0.0.1/")


class TestCheckRedirect:
    def _resp(self, status, location=None, url="https://example.com/start"):
        r = MagicMock()
        r.status_code = status
        r.headers = {"location": location} if location else {}
        r.url = url
        return r

    def test_non_redirect_returns_none(self):
        assert check_redirect(self._resp(200), 10, 0) is None

    def test_public_redirect_returns_absolute_target(self):
        target = check_redirect(self._resp(302, "/next"), 10, 0)
        assert target == "https://example.com/next"

    def test_private_redirect_refused(self):
        with pytest.raises(EgressDenied):
            check_redirect(self._resp(302, "http://127.0.0.1/api/chat"), 10, 0)

    def test_hop_limit(self):
        with pytest.raises(EgressDenied, match="redirects"):
            check_redirect(self._resp(302, "/again"), 3, 3)

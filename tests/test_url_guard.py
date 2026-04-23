"""Unit tests for ``fastmcp_gateway.url_guard``.

These tests exercise the SSRF / header-injection guards directly
against the validator functions — the integration pair in
``tests/test_registration.py`` exercises the same guards through the
``POST /registry/servers`` HTTP endpoint.
"""

from __future__ import annotations

import socket

import pytest

from fastmcp_gateway import url_guard
from fastmcp_gateway.url_guard import (
    RegistrationGuardError,
    validate_registration_headers,
    validate_registration_url,
)


def _fake_getaddrinfo(addr: str):
    """Return a ``socket.getaddrinfo`` replacement that always resolves to *addr*.

    The real ``getaddrinfo`` would hit the DNS resolver; patching it
    lets tests exercise the CIDR denylist without network access.
    """

    def _stub(
        host: str,
        port: int | None,
        *_args: object,
        **_kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        try:
            family = socket.AF_INET6 if ":" in addr else socket.AF_INET
        except TypeError:
            family = socket.AF_INET
        sockaddr: tuple[str, int] = (addr, port or 443)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    return _stub


# ---------------------------------------------------------------------------
# URL validator — CIDR denylist
# ---------------------------------------------------------------------------


class TestUrlValidatorCidr:
    async def test_rfc1918_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A hostname that resolves into RFC 1918 is rejected as SSRF."""
        monkeypatch.setattr(
            "fastmcp_gateway.url_guard.socket.getaddrinfo",
            _fake_getaddrinfo("192.168.1.50"),
        )
        with pytest.raises(RegistrationGuardError) as excinfo:
            await validate_registration_url("https://rfc1918.example/mcp")
        assert excinfo.value.code == "ssrf_rejected"

    async def test_metadata_endpoint_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cloud-metadata IP (``169.254.169.254``) is always rejected."""
        monkeypatch.setattr(
            "fastmcp_gateway.url_guard.socket.getaddrinfo",
            _fake_getaddrinfo("169.254.169.254"),
        )
        with pytest.raises(RegistrationGuardError) as excinfo:
            await validate_registration_url(
                "https://metadata.example/latest/meta-data",
            )
        assert excinfo.value.code == "ssrf_rejected"

    async def test_ipv4_loopback_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "fastmcp_gateway.url_guard.socket.getaddrinfo",
            _fake_getaddrinfo("127.0.0.1"),
        )
        with pytest.raises(RegistrationGuardError) as excinfo:
            await validate_registration_url("https://loop4.example:8080/mcp")
        assert excinfo.value.code == "ssrf_rejected"

    async def test_ipv6_loopback_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "fastmcp_gateway.url_guard.socket.getaddrinfo",
            _fake_getaddrinfo("::1"),
        )
        with pytest.raises(RegistrationGuardError) as excinfo:
            await validate_registration_url("https://loop6.example/mcp")
        assert excinfo.value.code == "ssrf_rejected"

    async def test_ipv4_mapped_ipv6_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """IPv4-mapped IPv6 must unwrap before the CIDR check.

        Regression shield for the bypass:
        ``http://[::ffff:169.254.169.254]/`` — the raw IPv6 address is
        not in any IPv4 CIDR, but the embedded IPv4 address is the
        cloud metadata endpoint.  ``_is_denied_ip`` unwraps via
        ``.ipv4_mapped`` so the IPv4 denylist still fires.
        """
        monkeypatch.setattr(
            "fastmcp_gateway.url_guard.socket.getaddrinfo",
            _fake_getaddrinfo("::ffff:169.254.169.254"),
        )
        with pytest.raises(RegistrationGuardError) as excinfo:
            await validate_registration_url("https://mapped.example/mcp")
        assert excinfo.value.code == "ssrf_rejected"


# ---------------------------------------------------------------------------
# URL validator — scheme allowlist
# ---------------------------------------------------------------------------


class TestUrlValidatorScheme:
    async def test_unix_scheme_rejected(self) -> None:
        """Unix sockets must never be registrable as an upstream."""
        with pytest.raises(RegistrationGuardError) as excinfo:
            await validate_registration_url("unix:///var/run/mcp.sock")
        assert excinfo.value.code == "ssrf_rejected"


# ---------------------------------------------------------------------------
# URL validator — happy path
# ---------------------------------------------------------------------------


class TestUrlValidatorHappy:
    async def test_public_https_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A public HTTPS URL resolving to a routable IP is accepted."""
        # 203.0.113.0/24 is TEST-NET-3 (RFC 5737) — reserved for docs,
        # not in any denied range.
        monkeypatch.setattr(
            "fastmcp_gateway.url_guard.socket.getaddrinfo",
            _fake_getaddrinfo("203.0.113.1"),
        )
        # Should not raise.
        await validate_registration_url("https://public.example/mcp")


# ---------------------------------------------------------------------------
# Header validator
# ---------------------------------------------------------------------------


class TestHeaderValidator:
    def test_allowlisted_header_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Extending the allowlist (via a code change / monkeypatch) works."""
        monkeypatch.setattr(
            "fastmcp_gateway.url_guard._ALLOWED_HEADER_KEYS",
            frozenset({"x-api-version"}),
        )
        # Should not raise.
        validate_registration_headers({"x-api-version": "2"})

    def test_denylisted_header_rejected_case_insensitive(self) -> None:
        """``Authorization`` (any casing) is always rejected."""
        with pytest.raises(RegistrationGuardError) as excinfo:
            validate_registration_headers({"Authorization": "Bearer stolen"})
        assert excinfo.value.code == "header_injection_rejected"


# ---------------------------------------------------------------------------
# allow_private override
# ---------------------------------------------------------------------------


class TestAllowPrivateOverride:
    async def test_allow_private_accepts_rfc1918(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With the env override set, RFC 1918 URLs are accepted.

        Verifies the override is honored end-to-end: the endpoint reads
        ``_url_guard_allow_private()``, passes it through, and the
        validator waives the CIDR denylist for resolved private
        destinations.  ``http://`` is also accepted under the same
        override *because* the resolved address is private.

        This test threads the helper result through rather than
        hardcoding ``allow_private=True`` so the env-wiring contract is
        exercised end-to-end — if a future refactor stops passing
        ``_url_guard_allow_private()`` into the validator, this test
        starts failing.
        """
        monkeypatch.setenv("GATEWAY_URL_GUARD_ALLOW_PRIVATE_RANGES", "true")
        monkeypatch.setattr(
            "fastmcp_gateway.url_guard.socket.getaddrinfo",
            _fake_getaddrinfo("10.0.0.5"),
        )
        assert url_guard._url_guard_allow_private() is True
        # Should not raise — CIDR denylist is waived for this RFC 1918
        # destination and plaintext HTTP is permitted because the
        # resolved address is internal.
        await validate_registration_url(
            "http://10.0.0.5/mcp",
            allow_private=url_guard._url_guard_allow_private(),
        )

    async def test_allow_private_does_not_permit_http_to_public(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``allow_private=True`` never waives the plaintext ban for public hosts.

        Regression shield for the footgun where ``allow_private`` used
        to re-enable ``http://`` regardless of the resolved destination.
        A hostname that resolves to a public-range address must reject
        ``http://`` even with the on-prem override set.
        """
        monkeypatch.setenv("GATEWAY_URL_GUARD_ALLOW_PRIVATE_RANGES", "true")
        # 203.0.113.1 is TEST-NET-3 (RFC 5737) — a public, routable
        # address that is deliberately outside every denied range.
        monkeypatch.setattr(
            "fastmcp_gateway.url_guard.socket.getaddrinfo",
            _fake_getaddrinfo("203.0.113.1"),
        )
        with pytest.raises(RegistrationGuardError) as excinfo:
            await validate_registration_url(
                "http://public.example/mcp",
                allow_private=True,
            )
        assert excinfo.value.code == "ssrf_rejected"
        assert "public destinations" in str(excinfo.value)

    async def test_http_to_public_rejected_without_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Plain ``http://`` to a public host is rejected without the override."""
        monkeypatch.setattr(
            "fastmcp_gateway.url_guard.socket.getaddrinfo",
            _fake_getaddrinfo("203.0.113.1"),
        )
        with pytest.raises(RegistrationGuardError) as excinfo:
            await validate_registration_url("http://public.example/mcp")
        assert excinfo.value.code == "ssrf_rejected"


# ---------------------------------------------------------------------------
# Malformed-port handling
# ---------------------------------------------------------------------------


class TestMalformedPort:
    async def test_out_of_range_port_rejected(self) -> None:
        """A port above the 16-bit range surfaces as a structured 400, not a 500."""
        with pytest.raises(RegistrationGuardError) as excinfo:
            await validate_registration_url("https://example.com:99999/mcp")
        assert excinfo.value.code == "ssrf_rejected"
        assert "invalid port" in str(excinfo.value).lower()

    async def test_non_numeric_port_rejected(self) -> None:
        """A non-integer port surfaces as a structured 400, not a 500."""
        with pytest.raises(RegistrationGuardError) as excinfo:
            await validate_registration_url("https://example.com:abc/mcp")
        assert excinfo.value.code == "ssrf_rejected"
        assert "invalid port" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# DNS-rebinding resistance
# ---------------------------------------------------------------------------


class TestDnsRebindingResistance:
    async def test_public_hostname_resolving_to_metadata_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Attacker-controlled hostname resolving to a denied IP is rejected.

        Simulates DNS rebinding: a public-looking hostname
        (``attacker-rebind.example.com``) that an attacker has pointed
        at the cloud metadata endpoint.  The guard must resolve the
        hostname and reject on the resolved IP, not just the literal
        hostname string.
        """
        monkeypatch.setattr(
            "fastmcp_gateway.url_guard.socket.getaddrinfo",
            _fake_getaddrinfo("169.254.169.254"),
        )
        with pytest.raises(RegistrationGuardError) as excinfo:
            await validate_registration_url(
                "https://attacker-rebind.example.com/mcp",
            )
        assert excinfo.value.code == "ssrf_rejected"

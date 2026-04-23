"""SSRF and header-injection guards for the ``/registry/servers`` endpoint.

The ``POST /registry/servers`` endpoint accepts an arbitrary
``http(s)://`` URL plus a caller-supplied ``headers`` dict, which the
gateway then uses to connect to the registered upstream.  Combined
these two fields are an SSRF + header-smuggling primitive once the
registration token leaks:

* An attacker can point the gateway at internal hosts — cloud metadata
  endpoints (``169.254.169.254``), RFC 1918 private ranges, loopback,
  unix sockets, link-local ranges, CGNAT / tailnet ranges — or use
  DNS rebinding (a public hostname that resolves to a private IP) to
  bypass a naive hostname-based denylist.
* An attacker can inject headers such as ``Host``, ``Authorization``,
  ``Cookie``, ``X-Forwarded-For`` into the gateway-to-upstream
  transport, poisoning request routing, impersonating the gateway to
  other services, or exfiltrating cookies.

This module provides two guard functions and a dedicated exception
that the registration endpoint uses to reject such requests with a
400 + structured error code before any upstream I/O happens.

On-prem / homelab override
--------------------------

Deployments that intentionally register internal upstreams (common in
on-prem / air-gapped environments with network-layer egress controls)
can set the env var:

    GATEWAY_URL_GUARD_ALLOW_PRIVATE_RANGES=true

This disables the **CIDR denylist only**.  Scheme allowlist and
header allowlist/denylist remain in force unconditionally.  The env
var is read per-call (not cached at import) so tests and operators
can toggle it at runtime.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import posixpath
import socket
from urllib.parse import urlparse

# Default timeout for DNS resolution during URL validation.  Capped to
# prevent a slow-resolving hostname from blocking the event loop
# indefinitely when the resolution runs in a worker thread.
_DEFAULT_DNS_TIMEOUT_SECONDS = 5.0

# CIDR blocks rejected by ``validate_registration_url`` unless the
# caller passes ``allow_private=True``.  Covers the ranges an
# SSRF-minded attacker would target to reach internal services:
#
# * RFC 1918 private IPv4 ranges (``10/8``, ``172.16/12``, ``192.168/16``)
# * IPv4 loopback (``127/8``) and IPv6 loopback (``::1/128``)
# * IPv4 link-local (``169.254/16``) — covers AWS / GCP / Azure
#   instance metadata service at ``169.254.169.254``
# * IPv6 link-local (``fe80::/10``)
# * CGNAT (``100.64/10``) — covers Tailscale and similar overlay
#   networks that assign addresses in this range
# * IPv6 unique-local (``fc00::/7``)
# * IPv4-mapped IPv6 (``::ffff:0:0/96``) — defense in depth; the
#   validator also unwraps mapped addresses before the IPv4 check
# * Multicast (``224/4`` IPv4, ``ff00::/8`` IPv6)
# * Unspecified (``0.0.0.0/8``)
_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_LOOPBACK4 = ipaddress.ip_network("127.0.0.0/8")
_LOOPBACK6 = ipaddress.ip_network("::1/128")
_LINK_LOCAL4 = ipaddress.ip_network("169.254.0.0/16")
_LINK_LOCAL6 = ipaddress.ip_network("fe80::/10")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_UNIQUE_LOCAL6 = ipaddress.ip_network("fc00::/7")
_IPV4_MAPPED = ipaddress.ip_network("::ffff:0:0/96")
_MULTICAST4 = ipaddress.ip_network("224.0.0.0/4")
_MULTICAST6 = ipaddress.ip_network("ff00::/8")
_UNSPECIFIED = ipaddress.ip_network("0.0.0.0/8")

_DENIED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    *_RFC1918,
    _LOOPBACK4,
    _LOOPBACK6,
    _LINK_LOCAL4,
    _LINK_LOCAL6,
    _CGNAT,
    _UNIQUE_LOCAL6,
    _IPV4_MAPPED,
    _MULTICAST4,
    _MULTICAST6,
    _UNSPECIFIED,
)

# Schemes permitted for registered upstream URLs.  Anything else
# (``unix``, ``file``, ``ftp``, ``gopher``, ``data``, ...) is rejected
# by omission.  ``http`` is permitted only with ``allow_private=True``
# because plaintext connections to public internet endpoints are a
# credential-theft risk for any upstream that requires auth.
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Header allowlist — empty by default.  Every caller-supplied header
# is rejected unless its lowercase key appears in this set.  This is
# an intentionally strict default: third-party adopters who need to
# permit specific headers must extend the allowlist via a code change,
# not an env toggle, so the threat model is auditable at review time.
_ALLOWED_HEADER_KEYS: frozenset[str] = frozenset()

# Header denylist — header keys that are never acceptable even if an
# operator extends ``_ALLOWED_HEADER_KEYS``.  These are hop-by-hop
# headers (``Connection``, ``Transfer-Encoding``, ...) or headers
# that control upstream routing / auth (``Host``, ``Authorization``,
# ``X-Forwarded-*``).  Letting any of these through lets a caller
# smuggle routing or auth state into the gateway-to-upstream request.
_DENIED_HEADER_KEYS: frozenset[str] = frozenset(
    {
        "host",
        "authorization",
        "cookie",
        "set-cookie",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "connection",
    }
)


class RegistrationGuardError(ValueError):
    """Raised when a registration request fails an SSRF or header guard.

    Subclasses :class:`ValueError` so existing ``try/except ValueError``
    callers keep working.  Carries a machine-readable ``code`` attribute
    (``"ssrf_rejected"`` or ``"header_injection_rejected"``) that the
    registration endpoint surfaces to the caller as the ``code`` field
    in the structured 400 response body.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code: str = code


def _url_guard_allow_private() -> bool:
    """Return whether the CIDR denylist is disabled for this call.

    Reads ``GATEWAY_URL_GUARD_ALLOW_PRIVATE_RANGES`` on every call
    (not cached at import) so tests can ``monkeypatch.setenv`` /
    ``delenv`` and operators can toggle the behavior at runtime.
    """
    return os.environ.get("GATEWAY_URL_GUARD_ALLOW_PRIVATE_RANGES", "").lower() == "true"


def _dns_timeout_seconds() -> float:
    """Return the DNS-resolution timeout, read per-call from the env."""
    raw = os.environ.get("GATEWAY_URL_GUARD_DNS_TIMEOUT_SECONDS")
    if not raw:
        return _DEFAULT_DNS_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_DNS_TIMEOUT_SECONDS
    # Negative / zero timeouts would cause ``asyncio.wait_for`` to
    # raise immediately.  Fall back to the default in that case rather
    # than silently breaking hostname resolution.
    if value <= 0:
        return _DEFAULT_DNS_TIMEOUT_SECONDS
    return value


def _is_denied_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if *ip* falls in any denied CIDR block.

    IPv6 addresses that carry an embedded IPv4 address (``::ffff:a.b.c.d``)
    are unwrapped via ``.ipv4_mapped`` and the resulting IPv4 address is
    checked against the IPv4 denylist.  Without this step an attacker
    can bypass IPv4-only denylists by submitting the mapped form —
    e.g. ``http://[::ffff:169.254.169.254]/`` hits the cloud metadata
    endpoint but the raw IPv6 address would not match any IPv4 CIDR.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    for network in _DENIED_NETWORKS:
        # ``network.version`` / ``ip.version`` must match before
        # ``in`` — mixing IPv4 and IPv6 raises ``TypeError``.
        if ip.version != network.version:
            continue
        if ip in network:
            return True
    return False


async def _resolve_host(hostname: str, port: int) -> list[str]:
    """Resolve *hostname* to a list of IP-address strings.

    ``socket.getaddrinfo`` is blocking, so it's dispatched to a worker
    thread via :func:`asyncio.to_thread` and bounded by the env-configurable
    timeout.  A resolution failure (``socket.gaierror``) surfaces as
    an empty list, which the caller treats as "cannot verify, reject".
    """

    def _lookup() -> list[str]:
        try:
            infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            return []
        # ``getaddrinfo`` returns tuples of
        # ``(family, type, proto, canonname, sockaddr)``.
        # ``sockaddr`` is ``(addr, port)`` for IPv4 and
        # ``(addr, port, flowinfo, scopeid)`` for IPv6 — ``addr`` is
        # always the first element.  The type stubs declare
        # ``sockaddr[0]`` as ``str | int`` (the AF_UNIX case), so
        # coerce to ``str`` for the downstream ``ip_address`` parse.
        return [str(info[4][0]) for info in infos]

    timeout = _dns_timeout_seconds()
    try:
        return await asyncio.wait_for(asyncio.to_thread(_lookup), timeout=timeout)
    except TimeoutError:
        return []


async def validate_registration_url(url: str, *, allow_private: bool = False) -> None:
    """Validate an upstream URL before registering it.

    Raises :class:`RegistrationGuardError` with ``code="ssrf_rejected"``
    when the URL fails any of the following checks (evaluated in order):

    1. Scheme must be ``http`` or ``https``.  Everything else
       (``unix``, ``file``, ``ftp``, ``gopher``, ...) is rejected.
    2. Hostname must be non-empty and must not be the string
       ``localhost`` (case-insensitive).
    3. The URL path, after ``posixpath.normpath``, must not contain
       ``..`` — a traversal segment that survived normalization.
    4. The URL port, if present, must parse as a valid integer in
       the standard TCP/UDP range.  Malformed ports (e.g. ``:99999``
       or ``:abc``) produce a structured 400 rather than a 500.
    5. The hostname is resolved via ``socket.getaddrinfo`` and every
       returned address is classified against :data:`_DENIED_NETWORKS`.
       Any address in a denied range is rejected unless *allow_private*
       is True.  This is the DNS-rebinding defense: an attacker-controlled
       public hostname that resolves to ``169.254.169.254`` is rejected
       even though the literal hostname is not in any denylist.
    6. ``http://`` is permitted only for resolved destinations that all
       fall inside :data:`_DENIED_NETWORKS` (i.e., actually-private
       destinations).  Plaintext HTTP to any public-range address is a
       credential-theft risk and is rejected even with *allow_private*
       set — the flag relaxes the private-range denial, not the
       plaintext ban on public hosts.

    Parameters
    ----------
    url:
        The candidate upstream URL, as supplied by the registration
        caller.
    allow_private:
        When True, the CIDR denylist is relaxed so resolved destinations
        in private / loopback / link-local / CGNAT ranges are accepted.
        The plaintext-``http`` ban is *only* waived for those
        actually-private destinations — ``http://public.example/mcp``
        is rejected with *allow_private* True or False.  The scheme
        allowlist and the hostname non-emptiness / ``localhost`` check
        always apply.  Set by the registration endpoint when
        ``GATEWAY_URL_GUARD_ALLOW_PRIVATE_RANGES`` is ``true``.
    """
    parsed = urlparse(url)

    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise RegistrationGuardError(
            code="ssrf_rejected",
            message=(f"URL scheme {scheme!r} is not permitted; use 'http' or 'https'."),
        )

    hostname = parsed.hostname
    if not hostname:
        raise RegistrationGuardError(
            code="ssrf_rejected",
            message="URL hostname is empty.",
        )
    if hostname.lower() == "localhost":
        raise RegistrationGuardError(
            code="ssrf_rejected",
            message="URL hostname 'localhost' is not permitted.",
        )

    # ``posixpath.normpath`` collapses ``foo/./bar`` and ``foo/../bar``.
    # Any surviving ``..`` segment after normalization is a traversal
    # attempt that we refuse to forward to the upstream.
    normalized_path = posixpath.normpath(parsed.path or "/")
    if ".." in normalized_path.split("/"):
        raise RegistrationGuardError(
            code="ssrf_rejected",
            message="URL path contains '..' traversal segments after normalization.",
        )

    # ``parsed.port`` is a computed property that raises ``ValueError``
    # on a malformed port (out-of-range integer or non-numeric), so
    # wrap the access to convert that into a structured 400 rather
    # than letting it bubble up as an unhandled 500.
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise RegistrationGuardError(
            code="ssrf_rejected",
            message=f"URL {url!r} contains an invalid port: {exc}.",
        ) from None

    resolved = await _resolve_host(hostname, port)
    if not resolved:
        raise RegistrationGuardError(
            code="ssrf_rejected",
            message=f"DNS resolution for {hostname!r} failed or timed out.",
        )

    # Classify every resolved address.  ``http://`` is only permitted
    # when **all** resolved addresses are internal; a single public
    # address in the returned set is enough to reject the URL under
    # plaintext-HTTP, even if other addresses happen to be private
    # (avoids a DNS-rebinding variant where an attacker controls a
    # hostname that resolves to both a private and a public IP).
    all_internal = True
    for addr_string in resolved:
        # ``getaddrinfo`` may return an IPv6 address with a scope id
        # (``fe80::1%eth0``); ``ip_address`` does not accept the scope
        # suffix, so strip it before parsing.
        bare_addr = addr_string.split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(bare_addr)
        except ValueError:
            # Unparseable address string is itself suspicious; reject
            # rather than allow through on a parse failure.
            raise RegistrationGuardError(
                code="ssrf_rejected",
                message=(f"DNS resolution for {hostname!r} returned an unparseable address {addr_string!r}."),
            ) from None

        is_internal = _is_denied_ip(ip_obj)
        if is_internal and not allow_private:
            raise RegistrationGuardError(
                code="ssrf_rejected",
                message=(
                    f"Hostname {hostname!r} resolved to {bare_addr!r}, "
                    "which is in a denied network range (loopback, "
                    "private, link-local, CGNAT, or metadata-endpoint)."
                ),
            )
        if not is_internal:
            all_internal = False

    if scheme == "http" and not all_internal:
        raise RegistrationGuardError(
            code="ssrf_rejected",
            message=(
                "Plaintext 'http://' URLs are not permitted to public "
                "destinations; use 'https://'.  The "
                "GATEWAY_URL_GUARD_ALLOW_PRIVATE_RANGES override only "
                "waives the ban for resolved addresses that are inside "
                "loopback, RFC 1918, link-local, CGNAT, or metadata ranges."
            ),
        )


def validate_registration_headers(headers: dict[str, str]) -> None:
    """Validate caller-supplied headers attached to an upstream registration.

    Raises :class:`RegistrationGuardError` with
    ``code="header_injection_rejected"`` when any header key:

    * Appears in :data:`_DENIED_HEADER_KEYS` (case-insensitive).
      These are hop-by-hop headers or headers that control routing /
      auth, and must never cross the gateway-to-upstream boundary
      under caller control.
    * Does not appear in :data:`_ALLOWED_HEADER_KEYS` (case-insensitive,
      after ``.strip()``).  The allowlist is empty by default; third-party
      adopters extend it via a code change, not an env toggle.

    Empty keys (after stripping) are always rejected.
    """
    for key in headers:
        normalized = key.lower().strip()
        if not normalized:
            raise RegistrationGuardError(
                code="header_injection_rejected",
                message=f"Header key {key!r} is empty after normalization.",
            )
        if normalized in _DENIED_HEADER_KEYS:
            raise RegistrationGuardError(
                code="header_injection_rejected",
                message=(
                    f"Header {key!r} is on the denylist (routing / auth / "
                    "hop-by-hop) and cannot be forwarded to the upstream."
                ),
            )
        if normalized not in _ALLOWED_HEADER_KEYS:
            raise RegistrationGuardError(
                code="header_injection_rejected",
                message=(
                    f"Header {key!r} is not on the allowlist; extend "
                    "fastmcp_gateway.url_guard._ALLOWED_HEADER_KEYS "
                    "via a code change to permit it."
                ),
            )

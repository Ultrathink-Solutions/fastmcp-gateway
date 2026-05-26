"""Tests for the differentiated error response model on POST /registry/servers.

The endpoint maps add_upstream() failures into three response classes
so controller-style callers can disambiguate retry-worthy errors from
caller-fixable config errors from genuine internal failures:

  * 503 + Retry-After  — transient upstream-unreachable (caller retries)
  * 422                — upstream auth/authz rejection (caller-fixable)
  * 500                — everything else (escalation-worthy)

Pre-fix, all three classes returned 500, leaving controllers unable
to differentiate startup-window noise from real problems.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import FastMCP
from httpx import ASGITransport, AsyncClient

from fastmcp_gateway.gateway import (
    GatewayServer,
    _scrub_url_for_diagnostics,  # pyright: ignore[reportAttributeAccessIssue]
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REGISTRATION_TOKEN = "test-secret-token"

pytestmark = pytest.mark.filterwarnings("ignore:registration_token is deprecated:DeprecationWarning")


def _create_seed_server() -> FastMCP:
    mcp = FastMCP("seed-upstream")

    @mcp.tool()
    def seed_ping() -> str:
        """Echo a ping."""
        return json.dumps({"ok": True})

    return mcp


@pytest.fixture
def seed_server() -> FastMCP:
    return _create_seed_server()


@pytest.fixture
async def gateway(seed_server: FastMCP) -> GatewayServer:
    gw = GatewayServer(
        {"seed": seed_server},  # type: ignore[dict-item]
        registration_token=REGISTRATION_TOKEN,
    )
    await gw.populate()
    return gw


def _auth_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {REGISTRATION_TOKEN}"}


async def _client(gateway: GatewayServer) -> AsyncClient:
    app = gateway.mcp.http_app(transport="streamable-http")
    # raise_app_exceptions=False lets Starlette's default ASGI
    # exception handler turn an uncaught route exception into the
    # 500 the production server would return, instead of re-raising
    # back through the test transport. That mirrors what the
    # registry-controller actually sees on the wire.
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


# RFC 5737 TEST-NET-3 — guaranteed-non-routable public IP block used
# in documentation. Public-classification per the URL guard's CIDR
# check (i.e. not RFC1918, not loopback, not link-local, not metadata),
# so the guard accepts the registration without our needing to stub
# DNS resolution.
_PUBLIC_TEST_IP = "203.0.113.5"


async def _post_register(
    gateway: GatewayServer,
    *,
    domain: str = "widgets",
    url: str | None = None,
    discovery_url: str | None = None,
) -> httpx.Response:
    """POST /registry/servers with a basic payload.

    Uses an RFC 5737 documentation IP so the URL guard passes without
    any DNS-resolution stubbing.
    """
    resolved_url = url or f"https://{_PUBLIC_TEST_IP}:8080/mcp"
    body: dict[str, str] = {"domain": domain, "url": resolved_url}
    if discovery_url is not None:
        body["discovery_url"] = discovery_url
    async with await _client(gateway) as http:
        return await http.post("/registry/servers", headers=_auth_headers(), json=body)


def _patch_add_upstream(gateway: GatewayServer, exc: BaseException) -> None:
    """Stub UpstreamManager.add_upstream to raise *exc* on next call."""

    async def _raiser(*_args: object, **_kwargs: object) -> None:
        raise exc

    gateway.upstream_manager.add_upstream = _raiser  # type: ignore[method-assign]


def _make_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError carrying *status_code* on its response."""
    request = httpx.Request("POST", f"https://{_PUBLIC_TEST_IP}:8080/mcp")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("upstream rejected", request=request, response=response)


# ---------------------------------------------------------------------------
# Transient errors → 503 + Retry-After
# ---------------------------------------------------------------------------


class TestTransientErrorsMapTo503:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc_cls",
        [
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
        ],
    )
    async def test_returns_503_with_retry_after(self, gateway: GatewayServer, exc_cls: type[BaseException]) -> None:
        _patch_add_upstream(gateway, exc_cls("upstream not yet reachable"))

        response = await _post_register(gateway)

        assert response.status_code == 503
        assert "Retry-After" in response.headers
        retry_after = int(response.headers["Retry-After"])
        assert retry_after == 5

        body = response.json()
        assert body["code"] == "upstream_not_ready"
        assert body["domain"] == "widgets"
        assert body["retry_after_seconds"] == 5
        # Error message names the exception class so the operator can
        # disambiguate ConnectError from ReadTimeout without leaving
        # the response body.
        assert exc_cls.__name__ in body["error"]

    @pytest.mark.asyncio
    async def test_mcp_error_connection_closed_maps_to_503(self, gateway: GatewayServer) -> None:
        """``McpError`` carrying ``CONNECTION_CLOSED`` is the only transport-class code.

        The MCP SDK's ``McpError`` is the unified wrapper for *any* error
        arriving over an MCP connection — both transport-class drops and
        peer-application protocol errors. Only the JSON-RPC codes that
        unambiguously represent a dropped session (``-32000``
        ``CONNECTION_CLOSED``) are retryable; the other reserved-range
        codes are tested in :class:`TestUnclassifiedErrorsMapTo500`.
        """
        try:
            from mcp.shared.exceptions import McpError
            from mcp.types import CONNECTION_CLOSED, ErrorData
        except ImportError:
            pytest.skip("mcp SDK exception module layout differs in this env")
        err_data = ErrorData(code=CONNECTION_CLOSED, message="upstream session dropped")
        _patch_add_upstream(gateway, McpError(err_data))

        response = await _post_register(gateway)
        assert response.status_code == 503
        assert response.json()["code"] == "upstream_not_ready"


# ---------------------------------------------------------------------------
# Upstream auth failures → 422
# ---------------------------------------------------------------------------


class TestUpstreamAuthErrorsMapTo422:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [401, 403])
    async def test_returns_422_on_unauthorized_or_forbidden(self, gateway: GatewayServer, status_code: int) -> None:
        _patch_add_upstream(gateway, _make_status_error(status_code))

        response = await _post_register(gateway)

        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "upstream_auth_failed"
        assert body["domain"] == "widgets"
        assert body["upstream_status"] == status_code
        # Stable contract: error key is present and non-empty.
        # Specific message copy is operator-facing prose that may
        # evolve without changing the machine-parseable contract
        # (code, status_code, domain, upstream_status).
        assert isinstance(body["error"], str)
        assert body["error"]


# ---------------------------------------------------------------------------
# Non-classifiable upstream errors → 500 (reserved for genuine internal failures)
# ---------------------------------------------------------------------------


class TestUnclassifiedErrorsMapTo500:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [404, 500, 502])
    async def test_non_auth_upstream_status_falls_through_to_500(
        self, gateway: GatewayServer, status_code: int
    ) -> None:
        """HTTPStatusError with non-401/403 status is not actionable by retry policy."""
        _patch_add_upstream(gateway, _make_status_error(status_code))

        response = await _post_register(gateway)

        # 422 is reserved for caller-fixable auth errors. Other upstream
        # status codes are not actionable by a controller-side retry
        # policy, so they should surface as a generic 500 — escalation-
        # worthy rather than "stop hammering this".
        assert response.status_code == 500

    @pytest.mark.asyncio
    async def test_arbitrary_exception_falls_through_to_500(self, gateway: GatewayServer) -> None:
        """A bare RuntimeError is a genuine internal failure → 500."""
        _patch_add_upstream(gateway, RuntimeError("registry corruption"))

        response = await _post_register(gateway)
        assert response.status_code == 500

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("code_name",),
        [
            ("METHOD_NOT_FOUND",),  # -32601 — peer doesn't implement tools/list
            ("INVALID_PARAMS",),  # -32602 — request shape mismatch
            ("INTERNAL_ERROR",),  # -32603 — peer-internal error, NOT transient
            ("PARSE_ERROR",),  # -32700 — peer couldn't parse our JSON
        ],
    )
    async def test_non_transient_mcp_error_codes_fall_through_to_500(
        self, gateway: GatewayServer, code_name: str
    ) -> None:
        """``McpError`` codes outside the transport-class set are not retryable.

        A controller looping on ``METHOD_NOT_FOUND`` or ``INVALID_PARAMS``
        would never make progress — these are peer-application protocol
        errors, not boot-window noise. They must fall through to the
        generic 500 path so they surface as escalation-worthy rather
        than as a stop-hammering 503.
        """
        try:
            from mcp.shared.exceptions import McpError
            from mcp.types import ErrorData
        except ImportError:
            pytest.skip("mcp SDK exception module layout differs in this env")
        from mcp import types

        code = getattr(types, code_name)
        err_data = ErrorData(code=code, message=f"peer reported {code_name}")
        _patch_add_upstream(gateway, McpError(err_data))

        response = await _post_register(gateway)
        # 503 reserved for transport-class transients; 422 for caller-
        # fixable auth. Protocol-class McpError falls through to 500.
        assert response.status_code == 500


# ---------------------------------------------------------------------------
# Transactional add_upstream: probe failure / refusal must not commit
# ---------------------------------------------------------------------------


class TestRegistrationIsTransactional:
    """``UpstreamManager.add_upstream`` rolls back on probe failure or refusal.

    Without these guarantees, a transient discovery error during an
    idempotent upsert would leave the manager pointing at the (unverified)
    new URL while the registry still carries the old tools — and
    ``execute_tool()`` would route to an upstream that never confirmed it
    was reachable.
    """

    @pytest.mark.asyncio
    async def test_transient_probe_failure_rolls_back_new_domain(self, gateway: GatewayServer) -> None:
        """Brand-new domain whose probe fails must not appear in ``list_upstreams``.

        Drives the real ``add_upstream`` path (no monkey-patch) so the
        rollback in ``UpstreamManager.add_upstream`` exercises end-to-end.
        We force the probe to fail by patching ``_populate_domain`` on
        the manager instance.
        """

        async def _fail_populate(*_args: object, **_kwargs: object) -> object:
            raise httpx.ConnectError("upstream not yet reachable")

        prior_domains = set(gateway.upstream_manager.list_upstreams().keys())
        gateway.upstream_manager._populate_domain = _fail_populate  # type: ignore[method-assign]

        response = await _post_register(gateway, domain="brand-new")
        assert response.status_code == 503

        # The transient probe failure must NOT leave 'brand-new' visible
        # in the manager — list_upstreams() and execute_tool() both read
        # from the same internal dicts that add_upstream stages, so a
        # leaked entry would surface here.
        assert set(gateway.upstream_manager.list_upstreams().keys()) == prior_domains
        assert "brand-new" not in gateway.upstream_manager.domains

    @pytest.mark.asyncio
    async def test_transient_probe_failure_preserves_prior_url_on_upsert(
        self, gateway: GatewayServer, seed_server: FastMCP
    ) -> None:
        """Upsert: a failed probe for the new URL must leave the prior URL in place."""
        # Register a real upstream first to give the upsert a prior URL
        # to preserve. Drives the real add_upstream path.
        await gateway.upstream_manager.add_upstream("widgets", seed_server)  # type: ignore[arg-type]
        prior_url = gateway.upstream_manager.upstream_url("widgets")

        async def _fail_populate(*_args: object, **_kwargs: object) -> object:
            raise httpx.ConnectError("v2 not yet reachable")

        gateway.upstream_manager._populate_domain = _fail_populate  # type: ignore[method-assign]

        response = await _post_register(
            gateway,
            domain="widgets",
            url=f"https://{_PUBLIC_TEST_IP}:9090/v2/mcp",
        )
        assert response.status_code == 503

        # The rolled-back state must continue to expose the prior URL
        # so execute_tool() keeps routing where it did before the POST.
        assert gateway.upstream_manager.upstream_url("widgets") == prior_url

    @pytest.mark.asyncio
    async def test_refused_diff_returns_409_and_rolls_back(self, gateway: GatewayServer) -> None:
        """``diff.refused=True`` must surface as 409 without touching shared state.

        Schema-integrity refusals are caller-fixable (operator needs to
        explicitly acknowledge the new digest), so they're not 500 and
        not 503. The manager-side rollback ensures ``execute_tool()``
        continues to read the committed structures.
        """
        from fastmcp_gateway.registry import RegistryDiff

        async def _refused_add(*_args: object, **_kwargs: object) -> RegistryDiff:
            # Return a refused diff WITHOUT raising, mirroring what
            # registry.populate_domain does on schema-digest mismatch.
            return RegistryDiff(
                domain="widgets",
                added=[],
                removed=[],
                tool_count=0,
                schema_digest="0" * 64,
                schema_digest_changed=False,
                refused=True,
            )

        gateway.upstream_manager.add_upstream = _refused_add  # type: ignore[method-assign]

        response = await _post_register(gateway)

        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "schema_refused"
        assert body["domain"] == "widgets"


# ---------------------------------------------------------------------------
# Happy path is unchanged
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# URL scrubbing — error responses + logs must not echo back userinfo / query
# ---------------------------------------------------------------------------


class TestScrubUrlForDiagnostics:
    """Unit tests for the scrub helper."""

    def test_drops_userinfo(self) -> None:
        scrubbed = _scrub_url_for_diagnostics("https://user:secret@upstream.example/mcp")
        assert "user" not in scrubbed
        assert "secret" not in scrubbed
        assert scrubbed == "https://upstream.example/mcp"

    def test_drops_query_string(self) -> None:
        scrubbed = _scrub_url_for_diagnostics("https://upstream.example/mcp?token=sk-deadbeef&signature=xyz")
        assert "token" not in scrubbed
        assert "sk-deadbeef" not in scrubbed
        assert scrubbed == "https://upstream.example/mcp"

    def test_drops_fragment(self) -> None:
        scrubbed = _scrub_url_for_diagnostics("https://upstream.example/mcp#fragment-secret")
        assert "fragment-secret" not in scrubbed
        assert scrubbed == "https://upstream.example/mcp"

    def test_drops_all_three_in_combination(self) -> None:
        scrubbed = _scrub_url_for_diagnostics("https://user:secret@upstream.example:8443/mcp?api_key=abc#frag")
        assert scrubbed == "https://upstream.example:8443/mcp"
        for leak in ("user", "secret", "api_key", "abc", "frag"):
            assert leak not in scrubbed

    def test_preserves_port(self) -> None:
        assert _scrub_url_for_diagnostics("http://upstream:8080/mcp") == "http://upstream:8080/mcp"

    def test_preserves_path_only(self) -> None:
        assert (
            _scrub_url_for_diagnostics("https://upstream.example/some/deep/path")
            == "https://upstream.example/some/deep/path"
        )

    def test_unparseable_url_returns_placeholder(self) -> None:
        # Empty string has no scheme + no hostname → placeholder.
        assert _scrub_url_for_diagnostics("") == "<unparseable-url>"
        assert _scrub_url_for_diagnostics("not-a-url") == "<unparseable-url>"

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            pytest.param(
                "http://[::1]:8080/mcp",
                "http://[::1]:8080/mcp",
                id="ipv6-loopback-with-port",
            ),
            pytest.param(
                "https://[2001:db8::1]/mcp",
                "https://[2001:db8::1]/mcp",
                id="ipv6-no-port",
            ),
            pytest.param(
                "https://user:secret@[2001:db8::1]:8443/mcp?k=v#f",
                "https://[2001:db8::1]:8443/mcp",
                id="ipv6-with-userinfo-port-query-fragment",
            ),
        ],
    )
    def test_ipv6_hosts_keep_brackets(self, url: str, expected: str) -> None:
        """IPv6 hosts must be re-bracketed before being composed into netloc.

        ``urlsplit`` returns ``parts.hostname`` without brackets
        (``[::1]:80`` → ``"::1"``), so a naive ``f"{host}:{port}"``
        would yield ``"::1:80"`` — ambiguous to downstream parsers
        (the host could be ``::1:80`` with no port, or ``::1`` with
        port ``80``). The helper restores brackets for any host
        carrying a colon.
        """
        assert _scrub_url_for_diagnostics(url) == expected

    def test_invalid_port_treated_as_absent(self) -> None:
        """Non-numeric / out-of-range port must not raise.

        ``parts.port`` is a property that raises ``ValueError`` for
        strings like ``"abc"`` or numbers outside [0, 65535]. The
        scrubber's contract is to never raise on caller-supplied
        input, so the bad port is silently dropped and the URL is
        reconstructed without one.
        """
        assert _scrub_url_for_diagnostics("http://host:abc/mcp") == "http://host/mcp"
        assert _scrub_url_for_diagnostics("http://host:99999/mcp") == "http://host/mcp"


class TestRegistrationErrorResponseScrubsUrls:
    """Integration tests: the 503 / 422 response bodies must not echo secrets."""

    @pytest.mark.asyncio
    async def test_503_response_body_does_not_contain_userinfo_or_query(self, gateway: GatewayServer) -> None:
        _patch_add_upstream(gateway, httpx.ConnectError("not yet reachable"))
        leaky_url = f"https://probe-user:probe-secret@{_PUBLIC_TEST_IP}:8080/mcp?token=sk-deadbeef"

        response = await _post_register(gateway, discovery_url=leaky_url)

        assert response.status_code == 503
        body_text = response.text
        for leak in ("probe-user", "probe-secret", "token=sk-deadbeef", "sk-deadbeef"):
            assert leak not in body_text, f"secret leaked into 503 body: {leak!r}"

    @pytest.mark.asyncio
    async def test_422_response_body_does_not_contain_userinfo_or_query(self, gateway: GatewayServer) -> None:
        _patch_add_upstream(gateway, _make_status_error(401))
        leaky_url = f"https://probe-user:probe-secret@{_PUBLIC_TEST_IP}:8080/mcp?token=sk-deadbeef"

        response = await _post_register(gateway, discovery_url=leaky_url)

        assert response.status_code == 422
        body_text = response.text
        for leak in ("probe-user", "probe-secret", "token=sk-deadbeef", "sk-deadbeef"):
            assert leak not in body_text, f"secret leaked into 422 body: {leak!r}"


class TestSuccessfulRegistrationUnchanged:
    @pytest.mark.asyncio
    async def test_registration_returns_200_on_success(self, gateway: GatewayServer) -> None:
        """Successful add_upstream still returns the 200 + tools_discovered body.

        Regression coverage that the new try/except wrapping did not
        accidentally change the success-path response shape.
        """

        async def _ok_register(*_args: object, **_kwargs: object) -> object:
            from fastmcp_gateway.registry import RegistryDiff

            return RegistryDiff(domain="widgets", added=["new_tool"], removed=[], tool_count=1)

        gateway.upstream_manager.add_upstream = _ok_register  # type: ignore[method-assign]

        response = await _post_register(gateway)

        assert response.status_code == 200
        body = response.json()
        assert body["registered"] == "widgets"
        assert body["tools_discovered"] == 1
        assert body["tools_added"] == ["new_tool"]

"""Tests for dynamic upstream registration API (POST/DELETE/GET /registry/servers)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC

import pytest
from fastmcp import FastMCP
from httpx import ASGITransport, AsyncClient

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.gateway import GatewayServer
from fastmcp_gateway.registration_auth import RegistrationClaims
from fastmcp_gateway.registry import ToolRegistry

# These tests deliberately exercise the deprecated static-bearer
# registration path, which emits a ``DeprecationWarning`` on each
# GatewayServer construction (the migration notice introduced alongside
# the JWT validator).  Silencing the warning at the module level keeps
# the test output focused on actual failures; the mutual-exclusion
# and JWT tests in test_registration_auth.py cover the new path.
pytestmark = pytest.mark.filterwarnings("ignore:registration_token is deprecated:DeprecationWarning")

# ---------------------------------------------------------------------------
# Mock upstream MCP servers
# ---------------------------------------------------------------------------

REGISTRATION_TOKEN = "test-secret-token"


def _create_sales_server() -> FastMCP:
    mcp = FastMCP("sales-upstream")

    @mcp.tool()
    def sales_contacts_search(query: str) -> str:
        """Search sales contacts."""
        return json.dumps({"contacts": [{"name": "Alice"}]})

    @mcp.tool()
    def sales_deals_list() -> str:
        """List sales deals."""
        return json.dumps({"deals": [{"id": "d-1"}]})

    return mcp


def _create_support_server() -> FastMCP:
    mcp = FastMCP("support-upstream")

    @mcp.tool()
    def support_tickets_list() -> str:
        """List support tickets."""
        return json.dumps({"tickets": [{"id": "t-1"}]})

    return mcp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sales_server() -> FastMCP:
    return _create_sales_server()


@pytest.fixture
def support_server() -> FastMCP:
    return _create_support_server()


@pytest.fixture
async def gateway_with_registration(sales_server: FastMCP) -> GatewayServer:
    """A gateway with registration enabled and one initial upstream."""
    gateway = GatewayServer(
        {"sales": sales_server},  # type: ignore[dict-item]
        registration_token=REGISTRATION_TOKEN,
    )
    await gateway.populate()
    return gateway


@pytest.fixture
async def gateway_no_registration(sales_server: FastMCP) -> GatewayServer:
    """A gateway without registration (registration_token not set)."""
    gateway = GatewayServer(
        {"sales": sales_server},  # type: ignore[dict-item]
    )
    await gateway.populate()
    return gateway


def _auth_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {REGISTRATION_TOKEN}"}


async def _http_client(gateway: GatewayServer) -> AsyncClient:
    """Create an httpx AsyncClient backed by the gateway's ASGI app."""
    app = gateway.mcp.http_app(transport="streamable-http")
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://testserver")


# ---------------------------------------------------------------------------
# UpstreamManager.add_upstream / remove_upstream (unit tests)
# ---------------------------------------------------------------------------


class TestUpstreamManagerAddRemove:
    @pytest.mark.asyncio
    async def test_add_upstream(self, sales_server: FastMCP, support_server: FastMCP) -> None:
        registry = ToolRegistry()
        manager = UpstreamManager(
            {"sales": sales_server},  # type: ignore[dict-item]
            registry,
        )
        await manager.populate_all()
        assert registry.tool_count == 2  # sales has 2 tools

        diff = await manager.add_upstream("support", support_server)  # type: ignore[arg-type]
        assert diff.tool_count == 1
        assert diff.domain == "support"
        assert "support_tickets_list" in diff.added
        assert registry.tool_count == 3

    @pytest.mark.asyncio
    async def test_add_upstream_upsert(self, sales_server: FastMCP) -> None:
        """Adding an existing domain re-populates (upsert)."""
        registry = ToolRegistry()
        manager = UpstreamManager(
            {"sales": sales_server},  # type: ignore[dict-item]
            registry,
        )
        await manager.populate_all()
        assert registry.tool_count == 2

        # Re-add same domain — should still have 2 tools
        diff = await manager.add_upstream("sales", sales_server)  # type: ignore[arg-type]
        assert diff.tool_count == 2
        assert registry.tool_count == 2

    @pytest.mark.asyncio
    async def test_remove_upstream(self, sales_server: FastMCP, support_server: FastMCP) -> None:
        registry = ToolRegistry()
        manager = UpstreamManager(
            {"sales": sales_server, "support": support_server},  # type: ignore[dict-item]
            registry,
        )
        await manager.populate_all()
        assert registry.tool_count == 3

        removed = await manager.remove_upstream("support")
        assert "support_tickets_list" in removed
        assert registry.tool_count == 2
        assert "support" not in manager.domains

    @pytest.mark.asyncio
    async def test_remove_unknown_domain_raises(self, sales_server: FastMCP) -> None:
        registry = ToolRegistry()
        manager = UpstreamManager(
            {"sales": sales_server},  # type: ignore[dict-item]
            registry,
        )
        await manager.populate_all()

        with pytest.raises(KeyError, match="not a registered upstream"):
            await manager.remove_upstream("nonexistent")

    @pytest.mark.asyncio
    async def test_list_upstreams(self, sales_server: FastMCP, support_server: FastMCP) -> None:
        registry = ToolRegistry()
        manager = UpstreamManager(
            {"sales": sales_server, "support": support_server},  # type: ignore[dict-item]
            registry,
        )
        upstreams = manager.list_upstreams()
        assert set(upstreams.keys()) == {"sales", "support"}


# ---------------------------------------------------------------------------
# REST endpoint tests (POST / DELETE / GET /registry/servers)
# ---------------------------------------------------------------------------


class TestRegistrationEndpoints:
    @pytest.mark.asyncio
    async def test_register_new_upstream_via_api(
        self,
        sales_server: FastMCP,
        support_server: FastMCP,
    ) -> None:
        """Test registration via the GatewayServer API (not REST endpoint).

        REST endpoints receive URL strings which need a real network server.
        For in-process FastMCP testing, we test the lock-protected API directly.
        """
        gateway = GatewayServer(
            {"sales": sales_server},  # type: ignore[dict-item]
            registration_token=REGISTRATION_TOKEN,
        )
        await gateway.populate()
        assert gateway.registry.tool_count == 2

        # Register a new upstream via the gateway's lock-protected path.
        async with gateway._registry_lock:
            diff = await gateway.upstream_manager.add_upstream("support", support_server)  # type: ignore[arg-type]
            gateway.registry.set_domain_description("support", "Support ticketing")
            gateway._apply_domain_descriptions()
            gateway._update_instructions()

        assert diff.tool_count == 1
        assert gateway.registry.tool_count == 3
        assert gateway.registry.has_domain("support")

    @pytest.mark.asyncio
    async def test_deregister_upstream(
        self,
        gateway_with_registration: GatewayServer,
    ) -> None:
        gateway = gateway_with_registration
        assert gateway.registry.tool_count == 2

        async with await _http_client(gateway) as client:
            resp = await client.delete(
                "/registry/servers/sales",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["deregistered"] == "sales"
        assert len(data["tools_removed"]) == 2
        assert gateway.registry.tool_count == 0

    @pytest.mark.asyncio
    async def test_deregister_unknown_returns_404(
        self,
        gateway_with_registration: GatewayServer,
    ) -> None:
        async with await _http_client(gateway_with_registration) as client:
            resp = await client.delete(
                "/registry/servers/nonexistent",
                headers=_auth_headers(),
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_servers(
        self,
        gateway_with_registration: GatewayServer,
    ) -> None:
        async with await _http_client(gateway_with_registration) as client:
            resp = await client.get(
                "/registry/servers",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["servers"][0]["domain"] == "sales"
        assert data["servers"][0]["tool_count"] == 2


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


class TestRegistrationAuth:
    @pytest.mark.asyncio
    async def test_no_token_returns_401(
        self,
        gateway_with_registration: GatewayServer,
    ) -> None:
        async with await _http_client(gateway_with_registration) as client:
            resp = await client.get("/registry/servers")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_token_returns_401(
        self,
        gateway_with_registration: GatewayServer,
    ) -> None:
        async with await _http_client(gateway_with_registration) as client:
            resp = await client.get(
                "/registry/servers",
                headers={"authorization": "Bearer wrong-token"},
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_correct_token_returns_200(
        self,
        gateway_with_registration: GatewayServer,
    ) -> None:
        async with await _http_client(gateway_with_registration) as client:
            resp = await client.get(
                "/registry/servers",
                headers=_auth_headers(),
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Backwards compatibility: no registration token → no endpoints
# ---------------------------------------------------------------------------


class TestNoRegistrationToken:
    @pytest.mark.asyncio
    async def test_endpoints_not_mounted(
        self,
        gateway_no_registration: GatewayServer,
    ) -> None:
        """When registration_token is not set, /registry/servers returns 404."""
        async with await _http_client(gateway_no_registration) as client:
            resp = await client.get(
                "/registry/servers",
                headers=_auth_headers(),
            )
        # FastMCP returns 404 for unregistered routes.
        assert resp.status_code in (404, 405)


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestRegistrationValidation:
    @pytest.mark.asyncio
    async def test_missing_domain_returns_400(
        self,
        gateway_with_registration: GatewayServer,
    ) -> None:
        async with await _http_client(gateway_with_registration) as client:
            resp = await client.post(
                "/registry/servers",
                json={"url": "http://example.com/mcp"},
                headers=_auth_headers(),
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_missing_url_returns_400(
        self,
        gateway_with_registration: GatewayServer,
    ) -> None:
        async with await _http_client(gateway_with_registration) as client:
            resp = await client.post(
                "/registry/servers",
                json={"domain": "test"},
                headers=_auth_headers(),
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_url_scheme_returns_400(
        self,
        gateway_with_registration: GatewayServer,
    ) -> None:
        async with await _http_client(gateway_with_registration) as client:
            resp = await client.post(
                "/registry/servers",
                json={"domain": "evil", "url": "file:///etc/passwd"},
                headers=_auth_headers(),
            )
        assert resp.status_code == 400
        body = resp.json()
        # Scheme allowlist now lives in the url_guard — surfaced as the
        # SSRF rejection code rather than the generic bad_request.
        assert body["code"] == "ssrf_rejected"
        assert isinstance(body.get("error"), str)

    @pytest.mark.asyncio
    async def test_invalid_headers_returns_400(
        self,
        gateway_with_registration: GatewayServer,
    ) -> None:
        async with await _http_client(gateway_with_registration) as client:
            resp = await client.post(
                "/registry/servers",
                json={"domain": "test", "url": "http://example.com/mcp", "headers": [1, 2]},
                headers=_auth_headers(),
            )
        assert resp.status_code == 400
        assert resp.json()["code"] == "bad_request"

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(
        self,
        gateway_with_registration: GatewayServer,
    ) -> None:
        async with await _http_client(gateway_with_registration) as client:
            resp = await client.post(
                "/registry/servers",
                content=b"not json",
                headers={**_auth_headers(), "content-type": "application/json"},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_ssrf_rfc1918_rejected(
        self,
        gateway_with_registration: GatewayServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POST with an RFC 1918 URL returns 400 + ``code='ssrf_rejected'``."""

        # Force the hostname to resolve to an RFC 1918 address so the
        # guard fires regardless of the CI machine's actual DNS.
        def _stub(host, port, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            import socket as _socket

            return [
                (
                    _socket.AF_INET,
                    _socket.SOCK_STREAM,
                    _socket.IPPROTO_TCP,
                    "",
                    ("10.0.0.42", port or 443),
                )
            ]

        monkeypatch.setattr(
            "fastmcp_gateway.url_guard.socket.getaddrinfo",
            _stub,
        )
        async with await _http_client(gateway_with_registration) as client:
            resp = await client.post(
                "/registry/servers",
                json={"domain": "evil", "url": "https://internal.example/mcp"},
                headers=_auth_headers(),
            )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "ssrf_rejected"

    @pytest.mark.asyncio
    async def test_header_injection_rejected(
        self,
        gateway_with_registration: GatewayServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """POST with a denylisted header key returns 400 + header_injection_rejected."""

        # Give the URL check a resolvable public-looking address so
        # the failure is unambiguously the header guard.
        def _stub(host, port, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            import socket as _socket

            return [
                (
                    _socket.AF_INET,
                    _socket.SOCK_STREAM,
                    _socket.IPPROTO_TCP,
                    "",
                    ("203.0.113.5", port or 443),
                )
            ]

        monkeypatch.setattr(
            "fastmcp_gateway.url_guard.socket.getaddrinfo",
            _stub,
        )
        async with await _http_client(gateway_with_registration) as client:
            resp = await client.post(
                "/registry/servers",
                json={
                    "domain": "evil",
                    "url": "https://public.example/mcp",
                    "headers": {"Host": "evil.internal"},
                },
                headers=_auth_headers(),
            )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "header_injection_rejected"


# ---------------------------------------------------------------------------
# Concurrent registration (lock safety)
# ---------------------------------------------------------------------------


class TestConcurrentRegistration:
    @pytest.mark.asyncio
    async def test_concurrent_add_does_not_corrupt_registry(
        self,
        sales_server: FastMCP,
        support_server: FastMCP,
    ) -> None:
        """Multiple simultaneous add_upstream calls don't corrupt the registry."""
        gateway = GatewayServer(
            {},
            registration_token=REGISTRATION_TOKEN,
        )

        async with gateway._registry_lock:
            # Lock is held — verify it's a real lock
            assert gateway._registry_lock.locked()

        # Run two registrations concurrently.
        async def register_sales() -> None:
            async with gateway._registry_lock:
                await gateway.upstream_manager.add_upstream("sales", sales_server)  # type: ignore[arg-type]

        async def register_support() -> None:
            async with gateway._registry_lock:
                await gateway.upstream_manager.add_upstream("support", support_server)  # type: ignore[arg-type]

        await asyncio.gather(register_sales(), register_support())

        # Both domains should be registered without corruption.
        assert gateway.registry.has_domain("sales")
        assert gateway.registry.has_domain("support")
        assert gateway.registry.tool_count == 3  # 2 sales + 1 support


# ---------------------------------------------------------------------------
# Registration passes auth headers for upstream discovery
# ---------------------------------------------------------------------------


class TestRegistrationAuthHeaders:
    @pytest.mark.asyncio
    async def test_register_passes_headers_for_discovery(
        self,
        sales_server: FastMCP,
        support_server: FastMCP,
    ) -> None:
        """Registration request headers are passed as registry_auth_headers.

        When the registry-controller sends ``headers`` in the registration
        payload, those headers must be used for the initial ``list_tools``
        discovery call (``registry_auth_headers``), not just stored for
        later tool execution.  Without this, authenticated upstreams
        reject the discovery connection with 401.
        """
        gateway = GatewayServer(
            {"sales": sales_server},  # type: ignore[dict-item]
            registration_token=REGISTRATION_TOKEN,
        )
        await gateway.populate()

        upstream_auth = {"authorization": "Bearer upstream-token"}

        # Register with headers — simulates what the REST endpoint does
        async with gateway._registry_lock:
            diff = await gateway.upstream_manager.add_upstream(
                "support",
                support_server,  # type: ignore[arg-type]
                headers=upstream_auth,
                registry_auth_headers=upstream_auth,
            )

        assert diff.tool_count == 1
        assert gateway.registry.tool_count == 3
        # Verify headers are stored for execution
        assert gateway.upstream_manager._upstream_headers.get("support") == upstream_auth


# ---------------------------------------------------------------------------
# Mutual exclusion: token + validator cannot both be passed
# ---------------------------------------------------------------------------


class _StubValidator:
    """Minimal validator stub — good enough to satisfy the protocol check."""

    def validate(self, bearer: str) -> RegistrationClaims:  # pragma: no cover - not invoked
        from datetime import datetime

        return RegistrationClaims(
            subject="stub",
            jti=None,
            issued_at=datetime.now(tz=UTC),
            raw={},
        )


class TestRegistrationAuthMutualExclusion:
    def test_mutual_exclusion(self, sales_server: FastMCP) -> None:
        """Passing both ``registration_token`` and ``registration_validator`` raises.

        The two paths are mutually exclusive to prevent a
        misconfiguration where an operator thought they had migrated
        to the JWT validator but the legacy static bearer was still
        honoured.  Failing loudly at construction means the mistake
        surfaces at startup rather than silently at runtime.
        """
        with pytest.raises(ValueError, match="mutually exclusive"):
            GatewayServer(
                {"sales": sales_server},  # type: ignore[dict-item]
                registration_token="some-static-token-long-enough",
                registration_validator=_StubValidator(),
            )

    def test_static_token_emits_deprecation_warning(self, sales_server: FastMCP) -> None:
        """Constructing with ``registration_token`` emits a ``DeprecationWarning``.

        The static-bearer path is retained for backward compatibility
        for one release; the warning gives deployments a clear
        migration signal.
        """
        # Match only the kwarg name rather than the full deprecation
        # phrase — we want to confirm the warning is about
        # ``registration_token`` (and not some unrelated
        # ``DeprecationWarning`` from a transitive dep) without pinning
        # the exact message wording the migration guidance uses.
        with pytest.warns(DeprecationWarning, match="registration_token"):
            GatewayServer(
                {"sales": sales_server},  # type: ignore[dict-item]
                registration_token="some-static-token-long-enough",
            )

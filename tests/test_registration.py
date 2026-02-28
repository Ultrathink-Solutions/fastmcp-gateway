"""Tests for dynamic upstream registration API (POST/DELETE/GET /registry/servers)."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastmcp import FastMCP
from httpx import ASGITransport, AsyncClient

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.gateway import GatewayServer
from fastmcp_gateway.registry import ToolRegistry

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
        assert body["code"] == "bad_request"
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

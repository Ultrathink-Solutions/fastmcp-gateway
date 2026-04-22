"""Tests for health check endpoints (/healthz and /readyz)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from fastmcp_gateway.gateway import GatewayServer

if TYPE_CHECKING:
    from fastmcp import FastMCP


@pytest.fixture
def empty_gateway() -> GatewayServer:
    """A gateway with no upstreams (registry is empty)."""
    with patch("fastmcp_gateway.client_manager.Client"):
        return GatewayServer({})


@pytest.fixture
def populated_gateway() -> GatewayServer:
    """A gateway whose registry has been manually populated."""
    with patch("fastmcp_gateway.client_manager.Client"):
        gw = GatewayServer({"svc": "http://svc:8080/mcp"})
    gw.registry.populate_domain(
        "svc",
        "http://svc:8080/mcp",
        [{"name": "svc_ping", "inputSchema": {}}],
    )
    return gw


def _http_app(mcp: FastMCP) -> httpx.ASGITransport:
    """Create an ASGI transport from a FastMCP server for testing."""
    app = mcp.http_app(transport="streamable-http")
    return httpx.ASGITransport(app=app)


# ---------------------------------------------------------------------------
# /healthz (liveness)
# ---------------------------------------------------------------------------


class TestHealthz:
    @pytest.mark.asyncio
    async def test_always_returns_200(self, empty_gateway: GatewayServer) -> None:
        transport = _http_app(empty_gateway.mcp)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_returns_200_when_populated(self, populated_gateway: GatewayServer) -> None:
        transport = _http_app(populated_gateway.mcp)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz")

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# /readyz (readiness)
# ---------------------------------------------------------------------------


class TestReadyz:
    @pytest.mark.asyncio
    async def test_returns_200_when_empty(self, empty_gateway: GatewayServer) -> None:
        transport = _http_app(empty_gateway.mcp)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/readyz")

        assert response.status_code == 200
        # Body is exactly ``{"status": "ready"}`` — the tool count is
        # not exposed in the response. Exposing it would disclose the
        # size of the routed-tool attack surface to any unauthenticated
        # caller. Kubernetes readiness probes only consume the status
        # code; tool-count visibility stays with operators via the
        # OTel ``registry.tool_count`` span attribute.
        assert response.json() == {"status": "ready"}

    @pytest.mark.asyncio
    async def test_returns_200_when_populated(self, populated_gateway: GatewayServer) -> None:
        transport = _http_app(populated_gateway.mcp)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/readyz")

        assert response.status_code == 200
        # Populated gateway body is identical to empty gateway body
        # — no subsystem-state disclosure regardless of tool count.
        body = response.json()
        assert body == {"status": "ready"}
        assert "tools" not in body

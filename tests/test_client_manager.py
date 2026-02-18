"""Tests for upstream client management."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.registry import ToolRegistry


@dataclass
class FakeTool:
    """Mimics mcp.types.Tool for testing without MCP dependency."""

    name: str
    description: str | None = None
    inputSchema: dict[str, Any] | None = None


def _make_fake_tools(domain: str) -> list[FakeTool]:
    """Create a realistic set of fake MCP tools for a domain."""
    return [
        FakeTool(
            name=f"{domain}_users_list",
            description="List users",
            inputSchema={"type": "object", "properties": {"limit": {"type": "integer"}}},
        ),
        FakeTool(
            name=f"{domain}_users_create",
            description="Create a user",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
        FakeTool(
            name=f"{domain}_billing_invoice",
            description="Generate invoice",
            inputSchema={"type": "object"},
        ),
    ]


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


@pytest.fixture
def upstreams() -> dict[str, str]:
    return {
        "acme": "http://acme-mcp:8080/mcp",
        "widgets": "http://widgets-mcp:8080/mcp",
    }


# ---------------------------------------------------------------------------
# populate_all
# ---------------------------------------------------------------------------


class TestPopulateAll:
    @pytest.mark.asyncio
    async def test_populates_all_upstreams(self, registry: ToolRegistry, upstreams: dict[str, str]) -> None:
        manager = UpstreamManager(upstreams, registry)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        call_count = 0

        def make_client(url: str) -> MagicMock:
            nonlocal call_count
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            # Determine domain from call order
            domain = list(upstreams.keys())[call_count]
            client.list_tools = AsyncMock(return_value=_make_fake_tools(domain))
            call_count += 1
            return client

        with patch("fastmcp_gateway.client_manager.Client", side_effect=make_client):
            manager = UpstreamManager(upstreams, registry)
            results = await manager.populate_all()

        assert results == {"acme": 3, "widgets": 3}
        assert registry.tool_count == 6
        assert registry.has_domain("acme")
        assert registry.has_domain("widgets")

    @pytest.mark.asyncio
    async def test_graceful_degradation_on_failure(self, registry: ToolRegistry, upstreams: dict[str, str]) -> None:
        """One failing upstream should not prevent others from populating."""
        call_count = 0

        def make_client(url: str) -> MagicMock:
            nonlocal call_count
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            domain = list(upstreams.keys())[call_count]
            if call_count == 0:
                # First upstream fails
                client.list_tools = AsyncMock(side_effect=ConnectionError("unreachable"))
            else:
                client.list_tools = AsyncMock(return_value=_make_fake_tools(domain))
            call_count += 1
            return client

        with patch("fastmcp_gateway.client_manager.Client", side_effect=make_client):
            manager = UpstreamManager(upstreams, registry)
            results = await manager.populate_all()

        # Only the second upstream succeeded
        assert len(results) == 1
        assert registry.tool_count == 3


# ---------------------------------------------------------------------------
# populate_domain (single)
# ---------------------------------------------------------------------------


class TestPopulateDomain:
    @pytest.mark.asyncio
    async def test_populates_single_domain(self, registry: ToolRegistry) -> None:
        def make_client(url: str) -> MagicMock:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            client.list_tools = AsyncMock(return_value=_make_fake_tools("svc"))
            return client

        with patch("fastmcp_gateway.client_manager.Client", side_effect=make_client):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, registry)
            count = await manager.populate_domain("svc")

        assert count == 3
        assert registry.has_domain("svc")

    @pytest.mark.asyncio
    async def test_unknown_domain_raises(self, registry: ToolRegistry) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, registry)
            with pytest.raises(KeyError):
                await manager.populate_domain("nonexistent")


# ---------------------------------------------------------------------------
# execute_tool
# ---------------------------------------------------------------------------


class TestExecuteTool:
    @pytest.mark.asyncio
    async def test_execute_routes_to_upstream(self, registry: ToolRegistry) -> None:
        """execute_tool creates a fresh client and calls the correct tool."""
        fake_result = MagicMock()
        fake_result.content = []
        fake_result.is_error = False

        fresh_client = AsyncMock()
        fresh_client.__aenter__ = AsyncMock(return_value=fresh_client)
        fresh_client.__aexit__ = AsyncMock(return_value=None)
        fresh_client.call_tool = AsyncMock(return_value=fake_result)

        base_client = MagicMock()
        base_client.new = MagicMock(return_value=fresh_client)

        def make_client(url: str) -> MagicMock:
            return base_client

        with patch("fastmcp_gateway.client_manager.Client", side_effect=make_client):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, registry)

        # Manually populate registry (bypassing client mocking for populate)
        registry.populate_domain(
            "svc",
            "http://svc:8080/mcp",
            [{"name": "svc_users_list", "inputSchema": {}}],
        )

        result = await manager.execute_tool("svc_users_list", {"limit": 10})

        assert result is fake_result
        base_client.new.assert_called_once()
        fresh_client.call_tool.assert_called_once_with(
            "svc_users_list",
            {"limit": 10},
            raise_on_error=False,
        )

    @pytest.mark.asyncio
    async def test_execute_unknown_tool_raises(self, registry: ToolRegistry) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, registry)
            with pytest.raises(KeyError, match="not found"):
                await manager.execute_tool("nonexistent_tool")

    @pytest.mark.asyncio
    async def test_execute_defaults_empty_arguments(self, registry: ToolRegistry) -> None:
        """When arguments is None, an empty dict is sent to call_tool."""
        fake_result = MagicMock()
        fresh_client = AsyncMock()
        fresh_client.__aenter__ = AsyncMock(return_value=fresh_client)
        fresh_client.__aexit__ = AsyncMock(return_value=None)
        fresh_client.call_tool = AsyncMock(return_value=fake_result)

        base_client = MagicMock()
        base_client.new = MagicMock(return_value=fresh_client)

        def make_client(url: str) -> MagicMock:
            return base_client

        with patch("fastmcp_gateway.client_manager.Client", side_effect=make_client):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, registry)

        registry.populate_domain("svc", "http://svc:8080/mcp", [{"name": "svc_ping", "inputSchema": {}}])

        await manager.execute_tool("svc_ping")

        fresh_client.call_tool.assert_called_once_with(
            "svc_ping",
            {},
            raise_on_error=False,
        )


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


class TestIntrospection:
    def test_domains(self) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            manager = UpstreamManager(
                {"beta": "http://b:8080/mcp", "alpha": "http://a:8080/mcp"},
                ToolRegistry(),
            )
        assert manager.domains == ["alpha", "beta"]

    def test_upstream_url(self) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, ToolRegistry())
        assert manager.upstream_url("svc") == "http://svc:8080/mcp"

    def test_upstream_url_unknown_raises(self) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, ToolRegistry())
        with pytest.raises(KeyError):
            manager.upstream_url("nonexistent")

"""Tests for registry refresh functionality (ULT-688)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client, FastMCP
from pydantic import ValidationError

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.gateway import GatewayServer
from fastmcp_gateway.meta_tools import register_meta_tools
from fastmcp_gateway.registry import RegistryDiff, ToolRegistry

# ---------------------------------------------------------------------------
# RegistryDiff model
# ---------------------------------------------------------------------------


class TestRegistryDiff:
    """Tests for the RegistryDiff Pydantic model."""

    def test_model_fields(self) -> None:
        """All fields are accessible and store correct values."""
        diff = RegistryDiff(domain="apollo", added=["tool_a"], removed=["tool_b"], tool_count=5)
        assert diff.domain == "apollo"
        assert diff.added == ["tool_a"]
        assert diff.removed == ["tool_b"]
        assert diff.tool_count == 5

    def test_empty_diff(self) -> None:
        """Empty added/removed lists indicate no changes."""
        diff = RegistryDiff(domain="apollo", added=[], removed=[], tool_count=3)
        assert not diff.added
        assert not diff.removed

    def test_frozen(self) -> None:
        """RegistryDiff is immutable (frozen Pydantic model)."""
        diff = RegistryDiff(domain="x", added=[], removed=[], tool_count=0)
        with pytest.raises(ValidationError):
            diff.domain = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# populate_domain diff tracking
# ---------------------------------------------------------------------------


class TestPopulateDomainDiff:
    """Tests for diff tracking in ToolRegistry.populate_domain."""

    def test_initial_populate_reports_all_added(self) -> None:
        """First populate reports all tools as added, none removed."""
        registry = ToolRegistry()
        diff = registry.populate_domain(
            "svc",
            "http://svc:8080/mcp",
            [{"name": "svc_foo", "inputSchema": {}}, {"name": "svc_bar", "inputSchema": {}}],
        )
        assert diff.domain == "svc"
        assert sorted(diff.added) == ["svc_bar", "svc_foo"]
        assert diff.removed == []
        assert diff.tool_count == 2

    def test_repopulate_detects_changes(self) -> None:
        """Re-populating detects added and removed tools."""
        registry = ToolRegistry()
        registry.populate_domain(
            "svc",
            "http://svc:8080/mcp",
            [{"name": "svc_old", "inputSchema": {}}, {"name": "svc_kept", "inputSchema": {}}],
        )
        diff = registry.populate_domain(
            "svc",
            "http://svc:8080/mcp",
            [{"name": "svc_new", "inputSchema": {}}, {"name": "svc_kept", "inputSchema": {}}],
        )
        assert diff.added == ["svc_new"]
        assert diff.removed == ["svc_old"]
        assert diff.tool_count == 2

    def test_no_change_produces_empty_diff(self) -> None:
        """Identical re-populate produces empty added/removed lists."""
        registry = ToolRegistry()
        registry.populate_domain("svc", "http://svc:8080/mcp", [{"name": "svc_tool", "inputSchema": {}}])
        diff = registry.populate_domain("svc", "http://svc:8080/mcp", [{"name": "svc_tool", "inputSchema": {}}])
        assert diff.added == []
        assert diff.removed == []
        assert diff.tool_count == 1

    def test_all_removed(self) -> None:
        """Re-populating with empty list reports all tools as removed."""
        registry = ToolRegistry()
        registry.populate_domain("svc", "http://svc:8080/mcp", [{"name": "svc_gone", "inputSchema": {}}])
        diff = registry.populate_domain("svc", "http://svc:8080/mcp", [])
        assert diff.added == []
        assert diff.removed == ["svc_gone"]
        assert diff.tool_count == 0


# ---------------------------------------------------------------------------
# UpstreamManager refresh methods
# ---------------------------------------------------------------------------


def _make_mock_client(tools: list[MagicMock]) -> MagicMock:
    """Create a mock FastMCP Client that returns *tools* from list_tools."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.list_tools = AsyncMock(return_value=tools)
    client.new = MagicMock(return_value=client)
    return client


def _make_fake_tool(name: str) -> MagicMock:
    """Create a mock MCP tool with the given name."""
    tool = MagicMock()
    tool.name = name
    tool.description = f"Tool {name}"
    tool.inputSchema = {}
    return tool


class TestRefreshMethods:
    """Tests for UpstreamManager.refresh_domain and refresh_all."""

    async def test_refresh_domain_returns_diff(self) -> None:
        """refresh_domain returns a RegistryDiff for the refreshed domain."""
        registry = ToolRegistry()
        fake_tool = _make_fake_tool("svc_ping")

        with patch(
            "fastmcp_gateway.client_manager.Client",
            side_effect=lambda url: _make_mock_client([fake_tool]),
        ):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, registry)
            diff = await manager.refresh_domain("svc")

        assert isinstance(diff, RegistryDiff)
        assert diff.domain == "svc"
        assert diff.tool_count == 1

    async def test_refresh_all_returns_list_of_diffs(self) -> None:
        """refresh_all returns a list of RegistryDiff objects."""
        registry = ToolRegistry()
        fake_tool = _make_fake_tool("svc_ping")

        with patch(
            "fastmcp_gateway.client_manager.Client",
            side_effect=lambda url: _make_mock_client([fake_tool]),
        ):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, registry)
            diffs = await manager.refresh_all()

        assert len(diffs) == 1
        assert diffs[0].domain == "svc"

    async def test_refresh_all_skips_failed_domains(self) -> None:
        """Unreachable upstreams are skipped, not raised."""
        registry = ToolRegistry()
        failing_client = AsyncMock()
        failing_client.__aenter__ = AsyncMock(side_effect=ConnectionError("refused"))
        failing_client.__aexit__ = AsyncMock(return_value=None)
        failing_client.new = MagicMock(return_value=failing_client)

        with patch(
            "fastmcp_gateway.client_manager.Client",
            return_value=failing_client,
        ):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, registry)
            diffs = await manager.refresh_all()

        assert diffs == []

    async def test_refresh_domain_unknown_raises(self) -> None:
        """Refreshing an unknown domain raises KeyError."""
        registry = ToolRegistry()
        with patch("fastmcp_gateway.client_manager.Client"):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, registry)
            with pytest.raises(KeyError):
                await manager.refresh_domain("nonexistent")


# ---------------------------------------------------------------------------
# refresh_registry meta-tool
# ---------------------------------------------------------------------------


class TestRefreshRegistryMetaTool:
    """Tests for the refresh_registry meta-tool."""

    async def test_returns_summary(self) -> None:
        """Meta-tool returns per-domain refresh summary."""
        registry = ToolRegistry()
        registry.populate_domain(
            "apollo",
            "http://apollo:8080/mcp",
            [{"name": "apollo_search", "description": "Search", "inputSchema": {}}],
        )

        with patch("fastmcp_gateway.client_manager.Client"):
            manager = UpstreamManager({"apollo": "http://apollo:8080/mcp"}, registry)

        diff = RegistryDiff(domain="apollo", added=["apollo_new"], removed=[], tool_count=2)
        manager.refresh_all = AsyncMock(return_value=[diff])  # type: ignore[method-assign]

        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, registry, manager)

        async with Client(mcp) as client:
            result = await client.call_tool("refresh_registry", {})

        text = result.content[0].text  # type: ignore[union-attr]
        data = json.loads(text)
        assert "refreshed" in data
        assert len(data["refreshed"]) == 1
        assert data["refreshed"][0]["domain"] == "apollo"
        assert data["refreshed"][0]["added"] == ["apollo_new"]
        assert data["refreshed"][0]["tool_count"] == 2

    async def test_meta_tool_is_listed(self) -> None:
        """refresh_registry appears in the tool listing."""
        registry = ToolRegistry()
        with patch("fastmcp_gateway.client_manager.Client"):
            manager = UpstreamManager({}, registry)

        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, registry, manager)

        async with Client(mcp) as client:
            tools = await client.list_tools()

        tool_names = [t.name for t in tools]
        assert "refresh_registry" in tool_names


# ---------------------------------------------------------------------------
# Background refresh loop
# ---------------------------------------------------------------------------


class TestBackgroundRefresh:
    """Tests for the background refresh loop in GatewayServer."""

    async def test_refresh_loop_calls_refresh_all(self) -> None:
        """Refresh loop calls refresh_all at least twice within the interval."""
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer(
                {"svc": "http://svc:8080/mcp"},
                refresh_interval=0.05,
            )

        gw.upstream_manager.refresh_all = AsyncMock(return_value=[])  # type: ignore[method-assign]

        task = asyncio.create_task(gw._refresh_loop())
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert gw.upstream_manager.refresh_all.call_count >= 2

    async def test_refresh_loop_survives_errors(self) -> None:
        """A failing refresh doesn't kill the loop."""
        call_count = 0

        async def failing_then_ok() -> list[RegistryDiff]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("transient failure")
            return []

        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer(
                {"svc": "http://svc:8080/mcp"},
                refresh_interval=0.05,
            )

        gw.upstream_manager.refresh_all = failing_then_ok  # type: ignore[method-assign]

        task = asyncio.create_task(gw._refresh_loop())
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Should have been called at least twice (one failure + one success).
        assert call_count >= 2

    def test_gateway_no_refresh_interval(self) -> None:
        """Without refresh_interval, no refresh task or lifespan is configured."""
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer({"svc": "http://svc:8080/mcp"})

        assert gw._refresh_interval is None
        assert gw._refresh_task is None

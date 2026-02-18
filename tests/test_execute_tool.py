"""Tests for the execute_tool meta-tool."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client, FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.meta_tools import register_meta_tools

if TYPE_CHECKING:
    from fastmcp_gateway.registry import ToolRegistry


@pytest.fixture
def manager(populated_registry: ToolRegistry) -> UpstreamManager:
    """An UpstreamManager with mocked Client constructor."""
    with patch("fastmcp_gateway.client_manager.Client"):
        return UpstreamManager(
            {"apollo": "http://apollo:8080/mcp", "hubspot": "http://hubspot:8080/mcp"},
            populated_registry,
        )


@pytest.fixture
def mcp_server(populated_registry: ToolRegistry, manager: UpstreamManager) -> FastMCP:
    """A FastMCP server with all 3 meta-tools registered."""
    mcp = FastMCP("test-gateway")
    register_meta_tools(mcp, populated_registry, manager)
    return mcp


def _fake_result(text: str, *, is_error: bool = False) -> MagicMock:
    """Create a fake CallToolResult with text content."""
    block = MagicMock()
    block.text = text
    result = MagicMock()
    result.content = [block]
    result.is_error = is_error
    return result


async def _call_execute(mcp: FastMCP, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Helper: call execute_tool via in-process client and parse JSON."""
    params: dict[str, Any] = {"tool_name": tool_name}
    if arguments is not None:
        params["arguments"] = arguments
    async with Client(mcp) as client:
        result = await client.call_tool("execute_tool", params)
    if result.data is not None:
        text = str(result.data)
    else:
        content_block = result.content[0]
        text = content_block.text  # type: ignore[union-attr]
    return json.loads(text)


# ---------------------------------------------------------------------------
# Successful execution
# ---------------------------------------------------------------------------


class TestExecuteToolSuccess:
    @pytest.mark.asyncio
    async def test_routes_and_returns_result(self, mcp_server: FastMCP, manager: UpstreamManager) -> None:
        manager.execute_tool = AsyncMock(return_value=_fake_result('{"people": []}'))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {"name": "Jane"})

        assert data["tool"] == "apollo_people_search"
        assert data["result"] == '{"people": []}'
        manager.execute_tool.assert_called_once_with("apollo_people_search", {"name": "Jane"})

    @pytest.mark.asyncio
    async def test_no_arguments_sends_none(self, mcp_server: FastMCP, manager: UpstreamManager) -> None:
        manager.execute_tool = AsyncMock(return_value=_fake_result("ok"))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search")

        assert data["result"] == "ok"
        manager.execute_tool.assert_called_once_with("apollo_people_search", None)


# ---------------------------------------------------------------------------
# Error: unknown tool
# ---------------------------------------------------------------------------


class TestExecuteToolUnknown:
    @pytest.mark.asyncio
    async def test_unknown_tool_with_suggestions(self, mcp_server: FastMCP) -> None:
        data = await _call_execute(mcp_server, "apollo_search")

        assert "error" in data
        assert "apollo_search" in data["error"]
        assert "Did you mean" in data["error"]

    @pytest.mark.asyncio
    async def test_unknown_tool_no_suggestions(self, mcp_server: FastMCP) -> None:
        data = await _call_execute(mcp_server, "completely_unrelated_xyz_123")

        assert "error" in data
        assert "discover_tools" in data["error"]


# ---------------------------------------------------------------------------
# Error: upstream unreachable
# ---------------------------------------------------------------------------


class TestExecuteToolUpstreamError:
    @pytest.mark.asyncio
    async def test_connectivity_error(self, mcp_server: FastMCP, manager: UpstreamManager) -> None:
        manager.execute_tool = AsyncMock(side_effect=ConnectionError("connection refused"))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {"name": "Jane"})

        assert "error" in data
        assert "failed" in data["error"]
        assert "apollo" in data["error"]

    @pytest.mark.asyncio
    async def test_upstream_tool_error(self, mcp_server: FastMCP, manager: UpstreamManager) -> None:
        """Upstream tool returns is_error=True."""
        manager.execute_tool = AsyncMock(  # type: ignore[method-assign]
            return_value=_fake_result("Invalid parameter: limit must be > 0", is_error=True)
        )

        data = await _call_execute(mcp_server, "apollo_people_search", {"limit": -1})

        assert data["tool"] == "apollo_people_search"
        assert "error" in data
        assert "Invalid parameter" in data["error"]
        assert "result" not in data

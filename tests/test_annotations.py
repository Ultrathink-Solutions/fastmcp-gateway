"""Tests for MCP tool annotations on gateway meta-tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.meta_tools import register_meta_tools

if TYPE_CHECKING:
    from fastmcp_gateway.registry import ToolRegistry


@pytest.fixture
def mcp_server(populated_registry: ToolRegistry) -> FastMCP:
    """Create a FastMCP server with meta-tools registered."""
    mcp = FastMCP("test-gateway")
    with patch("fastmcp_gateway.client_manager.Client"):
        manager = UpstreamManager(
            {"apollo": "http://apollo:8080/mcp", "hubspot": "http://hubspot:8080/mcp"},
            populated_registry,
        )
    register_meta_tools(mcp, populated_registry, manager)
    return mcp


async def _get_annotations(mcp: FastMCP, tool_name: str) -> dict[str, Any]:
    """Fetch tool annotations via list_tools."""
    async with Client(mcp) as client:
        tools = await client.list_tools()
    tool = next(t for t in tools if t.name == tool_name)
    assert tool.annotations is not None, f"{tool_name} has no annotations"
    return tool.annotations.model_dump(exclude_none=True)


class TestAnnotations:
    @pytest.mark.asyncio
    async def test_discover_tools_is_read_only(self, mcp_server: FastMCP) -> None:
        """discover_tools is read-only and open-world."""
        ann = await _get_annotations(mcp_server, "discover_tools")

        assert ann["readOnlyHint"] is True
        assert ann["openWorldHint"] is True

    @pytest.mark.asyncio
    async def test_get_tool_schema_is_read_only(self, mcp_server: FastMCP) -> None:
        """get_tool_schema is read-only and closed-world."""
        ann = await _get_annotations(mcp_server, "get_tool_schema")

        assert ann["readOnlyHint"] is True
        assert ann["openWorldHint"] is False

    @pytest.mark.asyncio
    async def test_execute_tool_is_not_read_only(self, mcp_server: FastMCP) -> None:
        """execute_tool is not read-only (mutates upstream) and is open-world."""
        ann = await _get_annotations(mcp_server, "execute_tool")

        assert ann["readOnlyHint"] is False
        assert ann["openWorldHint"] is True

    @pytest.mark.asyncio
    async def test_all_meta_tools_have_annotations(self, mcp_server: FastMCP) -> None:
        """Every meta-tool exposes non-null annotations."""
        async with Client(mcp_server) as client:
            tools = await client.list_tools()

        meta_tool_names = {"discover_tools", "get_tool_schema", "execute_tool"}
        for tool in tools:
            if tool.name in meta_tool_names:
                assert tool.annotations is not None, f"{tool.name} missing annotations"

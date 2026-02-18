"""Tests for the get_tool_schema meta-tool."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.meta_tools import _suggest_tool_names, register_meta_tools

if TYPE_CHECKING:
    from fastmcp_gateway.registry import ToolRegistry


@pytest.fixture
def mcp_server(populated_registry: ToolRegistry) -> FastMCP:
    """A FastMCP server with get_tool_schema registered."""
    mcp = FastMCP("test-gateway")
    with patch("fastmcp_gateway.client_manager.Client"):
        manager = UpstreamManager(
            {"apollo": "http://apollo:8080/mcp", "hubspot": "http://hubspot:8080/mcp"},
            populated_registry,
        )
    register_meta_tools(mcp, populated_registry, manager)
    return mcp


async def _call_schema(mcp: FastMCP, tool_name: str) -> dict[str, Any]:
    """Helper: call get_tool_schema via in-process client and parse JSON."""
    async with Client(mcp) as client:
        result = await client.call_tool("get_tool_schema", {"tool_name": tool_name})
    if result.data is not None:
        text = str(result.data)
    else:
        content_block = result.content[0]
        text = content_block.text  # type: ignore[union-attr]
    return json.loads(text)


# ---------------------------------------------------------------------------
# _suggest_tool_names
# ---------------------------------------------------------------------------


class TestSuggestToolNames:
    NAMES: ClassVar[list[str]] = [
        "apollo_people_search",
        "apollo_people_enrich",
        "apollo_org_search",
        "apollo_org_enrich",
        "hubspot_contacts_search",
        "hubspot_contacts_create",
        "hubspot_deals_list",
    ]

    def test_substring_match(self) -> None:
        suggestions = _suggest_tool_names("apollo_search", self.NAMES)
        assert "apollo_people_search" in suggestions
        assert "apollo_org_search" in suggestions

    def test_partial_name(self) -> None:
        suggestions = _suggest_tool_names("apollo_people", self.NAMES)
        assert "apollo_people_search" in suggestions
        assert "apollo_people_enrich" in suggestions

    def test_no_match(self) -> None:
        suggestions = _suggest_tool_names("salesforce_crm", self.NAMES)
        assert suggestions == []

    def test_max_suggestions(self) -> None:
        suggestions = _suggest_tool_names("apollo", self.NAMES, max_suggestions=2)
        assert len(suggestions) <= 2

    def test_prefix_scoring(self) -> None:
        """Tools sharing more prefix segments score higher."""
        suggestions = _suggest_tool_names("apollo_people_find", self.NAMES)
        # apollo_people_* should rank above apollo_org_*
        assert suggestions[0].startswith("apollo_people")


# ---------------------------------------------------------------------------
# get_tool_schema — success
# ---------------------------------------------------------------------------


class TestGetToolSchemaSuccess:
    @pytest.mark.asyncio
    async def test_returns_schema(self, mcp_server: FastMCP) -> None:
        data = await _call_schema(mcp_server, "apollo_people_search")

        assert data["name"] == "apollo_people_search"
        assert data["domain"] == "apollo"
        assert data["group"] == "people"
        assert "parameters" in data

    @pytest.mark.asyncio
    async def test_includes_description(self, mcp_server: FastMCP) -> None:
        data = await _call_schema(mcp_server, "apollo_people_search")

        assert "Search for people" in data["description"]

    @pytest.mark.asyncio
    async def test_parameters_is_json_schema(self, mcp_server: FastMCP) -> None:
        data = await _call_schema(mcp_server, "apollo_people_search")

        params = data["parameters"]
        assert params["type"] == "object"
        assert "properties" in params


# ---------------------------------------------------------------------------
# get_tool_schema — errors with suggestions
# ---------------------------------------------------------------------------


class TestGetToolSchemaErrors:
    @pytest.mark.asyncio
    async def test_unknown_tool(self, mcp_server: FastMCP) -> None:
        data = await _call_schema(mcp_server, "nonexistent_xyz")

        assert "error" in data
        assert "nonexistent_xyz" in data["error"]

    @pytest.mark.asyncio
    async def test_suggests_similar_tools(self, mcp_server: FastMCP) -> None:
        data = await _call_schema(mcp_server, "apollo_search")

        assert "error" in data
        assert "Did you mean" in data["error"]
        # Should suggest apollo tools
        assert "apollo_" in data["error"]

    @pytest.mark.asyncio
    async def test_no_suggestions_for_unrelated(self, mcp_server: FastMCP) -> None:
        data = await _call_schema(mcp_server, "completely_unrelated_xyz_123")

        assert "error" in data
        assert "discover_tools" in data["error"]

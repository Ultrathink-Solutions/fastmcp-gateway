"""Tests for the discover_tools meta-tool."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.meta_tools import register_meta_tools

if TYPE_CHECKING:
    from fastmcp_gateway.registry import ToolRegistry


@pytest.fixture
def registry(populated_registry: ToolRegistry) -> ToolRegistry:
    """Use the populated registry from conftest."""
    return populated_registry


@pytest.fixture
def mcp_server(registry: ToolRegistry) -> FastMCP:
    """A FastMCP server with discover_tools registered."""
    mcp = FastMCP("test-gateway")
    with patch("fastmcp_gateway.client_manager.Client"):
        manager = UpstreamManager(
            {"apollo": "http://apollo:8080/mcp", "hubspot": "http://hubspot:8080/mcp"},
            registry,
        )
    register_meta_tools(mcp, registry, manager)
    return mcp


async def _call_discover(mcp: FastMCP, **kwargs: str | None) -> dict:
    """Helper: call discover_tools via in-process client and parse JSON."""
    async with Client(mcp) as client:
        result = await client.call_tool("discover_tools", {k: v for k, v in kwargs.items() if v is not None})
    # result.data is the parsed return value for in-process calls
    if result.data is not None:
        text = str(result.data)
    else:
        content_block = result.content[0]
        text = content_block.text  # type: ignore[union-attr]
    return json.loads(text)


# ---------------------------------------------------------------------------
# Mode 1: no arguments -> domain summary
# ---------------------------------------------------------------------------


class TestDiscoverNoArgs:
    @pytest.mark.asyncio
    async def test_returns_all_domains(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server)

        assert "domains" in data
        assert data["total_tools"] == 7
        domain_names = {d["name"] for d in data["domains"]}
        assert domain_names == {"apollo", "hubspot"}

    @pytest.mark.asyncio
    async def test_domain_has_groups(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server)

        apollo = next(d for d in data["domains"] if d["name"] == "apollo")
        assert set(apollo["groups"]) == {"organizations", "people"}
        assert apollo["tool_count"] == 4

    @pytest.mark.asyncio
    async def test_domain_has_description(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server)

        apollo = next(d for d in data["domains"] if d["name"] == "apollo")
        assert apollo["description"] == "Apollo.io CRM and sales intelligence"


# ---------------------------------------------------------------------------
# Mode 2: domain only -> tools in domain
# ---------------------------------------------------------------------------


class TestDiscoverByDomain:
    @pytest.mark.asyncio
    async def test_lists_domain_tools(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, domain="apollo")

        assert data["domain"] == "apollo"
        assert len(data["tools"]) == 4
        names = {t["name"] for t in data["tools"]}
        assert "apollo_people_search" in names

    @pytest.mark.asyncio
    async def test_tools_include_group(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, domain="apollo")

        tool = next(t for t in data["tools"] if t["name"] == "apollo_people_search")
        assert tool["group"] == "people"

    @pytest.mark.asyncio
    async def test_unknown_domain_error(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, domain="salesforce")

        assert "error" in data
        assert "salesforce" in data["error"]
        assert "apollo" in data["error"]
        assert "hubspot" in data["error"]


# ---------------------------------------------------------------------------
# Mode 3: domain + group -> tools in group
# ---------------------------------------------------------------------------


class TestDiscoverByGroup:
    @pytest.mark.asyncio
    async def test_lists_group_tools(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, domain="apollo", group="people")

        assert data["domain"] == "apollo"
        assert data["group"] == "people"
        assert len(data["tools"]) == 2
        names = {t["name"] for t in data["tools"]}
        assert names == {"apollo_people_search", "apollo_people_enrich"}

    @pytest.mark.asyncio
    async def test_group_tools_have_description(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, domain="apollo", group="people")

        tool = next(t for t in data["tools"] if t["name"] == "apollo_people_search")
        assert "Search for people" in tool["description"]

    @pytest.mark.asyncio
    async def test_unknown_group_error(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, domain="apollo", group="nonexistent")

        assert "error" in data
        assert "nonexistent" in data["error"]
        assert "people" in data["error"]
        assert "organizations" in data["error"]

    @pytest.mark.asyncio
    async def test_unknown_domain_with_group(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, domain="unknown", group="people")

        assert "error" in data
        assert "Unknown domain" in data["error"]


# ---------------------------------------------------------------------------
# Mode 4: keyword search
# ---------------------------------------------------------------------------


class TestDiscoverByQuery:
    @pytest.mark.asyncio
    async def test_search_by_keyword(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, query="enrich")

        assert data["query"] == "enrich"
        names = {r["name"] for r in data["results"]}
        assert names == {"apollo_people_enrich", "apollo_org_enrich"}

    @pytest.mark.asyncio
    async def test_search_cross_domain(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, query="search")

        domains = {r["domain"] for r in data["results"]}
        assert "apollo" in domains
        assert "hubspot" in domains

    @pytest.mark.asyncio
    async def test_search_no_results(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, query="nonexistent_xyz_123")

        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_search_results_include_domain(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, query="enrich")

        for r in data["results"]:
            assert "domain" in r
            assert "group" in r
            assert "description" in r

    @pytest.mark.asyncio
    async def test_query_takes_priority_over_domain(self, mcp_server: FastMCP) -> None:
        """When query is provided alongside domain, query mode wins."""
        data = await _call_discover(mcp_server, domain="apollo", query="deals")

        # Should search across ALL domains, not filter by apollo
        assert data["query"] == "deals"
        assert len(data["results"]) == 1
        assert data["results"][0]["domain"] == "hubspot"

    @pytest.mark.asyncio
    async def test_empty_query_falls_back_to_domain_summary(self, mcp_server: FastMCP) -> None:
        """Blank query should not trigger search mode."""
        data = await _call_discover(mcp_server, query="")

        # Should fall through to Mode 1 (domain summary), not return all tools
        assert "domains" in data
        assert "total_tools" in data

    @pytest.mark.asyncio
    async def test_whitespace_query_falls_back_to_domain_summary(self, mcp_server: FastMCP) -> None:
        """Whitespace-only query should not trigger search mode."""
        data = await _call_discover(mcp_server, query="   ")

        assert "domains" in data
        assert "total_tools" in data

"""Tests for the discover_tools meta-tool."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.meta_tools import register_meta_tools
from tests.conftest import result_payload, result_text

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
    return result_payload(result)


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
        data = await _call_discover(mcp_server, domain="apollo", format="schema")

        assert data["domain"] == "apollo"
        assert len(data["tools"]) == 4
        names = {t["name"] for t in data["tools"]}
        assert "apollo_people_search" in names

    @pytest.mark.asyncio
    async def test_tools_include_group(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, domain="apollo", format="schema")

        tool = next(t for t in data["tools"] if t["name"] == "apollo_people_search")
        assert tool["group"] == "people"

    @pytest.mark.asyncio
    async def test_unknown_domain_error(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, domain="salesforce")

        assert data["code"] == "domain_not_found"
        assert "salesforce" in data["error"]
        assert "apollo" in data["error"]
        assert "hubspot" in data["error"]


# ---------------------------------------------------------------------------
# Mode 3: domain + group -> tools in group
# ---------------------------------------------------------------------------


class TestDiscoverByGroup:
    @pytest.mark.asyncio
    async def test_lists_group_tools(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, domain="apollo", group="people", format="schema")

        assert data["domain"] == "apollo"
        assert data["group"] == "people"
        assert len(data["tools"]) == 2
        names = {t["name"] for t in data["tools"]}
        assert names == {"apollo_people_search", "apollo_people_enrich"}

    @pytest.mark.asyncio
    async def test_group_tools_have_description(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, domain="apollo", group="people", format="schema")

        tool = next(t for t in data["tools"] if t["name"] == "apollo_people_search")
        assert "Search for people" in tool["description"]

    @pytest.mark.asyncio
    async def test_unknown_group_error(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, domain="apollo", group="nonexistent")

        assert data["code"] == "group_not_found"
        assert "nonexistent" in data["error"]
        assert "people" in data["error"]
        assert "organizations" in data["error"]

    @pytest.mark.asyncio
    async def test_unknown_domain_with_group(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, domain="unknown", group="people")

        assert data["code"] == "domain_not_found"
        assert "Unknown domain" in data["error"]


# ---------------------------------------------------------------------------
# Mode 4: keyword search
# ---------------------------------------------------------------------------


class TestDiscoverByQuery:
    @pytest.mark.asyncio
    async def test_search_by_keyword(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, query="enrich", format="schema")

        assert data["query"] == "enrich"
        names = {r["name"] for r in data["results"]}
        assert names == {"apollo_people_enrich", "apollo_org_enrich"}

    @pytest.mark.asyncio
    async def test_search_cross_domain(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, query="search", format="schema")

        domains = {r["domain"] for r in data["results"]}
        assert "apollo" in domains
        assert "hubspot" in domains

    @pytest.mark.asyncio
    async def test_search_no_results(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, query="nonexistent_xyz_123", format="schema")

        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_search_results_include_domain(self, mcp_server: FastMCP) -> None:
        data = await _call_discover(mcp_server, query="enrich", format="schema")

        for r in data["results"]:
            assert "domain" in r
            assert "group" in r
            assert "description" in r

    @pytest.mark.asyncio
    async def test_query_takes_priority_over_domain(self, mcp_server: FastMCP) -> None:
        """When query is provided alongside domain, query mode wins."""
        data = await _call_discover(mcp_server, domain="apollo", query="deals", format="schema")

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


# ---------------------------------------------------------------------------
# format="signatures" — plain-text Python-signature output
# ---------------------------------------------------------------------------


async def _call_discover_text(mcp: FastMCP, **kwargs: object) -> str:
    """Helper: call discover_tools and return the raw string (skip JSON parse)."""
    async with Client(mcp) as client:
        result = await client.call_tool("discover_tools", {k: v for k, v in kwargs.items() if v is not None})
    return result_text(result)


class TestDiscoverSignaturesFormat:
    @pytest.mark.asyncio
    async def test_domain_mode_signatures(self, mcp_server: FastMCP) -> None:
        """format=signatures returns plain-text sigs for domain-only mode."""
        out = await _call_discover_text(mcp_server, domain="apollo", format="signatures")

        # Should contain signature lines, not JSON.
        assert "apollo_people_search(" in out
        assert "apollo_people_enrich(" in out
        assert "-> any" in out
        # Descriptions are rendered on indented continuation lines.
        assert "Search for people" in out

    @pytest.mark.asyncio
    async def test_group_mode_signatures(self, mcp_server: FastMCP) -> None:
        out = await _call_discover_text(mcp_server, domain="apollo", group="people", format="signatures")
        assert "apollo_people_search(" in out
        assert "apollo_people_enrich(" in out
        # organizations group should NOT appear
        assert "apollo_org_search(" not in out

    @pytest.mark.asyncio
    async def test_query_mode_signatures(self, mcp_server: FastMCP) -> None:
        out = await _call_discover_text(mcp_server, query="deals", format="signatures")
        assert "hubspot_deals_list(" in out

    @pytest.mark.asyncio
    async def test_domain_summary_ignores_format(self, mcp_server: FastMCP) -> None:
        """Mode 1 (no-args) always returns JSON, ignoring format param."""
        out = await _call_discover_text(mcp_server, format="signatures")
        parsed = json.loads(out)
        assert "domains" in parsed

    @pytest.mark.asyncio
    async def test_explicit_schema_format_returns_json(self, mcp_server: FastMCP) -> None:
        """format="schema" (explicit) still returns the JSON summary."""
        data = await _call_discover(mcp_server, domain="apollo", format="schema")
        assert data["domain"] == "apollo"
        assert "tools" in data

    @pytest.mark.asyncio
    async def test_default_format_is_signatures(self, mcp_server: FastMCP) -> None:
        """No explicit ``format=`` renders signatures, not JSON, in domain and group modes."""
        # Domain-only mode: default is signatures text, not JSON.
        domain_out = await _call_discover_text(mcp_server, domain="apollo")
        assert "apollo_people_search(" in domain_out
        assert "apollo_people_enrich(" in domain_out
        with pytest.raises(json.JSONDecodeError):
            json.loads(domain_out)

        # Domain + group mode: default is signatures text, not JSON.
        group_out = await _call_discover_text(mcp_server, domain="apollo", group="people")
        assert "apollo_people_search(" in group_out
        assert "apollo_org_search(" not in group_out
        with pytest.raises(json.JSONDecodeError):
            json.loads(group_out)

        # format="schema" explicitly still returns JSON.
        schema_data = await _call_discover(mcp_server, domain="apollo", format="schema")
        assert schema_data["domain"] == "apollo"
        assert "tools" in schema_data

        # The no-argument domain summary is unchanged: always JSON.
        summary = await _call_discover(mcp_server)
        assert "domains" in summary
        assert summary["total_tools"] == 7

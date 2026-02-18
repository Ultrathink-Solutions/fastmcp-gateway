"""Integration tests: full gateway flow with real in-process upstream MCP servers."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.meta_tools import register_meta_tools
from fastmcp_gateway.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Mock upstream MCP servers
# ---------------------------------------------------------------------------


def _create_crm_server() -> FastMCP:
    """A mock CRM upstream with contacts and deals tools."""
    mcp = FastMCP("crm-upstream")

    @mcp.tool()
    def crm_contacts_search(query: str, limit: int = 10) -> str:
        """Search contacts by name or email."""
        return json.dumps({"contacts": [{"name": "Jane Doe", "email": "jane@example.com"}], "total": 1})

    @mcp.tool()
    def crm_contacts_create(name: str, email: str) -> str:
        """Create a new contact."""
        return json.dumps({"id": "c-123", "name": name, "email": email})

    @mcp.tool()
    def crm_deals_list(status: str = "open") -> str:
        """List deals by status."""
        return json.dumps({"deals": [{"id": "d-1", "name": "Big Deal", "status": status}]})

    return mcp


def _create_analytics_server() -> FastMCP:
    """A mock analytics upstream with reporting tools."""
    mcp = FastMCP("analytics-upstream")

    @mcp.tool()
    def analytics_reports_generate(report_type: str, date_range: str = "last_30d") -> str:
        """Generate an analytics report."""
        return json.dumps({"report": report_type, "date_range": date_range, "rows": 42})

    @mcp.tool()
    def analytics_metrics_query(metric: str) -> str:
        """Query a specific metric."""
        return json.dumps({"metric": metric, "value": 99.5})

    return mcp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def crm_server() -> FastMCP:
    return _create_crm_server()


@pytest.fixture
def analytics_server() -> FastMCP:
    return _create_analytics_server()


@pytest.fixture
async def gateway(crm_server: FastMCP, analytics_server: FastMCP) -> FastMCP:
    """A fully wired gateway with real upstream servers (in-process)."""
    registry = ToolRegistry()
    # Pass FastMCP instances directly — Client accepts them for in-process transport
    upstream_manager = UpstreamManager(
        {"crm": crm_server, "analytics": analytics_server},  # type: ignore[dict-item]
        registry,
    )
    await upstream_manager.populate_all()

    mcp = FastMCP("integration-gateway")
    register_meta_tools(mcp, registry, upstream_manager)
    return mcp


async def _call_tool(mcp: FastMCP, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a tool on the gateway and return parsed JSON."""
    async with Client(mcp) as client:
        result = await client.call_tool(name, args or {})
    text = str(result.data) if result.data is not None else result.content[0].text  # type: ignore[union-attr]
    return json.loads(text)


# ---------------------------------------------------------------------------
# Integration: full discovery → schema → execute flow
# ---------------------------------------------------------------------------


class TestFullFlow:
    @pytest.mark.asyncio
    async def test_discover_then_schema_then_execute(self, gateway: FastMCP) -> None:
        """Test the complete intended LLM workflow."""
        # Step 1: discover domains
        domains = await _call_tool(gateway, "discover_tools")
        assert domains["total_tools"] == 5
        domain_names = {d["name"] for d in domains["domains"]}
        assert domain_names == {"analytics", "crm"}

        # Step 2: drill into CRM domain
        crm_tools = await _call_tool(gateway, "discover_tools", {"domain": "crm"})
        assert len(crm_tools["tools"]) == 3
        tool_names = {t["name"] for t in crm_tools["tools"]}
        assert "crm_contacts_search" in tool_names

        # Step 3: get schema for a tool
        schema = await _call_tool(gateway, "get_tool_schema", {"tool_name": "crm_contacts_search"})
        assert schema["name"] == "crm_contacts_search"
        assert "parameters" in schema
        assert "query" in schema["parameters"]["properties"]

        # Step 4: execute the tool
        result = await _call_tool(
            gateway,
            "execute_tool",
            {"tool_name": "crm_contacts_search", "arguments": {"query": "Jane"}},
        )
        assert result["tool"] == "crm_contacts_search"
        inner = json.loads(result["result"])
        assert inner["contacts"][0]["name"] == "Jane Doe"


# ---------------------------------------------------------------------------
# Integration: multi-domain
# ---------------------------------------------------------------------------


class TestMultiDomain:
    @pytest.mark.asyncio
    async def test_cross_domain_search(self, gateway: FastMCP) -> None:
        """Keyword search finds tools across both upstream domains."""
        results = await _call_tool(gateway, "discover_tools", {"query": "query"})
        domains = {r["domain"] for r in results["results"]}
        assert "analytics" in domains

    @pytest.mark.asyncio
    async def test_execute_across_domains(self, gateway: FastMCP) -> None:
        """Execute tools on different upstreams in sequence."""
        # CRM tool
        r1 = await _call_tool(
            gateway,
            "execute_tool",
            {"tool_name": "crm_deals_list", "arguments": {"status": "closed"}},
        )
        assert r1["tool"] == "crm_deals_list"
        deals = json.loads(r1["result"])
        assert deals["deals"][0]["status"] == "closed"

        # Analytics tool
        r2 = await _call_tool(
            gateway,
            "execute_tool",
            {"tool_name": "analytics_reports_generate", "arguments": {"report_type": "revenue"}},
        )
        assert r2["tool"] == "analytics_reports_generate"
        report = json.loads(r2["result"])
        assert report["report"] == "revenue"


# ---------------------------------------------------------------------------
# Integration: group auto-discovery
# ---------------------------------------------------------------------------


class TestGroupDiscovery:
    @pytest.mark.asyncio
    async def test_groups_inferred_from_tool_names(self, gateway: FastMCP) -> None:
        """Groups are auto-inferred from tool name prefixes."""
        crm_tools = await _call_tool(gateway, "discover_tools", {"domain": "crm"})
        groups = {t["group"] for t in crm_tools["tools"]}
        assert groups == {"contacts", "deals"}

    @pytest.mark.asyncio
    async def test_filter_by_group(self, gateway: FastMCP) -> None:
        contacts = await _call_tool(gateway, "discover_tools", {"domain": "crm", "group": "contacts"})
        assert len(contacts["tools"]) == 2
        names = {t["name"] for t in contacts["tools"]}
        assert names == {"crm_contacts_search", "crm_contacts_create"}


# ---------------------------------------------------------------------------
# Integration: error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_unknown_tool_suggests_alternatives(self, gateway: FastMCP) -> None:
        result = await _call_tool(gateway, "execute_tool", {"tool_name": "crm_contacts"})
        assert "error" in result
        assert "Did you mean" in result["error"]

    @pytest.mark.asyncio
    async def test_unknown_domain_lists_available(self, gateway: FastMCP) -> None:
        result = await _call_tool(gateway, "discover_tools", {"domain": "salesforce"})
        assert "error" in result
        assert "crm" in result["error"]
        assert "analytics" in result["error"]

    @pytest.mark.asyncio
    async def test_unknown_group_lists_available(self, gateway: FastMCP) -> None:
        result = await _call_tool(gateway, "discover_tools", {"domain": "crm", "group": "billing"})
        assert "error" in result
        assert "contacts" in result["error"]
        assert "deals" in result["error"]

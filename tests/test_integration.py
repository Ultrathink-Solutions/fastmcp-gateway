"""Integration tests: full gateway flow with real in-process upstream MCP servers."""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp import types as mcp_types
from mcp.shared.exceptions import McpError

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.meta_tools import register_meta_tools
from fastmcp_gateway.registry import ToolEntry, ToolRegistry

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
    """Call a tool on the gateway and return the parsed legacy text envelope.

    Reads ``content[0].text`` directly: ``execute_tool`` now also populates
    ``structured_content`` on its ``ToolResult`` for the new MCP typed channel,
    but these tests assert on the legacy ``{"tool": ..., "result": ...}``
    envelope which lives in the TextContent block.
    """
    async with Client(mcp) as client:
        result = await client.call_tool(name, args or {})
    text = result.content[0].text  # type: ignore[union-attr]
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
        crm_tools = await _call_tool(gateway, "discover_tools", {"domain": "crm", "format": "schema"})
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
        results = await _call_tool(gateway, "discover_tools", {"query": "query", "format": "schema"})
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
        crm_tools = await _call_tool(gateway, "discover_tools", {"domain": "crm", "format": "schema"})
        groups = {t["group"] for t in crm_tools["tools"]}
        assert groups == {"contacts", "deals"}

    @pytest.mark.asyncio
    async def test_filter_by_group(self, gateway: FastMCP) -> None:
        contacts = await _call_tool(
            gateway, "discover_tools", {"domain": "crm", "group": "contacts", "format": "schema"}
        )
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


# ---------------------------------------------------------------------------
# Integration: upstream errors over a real MCP session
# ---------------------------------------------------------------------------

_BAD_WIDGET = "Tool 'widgets_get' parameter validation failed: widget_id: must start with 'w-'."


def _create_protocol_error_server() -> FastMCP:
    """An upstream that answers some calls with a JSON-RPC error response.

    Several MCP server frameworks validate a call's arguments before the tool
    runs and report a failure as a ``-32602`` JSON-RPC error rather than as an
    ``isError`` result. This server's ``tools/call`` handler does the same for
    a rule its declared schema cannot express, so the error reaches the
    gateway over a real session exactly as such a server sends it.
    """
    mcp = FastMCP("widgets-upstream")

    @mcp.tool()
    def widgets_get(widget_id: str) -> str:
        """Fetch one widget."""
        return json.dumps({"id": widget_id})

    @mcp.tool()
    def widgets_fail() -> str:
        """Fail inside the tool (reported as an isError result)."""
        raise ToolError("widget store refused the request")

    @mcp.tool()
    def widgets_crash() -> str:
        """Fail in the request handler (reported as a JSON-RPC internal error)."""
        return "unreachable"

    @mcp.tool()
    def widgets_busy() -> str:
        """Fail in the request handler with the implementation-defined server-error code -32000."""
        return "unreachable"

    @mcp.tool()
    def widgets_closed() -> str:
        """Fail in the request handler with the code and text the MCP client uses for a dropped session."""
        return "unreachable"

    @mcp.tool()
    def widgets_hang() -> str:
        """Never answer in time."""
        return "unreachable"

    lowlevel = mcp._mcp_server
    default_handler = lowlevel.request_handlers[mcp_types.CallToolRequest]

    async def handler(request: mcp_types.CallToolRequest) -> mcp_types.ServerResult:
        if request.params.name == "widgets_crash":
            raise McpError(mcp_types.ErrorData(code=mcp_types.INTERNAL_ERROR, message="widget index is rebuilding"))
        if request.params.name == "widgets_busy":
            raise McpError(mcp_types.ErrorData(code=-32000, message="widget quota exhausted for this hour"))
        if request.params.name == "widgets_closed":
            raise McpError(mcp_types.ErrorData(code=mcp_types.CONNECTION_CLOSED, message="Connection closed"))
        if request.params.name == "widgets_hang":
            await anyio.sleep(30)
        widget_id = (request.params.arguments or {}).get("widget_id")
        if request.params.name == "widgets_get" and not str(widget_id).startswith("w-"):
            raise McpError(mcp_types.ErrorData(code=mcp_types.INVALID_PARAMS, message=_BAD_WIDGET))
        return await default_handler(request)

    lowlevel.request_handlers[mcp_types.CallToolRequest] = handler
    return mcp


@pytest.fixture
async def widgets_gateway() -> FastMCP:
    """A gateway in front of the protocol-error upstream, plus one unreachable upstream."""
    registry = ToolRegistry()
    upstream_manager = UpstreamManager(
        {
            "widgets": _create_protocol_error_server(),  # type: ignore[dict-item]
            # Nothing listens on port 9 (discard) here: every call is refused at connect.
            "offline": "http://127.0.0.1:9/mcp",
        },
        registry,
    )
    await upstream_manager.populate_domain("widgets")
    registry.register_tool(
        ToolEntry(
            name="offline_ping",
            domain="offline",
            group="general",
            description="Ping a server that is down.",
            input_schema={"type": "object", "properties": {}},
            upstream_url="http://127.0.0.1:9/mcp",
        )
    )
    mcp = FastMCP("widgets-gateway")
    register_meta_tools(mcp, registry, upstream_manager)
    return mcp


@pytest.fixture
async def timeout_gateway() -> FastMCP:
    """A gateway whose execution client to the protocol-error upstream gives up after 0.2 seconds."""
    registry = ToolRegistry()
    upstream_manager = UpstreamManager({"widgets": _create_protocol_error_server()}, registry)  # type: ignore[dict-item]
    await upstream_manager.populate_domain("widgets")
    upstream_manager._execution_clients["widgets"] = Client(_create_protocol_error_server(), timeout=0.2)
    mcp = FastMCP("widgets-timeout-gateway")
    register_meta_tools(mcp, registry, upstream_manager)
    return mcp


class TestUpstreamErrorsOverARealSession:
    """What a caller is told when an upstream answers a call with an error,
    when it fails inside the tool, and when it does not answer at all."""

    @pytest.mark.asyncio
    async def test_a_jsonrpc_argument_rejection_reaches_the_caller_as_invalid_arguments(
        self, widgets_gateway: FastMCP
    ) -> None:
        result = await _call_tool(
            widgets_gateway, "execute_tool", {"tool_name": "widgets_get", "arguments": {"widget_id": "17"}}
        )

        assert result["code"] == "invalid_arguments"
        assert result["error"] == _BAD_WIDGET
        assert result["details"]["upstream_error_code"] == mcp_types.INVALID_PARAMS
        assert result["details"]["signature"].startswith("widgets_get(widget_id: str)")

    @pytest.mark.asyncio
    async def test_the_corrected_call_succeeds(self, widgets_gateway: FastMCP) -> None:
        result = await _call_tool(
            widgets_gateway, "execute_tool", {"tool_name": "widgets_get", "arguments": {"widget_id": "w-17"}}
        )

        assert json.loads(result["result"]) == {"id": "w-17"}

    @pytest.mark.asyncio
    async def test_another_jsonrpc_answer_reaches_the_caller_as_an_upstream_error(
        self, widgets_gateway: FastMCP
    ) -> None:
        result = await _call_tool(widgets_gateway, "execute_tool", {"tool_name": "widgets_crash", "arguments": {}})

        assert result["code"] == "upstream_error"
        assert result["error"] == "widget index is rebuilding"
        assert result["details"]["upstream_error_code"] == mcp_types.INTERNAL_ERROR

    @pytest.mark.asyncio
    async def test_a_server_sent_minus_32000_is_an_answer_not_a_dropped_session(self, widgets_gateway: FastMCP) -> None:
        """``-32000`` is also the code the MCP client uses for a dropped session,
        but on the wire it is the upstream's own answer."""
        result = await _call_tool(widgets_gateway, "execute_tool", {"tool_name": "widgets_busy", "arguments": {}})

        assert result["code"] == "upstream_error"
        assert result["error"] == "widget quota exhausted for this hour"
        assert result["details"]["upstream_error_code"] == -32000

    @pytest.mark.asyncio
    async def test_a_server_sent_connection_closed_is_an_answer(self, widgets_gateway: FastMCP) -> None:
        """The same code and text as the MCP client's own dropped-session error,
        but sent by the upstream, so it is the upstream's answer."""
        result = await _call_tool(widgets_gateway, "execute_tool", {"tool_name": "widgets_closed", "arguments": {}})

        assert result["code"] == "upstream_error"
        assert result["error"] == "Connection closed"
        assert result["details"]["upstream_error_code"] == mcp_types.CONNECTION_CLOSED

    @pytest.mark.asyncio
    async def test_a_read_timeout_is_still_an_execution_error(self, timeout_gateway: FastMCP) -> None:
        """The MCP client raises its own ``McpError`` when the deadline passes."""
        result = await _call_tool(timeout_gateway, "execute_tool", {"tool_name": "widgets_hang", "arguments": {}})

        assert result["code"] == "execution_error"
        assert result["details"] == {"tool": "widgets_hang", "domain": "widgets"}

    @pytest.mark.asyncio
    async def test_a_tool_failure_is_still_an_upstream_error(self, widgets_gateway: FastMCP) -> None:
        result = await _call_tool(widgets_gateway, "execute_tool", {"tool_name": "widgets_fail", "arguments": {}})

        assert result["code"] == "upstream_error"
        assert "widget store refused the request" in result["error"]

    @pytest.mark.asyncio
    async def test_an_unreachable_upstream_is_still_an_execution_error(self, widgets_gateway: FastMCP) -> None:
        result = await _call_tool(widgets_gateway, "execute_tool", {"tool_name": "offline_ping", "arguments": {}})

        assert result["code"] == "execution_error"
        assert result["details"] == {"tool": "offline_ping", "domain": "offline"}

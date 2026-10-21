"""The gateway serves MCP clients of both protocol eras.

FastMCP 4 servers answer both the 2025-11-25 ``initialize`` handshake and
the sessionless 2026-07-28 era (``server/discover``, per-request metadata).
These tests hold the gateway to that on every surface a client sees: the
in-process transport, streamable HTTP, and the JSON on the wire, which keeps
MCP's camelCase names whatever era serves it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from fastmcp import Client, FastMCP

from fastmcp_gateway.gateway import GatewayServer
from tests.conftest import result_text
from tests.http_upstreams import serve_http

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

HANDSHAKE_ERA = "2025-11-25"
MODERN_ERA = "2026-07-28"

# A client of each era: one that opens with the handshake, and one in FastMCP
# 4's default ``auto`` mode, which probes ``server/discover`` and adopts the
# 2026-07-28 era when the server offers it. Each test asserts the era the
# connection negotiated, so the second cannot pass by falling back.
ERAS = [pytest.param("legacy", HANDSHAKE_ERA, id="2025-11-25"), pytest.param("auto", MODERN_ERA, id="2026-07-28")]

META_TOOLS = {"discover_tools", "get_tool_schema", "execute_tool", "refresh_registry"}


def _widgets_upstream() -> FastMCP:
    mcp = FastMCP("widgets")

    @mcp.tool
    def widgets_echo(text: str) -> str:
        """Echo the text back."""
        return f"echo: {text}"

    return mcp


@pytest.fixture
async def gateway() -> GatewayServer:
    gw = GatewayServer({"widgets": _widgets_upstream()})  # type: ignore[dict-item]
    await gw.populate()
    return gw


@pytest.fixture
async def gateway_url(gateway: GatewayServer) -> AsyncIterator[str]:
    async with serve_http(gateway.mcp.http_app()) as base:
        yield f"{base}/mcp"


async def _exercise(client: Client) -> dict[str, Any]:
    """List the meta-tools, discover, and run an upstream tool through the gateway."""
    tools = {t.name: t for t in await client.list_tools()}
    domains = await client.call_tool("discover_tools", {})
    executed = await client.call_tool("execute_tool", {"tool_name": "widgets_echo", "arguments": {"text": "hi"}})
    return {
        "tools": tools,
        "domains": domains.structured_content,
        "executed": json.loads(result_text(executed)),
        "executed_is_error": executed.is_error,
    }


def _assert_served(seen: dict[str, Any]) -> None:
    tools = seen["tools"]
    assert set(tools) == META_TOOLS
    annotations = tools["discover_tools"].annotations
    assert annotations is not None
    assert annotations.read_only_hint is True
    assert annotations.open_world_hint is True
    assert "tool_name" in tools["execute_tool"].input_schema["properties"]
    assert seen["domains"]["total_tools"] == 1
    assert [d["name"] for d in seen["domains"]["domains"]] == ["widgets"]
    assert seen["executed"] == {"tool": "widgets_echo", "result": "echo: hi"}
    assert seen["executed_is_error"] is False


class TestServesBothEras:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("mode", "negotiated"), ERAS)
    async def test_in_process(self, gateway: GatewayServer, mode: str, negotiated: str) -> None:
        async with Client(gateway.mcp, mode=mode) as client:
            assert client.protocol_version == negotiated
            seen = await _exercise(client)
        _assert_served(seen)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("mode", "negotiated"), ERAS)
    async def test_over_streamable_http(self, gateway_url: str, mode: str, negotiated: str) -> None:
        async with Client(gateway_url, mode=mode) as client:
            assert client.protocol_version == negotiated
            seen = await _exercise(client)
            session_id = client.transport.get_session_id()
            instructions = client.instructions
        _assert_served(seen)
        # The handshake opens a session; the 2026-07-28 era is sessionless.
        if negotiated == HANDSHAKE_ERA:
            assert session_id
        else:
            assert session_id is None
        # Both eras deliver the instructions with the populated domain summary:
        # the handshake in its InitializeResult, 2026-07-28 in server/discover.
        assert instructions is not None
        assert "**widgets** (1 tools)" in instructions

    @pytest.mark.asyncio
    async def test_a_pinned_2026_client(self, gateway: GatewayServer) -> None:
        """A client pinned to 2026-07-28 never sends the handshake, and is served."""
        async with Client(gateway.mcp, mode=MODERN_ERA) as client:
            assert client.protocol_version == MODERN_ERA
            assert client.initialize_result is None
            seen = await _exercise(client)
        _assert_served(seen)


# ---------------------------------------------------------------------------
# The JSON on the wire
# ---------------------------------------------------------------------------


_CLIENT_INFO = {"name": "wire-check", "version": "1.0"}


class _RawSession:
    """Speak JSON-RPC to the gateway over HTTP by hand, as either era."""

    def __init__(self, http: httpx2.AsyncClient, url: str, era: str) -> None:
        self.http = http
        self.url = url
        self.era = era
        self.session_id: str | None = None
        self.next_id = 0

    async def open(self) -> None:
        if self.era == MODERN_ERA:
            return  # sessionless: every request carries its own metadata
        answer = await self.request(
            "initialize",
            {"protocolVersion": HANDSHAKE_ERA, "capabilities": {}, "clientInfo": _CLIENT_INFO},
        )
        assert answer["result"]["protocolVersion"] == HANDSHAKE_ERA
        await self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def post(self, message: dict[str, Any]) -> httpx2.Response:
        headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
        if message.get("method") != "initialize":
            headers["mcp-protocol-version"] = self.era
        if self.era == MODERN_ERA:
            # 2026-07-28 routing headers mirror the body's method and name.
            headers["mcp-method"] = message["method"]
            name = message.get("params", {}).get("name")
            if isinstance(name, str):
                headers["mcp-name"] = name
        if self.session_id is not None:
            headers["mcp-session-id"] = self.session_id
        response = await self.http.post(self.url, json=message, headers=headers)
        if response.headers.get("mcp-session-id"):
            self.session_id = response.headers["mcp-session-id"]
        return response

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.era == MODERN_ERA:
            params = {
                **params,
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": MODERN_ERA,
                    "io.modelcontextprotocol/clientInfo": _CLIENT_INFO,
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            }
        self.next_id += 1
        response = await self.post({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/json")
        return response.json()


def _keys(value: Any) -> set[str]:
    """Every object key anywhere in a JSON value, except a JSON Schema's property names."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            if key == "properties" and isinstance(item, dict):
                for schema in item.values():
                    found |= _keys(schema)
            else:
                found |= _keys(item)
    elif isinstance(value, list):
        for item in value:
            found |= _keys(item)
    return found


class TestWireNames:
    """Whatever era serves it, the gateway's JSON uses MCP's camelCase wire names."""

    @pytest.fixture
    async def json_gateway_url(self, gateway: GatewayServer) -> AsyncIterator[str]:
        # JSON (not event-stream) answers, so the raw body is one JSON-RPC message.
        async with serve_http(gateway.mcp.http_app(json_response=True)) as base:
            yield f"{base}/mcp"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("era", [HANDSHAKE_ERA, MODERN_ERA])
    async def test_tools_list_and_call_on_the_wire(self, json_gateway_url: str, era: str) -> None:
        async with httpx2.AsyncClient(timeout=10) as http:
            session = _RawSession(http, json_gateway_url, era)
            await session.open()
            listed = (await session.request("tools/list", {}))["result"]
            called = (
                await session.request(
                    "tools/call",
                    {"name": "execute_tool", "arguments": {"tool_name": "widgets_echo", "arguments": {"text": "hi"}}},
                )
            )["result"]
            failed = (
                await session.request(
                    "tools/call", {"name": "get_tool_schema", "arguments": {"tool_name": "no_such_tool"}}
                )
            )["result"]

        discover = next(t for t in listed["tools"] if t["name"] == "discover_tools")
        assert discover["annotations"] == {"readOnlyHint": True, "openWorldHint": True}
        assert discover["inputSchema"]["type"] == "object"
        # The gateway's own structured payloads keep their snake_case field
        # names; only MCP's protocol fields are camelCase on the wire.
        assert called["content"] == [{"type": "text", "text": '{"tool": "widgets_echo", "result": "echo: hi"}'}]
        assert called["structuredContent"] == {"result": "echo: hi"}
        assert called["isError"] is False
        assert failed["structuredContent"]["code"] == "tool_not_found"
        protocol_keys = _keys(listed) | _keys({k: v for k, v in called.items() if k != "structuredContent"})
        snake_case_protocol_names = {
            "input_schema",
            "output_schema",
            "read_only_hint",
            "open_world_hint",
            "destructive_hint",
            "idempotent_hint",
            "structured_content",
            "is_error",
            "next_cursor",
        }
        assert protocol_keys.isdisjoint(snake_case_protocol_names)

"""Tests for the execute_tool meta-tool."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client, FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.meta_tools import register_meta_tools
from fastmcp_gateway.registry import ToolEntry

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


def _fake_result(
    text: str,
    *,
    is_error: bool = False,
    structured_content: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a fake CallToolResult with text content.

    ``structured_content`` defaults to ``None`` (explicit, not MagicMock-auto)
    so ``execute_tool``'s passthrough respects the MCP-spec contract that
    ``structuredContent`` is an object-or-absent.
    """
    block = MagicMock()
    block.text = text
    result = MagicMock()
    result.content = [block]
    result.is_error = is_error
    result.structured_content = structured_content
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

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["tool"] == "apollo_people_search"
        assert data["result"] == '{"people": []}'
        manager.execute_tool.assert_called_once_with("apollo_people_search", {"query": "Jane"})

    @pytest.mark.asyncio
    async def test_no_arguments_sends_none(self, mcp_server: FastMCP, manager: UpstreamManager) -> None:
        manager.execute_tool = AsyncMock(return_value=_fake_result("ok"))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "hubspot_contacts_search")

        assert data["result"] == "ok"
        manager.execute_tool.assert_called_once_with("hubspot_contacts_search", None)


# ---------------------------------------------------------------------------
# Error: unknown tool
# ---------------------------------------------------------------------------


class TestExecuteToolUnknown:
    @pytest.mark.asyncio
    async def test_unknown_tool_with_suggestions(self, mcp_server: FastMCP) -> None:
        data = await _call_execute(mcp_server, "apollo_search")

        assert data["code"] == "tool_not_found"
        assert "apollo_search" in data["error"]
        assert "Did you mean" in data["error"]

    @pytest.mark.asyncio
    async def test_unknown_tool_no_suggestions(self, mcp_server: FastMCP) -> None:
        data = await _call_execute(mcp_server, "completely_unrelated_xyz_123")

        assert data["code"] == "tool_not_found"
        assert "discover_tools" in data["error"]


# ---------------------------------------------------------------------------
# Error: upstream unreachable
# ---------------------------------------------------------------------------


class TestExecuteToolUpstreamError:
    @pytest.mark.asyncio
    async def test_connectivity_error(self, mcp_server: FastMCP, manager: UpstreamManager) -> None:
        manager.execute_tool = AsyncMock(side_effect=ConnectionError("connection refused"))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == "execution_error"
        assert "failed" in data["error"]
        assert data["details"]["domain"] == "apollo"
        assert data["details"]["tool"] == "apollo_people_search"

    @pytest.mark.asyncio
    async def test_upstream_tool_error(self, mcp_server: FastMCP, manager: UpstreamManager) -> None:
        """Upstream tool returns is_error=True."""
        manager.execute_tool = AsyncMock(  # type: ignore[method-assign]
            return_value=_fake_result("Invalid parameter: limit must be > 0", is_error=True)
        )

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == "upstream_error"
        assert "Invalid parameter" in data["error"]
        assert data["details"]["tool"] == "apollo_people_search"
        assert "result" not in data


# ---------------------------------------------------------------------------
# Argument validation: unknown/missing arguments are rejected before dispatch
# ---------------------------------------------------------------------------


class TestExecuteToolArgumentValidation:
    """A call whose arguments don't match the tool's declared schema is
    rejected before it ever reaches the upstream server, with the tool's
    full expected signature in the error -- so a caller that guessed wrong
    gets the correction in one hop instead of a second blind guess."""

    @pytest.mark.asyncio
    async def test_missing_required_argument_is_rejected_before_dispatch(
        self, mcp_server: FastMCP, manager: UpstreamManager
    ) -> None:
        manager.execute_tool = AsyncMock(return_value=_fake_result("should never be called"))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {})

        assert data["code"] == "invalid_arguments"
        assert "query" in data["error"]
        assert data["details"]["signature"] == (
            "apollo_people_search(query: str) -> any\n  Search for people by name, title, company, or other criteria"
        )
        manager.execute_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_argument_is_rejected_before_dispatch(
        self, mcp_server: FastMCP, manager: UpstreamManager
    ) -> None:
        """Isolates the UNKNOWN-argument branch only: the required `query`
        arg IS supplied, alongside one bogus extra key, so `missing` is
        empty and only the `unknown` branch of `_describe_argument_errors`
        can fire. `apollo_people_search`'s fixture schema
        (`required=["query"]`) means a call like `{"name": "Jane"}` (no
        `query` at all) would trip BOTH the unknown AND missing branches at
        once, which couldn't tell "unknown detection is broken" apart from
        "missing detection is broken" from "both work" -- see
        `test_missing_required_argument_is_rejected_before_dispatch` above
        for the separate missing-only case."""
        manager.execute_tool = AsyncMock(return_value=_fake_result("should never be called"))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane", "bogus": "x"})

        assert data["code"] == "invalid_arguments"
        assert "bogus" in data["error"]
        assert "apollo_people_search(query: str)" in data["details"]["signature"]
        manager.execute_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_schema_without_properties_is_not_validated(self, registry: ToolRegistry) -> None:
        """A tool whose schema declares no 'properties' object makes no claim
        about what's valid -- every call passes through unchecked, exactly as
        before this feature existed (matches extract_params' own fallback)."""
        registry.set_domain_description("legacy", "Legacy passthrough tools")
        registry.register_tool(
            ToolEntry(
                name="legacy_passthrough",
                domain="legacy",
                group="misc",
                description="Accepts whatever the caller sends.",
                input_schema={"type": "object"},
                upstream_url="http://legacy-mcp:8080/mcp",
            )
        )
        with patch("fastmcp_gateway.client_manager.Client"):
            manager = UpstreamManager({"legacy": "http://legacy-mcp:8080/mcp"}, registry)
        manager.execute_tool = AsyncMock(return_value=_fake_result("ok"))  # type: ignore[method-assign]
        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, registry, manager)

        data = await _call_execute(mcp, "legacy_passthrough", {"anything": "goes"})

        assert data["result"] == "ok"
        manager.execute_tool.assert_called_once_with("legacy_passthrough", {"anything": "goes"})

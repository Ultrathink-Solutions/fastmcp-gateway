"""Tests for the optional instructions preamble."""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client

from fastmcp_gateway import GatewayServer
from tests.conftest import result_payload, result_text

DEFAULT_OPENING = "You have access to a tool discovery gateway"

# The text an unconfigured gateway serves, pinned as literals.
EMPTY_REGISTRY_DEFAULT = (
    "You have access to a tool discovery gateway with 3 tools:\n"
    "1. discover_tools - Browse available tools. Call with no arguments to see domains, "
    "or with a domain to see tool signatures.\n"
    "2. execute_tool - Run any discovered tool.\n"
    "3. get_tool_schema - Fetch a tool's full parameter schema when its signature "
    "is not enough.\n"
    "Workflow: discover_tools -> execute_tool; get_tool_schema only when needed. "
    "Skip discovery for tools you've already used in this conversation."
)
ONE_DOMAIN_DEFAULT = (
    "You have access to a tool discovery gateway with tools across these domains:\n\n"
    "- **svc** (1 tools)\n\n"
    "Workflow: discover_tools() \u2192 execute_tool() \u2014 discovery renders each tool "
    "as a callable signature.\n"
    'Use `discover_tools(domain="...")` to see tools in a specific domain.\n'
    "Call get_tool_schema() only when a signature is not enough "
    "(deeply nested arguments, or the raw JSON schema is needed).\n"
    "Skip discovery for tools you've already used in this conversation."
)
ONE_DOMAIN_LISTING = (
    '{"domains": [{"name": "svc", "description": "", "tool_count": 1, "groups": ["widgets"]}], "total_tools": 1}'
)
# sha256 of the discover_tools description as served by v0.39.1.
DISCOVER_DESCRIPTION_SHA256 = "a84cf7d75b326a1eaf60cb89011d7abfc4c4a091a44f0abf8176abe001d1b291"


def _gateway(**kwargs: Any) -> GatewayServer:
    with patch("fastmcp_gateway.client_manager.Client"):
        gw = GatewayServer({"svc": "http://svc:8080/mcp"}, **kwargs)
    gw.registry.populate_domain(
        "svc",
        "http://svc:8080/mcp",
        [{"name": "widgets_list", "inputSchema": {"type": "object"}}],
    )
    return gw


def test_instructions_open_with_the_preamble() -> None:
    gw = _gateway(instructions_preamble="RULE")
    text = gw._build_instructions()
    assert text.startswith(f"RULE\n\n{DEFAULT_OPENING}")
    assert "**svc** (1 tools)" in text


def test_the_preamble_survives_a_registry_refresh() -> None:
    gw = _gateway(instructions_preamble="RULE")
    gw._update_instructions()
    assert gw.mcp.instructions.startswith(f"RULE\n\n{DEFAULT_OPENING}")
    assert "**svc** (1 tools)" in gw.mcp.instructions


def test_empty_registry_instructions_open_with_the_preamble() -> None:
    with patch("fastmcp_gateway.client_manager.Client"):
        gw = GatewayServer({"svc": "http://svc:8080/mcp"}, instructions_preamble="RULE")
    assert gw.mcp.instructions == f"RULE\n\n{EMPTY_REGISTRY_DEFAULT}"


def test_an_empty_registry_rebuild_does_not_wrap_twice() -> None:
    with patch("fastmcp_gateway.client_manager.Client"):
        gw = GatewayServer({"svc": "http://svc:8080/mcp"}, instructions_preamble="RULE")
    gw._update_instructions()
    assert gw.mcp.instructions == f"RULE\n\n{EMPTY_REGISTRY_DEFAULT}"


def test_custom_instructions_are_used_whole() -> None:
    gw = _gateway(instructions="X", instructions_preamble="RULE")
    gw._update_instructions()
    assert gw.mcp.instructions == "X"


@pytest.mark.asyncio
async def test_custom_instructions_still_get_discovery_guidance() -> None:
    gw = _gateway(instructions="X", instructions_preamble="RULE")
    assert gw.mcp.instructions == "X"
    async with Client(gw.mcp) as client:
        listing = result_payload(await client.call_tool("discover_tools", {}))
    assert next(iter(listing)) == "guidance"
    assert listing["guidance"] == "RULE"


@pytest.mark.asyncio
async def test_domain_listing_carries_the_guidance() -> None:
    gw = _gateway(instructions_preamble="RULE")
    async with Client(gw.mcp) as client:
        listing = result_payload(await client.call_tool("discover_tools", {}))
        scoped = result_text(await client.call_tool("discover_tools", {"domain": "svc"}))
    assert next(iter(listing)) == "guidance"
    assert listing["guidance"] == "RULE"
    assert list(listing) == ["guidance", "domains", "total_tools"]
    assert "guidance" not in scoped
    assert "RULE" not in scoped


@pytest.mark.asyncio
async def test_no_preamble_changes_nothing() -> None:
    gw = _gateway()
    assert gw._build_instructions() == ONE_DOMAIN_DEFAULT
    with patch("fastmcp_gateway.client_manager.Client"):
        empty = GatewayServer({"svc": "http://svc:8080/mcp"})
    assert empty.mcp.instructions == EMPTY_REGISTRY_DEFAULT
    async with Client(gw.mcp) as client:
        assert result_text(await client.call_tool("discover_tools", {})) == ONE_DOMAIN_LISTING
        tool = next(t for t in await client.list_tools() if t.name == "discover_tools")
    assert hashlib.sha256((tool.description or "").encode()).hexdigest() == DISCOVER_DESCRIPTION_SHA256


def test_an_empty_preamble_argument_is_unset() -> None:
    gw = _gateway(instructions_preamble="")
    assert gw._instructions_preamble is None
    assert gw._build_instructions() == ONE_DOMAIN_DEFAULT


@pytest.mark.parametrize(("value", "expected"), [("", None), ("RULE", "RULE")])
def test_the_preamble_variable_reaches_the_server(value: str, expected: str | None) -> None:
    from fastmcp_gateway import __main__ as cli

    env = {"GATEWAY_UPSTREAMS": '{"svc": "http://svc:8080/mcp"}', "GATEWAY_INSTRUCTIONS_PREAMBLE": value}
    with (
        patch.dict("os.environ", env, clear=True),
        patch.object(cli, "GatewayServer") as server,
        patch.object(cli, "_populate", MagicMock()),
        patch.object(cli.asyncio, "run"),
    ):
        cli.main()
    assert server.call_args.kwargs["instructions_preamble"] == expected

"""The result-channel contract for the discovery meta-tools.

``discover_tools`` and ``get_tool_schema`` answer on two MCP channels:
the text block and ``structuredContent``. These tests pin what each
channel carries, and -- more importantly -- pin the *mechanism* that
keeps them from disagreeing.

A meta-tool annotated ``-> str`` returns a non-object. MCP requires
``structuredContent`` to be an object, so FastMCP derives an output
schema, marks it ``x-fastmcp-wrap-result``, and publishes the return as
``{"result": <the JSON string>}``. The payload is then encoded twice:
once by the tool's own ``json.dumps``, once by whatever renders the
wrapper object -- so a consumer sees ``\\"`` for every quote and ``\\n``
for every newline, and has to ``json.loads(data["result"])`` to reach
data the structured channel was supposed to hand it directly.

Returning ``ToolResult`` with an explicit ``output_schema=None`` is what
removes the wrap. Both halves matter, so the wrap-marker test below
asserts against the advertised schema rather than against a rendered
payload: it fails if either half is dropped, and it fails at the point
of regression rather than three layers downstream in a consumer.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.meta_tools import register_meta_tools

if TYPE_CHECKING:
    from fastmcp_gateway.registry import ToolRegistry

DISCOVERY_TOOLS = ("discover_tools", "get_tool_schema")


@pytest.fixture
def mcp_server(populated_registry: ToolRegistry) -> FastMCP:
    """A FastMCP server with the meta-tools registered."""
    mcp = FastMCP("test-gateway")
    with patch("fastmcp_gateway.client_manager.Client"):
        manager = UpstreamManager(
            {"apollo": "http://apollo:8080/mcp", "hubspot": "http://hubspot:8080/mcp"},
            populated_registry,
        )
    register_meta_tools(mcp, populated_registry, manager)
    return mcp


async def _call(mcp: FastMCP, tool: str, args: dict[str, Any]) -> Any:
    async with Client(mcp) as client:
        return await client.call_tool(tool, args)


# ---------------------------------------------------------------------------
# The mechanism: no wrapped output schema on either discovery meta-tool
# ---------------------------------------------------------------------------


class TestNoWrappedOutputSchema:
    @pytest.mark.parametrize("tool_name", DISCOVERY_TOOLS)
    @pytest.mark.asyncio
    async def test_no_wrap_result_marker(self, mcp_server: FastMCP, tool_name: str) -> None:
        """Neither tool advertises a wrap-result output schema.

        This is the regression sentinel. Re-annotating either tool
        ``-> str``, or dropping its ``output_schema=None``, re-derives a
        wrapped schema and fails here -- long before a consumer has to
        notice that its payload came back escaped twice.
        """
        async with Client(mcp_server) as client:
            tools = {t.name: t for t in await client.list_tools()}

        schema = tools[tool_name].outputSchema
        assert schema is None or "x-fastmcp-wrap-result" not in schema, (
            f"{tool_name} advertises a wrap-result output schema; its payload will be double-encoded"
        )


# ---------------------------------------------------------------------------
# get_tool_schema
# ---------------------------------------------------------------------------


class TestGetToolSchemaChannels:
    @pytest.mark.asyncio
    async def test_structured_content_is_the_schema_itself(self, mcp_server: FastMCP) -> None:
        result = await _call(mcp_server, "get_tool_schema", {"tool_name": "apollo_people_search"})

        assert result.structured_content is not None
        # The defect shape: a lone "result" key holding a JSON string.
        assert list(result.structured_content) != ["result"]
        assert result.structured_content["name"] == "apollo_people_search"
        assert result.structured_content["parameters"]["type"] == "object"

    @pytest.mark.asyncio
    async def test_no_value_is_a_serialised_json_object(self, mcp_server: FastMCP) -> None:
        """No field smuggles a nested JSON document through as a string."""
        result = await _call(mcp_server, "get_tool_schema", {"tool_name": "apollo_people_search"})

        for key, value in result.structured_content.items():
            assert not (isinstance(value, str) and value.lstrip().startswith("{")), (
                f"{key!r} carries a serialised JSON object rather than an object"
            )

    @pytest.mark.asyncio
    async def test_text_block_agrees_with_structured_content(self, mcp_server: FastMCP) -> None:
        """The text channel keeps its historical shape and the two agree."""
        result = await _call(mcp_server, "get_tool_schema", {"tool_name": "apollo_people_search"})

        assert json.loads(result.content[0].text) == result.structured_content

    @pytest.mark.asyncio
    async def test_unknown_tool_error_is_structured(self, mcp_server: FastMCP) -> None:
        """A caller can branch on ``code`` without parsing text."""
        result = await _call(mcp_server, "get_tool_schema", {"tool_name": "nonexistent_xyz"})

        assert result.structured_content["code"] == "tool_not_found"
        assert "nonexistent_xyz" in result.structured_content["error"]
        assert json.loads(result.content[0].text) == result.structured_content


# ---------------------------------------------------------------------------
# discover_tools
# ---------------------------------------------------------------------------


class TestDiscoverToolsChannels:
    @pytest.mark.parametrize(
        "args",
        [
            pytest.param({}, id="domain-summary"),
            pytest.param({"domain": "apollo", "format": "schema"}, id="domain-mode"),
            pytest.param({"domain": "apollo", "group": "people", "format": "schema"}, id="group-mode"),
            pytest.param({"query": "deals", "format": "schema"}, id="query-mode"),
        ],
    )
    @pytest.mark.asyncio
    async def test_json_modes_publish_the_object(self, mcp_server: FastMCP, args: dict[str, Any]) -> None:
        result = await _call(mcp_server, "discover_tools", args)

        assert result.structured_content is not None
        assert list(result.structured_content) != ["result"]
        assert json.loads(result.content[0].text) == result.structured_content

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param({"domain": "apollo"}, id="domain-mode"),
            pytest.param({"domain": "apollo", "group": "people"}, id="group-mode"),
            pytest.param({"query": "deals"}, id="query-mode"),
        ],
    )
    @pytest.mark.asyncio
    async def test_signatures_publish_no_structured_content(self, mcp_server: FastMCP, args: dict[str, Any]) -> None:
        """The signatures block is prose, so the structured channel stays empty.

        It previously arrived as ``{"result": "<the whole block>"}`` -- a
        structured channel whose only field restated the text channel.
        """
        result = await _call(mcp_server, "discover_tools", args)

        assert result.structured_content is None
        assert "apollo_people_search(" in result.content[0].text or "hubspot_deals_list(" in result.content[0].text

    @pytest.mark.parametrize(
        ("args", "code"),
        [
            pytest.param({"domain": "nope"}, "domain_not_found", id="domain"),
            pytest.param({"domain": "apollo", "group": "nope"}, "group_not_found", id="group"),
        ],
    )
    @pytest.mark.asyncio
    async def test_errors_are_structured(self, mcp_server: FastMCP, args: dict[str, Any], code: str) -> None:
        result = await _call(mcp_server, "discover_tools", args)

        assert result.structured_content["code"] == code
        assert json.loads(result.content[0].text) == result.structured_content

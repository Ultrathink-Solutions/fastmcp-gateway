"""The meta-tool descriptions a model reads.

A model choosing and calling a meta-tool sees only what ``tools/list``
serves: each tool's description and its input schema, including every
parameter's description. These tests read exactly that, through an MCP
client, and hold it to a few rules that keep it useful to a model:

1. No file paths. A model cannot open them.
2. No internal type or implementation names, and no wording about the
   gateway's history ("legacy", "envelope"). A model cannot act on them.
3. A description says more than a title: at least two sentences, not
   counting code spans.
4. Every parameter, and every field of an object parameter, has a
   non-empty description.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.meta_tools import register_meta_tools
from fastmcp_gateway.registry import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mcp.types import Tool

META_TOOLS = ("discover_tools", "get_tool_schema", "execute_tool", "refresh_registry", "execute_code")

_FILE_PATH = re.compile(r"\w\.py\b|\bsrc/")

_INTERNAL_NAMES = re.compile(
    r"ToolResult|CallToolResult|ToolRegistry|UpstreamManager|ToolEntry|HookRunner|fastmcp\."
    r"|:(?:class|meth|func|mod|attr):"
    r"|\blegacy\b|\benvelope\b",
    re.IGNORECASE,
)

_CODE_SPAN = re.compile(r"`[^`]*`")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


async def _served_tools() -> dict[str, Tool]:
    """Every meta-tool as an MCP client lists it, ``execute_code`` included."""
    registry = ToolRegistry()
    with patch("fastmcp_gateway.client_manager.Client"):
        manager = UpstreamManager({"widgets": "http://widgets:8080/mcp"}, registry)
    mcp = FastMCP("test-gateway")
    # Any non-None runner registers execute_code; listing tools never calls it.
    register_meta_tools(mcp, registry, manager, code_mode_runner=object())
    async with Client(mcp) as client:
        return {t.name: t for t in await client.list_tools()}


def _served_text(tool: Tool) -> str:
    """All the text a model reads for *tool*: its description and its input schema."""
    return f"{tool.description or ''}\n{json.dumps(tool.input_schema)}"


def _undescribed_fields(schema: Any, path: str = "") -> Iterator[str]:
    """Yield the path of every property under *schema* that has no description."""
    if not isinstance(schema, dict):
        return
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, prop in properties.items():
            field = f"{path}.{name}" if path else name
            description = prop.get("description") if isinstance(prop, dict) else None
            if not (isinstance(description, str) and description.strip()):
                yield field
            yield from _undescribed_fields(prop, field)
    for key in ("anyOf", "oneOf", "allOf"):
        for member in schema.get(key) or []:
            yield from _undescribed_fields(member, path)
    yield from _undescribed_fields(schema.get("items"), f"{path}[]")


@pytest.mark.asyncio
async def test_every_meta_tool_is_served() -> None:
    """The rules below cover every meta-tool; none is skipped by being absent."""
    assert set(await _served_tools()) == set(META_TOOLS)


@pytest.mark.parametrize("name", META_TOOLS)
@pytest.mark.asyncio
async def test_no_file_paths(name: str) -> None:
    tool = (await _served_tools())[name]
    found = _FILE_PATH.findall(_served_text(tool))
    assert not found, f"{name} names a file path a model cannot open: {found}"


@pytest.mark.parametrize("name", META_TOOLS)
@pytest.mark.asyncio
async def test_no_internal_names(name: str) -> None:
    tool = (await _served_tools())[name]
    found = _INTERNAL_NAMES.findall(_served_text(tool))
    assert not found, f"{name} names internals a model cannot act on: {found}"


@pytest.mark.parametrize("name", META_TOOLS)
@pytest.mark.asyncio
async def test_description_has_at_least_two_sentences(name: str) -> None:
    tool = (await _served_tools())[name]
    prose = _CODE_SPAN.sub("", tool.description or "")
    sentences = len(_SENTENCE_END.findall(prose))
    assert sentences >= 2, f"{name}'s description has {sentences} sentence(s); say what it does and when to use it"


@pytest.mark.parametrize("name", META_TOOLS)
@pytest.mark.asyncio
async def test_every_parameter_is_described(name: str) -> None:
    tool = (await _served_tools())[name]
    missing = list(_undescribed_fields(tool.input_schema))
    assert not missing, f"{name} serves parameters without a description: {missing}"

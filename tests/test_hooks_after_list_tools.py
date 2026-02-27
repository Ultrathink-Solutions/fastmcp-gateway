"""Tests for the after_list_tools hook phase: HookRunner + meta-tool integration."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from fastmcp import Client, FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.hooks import HookRunner, ListToolsContext
from fastmcp_gateway.meta_tools import register_meta_tools
from fastmcp_gateway.registry import ToolEntry, ToolRegistry


def _make_tool(name: str, domain: str = "test", group: str = "general") -> ToolEntry:
    return ToolEntry(
        name=name,
        domain=domain,
        group=group,
        description=f"Tool {name}",
        input_schema={"type": "object", "properties": {}},
        upstream_url=f"http://{domain}:8080/mcp",
    )


# ---------------------------------------------------------------------------
# HookRunner.run_after_list_tools (unit tests)
# ---------------------------------------------------------------------------


class TestRunAfterListTools:
    async def test_no_hooks_returns_original(self) -> None:
        runner = HookRunner()
        tools = [_make_tool("a"), _make_tool("b")]
        ctx = ListToolsContext(domain=None, headers={})
        result = await runner.run_after_list_tools(tools, ctx)
        assert result == tools

    async def test_single_hook_filters(self) -> None:
        class DropB:
            async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
                return [t for t in tools if t.name != "b"]

        runner = HookRunner([DropB()])
        tools = [_make_tool("a"), _make_tool("b"), _make_tool("c")]
        ctx = ListToolsContext(domain=None, headers={})
        result = await runner.run_after_list_tools(tools, ctx)
        assert [t.name for t in result] == ["a", "c"]

    async def test_pipeline_chains_hooks(self) -> None:
        """Each hook receives the previous hook's output."""

        class DropA:
            async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
                return [t for t in tools if t.name != "a"]

        class DropB:
            async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
                return [t for t in tools if t.name != "b"]

        runner = HookRunner([DropA(), DropB()])
        tools = [_make_tool("a"), _make_tool("b"), _make_tool("c")]
        ctx = ListToolsContext(domain=None, headers={})
        result = await runner.run_after_list_tools(tools, ctx)
        assert [t.name for t in result] == ["c"]

    async def test_hook_without_method_skipped(self) -> None:
        """Hooks that don't implement after_list_tools are silently skipped."""

        class NoListHook:
            async def before_execute(self, context: Any) -> None:
                pass

        runner = HookRunner([NoListHook()])
        tools = [_make_tool("a"), _make_tool("b")]
        ctx = ListToolsContext(domain=None, headers={})
        result = await runner.run_after_list_tools(tools, ctx)
        assert [t.name for t in result] == ["a", "b"]

    async def test_context_carries_domain_and_user(self) -> None:
        """Hook receives the correct domain and user from context."""
        captured: list[ListToolsContext] = []

        class CapturingHook:
            async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
                captured.append(context)
                return tools

        runner = HookRunner([CapturingHook()])
        tools = [_make_tool("a")]
        ctx = ListToolsContext(domain="malloy", headers={"authorization": "Bearer x"}, user={"sub": "user@test.com"})
        await runner.run_after_list_tools(tools, ctx)

        assert len(captured) == 1
        assert captured[0].domain == "malloy"
        assert captured[0].user == {"sub": "user@test.com"}
        assert captured[0].headers["authorization"] == "Bearer x"

    async def test_does_not_mutate_input(self) -> None:
        """The runner copies the input list — hooks get a fresh list."""

        class ReplaceAll:
            async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
                return []

        runner = HookRunner([ReplaceAll()])
        original = [_make_tool("a"), _make_tool("b")]
        ctx = ListToolsContext(domain=None, headers={})
        result = await runner.run_after_list_tools(original, ctx)
        assert result == []
        assert len(original) == 2  # original list untouched


# ---------------------------------------------------------------------------
# ListToolsContext
# ---------------------------------------------------------------------------


class TestListToolsContext:
    def test_defaults(self) -> None:
        ctx = ListToolsContext(domain=None, headers={})
        assert ctx.user is None
        assert ctx.domain is None

    def test_with_user(self) -> None:
        ctx = ListToolsContext(domain="malloy", headers={}, user={"sub": "nick"})
        assert ctx.user == {"sub": "nick"}
        assert ctx.domain == "malloy"


# ---------------------------------------------------------------------------
# discover_tools integration: hooks filter tool list
# ---------------------------------------------------------------------------


def _make_registry_with_malloy() -> ToolRegistry:
    """Create a registry with apollo (2 tools) and malloy (2 tools)."""
    registry = ToolRegistry()
    registry.set_domain_description("apollo", "Apollo CRM")
    registry.set_domain_description("malloy", "Malloy analytics")
    for name, domain, group in [
        ("apollo_people_search", "apollo", "people"),
        ("apollo_org_search", "apollo", "organizations"),
        ("malloy_executeQuery", "malloy", "general"),
        ("malloy_projectList", "malloy", "general"),
    ]:
        registry.register_tool(
            ToolEntry(
                name=name,
                domain=domain,
                group=group,
                description=f"Tool {name}",
                input_schema={"type": "object", "properties": {}},
                upstream_url=f"http://{domain}:8080/mcp",
            )
        )
    return registry


class _HideMalloyExecuteQuery:
    """Hook that filters out malloy_executeQuery (simulates SpiceDB denial)."""

    async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
        return [t for t in tools if t.name != "malloy_executeQuery"]


async def _call_discover(mcp: FastMCP, **kwargs: str | None) -> dict:
    """Call discover_tools via in-process client and parse JSON."""
    async with Client(mcp) as client:
        result = await client.call_tool("discover_tools", {k: v for k, v in kwargs.items() if v is not None})
    if result.data is not None:
        text = str(result.data)
    else:
        content_block = result.content[0]
        text = content_block.text  # type: ignore[union-attr]
    return json.loads(text)


async def _call_get_schema(mcp: FastMCP, tool_name: str) -> dict:
    """Call get_tool_schema via in-process client and parse JSON."""
    async with Client(mcp) as client:
        result = await client.call_tool("get_tool_schema", {"tool_name": tool_name})
    if result.data is not None:
        text = str(result.data)
    else:
        content_block = result.content[0]
        text = content_block.text  # type: ignore[union-attr]
    return json.loads(text)


def _make_mcp_with_hook(hook: Any | None = None) -> FastMCP:
    """Build a FastMCP server with the test registry and optional hook."""
    registry = _make_registry_with_malloy()
    hooks = [hook] if hook else None
    hook_runner = HookRunner(hooks)
    mcp = FastMCP("test-gateway")
    with patch("fastmcp_gateway.client_manager.Client"):
        manager = UpstreamManager(
            {"apollo": "http://apollo:8080/mcp", "malloy": "http://malloy:8080/mcp"},
            registry,
        )
    register_meta_tools(mcp, registry, manager, hook_runner)
    return mcp


class TestDiscoverToolsWithFilter:
    """discover_tools uses after_list_tools to filter results."""

    async def test_no_hook_returns_all(self) -> None:
        mcp = _make_mcp_with_hook()
        data = await _call_discover(mcp)
        assert data["total_tools"] == 4

    async def test_mode1_domain_summary_reflects_filter(self) -> None:
        """Mode 1 (no args): domain summary shows filtered counts."""
        mcp = _make_mcp_with_hook(_HideMalloyExecuteQuery())
        data = await _call_discover(mcp)

        assert data["total_tools"] == 3  # 4 - 1 filtered
        malloy = next(d for d in data["domains"] if d["name"] == "malloy")
        assert malloy["tool_count"] == 1  # only malloy_projectList remains
        apollo = next(d for d in data["domains"] if d["name"] == "apollo")
        assert apollo["tool_count"] == 2  # unchanged

    async def test_mode2_domain_tools_filtered(self) -> None:
        """Mode 2 (domain): filtered tools excluded."""
        mcp = _make_mcp_with_hook(_HideMalloyExecuteQuery())
        data = await _call_discover(mcp, domain="malloy")

        names = {t["name"] for t in data["tools"]}
        assert "malloy_executeQuery" not in names
        assert "malloy_projectList" in names

    async def test_mode4_search_results_filtered(self) -> None:
        """Mode 4 (search): filtered tools excluded from search results."""
        mcp = _make_mcp_with_hook(_HideMalloyExecuteQuery())
        data = await _call_discover(mcp, query="malloy")

        names = {r["name"] for r in data["results"]}
        assert "malloy_executeQuery" not in names
        assert "malloy_projectList" in names

    async def test_non_matching_domain_unaffected(self) -> None:
        """Tools in other domains pass through unfiltered."""
        mcp = _make_mcp_with_hook(_HideMalloyExecuteQuery())
        data = await _call_discover(mcp, domain="apollo")

        assert len(data["tools"]) == 2

    async def test_domain_hidden_when_all_tools_filtered(self) -> None:
        """When all tools in a domain are filtered, the domain disappears from summary."""

        class HideAllMalloy:
            async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
                return [t for t in tools if t.domain != "malloy"]

        mcp = _make_mcp_with_hook(HideAllMalloy())
        data = await _call_discover(mcp)

        domain_names = {d["name"] for d in data["domains"]}
        assert "malloy" not in domain_names
        assert "apollo" in domain_names
        assert data["total_tools"] == 2


class TestGetToolSchemaWithFilter:
    """get_tool_schema respects after_list_tools filtering."""

    async def test_visible_tool_returns_schema(self) -> None:
        mcp = _make_mcp_with_hook(_HideMalloyExecuteQuery())
        data = await _call_get_schema(mcp, "malloy_projectList")

        assert data["name"] == "malloy_projectList"
        assert "parameters" in data

    async def test_hidden_tool_returns_not_found(self) -> None:
        mcp = _make_mcp_with_hook(_HideMalloyExecuteQuery())
        data = await _call_get_schema(mcp, "malloy_executeQuery")

        assert data["code"] == "tool_not_found"

    async def test_no_hook_returns_all_schemas(self) -> None:
        mcp = _make_mcp_with_hook()
        data = await _call_get_schema(mcp, "malloy_executeQuery")

        assert data["name"] == "malloy_executeQuery"
        assert "parameters" in data

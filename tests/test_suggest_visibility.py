"""Tests for tool-name suggestion visibility filtering.

The did-you-mean path in ``get_tool_schema`` and ``execute_tool`` must run
fuzzy-match suggestions through the same hook-level filter that
``tools/list`` applies. Without that, a caller with narrow scopes can
probe the full registry by sending garbage tool names and reading the
suggestion list in the error response.

These tests guard both call sites and include a structural assertion
that the suggestion surface stays a subset of the ``discover_tools``
output for the same caller — catching any future drift where the
suggestion path reconstructs its own parallel filter.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.hooks import HookRunner, ListToolsContext
from fastmcp_gateway.meta_tools import register_meta_tools
from fastmcp_gateway.registry import ToolEntry, ToolRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_registry() -> ToolRegistry:
    """Registry with a ``foo`` domain and a ``bar`` domain — both share a
    suffix pattern so fuzzy matching against ``foo_zzz`` will pick up
    tools from both domains unless the filter intervenes.
    """
    registry = ToolRegistry()
    registry.set_domain_description("foo", "Foo domain")
    registry.set_domain_description("bar", "Bar domain")
    for name, domain, group in [
        ("foo_search", "foo", "general"),
        ("foo_enrich", "foo", "general"),
        ("foo_list", "foo", "general"),
        ("bar_search", "bar", "general"),
        ("bar_list", "bar", "general"),
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


class _HideFooDomain:
    """Hook that filters out every tool in the ``foo`` domain."""

    async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
        return [t for t in tools if t.domain != "foo"]


class _HideEverything:
    """Hook that filters out all tools — simulates a caller with no access."""

    async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
        return []


class _PassThrough:
    """Hook that implements ``after_list_tools`` but does not filter."""

    async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
        return tools


def _build_mcp(hook: Any | None = None) -> FastMCP:
    """Build a FastMCP server with the test registry and optional hook."""
    registry = _make_registry()
    hooks = [hook] if hook is not None else None
    hook_runner = HookRunner(hooks)
    mcp = FastMCP("test-gateway")
    with patch("fastmcp_gateway.client_manager.Client"):
        manager = UpstreamManager(
            {
                "foo": "http://foo:8080/mcp",
                "bar": "http://bar:8080/mcp",
            },
            registry,
        )
    register_meta_tools(mcp, registry, manager, hook_runner)
    return mcp


async def _call_get_schema(mcp: FastMCP, tool_name: str) -> dict[str, Any]:
    async with Client(mcp) as client:
        result = await client.call_tool("get_tool_schema", {"tool_name": tool_name})
    if result.data is not None:
        text = str(result.data)
    else:
        content_block = result.content[0]
        text = content_block.text  # type: ignore[union-attr]
    return json.loads(text)


async def _call_execute(mcp: FastMCP, tool_name: str) -> dict[str, Any]:
    async with Client(mcp) as client:
        result = await client.call_tool("execute_tool", {"tool_name": tool_name, "arguments": {}})
    if result.data is not None:
        text = str(result.data)
    else:
        content_block = result.content[0]
        text = content_block.text  # type: ignore[union-attr]
    return json.loads(text)


async def _call_discover(mcp: FastMCP, **kwargs: Any) -> dict[str, Any]:
    async with Client(mcp) as client:
        result = await client.call_tool("discover_tools", {k: v for k, v in kwargs.items() if v is not None})
    if result.data is not None:
        text = str(result.data)
    else:
        content_block = result.content[0]
        text = content_block.text  # type: ignore[union-attr]
    return json.loads(text)


# ---------------------------------------------------------------------------
# 1. Broad-visibility caller sees full suggestion surface
# ---------------------------------------------------------------------------


class TestBroadVisibility:
    @pytest.mark.asyncio
    async def test_passthrough_hook_yields_full_suggestions(self) -> None:
        """A hook that filters nothing returns the full registry's suggestions."""
        mcp = _build_mcp(_PassThrough())

        # ``foo_zzz`` fuzzy-matches every ``foo_*`` tool.
        data = await _call_get_schema(mcp, "foo_zzz")

        assert data["code"] == "tool_not_found"
        suggestions = data["details"]["suggestions"]
        # Every foo_* tool is visible to a pass-through caller.
        assert any(s.startswith("foo_") for s in suggestions)


# ---------------------------------------------------------------------------
# 2. Narrow-visibility caller — suggestions are filtered
# ---------------------------------------------------------------------------


class TestNarrowVisibility:
    @pytest.mark.asyncio
    async def test_hidden_domain_does_not_leak_via_suggestions(self) -> None:
        """A garbage ``foo_*`` lookup must not leak foo_* names when foo is hidden."""
        mcp = _build_mcp(_HideFooDomain())

        data = await _call_get_schema(mcp, "foo_xyz")

        assert data["code"] == "tool_not_found"
        suggestions = data["details"]["suggestions"]
        # No foo_* tool may appear — the caller cannot see the foo domain.
        assert not any(s.startswith("foo_") for s in suggestions), (
            f"foo_* name leaked in suggestions for hidden domain: {suggestions}"
        )


# ---------------------------------------------------------------------------
# 3. No-hooks deployment — fallback preserves current behavior
# ---------------------------------------------------------------------------


class TestNoHooksDeployment:
    @pytest.mark.asyncio
    async def test_no_hooks_returns_full_suggestions(self) -> None:
        """With no hooks registered, all tools remain candidates (intentional fallback)."""
        mcp = _build_mcp()  # HookRunner() with no hooks

        data = await _call_get_schema(mcp, "foo_zzz")

        assert data["code"] == "tool_not_found"
        suggestions = data["details"]["suggestions"]
        # Fallback: the full registry is the suggestion surface.
        assert any(s.startswith("foo_") for s in suggestions)


# ---------------------------------------------------------------------------
# 4. Filter eliminates all candidates → discover_tools hint
# ---------------------------------------------------------------------------


class TestEmptyFilterResult:
    @pytest.mark.asyncio
    async def test_all_filtered_gives_discover_tools_hint(self) -> None:
        """When the filter eliminates every candidate, the error points to discover_tools."""
        mcp = _build_mcp(_HideEverything())

        data = await _call_get_schema(mcp, "foo_search")

        assert data["code"] == "tool_not_found"
        # No "Did you mean" phrasing — that would imply we had suggestions.
        assert "Did you mean" not in data["error"]
        assert "discover_tools" in data["error"]
        assert data["details"]["suggestions"] == []


# ---------------------------------------------------------------------------
# 5. Structural reuse — suggestions are a subset of discover_tools output
# ---------------------------------------------------------------------------


class TestStructuralReuse:
    @pytest.mark.asyncio
    async def test_suggestions_subset_of_discover_for_same_caller(self) -> None:
        """The suggestion surface must never exceed the discover_tools surface.

        This guards against a future change that reconstructs a parallel
        filter on the suggestion path and drifts from the canonical
        ``_filter_tools`` closure.
        """
        hook = _HideFooDomain()
        mcp = _build_mcp(hook)

        # Fetch the full visible-tool set via discover_tools (mode 1).
        discover_data = await _call_discover(mcp)
        visible_names: set[str] = set()
        for d in discover_data["domains"]:
            dom_data = await _call_discover(mcp, domain=d["name"])
            visible_names.update(t["name"] for t in dom_data["tools"])

        # Now trigger a suggestion. The garbage name should still fuzzy-match
        # tools in the visible set (bar_search shares the "search" segment).
        suggest_data = await _call_get_schema(mcp, "foo_search")

        suggestions = set(suggest_data["details"]["suggestions"])
        assert suggestions.issubset(visible_names), (
            f"suggestions {suggestions - visible_names} leaked outside the visible set {visible_names}"
        )


# ---------------------------------------------------------------------------
# 6. execute_tool path also filters suggestion candidates
# ---------------------------------------------------------------------------


class TestExecuteToolPath:
    @pytest.mark.asyncio
    async def test_execute_tool_suggestions_filtered(self) -> None:
        """The ``execute_tool`` not-found branch must use the same visibility filter."""
        mcp = _build_mcp(_HideFooDomain())

        data = await _call_execute(mcp, "foo_zzz")

        assert data["code"] == "tool_not_found"
        suggestions = data["details"]["suggestions"]
        assert not any(s.startswith("foo_") for s in suggestions), (
            f"foo_* name leaked in execute_tool suggestions: {suggestions}"
        )

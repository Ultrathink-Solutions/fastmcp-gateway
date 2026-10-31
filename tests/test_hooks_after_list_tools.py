"""Tests for the after_list_tools hook phase: HookRunner + meta-tool integration."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.hooks import HookRunner, ListToolsContext
from fastmcp_gateway.meta_tools import register_meta_tools
from fastmcp_gateway.registry import ToolEntry, ToolRegistry
from tests.conftest import result_payload


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
    """Hook that filters out malloy_executeQuery (simulates an authorization denial)."""

    async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
        return [t for t in tools if t.name != "malloy_executeQuery"]


async def _call_discover(mcp: FastMCP, **kwargs: str | None) -> dict:
    """Call discover_tools via in-process client and parse JSON."""
    async with Client(mcp) as client:
        result = await client.call_tool("discover_tools", {k: v for k, v in kwargs.items() if v is not None})
    return result_payload(result)


async def _call_get_schema(mcp: FastMCP, tool_name: str) -> dict:
    """Call get_tool_schema via in-process client and parse JSON."""
    async with Client(mcp) as client:
        result = await client.call_tool("get_tool_schema", {"tool_name": tool_name})
    return result_payload(result)


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
        data = await _call_discover(mcp, domain="malloy", format="schema")

        names = {t["name"] for t in data["tools"]}
        assert "malloy_executeQuery" not in names
        assert "malloy_projectList" in names

    async def test_mode4_search_results_filtered(self) -> None:
        """Mode 4 (search): filtered tools excluded from search results."""
        mcp = _make_mcp_with_hook(_HideMalloyExecuteQuery())
        data = await _call_discover(mcp, query="malloy", format="schema")

        names = {r["name"] for r in data["results"]}
        assert "malloy_executeQuery" not in names
        assert "malloy_projectList" in names

    async def test_non_matching_domain_unaffected(self) -> None:
        """Tools in other domains pass through unfiltered."""
        mcp = _make_mcp_with_hook(_HideMalloyExecuteQuery())
        data = await _call_discover(mcp, domain="apollo", format="schema")

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


class _HideAllMalloy:
    """Hook that hides every malloy tool, as an authorization hook does for a domain the caller may not use."""

    async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
        return [t for t in tools if t.domain != "malloy"]


class _HideApolloOrganizations:
    """Hook that hides every tool of apollo's ``organizations`` group."""

    async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
        return [t for t in tools if (t.domain, t.group) != ("apollo", "organizations")]


@pytest.mark.asyncio
class TestDiscoverToolsHiddenDomains:
    """A domain or group whose tools are all filtered out does not exist for the caller.

    The no-argument summary already leaves such a domain out; naming it
    explicitly must not answer an empty listing instead.
    """

    @pytest.mark.parametrize("fmt", ["signatures", "schema"])
    async def test_a_fully_filtered_domain_is_not_found(self, fmt: str) -> None:
        mcp = _make_mcp_with_hook(_HideAllMalloy())
        data = await _call_discover(mcp, domain="malloy", format=fmt)

        assert data["code"] == "domain_not_found"
        assert data["details"]["available_domains"] == ["apollo"]

    async def test_a_group_of_a_fully_filtered_domain_is_not_found(self) -> None:
        mcp = _make_mcp_with_hook(_HideAllMalloy())
        data = await _call_discover(mcp, domain="malloy", group="general")

        assert data["code"] == "domain_not_found"

    async def test_an_unknown_domain_lists_only_the_domains_the_caller_sees(self) -> None:
        mcp = _make_mcp_with_hook(_HideAllMalloy())
        data = await _call_discover(mcp, domain="nope")

        assert data["code"] == "domain_not_found"
        assert data["details"]["available_domains"] == ["apollo"]
        assert "malloy" not in data["error"]

    async def test_a_fully_filtered_group_is_not_found(self) -> None:
        mcp = _make_mcp_with_hook(_HideApolloOrganizations())
        data = await _call_discover(mcp, domain="apollo", group="organizations")

        assert data["code"] == "group_not_found"
        assert data["details"]["available_groups"] == ["people"]

    async def test_a_partly_filtered_domain_still_lists_what_is_visible(self) -> None:
        mcp = _make_mcp_with_hook(_HideApolloOrganizations())
        data = await _call_discover(mcp, domain="apollo", format="schema")

        assert [t["name"] for t in data["tools"]] == ["apollo_people_search"]

    @pytest.mark.parametrize("requested", ["malloy", "nope"])
    async def test_available_domains_are_judged_as_a_domain_lookup_sees_them(self, requested: str) -> None:
        """A domain hidden only when listed by name is not offered: the lookup would reject it."""
        mcp = _make_mcp_with_hook(_HideMalloyWhenListedByName())
        data = await _call_discover(mcp, domain=requested)

        assert data["code"] == "domain_not_found"
        assert data["details"]["available_domains"] == ["apollo"]


class _HideMalloyWhenListedByName:
    """Hook that hides malloy's tools only when the listing names malloy (``ListToolsContext.domain``)."""

    async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
        return [] if context.domain == "malloy" else tools


class _CountingAuthHideMalloy:
    """Hook that counts ``on_authenticate`` calls, answers a new user each time, and records each pass's user."""

    def __init__(self) -> None:
        self.users_issued: list[dict[str, int]] = []
        self.users_seen: list[Any] = []

    async def on_authenticate(self, headers: dict[str, str]) -> dict[str, int]:
        user = {"call": len(self.users_issued) + 1}
        self.users_issued.append(user)
        return user

    async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
        self.users_seen.append(context.user)
        return [] if context.domain == "malloy" else tools


@pytest.mark.asyncio
class TestDiscoverToolsAuthenticatesOnce:
    """``on_authenticate`` runs once per ``discover_tools`` request, and every filtering pass reuses that user."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({}, id="summary"),
            pytest.param({"query": "search"}, id="search"),
            pytest.param({"domain": "apollo"}, id="domain"),
            pytest.param({"domain": "apollo", "group": "people"}, id="group"),
            pytest.param({"domain": "apollo", "group": "nope"}, id="group-not-found"),
            pytest.param({"domain": "malloy"}, id="hidden-domain-not-found"),
            pytest.param({"domain": "nope"}, id="unknown-domain-not-found"),
        ],
    )
    async def test_one_authentication_per_request(self, kwargs: dict[str, str]) -> None:
        hook = _CountingAuthHideMalloy()
        mcp = _make_mcp_with_hook(hook)
        await _call_discover(mcp, format="schema", **kwargs)

        assert len(hook.users_issued) == 1
        assert hook.users_seen
        assert all(user is hook.users_issued[0] for user in hook.users_seen)


@pytest.mark.asyncio
class TestGetToolSchemaAuthenticatesOnce:
    """``on_authenticate`` runs once per ``get_tool_schema`` request, and every filtering pass reuses that user."""

    @pytest.mark.parametrize(
        "tool_name",
        [
            pytest.param("apollo_people_search", id="visible-tool"),
            pytest.param("malloy_executeQuery", id="hidden-tool"),
            pytest.param("no_such_tool", id="unknown-tool"),
        ],
    )
    async def test_one_authentication_per_request(self, tool_name: str) -> None:
        hook = _CountingAuthHideMalloy()
        mcp = _make_mcp_with_hook(hook)
        await _call_get_schema(mcp, tool_name)

        assert len(hook.users_issued) == 1
        assert hook.users_seen
        assert all(user is hook.users_issued[0] for user in hook.users_seen)


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

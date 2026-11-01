"""``discover_tools(domain=..., format="schema")`` carries each tool's parameters.

Each listed tool's ``parameters`` must equal what ``get_tool_schema`` serves
for the same caller, so a client needs one call, not one per tool.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client, FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.hooks import HookRunner, ListToolsContext
from fastmcp_gateway.meta_tools import register_meta_tools
from fastmcp_gateway.registry import ToolEntry, ToolRegistry
from tests.conftest import result_payload

_NESTED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "filters": {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["open", "closed"]}},
            "required": ["status"],
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Page size"},
    },
    "required": ["filters"],
}


def _tool(name: str, domain: str, schema: dict[str, Any], *, group: str = "general") -> ToolEntry:
    return ToolEntry(
        name=name,
        domain=domain,
        group=group,
        description=f"{name} description",
        input_schema=schema,
        upstream_url=f"http://{domain}:8080/mcp",
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.set_domain_description("svc", "Service")
    registry.set_domain_description("other", "Other")
    registry.register_tool(_tool("svc_nested", "svc", _NESTED_SCHEMA))
    registry.register_tool(_tool("svc_empty", "svc", {"type": "object", "properties": {}}))
    registry.register_tool(_tool("svc_secret", "svc", {"type": "object", "properties": {"k": {"type": "string"}}}))
    # Same original name in two domains: both are renamed with a domain prefix.
    registry.register_tool(_tool("search", "svc", {"type": "object", "properties": {"svc_only": {"type": "string"}}}))
    registry.register_tool(
        _tool("search", "other", {"type": "object", "properties": {"other_only": {"type": "integer"}}})
    )
    registry.register_tool(_tool("svc_extra", "svc", {"type": "object", "properties": {}}))
    registry.register_tool(_tool("other_only", "other", {"type": "object", "properties": {}}))
    return registry


class _HideSecret:
    async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
        return [t for t in tools if t.name != "svc_secret"]


_REGISTRIES: dict[int, ToolRegistry] = {}


def _registry_of(mcp: FastMCP) -> ToolRegistry:
    return _REGISTRIES[id(mcp)]


class _MoveDuringBeforeExecute:
    """Re-registers the tool under another domain while the call's hooks run."""

    registry: ToolRegistry

    async def before_execute(self, context: Any) -> None:
        self.registry.clear_domain("svc")
        self.registry.register_tool(_tool("svc_extra", "other", {"type": "object", "properties": {}}))


def _server(hook: Any | None = None) -> FastMCP:
    return _gateway(hook)[0]


def _gateway(hook: Any | None = None) -> tuple[FastMCP, UpstreamManager]:
    registry = _registry()
    mcp = FastMCP("test-gateway")
    with patch("fastmcp_gateway.client_manager.Client"):
        manager = UpstreamManager({"svc": "http://svc:8080/mcp", "other": "http://other:8080/mcp"}, registry)
    register_meta_tools(mcp, registry, manager, HookRunner([hook] if hook is not None else None))
    _REGISTRIES[id(mcp)] = registry
    return mcp, manager


async def _listing(mcp: FastMCP) -> dict[str, Any]:
    async with Client(mcp) as client:
        return result_payload(await client.call_tool("discover_tools", {"domain": "svc", "format": "schema"}))


async def _schema(mcp: FastMCP, name: str) -> dict[str, Any]:
    async with Client(mcp) as client:
        return result_payload(await client.call_tool("get_tool_schema", {"tool_name": name}))


class TestSchemaListingParameters:
    @pytest.mark.asyncio
    async def test_parameters_equal_get_tool_schema_for_every_tool(self) -> None:
        mcp = _server()
        listing = await _listing(mcp)

        assert listing["tools"]
        for entry in listing["tools"]:
            assert entry["parameters"] == (await _schema(mcp, entry["name"]))["parameters"]

    @pytest.mark.asyncio
    async def test_nested_schema_is_carried_whole(self) -> None:
        listing = await _listing(_server())
        by_name = {t["name"]: t for t in listing["tools"]}

        assert by_name["svc_nested"]["parameters"] == _NESTED_SCHEMA

    @pytest.mark.asyncio
    async def test_hidden_tool_absent_from_both_answers(self) -> None:
        mcp = _server(_HideSecret())
        listing = await _listing(mcp)

        assert "svc_secret" not in {t["name"] for t in listing["tools"]}
        assert (await _schema(mcp, "svc_secret"))["code"] == "tool_not_found"
        names = {t["name"] for t in listing["tools"]}
        assert names == {"svc_nested", "svc_empty", "svc_search", "svc_extra"}
        assert all("k" not in t["parameters"].get("properties", {}) for t in listing["tools"])

    @pytest.mark.asyncio
    async def test_collision_renamed_tool_carries_its_own_parameters(self) -> None:
        mcp = _server()
        listing = await _listing(mcp)
        by_name = {t["name"]: t for t in listing["tools"]}

        assert "search" not in by_name
        renamed = by_name["svc_search"]["parameters"]
        assert list(renamed["properties"]) == ["svc_only"]
        assert renamed == (await _schema(mcp, "svc_search"))["parameters"]
        assert (await _schema(mcp, "other_search"))["parameters"]["properties"] == {"other_only": {"type": "integer"}}

    @pytest.mark.asyncio
    async def test_other_fields_unchanged(self) -> None:
        listing = await _listing(_server())

        assert set(listing) == {"domain", "tools"}
        for entry in listing["tools"]:
            assert set(entry) == {"name", "original_name", "group", "description", "parameters"}

    @pytest.mark.asyncio
    async def test_original_name_is_the_upstreams_own_name(self) -> None:
        listing = await _listing(_server())
        by_name = {t["name"]: t for t in listing["tools"]}

        assert by_name["svc_search"]["original_name"] == "search"
        assert by_name["svc_nested"]["original_name"] == "svc_nested"
        assert by_name["svc_extra"]["original_name"] == "svc_extra"


def _ok() -> MagicMock:
    block = MagicMock()
    block.text = "ok"
    result = MagicMock()
    result.content = [block]
    result.is_error = False
    result.structured_content = None
    return result


async def _execute(mcp: FastMCP, manager: UpstreamManager, **params: Any) -> dict[str, Any]:
    manager.execute_tool = AsyncMock(return_value=_ok())  # type: ignore[method-assign]
    async with Client(mcp) as client:
        result = await client.call_tool("execute_tool", params)
    return json.loads(result.content[0].text)  # type: ignore[union-attr]


class TestDomainPinnedExecute:
    @pytest.mark.asyncio
    async def test_original_name_reaches_the_renamed_tool(self) -> None:
        mcp, manager = _gateway()

        for domain in ("svc", "other"):
            data = await _execute(mcp, manager, tool_name="search", domain=domain)
            assert data["tool"] == f"{domain}_search"
            assert manager.execute_tool.call_args.args[0] == f"{domain}_search"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_registry_name_resolves_under_its_domain(self) -> None:
        mcp, manager = _gateway()

        data = await _execute(mcp, manager, tool_name="svc_search", domain="svc")

        assert data["tool"] == "svc_search"

    @pytest.mark.asyncio
    async def test_natively_prefixed_tool_is_not_reachable_by_its_suffix(self) -> None:
        mcp, manager = _gateway()

        data = await _execute(mcp, manager, tool_name="extra", domain="svc")

        assert data["code"] == "tool_not_found"
        assert data["details"]["domain"] == "svc"
        manager.execute_tool.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_name_registered_under_another_domain_is_refused(self) -> None:
        mcp, manager = _gateway()

        for name in ("other_only", "other_search"):
            data = await _execute(mcp, manager, tool_name=name, domain="svc")
            assert data["code"] == "tool_not_found"
            assert data["details"]["domain"] == "svc"
        manager.execute_tool.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_hidden_tool_is_refused_under_the_pin(self) -> None:
        mcp, manager = _gateway(_HideSecret())

        data = await _execute(mcp, manager, tool_name="svc_secret", domain="svc")

        assert data["code"] == "tool_not_found"
        manager.execute_tool.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_without_domain_resolution_is_unchanged(self) -> None:
        mcp, manager = _gateway()

        data = await _execute(mcp, manager, tool_name="svc_extra")
        assert data["tool"] == "svc_extra"

        missing = await _execute(mcp, manager, tool_name="extra")
        assert missing["code"] == "tool_not_found"
        assert "domain" not in missing["details"]


class _CountingAuth:
    def __init__(self) -> None:
        self.calls = 0

    async def on_authenticate(self, headers: dict[str, str]) -> Any:
        self.calls += 1
        return {"id": "u"}


class _RewriteSchemas:
    """Returns modified entries instead of a filtered list."""

    async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
        return [
            t.model_copy(update={"input_schema": {"type": "object", "properties": {"rewritten": {"type": "string"}}}})
            for t in tools
        ]


class TestFixRound1:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("domain", ["svc", None])
    async def test_execute_authenticates_once(self, domain: str | None) -> None:
        auth = _CountingAuth()
        mcp, manager = _gateway(auth)
        params: dict[str, Any] = {"tool_name": "svc_extra"}
        if domain:
            params["domain"] = domain

        data = await _execute(mcp, manager, **params)

        assert data["tool"] == "svc_extra"
        assert auth.calls == 1

    @pytest.mark.asyncio
    async def test_listing_ignores_schemas_a_hook_rewrites(self) -> None:
        mcp = _server(_RewriteSchemas())
        listing = await _listing(mcp)

        assert listing["tools"]
        for entry in listing["tools"]:
            served = (await _schema(mcp, entry["name"]))["parameters"]
            assert entry["parameters"] == served
            assert "rewritten" not in entry["parameters"].get("properties", {})

    @pytest.mark.asyncio
    async def test_pin_is_checked_again_at_call_time(self) -> None:
        mcp, manager = _gateway()
        first = await _execute(mcp, manager, tool_name="svc_extra", domain="svc")
        assert first["tool"] == "svc_extra"

        registry = _registry_of(mcp)
        registry.clear_domain("svc")
        registry.register_tool(_tool("svc_extra", "other", {"type": "object", "properties": {}}))
        manager.execute_tool.reset_mock()  # type: ignore[attr-defined]
        second = await _execute(mcp, manager, tool_name="svc_extra", domain="svc")

        assert second["code"] == "tool_not_found"
        manager.execute_tool.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_pin_is_rechecked_after_hooks_run(self) -> None:
        mcp, manager = _gateway(_MoveDuringBeforeExecute())
        _MoveDuringBeforeExecute.registry = _registry_of(mcp)

        data = await _execute(mcp, manager, tool_name="svc_extra", domain="svc")

        assert data["code"] == "tool_not_found"
        manager.execute_tool.assert_not_called()  # type: ignore[attr-defined]

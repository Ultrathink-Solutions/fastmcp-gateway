"""Shared test fixtures and result readers for fastmcp-gateway tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from fastmcp_gateway.registry import ToolEntry, ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager


def result_text(result: Any) -> str:
    """The first text block of a tool result."""
    return cast("str", result.content[0].text)


def result_payload(result: Any) -> dict[str, Any]:
    """A tool result's JSON payload, preferring MCP's structured channel.

    A meta-tool that answers with JSON publishes the same object on both
    channels, and reading ``structuredContent`` is what an MCP-aware
    caller does -- so that is what these assertions exercise. A tool that
    answers with prose leaves it unset, hence the text-block fallback.

    Deliberately not ``str(result.data)``. That idiom only ever yielded
    JSON because a ``-> str`` meta-tool got its return wrapped as
    ``{"result": <the JSON string>}`` and ``.data`` unwrapped it back to
    the string. Against a tool publishing a genuine object, ``str()``
    yields a Python repr -- single-quoted, and not JSON at all.
    """
    if isinstance(result.structured_content, dict):
        return result.structured_content
    return cast("dict[str, Any]", json.loads(result_text(result)))


def upstream_status_error(status_code: int, www_authenticate: str | None = None) -> httpx.HTTPStatusError:
    """The exception httpx raises for a non-2xx upstream response, with an optional challenge."""
    headers = {} if www_authenticate is None else {"WWW-Authenticate": www_authenticate}
    return httpx.HTTPStatusError(
        f"upstream answered {status_code}",
        request=httpx.Request("POST", "http://upstream:8080/mcp"),
        response=httpx.Response(status_code, headers=headers),
    )


@dataclass
class FakeTool:
    """Mimics ``mcp.types.Tool`` for tests that don't need the MCP dependency."""

    name: str
    description: str | None = None
    inputSchema: dict[str, Any] | None = None


@pytest.fixture
def deep_schema_tool() -> Callable[..., FakeTool]:
    """Factory for a tool whose inputSchema nests to a chosen depth.

    ``wrappers`` object layers around a scalar leaf. Each wrapper costs
    two counted levels (the object dict plus its ``properties`` dict)
    and the leaf's scalar value costs one, so the measured nesting depth
    is ``2 * wrappers + 1``. Callers pick *wrappers* to sit deliberately
    above or below the cap under test.
    """

    def _build(wrappers: int, *, name: str = "svc_deep") -> FakeTool:
        node: dict[str, Any] = {"type": "string"}
        for index in range(wrappers):
            node = {"type": "object", "properties": {f"level_{index}": node}}
        return FakeTool(name=name, description="Deeply nested tool", inputSchema=node)

    return _build


@pytest.fixture
def patch_upstream_client() -> Callable[..., AbstractContextManager[Any]]:
    """Factory patching the upstream ``Client`` so it lists the given tools.

    Returns a context manager, so the patch stays active for exactly the
    block that constructs the gateway/manager and runs the populate.
    """

    def _patch(*tools: FakeTool) -> AbstractContextManager[Any]:
        def make_client(url: str) -> MagicMock:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            client.list_tools = AsyncMock(return_value=list(tools))
            return client

        return patch("fastmcp_gateway.client_manager.Client", side_effect=make_client)

    return _patch


@pytest.fixture
def registry() -> ToolRegistry:
    """A fresh, empty tool registry."""
    return ToolRegistry()


@pytest.fixture
def upstreams() -> dict[str, str]:
    """Two sample upstream domains keyed by name -> URL."""
    return {
        "acme": "http://acme-mcp:8080/mcp",
        "widgets": "http://widgets-mcp:8080/mcp",
    }


@pytest.fixture
def empty_registry() -> ToolRegistry:
    """An empty tool registry."""
    return ToolRegistry()


@pytest.fixture
def populated_registry() -> ToolRegistry:
    """A registry with sample tools across multiple domains."""
    registry = ToolRegistry()

    # Apollo domain
    registry.set_domain_description("apollo", "Apollo.io CRM and sales intelligence")
    for name, group, desc in [
        ("apollo_people_search", "people", "Search for people by name, title, company, or other criteria"),
        ("apollo_people_enrich", "people", "Enrich a person record with full contact and company data"),
        ("apollo_org_search", "organizations", "Search for organizations by name, industry, or size"),
        ("apollo_org_enrich", "organizations", "Enrich an organization with firmographic data"),
    ]:
        registry.register_tool(
            ToolEntry(
                name=name,
                domain="apollo",
                group=group,
                description=desc,
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                upstream_url="http://apollo-mcp:8080/mcp",
            )
        )

    # HubSpot domain
    registry.set_domain_description("hubspot", "HubSpot CRM and marketing")
    for name, group, desc in [
        ("hubspot_contacts_search", "contacts", "Search HubSpot contacts by name, email, or properties"),
        ("hubspot_contacts_create", "contacts", "Create a new contact in HubSpot"),
        ("hubspot_deals_list", "deals", "List deals with optional filters"),
    ]:
        registry.register_tool(
            ToolEntry(
                name=name,
                domain="hubspot",
                group=group,
                description=desc,
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
                upstream_url="http://hubspot-mcp:8080/mcp",
            )
        )

    return registry

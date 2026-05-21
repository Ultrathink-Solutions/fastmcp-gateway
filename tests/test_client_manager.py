"""Tests for upstream client management."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.registry import ToolRegistry


@dataclass
class FakeTool:
    """Mimics mcp.types.Tool for testing without MCP dependency."""

    name: str
    description: str | None = None
    inputSchema: dict[str, Any] | None = None


def _make_fake_tools(domain: str) -> list[FakeTool]:
    """Create a realistic set of fake MCP tools for a domain."""
    return [
        FakeTool(
            name=f"{domain}_users_list",
            description="List users",
            inputSchema={"type": "object", "properties": {"limit": {"type": "integer"}}},
        ),
        FakeTool(
            name=f"{domain}_users_create",
            description="Create a user",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
        FakeTool(
            name=f"{domain}_billing_invoice",
            description="Generate invoice",
            inputSchema={"type": "object"},
        ),
    ]


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


@pytest.fixture
def upstreams() -> dict[str, str]:
    return {
        "acme": "http://acme-mcp:8080/mcp",
        "widgets": "http://widgets-mcp:8080/mcp",
    }


# ---------------------------------------------------------------------------
# populate_all
# ---------------------------------------------------------------------------


class TestPopulateAll:
    @pytest.mark.asyncio
    async def test_populates_all_upstreams(self, registry: ToolRegistry, upstreams: dict[str, str]) -> None:
        manager = UpstreamManager(upstreams, registry)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        def make_client(url: str) -> MagicMock:
            # Resolve domain by URL — UpstreamManager now constructs two
            # Client instances per domain (registry + execution) so any
            # call-order indexing breaks. Mapping by URL is order-independent.
            domain = next(d for d, u in upstreams.items() if u == url)
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            client.list_tools = AsyncMock(return_value=_make_fake_tools(domain))
            return client

        with patch("fastmcp_gateway.client_manager.Client", side_effect=make_client):
            manager = UpstreamManager(upstreams, registry)
            results = await manager.populate_all()

        assert results == {"acme": 3, "widgets": 3}
        assert registry.tool_count == 6
        assert registry.has_domain("acme")
        assert registry.has_domain("widgets")

    @pytest.mark.asyncio
    async def test_graceful_degradation_on_failure(self, registry: ToolRegistry, upstreams: dict[str, str]) -> None:
        """One failing upstream should not prevent others from populating."""
        first_domain = next(iter(upstreams.keys()))

        def make_client(url: str) -> MagicMock:
            domain = next(d for d, u in upstreams.items() if u == url)
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            if domain == first_domain:
                # First-domain upstream fails (selected by domain name
                # rather than call order — UpstreamManager now constructs
                # two Client instances per domain, so call-order indexing
                # would mis-target which domain fails).
                client.list_tools = AsyncMock(side_effect=ConnectionError("unreachable"))
            else:
                client.list_tools = AsyncMock(return_value=_make_fake_tools(domain))
            return client

        with patch("fastmcp_gateway.client_manager.Client", side_effect=make_client):
            manager = UpstreamManager(upstreams, registry)
            results = await manager.populate_all()

        # Only the second upstream succeeded
        assert len(results) == 1
        assert registry.tool_count == 3


# ---------------------------------------------------------------------------
# populate_domain (single)
# ---------------------------------------------------------------------------


class TestPopulateDomain:
    @pytest.mark.asyncio
    async def test_populates_single_domain(self, registry: ToolRegistry) -> None:
        def make_client(url: str) -> MagicMock:
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            client.list_tools = AsyncMock(return_value=_make_fake_tools("svc"))
            return client

        with patch("fastmcp_gateway.client_manager.Client", side_effect=make_client):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, registry)
            count = await manager.populate_domain("svc")

        assert count == 3
        assert registry.has_domain("svc")

    @pytest.mark.asyncio
    async def test_unknown_domain_raises(self, registry: ToolRegistry) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, registry)
            with pytest.raises(KeyError):
                await manager.populate_domain("nonexistent")


# ---------------------------------------------------------------------------
# execute_tool
# ---------------------------------------------------------------------------


class TestExecuteTool:
    @pytest.mark.asyncio
    async def test_execute_routes_to_upstream(self, registry: ToolRegistry) -> None:
        """execute_tool creates a fresh client and calls the correct tool."""
        fake_result = MagicMock()
        fake_result.content = []
        fake_result.is_error = False

        fresh_client = AsyncMock()
        fresh_client.__aenter__ = AsyncMock(return_value=fresh_client)
        fresh_client.__aexit__ = AsyncMock(return_value=None)
        fresh_client.call_tool = AsyncMock(return_value=fake_result)

        base_client = MagicMock()
        base_client.new = MagicMock(return_value=fresh_client)

        def make_client(url: str) -> MagicMock:
            return base_client

        with patch("fastmcp_gateway.client_manager.Client", side_effect=make_client):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, registry)

        # Manually populate registry (bypassing client mocking for populate)
        registry.populate_domain(
            "svc",
            "http://svc:8080/mcp",
            [{"name": "svc_users_list", "inputSchema": {"type": "object"}}],
        )

        result = await manager.execute_tool("svc_users_list", {"limit": 10})

        assert result is fake_result
        base_client.new.assert_called_once()
        fresh_client.call_tool.assert_called_once_with(
            "svc_users_list",
            {"limit": 10},
            raise_on_error=False,
        )

    @pytest.mark.asyncio
    async def test_execute_collision_prefixed_uses_original_name(self, registry: ToolRegistry) -> None:
        """Collision-prefixed tools must dispatch with the original upstream name."""
        fake_result = MagicMock()
        fake_result.content = []
        fake_result.is_error = False

        fresh_client = AsyncMock()
        fresh_client.__aenter__ = AsyncMock(return_value=fresh_client)
        fresh_client.__aexit__ = AsyncMock(return_value=None)
        fresh_client.call_tool = AsyncMock(return_value=fake_result)

        base_client = MagicMock()
        base_client.new = MagicMock(return_value=fresh_client)

        def make_client(url: str) -> MagicMock:
            return base_client

        with patch("fastmcp_gateway.client_manager.Client", side_effect=make_client):
            manager = UpstreamManager(
                {
                    "snowflake": "http://snowflake:8080/mcp",
                    "axon": "http://axon:8080/mcp",
                },
                registry,
            )

        # Both domains register a tool named "get_server_info" — triggers collision
        registry.populate_domain(
            "snowflake",
            "http://snowflake:8080/mcp",
            [{"name": "get_server_info", "inputSchema": {"type": "object"}}],
        )
        registry.populate_domain(
            "axon",
            "http://axon:8080/mcp",
            [{"name": "get_server_info", "inputSchema": {"type": "object"}}],
        )

        # Registry should have prefixed names
        assert registry.lookup("snowflake_get_server_info") is not None
        assert registry.lookup("snowflake_get_server_info").original_name == "get_server_info"  # type: ignore[union-attr]

        result = await manager.execute_tool("snowflake_get_server_info")

        assert result is fake_result
        # Must call upstream with the ORIGINAL name, not the prefixed one
        fresh_client.call_tool.assert_called_once_with(
            "get_server_info",
            {},
            raise_on_error=False,
        )

    @pytest.mark.asyncio
    async def test_execute_unknown_tool_raises(self, registry: ToolRegistry) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, registry)
            with pytest.raises(KeyError, match="not found"):
                await manager.execute_tool("nonexistent_tool")

    @pytest.mark.asyncio
    async def test_execute_defaults_empty_arguments(self, registry: ToolRegistry) -> None:
        """When arguments is None, an empty dict is sent to call_tool."""
        fake_result = MagicMock()
        fresh_client = AsyncMock()
        fresh_client.__aenter__ = AsyncMock(return_value=fresh_client)
        fresh_client.__aexit__ = AsyncMock(return_value=None)
        fresh_client.call_tool = AsyncMock(return_value=fake_result)

        base_client = MagicMock()
        base_client.new = MagicMock(return_value=fresh_client)

        def make_client(url: str) -> MagicMock:
            return base_client

        with patch("fastmcp_gateway.client_manager.Client", side_effect=make_client):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, registry)

        registry.populate_domain(
            "svc", "http://svc:8080/mcp", [{"name": "svc_ping", "inputSchema": {"type": "object"}}]
        )

        await manager.execute_tool("svc_ping")

        fresh_client.call_tool.assert_called_once_with(
            "svc_ping",
            {},
            raise_on_error=False,
        )


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


class TestIntrospection:
    def test_domains(self) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            manager = UpstreamManager(
                {"beta": "http://b:8080/mcp", "alpha": "http://a:8080/mcp"},
                ToolRegistry(),
            )
        assert manager.domains == ["alpha", "beta"]

    def test_upstream_url(self) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, ToolRegistry())
        assert manager.upstream_url("svc") == "http://svc:8080/mcp"

    def test_upstream_url_unknown_raises(self) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, ToolRegistry())
        with pytest.raises(KeyError):
            manager.upstream_url("nonexistent")


# ---------------------------------------------------------------------------
# registry_auth_headers
# ---------------------------------------------------------------------------


class TestRegistryAuthHeaders:
    def test_headers_set_on_registry_clients(self) -> None:
        """Registry clients should have auth headers on their transport."""
        mock_client = MagicMock()
        mock_transport = MagicMock()
        mock_client.transport = mock_transport

        with patch("fastmcp_gateway.client_manager.Client", return_value=mock_client):
            UpstreamManager(
                {"svc": "http://svc:8080/mcp"},
                ToolRegistry(),
                registry_auth_headers={"Authorization": "Bearer test-token"},
            )

        assert mock_transport.headers == {"Authorization": "Bearer test-token"}

    def test_no_headers_when_none(self) -> None:
        """When no auth headers, transport should not be modified."""
        mock_client = MagicMock()
        mock_transport = MagicMock()
        mock_transport.headers = {}
        mock_client.transport = mock_transport

        with patch("fastmcp_gateway.client_manager.Client", return_value=mock_client):
            UpstreamManager({"svc": "http://svc:8080/mcp"}, ToolRegistry())

        # Headers should still be the default empty dict
        assert mock_transport.headers == {}


# ---------------------------------------------------------------------------
# upstream_headers (per-domain execution headers)
# ---------------------------------------------------------------------------


class TestUpstreamHeaders:
    @pytest.mark.asyncio
    async def test_domain_with_override_uses_new_with_headers(self, registry: ToolRegistry) -> None:
        """Domains with upstream_headers should use client.new() and merge headers."""
        fake_result = MagicMock()
        fresh_client = AsyncMock()
        fresh_client.__aenter__ = AsyncMock(return_value=fresh_client)
        fresh_client.__aexit__ = AsyncMock(return_value=None)
        fresh_client.call_tool = AsyncMock(return_value=fake_result)
        fresh_client.transport = MagicMock()
        fresh_client.transport.headers = {}

        base_client = MagicMock()
        base_client.new = MagicMock(return_value=fresh_client)

        with patch("fastmcp_gateway.client_manager.Client", return_value=base_client):
            manager = UpstreamManager(
                {"svc": "http://svc:8080/mcp"},
                registry,
                upstream_headers={"svc": {"Authorization": "Bearer domain-key"}},
            )

        registry.populate_domain(
            "svc", "http://svc:8080/mcp", [{"name": "svc_ping", "inputSchema": {"type": "object"}}]
        )

        result = await manager.execute_tool("svc_ping")

        assert result is fake_result
        # Should use client.new() uniformly, then apply headers
        base_client.new.assert_called_once()
        assert fresh_client.transport.headers == {"Authorization": "Bearer domain-key"}

    @pytest.mark.asyncio
    async def test_domain_without_override_uses_new(self, registry: ToolRegistry) -> None:
        """Domains without upstream_headers should use base_client.new()."""
        fake_result = MagicMock()
        fresh_client = AsyncMock()
        fresh_client.__aenter__ = AsyncMock(return_value=fresh_client)
        fresh_client.__aexit__ = AsyncMock(return_value=None)
        fresh_client.call_tool = AsyncMock(return_value=fake_result)

        base_client = MagicMock()
        base_client.new = MagicMock(return_value=fresh_client)

        with patch("fastmcp_gateway.client_manager.Client", return_value=base_client):
            manager = UpstreamManager(
                {"svc": "http://svc:8080/mcp"},
                registry,
                upstream_headers={"other_domain": {"Authorization": "Bearer other"}},
            )

        registry.populate_domain(
            "svc", "http://svc:8080/mcp", [{"name": "svc_ping", "inputSchema": {"type": "object"}}]
        )

        await manager.execute_tool("svc_ping")

        # Should use base_client.new() since "svc" has no override
        base_client.new.assert_called_once()


# ---------------------------------------------------------------------------
# discovery_url separation (registry vs execution clients)
# ---------------------------------------------------------------------------


def _make_dual_client_mock(domain: str) -> MagicMock:
    """Build a Client mock that supports both discovery and execution paths.

    The base mock answers ``list_tools`` (discovery side) and exposes a
    ``.new()`` that returns a fresh mock answering ``call_tool``
    (execution side). Lets a single ``side_effect`` factory serve every
    ``Client(url)`` construction the manager performs.
    """
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.list_tools = AsyncMock(return_value=_make_fake_tools(domain))
    client.transport = MagicMock()
    client.transport.headers = {}

    fresh = AsyncMock()
    fresh.__aenter__ = AsyncMock(return_value=fresh)
    fresh.__aexit__ = AsyncMock(return_value=None)
    fresh.call_tool = AsyncMock(return_value=MagicMock())
    fresh.transport = MagicMock()
    fresh.transport.headers = {}
    client.new = MagicMock(return_value=fresh)
    return client


class TestDiscoveryUrlSeparation:
    """The registry client targets ``discovery_url`` (e.g. ``/_introspect``)
    while the execution client always targets the canonical MCP URL.

    Verifies the new contract added to support unauth-discovery backends
    where tool registration hits a route that does not require a JWT.
    """

    @pytest.mark.asyncio
    async def test_discovery_serves_list_tools_execution_serves_call_tool(self, registry: ToolRegistry) -> None:
        """The discovery URL handles ``list_tools``; the canonical URL
        handles ``execute_tool``. The discovery client is never cloned
        for execution — a regression that fell back to it would dispatch
        ``tools/call`` to an endpoint that does not accept it.
        """
        exec_url = "http://widgets:8080/mcp"
        disc_url = "http://widgets:8080/_introspect"

        clients_by_url: dict[str, MagicMock] = {}

        def make_client(url: str) -> MagicMock:
            client = _make_dual_client_mock("widgets")
            clients_by_url[url] = client
            return client

        with patch("fastmcp_gateway.client_manager.Client", side_effect=make_client):
            manager = UpstreamManager({}, registry)
            await manager.add_upstream("widgets", exec_url, discovery_url=disc_url)
            await manager.execute_tool("widgets_users_list")

        # Discovery URL served list_tools; never cloned for execution.
        assert clients_by_url[disc_url].list_tools.await_count == 1
        assert clients_by_url[disc_url].new.call_count == 0, (
            "discovery client must not be cloned for execution — would dispatch tools/call to the discovery endpoint"
        )

        # Canonical URL was cloned via .new() and the fresh client served call_tool.
        assert clients_by_url[exec_url].new.call_count == 1
        exec_fresh = clients_by_url[exec_url].new.return_value
        exec_fresh.call_tool.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_omitting_discovery_url_routes_both_paths_to_url(self, registry: ToolRegistry) -> None:
        """Backward-compat: when ``discovery_url`` is omitted, both
        ``list_tools`` and ``execute_tool`` target the single canonical URL.

        Two ``Client`` instances are still constructed (registry +
        execution), but both point at the same URL.
        """
        exec_url = "http://legacy:8080/mcp"
        constructed: list[MagicMock] = []

        def make_client(url: str) -> MagicMock:
            assert url == exec_url, f"unexpected URL {url!r}"
            client = _make_dual_client_mock("legacy")
            constructed.append(client)
            return client

        with patch("fastmcp_gateway.client_manager.Client", side_effect=make_client):
            manager = UpstreamManager({}, registry)
            await manager.add_upstream("legacy", exec_url)
            await manager.execute_tool("legacy_users_list")

        # Two distinct Client instances — order-independent assertions so
        # a future refactor of construction order in __init__/add_upstream
        # doesn't break this test.
        assert len(constructed) == 2
        total_list_tools = sum(c.list_tools.await_count for c in constructed)
        total_clones = sum(c.new.call_count for c in constructed)
        assert total_list_tools == 1, "list_tools should fire exactly once on the registry client"
        assert total_clones == 1, "execute_tool should clone exactly one base client"

    def test_registry_auth_headers_do_not_bleed_into_execution_client(self) -> None:
        """Regression guard for the ``Client.new()`` header-bleed bug.

        FastMCP's ``Client.new()`` preserves transport headers; if
        ``registry_auth_headers`` were applied to the execution base
        client, every per-request clone would inherit them and leak
        the service credential onto user-driven ``execute_tool`` calls.
        Auth headers must stay on the registry client only.
        """
        constructed: list[MagicMock] = []

        def make_client(url: str) -> MagicMock:
            client = MagicMock()
            client.transport = MagicMock()
            client.transport.headers = {}
            constructed.append(client)
            return client

        with patch("fastmcp_gateway.client_manager.Client", side_effect=make_client):
            UpstreamManager(
                {"svc": "http://svc:8080/mcp"},
                ToolRegistry(),
                registry_auth_headers={"Authorization": "Bearer registry-only"},
            )

        # __init__ constructs (registry, execution) in that order.
        reg_client, exec_client = constructed
        assert reg_client.transport.headers == {"Authorization": "Bearer registry-only"}
        assert exec_client.transport.headers == {}, (
            "execution base client must remain headerless; Client.new() preserves headers, "
            "so any value here would bleed into every execute_tool() clone"
        )

    @pytest.mark.asyncio
    async def test_add_upstream_upsert_closes_previous_client_pair(self, registry: ToolRegistry) -> None:
        """Re-registering an existing domain must close the prior
        registry/execution client pair before replacing them.

        The two-client model doubles the leak surface — without this
        close path, every URL/header refresh would orphan two live
        Client sessions per domain.
        """
        # Each Client constructed is an AsyncMock whose __aexit__ we
        # can audit. We tag the first pair (the ones built during the
        # initial add_upstream) so the upsert assertions can check
        # exactly those instances were closed.
        constructed: list[AsyncMock] = []

        def make_client(url: str) -> AsyncMock:
            client = _make_dual_client_mock("widgets")
            constructed.append(client)
            return client

        with patch("fastmcp_gateway.client_manager.Client", side_effect=make_client):
            manager = UpstreamManager({}, registry)
            await manager.add_upstream("widgets", "http://widgets-v1:8080/mcp")

            # First add_upstream constructed (registry, execution) for widgets.
            first_reg, first_exec = constructed
            # The registry client was opened by _populate_domain — its
            # __aexit__ has already fired once from list_tools.
            initial_reg_exits = first_reg.__aexit__.await_count

            # Re-register: should close both prior clients before
            # constructing the replacement pair.
            await manager.add_upstream("widgets", "http://widgets-v2:8080/mcp")

        # Four total: two from the first add, two from the upsert.
        assert len(constructed) == 4
        # Both prior clients were entered and exited once for the close
        # path. The registry client picks up an additional pair (it was
        # opened a second time by the upsert's own _populate_domain
        # before the new replacement entered the registry slot, but that
        # is the *replacement* registry client, not the original).
        assert first_exec.__aenter__.await_count == 1
        assert first_exec.__aexit__.await_count == 1
        # The first registry client's exit count grew by 1 above the
        # initial baseline (the close-on-upsert pass).
        assert first_reg.__aexit__.await_count == initial_reg_exits + 1

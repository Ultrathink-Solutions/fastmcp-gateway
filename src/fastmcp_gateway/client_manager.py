"""Upstream client management with dual connection strategy.

Persistent clients are used for registry population (list_tools at
startup/refresh).  Fresh-per-request clients are used for execute_tool
so that each call inherits the current user's HTTP headers via
FastMCP's ``get_http_headers()`` ContextVar.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastmcp import Client

if TYPE_CHECKING:
    from fastmcp.client.client import CallToolResult

    from fastmcp_gateway.registry import ToolRegistry

logger = logging.getLogger(__name__)


class UpstreamManager:
    """Manages connections to upstream MCP servers.

    Parameters
    ----------
    upstreams:
        Mapping of domain name to upstream MCP server URL.
    registry:
        The shared tool registry to populate.
    """

    def __init__(self, upstreams: dict[str, str], registry: ToolRegistry) -> None:
        self._upstreams = upstreams
        self._registry = registry

        # Persistent clients for registry operations (no user context).
        self._registry_clients: dict[str, Client] = {domain: Client(url) for domain, url in upstreams.items()}

    # ------------------------------------------------------------------
    # Registry population
    # ------------------------------------------------------------------

    async def populate_all(self) -> dict[str, int]:
        """Discover tools from every upstream and populate the registry.

        Returns a mapping of domain -> tool count for each successfully
        populated upstream.  Unreachable upstreams are logged and skipped
        (graceful degradation per FR-7).
        """
        results: dict[str, int] = {}
        for domain, client in self._registry_clients.items():
            try:
                count = await self._populate_domain(domain, client)
                results[domain] = count
                logger.info("Populated %d tools from upstream '%s'", count, domain)
            except Exception:
                logger.exception("Failed to populate upstream '%s' — skipping", domain)
        return results

    async def populate_domain(self, domain: str) -> int:
        """Re-populate a single domain (used for targeted refresh).

        Raises ``KeyError`` if *domain* is not a configured upstream.
        """
        client = self._registry_clients[domain]
        return await self._populate_domain(domain, client)

    async def _populate_domain(self, domain: str, client: Client) -> int:
        """Connect to *client*, list its tools, and register them."""
        async with client:
            mcp_tools = await client.list_tools()

        raw_tools: list[dict[str, Any]] = [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.inputSchema,
            }
            for t in mcp_tools
        ]

        return self._registry.populate_domain(
            domain=domain,
            upstream_url=self._upstreams[domain],
            tools=raw_tools,
        )

    # ------------------------------------------------------------------
    # Tool execution (fresh client per request)
    # ------------------------------------------------------------------

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Execute a tool on its upstream server.

        Creates a **fresh** ``Client`` for each call so that FastMCP's
        ``get_http_headers()`` ContextVar resolves the *current* user's
        HTTP headers, ensuring per-request auth isolation.

        Raises ``KeyError`` if *tool_name* is not in the registry.
        """
        entry = self._registry.lookup(tool_name)
        if entry is None:
            msg = f"Tool '{tool_name}' not found in registry"
            raise KeyError(msg)

        # Resolve the domain's base client and create a fresh session.
        base_client = self._registry_clients[entry.domain]
        fresh_client = base_client.new()

        async with fresh_client:
            return await fresh_client.call_tool(
                tool_name,
                arguments or {},
                raise_on_error=False,
            )

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    @property
    def domains(self) -> list[str]:
        """Configured upstream domain names."""
        return sorted(self._upstreams.keys())

    def upstream_url(self, domain: str) -> str:
        """Return the URL for a domain.  Raises ``KeyError`` if unknown."""
        return self._upstreams[domain]

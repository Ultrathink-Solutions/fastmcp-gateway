"""Upstream client management with dual connection strategy.

Persistent clients are used for registry population (list_tools at
startup/refresh).  Fresh-per-request clients are used for execute_tool
so that each call inherits the current user's HTTP headers via
FastMCP's ``get_http_headers()`` ContextVar.

Optionally, per-domain headers can be configured via *upstream_headers*
to override the ContextVar passthrough for specific domains (e.g. when
an upstream requires a different auth token).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastmcp import Client

if TYPE_CHECKING:
    from fastmcp.client.client import CallToolResult

    from fastmcp_gateway.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _set_transport_headers(client: Client, headers: dict[str, str]) -> None:
    """Merge explicit headers into an HTTP-based client transport.

    Preserves any existing default headers on the transport and overlays
    the provided *headers* on top.  FastMCP transports created from URLs
    (SSE / Streamable-HTTP) have a ``headers`` attribute.  Stdio transports
    do not, but the gateway only creates HTTP clients, so this is safe in
    practice.
    """
    transport: Any = client.transport
    existing = dict(transport.headers) if transport.headers else {}
    transport.headers = {**existing, **headers}


class UpstreamManager:
    """Manages connections to upstream MCP servers.

    Parameters
    ----------
    upstreams:
        Mapping of domain name to upstream MCP server URL.
    registry:
        The shared tool registry to populate.
    registry_auth_headers:
        Headers to send when populating the registry at startup.
        Registry clients run outside any HTTP request context, so
        ``get_http_headers()`` is empty.  Pass auth headers here
        for upstreams that require authentication on ``list_tools``.
    upstream_headers:
        Per-domain headers for tool execution.  When ``execute_tool``
        routes to a domain listed here, these headers are used instead
        of the default request-passthrough behaviour.
    """

    def __init__(
        self,
        upstreams: dict[str, str],
        registry: ToolRegistry,
        *,
        registry_auth_headers: dict[str, str] | None = None,
        upstream_headers: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._upstreams = upstreams
        self._registry = registry
        self._upstream_headers = upstream_headers or {}

        # Persistent clients for registry operations (no user context).
        self._registry_clients: dict[str, Client] = {}
        for domain, url in upstreams.items():
            client = Client(url)
            if registry_auth_headers:
                _set_transport_headers(client, registry_auth_headers)
            self._registry_clients[domain] = client

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
            upstream_url=str(self._upstreams[domain]),
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

        Creates a **fresh** ``Client`` for each call.  For domains with
        explicit *upstream_headers*, those headers are applied directly.
        For all other domains, FastMCP's ``get_http_headers()`` ContextVar
        resolves the *current* user's HTTP headers (request passthrough).

        Raises ``KeyError`` if *tool_name* is not in the registry.
        """
        entry = self._registry.lookup(tool_name)
        if entry is None:
            msg = f"Tool '{tool_name}' not found in registry"
            raise KeyError(msg)

        fresh_client = self._make_execution_client(entry.domain)

        async with fresh_client:
            return await fresh_client.call_tool(
                tool_name,
                arguments or {},
                raise_on_error=False,
            )

    def _make_execution_client(self, domain: str) -> Client:
        """Create a fresh client for tool execution via ``client.new()``.

        Always uses ``client.new()`` to create a shallow copy of the
        registry client, ensuring consistent transport configuration.
        If *domain* has explicit upstream headers, those are merged onto
        the new client's transport.  Otherwise the client inherits
        request-passthrough behaviour via the ContextVar.
        """
        client = self._registry_clients[domain].new()
        if domain in self._upstream_headers:
            _set_transport_headers(client, self._upstream_headers[domain])
        return client

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

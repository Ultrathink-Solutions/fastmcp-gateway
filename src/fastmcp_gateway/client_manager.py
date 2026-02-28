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
from fastmcp.server.dependencies import get_http_headers
from opentelemetry import trace

if TYPE_CHECKING:
    from fastmcp.client.client import CallToolResult

    from fastmcp_gateway.registry import RegistryDiff, ToolRegistry

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("fastmcp_gateway.client_manager")


def get_user_headers(*, include_all: bool = False) -> dict[str, str]:
    """Return the HTTP headers from the current incoming MCP request.

    This is a convenience wrapper around FastMCP's request context.
    Call this from within a tool handler or middleware to access the
    originating user's headers (e.g. ``Authorization``).

    Returns an empty dict when called outside an HTTP request context
    (e.g. during startup population).

    Parameters
    ----------
    include_all:
        If ``True``, return all headers including hop-by-hop headers
        that are normally stripped (``content-length``, ``host``, etc.).
    """
    return get_http_headers(include_all=include_all)


def _set_transport_headers(client: Client, headers: dict[str, str]) -> None:
    """Merge explicit headers into an HTTP-based client transport.

    Preserves any existing default headers on the transport and overlays
    the provided *headers* on top.  FastMCP transports created from URLs
    (SSE / Streamable-HTTP) have a ``headers`` attribute.  In-process
    and stdio transports do not — this function is a no-op for those.
    """
    transport: Any = client.transport
    if not hasattr(transport, "headers"):
        logger.debug("Transport %s does not support headers — skipping", type(transport).__name__)
        return
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
        with _tracer.start_as_current_span("gateway.populate_all") as span:
            results: dict[str, int] = {}
            for domain, client in self._registry_clients.items():
                try:
                    diff = await self._populate_domain(domain, client)
                    results[domain] = diff.tool_count
                    logger.info("Populated %d tools from upstream '%s'", diff.tool_count, domain)
                except Exception:
                    logger.exception("Failed to populate upstream '%s' — skipping", domain)
            span.set_attribute("gateway.domain_count", len(results))
            span.set_attribute("gateway.total_tools", sum(results.values()))
            return results

    async def populate_domain(self, domain: str) -> int:
        """Re-populate a single domain (used for targeted refresh).

        Raises ``KeyError`` if *domain* is not a configured upstream.
        """
        client = self._registry_clients[domain]
        diff = await self._populate_domain(domain, client)
        return diff.tool_count

    async def _populate_domain(self, domain: str, client: Client) -> RegistryDiff:
        """Connect to *client*, list its tools, and register them."""
        with _tracer.start_as_current_span("gateway.populate_domain") as span:
            span.set_attribute("gateway.domain", domain)

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

            diff = self._registry.populate_domain(
                domain=domain,
                upstream_url=str(self._upstreams[domain]),
                tools=raw_tools,
            )
            span.set_attribute("gateway.tool_count", diff.tool_count)
            return diff

    # ------------------------------------------------------------------
    # Registry refresh
    # ------------------------------------------------------------------

    async def refresh_all(self) -> list[RegistryDiff]:
        """Re-populate all domains and return per-domain diffs.

        Unlike :meth:`populate_all`, this returns :class:`RegistryDiff`
        objects so callers can inspect what changed.
        """
        with _tracer.start_as_current_span("gateway.refresh_all") as span:
            diffs: list[RegistryDiff] = []
            for domain, client in self._registry_clients.items():
                try:
                    diff = await self._populate_domain(domain, client)
                    diffs.append(diff)
                except Exception:
                    logger.exception("Failed to refresh upstream '%s' — skipping", domain)
            span.set_attribute("gateway.domain_count", len(diffs))
            return diffs

    async def refresh_domain(self, domain: str) -> RegistryDiff:
        """Re-populate a single domain and return the diff.

        Raises ``KeyError`` if *domain* is not a configured upstream.
        """
        client = self._registry_clients[domain]
        return await self._populate_domain(domain, client)

    # ------------------------------------------------------------------
    # Tool execution (fresh client per request)
    # ------------------------------------------------------------------

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> CallToolResult:
        """Execute a tool on its upstream server.

        Creates a **fresh** ``Client`` for each call.  For domains with
        explicit *upstream_headers*, those headers are applied directly.
        For all other domains, FastMCP's ``get_http_headers()`` ContextVar
        resolves the *current* user's HTTP headers (request passthrough).

        Parameters
        ----------
        tool_name:
            Name of the tool to execute.
        arguments:
            Tool arguments.
        extra_headers:
            Additional headers from hooks, merged with highest priority.

        Raises ``KeyError`` if *tool_name* is not in the registry.
        """
        with _tracer.start_as_current_span("gateway.upstream.execute") as span:
            span.set_attribute("gateway.tool_name", tool_name)

            entry = self._registry.lookup(tool_name)
            if entry is None:
                msg = f"Tool '{tool_name}' not found in registry"
                raise KeyError(msg)

            span.set_attribute("gateway.domain", entry.domain)
            fresh_client = self._make_execution_client(entry.domain, extra_headers=extra_headers)

            # Use the original (upstream) tool name when dispatching.
            # Collision-prefixed names (e.g., "snowflake_get_server_info")
            # exist only in the gateway registry — the upstream server only
            # knows the original name ("get_server_info").
            upstream_name = entry.original_name or entry.name

            async with fresh_client:
                return await fresh_client.call_tool(
                    upstream_name,
                    arguments or {},
                    raise_on_error=False,
                )

    def _make_execution_client(
        self,
        domain: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> Client:
        """Create a fresh client for tool execution via ``client.new()``.

        Always uses ``client.new()`` to create a shallow copy of the
        registry client, ensuring consistent transport configuration.
        If *domain* has explicit upstream headers, those are merged onto
        the new client's transport.  Otherwise the client inherits
        request-passthrough behaviour via the ContextVar.

        Header merge priority (highest wins):
        1. *extra_headers* from hooks (e.g. ``X-User-Subject``)
        2. Static ``upstream_headers[domain]`` (e.g. per-domain API keys)
        3. Request passthrough via ContextVar (incoming request headers)
        """
        client = self._registry_clients[domain].new()
        merged: dict[str, str] = {}
        if domain in self._upstream_headers:
            merged.update(self._upstream_headers[domain])
        if extra_headers:
            merged.update(extra_headers)
        if merged:
            _set_transport_headers(client, merged)
        return client

    # ------------------------------------------------------------------
    # Dynamic upstream management
    # ------------------------------------------------------------------

    async def add_upstream(
        self,
        domain: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        registry_auth_headers: dict[str, str] | None = None,
    ) -> RegistryDiff:
        """Add a new upstream at runtime and populate its tools.

        If the domain already exists, its URL and headers are updated and
        the registry is re-populated (idempotent upsert).

        Parameters
        ----------
        domain:
            Domain name for the upstream (e.g. ``"apollo"``).
        url:
            MCP server URL (e.g. ``"http://apollo:8080/mcp"``).
        headers:
            Per-domain headers for tool execution (e.g. auth tokens).
        registry_auth_headers:
            Headers for registry population (list_tools calls).
        """
        with _tracer.start_as_current_span("gateway.add_upstream") as span:
            span.set_attribute("gateway.domain", domain)
            span.set_attribute("gateway.url", url)

            self._upstreams[domain] = url
            if headers:
                self._upstream_headers[domain] = headers
            else:
                self._upstream_headers.pop(domain, None)

            client = Client(url)
            if registry_auth_headers:
                _set_transport_headers(client, registry_auth_headers)
            self._registry_clients[domain] = client

            diff = await self._populate_domain(domain, client)
            logger.info(
                "Registered upstream '%s' (%s): %d tools",
                domain,
                url,
                diff.tool_count,
            )
            return diff

    async def remove_upstream(self, domain: str) -> list[str]:
        """Remove an upstream and all its tools from the registry.

        Closes the persistent registry client to free resources.
        Returns the list of tool names that were removed.
        Raises ``KeyError`` if the domain is not registered.
        """
        with _tracer.start_as_current_span("gateway.remove_upstream") as span:
            span.set_attribute("gateway.domain", domain)

            if domain not in self._upstreams:
                msg = f"Domain '{domain}' is not a registered upstream"
                raise KeyError(msg)

            # Collect tool names before clearing.
            removed = [t.name for t in self._registry.get_tools_by_domain(domain)]

            self._registry.clear_domain(domain)
            self._upstreams.pop(domain, None)
            client = self._registry_clients.pop(domain, None)
            self._upstream_headers.pop(domain, None)

            # Close the client to release connection resources.
            if client is not None:
                try:
                    async with client:
                        pass  # __aexit__ closes the session
                except Exception:
                    logger.debug("Error closing client for domain '%s'", domain, exc_info=True)

            span.set_attribute("gateway.tools_removed", len(removed))
            logger.info("Deregistered upstream '%s': removed %d tools", domain, len(removed))
            return removed

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

    def list_upstreams(self) -> dict[str, str]:
        """Return a snapshot of all registered upstreams (domain -> URL).

        Values are coerced to strings so that in-process FastMCP server
        references are safely serializable.
        """
        return {domain: str(url) for domain, url in self._upstreams.items()}

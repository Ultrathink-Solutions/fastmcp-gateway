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

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from fastmcp import Client
from fastmcp.server.dependencies import get_http_headers
from opentelemetry import trace

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastmcp.client.client import CallToolResult

    from fastmcp_gateway.access_policy import AccessPolicy
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
    registry_token_provider:
        Optional zero-argument callable returning a bearer token string,
        invoked immediately before every registry ``list_tools`` fetch
        (startup population, refresh, and dynamic registration). Use this
        when the registry credential is short-lived and rotates: the
        registry clients are persistent, so a token passed once via
        *registry_auth_headers* is captured at construction and then
        expires mid-life. The returned value is sent as ``Authorization:
        Bearer <token>`` and takes precedence over *registry_auth_headers*
        on the fetch path. Keep it non-blocking (return a cached token and
        refresh out of band) — it runs on the async populate/refresh path.
    upstream_headers:
        Per-domain headers for tool execution.  When ``execute_tool``
        routes to a domain listed here, these headers are used instead
        of the default request-passthrough behaviour.
    policy:
        Optional :class:`~fastmcp_gateway.access_policy.AccessPolicy` applied
        to every registry population (startup, refresh, and dynamic registration).
        Tools rejected by the policy never enter the registry.
    sanitizer_trusted_domains:
        Optional set of domain names for which the description
        sanitizer will skip the **injection-pattern scan** only. All
        other sanitation (Unicode normalization, control-character
        stripping, length cap) and schema validation still apply. Use
        this for legitimate prompt-processing tools whose descriptions
        intentionally contain denylist tokens. Passed as an explicit
        Python-code kwarg (no env-var form) so a deployment mistake
        can't silently weaken sanitation.
    trusted_output_tools:
        Optional iterable of ``fnmatch`` glob patterns naming tools
        whose result text the gateway-level output guard will skip
        scrubbing. Patterns are applied during
        :meth:`ToolRegistry.populate_domain` — any registered tool
        whose name matches is flagged
        :attr:`ToolEntry.raw_output_trusted` so the output guard (if
        enabled) bypasses it on each invocation. Complementary to the
        upstream-declared ``annotations: {"x-raw-output-trusted":
        true}`` custom extension.
    """

    def __init__(
        self,
        upstreams: dict[str, Any],
        registry: ToolRegistry,
        *,
        registry_auth_headers: dict[str, str] | None = None,
        registry_token_provider: Callable[[], str] | None = None,
        upstream_headers: dict[str, dict[str, str]] | None = None,
        policy: AccessPolicy | None = None,
        sanitizer_trusted_domains: set[str] | None = None,
        trusted_output_tools: set[str] | None = None,
        discovery_urls: dict[str, str] | None = None,
    ) -> None:
        self._upstreams = upstreams
        self._registry = registry
        self._upstream_headers = upstream_headers or {}
        self._registry_auth_headers = registry_auth_headers
        self._registry_token_provider = registry_token_provider
        # Per-domain locks serialize the (token refresh + header set + list_tools)
        # sequence on each persistent registry client, so concurrent same-domain
        # populate/refresh calls can't overwrite Authorization between the header
        # update and the fetch that must use it.
        self._registry_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._policy = policy
        # Defensive copy so later mutation by the caller doesn't silently
        # change sanitation behaviour mid-flight.
        self._sanitizer_trusted_domains: set[str] = (
            set(sanitizer_trusted_domains) if sanitizer_trusted_domains else set()
        )
        # Stored as a sorted list (not a set) so iteration order is
        # deterministic — matters when a future extension logs which
        # pattern triggered trust.
        self._trusted_output_tool_patterns: list[str] = sorted(trusted_output_tools) if trusted_output_tools else []

        # Per-domain client pairs. The registry client targets the
        # discovery URL (e.g. an unauth `/_introspect` endpoint that
        # speaks the MCP discovery slice without requiring a JWT); the
        # execution client targets the canonical MCP URL where user
        # tool calls are dispatched and the inbound JWT must be
        # validated. When no discovery URL is supplied for a domain
        # the two clients connect to the same URL, preserving the
        # pre-discovery-url-split behaviour exactly.
        #
        # registry_auth_headers are applied to the registry client only.
        # The execution base client must stay header-free: `Client.new()`
        # preserves transport headers, so any header set here would bleed
        # into every per-request clone made by `_make_execution_client`
        # and leak service credentials onto user-driven `execute_tool`
        # calls.
        self._registry_clients: dict[str, Client] = {}
        self._execution_clients: dict[str, Client] = {}
        for domain, url in upstreams.items():
            disc_url: str = (discovery_urls or {}).get(domain) or url
            reg_client = Client(disc_url)
            if registry_auth_headers:
                _set_transport_headers(reg_client, registry_auth_headers)
            self._registry_clients[domain] = reg_client

            self._execution_clients[domain] = Client(url)

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

    async def _populate_domain(
        self,
        domain: str,
        client: Client,
        *,
        expected_digest: str | None = None,
        upstream_url: str | None = None,
    ) -> RegistryDiff:
        """Connect to *client*, list its tools, and register them.

        *expected_digest*, when set, is threaded through to
        :meth:`ToolRegistry.populate_domain` to explicitly acknowledge a
        schema contract change.  See that method's docstring for the
        full integrity-gate semantics.

        *upstream_url*, when provided, overrides the URL passed into
        the registry.  :meth:`add_upstream` uses this to probe a
        candidate URL without first staging it into
        ``self._upstreams`` — that lets a failed probe leave the
        manager's per-domain dicts untouched instead of relying on
        rollback after a partial commit.  Refresh paths leave this
        ``None`` so the URL already committed in
        ``self._upstreams[domain]`` is used.
        """
        with _tracer.start_as_current_span("gateway.populate_domain") as span:
            span.set_attribute("gateway.domain", domain)

            # Refresh the registry client's auth header from the token provider
            # (if configured) so a short-lived, rotating credential is current on
            # every fetch. The registry client is persistent, so a token applied
            # once at construction via registry_auth_headers would expire mid-life;
            # minting per fetch keeps populate/refresh/add_upstream authenticated.
            #
            # The provider call, header mutation, and fetch run under a per-domain
            # lock: the persistent client's headers are shared, so a concurrent
            # same-domain call must not overwrite Authorization between this fetch's
            # header update and its list_tools. Per-domain (not global) so distinct
            # domains still populate/refresh concurrently.
            async with self._registry_locks[domain]:
                if self._registry_token_provider is not None:
                    token = self._registry_token_provider()
                    _set_transport_headers(client, {"Authorization": f"Bearer {token}"})

                async with client:
                    mcp_tools = await client.list_tools()

            raw_tools: list[dict[str, Any]] = []
            for t in mcp_tools:
                entry: dict[str, Any] = {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": t.inputSchema,
                }
                # MCP tool ``annotations`` (optional) carries custom
                # extensions like ``x-raw-output-trusted``. Prefer
                # the attribute — the MCP Python SDK exposes annotations
                # as a Pydantic model whose ``model_dump`` we want,
                # but we fall back to whatever shape the SDK hands us
                # so a newer SDK version that returns a plain dict
                # still works.
                annotations = getattr(t, "annotations", None)
                if annotations is not None:
                    if hasattr(annotations, "model_dump"):
                        entry["annotations"] = annotations.model_dump(exclude_none=True)
                    elif isinstance(annotations, dict):
                        entry["annotations"] = annotations
                raw_tools.append(entry)

            effective_url = str(upstream_url) if upstream_url is not None else str(self._upstreams[domain])
            diff = self._registry.populate_domain(
                domain=domain,
                upstream_url=effective_url,
                tools=raw_tools,
                policy=self._policy,
                expected_digest=expected_digest,
                trusted_domains=self._sanitizer_trusted_domains,
                trusted_output_tool_patterns=self._trusted_output_tool_patterns,
            )
            span.set_attribute("gateway.tool_count", diff.tool_count)
            if diff.refused:
                span.set_attribute("gateway.schema_refused", True)
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

    async def refresh_domain(
        self,
        domain: str,
        *,
        expected_digest: str | None = None,
    ) -> RegistryDiff:
        """Re-populate a single domain and return the diff.

        When *expected_digest* is set, the refresh explicitly
        acknowledges a schema contract change.  If the candidate digest
        (computed from the upstream's current ``tools/list`` payload)
        matches *expected_digest*, the transition commits; otherwise the
        refresh is refused and the returned diff has ``refused=True``.
        See :meth:`ToolRegistry.populate_domain` for the full semantics.

        Raises ``KeyError`` if *domain* is not a configured upstream.
        """
        client = self._registry_clients[domain]
        return await self._populate_domain(domain, client, expected_digest=expected_digest)

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
        execution-side client, ensuring consistent transport configuration.
        If *domain* has explicit upstream headers, those are merged onto
        the new client's transport.  Otherwise the client inherits
        request-passthrough behaviour via the ContextVar.

        Header merge priority (highest wins):
        1. *extra_headers* from hooks (e.g. ``X-User-Subject``)
        2. Static ``upstream_headers[domain]`` (e.g. per-domain API keys)
        3. Request passthrough via ContextVar (incoming request headers)
        """
        # Clone the execution-side client (not the registry one); the
        # registry client may target a separate discovery URL (e.g.
        # `/_introspect`) that does not accept `tools/call`.
        client = self._execution_clients[domain].new()
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
        discovery_url: str | None = None,
        headers: dict[str, str] | None = None,
        registry_auth_headers: dict[str, str] | None = None,
    ) -> RegistryDiff:
        """Add a new upstream at runtime and populate its tools.

        If the domain already exists, its URL and headers are updated and
        the registry is re-populated (idempotent upsert).

        Transactional semantics
        -----------------------
        The probe (:meth:`_populate_domain`) runs against a candidate
        ``reg_client`` built as a local — none of the per-domain
        dicts (``_upstreams``, ``_upstream_headers``,
        ``_registry_clients``, ``_execution_clients``) are mutated
        until the probe succeeds.  Concurrent observers
        (:meth:`list_upstreams`, :meth:`upstream_url`,
        :meth:`execute_tool`) therefore never see a half-staged URL
        paired with the prior tools, nor an unverified client pair
        routing traffic before discovery confirmed the upstream is
        reachable.

        On probe failure or ``diff.refused``, the manager state is
        unchanged — no rollback is required because no mutation
        occurred.  On success, the four maps are committed in a
        single synchronous region (no ``await``), making the
        transition atomic from the asyncio event loop's perspective;
        the prior client pair is closed only after that commit so
        any in-flight :meth:`execute_tool` dispatch lands on the new
        pair rather than a half-shut-down client.

        Parameters
        ----------
        domain:
            Domain name for the upstream (e.g. ``"apollo"``).
        url:
            MCP server URL (e.g. ``"http://apollo:8080/mcp"``) used for
            ``execute_tool`` dispatch. Carries the inbound user JWT via
            request-passthrough.
        discovery_url:
            Optional MCP URL used for ``list_tools`` discovery only —
            e.g. an unauth ``/_introspect`` endpoint mounted at host
            root, network-policy-protected, that speaks the discovery
            slice of the MCP protocol without requiring a user JWT.
            When ``None``, discovery uses *url* and the prior single-
            client behaviour applies.
        headers:
            Per-domain headers for tool execution (e.g. auth tokens).
        registry_auth_headers:
            Headers for registry population (list_tools calls).
        """
        with _tracer.start_as_current_span("gateway.add_upstream") as span:
            span.set_attribute("gateway.domain", domain)
            span.set_attribute("gateway.url", url)
            if discovery_url:
                span.set_attribute("gateway.discovery_url", discovery_url)

            # Use explicitly provided registry_auth_headers, or fall back
            # to the default configured at startup (GATEWAY_REGISTRY_AUTH_TOKEN).
            # None-check (not truthiness) so callers can pass {} to explicitly
            # disable auth for a specific upstream even when startup auth exists.
            effective_auth = registry_auth_headers if registry_auth_headers is not None else self._registry_auth_headers

            # Build candidate clients as locals — ``Client(...)`` is
            # pure construction (no I/O) and no shared-state mutation
            # yet.  Auth headers stay on the registry side only — see
            # the comment in __init__ for the Client.new() bleed
            # rationale.
            disc_url: str = discovery_url or url
            reg_client = Client(disc_url)
            if effective_auth:
                _set_transport_headers(reg_client, effective_auth)
            exec_client = Client(url)

            # Probe with the candidate URL passed explicitly so
            # _populate_domain does NOT read from self._upstreams.
            # If this raises or returns diff.refused, the per-domain
            # dicts remain in their pre-call state — concurrent
            # observers see no transient half-staged URL paired with
            # the prior tools.
            # _populate_domain lazily created a per-domain lock for this probe.
            # If the domain is NEW and the probe fails or is refused it never
            # enters the manager, so drop the lock to keep _registry_locks from
            # growing without bound under add/remove churn.
            new_domain = domain not in self._upstreams
            try:
                diff = await self._populate_domain(domain, reg_client, upstream_url=url)
            except Exception:
                # Re-check live state: a concurrent successful add_upstream for
                # this domain may have registered it (making its lock canonical)
                # while we awaited, so only drop the lock if it's still this
                # failing attempt's orphan.
                if new_domain and domain not in self._upstreams:
                    self._registry_locks.pop(domain, None)
                raise
            if diff.refused:
                # Same re-check as the except path — don't evict a lock a
                # concurrent successful registration now owns.
                if new_domain and domain not in self._upstreams:
                    self._registry_locks.pop(domain, None)
                # Registry preserved its prior tools on refusal;
                # manager keeps its prior URL/clients.  No mutations
                # to undo.  Return the diff so the caller can
                # surface the schema-mismatch detail in its response.
                return diff

            # Probe succeeded — commit all four maps in one
            # synchronous region.  asyncio is single-threaded between
            # awaits, so the four assignments below are atomic from
            # any concurrent coroutine's perspective.
            prior_reg_client = self._registry_clients.get(domain)
            prior_exec_client = self._execution_clients.get(domain)
            self._upstreams[domain] = url
            if headers:
                self._upstream_headers[domain] = headers
            else:
                self._upstream_headers.pop(domain, None)
            self._registry_clients[domain] = reg_client
            self._execution_clients[domain] = exec_client

            # Close the prior client pair after commit.  Any concurrent
            # dispatch that resumes here sees the already-committed
            # new pair, never a half-shut-down client.
            for old in (prior_reg_client, prior_exec_client):
                if old is None:
                    continue
                try:
                    async with old:
                        pass  # __aexit__ closes the session
                except Exception:
                    logger.debug("Error closing prior client for domain '%s'", domain, exc_info=True)

            logger.info(
                "Registered upstream '%s' (%s, discovery=%s): %d tools",
                domain,
                url,
                disc_url,
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
            reg_client = self._registry_clients.pop(domain, None)
            exec_client = self._execution_clients.pop(domain, None)
            self._upstream_headers.pop(domain, None)
            self._registry_locks.pop(domain, None)

            # Close both clients to release connection resources. When
            # discovery_url == url at add_upstream time, exec_client is a
            # distinct Client instance pointing at the same URL — closing
            # both is correct and not a double-close.
            for c in (reg_client, exec_client):
                if c is None:
                    continue
                try:
                    async with c:
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

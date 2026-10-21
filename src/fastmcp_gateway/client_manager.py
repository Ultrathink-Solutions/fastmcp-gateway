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
import inspect
import logging
import re
import weakref
from collections import defaultdict
from contextlib import asynccontextmanager
from copy import copy, deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from fastmcp import Client
from fastmcp.server.dependencies import get_http_headers
from mcp.shared.exceptions import McpError
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict

from fastmcp_gateway.errors import RegistrationSelectionError, UpstreamJsonRpcError
from fastmcp_gateway.registration import (
    RegistrationChange,
    RegistrationCommitHook,
    RegistrationIdentity,
    RegistrationSnapshot,
    registration_snapshot,
)
from fastmcp_gateway.registry import ToolEntry
from fastmcp_gateway.sanitize import DEFAULT_MAX_SCHEMA_DEPTH, _validate_max_schema_depth

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

    from fastmcp.client.client import CallToolResult
    from mcp.types import ErrorData, RequestId

    from fastmcp_gateway.access_policy import AccessPolicy
    from fastmcp_gateway.registry import PreparedDomain, RegistryDiff, ToolRegistry

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


async def _call_tool_without_metadata_injection(
    client: Client,
    name: str,
    arguments: dict[str, Any],
    request_meta: Mapping[str, Any] | None,
) -> CallToolResult:
    """Call through the MCP session without FastMCP trace metadata injection.

    FastMCP 3.4.x has no public equivalents for its session monitor or raw
    protocol-result adapter. Keep both private API dependencies isolated here.
    """

    dispatch_meta = None if request_meta is None else deepcopy(dict(request_meta))
    raw_result = await client._await_with_session_monitoring(
        client.session.call_tool(
            name,
            arguments,
            meta=dispatch_meta,
        )
    )
    return await client._parse_call_tool_result(
        name,
        raw_result,
        raise_on_error=False,
    )


class _ArrivedErrors:
    """Records the JSON-RPC error responses that arrive on one MCP session.

    Registered as the session's response router, it sees every error response
    read from the transport before it is delivered to the waiting request, and
    nothing the client builds itself (the ``"Connection closed"`` it hands each
    pending request when the session drops, a read timeout). It never claims a
    response, so delivery is unchanged. The SDK raises the ``McpError`` for a
    received error with the same ``ErrorData`` object, so an identity match is
    how ``execute_tool`` knows the error is the upstream's answer.
    """

    def __init__(self) -> None:
        self._errors: list[ErrorData] = []

    def route_response(self, request_id: RequestId, response: dict[str, Any]) -> bool:
        return False

    def route_error(self, request_id: RequestId, error: ErrorData) -> bool:
        self._errors.append(error)
        return False

    def arrived(self, error: ErrorData) -> bool:
        return any(error is seen for seen in self._errors)


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


async def _settle_task(task: asyncio.Task[Any]) -> Any:
    """Finish an irreversible commit or cleanup before propagating cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break  # Read and propagate the settled task's exception below.
    if cancelled:
        try:
            task.result()
        except BaseException as exc:
            raise asyncio.CancelledError from exc
        raise asyncio.CancelledError
    return task.result()


class ResolvedTool(BaseModel):
    """Manager-issued detached tool and registration contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    entry: ToolEntry
    registration: RegistrationSnapshot


@dataclass
class _RegistrationRuntime:
    snapshot: RegistrationSnapshot
    retiring: bool = False
    active: int = 0
    drained: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.drained.set()


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
    max_schema_depth:
        Forwarded to :meth:`ToolRegistry.populate_domain` as its
        ``max_schema_depth`` on every populate/refresh/add_upstream
        call. Defaults to
        :data:`~fastmcp_gateway.sanitize.DEFAULT_MAX_SCHEMA_DEPTH`,
        matching prior behaviour. Must be an int between ``1`` and
        :data:`~fastmcp_gateway.sanitize.MAX_ALLOWED_SCHEMA_DEPTH`
        inclusive; validated here at
        construction time (not deferred to the first populate call) so
        a caller that constructs ``UpstreamManager`` directly --
        bypassing ``GatewayServer`` -- gets the same fail-fast
        guarantee.
    """

    def __init__(
        self,
        upstreams: dict[str, Any],
        registry: ToolRegistry,
        *,
        registry_auth_headers: dict[str, str] | None = None,
        registry_token_provider: Callable[[], str | Awaitable[str]] | None = None,
        upstream_headers: dict[str, dict[str, str]] | None = None,
        policy: AccessPolicy | None = None,
        sanitizer_trusted_domains: set[str] | None = None,
        trusted_output_tools: set[str] | None = None,
        discovery_urls: dict[str, str] | None = None,
        max_schema_depth: int = DEFAULT_MAX_SCHEMA_DEPTH,
        registration_identities: Mapping[str, RegistrationIdentity] | None = None,
    ) -> None:
        # Fail fast here rather than deferring to the first populate()
        # call -- a caller that constructs UpstreamManager directly
        # (bypassing GatewayServer's own construction-time check) must
        # get the same immediate failure. The upper bound guards against
        # a stack-overflow-prone recursion depth, not just a typo.
        _validate_max_schema_depth(max_schema_depth)
        unknown = set(registration_identities or {}) - set(upstreams)
        if unknown:
            raise ValueError("Registration identities contain unknown upstreams")
        self._identities = dict(registration_identities or {})
        self._runtimes: dict[str, _RegistrationRuntime] = {}
        self._issued: dict[int, tuple[weakref.ReferenceType[ResolvedTool], ResolvedTool]] = {}
        self._commit_hook: RegistrationCommitHook | None = None
        self._publication_lock = asyncio.Lock()
        self._blocked_domains: set[str] = set()
        self._transitions: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._discovery_urls = dict(discovery_urls or {})
        self._upstreams: dict[str, Any] = {
            name: deepcopy(value) if isinstance(value, dict) else value for name, value in upstreams.items()
        }
        self._registry = registry
        self._upstream_headers = deepcopy(upstream_headers or {})
        self._registry_auth_headers = deepcopy(registry_auth_headers)
        self._registry_token_provider = registry_token_provider
        # Per-domain locks serialize the (token refresh + header set + list_tools)
        # sequence on each persistent registry client, so concurrent same-domain
        # populate/refresh calls can't overwrite Authorization between the header
        # update and the fetch that must use it.
        self._registry_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._policy = deepcopy(policy)
        self._max_schema_depth = max_schema_depth
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
        for domain, url in self._upstreams.items():
            disc_url = self._discovery_urls.get(domain) or url
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
            for domain in list(self._registry_clients):
                try:
                    client = self._registry_clients[domain]
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

    async def _prepare_domain(
        self,
        domain: str,
        client: Client,
        *,
        expected_digest: str | None = None,
        upstream_url: str | None = None,
    ) -> PreparedDomain:
        """Connect to *client* and prepare its tools without publication.

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
                    # Support both sync (`-> str`) and async (`-> Awaitable[str]`)
                    # providers: a token cache that mints over HTTP is naturally
                    # async, and awaiting here (rather than forcing a sync bridge)
                    # keeps the event loop unblocked and avoids a cold-start gap.
                    result = self._registry_token_provider()
                    token = await result if inspect.isawaitable(result) else result
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
                meta = getattr(t, "meta", None)
                if isinstance(meta, dict):
                    entry["_meta"] = dict(meta)
                raw_tools.append(entry)

            target = upstream_url if upstream_url is not None else self._upstreams[domain]
            effective_url = target if isinstance(target, str) else ""
            prepared = self._registry.prepare_domain(
                domain=domain,
                upstream_url=effective_url,
                tools=raw_tools,
                policy=self._policy,
                expected_digest=expected_digest,
                trusted_domains=self._sanitizer_trusted_domains,
                trusted_output_tool_patterns=self._trusted_output_tool_patterns,
                max_schema_depth=self._max_schema_depth,
            )
            span.set_attribute("gateway.tool_count", prepared.diff.tool_count)
            if prepared.diff.refused:
                span.set_attribute("gateway.schema_refused", True)
            return prepared

    async def _populate_domain(
        self,
        domain: str,
        client: Client,
        *,
        expected_digest: str | None = None,
        upstream_url: str | None = None,
    ) -> RegistryDiff:
        async with self._transitions[domain]:
            # Refresh the currently published template after obtaining the lock.
            client = self._registry_clients[domain]
            prepared = await self._prepare_domain(domain, client, expected_digest=expected_digest)
            if prepared.diff.refused:
                return prepared.diff
            identity = self._identities.get(domain) or RegistrationIdentity(registration_id=uuid4())

            def candidate() -> RegistrationSnapshot:
                return self._snapshot_candidate(
                    domain, identity, self._upstreams[domain], self._discovery_urls.get(domain), prepared
                )

            # No awaits between projection and this no-op check: raw integrity
            # acknowledgement needs neither retirement nor a persistence callback.
            current = self._runtimes.get(domain)
            if current is not None and not current.retiring:
                projected = candidate()
                if current.snapshot.registration_revision == projected.registration_revision:
                    self._registry.commit_schema_digest(prepared)
                    return prepared.diff.model_copy(update={"tool_count": len(projected.tools)})

            def publish(snapshot: RegistrationSnapshot | None) -> RegistryDiff:
                assert snapshot is not None
                diff = self._registry.commit_domain(prepared)
                self._identities[domain] = identity
                self._runtimes[domain] = _RegistrationRuntime(snapshot)
                return diff

            return await self._transition(domain, "refresh" if current is not None else "replace", candidate, publish)

    def _snapshot_candidate(
        self,
        domain: str,
        identity: RegistrationIdentity,
        url: Any,
        discovery_url: str | None,
        prepared: PreparedDomain,
    ) -> RegistrationSnapshot:
        # Apply existing collision admission to a detached registry, never to served state.
        staged = deepcopy(self._registry)
        staged.commit_domain(prepared)
        return registration_snapshot(
            domain,
            identity,
            url if isinstance(url, str) else None,
            discovery_url or (url if isinstance(url, str) else None),
            tuple(staged.get_tools_by_domain(domain)),
        )

    def registration_snapshot(self, server_name: str) -> RegistrationSnapshot | None:
        """Return a detached available contract, or hide a retiring incarnation."""
        runtime = self._runtimes.get(server_name)
        if runtime is None or runtime.retiring or server_name in self._blocked_domains:
            return None
        # Aliases are display state and may have changed due to another domain.
        return runtime.snapshot.model_copy(
            deep=True,
            update={
                "tools": tuple(t.model_copy(deep=True) for t in self._registry.get_tools_by_domain(server_name)),
            },
        )

    def resolve_tool(
        self,
        tool_name: str,
        *,
        server_name: str | None = None,
        registration_revision: str | None = None,
    ) -> ResolvedTool:
        """Resolve once; exact server selectors never fall back to global aliases."""
        selected = server_name is not None or registration_revision is not None
        if selected and (
            not isinstance(server_name, str)
            or not server_name.strip()
            or not isinstance(registration_revision, str)
            or re.fullmatch(r"reg-v1:[0-9a-f]{64}", registration_revision) is None
        ):
            raise RegistrationSelectionError("invalid_selector", "Invalid registration selector")
        entry = (
            self._registry.lookup_registered(server_name, tool_name)
            if server_name is not None
            else self._registry.lookup(tool_name)
        )
        if server_name is not None:
            runtime = self._runtimes.get(server_name)
            if runtime is not None and (
                runtime.retiring or runtime.snapshot.registration_revision != registration_revision
            ):
                raise RegistrationSelectionError("registration_changed", "Registration changed")
        if (server_name is not None and server_name in self._blocked_domains) or (
            entry is not None and entry.domain in self._blocked_domains
        ):
            raise RegistrationSelectionError("registration_changed", "Registration changed")
        if entry is None:
            raise RegistrationSelectionError("tool_not_found", "Tool not found")
        runtime = self._runtimes.get(entry.domain)
        if runtime is None:
            # Programmatic legacy registries may be populated directly before first use.
            if selected or entry.domain not in self._upstreams:
                raise RegistrationSelectionError("tool_not_found", "Registration not found")
            identity = self._identities.get(entry.domain) or RegistrationIdentity(registration_id=uuid4())
            self._identities[entry.domain] = identity
            url = self._upstreams[entry.domain]
            snapshot = registration_snapshot(
                entry.domain,
                identity,
                url if isinstance(url, str) else None,
                self._discovery_urls.get(entry.domain) or (url if isinstance(url, str) else None),
                tuple(self._registry.get_tools_by_domain(entry.domain)),
            )
            runtime = self._runtimes[entry.domain] = _RegistrationRuntime(snapshot)
        if runtime.retiring:
            raise RegistrationSelectionError("registration_changed", "Registration changed")
        resolved = ResolvedTool(
            entry=ToolEntry.model_validate(entry.model_dump()), registration=runtime.snapshot.model_copy(deep=True)
        )
        key = id(resolved)
        self._issued[key] = (
            weakref.ref(resolved, lambda _: self._issued.pop(key, None)),
            resolved.model_copy(deep=True),
        )
        return resolved

    @asynccontextmanager
    async def pin_registration(self, resolved: ResolvedTool) -> AsyncIterator[None]:
        """Admit a manager-issued snapshot without serializing concurrent borrowers."""
        issued = self._issued.get(id(resolved))
        runtime = self._runtimes.get(resolved.registration.server_name)
        canonical = resolved.entry.original_name or resolved.entry.name
        if (
            issued is None
            or issued[0]() is not resolved
            or issued[1] != resolved
            or runtime is None
            or runtime.retiring
            or resolved.entry.domain in self._blocked_domains
            or runtime.snapshot.registration_revision != resolved.registration.registration_revision
            or self._registry.lookup_registered(resolved.entry.domain, canonical) is None
            or not any(
                (t.original_name or t.name) == canonical and t.domain == resolved.entry.domain
                for t in runtime.snapshot.tools
            )
        ):
            raise RegistrationSelectionError("registration_changed", "Registration changed")
        runtime.active += 1
        runtime.drained.clear()
        try:
            yield
        finally:
            runtime.active -= 1
            if not runtime.active:
                runtime.drained.set()

    def set_registration_commit_hook(self, hook: RegistrationCommitHook) -> None:
        """Install the durable adapter once, before the first population."""
        if self._commit_hook is not None or self._runtimes:
            raise ValueError("Registration commit hook must be installed once before population")
        self._commit_hook = hook

    async def _transition(
        self,
        domain: str,
        kind: Literal["replace", "refresh", "remove"],
        candidate: Callable[[], RegistrationSnapshot] | None,
        publish: Callable[[RegistrationSnapshot | None], Any],
    ) -> Any:
        previous = self._runtimes.get(domain)
        was_retiring = previous.retiring if previous is not None else False
        was_blocked = domain in self._blocked_domains
        commit_started = False
        if previous is not None:
            previous.retiring = True
        try:
            if previous is not None:
                await previous.drained.wait()
            # A slow admitted call holds only its domain's transition lock.
            # Recompute aliases after acquiring publication ownership: another
            # domain may have published while this registration was draining.
            async with self._publication_lock:
                owner = asyncio.current_task()
                if owner is not None and owner.cancelling():
                    raise asyncio.CancelledError
                snapshot = candidate() if candidate is not None else None
                change = RegistrationChange(
                    kind=kind,
                    previous=previous.snapshot.model_copy(deep=True) if previous else None,
                    current=snapshot.model_copy(deep=True) if snapshot else None,
                )
                self._blocked_domains.add(domain)

                async def commit() -> Any:
                    # Cancellation queued before the callback starts is still pre-commit.
                    if owner is not None and owner.cancelling():
                        if previous is not None:
                            previous.retiring = was_retiring
                        if not was_blocked:
                            self._blocked_domains.discard(domain)
                        raise asyncio.CancelledError
                    if self._commit_hook is not None:
                        await self._commit_hook(change)
                    result = publish(snapshot)
                    self._blocked_domains.discard(domain)
                    return result

                commit_started = True
                return await _settle_task(asyncio.create_task(commit()))
        except BaseException:
            if not commit_started and previous is not None:
                previous.retiring = was_retiring
            raise

    async def _close_clients(self, *clients: Client | None) -> None:
        async def close_all() -> None:
            seen: set[int] = set()
            for client in clients:
                if client is None or id(client) in seen:
                    continue
                seen.add(id(client))
                try:
                    await client.close()
                except Exception:
                    logger.warning("Error closing retired registration client", exc_info=True)

        await _settle_task(asyncio.create_task(close_all()))

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
            for domain in list(self._registry_clients):
                try:
                    client = self._registry_clients[domain]
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
        request_meta: Mapping[str, Any] | None = None,
        resolved_tool: ResolvedTool | None = None,
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
        request_meta:
            Exact inbound MCP request metadata to forward. ``None`` stays absent.

        Raises ``KeyError`` if *tool_name* is not in the registry, and
        :class:`~fastmcp_gateway.errors.UpstreamJsonRpcError` (an ``McpError``)
        when the upstream answers the call with a JSON-RPC error response. An
        ``McpError`` the MCP client raises by itself, because no answer arrived,
        propagates unchanged.
        """
        with _tracer.start_as_current_span("gateway.upstream.execute") as span:
            span.set_attribute("gateway.tool_name", tool_name)

            if resolved_tool is None:
                try:
                    resolved_tool = self.resolve_tool(tool_name)
                except RegistrationSelectionError as exc:
                    if exc.code == "tool_not_found":
                        raise KeyError(f"Tool '{tool_name}' not found in registry") from exc
                    raise
            async with self.pin_registration(resolved_tool):
                entry = self._issued[id(resolved_tool)][1].entry
                span.set_attribute("gateway.domain", entry.domain)
                fresh_client = self._make_execution_client(entry.domain, extra_headers=extra_headers)

                # Use the original (upstream) tool name when dispatching.
                # Collision-prefixed names (e.g., "snowflake_get_server_info")
                # exist only in the gateway registry — the upstream server only
                # knows the original name ("get_server_info").
                upstream_name = entry.original_name or entry.name

                async with fresh_client:
                    arrived = _ArrivedErrors()
                    fresh_client.session.add_response_router(arrived)
                    try:
                        return await _call_tool_without_metadata_injection(
                            fresh_client,
                            upstream_name,
                            arguments or {},
                            request_meta,
                        )
                    except McpError as exc:
                        if arrived.arrived(exc.error):
                            raise UpstreamJsonRpcError(exc.error) from exc
                        raise

    def _make_execution_client(
        self,
        domain: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> Client:
        """Create a fresh client for tool execution via ``client.new()``.

        Always uses ``client.new()`` to create a shallow copy of the
        execution-side client, ensuring consistent transport configuration,
        and gives the copy its own copy of the transport: ``Client.new()``
        shares the transport with the base client, so headers merged onto
        it would otherwise reach every later and concurrent call to the
        same upstream.  If *domain* has explicit upstream headers, those are
        merged onto the new client's transport.  Otherwise the client
        inherits request-passthrough behaviour via the ContextVar.

        Header merge priority (highest wins):
        1. *extra_headers* from hooks (e.g. ``X-User-Subject``)
        2. Static ``upstream_headers[domain]`` (e.g. per-domain API keys)
        3. Request passthrough via ContextVar (incoming request headers)
        """
        # Clone the execution-side client (not the registry one); the
        # registry client may target a separate discovery URL (e.g.
        # `/_introspect`) that does not accept `tools/call`.
        client = self._execution_clients[domain].new()
        # The call's headers (and the per-connection state a transport
        # records) belong to this call alone.
        client.transport = copy(client.transport)
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
        configuration_revision: str | None = None,
        registration_identity: RegistrationIdentity | None = None,
    ) -> RegistryDiff:
        """Prepare, drain, durably commit and publish a new incarnation."""
        headers = deepcopy(headers)
        registry_auth_headers = deepcopy(registry_auth_headers)
        async with self._transitions[domain]:
            identity = registration_identity or RegistrationIdentity(
                registration_id=uuid4(), configuration_revision=configuration_revision
            )
            if registration_identity is not None:
                if configuration_revision is not None and configuration_revision != identity.configuration_revision:
                    raise ValueError("Restored identity configuration revision mismatch")
                current = self._identities.get(domain)
                if (
                    domain in self._upstreams
                    and current is not None
                    and current.registration_id == identity.registration_id
                ):
                    raise ValueError("Cannot restore the same identity over a live registration")
            effective_auth = registry_auth_headers if registry_auth_headers is not None else self._registry_auth_headers
            reg_client = Client(discovery_url or url)
            exec_client = Client(url)
            if effective_auth:
                _set_transport_headers(reg_client, dict(effective_auth))
            published = False
            old_reg = self._registry_clients.get(domain)
            old_exec = self._execution_clients.get(domain)
            try:
                prepared = await self._prepare_domain(domain, reg_client, upstream_url=url)
                if prepared.diff.refused:
                    return prepared.diff

                def candidate() -> RegistrationSnapshot:
                    return self._snapshot_candidate(domain, identity, url, discovery_url, prepared)

                def publish(snapshot: RegistrationSnapshot | None) -> RegistryDiff:
                    nonlocal published
                    assert snapshot is not None
                    diff = self._registry.commit_domain(prepared)
                    self._upstreams[domain] = url
                    self._upstream_headers.pop(domain, None)
                    if headers:
                        self._upstream_headers[domain] = dict(headers)
                    self._registry_clients[domain] = reg_client
                    self._execution_clients[domain] = exec_client
                    self._identities[domain] = identity
                    if discovery_url:
                        self._discovery_urls[domain] = discovery_url
                    else:
                        self._discovery_urls.pop(domain, None)
                    self._runtimes[domain] = _RegistrationRuntime(snapshot)
                    published = True
                    return diff

                return await self._transition(domain, "replace", candidate, publish)
            finally:
                # A shielded commit may have published even when its caller was cancelled.
                await self._close_clients(*((old_reg, old_exec) if published else (reg_client, exec_client)))

    async def remove_upstream(self, domain: str) -> list[str]:
        """Retire admission and drain calls before committing removal."""
        async with self._transitions[domain]:
            if domain not in self._upstreams:
                raise KeyError(f"Domain '{domain}' is not a registered upstream")
            old_reg = self._registry_clients.get(domain)
            old_exec = self._execution_clients.get(domain)
            published = False

            def publish(snapshot: RegistrationSnapshot | None) -> list[str]:
                nonlocal published
                removed = [t.name for t in self._registry.get_tools_by_domain(domain)]
                self._registry.clear_domain(domain)
                self._upstreams.pop(domain, None)
                self._registry_clients.pop(domain, None)
                self._execution_clients.pop(domain, None)
                self._upstream_headers.pop(domain, None)
                self._discovery_urls.pop(domain, None)
                self._identities.pop(domain, None)
                self._runtimes.pop(domain, None)
                published = True
                return removed

            try:
                return await self._transition(domain, "remove", None, publish)
            finally:
                if published:
                    await self._close_clients(old_reg, old_exec)

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

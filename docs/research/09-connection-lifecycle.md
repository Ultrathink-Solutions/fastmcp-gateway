# Connection Lifecycle and Authentication Passthrough

**Date:** 2026-02-18
**Status:** Design complete
**Source:** FastMCP source analysis (`/tmp/fastmcp-source`), requirements.md open questions 1 and 7

---

## Overview

The gateway operates as a dual-role participant in the MCP protocol:
- **Server role**: Receives MCP requests from clients (PydanticAI agents, Claude Code, etc.)
- **Client role**: Forwards requests to multiple upstream MCP servers

This creates two design challenges:
1. How to manage connections to upstream servers (lifecycle, reuse, overhead)
2. How to flow per-user authentication and context from incoming requests through to upstream calls

---

## 1. FastMCP Client Connection Internals

### Client Architecture

FastMCP's `Client` class uses a **reentrant async context manager** with reference counting:

```python
async with client:                    # nesting_counter = 1, session starts
    async with client:                # nesting_counter = 2, session reused
        await client.call_tool(...)   # uses active session
    # nesting_counter = 1, session stays alive
# nesting_counter = 0, session disconnects
```

Each context entry to an unconnected client triggers `_session_runner()`, a background task that:
1. Opens the transport (`connect_session()`)
2. Runs `auto_initialize()` (MCP handshake)
3. Signals readiness via `ready_event`
4. Waits for `stop_event` (disconnect signal)

### Client.new() -- Fresh Session Strategy

`Client.new()` creates a shallow copy with fresh `ClientSessionState` but the **same transport configuration**:

```python
def new(self) -> Client[ClientTransportT]:
    new_client = copy.copy(self)
    new_client._session_state = ClientSessionState()  # fresh state
    return new_client
```

This is the pattern `ProxyProvider` uses for per-request isolation.

### Transport-Level Connection Details

**StreamableHttpTransport** creates a new `httpx.AsyncClient` per session:

```python
async def connect_session(self, **session_kwargs):
    headers = get_http_headers() | self.headers  # Dynamic header merge!
    http_client = create_mcp_http_client(
        headers=headers,
        timeout=timeout,
        auth=self.auth,
    )
    async with (
        http_client,
        streamable_http_client(self.url, http_client=http_client) as transport,
    ):
        async with ClientSession(read_stream, write_stream, **session_kwargs) as session:
            yield session
```

Key points:
- HTTP/2 by default (persistent connection support within a session)
- New `httpx.AsyncClient` per `connect_session()` call (no cross-session pooling)
- Headers are resolved at connection time, not at construction time

**StdioTransport** keeps the subprocess alive by default (`keep_alive=True`), reusing it across sessions.

### Connection Overhead

Per tool call with connect-per-call pattern:

| Phase | Overhead | Notes |
|---|---|---|
| `Client.new()` | ~0ms | Shallow copy, no I/O |
| `connect_session()` | 10-50ms | TCP + TLS + HTTP/2 setup (in-cluster: 1-5ms without TLS) |
| `auto_initialize()` | 5-20ms | MCP handshake (protocol version, capabilities) |
| `call_tool()` | varies | Upstream processing time |
| Disconnect | ~0ms | httpx client cleanup |
| **Total overhead** | **15-70ms** | **In-cluster without TLS: 2-10ms** |

For in-cluster deployments (the primary use case), the overhead is negligible -- HTTP connection setup without TLS is sub-millisecond, and the MCP initialize handshake is a single round-trip.

---

## 2. ProxyProvider Strategy Analysis

FastMCP's `ProxyProvider` uses three strategies, selected by `_create_client_factory()`:

### Strategy A: Connected Client Reuse

```python
if client.is_connected():
    def reuse_client_factory():
        return client  # Same instance, same session
```

- **Pros**: Zero per-request overhead
- **Cons**: Context mixing in concurrent scenarios (ContextVar staleness), no per-request header injection
- **Use case**: Single-user scenarios, testing

### Strategy B: Fresh Session Per Request (Default)

```python
else:
    def fresh_client_factory():
        return client.new()  # New session state, same transport config
```

- **Pros**: Clean isolation, headers resolved per connection
- **Cons**: Per-request overhead (connection + initialize)
- **Use case**: Multi-user gateways, production deployments

### Strategy C: Stateful Session Cache

```python
class StatefulProxyClient:
    def new_stateful(self):
        session = get_context().session
        proxy_client = self._caches.get(session)
        if proxy_client is None:
            proxy_client = self.new()
            self._caches[session] = proxy_client
```

- **Pros**: One upstream session per client session (stateful backends)
- **Cons**: Memory growth with sessions, stale context requires `_restore_request_context()`
- **Use case**: Stateful backends like Playwright MCP server

---

## 3. Gateway Connection Strategy: Recommended Design

### Decision: Fresh Session Per Request for `execute_tool`

**Rationale:**
1. **Per-user headers are critical** -- each `execute_tool` call may carry different user context (auth tokens, user IDs). Fresh sessions ensure headers are resolved from the current HTTP request context.

2. **FastMCP's header forwarding works exactly for this** -- `StreamableHttpTransport.connect_session()` calls `get_http_headers()`, which reads from the current request's ContextVar. Fresh sessions mean correct headers.

3. **Overhead is acceptable** -- In-cluster without TLS, the per-request overhead is 2-10ms. This is negligible compared to the upstream tool execution time (typically 50ms-5s).

4. **Concurrency safety** -- No shared session state means no context mixing between concurrent requests from different users.

### Decision: Persistent Connections for Registry Operations

Registry operations (`list_tools()` during startup/refresh) don't carry per-user context. They can use persistent connections:

```python
class GatewayRegistry:
    """Manages upstream server connections and the tool registry."""

    def __init__(self, upstreams: dict[str, str]):
        # Persistent clients for registry operations (no user context)
        self._registry_clients: dict[str, Client] = {
            name: Client(url) for name, url in upstreams.items()
        }
        # Client factories for per-request tool execution (with user context)
        self._execution_factories: dict[str, Callable[[], Client]] = {
            name: lambda url=url: Client(url).new()
            for name, url in upstreams.items()
        }

    async def populate(self):
        """Startup: discover tools from all upstreams using persistent clients."""
        for name, client in self._registry_clients.items():
            async with client:
                tools = await client.list_tools()
                self._register_tools(name, tools)

    async def execute(self, tool_name: str, arguments: dict) -> Any:
        """Per-request: execute via fresh client (inherits current user headers)."""
        upstream = self._resolve_upstream(tool_name)
        client = self._execution_factories[upstream]()
        async with client:
            return await client.call_tool(tool_name, arguments)
```

### Connection Lifecycle Summary

| Operation | Strategy | User Context | Connection |
|---|---|---|---|
| `list_tools()` at startup | Persistent client | None (system-level) | Long-lived, reused |
| `list_tools()` on refresh | Persistent client | None (system-level) | Long-lived, reused |
| `discover_tools()` handling | No upstream call | N/A (reads from in-memory registry) | None |
| `get_tool_schema()` handling | No upstream call | N/A (reads from in-memory registry) | None |
| `execute_tool()` handling | Fresh client per request | Yes (user headers forwarded) | Short-lived, per-request |

Key insight: `discover_tools` and `get_tool_schema` never touch upstream servers during request handling -- they read from the in-memory registry. Only `execute_tool` requires a live upstream connection, and it uses the fresh-per-request strategy for user context safety.

---

## 4. Authentication Passthrough Architecture

### The Challenge

User credentials must flow through the full chain:

```
PydanticAI Agent
  |-- DynamicToolset injects headers: Authorization, X-User-Id
      |
      v
  Gateway (HTTP server)
  |-- FastMCP DI extracts: get_http_headers(), get_access_token()
      |
      v
  Gateway execute_tool() routes to upstream
  |-- ??? How do user headers reach the upstream client?
      |
      v
  Upstream MCP Server
  |-- Receives forwarded headers
```

### How FastMCP Solves This (Already Built)

The critical mechanism is in `StreamableHttpTransport.connect_session()`:

```python
# fastmcp/client/transports/http.py, line 95-98
headers = get_http_headers() | self.headers
```

`get_http_headers()` reads from a ContextVar (`_current_http_request`) that FastMCP's HTTP server sets for each incoming request. When the gateway creates a fresh client and enters its async context **within the same request handler**, the ContextVar is still set, so the incoming headers automatically forward to the upstream connection.

This means: **for HTTP deployments, authentication passthrough is free.** No custom code needed.

### Path A: HTTP Deployment (Primary)

```
PydanticAI --> HTTP --> Gateway Server --> HTTP --> Upstream Server
                        |                  |
                        |  ContextVar:     |  get_http_headers()
                        |  _current_http_  |  resolves from same
                        |  request         |  ContextVar
```

Flow:
1. PydanticAI's `DynamicToolset` sets `Authorization` and `X-User-Id` headers on the HTTP request
2. Gateway's Starlette/ASGI handler sets `_current_http_request` ContextVar
3. `execute_tool` handler creates a fresh `Client` for the upstream
4. `client.__aenter__()` calls `transport.connect_session()`
5. `connect_session()` calls `get_http_headers()` which reads from the ContextVar
6. Headers merge: `incoming_headers | static_transport_headers`
7. Upstream receives the forwarded headers

**What headers are forwarded?** FastMCP's `get_http_headers()` strips hop-by-hop headers (`connection`, `host`, `content-length`, `transfer-encoding`, etc.) and forwards everything else. This includes:
- `authorization` -- user's auth token
- `x-user-id` -- user identity
- `x-conversation-id` -- conversation context
- Any custom headers the client sets

**What headers are NOT forwarded?**
- `host` (replaced with upstream's host)
- `content-length`, `content-type` (set by httpx for the new request)
- `connection`, `upgrade`, `transfer-encoding` (hop-by-hop)

### Path B: In-Process Deployment (FastMCPToolset)

When the gateway runs in-process via `FastMCPToolset`, there are no HTTP headers. The MCP `_meta` field becomes the context carrier:

```
PydanticAI --> In-Process --> Gateway Server --> HTTP --> Upstream Server
               (no HTTP)      |                  |
                              |  No ContextVar   |  Need alternative
                              |  for headers     |  context passing
```

For in-process deployments, user context must flow via MCP's `_meta` field. PydanticAI's `process_tool_call` hook can inject metadata:

```python
async def inject_user_context(ctx, call_fn, name, args):
    metadata = {"user_id": ctx.deps.user_id, "auth_token": ctx.deps.auth_token}
    return await call_fn(name, args, metadata)

gateway = FastMCPToolset(gateway_server, process_tool_call=inject_user_context)
```

On the gateway side, the `execute_tool` handler extracts from `_meta`:

```python
from fastmcp.server.dependencies import get_context

@gateway.tool()
async def execute_tool(tool_name: str, arguments: dict) -> Any:
    ctx = get_context()
    meta = ctx.request_context.meta if ctx.request_context else {}
    user_context = UserContext(
        user_id=meta.get("user_id", ""),
        auth_token=meta.get("auth_token", ""),
    )
    # Forward to upstream with explicit headers
    ...
```

**However**, this creates a challenge: the upstream `Client`'s `get_http_headers()` will return nothing (no HTTP request context). The gateway must explicitly set headers on the upstream transport:

```python
# Option: Create client with explicit headers for in-process path
upstream_client = Client(
    StreamableHttpTransport(
        url=upstream_url,
        headers={
            "Authorization": f"Bearer {user_context.auth_token}",
            "X-User-Id": user_context.user_id,
        },
    )
)
```

### Unified Auth Passthrough Design

To handle both HTTP and in-process paths uniformly:

```python
from fastmcp.server.dependencies import get_context, get_http_headers

def get_user_headers() -> dict[str, str]:
    """Get user context headers from either HTTP request or MCP _meta.

    In HTTP deployments, headers come from the HTTP request.
    In in-process deployments, headers come from MCP _meta.
    """
    # Try HTTP headers first (works in HTTP deployments)
    try:
        headers = get_http_headers()
        if headers.get("authorization") or headers.get("x-user-id"):
            return headers
    except RuntimeError:
        pass

    # Fall back to MCP _meta (works in in-process deployments)
    try:
        ctx = get_context()
        if ctx.request_context and ctx.request_context.meta:
            meta = dict(ctx.request_context.meta)
            return {
                k: str(v) for k, v in meta.items()
                if k.startswith(("authorization", "x-"))
            }
    except RuntimeError:
        pass

    return {}
```

For `execute_tool`, the upstream client creation branches:

```python
async def execute_upstream(upstream_url: str, tool_name: str, args: dict) -> Any:
    # In HTTP path: get_http_headers() auto-forwards (FastMCP built-in)
    # In in-process path: we need explicit headers
    try:
        # Check if we're in an HTTP request context
        get_http_headers()
        # HTTP path: create plain client, headers forward automatically
        client = Client(upstream_url)
    except RuntimeError:
        # In-process path: inject headers explicitly
        user_headers = get_user_headers()
        client = Client(StreamableHttpTransport(url=upstream_url, headers=user_headers))

    async with client.new():
        return await client.call_tool(tool_name, args)
```

### Auth Passthrough Summary

| Deployment | Context Source | Header Mechanism | Custom Code Needed |
|---|---|---|---|
| HTTP (primary) | HTTP request headers | `get_http_headers()` ContextVar, auto-forwarded | None |
| In-process | MCP `_meta` field | Extract from meta, set on transport | Yes (gateway helper) |
| Stdio | MCP `_meta` field | Same as in-process | Yes (gateway helper) |

### Security Considerations

1. **Header filtering**: The gateway should only forward known-safe headers to upstreams. FastMCP already strips hop-by-hop headers. The gateway should additionally consider stripping internal-only headers before forwarding.

2. **Token scope**: If the gateway performs auth (validates JWTs, checks scopes), it should forward the validated identity, not the raw token. This prevents token replay across upstream boundaries.

3. **Upstream auth isolation**: Different upstreams may require different auth. The gateway can maintain per-upstream auth configuration (static API keys, OAuth client credentials) that supplements or replaces forwarded user auth:

```python
upstreams = {
    "apollo": UpstreamConfig(
        url="http://apollo-mcp:8080/mcp",
        static_headers={"X-API-Key": "apollo-key-123"},  # Gateway's own credential
        forward_user_headers=True,                         # Also forward user context
    ),
    "internal": UpstreamConfig(
        url="http://internal-mcp:8080/mcp",
        static_headers={"X-Service-Auth": "gateway-token"},
        forward_user_headers=False,                        # Don't forward user context
    ),
}
```

4. **No credential leakage**: The 3 meta-tools' responses should never include auth headers, tokens, or internal routing details. Only tool names, descriptions, schemas, and results are returned to the LLM.

---

## 5. Upstream Health and Failure Handling

### Connection Failures During Registry Population

At startup, the gateway attempts `list_tools()` on each upstream. If an upstream is unreachable:

```python
async def populate(self):
    for name, client in self._registry_clients.items():
        try:
            async with client:
                tools = await client.list_tools()
                self._register_tools(name, tools)
        except Exception as e:
            logger.error(f"Failed to connect to upstream '{name}': {e}")
            # Continue with other upstreams (graceful degradation, FR-7)
```

### Connection Failures During `execute_tool`

When an upstream is unreachable at execution time:

```python
async def execute(self, tool_name: str, arguments: dict) -> ToolResult:
    upstream = self._resolve_upstream(tool_name)
    client = self._execution_factories[upstream]()
    try:
        async with client:
            return await client.call_tool(tool_name, arguments)
    except ConnectionError as e:
        return ToolResult(
            is_error=True,
            content=[TextContent(
                text=f"Upstream server '{upstream}' is currently unreachable. "
                     f"The tool '{tool_name}' is temporarily unavailable."
            )],
        )
```

### Health Checks and Circuit Breaking (Phase 2)

For production deployments, the gateway could track upstream health:

```python
class UpstreamHealth:
    consecutive_failures: int = 0
    last_failure: datetime | None = None
    is_circuit_open: bool = False

    def record_failure(self):
        self.consecutive_failures += 1
        self.last_failure = datetime.now()
        if self.consecutive_failures >= 3:
            self.is_circuit_open = True

    def record_success(self):
        self.consecutive_failures = 0
        self.is_circuit_open = False
```

This is explicitly a Phase 2 concern -- Phase 1 uses simple try/except with logging.

---

## 6. Technical Requirements Summary

| Requirement | Decision | Rationale |
|---|---|---|
| Registry operations | Persistent connections | No user context needed, reuse reduces overhead |
| `execute_tool` forwarding | Fresh client per request | User header isolation, concurrency safety |
| HTTP auth passthrough | Automatic via `get_http_headers()` | FastMCP built-in, zero custom code |
| In-process auth passthrough | Extract from MCP `_meta`, set on transport | Custom helper needed |
| Header filtering | Forward user headers, strip internal | Security boundary at gateway |
| Per-upstream auth | Static headers per upstream config | Gateway's own credentials |
| Upstream failure during registry | Log and skip, other upstreams unaffected | FR-7 graceful degradation |
| Upstream failure during execution | Return `is_error` content to LLM | Clear error message, no crash |
| Circuit breaking | Phase 2 | Simple try/except for Phase 1 |

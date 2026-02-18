# Language Choice and Architecture Analysis

**Date:** 2026-02-17
**Status:** Research complete

---

## 1. Language Choice: Python

### Is FastMCP Built in Rust?

No. FastMCP is **100% pure Python** (228 files, ~58K LOC). There are no Rust, C, or compiled components anywhere in the codebase. No `Cargo.toml`, no `.so`/`.pyd` files, no FFI bindings.

The confusion likely stems from **Pydantic v2**, which FastMCP depends on heavily. Pydantic v2's validation engine (`pydantic-core`) is written in Rust via PyO3. This gives all Pydantic model validation and serialization near-native speed. But FastMCP itself adds no Rust -- it's pure Python on top of Pydantic.

### The Full Stack

| Layer | Language | Details |
|---|---|---|
| FastMCP | Pure Python (228 files, ~58K LOC) | Server framework, DI, middleware, providers |
| `mcp` SDK (Anthropic) | Pure Python | Official MCP protocol implementation |
| Transport | Starlette (ASGI) + uvicorn | Standard Python async web stack |
| Serialization | Pydantic v2 | Rust-powered validation via `pydantic-core` |
| HTTP Client | httpx | Async HTTP for upstream connections |

### Why Python Is the Right Choice

**The gateway is I/O-bound, not CPU-bound.** The request path is:

```
Client request
  -> uvicorn (async event loop)
    -> Starlette ASGI
      -> StreamableHTTPSessionManager (mcp SDK)
        -> FastMCP server (tool dispatch, middleware, DI)
          -> httpx async client (upstream MCP call)
            -> Upstream MCP server response
```

The gateway adds **microseconds** of CPU work (dict lookup for routing, Pydantic validation) to requests that take **hundreds of milliseconds to seconds** (upstream API calls, LLM inference). Python's single-threaded performance is irrelevant. The async I/O model is what matters, and `asyncio` + `uvicorn` handles that well.

**The performance that matters is token efficiency.** Reducing 100K tokens to 2K per request saves far more real-world cost and latency (LLM inference time scales with input tokens) than any language-level optimization.

**The ecosystem value outweighs raw speed.** Choosing Go or Rust would mean rebuilding: DI, middleware pipeline, server composition, proxy infrastructure, config-driven setup, tool transforms, auth, and the Pydantic/PydanticAI/Logfire integration. That's ~80% of what FastMCP provides for free.

**The one potential bottleneck:** Embedding-based semantic search (Phase 2). But even then, the embeddings would be computed by native libraries (sentence-transformers / FAISS), not pure Python.

---

## 2. Extensibility: Use FastMCP's Existing System, Don't Build Our Own

### FastMCP's Three Extension Layers

FastMCP already provides three distinct, composable extensibility mechanisms. Building a plugin system on top would be redundant.

#### Layer 1: Providers (Where Components Come From)

```python
class Provider:
    async def _list_tools(self) -> Sequence[Tool]: ...
    async def _get_tool(self, name: str) -> Tool | None: ...
    async def lifespan(self) -> AsyncIterator[None]: ...
```

Providers are **sources of components**. They own lifecycle management, participate in transform chains, and support visibility controls. Built-in providers:

- `LocalProvider` -- `@mcp.tool()` decorated functions
- `ProxyProvider` -- tools from a remote MCP server via `Client`
- `FileSystemProvider` -- tools from Python files on disk
- `OpenAPIProvider` -- tools generated from OpenAPI specs
- `AggregateProvider` -- merges multiple providers

#### Layer 2: Transforms (How Components Are Modified)

```python
class Transform:
    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]: ...
    async def get_tool(self, name: str, call_next: GetToolNext) -> Tool | None: ...
```

Transforms are **pure functions over component sequences**. They sit between providers and the server. Built-in transforms:

- `Namespace` -- prefix tool names (`tool` -> `api_tool`)
- `Visibility` -- enable/disable by tags
- `ToolTransform` -- rename, re-describe, hide args
- `VersionFilter` -- filter by version
- `PromptsAsTools`, `ResourcesAsTools` -- expose other MCP primitives as tools

#### Layer 3: Middleware (How Requests Are Intercepted)

```python
class Middleware:
    async def on_list_tools(self, context, call_next) -> Sequence[Tool]: ...
    async def on_call_tool(self, context, call_next) -> ToolResult: ...
    # + hooks for every MCP method
```

Middleware operates on **MCP protocol messages**. Chain-of-responsibility pattern with typed hooks. Built-in middleware:

- `AuthMiddleware` -- scope-based authorization, filters tools per user
- `ToolInjectionMiddleware` -- adds extra tools and intercepts their calls
- `RateLimitingMiddleware`, `CachingMiddleware`, `TimingMiddleware`, etc.

#### How They Compose

```
Client request (tools/list)
  |
  v
Middleware pipeline  (auth, logging, rate limiting, ...)
  |
  v
Server (FastMCP)
  |
  v
AggregateProvider
  |-- Provider A + Transforms (Namespace, Visibility, ...)
  |-- Provider B + Transforms
  |-- Provider C + Transforms
  |
  v
Response
```

### Why Provider Is the Right Extension Point

The gateway's core logic maps to a custom `Provider`, not middleware or a plugin system:

| Consideration | Provider | Middleware | Custom Plugin System |
|---|---|---|---|
| Owns component lifecycle | Yes (`lifespan()`) | No | Would need to build |
| Participates in transforms | Yes (Namespace, Visibility, etc.) | No | Would need to build |
| Can manage upstream connections | Yes (via `lifespan()`) | No | Would need to build |
| Users can compose with auth, logging, etc. | Yes (standard FastMCP) | N/A | Would need to build |
| Familiar to FastMCP users | Yes | Yes | No |

`ToolInjectionMiddleware` (30 lines of code in FastMCP) proves the meta-tools pattern works within FastMCP's architecture -- it injects tools into `tools/list` and intercepts their `tools/call`. Our `DiscoveryProvider` does the same thing at the provider level, which is cleaner because providers participate in the full transform pipeline.

---

## 3. Architecture: `DiscoveryProvider` Design

### Core Structure

The gateway is a plain FastMCP server with a custom provider that exposes 3 meta-tools and manages an internal tool registry:

```python
from fastmcp import FastMCP
from fastmcp.server.middleware import AuthMiddleware
from fastmcp_gateway import DiscoveryProvider

gateway = FastMCP(
    "my-gateway",
    providers=[
        DiscoveryProvider(
            upstreams={
                "apollo": "http://apollo-mcp:8080/mcp",
                "hubspot": "http://hubspot-mcp:8080/mcp",
            },
            groups={
                "apollo": ["search", "enrich", "sequences"],
            },
        ),
    ],
    # Standard FastMCP extensibility -- no gateway-specific plugins needed
    middleware=[
        AuthMiddleware(auth=require_scopes("tools")),
    ],
)

# Native tools work alongside discovered tools
@gateway.tool()
async def my_custom_tool(query: str) -> str:
    """A native tool, discoverable through the same meta-tools."""
    ...
```

### What Users Customize via Existing FastMCP Mechanisms

| Concern | FastMCP Mechanism | How |
|---|---|---|
| Auth / RBAC | `AuthMiddleware` | `FastMCP(middleware=[AuthMiddleware(...)])` |
| Logging | `LoggingMiddleware` | Built-in |
| Rate limiting | `RateLimitingMiddleware` | Built-in |
| Tool rename/hide | `ToolTransform` | Standard transform on provider |
| Feature flags | `Visibility` transform | `provider.disable(tags={"beta"})` |
| Observability | OpenTelemetry | Built-in via `server_span` |
| Custom HTTP routes | Starlette routes on `http_app()` | Standard ASGI |
| Runtime admin | `ComponentManager` | `set_up_component_manager(server=mcp)` |

None of these require gateway-specific code. They compose exactly as with any FastMCP server.

### What the Gateway Adds (Extension Points on `DiscoveryProvider`)

These are the gateway-specific customization points -- constructor parameters or overridable methods, not a plugin framework:

1. **Registry organization** -- how upstream tools map to domains/groups.
   - Default: auto-infer from namespace prefix
   - Override: explicit config dict or a callback function

2. **Discovery response format** -- what `discover_tools` returns.
   - Default: name + one-line description
   - Override: config to include tags, annotations, group paths, etc.

3. **Search strategy** -- how `discover_tools(query="...")` matches tools.
   - Default: keyword matching against name + description
   - Override: swap in embedding-based search (Phase 2)

---

## 4. Key Decision Summary

| Decision | Choice | Rationale |
|---|---|---|
| Language | Python | I/O-bound workload; FastMCP ecosystem value; Pydantic stack integration |
| Extension model | FastMCP's Provider + Transform + Middleware | Already composable, typed, lifecycle-aware. Don't reinvent it. |
| Core abstraction | `DiscoveryProvider` (custom Provider subclass) | Owns lifecycle, participates in transforms, clean separation |
| Plugin system | Not needed | FastMCP's 3 layers cover auth, logging, rate limiting, transforms, visibility, and runtime management |
| Gateway-specific extension | Constructor params + overridable methods on `DiscoveryProvider` | Minimal surface area, familiar Python patterns |

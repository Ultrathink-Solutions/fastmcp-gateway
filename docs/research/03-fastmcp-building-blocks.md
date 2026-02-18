# FastMCP Building Blocks Analysis

**Date:** 2026-02-17
**Status:** Research complete

---

## Why Build on FastMCP?

FastMCP (Python) is the most popular MCP server framework. It already provides rich infrastructure for server composition, proxying, dependency injection, and middleware -- but it does NOT provide progressive tool discovery. Building `fastmcp-gateway` as a FastMCP extension means:

1. **Zero new dependencies** for existing FastMCP users
2. **Native DI** via `Depends()`, `CurrentHeaders()`, `get_access_token()`
3. **Works with any MCP client** (PydanticAI, LangChain, Claude Code, OpenAI SDK, etc.)
4. **Middleware composability** with FastMCP's existing pipeline
5. **Server composition** via `mount()` and `MCPConfig`

This document catalogs what FastMCP already provides and identifies what must be built.

---

## What FastMCP Already Provides

### Proxy Server (`FastMCPProxy`)

FastMCP has a complete proxy server built in. `FastMCPProxy` extends `FastMCP` and forwards all requests (tools, resources, prompts) to a backend MCP server.

Key classes:
- `FastMCPProxy` -- A FastMCP server that forwards to a backend via a client factory
- `ProxyToolManager` -- Sources tools from both local registration AND a remote client
- `ProxyTool` -- Executes by forwarding `call_tool` through the remote client
- `StatefulProxyClient` -- Per-session client caching for stateful backends

Created via `FastMCP.as_proxy(backend)`.

### Server Composition (`mount()`)

The `mount()` method creates a live, dynamic link between a parent server and a child server:

```python
gateway = FastMCP("gateway")
gateway.mount(apollo_server, prefix="apollo")     # tools become apollo_*
gateway.mount(hubspot_server, prefix="hubspot")   # tools become hubspot_*
```

Two modes:
- **Direct mounting** (default): In-memory access to child's objects
- **Proxy mounting** (`as_proxy=True`): Full client transport communication

### Config-Driven Composition (`MCPConfig`)

Create proxies from JSON configuration following the MCP config format:

```json
{
  "mcpServers": {
    "apollo": { "url": "http://apollo-mcp:8080/mcp" },
    "hubspot": { "url": "http://hubspot-mcp:8080/mcp" }
  }
}
```

Supports stdio and remote server transports, with tool transformation capabilities (rename, filter by tags, modify arguments).

### Dependency Injection

FastMCP's DI system provides request-scoped injection with automatic schema exclusion:

- `get_context()` -- Current MCP `Context` (logging, progress, sampling)
- `get_http_request()` -- Raw Starlette `Request` object
- `get_http_headers()` -- HTTP headers (strips hop-by-hop headers)
- `get_access_token()` -- Authenticated user's `AccessToken`

**Critical detail**: Dependency parameters are automatically excluded from the MCP tool schema -- the LLM never sees them.

```python
from fastmcp.server.dependencies import Depends, get_http_headers

async def get_user_context(headers: dict = Depends(get_http_headers)):
    return UserContext(
        user_id=headers.get("x-user-id", ""),
        conversation_id=headers.get("x-conversation-id", ""),
    )

@mcp.tool()
async def list_campaigns(
    status: str | None = None,
    user_ctx: UserContext = Depends(get_user_context),  # hidden from LLM
) -> list[dict]:
    ...
```

### Middleware Pipeline

Comprehensive middleware with typed hooks for every MCP operation:

- `on_list_tools`, `on_call_tool`
- `on_list_resources`, `on_read_resource`
- `on_list_prompts`, `on_get_prompt`
- `on_initialize`, `on_message`, `on_request`, `on_notification`

**Key existing middleware:**

| Middleware | What It Does | Relevance |
|---|---|---|
| `ToolInjectionMiddleware` | Injects additional tools into `list_tools` and intercepts their calls | Pattern for injecting meta-tools |
| `PromptToolMiddleware` | Exposes prompts as tools ("X-as-tools" pattern) | Same concept as meta-tools |
| `ResourceToolMiddleware` | Exposes resources as tools | Same pattern |

### Tool Transformation

Rich transformation system for modifying tools at the gateway level:

- `TransformedTool` -- Rename, hide, retype arguments; custom descriptions
- `ArgTransform` -- Per-argument transformation
- `ToolTransformConfig` -- Declarative config-driven transformations

### Tag-Based Filtering

Both `FastMCP` server and `MCPConfig` support `include_tags` / `exclude_tags`:

```python
@mcp.tool(tags={"search", "core"})
async def people_search(...):
    ...
```

### Component Manager (contrib)

HTTP endpoints for runtime enable/disable of tools, resources, and prompts -- with optional auth scope requirements. Essentially an admin API for tool availability.

---

## PydanticAI Toolset Integration

PydanticAI provides composable toolsets that determine what tools the agent sees:

| Toolset | Purpose | Relevance |
|---|---|---|
| `DynamicToolset` | Rebuild toolset per agent step with `RunContext` access | Per-user header injection into gateway connection |
| `FilteredToolset` | Context-dependent tool filtering per step | Could filter based on conversation state |
| `PrefixedToolset` | Add prefix to tool names | Already used for MCP server namespacing |
| `CombinedToolset` | Merge multiple toolsets | Internal composition |
| `FastMCPToolset` | PydanticAI <-> FastMCP bridge | Agent-to-gateway connection |

The `DynamicToolset` is the key primitive for the agent side -- it rebuilds the toolset per run step, allowing per-user headers to flow through to the gateway:

```python
@agent.toolset
def gateway_toolset(ctx: RunContext[ChatDeps]):
    return MCPServerStreamableHTTP(
        settings.tool_gateway_mcp_url,
        headers={
            "x-user-id": ctx.deps.user_id,
            "x-conversation-id": ctx.deps.conversation_id,
        },
    )
```

---

## What Must Be Built (The Gap)

| Component | Description | Complexity |
|---|---|---|
| **Progressive discovery meta-tools** | `discover_tools`, `get_tool_schema`, `execute_tool` -- the 3-tool pattern | Core innovation |
| **Tool registry with domain/group metadata** | Maps `(domain, group, tool_name)` to upstream servers. Hierarchical organization. | Medium |
| **Schema-deferred tool listing** | Return name+description without schema on discovery; full schema on explicit request | Medium |
| **Cross-server execution routing** | Route `execute_tool("apollo_people_search")` to Apollo, `execute_tool("hubspot_search_contacts")` to HubSpot | Medium |
| **Per-upstream graceful degradation** | Apollo being down shouldn't affect HubSpot tools | Medium |

### What Exists But Needs Adaptation

| Building Block | Adaptation Needed |
|---|---|
| `mount()` with proxy | Currently exposes mounted tools directly via `list_tools`. The gateway must suppress mounted tools from the listing and only expose meta-tools, while keeping the full registry accessible internally. |
| `ToolInjectionMiddleware` | Could be inverted: instead of injecting tools INTO the list, replace the entire list with meta-tools while keeping the full registry for `execute_tool` routing. |
| `MCPConfig` | Currently creates one combined proxy. The gateway needs per-server client factories for independent health/lifecycle management. |

---

## Recommended Implementation Approach

**Option C from the design analysis: Plain FastMCP server with internal MCP clients.**

The gateway's job is fundamentally different from a transparent proxy. It is not trying to expose upstream tools directly -- it is exposing a discovery interface. Build the gateway as a plain FastMCP server with 3 `@mcp.tool()` decorators, using `Client` instances internally for upstream communication.

```python
from fastmcp import FastMCP, Client

gateway = FastMCP("fastmcp-gateway")

# Internal: connect to upstreams, build registry
# External: expose only 3 meta-tools

@gateway.tool()
async def discover_tools(domain: str | None = None, group: str | None = None, query: str | None = None) -> list[dict]:
    """Browse available tools by domain, group, or keyword."""
    ...

@gateway.tool()
async def get_tool_schema(tool_name: str) -> dict:
    """Get the full parameter schema for a specific tool."""
    ...

@gateway.tool()
async def execute_tool(tool_name: str, arguments: dict) -> Any:
    """Execute any tool by name. Routes to the appropriate upstream server."""
    ...
```

This approach:
- Doesn't fight against FastMCP's `mount()` system
- Keeps the gateway's tool list at exactly 3
- Uses FastMCP's DI for user context injection
- Uses FastMCP's `Client` for upstream communication
- Can be extended with middleware (logging, auth, rate limiting) using FastMCP's existing pipeline

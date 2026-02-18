# Why Build Something New?

**Date:** 2026-02-17
**Status:** Research complete

---

## The Short Answer

No existing project combines these three things:
1. **Progressive tool discovery** (meta-tools pattern for token-efficient tool access)
2. **MCP gateway** (multi-server aggregation behind a single endpoint)
3. **FastMCP-native** (Python, DI, middleware, Pydantic stack integration)

Each exists in isolation. Nobody has put them together.

---

## The Landscape Gap

### Gateways without discovery

MetaMCP, Microsoft MCP Gateway, and IBM ContextForge are mature gateway projects. They solve multi-server aggregation, auth, RBAC, and namespace management. But when a client connects, they return **every single tool from every upstream server** in one flat list.

At 200+ tools, this exceeds GPT-5.1's 128-tool hard limit and consumes 80-100K tokens of context. The gateways have no mechanism to present tools progressively.

### Discovery without gateways

Speakeasy's Dynamic Toolsets and Claude Code's Tool Search implement progressive discovery. But they are not gateways:
- Speakeasy's implementation is a **proprietary cloud feature** (not available as a library)
- Claude Code's Tool Search is **client-locked** (not available to PydanticAI, LangChain, or other frameworks)

### Open-source attempts

The open-source progressive discovery projects (lazy-mcp, pmcp, MCP-Zero) are immature:
- **lazy-mcp** (71 stars, Go) -- Simple tree navigation, no schema deferral, no Python
- **pmcp** (2 stars, Python) -- Very early, 11 meta-tools (too many), no FastMCP integration
- **MCP-Zero** (446 stars, Python) -- Academic research, not production-ready

---

## Why Not Extend an Existing Gateway?

### MetaMCP
- TypeScript (our stack is Python)
- AGPL-3.0 (copyleft)
- No user context passthrough
- Adding progressive discovery would require rewriting the tool listing pipeline

### Microsoft MCP Gateway
- C# / .NET 8 (our stack is Python)
- Azure-centric (Cosmos DB, Entra ID)
- Tools are HTTP endpoints, not MCP servers
- Fundamentally different architecture (transport proxy, not MCP-aware)

### IBM ContextForge
- Python, but extremely heavy (4800-line db.py, 50+ plugins)
- No progressive discovery infrastructure to build on
- Federation model is schema-only (commented out at runtime)
- Adopting it means adopting their entire data model and admin UI

### lazy-mcp
- Go (no Python/FastMCP integration)
- Simple tree navigation (not the semantic/hierarchical discovery we need)
- No DI, no middleware, no schema deferral

---

## Why FastMCP Is the Right Foundation

FastMCP already provides 80% of the infrastructure we need:

| Capability | FastMCP Has It? | Notes |
|---|---|---|
| MCP server framework | Yes | `FastMCP` class |
| Proxy to upstream servers | Yes | `FastMCPProxy`, `Client` |
| Server composition | Yes | `mount()`, `import_server()` |
| Config-driven setup | Yes | `MCPConfig` |
| Dependency injection | Yes | `Depends()`, `get_http_headers()` |
| Middleware pipeline | Yes | Typed hooks for all MCP operations |
| Tool injection pattern | Yes | `ToolInjectionMiddleware` |
| Tag-based filtering | Yes | `include_tags` / `exclude_tags` |
| Tool transformation | Yes | `TransformedTool`, `ToolTransformConfig` |
| Runtime tool management | Yes | `ComponentManager` (contrib) |
| Progressive discovery | **No** | This is what we build |
| Tool registry with hierarchy | **No** | This is what we build |
| Schema-deferred listing | **No** | This is what we build |
| Cross-server routing | **No** | This is what we build |

Building on FastMCP means:
- **~1000 LOC of core logic** (registry + 3 meta-tools + routing), not a 50,000+ LOC platform
- Native integration with PydanticAI via `DynamicToolset` / `FastMCPToolset`
- Native observability via Logfire/OpenTelemetry
- Composable with any existing FastMCP middleware
- Familiar patterns for anyone who uses FastMCP

---

## The Pydantic Stack Opportunity

The Pydantic ecosystem (Pydantic, PydanticAI, FastMCP, Logfire) is becoming the standard Python AI infrastructure stack:

- **Pydantic** -- Data validation and serialization
- **PydanticAI** -- Agent orchestration and toolsets
- **FastMCP** -- MCP server framework
- **Logfire** -- Observability and tracing

There's a missing piece: **how does the agent efficiently discover tools at scale?**

`fastmcp-gateway` fills this gap as a natural extension of the stack:

```
PydanticAI Agent
    |
    |-- DynamicToolset (per-user headers)
    |       |
    |       v
    |-- fastmcp-gateway (progressive discovery)
    |       |
    |       |-- FastMCP Client -> Apollo MCP Server
    |       |-- FastMCP Client -> HubSpot MCP Server
    |       |-- FastMCP Client -> LinkedIn MCP Server
    |       |-- Native tools (registered directly)
    |       |
    |       |-- Logfire tracing on all operations
    |
    v
  LLM (any model: GPT, Claude, Gemini)
```

---

## Build Strategy

### Phase 1: Internal validation
Build for our production use case (221 tools across 8 MCP servers). Validate progressive discovery works with real enterprise workloads.

### Phase 2: Open-source release
Extract into a standalone pip package. Apache-2.0 license. Generalize configuration, remove internal-specific code.

### Phase 3: Upstream contribution
Propose `ProgressiveDiscoveryMiddleware` as a FastMCP built-in middleware. The gateway server could remain a separate package.

### Phase 4: MCP spec contribution
Once validated with production data, contribute to MCP specification discussions with a reference implementation.

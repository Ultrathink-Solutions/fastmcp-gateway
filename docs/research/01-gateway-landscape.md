# MCP Gateway Landscape Analysis

**Date:** 2026-02-17
**Status:** Research complete

---

## The Problem

Every major AI agent framework (PydanticAI, LangChain, CrewAI, OpenAI Agents SDK) integrates with MCP servers for tool access. As agents connect to more MCP servers, the total tool count grows linearly. This creates two problems:

1. **Hard limits**: GPT-5.1 rejects requests with >128 tools. Gemini has similar limits. Even Claude, which has no hard limit, degrades in accuracy with hundreds of tools in context.
2. **Token overhead**: Each tool definition costs ~400-500 tokens. At 200 tools, that's ~80-100K tokens of schema overhead per request — before the conversation even starts.

The MCP ecosystem has responded with **gateway** projects that aggregate multiple MCP servers behind a single endpoint. But none of them solve the token/tool-count problem — they just make the flat list easier to manage.

---

## Existing Open-Source Gateways

We cloned and reviewed the source code of the three most prominent MCP gateways.

### MetaMCP

- **Repo**: [metatool-ai/metamcp](https://github.com/metatool-ai/metamcp)
- **Language**: TypeScript (Express + Next.js)
- **License**: AGPL-3.0
- **Stars**: ~211

**Architecture**: Two-tier connection pool. `McpServerPool` manages raw connections to individual upstream MCP servers (stdio, SSE, streamable-http). `MetaMcpServerPool` wraps these into aggregated "namespace servers" — each namespace is a virtual MCP server that merges tools from multiple upstreams.

**Namespace model**: PostgreSQL-backed M:N mapping between namespaces and servers. Each namespace gets its own endpoint URL (`/metamcp/{name}/mcp`). Tools are prefixed with `{ServerName}__toolName` for conflict avoidance.

**Middleware**: Functional middleware chain for `tools/list` and `tools/call` — tool filtering (active/inactive per namespace) and tool overrides (rename, description changes per namespace). No middleware for prompts or resources.

**Strengths**:
- Best namespace model — M:N mapping between namespaces and servers is flexible
- Tool rename/override per-namespace via `namespace_tool_mappings`
- `ToolsSyncCache` with SHA-256 hashing avoids unnecessary DB writes
- Defensive error handling (`Promise.allSettled` for fan-out operations)

**Critical gaps**:
- **No progressive discovery** — `tools/list` fans out to ALL upstream servers and returns ALL tools. No caching of aggregated results.
- **No user context passthrough** — `MetaMCPHandlerContext` only carries `namespaceUuid` + `sessionId`. Upstream servers always see "metamcp-client", never the end user.
- **Single-instance only** — All pools/caches are in-memory singletons. No horizontal scaling.
- **Near-zero test coverage** — Only 1 test file in the entire backend.
- **AGPL-3.0** — Copyleft license limits enterprise adoption.

---

### Microsoft MCP Gateway

- **Repo**: [microsoft/mcp-gateway](https://github.com/microsoft/mcp-gateway)
- **Language**: C# / .NET 8
- **License**: MIT

**Architecture**: HTTP reverse proxy + separate Tool Gateway Router service. The gateway doesn't parse MCP protocol messages — it's a transport-level proxy with session-affinity routing. All MCP-level intelligence (tool listing, dispatch) lives in the Tool Gateway Router.

**K8s integration**: Each MCP server deploys as a StatefulSet with headless services for pod-level DNS addressing. `AdapterKubernetesNodeInfoProvider` uses K8s watch API for real-time pod discovery. Security contexts properly locked down (non-root, dropped capabilities).

**Session management**: Session ID extracted from query params or `mcp-session-id` header. New sessions get random pod assignment; existing sessions are routed to the same pod via distributed session store (Redis dev / Cosmos DB prod).

**Strengths**:
- Production K8s patterns (StatefulSets, headless services, pod watch API)
- Clean control plane / data plane separation
- Entra ID RBAC with user identity forwarded via `X-Mcp-UserId` / `X-Mcp-Roles` headers
- The `mcp-proxy` pattern for bridging stdio MCP servers to HTTP

**Critical gaps**:
- **No progressive discovery** — `StorageToolDefinitionProvider` loads ALL tool definitions with no pagination (`NextCursor` always null).
- **Tools are NOT MCP servers** — Tool endpoints are plain HTTP POST handlers, not full MCP servers. Can't maintain state, push notifications, or expose resources.
- **Azure-centric** — Cosmos DB, Entra ID, ACR, AKS. Requires significant adaptation for non-Azure environments.
- **Single namespace** — No namespace-per-tenant isolation.
- **No rate limiting or circuit breaking**.

---

### IBM ContextForge

- **Repo**: [IBM/mcp-context-forge](https://github.com/IBM/mcp-context-forge)
- **Language**: Python
- **License**: Apache-2.0

**Architecture**: The most enterprise-complete gateway. FastAPI-based with SQLAlchemy ORM, virtual servers, team-based RBAC, and federation support.

**Scale**: Massive codebase — `db.py` alone is 4800+ lines. 50+ plugins, 16 middleware files (auth, RBAC, compression, correlation ID, token scoping, security headers, etc.), 610 test files.

**Federation**: `federation_source` columns exist on every entity (Tool, Resource, Prompt, Gateway), with a `Gateway` ORM model for federated peers. However, the actual federation relationships are **commented out** — this is schema-level preparation, not a working feature.

**Strengths**:
- Most complete enterprise feature set (RBAC, teams, audit, admin UI)
- Comprehensive middleware pipeline
- Apache-2.0 license
- 610 test files — serious test coverage

**Critical gaps**:
- **No progressive discovery** — All "lazy load" references are SQLAlchemy ORM lazy-loading, not tool discovery.
- **Extremely heavy** — Overkill for teams that just need progressive discovery.
- **Federation not operational** — Federated relationships are commented out in the ORM.

---

## Other Notable Gateways

| Gateway | Language | Key Feature | Progressive Discovery? |
|---------|----------|-------------|----------------------|
| [MCPJungle](https://github.com/mcpjungle/MCPJungle) | Go | `server__tool` namespacing, PostgreSQL store | No |
| [mcpproxy-go](https://github.com/mcp-proxy/mcpproxy-go) | Go | BM25 keyword relevance filtering | **Partial** — keyword filtering, not full meta-tools |
| [mcp-proxy](https://github.com/TBXark/mcp-proxy) | Go | SSE + streamable-http thin proxy | No |
| [mcp-gateway (Lasso)](https://pypi.org/project/mcp-gateway/) | Python | Security guardrails plugins (PII, prompt injection) | No |
| [Obot](https://github.com/obot-platform/obot) | Go | Full AI platform with tool management | No |

---

## Summary: What Gateways Solve and What They Don't

**Solved by existing gateways:**
- Multi-server aggregation behind a single endpoint
- Namespace/prefix-based tool organization
- Auth and RBAC at the gateway level
- K8s deployment patterns

**NOT solved by any existing gateway:**
- Progressive tool discovery (meta-tools pattern)
- Schema-deferred loading (send name+description first, schema on demand)
- Token-efficient tool presentation regardless of total tool count
- Framework-native DI integration (FastMCP `Depends()` / `CurrentHeaders()`)
- Native Pydantic stack integration (FastMCP + PydanticAI + Logfire)

This gap is the motivation for `fastmcp-gateway`.

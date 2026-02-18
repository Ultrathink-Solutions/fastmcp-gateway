# fastmcp-gateway Requirements

**Date:** 2026-02-17
**Status:** Draft

---

## Overview

`fastmcp-gateway` is a progressive tool discovery gateway for MCP, built on FastMCP. It aggregates tools from multiple upstream MCP servers and native Python functions, exposing them through a small set of meta-tools that enable LLMs to discover and invoke tools on-demand rather than receiving all tool schemas upfront.

---

## Functional Requirements

### FR-1: Progressive tool discovery via meta-tools

The gateway exposes exactly 3 tools to the LLM:

1. **`discover_tools`** -- Browse available tools by domain, group, or keyword. Returns lightweight summaries (name + one-line description), NOT full schemas.
2. **`get_tool_schema`** -- Get the full JSON Schema for a specific tool's parameters. Called after discovery, before execution.
3. **`execute_tool`** -- Execute any tool by name. Routes to the appropriate upstream MCP server or native function.

Without filters, `discover_tools` returns a domain-level summary (table of contents) showing available domains, tool counts, and group names.

### FR-2: Multi-server aggregation

The gateway connects to multiple upstream MCP servers as a client, aggregating their tools into a unified registry. Each upstream server is a "domain" in the discovery hierarchy.

### FR-3: Domain and group organization

Tools are organized hierarchically:
- **Domain**: The upstream server or functional area (e.g., "apollo", "hubspot", "native")
- **Group**: A functional category within a domain (e.g., "search", "crm", "sequences")

Groups can be auto-discovered from upstream server metadata or configured explicitly.

### FR-4: Native tool support

Python functions can be registered directly on the gateway as tools, alongside MCP-proxied tools. Native tools are discoverable and executable through the same meta-tools interface. Native tools support FastMCP's DI system (`Depends()`, `get_http_headers()`, etc.).

### FR-5: Transparent upstream routing

`execute_tool` routes calls to the appropriate upstream MCP server without the LLM needing to know which server hosts which tool. The gateway maintains a mapping from tool names to upstream connections.

### FR-6: Schema-deferred loading

Tool schemas are NOT included in `discover_tools` responses. Schemas are only returned when explicitly requested via `get_tool_schema`. This is the key token-saving mechanism.

### FR-7: Graceful degradation

If an upstream MCP server is unreachable, its tools should be absent from discovery results but the gateway remains operational for all other domains. Connection failures should be logged, not propagated.

---

## Non-Functional Requirements

### NFR-1: Startup-time registry population

The tool registry is populated at gateway startup by calling `list_tools()` on each upstream server. No `list_tools()` calls during request handling (except for cache refresh).

### NFR-2: Registry refresh

The gateway supports re-discovery via:
- Configurable polling interval
- Manual trigger (admin endpoint or API call)
- MCP `notifications/tools/list_changed` (when supported by upstream)

### NFR-3: FastMCP-native

The gateway is a FastMCP server. It uses FastMCP's DI, middleware, transport, and tool registration systems. It should feel like a natural extension of FastMCP, not a separate framework.

### NFR-4: Framework-agnostic client support

Any MCP client can connect to the gateway -- PydanticAI, LangChain, Claude Code, OpenAI Agents SDK, or raw MCP clients. The gateway is a standard MCP server; the progressive discovery pattern is implemented entirely server-side.

### NFR-5: Model-agnostic

The gateway works with any LLM provider. The 3 meta-tools are well within every model's tool limit (GPT-5.1: 128, Claude: no limit, Gemini: ~128).

### NFR-6: Observability

All tool discovery and execution calls are traceable via OpenTelemetry/Logfire. The gateway provides a single observability surface for all tool interactions across all upstream servers.

### NFR-7: Minimal configuration

The gateway should be usable with minimal configuration:

```python
from fastmcp_gateway import GatewayServer

gateway = GatewayServer({
    "apollo": "http://apollo-mcp:8080/mcp",
    "hubspot": "http://hubspot-mcp:8080/mcp",
})
gateway.run()
```

More advanced configuration (groups, native tools, auth, DI) should be available but not required.

### NFR-8: Composable with FastMCP middleware

The gateway should work with any existing FastMCP middleware (auth, logging, rate limiting, etc.) without special handling.

---

## Token Budget Analysis

Based on [Speakeasy's benchmarks](https://www.speakeasy.com/blog/100x-token-reduction-dynamic-toolsets):

| Approach | Tools in context | Schema tokens per request |
|----------|-----------------|--------------------------|
| All tools flat | 200 | ~80K-100K |
| Server-side groups | ~60-80 | ~24K-32K |
| Client-side filtering | ~40-80 | ~16K-32K |
| **Progressive discovery** | **3** | **~2K initial, +1-2K per discovered tool** |

Typical interaction requiring tools from two domains:

```
Step 1: discover_tools()                              -> domain summary (~500 tokens)
Step 2: discover_tools(domain="sec_edgar")            -> 14 tool summaries (~700 tokens)
Step 3: discover_tools(domain="apollo", group="core") -> 3 tool summaries (~200 tokens)
Step 4: get_tool_schema("sec_edgar_search_filings")   -> 1 schema (~400 tokens)
Step 5: get_tool_schema("apollo_org_enrichment")      -> 1 schema (~400 tokens)
Step 6: execute_tool("sec_edgar_search_filings", ...) -> results
Step 7: execute_tool("apollo_org_enrichment", ...)    -> results

Total schema tokens loaded: ~2,200 (vs ~100,000 with flat loading)
Extra round-trips: 4 (steps 1-5 before first execution)
```

On subsequent messages in the same conversation, the LLM already has schemas in history and can skip discovery.

---

## Open Design Questions

These require further discussion and experimentation:

1. **Upstream connection lifecycle.** Persistent connections vs. connect-per-call to upstream servers. In-cluster connections may not use TLS, so per-call overhead is minimal.

2. **Semantic search.** Should `discover_tools` support embedding-based semantic search in addition to keyword/group filtering? This would require embedding infrastructure (sentence-transformers, FAISS). Could be a Phase 2 feature.

3. **System prompt guidance.** The LLM needs instructions on how to use meta-tools effectively. How much prompt engineering is needed? Should the gateway provide a recommended system prompt snippet?

4. **Conversation history cost.** After `discover_tools` and `get_tool_schema` calls, those results are in conversation history. Over long conversations, does accumulated discovery data significantly increase context usage?

5. **Group auto-discovery.** Can groups be inferred from tool name prefixes, tags, or upstream server metadata? Or must they be explicitly configured?

6. **Registry as MCP resource.** Should the tool registry be exposed as an MCP resource (in addition to the `discover_tools` tool)? This would allow clients to read the full registry without tool calls.

7. **Authentication passthrough.** How should user credentials flow from the client, through the gateway, to upstream servers? Options: HTTP headers (simple), token exchange (secure), no passthrough (gateway handles all auth).

# Community Anchors: Specs, Best Practices, and Reference Architectures

**Date:** 2026-02-17
**Status:** Research complete

---

## Purpose

Before designing `fastmcp-gateway`, we surveyed the MCP specification, active proposals, production implementations, and community best practices to identify anchoring points. This document captures what the community is converging on so our design stays aligned.

---

## 1. The Meta-Tool Pattern Is the De Facto Standard

Every serious progressive discovery implementation converges on 2-4 meta-tools:

| Implementation | Meta-Tools | Status |
|---|---|---|
| Anthropic Tool Search API | `tool_search` (regex or BM25) + `defer_loading` flag | Production (API-level) |
| Speakeasy Dynamic Toolsets v2 | `search_tools` + `describe_tools` + `execute_tool` | Production (proprietary) |
| Claude Code MCPSearch | 1 search meta-tool + `defer_loading` flag | Production (client-locked) |
| lazy-mcp | `get_tools_in_category` + `execute_tool` | Early production |
| MCP spec proposals (#1978, #1923) | `tools/list_summary` + `tools/get_schema` | Draft proposals |

**Consensus:** The 3-tool pattern (search/browse, describe, execute) is the sweet spot. Two feels too constrained (forces schema into discovery). Eleven is too many (pmcp's mistake -- increases LLM cognitive load).

---

## 2. Anthropic's `defer_loading` + `tool_reference` API

The most significant anchoring point. Anthropic built progressive discovery into the **Claude API itself** (not just Claude Code).

### How It Works

1. Tools are marked with `defer_loading: true` in the API request
2. Deferred tools are sent to the API but excluded from the LLM's context
3. A built-in `tool_search_tool` (regex or BM25) lets Claude discover deferred tools
4. Search results return `tool_reference` blocks that the API **automatically expands** into full schemas
5. The LLM can then invoke the discovered tools normally

### Key API Details

**Two search variants:**

| Variant | Type String | Query Format |
|---------|-------------|--------------|
| Regex | `tool_search_tool_regex_20251119` | Python `re.search()` patterns |
| BM25 | `tool_search_tool_bm25_20251119` | Natural language queries |

**Custom tool search:** Any tool can return `tool_reference` blocks -- not just the built-in search. This means our gateway's `discover_tools` could return `tool_reference` blocks when used with Claude-based clients:

```json
{
  "type": "tool_result",
  "tool_use_id": "toolu_your_tool_id",
  "content": [
    { "type": "tool_reference", "tool_name": "discovered_tool_name" }
  ]
}
```

Every referenced tool must have a corresponding definition with `defer_loading: true` in the top-level `tools` parameter.

**MCP integration (beta):**

```json
{
  "type": "mcp_toolset",
  "mcp_server_name": "database-server",
  "default_config": { "defer_loading": true },
  "configs": {
    "search_events": { "defer_loading": false }
  }
}
```

**Limits:** Up to 10,000 tools in catalog. Returns 3-5 most relevant tools per search.

**Performance:**
- 85% token reduction (77K -> 8.7K with 50+ tools)
- Accuracy improvement: Opus 4 from 49% to 74%, Opus 4.5 from 79.5% to 88.1%

### Implication for fastmcp-gateway

The gateway should support **two paths**:
1. **Claude clients**: Participate in the native `tool_reference` + `defer_loading` system
2. **All other clients**: Use our 3 meta-tools for model-agnostic progressive discovery

**Sources:**
- [Tool Search Tool API Docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)
- [Introducing Advanced Tool Use](https://www.anthropic.com/engineering/advanced-tool-use)

---

## 3. MCP Specification: Current State

### What the Spec Provides Today

| Feature | Spec Status | Relevance |
|---|---|---|
| `_meta` field on tools | Released (2025-06-18) | Extensibility point for groups/categories |
| Cursor-based pagination on `tools/list` | Released | Lazy fetching from upstreams |
| `notifications/tools/list_changed` | Released | Dynamic tool sets |
| Tool annotations (`readOnlyHint`, `destructiveHint`, etc.) | Released | Behavioral metadata |
| `title` field (separate from `name`) | Released (2025-06-18) | Human-readable display |
| `outputSchema` for structured results | Released (2025-06-18) | Validation and transformation |
| Extensions framework (SEP-2133) | Finalized | Custom capability advertisement |

### What the Spec Does NOT Have

- No `query` or `filter` parameter on `tools/list`
- No `tools/search`, `tools/discover`, or `tools/get_schema` methods
- No groups, categories, or tags
- No minimal/summary listing mode (always returns full schema)
- No tool dependency declarations

### The `_meta` Extensibility Escape Hatch

The `_meta` field is the spec-blessed way to attach arbitrary metadata to tools:

```json
{
  "name": "apollo_people_search",
  "title": "Search People",
  "description": "Search Apollo's people database",
  "inputSchema": { ... },
  "_meta": {
    "io.modelcontextprotocol/groups": ["apollo", "search"],
    "com.ultrathink/priority": 1
  }
}
```

Keys use reverse domain notation. Keys beginning with `modelcontextprotocol` or `mcp` are reserved for official use.

**Sources:**
- [MCP Tools Spec 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [SEP-986: Tool Name Format](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/986)

---

## 4. Active MCP Specification Proposals

### SEP-2084: Primitive Grouping (Most Relevant)

The most advanced proposal for tool organization. Emerged from the highly-discussed SEP-1300 (59 comments).

**Proposes:**
- `Group` as a first-class MCP primitive
- `groups/list` request (paginated)
- `notifications/groups/list_changed` notification
- Group membership via `_meta["io.modelcontextprotocol/groups"]` -- array of group name strings
- Multi-group membership and nested groups

**Status:** Open SEP with TypeScript SDK reference implementation (PR #1399). Not in released spec.

### Primitive Grouping Interest Group

An official MCP Interest Group meeting weekly (Mondays 11:30am ET). Facilitated by members from UW, GitHub, and Futurescale/MCP Core.

**Status:** Active exploration. Must achieve consensus + two Core Maintainer sponsors to graduate to Working Group.

**Repo:** [modelcontextprotocol/experimental-ext-grouping](https://github.com/modelcontextprotocol/experimental-ext-grouping)

### Other Active Proposals

| Proposal | What It Does | Status |
|---|---|---|
| SEP-1821 | Adds `query` string param to `tools/list` | Open, controversial |
| #1978 | `tools/list_summary` + `tools/get_schema` (lazy hydration) | Open, low traction |
| SEP-2127 | Server Cards (`.well-known/mcp/server-card.json`) | Open |
| SEP-2053 | Server Variants (different tool surfaces per config) | Open |
| SEP-2076 | Agent Skills (multi-step workflow instructions) | Open |
| SEP-1881 | Scope-filtered tool discovery (OAuth-based) | Open |

### Skills Over MCP Interest Group

Explores "agent skills" -- higher-level workflow instructions discoverable through MCP. Orthogonal to progressive discovery but related.

**Repo:** [modelcontextprotocol/experimental-ext-skills](https://github.com/modelcontextprotocol/experimental-ext-skills)

**Sources:**
- [SEP-2084 (Primitive Grouping)](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/2084)
- [SEP-1300 (Groups & Tags)](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1300)
- [SEP-1821 (Dynamic Discovery)](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1821)
- [Issue #1978 (Lazy Hydration)](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1978)

---

## 5. Production Implementations: Detailed Findings

### Anthropic: Claude Code MCPSearch

**Shipped:** v2.1.7, January 14, 2026

**Trigger:** Activates when MCP tool descriptions exceed 10% of context window (configurable via `ENABLE_TOOL_SEARCH=auto:N`).

**Implementation:** Uses the Anthropic API's `tool_search_tool` with `defer_loading: true` on MCP tools. Not a client-side search -- the API handles search and schema expansion server-side.

**Recommendation:** Keep 3-5 most frequently used tools as non-deferred.

**Sources:**
- [Claude Code MCP Docs](https://code.claude.com/docs/en/mcp)
- [GitHub Issue #7336](https://github.com/anthropics/claude-code/issues/7336)

### Speakeasy Dynamic Toolsets v2

**Two discovery variants:**

1. **Progressive Discovery:** `list_tools` (hierarchical prefix browsing) + `describe_tools` + `execute_tool`
2. **Semantic Search:** `search_tools` (embeddings-based) + `describe_tools` + `execute_tool`

**Performance (400-tool server):**

| Approach | Initial Tokens | Query Tokens | Total |
|----------|---------------|-------------|-------|
| Progressive Discovery | 2,500 | 2,700 | 5,200 |
| Semantic Search | 1,300 | 3,400 | 4,700 |
| Static (all tools) | 405,100 | N/A | Exceeds context |

**Key finding:** Schema deferral is the critical insight. Tool schemas represent 60-80% of token consumption. Separating name+description from full schema is universally recommended.

**Availability:** Proprietary cloud feature. Not available as a library.

**Sources:**
- [How we reduced token usage by 100x](https://www.speakeasy.com/blog/how-we-reduced-token-usage-by-100x-dynamic-toolsets-v2)
- [Progressive Discovery vs Semantic Search](https://www.speakeasy.com/blog/100x-token-reduction-dynamic-toolsets)

---

## 6. Community Best Practices

### Anthropic's Tool Design Guidance

From [Writing Effective Tools for AI Agents](https://www.anthropic.com/engineering/writing-tools-for-agents):

1. **Consolidate related operations** into higher-level tools -- replace `list_users` + `list_events` + `create_event` with a unified `schedule_event`
2. **"More tools don't always lead to better outcomes"** -- build fewer, more intentional tools
3. **Return high-signal information** -- exclude `uuid`, `256px_image_url`, `mime_type`; include `name`, `image_url`, `file_type`
4. **Flexible response formats** -- expose a `response_format` enum (`"concise"` vs `"detailed"`)
5. **Prefer `search_contacts` over `list_contacts`** -- agents waste context iterating large datasets
6. **Helpful error messages** -- transform opaque error codes into specific, actionable feedback with examples

### Tool Naming Conventions

- **90%+ of tools use snake_case** -- GPT-4o tokenization handles it best
- **Vendor prefix for namespacing**: `github_create_issue`, `slack_send_message`
- **Accepted spec format (SEP-986):** `[a-zA-Z0-9_-./]`, 1-64 chars, case-sensitive
- **Double underscore separator** (`server__tool`) used by Docker MCP Gateway, MetaMCP as namespace delimiter

### Simon Willison's Perspectives

- Quantified the token cost problem: GitHub MCP alone (93 tools) consumes ~55K tokens
- Advocates CLI tools over MCP for coding agents (near-zero token cost)
- Identified the "lethal trifecta" security pattern: private data + untrusted content + external comms
- Progressive discovery helps reduce lethal trifecta surface area by limiting loaded tools

**Sources:**
- [Too Many MCPs](https://simonwillison.net/2025/Aug/22/too-many-mcps/)
- [Agentic Engineering](https://simonwillison.net/2025/Oct/14/agentic-engineering/)
- [MCP Server Naming Conventions](https://zazencodes.com/blog/mcp-server-naming-conventions)

### OpenAI Guidance

- `allowed_tools` parameter to restrict callable tools per request (maximizes prompt caching)
- RAG pipeline for tool selection recommended over giving model all tools
- Flatten tool parameters -- deeply nested argument trees degrade performance
- Tool selection accuracy degrades with scale

### Other Notable Patterns

- **Composio Tool Router**: 850+ toolkits / 11,000+ tools. Uses `Planner` + `Search` meta-tools
- **AWS AgentCore Gateway**: Token-based workflow control with tool ordering and dependency management
- **FastMCP 3.0 session-scoped visibility**: `ctx.enable_components(tags={"premium"})` for per-session progressive disclosure

---

## 7. Design Anchoring Points for fastmcp-gateway

Based on this research, the following are our concrete anchoring points:

### Anchor 1: 3-Tool Meta-Tool Shape

`discover_tools` + `get_tool_schema` + `execute_tool` matches community consensus. Do not deviate.

### Anchor 2: Dual-Path Client Support

- **Claude clients:** Participate in native `tool_reference` + `defer_loading` system when possible
- **All other clients:** Use meta-tools for model-agnostic progressive discovery

### Anchor 3: `_meta` for Group/Category Metadata

Use `_meta["io.modelcontextprotocol/groups"]` for tool organization. This is the key proposed by SEP-2084 and the Primitive Grouping IG. Forward-compatible with the spec's direction.

### Anchor 4: SEP-986 Tool Naming

Use the accepted tool name format: `[a-zA-Z0-9_-./]`, 1-64 chars. Supports hierarchical naming via dots/slashes.

### Anchor 5: Emit `notifications/tools/list_changed`

Declare `listChanged: true` and forward upstream change notifications. Already in spec; clients expect it.

### Anchor 6: FastMCP 3.0 Provider/Transform Architecture

Design for eventual implementation as a `ProgressiveDiscoveryProvider` or transform layer. Positions for upstream FastMCP contribution (Phase 3).

### Anchor 7: Anthropic Tool Design Principles

Apply to our meta-tools and to guidance for upstream MCP server authors:
- Consolidate over proliferate
- High-signal responses with flexible format
- Helpful, actionable error messages
- Search over list patterns

---

## 8. Strategic Assessment

**The MCP spec is not converging on a progressive discovery standard anytime soon.** The Interest Groups are still in exploration phase. This gives us freedom to build what works, with low risk of the spec contradicting us -- as long as we use extensibility points (`_meta`, `notifications/tools/list_changed`, pagination) rather than inventing incompatible protocol extensions.

**The meta-tool pattern is validated.** Every production implementation uses it. The only question is implementation details (search algorithm, response format, grouping model).

**Anthropic's `defer_loading` is the closest thing to a standard.** It's production, API-level, and supports custom search implementations. We should be compatible with it.

# Meta-Tool Schema Design

**Date:** 2026-02-18
**Status:** Design complete
**Source:** Analysis of 7 progressive discovery implementations (Claude Code, Block/Square, Speakeasy, Lazy-MCP, ProDisco, SEP-1888, MCP Discussion #532)

---

## Overview

The gateway exposes exactly 3 tools to the LLM. These schemas are the core API surface -- every LLM interaction passes through them. The design must balance:

- **Token efficiency**: Schemas themselves consume tokens; keep them compact
- **LLM usability**: Clear descriptions, intuitive parameters, predictable responses
- **Expressiveness**: Support both browsing and searching
- **Error clarity**: LLMs need actionable error messages to self-correct

---

## 1. Prior Art Analysis

### Schema Patterns Across Implementations

| Implementation | Discovery Tool | Schema Tool | Execution Tool | Total Tools |
|---|---|---|---|---|
| **Speakeasy** | `search_tools` (semantic search) | `describe_tools` (schema on demand) | `execute_tool` | 3 |
| **Block/Square** | `get_service_info` (by service) | `get_type_info` (by service + method) | `make_api_request` | 3 |
| **Claude Code** | `tool_search` (regex or BM25) | N/A (returns full tool refs) | N/A (uses native tool calls) | 1 |
| **Lazy-MCP** | `get_tools_in_category` (path nav) | N/A (descriptions only) | `execute_tool` | 2 |
| **ProDisco** | `searchTools` (filters + text) | N/A (inline in search results) | `runSandbox` (code exec) | 2 |
| **SEP-1888** | Single tool with `operations` / `types` modes | (same tool, `types` mode) | N/A (external) | 1 |
| **MCP #532** | `tools/categories` + `tools/discover` | `tools/load` | N/A (native) | 3-4 |

### Key Design Lessons from Prior Art

1. **3 tools is the right number.** Speakeasy and Block/Square both converge on 3 with clear separation of concerns. Claude Code uses 1 but only because it controls the client-side tool loading mechanism. SEP-1888's dual-mode single tool overloads the schema.

2. **Separate discovery from schema loading.** Speakeasy's key insight: tool schemas represent 60-80% of token usage. Returning descriptions without schemas in the discovery step, then loading schemas on demand, is the core efficiency mechanism.

3. **Hierarchical browsing AND search.** Lazy-MCP uses only path navigation (no search). Speakeasy uses only semantic search (no browsing). The best experience supports both -- Block/Square demonstrates this with service-level browsing plus method-level detail.

4. **Responses must be compact.** The LLM receives these in its context window. Every token in the response counts. Prior art responses range from 50 tokens (category list) to 500 tokens (full schema) per call.

5. **Error messages guide next action.** Block/Square's layer pattern uses descriptions like "Call me before trying to get type info" to teach the LLM the intended workflow.

---

## 2. Schema Design: `discover_tools`

### Purpose

Browse and search the tool registry. Returns lightweight summaries (name + one-line description), NOT full schemas.

### Tool Definition

```python
@gateway.tool(
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
async def discover_tools(
    domain: str | None = None,
    group: str | None = None,
    query: str | None = None,
) -> str:
    """Browse available tools by domain, group, or keyword.

    Call with no arguments to see all available domains and their tool counts.
    Call with a domain to see groups and tools within that domain.
    Call with a domain and group to see tools in that specific group.
    Call with a query to search across all tools by keyword.
    """
```

### JSON Schema (what the LLM sees)

```json
{
  "name": "discover_tools",
  "description": "Browse available tools by domain, group, or keyword.\n\nCall with no arguments to see all available domains and their tool counts.\nCall with a domain to see groups and tools within that domain.\nCall with a domain and group to see tools in that specific group.\nCall with a query to search across all tools by keyword.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "domain": {
        "type": "string",
        "description": "Filter by domain (e.g., 'apollo', 'hubspot'). Omit to see all domains."
      },
      "group": {
        "type": "string",
        "description": "Filter by group within a domain (e.g., 'search', 'crm'). Requires domain."
      },
      "query": {
        "type": "string",
        "description": "Search tools by keyword across names and descriptions."
      }
    }
  }
}
```

**Schema token cost**: ~120 tokens (compact, no required fields)

### Response Formats

**No arguments** -- Domain summary (table of contents):

```json
{
  "domains": [
    {
      "name": "apollo",
      "description": "Apollo.io CRM and sales intelligence",
      "tool_count": 14,
      "groups": ["people", "organizations", "sequences"]
    },
    {
      "name": "hubspot",
      "description": "HubSpot CRM and marketing",
      "tool_count": 22,
      "groups": ["contacts", "deals", "emails", "workflows"]
    },
    {
      "name": "sec_edgar",
      "description": "SEC EDGAR financial filings",
      "tool_count": 8,
      "groups": ["search", "filings", "companies"]
    }
  ],
  "total_tools": 44
}
```

**Response tokens**: ~150-300 depending on number of domains

**With domain** -- Tools in that domain:

```json
{
  "domain": "apollo",
  "tools": [
    {"name": "apollo_people_search", "group": "people", "description": "Search for people by name, title, company, or other criteria"},
    {"name": "apollo_people_enrich", "group": "people", "description": "Enrich a person record with full contact and company data"},
    {"name": "apollo_org_search", "group": "organizations", "description": "Search for organizations by name, industry, or size"},
    {"name": "apollo_org_enrich", "group": "organizations", "description": "Enrich an organization with firmographic data"},
    {"name": "apollo_sequence_list", "group": "sequences", "description": "List available email sequences"},
    {"name": "apollo_sequence_add", "group": "sequences", "description": "Add a contact to an email sequence"}
  ]
}
```

**Response tokens**: ~30-50 per tool (name + group + one-line description)

**With domain + group** -- Tools in that group:

```json
{
  "domain": "apollo",
  "group": "people",
  "tools": [
    {"name": "apollo_people_search", "description": "Search for people by name, title, company, or other criteria"},
    {"name": "apollo_people_enrich", "description": "Enrich a person record with full contact and company data"},
    {"name": "apollo_people_bulk_enrich", "description": "Enrich multiple person records in a single call"}
  ]
}
```

**With query** -- Search results across all domains:

```json
{
  "query": "search contacts",
  "results": [
    {"name": "apollo_people_search", "domain": "apollo", "group": "people", "description": "Search for people by name, title, company, or other criteria"},
    {"name": "hubspot_contacts_search", "domain": "hubspot", "group": "contacts", "description": "Search HubSpot contacts by name, email, or properties"},
    {"name": "hubspot_contacts_filter", "domain": "hubspot", "group": "contacts", "description": "Filter contacts by property values and list membership"}
  ]
}
```

### Error Responses

```json
{"error": "Unknown domain 'salesforce'. Available domains: apollo, hubspot, sec_edgar"}
```

```json
{"error": "Unknown group 'crm' in domain 'apollo'. Available groups: people, organizations, sequences"}
```

Errors include the valid options so the LLM can self-correct without an additional round-trip.

### Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| No required parameters | All optional | Enables zero-argument "table of contents" call |
| Flat parameters (not nested `filters` object) | `domain`, `group`, `query` at top level | Simpler for LLMs; nested objects increase error rate |
| `query` is keyword, not semantic | Phase 1: substring matching | No embedding infrastructure needed; semantic search is Phase 2 |
| Groups in domain summary | Include group names | LLM can skip directly to `domain+group` without an extra call |
| One-line descriptions | Max ~80 chars | Compact enough for 50+ tools without overwhelming context |
| Domain+group required together | `group` requires `domain` | Groups are scoped to domains; cross-domain groups are confusing |

---

## 3. Schema Design: `get_tool_schema`

### Purpose

Retrieve the full JSON Schema for a specific tool's input parameters. Called after `discover_tools` identifies the needed tool, before calling `execute_tool`.

### Tool Definition

```python
@gateway.tool(
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
async def get_tool_schema(
    tool_name: str,
) -> str:
    """Get the full parameter schema for a specific tool.

    Call this after discover_tools to get the complete input schema
    before calling execute_tool. Returns the JSON Schema that describes
    what arguments the tool accepts.
    """
```

### JSON Schema (what the LLM sees)

```json
{
  "name": "get_tool_schema",
  "description": "Get the full parameter schema for a specific tool.\n\nCall this after discover_tools to get the complete input schema before calling execute_tool. Returns the JSON Schema that describes what arguments the tool accepts.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "tool_name": {
        "type": "string",
        "description": "The tool name as returned by discover_tools (e.g., 'apollo_people_search')."
      }
    },
    "required": ["tool_name"]
  }
}
```

**Schema token cost**: ~80 tokens (single required parameter)

### Response Format

```json
{
  "name": "apollo_people_search",
  "domain": "apollo",
  "group": "people",
  "description": "Search for people by name, title, company, or other criteria. Returns matching person records with contact information.",
  "parameters": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string",
        "description": "Person's full name or partial name to search for"
      },
      "title": {
        "type": "string",
        "description": "Job title to filter by (e.g., 'VP Sales', 'Engineer')"
      },
      "company": {
        "type": "string",
        "description": "Company name to filter by"
      },
      "location": {
        "type": "string",
        "description": "Location to filter by (city, state, or country)"
      },
      "limit": {
        "type": "integer",
        "description": "Maximum results to return (default: 10, max: 100)",
        "default": 10
      }
    },
    "required": ["name"]
  }
}
```

**Response tokens**: ~200-500 per tool (varies with schema complexity)

### Error Responses

```json
{"error": "Unknown tool 'apollo_search'. Did you mean 'apollo_people_search' or 'apollo_org_search'? Use discover_tools to browse available tools."}
```

Fuzzy matching on the tool name provides suggestions. The error guides the LLM back to `discover_tools` if the name is completely wrong.

### Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Single `tool_name` parameter | No batch support | One schema at a time keeps responses focused; LLMs can call multiple times |
| Return full JSON Schema | Standard JSON Schema format | LLMs are trained on JSON Schema; non-standard formats increase error rates |
| Include `description` in response | Longer description than in `discover_tools` | The full description helps the LLM understand the tool before calling it |
| Include `domain` and `group` | Contextual metadata | Helps the LLM understand where this tool fits |
| Fuzzy match on error | Suggest similar names | Reduces round-trips when the LLM gets a name slightly wrong |
| No `outputSchema` by default | Omit unless upstream provides it | Most MCP tools don't declare output schemas; including empty ones wastes tokens |

---

## 4. Schema Design: `execute_tool`

### Purpose

Execute any tool by name, routing to the appropriate upstream MCP server. This is the actual work tool.

### Tool Definition

```python
@gateway.tool(
    annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True),
)
async def execute_tool(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> str:
    """Execute a tool by name with the given arguments.

    Use discover_tools to find available tools, then get_tool_schema
    to see what arguments a tool accepts, then call this to execute it.
    """
```

### JSON Schema (what the LLM sees)

```json
{
  "name": "execute_tool",
  "description": "Execute a tool by name with the given arguments.\n\nUse discover_tools to find available tools, then get_tool_schema to see what arguments a tool accepts, then call this to execute it.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "tool_name": {
        "type": "string",
        "description": "The tool to execute (e.g., 'apollo_people_search')."
      },
      "arguments": {
        "type": "object",
        "description": "Arguments for the tool, matching the schema from get_tool_schema."
      }
    },
    "required": ["tool_name"]
  }
}
```

**Schema token cost**: ~90 tokens

### Response Format

The response passes through the upstream tool's result. The gateway wraps it minimally:

**Successful execution:**

```json
{
  "tool": "apollo_people_search",
  "result": {
    "people": [
      {"name": "Jane Smith", "title": "VP Sales", "company": "Acme Corp", "email": "jane@acme.com"},
      {"name": "John Doe", "title": "Sales Director", "company": "Widget Inc", "email": "john@widget.com"}
    ],
    "total": 47,
    "page": 1
  }
}
```

**Upstream error (tool returned isError):**

```json
{
  "tool": "apollo_people_search",
  "error": "Invalid parameter: 'limit' must be between 1 and 100, got 500"
}
```

**Gateway-level error (routing, connectivity):**

```json
{
  "error": "Tool 'apollo_people_search' is temporarily unavailable: upstream server 'apollo' is unreachable. Other domains are still available."
}
```

```json
{
  "error": "Unknown tool 'apollo_search'. Did you mean 'apollo_people_search' or 'apollo_org_search'?"
}
```

### Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| `arguments` is optional | Default to `{}` | Some tools take no arguments; requiring an empty object is unnecessary friction |
| `arguments` is untyped `object` | No schema validation in meta-tool | The upstream server validates arguments; double-validation adds complexity with no benefit to the LLM |
| Wrap result in `{"tool": ..., "result": ...}` | Minimal wrapper | LLM knows which tool produced the result; useful when executing multiple tools |
| Error distinction | `error` field for gateway errors, upstream `isError` content for tool errors | LLM can distinguish "wrong tool name" from "tool failed" and take different corrective action |
| No `timeout` parameter | Gateway controls timeouts | LLMs shouldn't manage timeouts; the gateway has per-upstream timeout config |
| No `_meta` passthrough | Gateway handles internally | LLM shouldn't see or manage MCP metadata |

---

## 5. Total Token Budget for Meta-Tool Schemas

The 3 meta-tool schemas that go into the LLM's tool list at the start of every conversation:

| Tool | Schema Tokens | Notes |
|---|---|---|
| `discover_tools` | ~120 | 3 optional params, descriptive |
| `get_tool_schema` | ~80 | 1 required param |
| `execute_tool` | ~90 | 2 params (1 required, 1 optional object) |
| **Total** | **~290** | **vs. ~80,000-100,000 for 200 tools flat** |

This is consistent with our requirements doc estimate of ~2K initial tokens (the ~290 is just the schemas; the tool list JSON envelope adds overhead).

---

## 6. LLM Interaction Patterns

### Typical First Use (Cold Start)

```
LLM:    discover_tools()
Result: 5 domains, 220 total tools

LLM:    discover_tools(domain="apollo")
Result: 14 tools in 3 groups

LLM:    get_tool_schema("apollo_people_search")
Result: Full schema with 5 parameters

LLM:    execute_tool("apollo_people_search", {"name": "Jane Smith", "company": "Acme"})
Result: Search results

Total meta-tool calls: 4 (3 discovery + 1 execution)
Total token overhead: ~290 (schemas) + ~300 (domain list) + ~420 (tool list) + ~350 (schema) = ~1,360
```

### Subsequent Use (Warm Context)

The LLM has schemas in conversation history from previous turns:

```
LLM:    execute_tool("apollo_people_search", {"name": "Bob Jones"})
Result: Search results

Total meta-tool calls: 1
Total token overhead: 0 additional (schemas already in history)
```

### Cross-Domain Use

```
LLM:    discover_tools(query="financial filings")
Result: 3 matching tools across sec_edgar domain

LLM:    get_tool_schema("sec_edgar_search_filings")
Result: Full schema

LLM:    execute_tool("sec_edgar_search_filings", {"company": "AAPL", "form_type": "10-K"})
Result: Filing results

Total meta-tool calls: 3
```

### Error Recovery

```
LLM:    execute_tool("apollo_search", {"name": "Jane"})
Result: {"error": "Unknown tool 'apollo_search'. Did you mean 'apollo_people_search' or 'apollo_org_search'?"}

LLM:    execute_tool("apollo_people_search", {"name": "Jane"})
Result: Search results

Total recovery cost: 1 extra round-trip
```

---

## 7. Annotations and Metadata

### MCP Tool Annotations

The meta-tools should carry meaningful MCP annotations (available in MCP 2025-03-26+):

```python
discover_tools:
    readOnlyHint: true      # Never modifies data
    openWorldHint: false     # Fixed behavior, no side effects
    idempotentHint: true     # Same inputs always return same results

get_tool_schema:
    readOnlyHint: true
    openWorldHint: false
    idempotentHint: true

execute_tool:
    readOnlyHint: false      # May modify data (depends on underlying tool)
    openWorldHint: true      # Can interact with external systems
    idempotentHint: false    # Side effects depend on underlying tool
```

These annotations help frameworks like PydanticAI make intelligent decisions about tool call parallelism, caching, and approval flows.

### MCP Server Instructions

The gateway should provide system prompt guidance via the MCP `instructions` field (returned during `initialize`):

```
You have access to a tool discovery gateway with 3 tools:

1. discover_tools - Browse available tools. Call with no arguments to see domains,
   or with a domain to see specific tools.
2. get_tool_schema - Get a tool's parameter schema before using it.
3. execute_tool - Run any discovered tool.

Workflow: discover_tools → get_tool_schema → execute_tool.
Skip discovery for tools you've already used in this conversation.
```

This is ~80 tokens and teaches the LLM the intended workflow without requiring the user to configure a system prompt.

---

## 8. Schema Extensibility

### Future: Semantic Search (Phase 2)

When semantic search is added, `discover_tools` gains a `search_mode` parameter:

```python
async def discover_tools(
    domain: str | None = None,
    group: str | None = None,
    query: str | None = None,
    search_mode: Literal["keyword", "semantic"] = "keyword",  # Phase 2
) -> str:
```

The schema change is backward-compatible -- existing callers that omit `search_mode` get keyword search.

### Future: Batch Schema Retrieval

If token overhead from multiple `get_tool_schema` calls becomes an issue:

```python
async def get_tool_schema(
    tool_name: str | None = None,
    tool_names: list[str] | None = None,  # Phase 2: batch mode
) -> str:
```

### Future: Tool Output Schema

When MCP `outputSchema` adoption grows, `get_tool_schema` can include it:

```json
{
  "name": "apollo_people_search",
  "parameters": { ... },
  "output_schema": {
    "type": "object",
    "properties": {
      "people": {"type": "array", "items": {"$ref": "#/$defs/Person"}},
      "total": {"type": "integer"}
    }
  }
}
```

---

## 9. Comparison with Prior Art

### How Our Design Differs

| Aspect | Speakeasy | Block/Square | Lazy-MCP | **Ours** |
|---|---|---|---|---|
| Discovery method | Semantic search only | Hierarchical only | Path navigation only | **Hierarchical + keyword** |
| Schema in discovery? | No | Partial (type info) | No (descriptions only) | **No** |
| Separate schema tool? | Yes (`describe_tools`) | Yes (`get_type_info`) | No | **Yes** |
| Navigation structure | Flat tags | Service → method | Dot-path tree | **Domain → group → tool** |
| Error guidance | Generic | "Call me before..." | Generic | **Fuzzy match + suggestions** |
| Tool count | 3 | 3 | 2 | **3** |
| Unfiltered discovery | Returns nothing useful | Requires service name | Returns top categories | **Returns domain summary** |

### Why 3 Levels of Discovery Granularity

Our `discover_tools` supports progressive drill-down:

```
discover_tools()                              → All domains (~5-10 entries)
discover_tools(domain="apollo")               → All tools in domain (~5-20 entries)
discover_tools(domain="apollo", group="people")  → Tools in group (~2-5 entries)
```

This is more granular than Speakeasy (flat search) but simpler than Lazy-MCP (arbitrary depth paths). The fixed 2-level hierarchy (domain/group) keeps the cognitive model simple for both LLMs and humans.

---

## 10. Technical Requirements Summary

| Requirement | Priority | Notes |
|---|---|---|
| 3 meta-tools with schemas defined above | P0 | Core API surface |
| `discover_tools` supports no-arg, domain, domain+group, and query modes | P0 | All 4 modes in Phase 1 |
| `get_tool_schema` returns standard JSON Schema | P0 | Upstream schemas passed through |
| `execute_tool` routes to correct upstream and returns results | P0 | Core routing logic |
| Error messages include suggestions and valid options | P0 | LLM self-correction |
| Fuzzy matching on tool names | P1 | Reduce error round-trips |
| MCP `annotations` on all 3 tools | P1 | readOnlyHint, openWorldHint |
| MCP `instructions` with workflow guidance | P1 | ~80 token system prompt |
| `query` mode uses keyword matching | P0 (Phase 1) | Substring/token matching |
| `query` mode supports semantic search | P2 (Phase 2) | Requires embedding infrastructure |
| Batch `get_tool_schema` | P2 (Phase 2) | Multiple schemas in one call |

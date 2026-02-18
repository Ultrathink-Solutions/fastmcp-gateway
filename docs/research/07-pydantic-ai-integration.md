# PydanticAI Integration Analysis

**Date:** 2026-02-17
**Status:** Research complete
**Source:** Cloned and analyzed [pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai) source code

---

## Overview

PydanticAI is the agent framework in our stack. The gateway must integrate cleanly with it -- both as a standard MCP server (any client can connect) and with deeper, framework-specific integration for per-user context and dynamic toolset management.

This document maps PydanticAI's toolset architecture, identifies integration points, and defines technical requirements.

---

## 1. PydanticAI Toolset Architecture

### Core Abstractions

PydanticAI's tool system is built on composable toolsets:

```
Agent
  |
  |-- ToolManager (per run step)
  |     |
  |     |-- CombinedToolset (merges all toolsets)
  |           |-- FunctionToolset (@agent.tool decorators)
  |           |-- MCPServer (MCP client connection)
  |           |-- FastMCPToolset (in-process FastMCP bridge)
  |           |-- DynamicToolset (per-step rebuild)
  |           |-- FilteredToolset (context-dependent filtering)
  |           |-- PrefixedToolset (name prefixing)
  |           |-- PreparedToolset (per-step tool definition modification)
  |           |-- RenamedToolset (name remapping)
  |           |-- ApprovalRequiredToolset (human-in-the-loop)
```

### `AbstractToolset` Protocol

Every toolset implements two methods:

```python
class AbstractToolset(ABC, Generic[AgentDepsT]):
    async def get_tools(self, ctx: RunContext[AgentDepsT]) -> dict[str, ToolsetTool[AgentDepsT]]:
        """Return available tools for this run step."""
        ...

    async def call_tool(
        self, name: str, tool_args: dict[str, Any],
        ctx: RunContext[AgentDepsT], tool: ToolsetTool[AgentDepsT]
    ) -> Any:
        """Execute a tool call."""
        ...
```

Key: `get_tools()` receives the `RunContext`, which carries user dependencies (`ctx.deps`), the current run step, retry state, model info, and usage tracking.

### `ToolDefinition` Structure

What PydanticAI passes to the LLM:

```python
@dataclass(kw_only=True)
class ToolDefinition:
    name: str
    description: str
    parameters_json_schema: dict[str, Any]  # JSON Schema
    metadata: dict[str, Any] | None = None  # _meta, annotations, output_schema
    kind: Literal['function', 'output', 'external'] = 'function'
    strict: bool | None = None              # OpenAI strict mode
    sequential: bool = False                 # Force sequential execution
```

The `metadata` field carries MCP `_meta`, `annotations`, and `outputSchema` -- preserving the full MCP tool metadata through to the agent.

### `ToolsetTool` Wrapper

Wraps a `ToolDefinition` with execution context:

```python
@dataclass(kw_only=True)
class ToolsetTool(Generic[AgentDepsT]):
    toolset: AbstractToolset[AgentDepsT]     # Which toolset owns this tool
    tool_def: ToolDefinition                  # The definition
    max_retries: int                          # Retry count
    args_validator: SchemaValidator           # Pydantic Core validator
```

---

## 2. MCP Connection Patterns

PydanticAI provides two ways to connect to MCP servers:

### Pattern A: `MCPServer` (MCP SDK Client)

Direct MCP client connection via the official `mcp` Python SDK:

```python
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStreamableHTTP

gateway = MCPServerStreamableHTTP(
    "http://gateway:8080/mcp",
    tool_prefix="gw",
    headers={"Authorization": "Bearer ..."},
)
agent = Agent("openai:gpt-5.2", toolsets=[gateway])
```

**How it works** (from `mcp.py:572-588`):
1. `get_tools()` calls `self.list_tools()` on the MCP client session
2. Each MCP tool is mapped to a `ToolDefinition` with `_meta`, `annotations`, and `outputSchema` in `metadata`
3. `call_tool()` sends `tools/call` via the MCP client session
4. Tool results are mapped from MCP content blocks to PydanticAI types

**Key features:**
- `tool_prefix` -- automatic namespace prefixing (stripped before forwarding to server)
- `headers` -- static HTTP headers (auth tokens, etc.)
- `cache_tools` -- caches `tools/list` until `notifications/tools/list_changed`
- `process_tool_call` -- hook to customize tool calls (add metadata, transform args)
- `max_retries` -- per-tool retry with `ModelRetry` on MCP errors

### Pattern B: `FastMCPToolset` (In-Process Bridge)

Direct in-process connection to a FastMCP server -- no network, no serialization:

```python
from pydantic_ai import Agent
from pydantic_ai.toolsets import FastMCPToolset
from fastmcp import FastMCP

gateway_server = FastMCP("gateway")
# ... configure gateway ...

agent = Agent(
    "openai:gpt-5.2",
    toolsets=[FastMCPToolset(gateway_server)],
)
```

**How it works** (from `toolsets/fastmcp.py:131-147`):
1. Wraps a FastMCP `Client` which can accept a `FastMCP` server instance directly
2. Uses `client.list_tools()` and `client.call_tool()` -- same MCP protocol but in-process
3. Preserves `_meta`, `annotations`, `outputSchema` in `ToolDefinition.metadata`

**When to use:** Development, testing, single-process deployments. Avoids network overhead.

---

## 3. Dynamic Per-User Context: The Key Integration Pattern

### `DynamicToolset` -- Per-Step Toolset Rebuild

This is the critical pattern for flowing per-user context through to the gateway:

```python
from pydantic_ai import Agent, RunContext
from pydantic_ai.mcp import MCPServerStreamableHTTP

@dataclass
class ChatDeps:
    user_id: str
    conversation_id: str
    auth_token: str

agent = Agent("openai:gpt-5.2", deps_type=ChatDeps)

@agent.toolset
def gateway_toolset(ctx: RunContext[ChatDeps]) -> MCPServerStreamableHTTP:
    return MCPServerStreamableHTTP(
        "http://gateway:8080/mcp",
        headers={
            "Authorization": f"Bearer {ctx.deps.auth_token}",
            "X-User-Id": ctx.deps.user_id,
            "X-Conversation-Id": ctx.deps.conversation_id,
        },
    )
```

**How it works** (from `toolsets/_dynamic.py:19-99`):
1. `DynamicToolset` wraps a function that takes `RunContext` and returns a toolset
2. By default (`per_run_step=True`), the function is re-evaluated every run step
3. The returned toolset is entered (`__aenter__`) and used for that step
4. Previous toolset is exited (`__aexit__`) when a new one is created
5. Setting `per_run_step=False` evaluates once per agent run

**Why this matters for the gateway:**
- The gateway receives per-user headers on every MCP request
- FastMCP's `get_http_headers()` / `get_access_token()` DI extracts these
- The gateway can pass them through to upstream MCP servers
- No user context leaks between requests

### `FilteredToolset` -- Context-Dependent Tool Filtering

```python
gateway = MCPServerStreamableHTTP("http://gateway:8080/mcp")

# Only show tools that match the current conversation's domain
filtered_gateway = gateway.filtered(
    lambda ctx, tool_def: tool_def.name.startswith(ctx.deps.active_domain)
)
```

**How it works** (from `toolsets/filtered.py:13-24`):
- Wraps any toolset, filtering `get_tools()` output per step
- Filter function receives `RunContext` and `ToolDefinition`
- Can filter on name, description, metadata, tags, annotations

**Relevance:** Client-side filtering complements server-side progressive discovery. An agent could filter the 3 meta-tools down to just `execute_tool` after discovery is complete.

### `PreparedToolset` -- Per-Step Tool Definition Modification

```python
gateway = MCPServerStreamableHTTP("http://gateway:8080/mcp")

async def inject_context(ctx: RunContext[ChatDeps], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
    """Modify tool descriptions to include user-specific context."""
    return [
        replace(td, description=f"{td.description} (User: {ctx.deps.user_id})")
        for td in tool_defs
    ]

prepared_gateway = gateway.prepared(inject_context)
```

**How it works** (from `toolsets/prepared.py:13-36`):
- Wraps any toolset, modifying `ToolDefinition` objects per step
- Cannot add or rename tools (only modify existing definitions)
- Useful for injecting context-specific information into descriptions

### `process_tool_call` Hook -- Tool Call Interception

```python
async def add_tracing(
    ctx: RunContext[ChatDeps],
    call_fn: CallToolFunc,
    name: str,
    args: dict[str, Any],
) -> ToolResult:
    """Add request metadata to every tool call."""
    metadata = {
        "user_id": ctx.deps.user_id,
        "trace_id": str(uuid4()),
    }
    return await call_fn(name, args, metadata)

gateway = MCPServerStreamableHTTP(
    "http://gateway:8080/mcp",
    process_tool_call=add_tracing,
)
```

**How it works** (from `mcp.py:556-570`):
- Intercepts every `tools/call` before it reaches the MCP server
- Receives `RunContext`, the original call function, tool name, and args
- Can modify args, add `_meta` metadata to the MCP request, or short-circuit
- The `metadata` dict is sent as `_meta` on the MCP `CallToolRequest`

---

## 4. Integration Points for fastmcp-gateway

### Integration Point 1: Standard MCP Server (Zero Integration Required)

The gateway is a standard MCP server. Any PydanticAI agent can connect via `MCPServerStreamableHTTP`:

```python
gateway = MCPServerStreamableHTTP("http://gateway:8080/mcp")
agent = Agent("openai:gpt-5.2", toolsets=[gateway])
```

The agent sees 3 tools: `discover_tools`, `get_tool_schema`, `execute_tool`. No special integration needed. This is the baseline -- works with any MCP client, any agent framework.

### Integration Point 2: Per-User Headers via `DynamicToolset`

The `@agent.toolset` decorator enables per-user auth/context passthrough:

```python
@agent.toolset
def gateway(ctx: RunContext[ChatDeps]) -> MCPServerStreamableHTTP:
    return MCPServerStreamableHTTP(
        settings.gateway_url,
        headers={
            "Authorization": f"Bearer {ctx.deps.auth_token}",
            "X-User-Id": ctx.deps.user_id,
        },
    )
```

**Gateway side** -- FastMCP's DI extracts these headers:

```python
from fastmcp.server.dependencies import Depends, get_http_headers

async def get_user_context(headers: dict = Depends(get_http_headers)):
    return UserContext(user_id=headers.get("x-user-id", ""))

@gateway.tool()
async def execute_tool(
    tool_name: str,
    arguments: dict,
    user_ctx: UserContext = Depends(get_user_context),  # hidden from LLM
) -> Any:
    # user_ctx.user_id flows through to upstream MCP servers
    ...
```

**Technical requirement:** The gateway must forward user context headers to upstream MCP servers when executing tools. This means upstream `Client` connections need per-request header injection.

### Integration Point 3: In-Process via `FastMCPToolset`

For development, testing, or single-process deployments:

```python
from pydantic_ai.toolsets import FastMCPToolset
from fastmcp_gateway import DiscoveryProvider, create_gateway

gateway_server = create_gateway(
    upstreams={"apollo": "http://apollo:8080/mcp"},
)
agent = Agent("openai:gpt-5.2", toolsets=[FastMCPToolset(gateway_server)])
```

**Technical requirement:** The gateway must work as both a standalone HTTP server AND as an in-process FastMCP instance passed to `FastMCPToolset`. This is free if we build on `FastMCP` (it supports both modes natively).

### Integration Point 4: System Prompt Guidance

The LLM needs instructions on how to use the 3 meta-tools effectively. PydanticAI supports system prompts:

```python
agent = Agent(
    "openai:gpt-5.2",
    toolsets=[gateway],
    system_prompt=[
        "You have access to a tool discovery gateway.",
        "Use discover_tools() first to see available domains.",
        "Then use get_tool_schema() to load schemas for tools you need.",
        "Finally use execute_tool() to run tools.",
    ],
)
```

**Technical requirement:** The gateway should provide a recommended system prompt snippet that users can include. This could be:
- A static string exported from the package
- An MCP resource (`gateway://system-prompt`)
- Part of the server's `instructions` field (sent during MCP `initialize`)

The MCP `instructions` field is the most spec-aligned option -- PydanticAI already reads it:

```python
# From mcp.py:704-705
result = await self._client.initialize()
self._instructions = result.instructions
```

### Integration Point 5: Tool Metadata Passthrough

PydanticAI preserves MCP tool metadata in `ToolDefinition.metadata`:

```python
# From mcp.py:577-583
ToolDefinition(
    name=name,
    description=mcp_tool.description,
    parameters_json_schema=mcp_tool.inputSchema,
    metadata={
        'meta': mcp_tool.meta,           # MCP _meta field
        'annotations': mcp_tool.annotations.model_dump() if mcp_tool.annotations else None,
        'output_schema': mcp_tool.outputSchema or None,
    },
)
```

**Implication:** Our 3 meta-tools should have meaningful `_meta` and `annotations`:
- `discover_tools` -- `readOnlyHint: true`, `openWorldHint: false`
- `get_tool_schema` -- `readOnlyHint: true`, `openWorldHint: false`
- `execute_tool` -- `readOnlyHint: false`, `openWorldHint: true`

### Integration Point 6: `notifications/tools/list_changed`

PydanticAI's `MCPServer` handles cache invalidation:

```python
# From mcp.py:761-763
if isinstance(message.root, mcp_types.ToolListChangedNotification):
    self._cached_tools = None
```

**Technical requirement:** If upstream servers change their tool lists, the gateway should:
1. Update its internal registry
2. Emit `notifications/tools/list_changed` to connected clients
3. PydanticAI will automatically re-fetch `tools/list` (the 3 meta-tools -- these don't change, but the registry content does)

Note: Since our `tools/list` always returns the same 3 meta-tools, `tools/list_changed` notifications are only needed if we dynamically add/remove meta-tools. The registry changes are internal and surfaced through `discover_tools` responses.

---

## 5. What We Should NOT Build

### Custom PydanticAI Toolset

We do NOT need a custom `GatewayToolset(AbstractToolset)`. The gateway is a standard MCP server -- `MCPServerStreamableHTTP` and `FastMCPToolset` already handle the connection. Building a custom toolset would:
- Duplicate MCP client logic
- Break compatibility with other MCP clients
- Create a PydanticAI-specific coupling

### Client-Side Progressive Discovery

The progressive discovery logic lives entirely **server-side** in the gateway. The PydanticAI agent just sees 3 tools and uses them naturally. We don't need client-side tool filtering, prepared toolsets, or dynamic toolsets for the discovery pattern itself.

The `DynamicToolset` is only needed for per-user header injection, which is an orthogonal concern.

---

## 6. Technical Requirements Summary

| Requirement | Source | Priority |
|---|---|---|
| Work as standard MCP server (any client connects) | Integration Point 1 | P0 |
| Work as in-process FastMCP instance (for `FastMCPToolset`) | Integration Point 3 | P0 |
| Forward user context headers to upstream servers | Integration Point 2 | P0 |
| Provide system prompt guidance via MCP `instructions` | Integration Point 4 | P1 |
| Set meaningful tool annotations on meta-tools | Integration Point 5 | P1 |
| Emit `notifications/tools/list_changed` when registry updates | Integration Point 6 | P2 |
| Document `DynamicToolset` pattern for per-user context | Integration Point 2 | P1 |
| Provide example agent code with `system_prompt` | Integration Point 4 | P1 |

---

## 7. Reference: Complete Agent Integration Example

```python
from dataclasses import dataclass
from pydantic_ai import Agent, RunContext
from pydantic_ai.mcp import MCPServerStreamableHTTP

@dataclass
class ChatDeps:
    user_id: str
    auth_token: str

agent = Agent(
    "anthropic:claude-sonnet-4-5-20250929",
    deps_type=ChatDeps,
    system_prompt=[
        "You have access to tools via a discovery gateway.",
        "Start by calling discover_tools() to see available domains.",
        "Use get_tool_schema(tool_name) to load a tool's parameters.",
        "Use execute_tool(tool_name, arguments) to run any tool.",
        "You can skip discovery for tools you've already used in this conversation.",
    ],
)

@agent.toolset
def gateway(ctx: RunContext[ChatDeps]) -> MCPServerStreamableHTTP:
    return MCPServerStreamableHTTP(
        "http://gateway:8080/mcp",
        headers={
            "Authorization": f"Bearer {ctx.deps.auth_token}",
            "X-User-Id": ctx.deps.user_id,
        },
    )

# Usage
async def handle_chat(user_id: str, token: str, message: str):
    result = await agent.run(
        message,
        deps=ChatDeps(user_id=user_id, auth_token=token),
    )
    return result.output
```

No custom toolsets, no special integrations. The gateway is just an MCP server. PydanticAI's existing primitives handle everything else.

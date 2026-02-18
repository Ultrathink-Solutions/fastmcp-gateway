# Observability: OpenTelemetry, MCP Semantic Conventions, and Logfire

**Date:** 2026-02-17
**Status:** Research complete
**Sources:** OTel GenAI Semantic Conventions spec, FastMCP source (`/tmp/fastmcp-source`), Logfire source (`/tmp/logfire-source`), PydanticAI source (`/tmp/pydantic-ai-source`)

---

## Overview

The gateway must be observable across the full request chain: PydanticAI agent -> gateway -> upstream MCP servers. Three layers of telemetry conventions apply:

1. **OTel GenAI Semantic Conventions** (`gen_ai.*`) -- agent and tool call spans (PydanticAI emits these)
2. **OTel MCP Semantic Conventions** (`mcp.*`) -- protocol-level spans (FastMCP emits these)
3. **Logfire** -- adds MCP instrumentation via monkey-patching, propagates context through MCP `_meta`

The gateway sits at the boundary between GenAI and MCP convention worlds. FastMCP gives us MCP-level telemetry for free. We need to add gateway-specific routing spans and ensure context propagation flows end-to-end.

---

## 1. OTel GenAI Semantic Conventions

**Status:** Development (not yet stable). All attributes use the `gen_ai.*` namespace.

### Relevant Operation Types

| Operation | Span Kind | Span Name Format | Used By |
|---|---|---|---|
| `execute_tool` | `INTERNAL` | `execute_tool {gen_ai.tool.name}` | PydanticAI tool execution |
| `invoke_agent` | `CLIENT` or `INTERNAL` | `invoke_agent {gen_ai.agent.name}` | PydanticAI agent run |
| `chat` | `CLIENT` | `chat {gen_ai.request.model}` | LLM inference call |

### Execute Tool Span Attributes

**Required:**
- `gen_ai.operation.name` = `"execute_tool"`

**Recommended:**
- `gen_ai.tool.name` -- tool name
- `gen_ai.tool.type` -- `"function"`, `"extension"`, or `"datastore"`
- `gen_ai.tool.call.id` -- tool call identifier from LLM response
- `gen_ai.tool.description` -- tool description

**Opt-in (sensitive):**
- `gen_ai.tool.call.arguments` -- parameters passed to tool
- `gen_ai.tool.call.result` -- result returned by tool

Key: arguments and results are marked **sensitive** -- instrumentations SHOULD NOT capture them by default but MUST provide opt-in options.

---

## 2. OTel MCP Semantic Conventions

The OTel spec has explicit MCP conventions layered on standard RPC conventions. These are what FastMCP uses.

### MCP Client Span

| Property | Value |
|---|---|
| Span Kind | `CLIENT` |
| Span Name | `{mcp.method.name} {target}` (e.g., `tools/call search_filings`) |

**Required:** `mcp.method.name`

**Recommended:**
- `gen_ai.operation.name` (set to `execute_tool` for tool calls)
- `gen_ai.tool.name` (for tool operations)
- `jsonrpc.protocol.version`, `mcp.protocol.version`
- `mcp.session.id`
- `server.address`, `server.port`

### MCP Server Span

Same structure as client spans with `SERVER` kind. Uses `client.address`/`client.port` instead of `server.*`.

### Well-Known MCP Method Names

`initialize`, `tools/call`, `tools/list`, `resources/read`, `resources/list`, `prompts/get`, `prompts/list`, `notifications/initialized`, `notifications/tools/list_changed`

### MCP Metrics

| Metric | Type | Unit |
|---|---|---|
| `mcp.client.operation.duration` | Histogram | seconds |
| `mcp.server.operation.duration` | Histogram | seconds |
| `mcp.client.session.duration` | Histogram | seconds |
| `mcp.server.session.duration` | Histogram | seconds |

### Context Propagation

The MCP semconv spec mandates propagating trace context via `params._meta` using W3C Trace Context format (`traceparent`, `tracestate`).

---

## 3. What FastMCP Already Provides

FastMCP has built-in OTel support across three telemetry modules. **We get all of this for free.**

### Core (`fastmcp/telemetry.py`)

- Instrumentation name: `"fastmcp"`
- Uses `opentelemetry-api` only -- no-op unless SDK configured
- Context propagation keys: `fastmcp.traceparent`, `fastmcp.tracestate` in `_meta`
- `inject_trace_context(meta)` -- injects current trace context into MCP request meta
- `extract_trace_context(meta)` -- extracts trace context from incoming MCP request meta
- Respects existing trace context (won't override HTTP-propagated context)

### Server Telemetry (`fastmcp/server/telemetry.py`)

`server_span()` creates `SpanKind.SERVER` spans with:

```python
{
    "rpc.system": "mcp",
    "rpc.service": server_name,
    "rpc.method": method,              # e.g., "tools/call"
    "mcp.method.name": method,
    "fastmcp.server.name": server_name,
    "fastmcp.component.type": "tool",  # or "resource", "prompt"
    "fastmcp.component.key": tool_name,
    # Conditionally:
    "mcp.resource.uri": resource_uri,
    "enduser.id": token.client_id,     # If authenticated
    "enduser.scope": scopes,
    "mcp.session.id": session_id,
}
```

Also includes:
- `delegate_span()` for provider delegation (INTERNAL kind)
- `get_auth_span_attributes()` -- adds `enduser.id` and `enduser.scope` from access tokens
- `get_session_span_attributes()` -- adds `mcp.session.id`
- `_get_parent_trace_context()` -- extracts trace context from `request_ctx.meta`

### Client Telemetry (`fastmcp/client/telemetry.py`)

`client_span()` creates `SpanKind.CLIENT` spans with:

```python
{
    "rpc.system": "mcp",
    "rpc.method": method,
    "mcp.method.name": method,
    "fastmcp.component.key": component_key,
    "mcp.session.id": session_id,      # Conditional
    "mcp.resource.uri": resource_uri,   # Conditional
}
```

### ProxyProvider Trace Chain

When FastMCP proxies a tool call to an upstream server, it creates:

1. **Server span** (SERVER): `tools/call {name}` -- gateway receives the call
2. **Client span** (CLIENT): `tools/call {backend_name}` -- gateway forwards upstream
3. **Trace context injection**: `inject_trace_context(meta)` into upstream `_meta`

This is the same pattern our gateway will use.

### What FastMCP Does NOT Emit

FastMCP does NOT currently use `gen_ai.*` attributes. It uses only:
- `rpc.*` (standard RPC semconv)
- `mcp.*` (MCP semconv)
- `fastmcp.*` (custom attributes)

---

## 4. How Logfire Adds MCP Instrumentation

**Source:** `logfire/_internal/integrations/mcp.py`

Logfire adds observability on top of the standard MCP SDK (not FastMCP) by monkey-patching `BaseSession.send_request`.

### What Logfire Does

1. **Patches `BaseSession.send_request`** -- wraps every MCP request in a Logfire span:
   ```python
   with logfire_instance.span(span_name, **attributes) as span:
       _attach_context_to_request(root)  # inject OTel context into _meta
       result = await original_send_request(self, request, *args, **kwargs)
       span.set_attribute('response', result)
   ```

2. **Span attributes:**
   - `rpc.system: "jsonrpc"`
   - `rpc.jsonrpc.version: "2.0"`
   - `rpc.method: {method}` (e.g., `tools/call`)
   - Span name: `MCP request: {method}` (with tool name appended for `CallToolRequest`)

3. **Context propagation via `_meta`:**
   - Uses `get_context()` / `attach_context()` from `logfire.propagate`
   - Injects `traceparent`/`tracestate` into outgoing request `params.meta`
   - Extracts and restores context from incoming request `params.meta`
   - This is the **same pattern** as FastMCP's `inject_trace_context` / `extract_trace_context`

4. **Patches notifications:**
   - `ClientSession._received_notification` -- creates spans for MCP server log messages
   - Maps MCP log levels to Logfire levels (`critical`/`alert`/`emergency` -> `fatal`)

5. **Patches request handlers:**
   - `Server._handle_request` -- wraps server-side request handling in spans
   - Records the response on the span

### Logfire + FastMCP Compatibility

**Critical finding:** Logfire and FastMCP both inject trace context into `_meta`, but using different key names:
- Logfire uses standard W3C keys: `traceparent`, `tracestate` (via `opentelemetry.propagate`)
- FastMCP uses namespaced keys: `fastmcp.traceparent`, `fastmcp.tracestate`

FastMCP's `extract_trace_context()` checks for `fastmcp.traceparent` in meta, but ALSO checks if there's already a valid span from HTTP propagation (which Logfire would have set up). So in practice they don't conflict:

```python
# FastMCP extract_trace_context:
current_span = trace.get_current_span()
if current_span.get_span_context().is_valid:
    return otel_context.get_current()  # Use existing (Logfire-set) context
```

**Implication:** When both Logfire and FastMCP are active, Logfire's HTTP-level context propagation takes precedence, and FastMCP's `_meta`-based propagation is a fallback for non-HTTP transports (stdio, in-process).

---

## 5. How PydanticAI Emits OTel

**Source:** `pydantic_ai/_instrumentation.py`, `_tool_manager.py`

### Versioned Instrumentation

PydanticAI supports instrumentation versions 1-4. Version 3+ uses OTel GenAI semantic conventions:

| Property | Version <= 2 | Version >= 3 |
|---|---|---|
| Agent span | `agent run` | `invoke_agent {agent_name}` |
| Tool span | `running tool` | `execute_tool {tool_name}` |
| Tool args attr | `tool_arguments` | `gen_ai.tool.call.arguments` |
| Tool result attr | `tool_response` | `gen_ai.tool.call.result` |

### Tool Call Span Attributes (v3+)

```python
{
    'gen_ai.tool.name': tool_name,
    'gen_ai.tool.call.id': tool_call_id,
    # Only if include_content=True:
    'gen_ai.tool.call.arguments': args_json,
    'gen_ai.tool.call.result': result,
}
```

### Span Hierarchy

```
invoke_agent {agent_name}           (root span)
  |
  running tools                     (grouping span for parallel tool calls)
    |
    execute_tool {tool_name}        (individual tool span)
```

### Content Recording Control

`InstrumentationSettings.include_content: bool` (default `True`) controls whether `gen_ai.tool.call.arguments` and `gen_ai.tool.call.result` are recorded. Aligns with OTel spec's opt-in approach.

### Logfire Integration

Logfire instruments PydanticAI by passing its `TracerProvider`, `MeterProvider`, and `LoggerProvider` to PydanticAI's `InstrumentationSettings`:

```python
settings = InstrumentationSettings(
    tracer_provider=logfire_instance.config.get_tracer_provider(),
    meter_provider=logfire_instance.config.get_meter_provider(),
    logger_provider=logfire_instance.config.get_logger_provider(),
)
```

This means PydanticAI spans automatically flow into Logfire dashboards.

---

## 6. Gateway Telemetry Design

### 6.1 Span Chain for a Typical `execute_tool` Request

```
[PydanticAI - CLIENT]  execute_tool search_filings       (gen_ai.* attributes)
  |
  [Gateway - SERVER]   tools/call execute_tool            (FastMCP server span, auto)
    |
    [Gateway - INTERNAL]  gateway.route search_filings    (gateway routing logic)
      |
      [Gateway - CLIENT]  tools/call search_filings       (MCP client span to upstream)
        |
        [Upstream - SERVER]  tools/call search_filings    (upstream MCP server span)
```

Trace context flows end-to-end via:
- HTTP headers (PydanticAI -> Gateway): standard W3C propagation
- MCP `_meta` (Gateway -> Upstream): `inject_trace_context()` into `_meta`

### 6.2 What We Get for Free (from FastMCP)

Since the gateway is a FastMCP server, these spans are emitted automatically:

| Span Name | Kind | When |
|---|---|---|
| `tools/call discover_tools` | SERVER | Client calls discover_tools |
| `tools/call get_tool_schema` | SERVER | Client calls get_tool_schema |
| `tools/call execute_tool` | SERVER | Client calls execute_tool |
| `tools/list` | SERVER | Client lists tools (the 3 meta-tools) |

Standard attributes on all: `rpc.system`, `rpc.service`, `rpc.method`, `mcp.method.name`, `mcp.session.id`, `fastmcp.server.name`, `fastmcp.component.*`

### 6.3 Gateway-Specific Spans to Add

These INTERNAL spans provide visibility into the gateway's routing and registry operations:

**Registry Lookup:**
```
Span Name:  gateway.route {tool_name}
Kind:       INTERNAL
Attributes:
  gateway.tool.name:         requested tool name
  gateway.tool.domain:       resolved domain
  gateway.tool.group:        resolved group
  gateway.upstream.server:   resolved upstream server identifier
```

**Discovery:**
```
Span Name:  gateway.discover
Kind:       INTERNAL
Attributes:
  gateway.discover.domain:       filter domain (if provided)
  gateway.discover.group:        filter group (if provided)
  gateway.discover.query:        search query (if provided)
  gateway.discover.result_count: number of tools returned
```

**Schema Retrieval:**
```
Span Name:  gateway.get_schema {tool_name}
Kind:       INTERNAL
Attributes:
  gateway.tool.name:      requested tool name
  gateway.schema.source:  "cache" or "upstream"
```

**Registry Population (startup):**
```
Span Name:  gateway.registry.populate
Kind:       INTERNAL
Attributes:
  gateway.registry.server_count:   number of upstream servers
  gateway.registry.tool_count:     total tools discovered
  gateway.registry.failed_servers: list of unreachable servers
```

**Registry Refresh:**
```
Span Name:  gateway.registry.refresh
Kind:       INTERNAL
Attributes:
  gateway.refresh.trigger:       "poll", "manual", or "notification"
  gateway.refresh.added_tools:   count of newly added tools
  gateway.refresh.removed_tools: count of removed tools
```

### 6.4 Upstream Forwarding Spans

When `execute_tool` routes to an upstream server, the FastMCP client creates a CLIENT span. We should augment with gateway-specific attributes:

```
Span Name:  tools/call {upstream_tool_name}
Kind:       CLIENT
Attributes:
  rpc.system:                "mcp"
  rpc.method:                "tools/call"
  mcp.method.name:           "tools/call"
  gen_ai.tool.name:          upstream tool name
  mcp.session.id:            upstream session ID
  gateway.upstream.server:   upstream server identifier
  gateway.upstream.domain:   domain name
```

### 6.5 GenAI Attribute Bridge

The gateway can optionally include `gen_ai.operation.name: "execute_tool"` on its server spans. The MCP semconv spec explicitly lists `gen_ai.operation.name` as a recommended attribute on MCP spans, so this bridges the two convention worlds:

```python
# On the gateway's tools/call execute_tool SERVER span:
span.set_attribute("gen_ai.operation.name", "execute_tool")
span.set_attribute("gen_ai.tool.name", actual_tool_name)
```

This helps observability tools (Logfire, Jaeger, Honeycomb) correlate GenAI agent spans with MCP protocol spans.

### 6.6 Sensitive Data Handling

Following both OTel spec and PydanticAI's pattern:

- **Default**: Do NOT record `gen_ai.tool.call.arguments` or `gen_ai.tool.call.result`
- **Opt-in**: Provide a configuration option to enable recording (useful in development/staging)
- **Implementation**: A simple `include_content: bool` flag in gateway configuration

### 6.7 Error Handling in Spans

| Scenario | Span Status | Error Attribute |
|---|---|---|
| Tool not found in registry | ERROR | `error.type: "tool_not_found"` |
| Upstream server unreachable | ERROR (on upstream span only) | `error.type: "connection_error"` |
| Upstream execution failure | ERROR (propagated) | `error.type: {exception_class}` |
| Registry refresh partial failure | OK (parent), ERROR (child) | Per-server error recording |

Key: For graceful degradation (FR-7), mark individual upstream failures as errors but keep the parent registry span healthy. This matches how FastMCP's `ProxyProvider` already handles errors.

### 6.8 Gateway-Specific Metrics

Beyond what FastMCP already emits (`mcp.server.operation.duration`, `mcp.client.operation.duration`):

| Metric | Type | Unit | Description |
|---|---|---|---|
| `gateway.registry.size` | UpDownCounter | `{tools}` | Current tool count in registry |
| `gateway.registry.refresh.duration` | Histogram | seconds | Registry refresh time |
| `gateway.upstream.errors` | Counter | `{errors}` | Upstream errors by server and type |
| `gateway.discovery.requests` | Counter | `{requests}` | discover_tools calls by domain/group |
| `gateway.execute.requests` | Counter | `{requests}` | execute_tool calls by tool/domain |

---

## 7. Attribute Namespace Summary

| Namespace | Source | Used For |
|---|---|---|
| `gen_ai.*` | OTel GenAI semconv | Tool call metadata, operation names, sensitive content |
| `rpc.*` | OTel RPC semconv | System (mcp), method, service |
| `mcp.*` | OTel MCP semconv | Method name, session ID, resource URI, protocol version |
| `fastmcp.*` | FastMCP custom | Server name, component type/key, provider type |
| `gateway.*` | Gateway custom | Domain, group, upstream server, registry operations |
| `server.*` / `client.*` | OTel general | Address and port |
| `error.*` | OTel general | Error type classification |
| `enduser.*` | OTel general | Authenticated user identity and scopes |

---

## 8. Logfire-Specific Considerations

### Do We Need Logfire-Specific Code?

**No.** The gateway does not need any Logfire-specific instrumentation because:

1. **FastMCP already provides OTel spans** with standard attributes. Logfire collects these automatically when configured as the OTel backend.

2. **Logfire's MCP instrumentation** patches the low-level `BaseSession` in the MCP SDK. This catches both the gateway's outgoing calls (as an MCP client to upstreams) and incoming calls (as an MCP server receiving from PydanticAI agents). These patches apply at the SDK level regardless of FastMCP.

3. **Logfire's PydanticAI instrumentation** handles the agent-side spans. The gateway doesn't need to know about this -- it just receives well-instrumented trace context via HTTP headers.

4. **Context propagation just works**: When both Logfire and FastMCP are active, Logfire's HTTP-level context propagation takes precedence, and FastMCP's `_meta`-based propagation is the fallback for non-HTTP transports.

### Logfire + FastMCP Key Compatibility Detail

Logfire uses standard W3C keys (`traceparent`, `tracestate`) via `opentelemetry.propagate.inject()`.
FastMCP uses namespaced keys (`fastmcp.traceparent`, `fastmcp.tracestate`).

They don't conflict because FastMCP's `extract_trace_context()` first checks if there's already a valid span from HTTP propagation (which Logfire would have established). The `_meta`-based keys are only used as a fallback when no HTTP context exists (stdio transport, in-process connections).

### What Users Need to Do

For full observability with Logfire:

```python
import logfire

logfire.configure()
logfire.instrument_mcp()          # Patches MCP SDK for span creation + context propagation
logfire.instrument_pydantic_ai()  # Instruments PydanticAI agent spans

# The gateway (FastMCP-based) emits its own spans via opentelemetry-api.
# Logfire's TracerProvider picks these up automatically.
```

No gateway-specific Logfire calls needed.

---

## 9. Technical Requirements Summary

| Requirement | Source | Priority | Notes |
|---|---|---|---|
| Emit standard MCP semconv spans for all operations | OTel MCP spec | P0 | Free from FastMCP |
| Propagate trace context to upstream servers via `_meta` | OTel MCP spec | P0 | Free from FastMCP ProxyProvider pattern |
| Add gateway-specific INTERNAL spans for routing | Gateway observability | P1 | `gateway.route`, `gateway.discover`, `gateway.get_schema` |
| Add registry operation spans (populate, refresh) | Gateway observability | P1 | `gateway.registry.*` |
| Bridge GenAI attributes on server spans | OTel GenAI + MCP specs | P1 | `gen_ai.operation.name`, `gen_ai.tool.name` |
| Provide opt-in sensitive data recording | OTel GenAI spec | P1 | `include_content` configuration flag |
| Emit gateway-specific metrics | Gateway observability | P2 | `gateway.registry.size`, `gateway.execute.requests`, etc. |
| Graceful error span handling | FR-7, OTel best practices | P0 | Individual upstream errors, parent spans stay healthy |
| No Logfire-specific code needed | Logfire analysis | -- | Standard OTel is sufficient |

---

## 10. Implementation Approach

1. **Phase 1 (P0):** Rely entirely on FastMCP's built-in telemetry. The gateway is a FastMCP server with upstream MCP clients -- spans and context propagation are automatic.

2. **Phase 2 (P1):** Add gateway-specific INTERNAL spans using FastMCP's `get_tracer()`:
   ```python
   from fastmcp.telemetry import get_tracer

   tracer = get_tracer()
   with tracer.start_as_current_span("gateway.route", kind=SpanKind.INTERNAL) as span:
       span.set_attribute("gateway.tool.name", tool_name)
       span.set_attribute("gateway.tool.domain", domain)
       # ... route to upstream
   ```

3. **Phase 3 (P2):** Add metrics using `opentelemetry.metrics`:
   ```python
   from opentelemetry import metrics

   meter = metrics.get_meter("fastmcp-gateway")
   registry_size = meter.create_up_down_counter("gateway.registry.size", unit="{tools}")
   execute_counter = meter.create_counter("gateway.execute.requests", unit="{requests}")
   ```

This phased approach means the gateway is fully observable from day one (via FastMCP's built-in telemetry), with progressive enhancement for gateway-specific insights.

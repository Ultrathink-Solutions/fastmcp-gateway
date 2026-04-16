# fastmcp-gateway

[![PyPI](https://img.shields.io/pypi/v/fastmcp-gateway)](https://pypi.org/project/fastmcp-gateway/)
[![Python](https://img.shields.io/pypi/pyversions/fastmcp-gateway)](https://pypi.org/project/fastmcp-gateway/)
[![License](https://img.shields.io/github/license/Ultrathink-Solutions/fastmcp-gateway)](LICENSE)
[![CI](https://github.com/Ultrathink-Solutions/fastmcp-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/Ultrathink-Solutions/fastmcp-gateway/actions/workflows/ci.yml)

**Progressive tool discovery gateway for MCP.** Aggregates tools from multiple upstream [MCP](https://modelcontextprotocol.io/) servers and exposes them through 4 meta-tools, enabling LLMs to discover and use hundreds of tools without loading all schemas upfront.

```text
LLM
 │
 └── fastmcp-gateway (4 meta-tools)
       ├── discover_tools    → browse domains and tools
       ├── get_tool_schema   → get parameter schema for a tool
       ├── execute_tool      → run any discovered tool
       │     ├── apollo      (upstream MCP server)
       │     ├── hubspot     (upstream MCP server)
       │     ├── slack       (upstream MCP server)
       │     └── ...
       └── refresh_registry  → re-query upstreams for changes
```

## Why?

When an LLM connects to many MCP servers, it receives all tool schemas at once. With 100+ tools, context windows fill up and tool selection accuracy drops. **fastmcp-gateway** solves this with progressive discovery: the LLM starts with 4 meta-tools and loads individual schemas on demand.

## Install

```bash
pip install fastmcp-gateway
```

## Quick Start

### Python API

```python
import asyncio
from fastmcp_gateway import GatewayServer

gateway = GatewayServer(
    {
        "apollo": "http://apollo-mcp:8080/mcp",
        "hubspot": "http://hubspot-mcp:8080/mcp",
    },
    refresh_interval=300,  # Re-query upstreams every 5 minutes (optional)
)

async def main():
    await gateway.populate()     # Discover tools from upstreams
    gateway.run(transport="streamable-http", port=8080)

asyncio.run(main())
```

### CLI

```bash
export GATEWAY_UPSTREAMS='{"apollo": "http://apollo-mcp:8080/mcp", "hubspot": "http://hubspot-mcp:8080/mcp"}'
python -m fastmcp_gateway
```

The gateway starts on `http://0.0.0.0:8080/mcp` and exposes 4 tools to any MCP client.

## How It Works

1. **`discover_tools()`** — Call with no arguments to see all domains and tool counts. Call with `domain="apollo"` to see that domain's tools with descriptions. Pass `format="signatures"` to receive Python-style function signatures (`apollo_search(query: str, limit: int = None) -> any`) instead of the default JSON summary — useful when the LLM will subsequently write code against the listed tools.

2. **`get_tool_schema("apollo_people_search")`** — Returns the full JSON Schema for a tool's parameters. Supports fuzzy matching.

3. **`execute_tool("apollo_people_search", {"query": "Anthropic"})`** — Routes the call to the correct upstream server and returns the result.

4. **`refresh_registry()`** — Re-query all upstream servers and return a summary of added/removed tools per domain. Useful when upstreams are updated while the gateway is running.

LLMs learn the workflow from the gateway's built-in system instructions and only load schemas for tools they actually need.

## Configuration

All configuration is via environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GATEWAY_UPSTREAMS` | Yes | — | JSON object: `{"domain": "url", ...}` or `{"domain": {"url": "...", "allowed_tools": [...], "denied_tools": [...]}}` (see Access Control) |
| `GATEWAY_NAME` | No | `fastmcp-gateway` | Server name |
| `GATEWAY_HOST` | No | `0.0.0.0` | Bind address |
| `GATEWAY_PORT` | No | `8080` | Bind port |
| `GATEWAY_INSTRUCTIONS` | No | Built-in | Custom LLM system instructions |
| `GATEWAY_REGISTRY_AUTH_TOKEN` | No | — | Bearer token for upstream discovery |
| `GATEWAY_DOMAIN_DESCRIPTIONS` | No | — | JSON object: `{"domain": "description", ...}` |
| `GATEWAY_UPSTREAM_HEADERS` | No | — | JSON object: `{"domain": {"Header": "Value"}, ...}` |
| `GATEWAY_REFRESH_INTERVAL` | No | Disabled | Seconds between automatic registry refresh cycles |
| `GATEWAY_HOOK_MODULE` | No | — | Python module path for execution hooks: `module.path:factory_function` |
| `GATEWAY_REGISTRATION_TOKEN` | No | — | Shared secret for dynamic registration endpoints (see below) |
| `GATEWAY_CODE_MODE` | No | `false` | Enable the experimental `execute_code` meta-tool (see Code Mode) |
| `GATEWAY_CODE_MODE_MAX_DURATION_SECS` | No | `30` | Per-run wall-clock cap for `execute_code` |
| `GATEWAY_CODE_MODE_MAX_MEMORY` | No | `268435456` | Per-run heap memory cap (bytes) for `execute_code` |
| `GATEWAY_CODE_MODE_MAX_ALLOCATIONS` | No | `10000000` | Per-run allocation cap for `execute_code` |
| `GATEWAY_CODE_MODE_MAX_RECURSION_DEPTH` | No | `200` | Per-run stack depth cap for `execute_code` |
| `GATEWAY_CODE_MODE_MAX_NESTED_CALLS` | No | `50` | Max number of upstream tool calls one `execute_code` run may make |
| `GATEWAY_CODE_MODE_AUDIT_VERBATIM` | No | `false` | Emit raw code body at DEBUG in audit logs (PII-sensitive) |
| `LOG_LEVEL` | No | `INFO` | Logging level |

### Per-Upstream Auth

If your upstream servers require different authentication, use `GATEWAY_UPSTREAM_HEADERS` to set per-domain headers:

```bash
export GATEWAY_UPSTREAM_HEADERS='{"ahrefs": {"Authorization": "Bearer sk-xxx"}}'
```

Domains without overrides use request passthrough (headers from the incoming MCP request are forwarded to the upstream).

## Dynamic Upstream Registration

When `GATEWAY_REGISTRATION_TOKEN` is set, the gateway exposes REST endpoints for runtime upstream management — add, remove, and list upstream MCP servers without restarting.

### Endpoints

All endpoints require `Authorization: Bearer <token>` matching the configured token.

**Register an upstream:**

```bash
curl -X POST http://gateway:8080/registry/servers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"domain": "apollo", "url": "http://apollo-mcp:8080/mcp", "description": "Apollo.io CRM"}'
```

Response: `{"registered": "apollo", "url": "...", "tools_discovered": 12, "tools_added": ["search", ...]}`

**Deregister an upstream:**

```bash
curl -X DELETE http://gateway:8080/registry/servers/apollo \
  -H "Authorization: Bearer $TOKEN"
```

**List registered upstreams:**

```bash
curl http://gateway:8080/registry/servers \
  -H "Authorization: Bearer $TOKEN"
```

### Python API

```python
gateway = GatewayServer(upstreams, registration_token="secret-token")
```

When the token is not set (default), the registration endpoints are **not** mounted — existing deployments are unaffected.

### Thread Safety

All registry mutations (populate, add, remove, refresh) are serialized with an `asyncio.Lock` to prevent concurrent corruption.

## Access Control

Restrict which downstream tools are exposed through the gateway using per-upstream allow/deny lists with glob matching. Policies are applied during registry population — blocked tools never enter the registry, so every meta-tool (`discover_tools`, `get_tool_schema`, `execute_tool`, search) sees the filtered view automatically.

### Configuration (env var)

Extend `GATEWAY_UPSTREAMS` values with optional `allowed_tools` / `denied_tools` lists. Simple string values still work as before.

```bash
export GATEWAY_UPSTREAMS='{
  "apollo": {
    "url": "http://apollo:8080/mcp",
    "allowed_tools": ["apollo_search_*", "apollo_contact_*"]
  },
  "hubspot": {
    "url": "http://hubspot:8080/mcp",
    "denied_tools": ["*_delete"]
  },
  "linear": "http://linear:8080/mcp"
}'
```

### Configuration (Python API)

Pass per-upstream filters inline, or build an `AccessPolicy` and pass it explicitly.

```python
from fastmcp_gateway import AccessPolicy, GatewayServer

policy = AccessPolicy(
    allow={
        "apollo":  ["apollo_search_*", "apollo_contact_*"],
        "hubspot": ["*"],
    },
    deny={"apollo": ["*_delete"]},
)
gateway = GatewayServer(
    {"apollo": "http://apollo:8080/mcp", "hubspot": "http://hubspot:8080/mcp"},
    access_policy=policy,
)
```

### Semantics

Patterns use `fnmatch.fnmatchcase` (case-sensitive `*` / `?` globs). Matched against both the registered tool name and its `original_name` so collision-prefix renames can't bypass policy.

- **`allow`**: when non-empty, only domains listed here are exposed, and only their tools matching at least one pattern. Domains absent from a non-empty `allow` map are fully denied. Leave empty to allow every domain by default.
- **`deny`**: always applied after `allow`. A tool matching a `deny` pattern is blocked even if it also matches an `allow` pattern.

When both object-shaped `upstreams` and an explicit `access_policy=` are provided, the explicit argument wins.

## Execution Hooks

Hooks provide middleware-style lifecycle callbacks around tool execution and discovery. Use them for authentication, authorization, token exchange, audit logging, or result transformation.

### Python API

```python
from fastmcp_gateway import GatewayServer, ExecutionContext, ExecutionDenied

class AuthHook:
    async def on_authenticate(self, headers: dict[str, str]):
        token = headers.get("authorization", "").removeprefix("Bearer ")
        return validate_jwt(token)  # Return user identity or None

    async def before_execute(self, context: ExecutionContext):
        if not has_permission(context.user, context.tool.domain):
            raise ExecutionDenied("Insufficient permissions", code="forbidden")
        # Inject headers for the upstream server
        context.extra_headers["X-User-Token"] = exchange_token(context.user)

gateway = GatewayServer(upstreams, hooks=[AuthHook()])
```

### CLI (env var)

Point `GATEWAY_HOOK_MODULE` at a factory function that returns a list of hook instances:

```bash
export GATEWAY_HOOK_MODULE='my_package.hooks:create_hooks'
```

### Hook Lifecycle

For each `execute_tool` call:

1. **`on_authenticate(headers)`** — Extract user identity from request headers. Last non-None result wins across multiple hooks.
2. **`before_execute(context)`** — Validate permissions, mutate arguments, set `extra_headers`. Raise `ExecutionDenied` to block.
3. **Upstream call** — `extra_headers` merge with highest priority over static `upstream_headers`.
4. **`after_execute(context, result, is_error)`** — Transform or log the result. Each hook receives the previous hook's output.
5. **`on_error(context, error)`** — Observability only (exceptions in hooks are logged, not raised).

All methods are optional — implement only the ones you need.

### Tool Visibility Hooks

The `after_list_tools` hook phase lets you filter tool lists before returning them to clients — useful for per-user access control:

```python
from fastmcp_gateway import ListToolsContext

class AccessControlHook:
    async def after_list_tools(self, context: ListToolsContext, tools: list) -> list:
        # Filter tools based on user permissions
        return [t for t in tools if has_access(context.user, t.domain)]
```

Hidden tools also return `tool_not_found` from `get_tool_schema` to prevent information leakage.

## Code Mode (Experimental)

**Experimental, off by default.** Code mode exposes a fifth meta-tool, `execute_code`, that runs LLM-authored Python in a [Monty](https://github.com/pydantic/monty) sandbox. Every registered tool is pre-bound as a named async callable inside the sandbox, so the model can chain calls — and use `asyncio.gather` to fan out — in a single round-trip without intermediate payloads passing through the agent's context window.

> **Not for analytical workloads.** The Monty sandbox is sized for small-payload cross-tool chaining (dozens of rows, kilobytes of JSON). Large-payload data analysis belongs in a dedicated analytics server with a full Python sandbox.

### Install the extra

```bash
pip install "fastmcp-gateway[code-mode]"
```

### Enable

```python
from fastmcp_gateway import GatewayServer

async def may_use_code_mode(user, context) -> bool:
    return user.id in {"alice", "bob"}  # bind this to your policy engine

gateway = GatewayServer(
    {"crm": "http://crm:8080/mcp", "analytics": "http://analytics:8080/mcp"},
    code_mode=True,
    code_mode_authorizer=may_use_code_mode,  # optional; any authenticated caller allowed when None
)
```

Or via env vars:

```bash
export GATEWAY_CODE_MODE=true
export GATEWAY_CODE_MODE_MAX_DURATION_SECS=30
export GATEWAY_CODE_MODE_MAX_NESTED_CALLS=50
```

### How the LLM uses it

Call `discover_tools(format="signatures")` to get readable Python signatures first, then write code that calls those functions:

```python
# What the LLM emits as the `code` argument to execute_code:
people = await crm_search(query="Anthropic", limit=5)
emails = [p["email"] for p in people["people"]]
{"count": len(emails), "emails": emails}
```

### Safety guarantees

- Every nested tool call goes through the same `before_execute` / `after_execute` hook pipeline as a direct `execute_tool`, so access policies and audit hooks apply unchanged.
- Only tools surviving `after_list_tools` filtering are bound into the sandbox namespace — unauthorized tool names never appear as callables, so an attacker can't enumerate them by reading the sandbox scope.
- Outer-request headers and user identity are captured once at the boundary and closed over in each wrapper; the sandbox's worker thread never reads the auth ContextVar directly.
- Resource limits (duration, memory, allocations, recursion depth, nested-call count) apply to every run.
- Audit: the default `code_mode.invoked` INFO record carries `code_sha256`, `tool_names_invoked`, `step_count`, and `duration_ms`. Raw code is only emitted at DEBUG when `code_mode_audit_verbatim=True` — enable only for debugging; raw LLM code is PII-sensitive.

### Constructor reference

| Parameter | Default | Description |
|---|---|---|
| `code_mode` | `False` | Master switch; gates `execute_code` registration |
| `code_mode_authorizer` | `None` | Async `(user, context) -> bool` per-call permission check |
| `code_mode_limits` | `CodeModeLimits()` | `max_duration_secs=30`, `max_memory=256 MiB`, `max_allocations=10M`, `max_recursion_depth=200`, `max_nested_calls=50` |
| `code_mode_audit_verbatim` | `False` | Emit raw code at DEBUG (PII-risk; leave off in prod) |

## Observability

The gateway emits OpenTelemetry spans for all operations. Bring your own exporter (Logfire, Jaeger, OTLP, etc.) — the gateway uses the `opentelemetry-api` and will pick up any configured `TracerProvider`.

Key spans: `gateway.discover_tools`, `gateway.get_tool_schema`, `gateway.execute_tool`, `gateway.refresh_registry`, `gateway.populate_all`, `gateway.background_refresh`.

Each span includes attributes including `gateway.domain`, `gateway.tool_name`, `gateway.result_count`, and `gateway.error_code` for filtering and alerting.

## Error Handling

All meta-tools return structured JSON errors with a `code` field for programmatic handling and a human-readable `error` message:

```json
{"error": "Unknown tool 'crm_contacts'.", "code": "tool_not_found", "details": {"suggestions": ["crm_contacts_search"]}}
```

Error codes: `tool_not_found`, `domain_not_found`, `group_not_found`, `execution_error`, `upstream_error`, `refresh_error`.

## Tool Name Collisions

When two upstream domains register tools with the same name, the gateway automatically prefixes both with their domain name to prevent conflicts:

```text
apollo registers "search"  →  apollo_search
hubspot registers "search" →  hubspot_search
```

The original names remain searchable via `discover_tools(query="search")`.

## MCP Handshake Instructions

After `populate()`, the gateway automatically builds domain-aware instructions that are included in the MCP `InitializeResult` handshake. MCP clients immediately know what tool domains are available without calling `discover_tools()` first:

```text
You have access to a tool discovery gateway with tools across these domains:

- **apollo** (12 tools) — Apollo.io CRM and sales intelligence
- **hubspot** (8 tools) — HubSpot CRM for contacts, companies, and deals

Workflow: discover_tools() → get_tool_schema() → execute_tool()
```

Instructions are automatically rebuilt when the registry changes during background refresh or dynamic registration. Custom `instructions=` passed at construction time are never overwritten.

## Health Endpoints

The gateway exposes Kubernetes-compatible health checks:

- **`GET /healthz`** — Liveness probe. Always returns 200.
- **`GET /readyz`** — Readiness probe. Returns 200 if tools are populated, 503 otherwise.

## Docker & Kubernetes

See [`examples/kubernetes/`](examples/kubernetes/) for a ready-to-use Dockerfile and Kubernetes manifests.

```bash
# Build
docker build -f examples/kubernetes/Dockerfile -t fastmcp-gateway .

# Run
docker run -e GATEWAY_UPSTREAMS='{"svc": "http://host.docker.internal:8080/mcp"}' \
  -p 8080:8080 fastmcp-gateway
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, architecture overview, and guidelines.

## License

Apache License 2.0. See [LICENSE](LICENSE).

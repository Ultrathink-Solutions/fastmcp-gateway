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

1. **`discover_tools()`** — Call with no arguments to see all domains and tool counts. Call with `domain="apollo"` to see that domain's tools with descriptions.

2. **`get_tool_schema("apollo_people_search")`** — Returns the full JSON Schema for a tool's parameters. Supports fuzzy matching.

3. **`execute_tool("apollo_people_search", {"query": "Anthropic"})`** — Routes the call to the correct upstream server and returns the result.

4. **`refresh_registry()`** — Re-query all upstream servers and return a summary of added/removed tools per domain. Useful when upstreams are updated while the gateway is running.

LLMs learn the workflow from the gateway's built-in system instructions and only load schemas for tools they actually need.

## Configuration

All configuration is via environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GATEWAY_UPSTREAMS` | Yes | — | JSON object: `{"domain": "url", ...}` |
| `GATEWAY_NAME` | No | `fastmcp-gateway` | Server name |
| `GATEWAY_HOST` | No | `0.0.0.0` | Bind address |
| `GATEWAY_PORT` | No | `8080` | Bind port |
| `GATEWAY_INSTRUCTIONS` | No | Built-in | Custom LLM system instructions |
| `GATEWAY_REGISTRY_AUTH_TOKEN` | No | — | Bearer token for upstream discovery |
| `GATEWAY_DOMAIN_DESCRIPTIONS` | No | — | JSON object: `{"domain": "description", ...}` |
| `GATEWAY_UPSTREAM_HEADERS` | No | — | JSON object: `{"domain": {"Header": "Value"}, ...}` |
| `GATEWAY_REFRESH_INTERVAL` | No | Disabled | Seconds between automatic registry refresh cycles |
| `GATEWAY_HOOK_MODULE` | No | — | Python module path for execution hooks: `module.path:factory_function` |
| `LOG_LEVEL` | No | `INFO` | Logging level |

### Per-Upstream Auth

If your upstream servers require different authentication, use `GATEWAY_UPSTREAM_HEADERS` to set per-domain headers:

```bash
export GATEWAY_UPSTREAM_HEADERS='{"ahrefs": {"Authorization": "Bearer sk-xxx"}}'
```

Domains without overrides use request passthrough (headers from the incoming MCP request are forwarded to the upstream).

## Execution Hooks

Hooks provide middleware-style lifecycle callbacks around tool execution. Use them for authentication, authorization, token exchange, audit logging, or result transformation.

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

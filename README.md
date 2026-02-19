# fastmcp-gateway

[![PyPI](https://img.shields.io/pypi/v/fastmcp-gateway)](https://pypi.org/project/fastmcp-gateway/)
[![Python](https://img.shields.io/pypi/pyversions/fastmcp-gateway)](https://pypi.org/project/fastmcp-gateway/)
[![License](https://img.shields.io/github/license/Ultrathink-Solutions/fastmcp-gateway)](LICENSE)
[![CI](https://github.com/Ultrathink-Solutions/fastmcp-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/Ultrathink-Solutions/fastmcp-gateway/actions/workflows/ci.yml)

**Progressive tool discovery gateway for MCP.** Aggregates tools from multiple upstream [MCP](https://modelcontextprotocol.io/) servers and exposes them through 3 meta-tools, enabling LLMs to discover and use hundreds of tools without loading all schemas upfront.

```text
LLM
 │
 └── fastmcp-gateway (3 meta-tools)
       ├── discover_tools    → browse domains and tools
       ├── get_tool_schema   → get parameter schema for a tool
       └── execute_tool      → run any discovered tool
             ├── apollo      (upstream MCP server)
             ├── hubspot     (upstream MCP server)
             ├── slack       (upstream MCP server)
             └── ...
```

## Why?

When an LLM connects to many MCP servers, it receives all tool schemas at once. With 100+ tools, context windows fill up and tool selection accuracy drops. **fastmcp-gateway** solves this with progressive discovery: the LLM starts with 3 meta-tools and loads individual schemas on demand.

## Install

```bash
pip install fastmcp-gateway
```

## Quick Start

### Python API

```python
import asyncio
from fastmcp_gateway import GatewayServer

gateway = GatewayServer({
    "apollo": "http://apollo-mcp:8080/mcp",
    "hubspot": "http://hubspot-mcp:8080/mcp",
})

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

The gateway starts on `http://0.0.0.0:8080/mcp` and exposes 3 tools to any MCP client.

## How It Works

1. **`discover_tools()`** — Call with no arguments to see all domains and tool counts. Call with `domain="apollo"` to see that domain's tools with descriptions.

2. **`get_tool_schema("apollo_people_search")`** — Returns the full JSON Schema for a tool's parameters. Supports fuzzy matching.

3. **`execute_tool("apollo_people_search", {"query": "Anthropic"})`** — Routes the call to the correct upstream server and returns the result.

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
| `LOG_LEVEL` | No | `INFO` | Logging level |

### Per-Upstream Auth

If your upstream servers require different authentication, use `GATEWAY_UPSTREAM_HEADERS` to set per-domain headers:

```bash
export GATEWAY_UPSTREAM_HEADERS='{"ahrefs": {"Authorization": "Bearer sk-xxx"}}'
```

Domains without overrides use request passthrough (headers from the incoming MCP request are forwarded to the upstream).

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

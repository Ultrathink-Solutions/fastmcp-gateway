# fastmcp-gateway

## Project Overview

Progressive tool discovery gateway for MCP, built on FastMCP. Exposes 3 meta-tools (`discover_tools`, `get_tool_schema`, `execute_tool`) to enable LLMs to discover and load tool schemas on-demand.

## Tech Stack

- **Python 3.11+** with type annotations
- **FastMCP** (>=2.0) for MCP server framework
- **OpenTelemetry API** for observability
- **pytest + pytest-asyncio** for testing
- **ruff** for formatting and linting
- **pyright** for type checking

## Package Structure

```text
src/fastmcp_gateway/
    __init__.py        # Public API: GatewayServer
    gateway.py         # GatewayServer class (main entry point)
    registry.py        # ToolRegistry (in-memory tool store)
    meta_tools.py      # 3 meta-tool implementations
    client_manager.py  # UpstreamManager (dual connection strategy)
```

## Commands

```bash
# Install in dev mode
pip install -e ".[dev]"

# Run tests
pytest

# Lint and format
ruff check src tests
ruff format src tests

# Type check
pyright
```

## Conventions

- Use `src/` layout with `fastmcp_gateway` package
- All async functions use `async def`
- Type annotations on all public functions
- Tests mirror source structure in `tests/`
- One Linear issue per Graphite branch/PR

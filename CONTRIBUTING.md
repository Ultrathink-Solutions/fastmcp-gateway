# Contributing to fastmcp-gateway

Thank you for your interest in contributing! This guide will help you get started.

## Prerequisites

- Python 3.11 or later
- [pip](https://pip.pypa.io/) or [uv](https://github.com/astral-sh/uv) for package management

## Development Setup

1. Clone the repository:

```bash
git clone https://github.com/Ultrathink-Solutions/fastmcp-gateway.git
cd fastmcp-gateway
```

2. Install in development mode:

```bash
pip install -e ".[dev]"
```

3. Verify the setup:

```bash
pytest tests/ -v
```

## Code Quality

All changes must pass the following checks (run by CI on every PR):

```bash
# Linting
ruff check src/ tests/

# Formatting
ruff format --check src/ tests/

# Type checking
pyright src/

# Tests
pytest tests/ -v
```

To auto-fix formatting and lint issues:

```bash
ruff check --fix src/ tests/
ruff format src/ tests/
```

## Architecture

The codebase follows a clean separation of concerns:

```text
src/fastmcp_gateway/
    __init__.py        # Public API: GatewayServer
    gateway.py         # GatewayServer class (main entry point, health routes)
    registry.py        # ToolRegistry (in-memory tool index with fuzzy search)
    meta_tools.py      # 3 meta-tools: discover_tools, get_tool_schema, execute_tool
    client_manager.py  # UpstreamManager (MCP client lifecycle, per-domain routing)
    __main__.py        # CLI entry point with env var configuration
```

**Key concepts:**

- **ToolRegistry** stores tool metadata indexed by domain. Supports fuzzy matching for `get_tool_schema`.
- **UpstreamManager** manages MCP client connections to upstream servers. Uses a dual-client strategy: registry clients for startup discovery, execution clients (fresh per-call) for tool invocation.
- **Meta-tools** are the 3 tools exposed to LLMs: `discover_tools` (browse domains/tools), `get_tool_schema` (get parameter schema), `execute_tool` (invoke upstream tool).
- **GatewayServer** wires everything together and mounts on FastMCP.

## Pull Request Guidelines

- **One change per PR.** Keep PRs focused on a single concern.
- **Conventional commits.** Use prefixes like `feat:`, `fix:`, `refactor:`, `test:`, `chore:`.
- **Tests required.** All new features and bug fixes should include tests.
- **CI must pass.** PRs are not merged until linting, type checking, and tests pass.
- **Type annotations.** All public functions must have type annotations.

## Reporting Issues

Use [GitHub Issues](https://github.com/Ultrathink-Solutions/fastmcp-gateway/issues) to report bugs or request features. Please include:

- Steps to reproduce (for bugs)
- Expected vs. actual behavior
- Python version and OS
- Relevant error messages or logs

## License

By contributing, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).

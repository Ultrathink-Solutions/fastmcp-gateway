# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-02-19

### Fixed

- Resolve nested event loop crash in `__main__.py` entry point — `asyncio.run()` and `gateway.run()` (which calls `anyio.run()`) no longer collide (#12)

## [0.1.0] - 2026-02-18

### Added

- Progressive tool discovery gateway with three meta-tools: `discover_tools`, `get_tool_schema`, `execute_tool` (#1, #2, #3, #4, #5, #6)
- Registry population with automatic tool discovery from upstream MCP servers (#2)
- Upstream client management with dual connection strategy (streamable-HTTP + SSE fallback) (#3)
- `discover_tools` meta-tool with 4 query modes: list all domains, search by keyword, filter by domain, full catalog (#4)
- `get_tool_schema` meta-tool with fuzzy matching for tool name resolution (#5)
- `execute_tool` meta-tool with upstream routing and argument forwarding (#6)
- Per-upstream auth headers via `GATEWAY_UPSTREAM_HEADERS` env var (#10)
- Standalone server entry point (`python -m fastmcp_gateway`) with env-based configuration (#10)
- Health check endpoints (`/healthz`, `/readyz`) for Kubernetes readiness/liveness probes (#10)
- Domain descriptions for human-readable tool discovery output (#10)
- Registry auth token support for authenticated upstream connections (#10)
- Integration tests with real in-process upstream MCP servers (#7)
- PyPI package publishing via GitHub Actions with OIDC trusted publisher (#11)
- Kubernetes deployment examples and documentation (#11)

### Changed

- Migrated `ToolEntry` and `DomainInfo` from dataclasses to Pydantic models (#9)

[0.1.1]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/releases/tag/v0.1.0

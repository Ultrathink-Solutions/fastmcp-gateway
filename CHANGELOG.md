# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.1] - 2026-02-28

### Fixed

- **Timing-safe token comparison**: Registration endpoint auth now uses `hmac.compare_digest` to prevent timing side-channel attacks (#30, ULT-1233)
- **Stale headers on upsert**: `add_upstream()` now clears old `upstream_headers` when re-registering without headers, instead of silently preserving them (#30)
- **Client resource leak**: `remove_upstream()` is now async and properly closes the persistent `Client` connection (#30)
- **GET endpoint read consistency**: `GET /registry/servers` now holds `_registry_lock` to prevent reading partially-mutated state during concurrent registration (#30)

### Added

- **URL scheme validation**: `POST /registry/servers` rejects URLs that don't use `http://` or `https://` scheme (#30)
- **Headers payload validation**: `POST /registry/servers` rejects `headers` values that aren't `dict[str, str]`, preventing downstream 500s from malformed input (#30)
- **Token entropy warning**: Logs a warning at startup when `GATEWAY_REGISTRATION_TOKEN` is shorter than 16 characters (#30)
- **`ToolRegistry.get_domain_description()`**: Public accessor for domain descriptions, replacing direct access to the private `_domain_descriptions` dict (#30)

## [0.6.0] - 2026-02-28

### Added

- **Dynamic upstream registration API**: REST endpoints for runtime upstream management — add, remove, and list upstream MCP servers without restarting the gateway (#28, ULT-1233)
  - `POST /registry/servers` — Register a new upstream with domain, URL, description, and optional auth headers
  - `DELETE /registry/servers/{domain}` — Deregister an upstream and remove all its tools
  - `GET /registry/servers` — List all registered upstreams with tool counts
- **`GATEWAY_REGISTRATION_TOKEN` env var**: Shared secret that protects the registration endpoints — when not set, endpoints are not mounted (backwards-compatible) (#28)
- **`UpstreamManager.add_upstream()`**: Add a new upstream at runtime, create a persistent client, and populate its tools into the registry — supports idempotent upsert (#28)
- **`UpstreamManager.remove_upstream()`**: Remove an upstream and all its tools from the registry at runtime (#28)
- **`UpstreamManager.list_upstreams()`**: Return a snapshot of all registered upstreams (domain to URL mapping) (#28)
- **`GatewayServer(registration_token=)`**: New constructor parameter to enable the registration API (#28)
- **Registry thread safety**: `asyncio.Lock` protects all registry mutation paths (populate, add, remove, refresh) to prevent concurrent corruption (#28)

## [0.5.1] - 2026-02-28

### Fixed

- **Collision-prefixed tool dispatch**: `execute_tool` now sends the original upstream tool name (e.g., `get_server_info`) instead of the collision-prefixed gateway name (e.g., `snowflake_get_server_info`) when routing to upstream servers — upstream MCP servers only know their original tool names (#27, ULT-1222)

## [0.5.0] - 2026-02-28

### Added

- **Dynamic MCP instructions after `populate()`**: The gateway now automatically builds domain-aware instructions that are returned in the MCP `InitializeResult` handshake — MCP clients immediately know what tool domains are available without calling `discover_tools()` first (#25, ULT-1220)
- **Domain summary in instructions**: Each domain's name, tool count, and description (if configured) are included in the auto-generated instructions
- **Background refresh updates instructions**: When the registry changes during background refresh, instructions are automatically rebuilt to reflect new/removed domains (#25)

### Changed

- Extracted `_apply_domain_descriptions()` method for reuse in both `populate()` and background refresh paths (#25)
- Custom `instructions=` passed at construction time are never overwritten by dynamic content (#25)
- Bumped version to 0.5.0

## [0.4.0] - 2026-02-27

### Added

- **`after_list_tools` hook phase**: New lifecycle callback for filtering tool lists before returning to clients — enables per-user access control over tool visibility via SpiceDB or similar authorization systems (#23)
- **`ListToolsContext` dataclass**: Context carrier for `after_list_tools` hooks with `domain`, `headers`, and `user` fields (#23)
- **`HookRunner.run_after_list_tools()`**: Pipelines tool lists through registered hooks, with input list copying to prevent mutation (#23)
- **Hook integration in `discover_tools`**: All 4 query modes (domain summary, domain tools, domain+group, keyword search) now pass through `after_list_tools` hooks — domain summary is rebuilt from filtered results (#23)
- **Hook integration in `get_tool_schema`**: Hidden tools return `tool_not_found` to prevent information leakage (#23)
- **`ListToolsContext` exported from `fastmcp_gateway`**: Available as a public API for hook implementations (#23)

### Changed

- `register_meta_tools()` now authenticates and applies `after_list_tools` hooks before returning tool lists (#23)
- Bumped version to 0.4.0

## [0.3.0] - 2026-02-24

### Added

- **Execution hooks system**: Middleware-style lifecycle callbacks around tool execution — implement any subset of `on_authenticate`, `before_execute`, `after_execute`, `on_error` via the `Hook` protocol (#21)
- **`ExecutionContext` carrier**: Mutable dataclass that flows through the hook pipeline, carrying tool entry, arguments, headers, user identity, `extra_headers`, and hook-to-hook metadata (#21)
- **`ExecutionDenied` exception**: Hooks can raise this in `before_execute` to block tool execution with a structured error response and custom error code (#21)
- **`HookRunner` orchestrator**: Manages hook registration and executes lifecycle methods in order — `run_authenticate` (last-non-None wins), `run_before_execute` (chain-halting), `run_after_execute` (result pipeline), `run_on_error` (fault-tolerant) (#21)
- **`GATEWAY_HOOK_MODULE` env var**: Load hooks from external Python modules at startup via `module.path:factory_function` format (#21)
- **`extra_headers` on `execute_tool`**: Hooks can inject per-request headers (e.g. `X-User-Token`) that merge with highest priority over static upstream headers (#21)
- **`GatewayServer.add_hook()` and `.hook_runner`**: Runtime hook registration and access to the hook runner for advanced use cases (#21)

### Changed

- `_make_execution_client()` now supports 3-tier header merge priority: hook `extra_headers` > static `upstream_headers` > ContextVar request passthrough (#21)
- `register_meta_tools()` accepts an optional `hook_runner` parameter (#21)
- `GatewayServer.__init__()` accepts an optional `hooks` parameter (#21)
- Bumped version to 0.3.0

## [0.2.0] - 2026-02-20

### Added

- **Structured error responses**: New `GatewayError` Pydantic model and `error_response()` helper for consistent, machine-parseable error JSON from all meta-tools — includes `error`, `code`, and `details` fields (#14)
- **Tool name collision handling**: When two upstream domains register tools with the same name, both are automatically prefixed with their domain name (e.g., `apollo_search`, `hubspot_search`) to prevent silent overwrites — includes secondary collision guard and same-domain update safety (#15)
- **MCP tool annotations**: All meta-tools now declare `ToolAnnotations` metadata (`readOnlyHint`, `openWorldHint`) so MCP clients can make informed decisions about tool behavior (#16)
- **Auth passthrough helper**: New public `get_user_headers()` function exposes forwarded HTTP headers from the current MCP request context, useful for consumers building on the gateway (#17)
- **OpenTelemetry instrumentation**: Gateway-specific spans with domain, tool name, result count, and error code attributes (#18)
  - Meta-tool spans: `gateway.discover_tools`, `gateway.get_tool_schema`, `gateway.execute_tool`, `gateway.refresh_registry`
  - Upstream client spans: `gateway.populate_all`, `gateway.populate_domain`, `gateway.upstream.execute`
  - Registry spans: `gateway.registry.populate_domain`, `gateway.registry.search`
  - Background refresh span: `gateway.background_refresh`
- **Registry refresh**: Background polling via `GATEWAY_REFRESH_INTERVAL` env var to keep the tool registry up-to-date, plus a manual `refresh_registry` meta-tool that returns per-domain diffs (added/removed tools) — managed by the ASGI server lifespan with graceful cancellation (#19)
- **`RegistryDiff` model**: New Pydantic model tracking per-domain changes (added tools, removed tools, tool count) returned by `populate_domain()` and refresh operations (#19)

### Changed

- `ToolEntry` now includes an `original_name` field to track the pre-collision name when tools are auto-prefixed (#15)
- `ToolRegistry.populate_domain()` now returns `RegistryDiff` instead of `int` for richer change tracking (#19)
- `UpstreamManager` gained `refresh_all()` and `refresh_domain()` methods returning `RegistryDiff` objects (#19)
- Bumped version to 0.2.0

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

[0.6.1]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/releases/tag/v0.1.0

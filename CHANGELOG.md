# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.11.0] - 2026-04-21

### Added

- **`middleware` kwarg on `GatewayServer`**: `GatewayServer(upstreams, middleware=[...])` accepts a list of ASGI middleware wrapped around the gateway's HTTP app. When set, `GatewayServer.run()` builds the ASGI app via `FastMCP.http_app(middleware=...)` and serves it with uvicorn directly; when unset, falls back to `FastMCP.run()` — same code path as before, zero behavior change for existing callers. HTTP-transport only (stdio/sse raise `ValueError` when `middleware` is set, rather than silently dropping it). Middleware list is shallow-copied at construction so post-construction mutations of the caller's list don't leak into the running server.

### Notes

- **Use cases**: host-allowlist filtering (DNS-rebinding defense), request-id injection, rate limiting, CSP headers, structured-logging middleware — anything that benefits from ASGI-level interception of requests to the gateway. Apply in declaration order (first entry outermost), matching the Starlette `Middleware` stack convention.
- **Migration**: none required. `GatewayServer` constructions without `middleware=` behave identically to prior releases.

## [0.10.1] - 2026-04-21

Release-hygiene patch. No functional or API changes from 0.10.0.

### Changed

- **Single source of truth for `__version__`**: `pyproject.toml` now declares `dynamic = ["version"]` and hatchling reads the version string from `src/fastmcp_gateway/__init__.py` at build time. Previous releases required a manual dual-bump in both files; a missed bump in one of them caused the PyPI publish during v0.9.0 to reject with "file already exists", requiring recovery PR #43. Every release from 0.10.1 onward updates exactly one line (`__version__` in `__init__.py`); `pyproject.toml` no longer carries a redundant `version` field. (#45)

### Notes

- **For consumers**: no change. `fastmcp_gateway.__version__` still resolves to the current version at runtime; `importlib.metadata.version("fastmcp-gateway")` returns the same string.
- **For contributors**: when cutting a release, bump only `__version__` in `src/fastmcp_gateway/__init__.py`. The wheel's `METADATA.Version` is generated from it automatically.

## [0.10.0] - 2026-04-21

Security-hardening release. Closes a tool-impersonation / sandbox-escape primitive in the `ToolRegistry.register_tool` ingress.

### Changed (breaking for non-conformant upstreams)

- **`ToolRegistry.register_tool` now validates the upstream tool name** and silently drops unsafe entries with a structured audit log. An unsafe name (dunder, Python keyword / soft-keyword, or Python builtin that would shadow the binding inside the `execute_code` sandbox namespace) is rejected before the entry reaches the registry. The rejection is silent (log at `WARNING`, no exception) so a single bad upstream name in a populate batch does not abort siblings — same convention as the existing "skip tool with empty name" branch in `populate_domain`. (#44)

### Added

- **`fastmcp_gateway.tool_name` module** (internal): new `validate_tool_name(name: str) -> str | None` helper — returns `None` for a safe name or a short diagnostic string for an unsafe one. Consumed by `ToolRegistry.register_tool`; not exported as public API. Compiles the shape regex and denylist from `builtins` + `keyword.kwlist` + `keyword.softkwlist` at import time so the set tracks Python version changes.

### Security

- **Validation rules** applied at `register_tool` ingress:
  - Shape: `^[a-z][a-zA-Z0-9_]{0,63}$` — lowercase first character, alphanumerics and underscores thereafter, length 1 to 64. First-char lowercase rule rejects dunders (`__class__`, `__import__`) and all-uppercase forms (`CLASS`) in a single check. Mixed case is accepted in subsequent characters so camelCase names real upstreams advertise (e.g. `executeQuery`) continue to validate — a strict snake-case rule would reject valid production tools without adding security.
  - No Python keywords, soft-keywords, or builtins that would otherwise pass the shape check (covers `eval`, `exec`, `compile`, `class`, `type`, `match`, `case`, etc.).

### Notes

- **Upgrade path**: legitimate upstreams following any reasonable naming convention see no change. If a tool you depend on fails validation, the `WARNING`-level log line at startup names it explicitly (`Rejected tool registration: domain=X name=Y reason=Z`) — coordinate with the upstream owner to rename to a conformant identifier.
- **Catch-site compatibility**: validation does not raise; existing `try/except` sites around `register_tool` remain unchanged.

## [0.9.0] - 2026-04-21

Security-hardening release. Closes two code-injection primitives in the env-driven configuration path. Both primary changes are **breaking** for configs that relied on the previous permissive defaults — see the upgrade notes below.

### Changed (breaking)

- **`GATEWAY_HOOK_MODULE` now requires an allowlist.** The previous set-and-import behaviour made this env var a code-injection primitive for any process with write access to the gateway's env. `GATEWAY_HOOK_MODULE=my.module:factory` now has no effect unless `GATEWAY_ALLOWED_HOOK_PREFIXES` is also set and the requested module path matches one of its comma-separated prefixes, with dot-boundary matching so `my_org` does not match `my_org_evil`. Deployments that relied on the old behaviour must opt in by adding `GATEWAY_ALLOWED_HOOK_PREFIXES=my_org.hooks,my_other_org.hooks` alongside the existing `GATEWAY_HOOK_MODULE` value. (#40)
- **`code_mode=True` requires an explicit `code_mode_authorizer`.** Previously, `GatewayServer` scanned the hook chain for any `authorize_code_mode` attribute and silently opened the gate if one was found (the auto-discovery path). A downstream hook that happened to expose `authorize_code_mode` returning `True` could therefore bypass the code-mode gate with no explicit opt-in at the call site. Auto-discovery is removed; constructing a gateway with `code_mode=True` and no `code_mode_authorizer` now raises `CodeModeAuthorizerRequiredError` (a `ValueError` subclass) at init time. Callers that want the previous duck-typed behaviour must now pass the hook's method directly: `code_mode_authorizer=my_hook.authorize_code_mode`. (#40)
- **`GATEWAY_CODE_MODE=true` is no longer supported via the CLI / environment.** Code mode now requires programmatic `GatewayServer` construction with an explicit async authorizer. Setting `GATEWAY_CODE_MODE=true` without also constructing `GatewayServer` programmatically causes the process to exit at startup with a typed `CodeModeAuthorizerRequiredError`, logged as an actionable operator message via `__main__`. (#40)

### Added

- **`CodeModeAuthorizerRequiredError`** (importable from `fastmcp_gateway.gateway`): dedicated `ValueError` subclass for the authorizer-missing case. Lets callers route this specific misconfiguration to a user-friendly operator message without string-matching the error text. Not re-exported at the package root — broad `except ValueError` catch-sites continue to work unchanged. (#40)
- **`fastmcp_gateway._hook_loading` helpers**: the hook-loading logic (previously inline in `__main__.py`) is split into `_parse_allowed_hook_prefixes`, `_hook_module_allowed`, and `_load_hooks`. Private by underscore convention; exposed at the module path so tests can import them as ordinary symbols. (#40)

### Security

- **Strict authorizer shape-check**: when `code_mode=True`, the constructor now rejects a synchronous function / object passed as `code_mode_authorizer` using `inspect.iscoroutinefunction` instead of a plain `callable()` test. A sync authorizer would otherwise blow up with `TypeError: object bool can't be used in 'await' expression` at the first `execute_code` invocation — a runtime-only landmine that now fails fast at init. The check is gated on `code_mode=True` so sync authorizers passed alongside `code_mode=False` (where they would never be invoked) remain accepted. (#40)
- **CI supply-chain hardening**: all third-party GitHub Actions now pinned to full commit SHAs (previously tag refs, which the action author could re-point after a PR was approved). Pinned at their current commit tips with human-readable version comments: `actions/checkout@v6.0.2`, `astral-sh/setup-uv@v8.0.0`, `actions/setup-python@v5.6.0`, `pypa/gh-action-pypi-publish@release/v1`. (#41)

### Notes

- **Upgrade path for deployments currently using `GATEWAY_HOOK_MODULE`**:
  1. Identify the module paths you load (e.g. `my_org.hooks:build_hooks`).
  2. Set `GATEWAY_ALLOWED_HOOK_PREFIXES` to their common prefix (comma-separated for multiple; e.g. `my_org.hooks`).
  3. Deploy. Existing `GATEWAY_HOOK_MODULE` values resume loading.
  Without the allowlist, the hook module is silently ignored (logged at INFO), which surfaces as "no hooks loaded" in startup logs rather than a crash.
- **Upgrade path for deployments currently using `GATEWAY_CODE_MODE=true`**:
  - Switch to programmatic construction: `GatewayServer(code_mode=True, code_mode_authorizer=<async fn>, ...).run(...)`. The env-var form is no longer supported and will exit at startup with a typed error.
- **Catch-site compatibility**: `CodeModeAuthorizerRequiredError` is a `ValueError` subclass, so existing `except ValueError` handlers continue to match. Update the catch site only if you want to route the specific error separately.

## [0.8.0] - 2026-04-16

### Added (experimental)

- **`execute_code` meta-tool**: new experimental tool that runs LLM-authored Python in a [Monty](https://github.com/pydantic/monty) sandbox, with every registered tool exposed as a named async callable inside. Lets the model chain multiple upstream calls — and use `asyncio.gather` to fan-out — in a single round-trip, keeping intermediate payloads out of the agent's context window. Marked **experimental, off by default**.
- **New `GatewayServer` constructor parameters** (all no-ops when `code_mode=False`):
  - `code_mode: bool = False` — gates `execute_code` registration.
  - `code_mode_authorizer: Callable | None` — optional async `(user, context) -> bool` session-level permission check. Typed as `Any` in the public surface so extensions can bind their own identity types without leaking them into OSS.
  - `code_mode_limits: CodeModeLimits | None` — resource caps (duration, memory, allocations, recursion, nested-call count).
  - `code_mode_audit_verbatim: bool = False` — when `True`, raw code body is emitted at DEBUG; default hash+metadata-only audit preserves PII hygiene.
- **`fastmcp_gateway.code_mode` module**: `CodeModeRunner`, `CodeModeLimits`, `CodeModeUnavailableError`.
- **Env-var configuration**: `GATEWAY_CODE_MODE`, `GATEWAY_CODE_MODE_MAX_DURATION_SECS`, `GATEWAY_CODE_MODE_MAX_MEMORY`, `GATEWAY_CODE_MODE_MAX_ALLOCATIONS`, `GATEWAY_CODE_MODE_MAX_RECURSION_DEPTH`, `GATEWAY_CODE_MODE_MAX_NESTED_CALLS`, `GATEWAY_CODE_MODE_AUDIT_VERBATIM`.
- **Optional extra**: install with `pip install "fastmcp-gateway[code-mode]"` to pull in `pydantic-monty>=0.0.12`.

### Safety guarantees

- Every nested tool call goes through the same `before_execute` / `after_execute` hook pipeline as a direct `execute_tool`, so access policies, authz, and audit survive unchanged.
- The sandbox's callable namespace is built from the result of `after_list_tools` hook filtering — tools a user can't see don't even appear as function names.
- Outer-request headers and user identity are captured once at the boundary and closed over in each wrapper; never read via ContextVar from inside Monty's worker thread.
- Structured content from upstream tools round-trips as indexable Python dicts; `asyncio.gather` works inside the sandbox.

### Notes

- Intended for small-payload cross-tool chaining. Large-payload analytical workloads belong in a dedicated analytics server with a full Python sandbox — don't use `execute_code` for dataset crunching.

## [0.7.1] - 2026-04-16

### Added

- **Python-style tool signatures**: new `discover_tools(format="signatures")` rendering mode returns each tool as a plain-text Python function signature (`name(arg: type, …) -> any`) instead of a JSON summary. Easier for LLMs that will subsequently write code against the listed tools than mentally translating JSON Schema.
- **`fastmcp_gateway.signatures` module**: new public helpers `extract_params`, `format_schema`, and `tool_to_signature` for anyone building their own rendering on top of the registry.

### Notes

- `format="schema"` remains the default; existing consumers see no change.
- The mode-1 domain summary (no-arguments call) ignores `format` and always returns JSON, since it reports domain counts rather than a tool list.

## [0.7.0] - 2026-04-16

### Added

- **Per-upstream access policy**: new `AccessPolicy` dataclass for allow/deny tool filtering with `fnmatch` glob patterns. Rules are keyed by domain and matched against both the registered tool name and its `original_name` so collision-prefix renames can't bypass policy.
- **Dual-form rule matching**: a rule may be written in either the fully-qualified `{domain}_{tool}` form or the bare upstream `{tool}` form — both match the same tool regardless of whether collision handling renames it at registration time. Rule authors don't have to predict whether a collision will occur.
- **Object-shaped `GATEWAY_UPSTREAMS`**: env var values may now be either a URL string (existing form, back-compat) or an object with `url` plus `allowed_tools` and/or `denied_tools` lists. A dict counts as a filter config only when it contains one of those filter keys, so fastmcp-native transport dicts that happen to include a bare `"url"` pass through unchanged. Mixed shapes are allowed in the same config.
- **`GatewayServer(access_policy=)`**: new constructor parameter accepting an `AccessPolicy` directly. When both object-shaped upstreams and an explicit `access_policy` are provided, the explicit argument wins.
- **`GatewayServer.access_policy` property**: public read-only accessor for the effective policy resolved during construction. Useful for introspection and testing without reaching into private attributes.
- **Registry-level filtering**: policy is applied during `ToolRegistry.populate_domain`, so rejected tools never enter the registry. Every consumer (`discover_tools`, `get_tool_schema`, `execute_tool`, `search`, `get_all_tool_names`) sees the filtered view automatically — including dynamic registrations via `POST /registry/servers`.

### Changed

- `GatewayServer` constructor `upstreams` parameter now accepts mixed `str | dict[str, Any]` values. Pure string values preserve existing behaviour.
- `UpstreamManager.__init__()` accepts an optional `policy` kwarg, forwarded to every registry population call.
- `ToolRegistry.populate_domain()` accepts an optional `policy` kwarg; rejected tools are skipped with a DEBUG log and surface as a `gateway.policy_filtered_count` span attribute.
- Bumped version to 0.7.0.

## [0.6.4] - 2026-03-09

### Fixed

- **Readyz with empty registry**: `/readyz` now returns 200 even when zero tools are registered. Previously, a gateway with no upstreams (e.g., fresh start before dynamic registration) returned 503, causing Kubernetes to restart the pod in a crash loop.

## [0.6.3] - 2026-03-08

### Fixed

- **Registration endpoint auth passthrough**: `POST /registry/servers` now passes the `headers` payload as `registry_auth_headers` to `add_upstream()`, so the initial `list_tools` discovery call authenticates with the upstream. Previously, headers from the registration request were only stored for tool execution but not used during discovery, causing 401 errors when the registry-controller registered authenticated upstreams.

## [0.6.2] - 2026-03-06

### Fixed

- **Dynamic registration auth**: `add_upstream()` now inherits the startup `registry_auth_headers` (from `GATEWAY_REGISTRY_AUTH_TOKEN`) when no explicit auth headers are provided. Previously, dynamically registered upstreams had no authentication during tool discovery, causing 401 errors from upstream servers that require auth on `list_tools`. (#31)

## [0.6.1] - 2026-02-28

### Fixed

- **Timing-safe token comparison**: Registration endpoint auth now uses `hmac.compare_digest` to prevent timing side-channel attacks (#30)
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

- **Dynamic upstream registration API**: REST endpoints for runtime upstream management — add, remove, and list upstream MCP servers without restarting the gateway (#28)
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

- **Collision-prefixed tool dispatch**: `execute_tool` now sends the original upstream tool name (e.g., `get_server_info`) instead of the collision-prefixed gateway name (e.g., `snowflake_get_server_info`) when routing to upstream servers — upstream MCP servers only know their original tool names (#27)

## [0.5.0] - 2026-02-28

### Added

- **Dynamic MCP instructions after `populate()`**: The gateway now automatically builds domain-aware instructions that are returned in the MCP `InitializeResult` handshake — MCP clients immediately know what tool domains are available without calling `discover_tools()` first (#25)
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

[0.11.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.6.4...v0.7.0
[0.6.4]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.6.3...v0.6.4
[0.6.3]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.6.2...v0.6.3
[0.6.2]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/releases/tag/v0.1.0

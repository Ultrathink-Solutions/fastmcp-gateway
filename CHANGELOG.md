# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.17.0] - 2026-04-23

### Added

- **`RegistrationTokenValidator` protocol + `JWTRegistrationValidator`** in `fastmcp_gateway.registration_auth`: abstract validator shape + concrete JWT-based implementation that verifies `iss`, `aud`, `exp`, and signature on each registration bearer. Gives per-caller identity, automatic rotation (short expiry), and an audit-log trail that a shared static bearer cannot provide.
- **`registration_validator` kwarg on `GatewayServer`**: accepts any `RegistrationTokenValidator`. Mutually exclusive with the existing `registration_token` kwarg — setting both raises `ValueError` at construction. When set, `/registry/servers` routes delegate bearer validation to the validator and 401-reject any bearer the validator rejects.
- **Audit log on successful validation**: each authenticated registration request emits a structured `registry.auth.ok` log line with `subject`, `jti`, `iat`, and `route` fields — the JWT-validator path is the only path that produces this record (the legacy static-bearer path has no principal to record).
- **`GATEWAY_REGISTRATION_ISSUER` / `GATEWAY_REGISTRATION_AUDIENCE` / `GATEWAY_REGISTRATION_VERIFY_KEY` env vars**: when all three are set, the CLI entry point auto-constructs a `JWTRegistrationValidator`. Partial configuration (any subset) is a startup error. `GATEWAY_REGISTRATION_VERIFY_KEY` is a PEM public key and may be multi-line.
- **`GATEWAY_REGISTRATION_ALGORITHMS` env var**: optional comma-separated override of accepted JWT signing algorithms (default `ES256`). The string `none` is explicitly rejected.
- **New dependency `PyJWT[crypto]>=2.8`**: pulls in `cryptography` (~7MB wheel) required for signature verification. The `import jwt` call is deferred to `JWTRegistrationValidator.__init__` so deployments that stay on the static-bearer path — or disable registration — don't pay the import cost at module-load time.

### Deprecated

- **`registration_token` kwarg + `GATEWAY_REGISTRATION_TOKEN` env var**: the shared-static-bearer registration path is retained for one release to give deployments a migration window, but constructing `GatewayServer(registration_token=...)` now emits a `DeprecationWarning`. Migrate to `registration_validator` (programmatic) or the three JWT env vars (CLI). The static-bearer path will be removed in a future release.

### Notes

- **Mutual exclusion is enforced at two layers**: `GatewayServer.__init__` raises `ValueError` when both `registration_token` and `registration_validator` are passed, and the CLI entry point performs the same check across the static-token env and the JWT env vars so the error surfaces at startup rather than at first registration request.
- **No `jti` replay cache in this release**: short expiry (recommended ≤ 5 minutes at the issuer) is the primary replay mitigation. A `jti` cache can be layered on top of a validator without changing the public surface; it's deferred to a future ticket.

## [0.16.0] - 2026-04-23

Security-hardening release. Closes a prompt-injection surface on
``ToolRegistry.populate_domain`` where upstream tool descriptions (and
``inputSchema`` documents) crossed the gateway boundary without
normalization or denylist scanning, and adds input-schema validation
at registry ingest.

### Added

- **`fastmcp_gateway.sanitize` module**: new description sanitizer and
  inputSchema validator applied inside ``ToolRegistry.populate_domain``
  (the registry ingest path). ``sanitize_description()`` runs a fixed pipeline —
  type-guard for non-string descriptions, NFC normalization, C0 /
  zero-width / bidi-format-control stripping, injection-pattern scrub
  against the shared ``INJECTION_PATTERNS`` denylist, and a 2048-char
  length cap with a ``" [truncated]"`` marker. ``validate_input_schema()``
  rejects schemas with ``$ref`` (TOCTOU risk against the sandbox),
  schemas nested beyond depth 5, non-object root types, and non-dict
  ``properties`` values.
- **`fastmcp_gateway.injection_patterns` module**: shared denylist of
  regex patterns (``ignore all previous instructions``, ``<system>``
  tags, etc.) compiled once at import time with ``IGNORECASE`` +
  ``DOTALL`` so case-variant and newline-split attempts still match.
  Exposed as a separate module so follow-on work that needs the same
  pattern set (output guards, conversation-log scrubbers) can import
  it without creating a circular dependency.
- **Expanded invisible-character denylist**: zero-width stripping now
  covers the bidi / directional format controls (U+202A..U+202E,
  U+2066..U+2069) and U+2060 WORD JOINER in addition to the zero-width
  spaces and separators already handled. These codepoints let an
  attacker visually reorder text so the denylist scan sees one string
  while a human reviewer sees another; stripping them before the
  pattern pass forecloses the bypass.
- **Trusted-domain override**: ``sanitize_description(raw,
  skip_pattern_scan=True)`` skips *only* the injection-pattern scrub
  for operator-configured trusted domains whose tool descriptions
  legitimately contain denylist tokens (prompt-processing utilities,
  etc.). Always-on hygiene (NFC, control / zero-width stripping,
  length cap) still applies. Configured per-deployment via code
  (``GatewayServer(sanitizer_trusted_domains=...)``) rather than an
  env toggle so the trust boundary is auditable in-repo.

### Changed

- **Log lines in `sanitize_description` no longer emit attacker-controlled
  content**: previous audit lines included ``value=%r`` and ``match=%r``
  which could leak attacker-supplied text (including credential strings
  or oversized payloads) into ops log aggregators. The log format is
  now metadata-only — type name for the non-string branch; offset +
  length for the pattern-strip branch. Incident triage remains possible;
  attacker amplification of audit logs does not.
- **``ToolRegistry.populate_domain`` runs descriptions through the
  sanitizer and validates ``inputSchema``** before the registry
  accepts the tool. A failing ``inputSchema`` raises
  ``SchemaValidationError`` (subclass of ``ValueError``) so callers
  that already catch the broad type keep working. A poisoned
  description is rewritten to its scrubbed form; a non-string
  description is replaced with the empty string. Neither case aborts
  the surrounding populate batch — one malicious tool can't DoS
  siblings.

### Notes

- **35 existing tests updated**: the stricter ``inputSchema`` validator
  rejects ``{}`` (missing root ``type``) that prior tests used as a
  placeholder. Updated to the minimal valid form ``{"type": "object"}``.
  This is the only consumer-visible contract change; third-party
  consumers that pass truly empty schemas need to migrate to a minimal
  valid schema.
- **Why ``$ref`` is rejected rather than resolved**: resolving a ``$ref``
  at ingest requires either following an external URL (SSRF surface,
  now closed by the registration URL guard in v0.15.0) or walking
  back to the root schema (introduces a TOCTOU window between
  validation and the time the sandbox type renderer reads the schema).
  Static inline schemas are strictly more auditable and the
  rejection is cheap to work around upstream.

## [0.15.0] - 2026-04-23

Security-hardening release. Closes an SSRF surface and a header-smuggling
surface on the ``POST /registry/servers`` endpoint, and adds a structured
400 for malformed-port URLs that previously surfaced as unhandled 500s.

### Added

- **`fastmcp_gateway.url_guard` module**: new SSRF and header-injection
  guards applied before any registration call reaches the upstream
  client. ``validate_registration_url()`` rejects non-``http(s)``
  schemes, empty / ``localhost`` hostnames, ``..`` path-traversal
  segments, and any hostname whose DNS resolution lands inside a denied
  CIDR range (RFC 1918 private, loopback, IPv4 / IPv6 link-local
  including cloud-metadata ``169.254.169.254``, CGNAT, carrier-grade
  NAT).  IPv4-mapped IPv6 addresses (``::ffff:a.b.c.d``) are unwrapped
  before the CIDR check so the mapped form can't bypass an IPv4-only
  denylist.  ``validate_registration_headers()`` rejects any header key
  outside the (empty-by-default) allowlist or inside the routing /
  auth / hop-by-hop denylist (``Host``, ``Authorization``, ``Cookie``,
  ``X-Forwarded-*``, ``Connection``, ``Transfer-Encoding``, etc.).
- **`GATEWAY_URL_GUARD_DNS_TIMEOUT_SECONDS` env var** (default: 5.0
  seconds): caps the per-registration DNS resolution wait to prevent
  a slow / poisoned resolver from stalling the registration path.
  Timeout failures map to the same structured 400 as DNS-failure, not
  a 504.  Non-numeric, zero, or negative values fall back to the
  default rather than producing an immediate DNS-failure.
- **`GATEWAY_URL_GUARD_ALLOW_PRIVATE_RANGES` env var** (default:
  ``false``): opt-in override for on-prem deployments that need to
  register upstreams inside private / internal ranges.  The override
  waives the CIDR denial — it does *not* unconditionally waive the
  plaintext-``http`` ban.  ``http://`` is permitted only when every
  resolved address for the target hostname is inside a denied range;
  a single public-range address in the resolution set keeps the
  plaintext ban in force.  Deployments relying on this flag need
  independent network-layer egress controls.

### Changed

- **``POST /registry/servers`` registration flow** now calls
  ``validate_registration_url()`` and ``validate_registration_headers()``
  before constructing the upstream client.  Rejections surface as
  structured 400 responses with ``{"code": "ssrf_rejected" |
  "header_injection_rejected", "message": "..."}`` rather than
  bubbling out as 500s or silently forwarding attacker-controlled
  headers.
- **Malformed-port URLs** (``https://host:99999/mcp``,
  ``https://host:abc/mcp``) now surface the ``ValueError`` from
  ``urllib.parse.urlparse().port`` as a structured 400
  (``ssrf_rejected``) rather than propagating as an unhandled 500.

### Notes

- **Allowlist is empty by default**: third-party adopters who need
  to forward specific upstream headers must extend
  ``_ALLOWED_HEADER_KEYS`` via a code change, not an env toggle —
  header-forwarding policy is a deployment contract, not a runtime
  config.
- **IPv6 scope IDs stripped**: ``getaddrinfo`` can return
  ``fe80::1%eth0`` for link-local IPv6; the guard strips the ``%...``
  suffix before ``ipaddress.ip_address()`` parsing so the underlying
  address (which *is* in the denied ``fe80::/10`` range) is still
  recognized.
- **Why no env flag on the plaintext-``http`` ban to public hosts**:
  a fail-open env toggle on that specific check is a permanent latent
  bypass that an attacker with process-env write access can flip.
  Deployments that need plaintext transport to a specific public
  upstream should either front it with TLS termination on their own
  network edge or land a code-level allowlist addition — both leave
  an audit trail that an env-var flip would not.

## [0.14.0] - 2026-04-23

Security-hardening release. Closes the tool-schema rug-pull pattern where a
compromised upstream could mutate tool ``(name, description, inputSchema)``
between background refresh cycles without operator visibility.

### Added

- **Schema-digest integrity check on the tool registry**: every `ToolRegistry.populate_domain` call now computes a SHA-256 digest over the canonical `(name, description, inputSchema)` form of the incoming tool set. The first populate for a domain records this digest as the baseline; subsequent populates whose digest diverges from the baseline are refused — the registry state is preserved verbatim, an ERROR-level audit line is emitted (`"Schema integrity violation: domain=... prior_digest=... candidate_digest=... -- refresh refused; registry state unchanged"`), and the returned `RegistryDiff` has `refused=True`. This blocks the rug-pull pattern where a compromised upstream quietly mutates tool schemas between background refresh cycles to smuggle new behaviour past the audit trail.
- **`POST /registry/servers/refresh?expected_digest=<hex>`**: new bearer-authenticated endpoint (mounted when `registration_token` is set) that lets operators acknowledge a legitimate schema evolution. The operator independently verifies the new upstream contract, computes the expected post-transition digest, and presents it as a 64-char lowercase hex query parameter. On match, the transition commits; on mismatch, 409 Conflict with truncated digest pair for diagnostics. There is deliberately no env-flag bypass — env toggles on integrity paths create a permanent latent "off" state that an attacker with process-env write access can flip, whereas a per-call query-param shape forces every transition to be an intentional, audited operator action.
- **`RegistryDiff` fields**: `schema_digest: str | None`, `schema_digest_changed: bool`, `refused: bool`. Backward-compatible defaults (`None` / `False` / `False`) so existing callers that inspect only `added` / `removed` / `tool_count` are unaffected.
- **`ToolRegistry.get_schema_digest(domain)`** and **module-level `compute_schema_digest(tools)`**: public accessors that expose the canonical digest so operators can compute the expected value out-of-band (e.g., from a signed upstream schema manifest).
- **`populate_domain(..., expected_digest=...)`** / **`UpstreamManager.refresh_domain(..., expected_digest=...)`**: kwargs thread the explicit acknowledgement through to the registry gate.

### Notes

- **Migration — no operator action required**: the first `populate_domain` after upgrading records whatever the upstream currently serves as the baseline and emits an INFO-level log line (`"Schema digest baseline established: domain=<name> digest=<hex8>"`). Operators see the audit trail in their log aggregator without any manual baselining step.
- **Background refresh semantics**: the periodic `refresh_interval` loop no longer accepts schema mutations. A compromised upstream's mutation now lands as a refused diff plus an ERROR-level audit line, not as a silent contract swap. Operators who hit this refusal should: (1) verify the new upstream payload is intentional, (2) compute its expected digest via `compute_schema_digest` or the logged candidate fingerprint, and (3) issue a `POST /registry/servers/refresh?expected_digest=<hex>` with that value.
- **Digest stability**: the canonical form uses the upstream-advertised tool name (not the gateway's collision-prefixed name), sorts entries for order-independence, and uses `sort_keys=True` inside each entry — so benign JSON reformatting upstream does not trip the gate. Any actual contract change (new tool, dropped tool, renamed tool, description edit, schema edit) does.

## [0.13.0] - 2026-04-23

Security-hardening release. Closes a scope-probing information-leak in the
fuzzy-match suggestion surface of ``get_tool_schema`` and ``execute_tool``.

### Changed

- **"Did you mean" suggestions now respect visibility filters**: the
  ``get_tool_schema`` and ``execute_tool`` meta-tools previously fed
  ``registry.get_all_tool_names()`` into ``_suggest_tool_names``, which let a
  caller with narrow scopes enumerate every registered tool by sending garbage
  tool-name lookups and reading the suggestions back from the error response.
  Both call sites now route candidates through the same hook-level filter that
  ``tools/list`` uses (via a new ``_visible_tool_names`` closure), so the
  suggestion surface is a strict subset of what the caller could already see
  through ``discover_tools``.

### Notes

- **Zero behavior change for trusted callers**: a caller whose visibility
  already covers the registry sees identical suggestions. Only callers who
  were relying on the leak (i.e. probing the registry despite being filtered)
  observe a difference — and that's the fix.
- **Guard test included**: ``tests/test_suggest_visibility.py`` adds a
  structural assertion that the suggestion surface stays a subset of
  ``discover_tools`` output for the same caller, catching any future drift
  where the suggestion path reconstructs its own parallel filter.

## [0.12.0] - 2026-04-21

### Added

- **`GATEWAY_MIDDLEWARE_MODULE` env var**: loads ASGI middleware from a dotted Python path (``module.path:function_name``) and passes the returned list to ``GatewayServer(middleware=...)``. Mirrors the existing ``GATEWAY_HOOK_MODULE`` shape exactly — same format, same failure modes, same security posture. Lets deployment shims inject host-allowlist filtering, request-id middleware, rate limiting, CSP headers, etc. without modifying the gateway entry point or constructing ``GatewayServer`` programmatically.
- **`GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES` env var**: comma-separated allowlist of module prefixes that ``GATEWAY_MIDDLEWARE_MODULE`` may resolve to. **Required** — without it, ``GATEWAY_MIDDLEWARE_MODULE`` is silently ignored with a startup warning log. Same security rationale as ``GATEWAY_ALLOWED_HOOK_PREFIXES``: without the allowlist, the env var would be a code-injection primitive for any process with write access to the gateway's env. Dot-boundary match (``my_org`` does not match ``my_org_evil``).
- **`fastmcp_gateway._middleware_loading` module**: internal helpers `_load_middleware`, `_middleware_module_allowed`, `_parse_allowed_middleware_prefixes`. Private by underscore convention; exposed at the module path so tests can import them.

### Changed

- **`GATEWAY_HOOK_MODULE` loader hardening**: the sibling hook loader gets the same two hardening fixes applied to the new middleware loader, to preserve the "mirror" contract between the two: (1) whitespace-only values (e.g. ``GATEWAY_HOOK_MODULE="   "`` from a malformed ``.env`` file) are now treated as disabled instead of falling through to a confusing "must be in ``module.path:function_name`` format" error; (2) the import try/except now catches any ``Exception`` raised during module top-level execution (``RuntimeError``, ``SyntaxError``, ``ValueError`` in config validation, etc.) rather than only ``ImportError`` — these convert to a clean ``SystemExit`` with a full traceback via ``logger.exception`` rather than propagating as a raw traceback. ``BaseException`` subclasses like ``KeyboardInterrupt`` and ``SystemExit`` still propagate unchanged.

### Notes

- **Zero behavior change for existing deployments**: ``GatewayServer`` constructions that don't set the new env vars behave identically to v0.11.0. The kwarg path introduced in v0.11.0 (``GatewayServer(middleware=[...])``) is unchanged. The ``GATEWAY_HOOK_MODULE`` hardening is strictly a fail-closed improvement — operators who were previously seeing a raw traceback on a broken factory module now see a clean ``SystemExit`` with the full stack in logs.
- **Allowlist is mandatory**: deployments that want env-driven middleware loading MUST set both ``GATEWAY_MIDDLEWARE_MODULE`` and ``GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES``. Setting only the module var emits a ``WARNING``-level startup log and refuses to load — same fail-closed posture the hooks loader uses.

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

[0.17.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/Ultrathink-Solutions/fastmcp-gateway/compare/v0.11.0...v0.12.0
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

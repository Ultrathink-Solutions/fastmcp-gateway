"""Entry point for running fastmcp-gateway as a standalone server.

Configure via environment variables:

    GATEWAY_UPSTREAMS (required)
        JSON object mapping domain names to upstream MCP server URLs.
        Values may be either a URL string (simple form) or an object with
        ``url`` plus optional ``allowed_tools`` / ``denied_tools`` lists of
        fnmatch glob patterns.  Mixed shapes are allowed.
        Example (simple):
            {"apollo": "http://apollo:8080/mcp", "hubspot": "http://hubspot:8080/mcp"}
        Example (with filters):
            {
              "apollo": {
                "url": "http://apollo:8080/mcp",
                "allowed_tools": ["apollo_search_*", "apollo_contact_*"]
              },
              "hubspot": {
                "url": "http://hubspot:8080/mcp",
                "denied_tools": ["*_delete"]
              }
            }

    GATEWAY_NAME
        Server name (default: "fastmcp-gateway").

    GATEWAY_HOST
        Bind address (default: "0.0.0.0").

    GATEWAY_PORT
        Bind port (default: 8080).

    GATEWAY_INSTRUCTIONS
        Custom LLM system instructions.

    GATEWAY_REGISTRY_AUTH_TOKEN
        Bearer token sent to upstreams during startup registry population.

    GATEWAY_DOMAIN_DESCRIPTIONS
        JSON object mapping domain names to human-readable descriptions.
        Example: {"apollo": "Sales intelligence", "hubspot": "CRM operations"}

    GATEWAY_UPSTREAM_HEADERS
        JSON object mapping domain names to header dicts for tool execution.
        Domains listed here use these headers instead of request-passthrough.
        Example: {"ahrefs": {"Authorization": "Bearer <key>"}}

    GATEWAY_REFRESH_INTERVAL
        Seconds between automatic registry refresh cycles (float).
        When set, the gateway periodically re-queries all upstreams
        to detect added/removed tools.  Disabled by default.

    GATEWAY_HOOK_MODULE
        Python dotted path to a factory function that returns a list of hook
        instances.  Format: ``module.path:function_name``.
        Example: my_package.hooks:create_hooks

        **Ignored unless GATEWAY_ALLOWED_HOOK_PREFIXES is also set** — see
        that variable.  This is a security boundary: the previous
        "set-and-import" behaviour let any process with write access to
        the gateway's env turn GATEWAY_HOOK_MODULE into a code-injection
        primitive.  Operators who need env-based hook loading must now
        explicitly pin the set of module prefixes they trust.

    GATEWAY_ALLOWED_HOOK_PREFIXES
        Comma-separated allowlist of Python module prefixes that
        GATEWAY_HOOK_MODULE may resolve to.  When unset, GATEWAY_HOOK_MODULE
        is ignored entirely and hooks must be passed programmatically to
        ``GatewayServer(..., hooks=[...])``.  A prefix matches when the
        requested module path equals it exactly or begins with
        ``<prefix>.`` — i.e. plain ``str.startswith`` with a dot-boundary
        check.
        Example: ``GATEWAY_ALLOWED_HOOK_PREFIXES=my_org.hooks,ops.hooks``

    GATEWAY_MIDDLEWARE_MODULE
        Python dotted path to a factory function that returns a list of
        ASGI middleware descriptors (typically
        ``starlette.middleware.Middleware`` instances).  Format:
        ``module.path:function_name``.  The returned list is passed
        straight to ``GatewayServer(middleware=...)``; the gateway
        wraps its HTTP app with these middleware (outermost first)
        before handing to uvicorn.  Useful for injecting host-allowlist
        filtering, request-id middleware, rate limiting, CSP headers,
        or structured-logging middleware without modifying the gateway
        entry point.
        Example: my_package.middleware:build_middleware

        **Ignored unless GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES is also
        set** — same security boundary as ``GATEWAY_HOOK_MODULE``.

    GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES
        Comma-separated allowlist of Python module prefixes that
        GATEWAY_MIDDLEWARE_MODULE may resolve to.  When unset,
        GATEWAY_MIDDLEWARE_MODULE is ignored entirely and middleware
        must be passed programmatically to ``GatewayServer(...,
        middleware=[...])``.  Dot-boundary match, same rule as
        ``GATEWAY_ALLOWED_HOOK_PREFIXES``.
        Example: ``GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES=my_org.middleware,ops.middleware``

    GATEWAY_REGISTRATION_TOKEN
        Shared secret that protects the dynamic registration REST endpoints
        (POST/DELETE/GET /registry/servers).  When set, the gateway exposes
        these endpoints and requires callers to send
        ``Authorization: Bearer <token>``.  When not set, the endpoints are
        not mounted (default — backwards-compatible).

    GATEWAY_CODE_MODE
        Reserved — setting this to ``true`` is **not** a supported way to
        enable the experimental ``execute_code`` meta-tool from the CLI.
        Code mode now requires an explicit ``code_mode_authorizer``
        callback which cannot be supplied through environment variables;
        setting ``GATEWAY_CODE_MODE=true`` without constructing
        ``GatewayServer`` programmatically causes the process to exit at
        startup with a typed ``CodeModeAuthorizerRequiredError``.
        To use code mode, construct ``GatewayServer`` programmatically
        with ``code_mode=True`` and
        ``code_mode_authorizer=<your async callback>``; leave this env
        var unset for CLI usage. Requires the optional ``code-mode``
        extra (``pip install "fastmcp-gateway[code-mode]"``).

    GATEWAY_CODE_MODE_MAX_DURATION_SECS, GATEWAY_CODE_MODE_MAX_MEMORY,
    GATEWAY_CODE_MODE_MAX_ALLOCATIONS, GATEWAY_CODE_MODE_MAX_RECURSION_DEPTH,
    GATEWAY_CODE_MODE_MAX_NESTED_CALLS
        Optional resource caps for each ``execute_code`` invocation.
        Missing values use the CodeModeLimits defaults.

    GATEWAY_CODE_MODE_AUDIT_VERBATIM
        When ``true``, raw LLM-authored code is emitted at DEBUG level
        in audit logs.  High PII risk; leave off unless explicitly
        required for incident response.

Usage::

    GATEWAY_UPSTREAMS='{"apollo": "http://localhost:8080/mcp"}' python -m fastmcp_gateway
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
from typing import Any

from fastmcp_gateway._hook_loading import _load_hooks
from fastmcp_gateway._middleware_loading import _load_middleware
from fastmcp_gateway.code_mode import CodeModeUnavailableError
from fastmcp_gateway.gateway import CodeModeAuthorizerRequiredError, GatewayServer

logger = logging.getLogger("fastmcp_gateway")


def _load_json_env(name: str, *, required: bool = False) -> dict[str, Any] | None:
    """Load and parse a JSON environment variable."""
    raw = os.environ.get(name, "")
    if not raw:
        if required:
            logger.error("Required environment variable %s is not set", name)
            sys.exit(1)
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON in %s: %s", name, exc)
        sys.exit(1)
    if not isinstance(value, dict):
        logger.error("%s must be a JSON object, got %s", name, type(value).__name__)
        sys.exit(1)
    return value


_TRUE_TOKENS = frozenset({"true", "1", "yes", "on"})
_FALSE_TOKENS = frozenset({"false", "0", "no", "off"})


def _bool_env(name: str, default: bool = False) -> bool:
    """Parse a boolean env var with strict token matching.

    Recognised: ``true`` / ``1`` / ``yes`` / ``on`` for True, and
    ``false`` / ``0`` / ``no`` / ``off`` for False (case-insensitive).
    An empty value returns *default*.  Any other value is rejected --
    typos like ``GATEWAY_CODE_MODE=treu`` should fail fast instead of
    silently disabling a security-relevant feature.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_TOKENS:
        return True
    if raw in _FALSE_TOKENS:
        return False
    logger.error(
        "Invalid %s: %r (expected one of: %s)",
        name,
        raw,
        ", ".join(sorted(_TRUE_TOKENS | _FALSE_TOKENS)),
    )
    sys.exit(1)


def _float_env(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logger.error("Invalid %s: %s (must be a number)", name, raw)
        sys.exit(1)


def _int_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.error("Invalid %s: %s (must be an integer)", name, raw)
        sys.exit(1)


def _load_code_mode_config() -> tuple[bool, Any | None, bool]:
    """Parse the code-mode env vars into (flag, CodeModeLimits | None, verbatim)."""
    enabled = _bool_env("GATEWAY_CODE_MODE", default=False)
    if not enabled:
        return False, None, False

    # Defer the import so systems with code mode disabled don't pay the cost.
    # The missing-extra case is handled later when GatewayServer constructs
    # CodeModeRunner (see main()); this import itself cannot fail because
    # CodeModeLimits is a plain dataclass with no pydantic-monty dependency.
    from fastmcp_gateway.code_mode import CodeModeLimits

    # Each override is optional; when omitted we keep the dataclass default.
    # Every limit must be strictly positive and finite -- zero or negative
    # values would either degrade the sandbox to "no limit" (dangerous) or
    # make it reject every call (useless).  NaN / inf are never valid here.
    overrides: dict[str, Any] = {}

    duration = _float_env("GATEWAY_CODE_MODE_MAX_DURATION_SECS")
    if duration is not None:
        if not math.isfinite(duration) or duration <= 0:
            logger.error(
                "Invalid GATEWAY_CODE_MODE_MAX_DURATION_SECS: %s (must be finite and > 0)",
                duration,
            )
            sys.exit(1)
        overrides["max_duration_secs"] = duration

    for var_name, limit_name in (
        ("GATEWAY_CODE_MODE_MAX_MEMORY", "max_memory"),
        ("GATEWAY_CODE_MODE_MAX_ALLOCATIONS", "max_allocations"),
        ("GATEWAY_CODE_MODE_MAX_RECURSION_DEPTH", "max_recursion_depth"),
        ("GATEWAY_CODE_MODE_MAX_NESTED_CALLS", "max_nested_calls"),
    ):
        value = _int_env(var_name)
        if value is None:
            continue
        if value <= 0:
            logger.error("Invalid %s: %d (must be > 0)", var_name, value)
            sys.exit(1)
        overrides[limit_name] = value

    limits = CodeModeLimits(**overrides) if overrides else CodeModeLimits()
    verbatim = _bool_env("GATEWAY_CODE_MODE_AUDIT_VERBATIM", default=False)
    return True, limits, verbatim


async def _populate(gateway: GatewayServer) -> None:
    """Populate the gateway registry from upstream servers."""
    results = await gateway.populate()
    total = sum(results.values())
    logger.info(
        "Registry populated: %d tools across %d domains %s",
        total,
        len(results),
        dict(results),
    )


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    upstreams = _load_json_env("GATEWAY_UPSTREAMS", required=True)
    assert upstreams is not None  # guaranteed by required=True

    # Optional configuration.
    name = os.environ.get("GATEWAY_NAME", "fastmcp-gateway")
    host = os.environ.get("GATEWAY_HOST", "0.0.0.0")
    port_raw = os.environ.get("GATEWAY_PORT", "8080")
    try:
        port = int(port_raw)
    except ValueError:
        logger.error("Invalid GATEWAY_PORT value: %s (must be an integer)", port_raw)
        sys.exit(1)
    instructions = os.environ.get("GATEWAY_INSTRUCTIONS") or None
    domain_descriptions = _load_json_env("GATEWAY_DOMAIN_DESCRIPTIONS")
    upstream_headers = _load_json_env("GATEWAY_UPSTREAM_HEADERS")

    # Registry auth: convert a bearer token to an Authorization header.
    registry_auth_headers: dict[str, str] | None = None
    registry_token = os.environ.get("GATEWAY_REGISTRY_AUTH_TOKEN", "")
    if registry_token:
        registry_auth_headers = {"Authorization": f"Bearer {registry_token}"}

    # Background refresh interval (optional).
    refresh_interval: float | None = None
    refresh_interval_raw = os.environ.get("GATEWAY_REFRESH_INTERVAL", "")
    if refresh_interval_raw:
        try:
            refresh_interval = float(refresh_interval_raw)
        except ValueError:
            logger.error("Invalid GATEWAY_REFRESH_INTERVAL: %s (must be a number)", refresh_interval_raw)
            sys.exit(1)
        if not math.isfinite(refresh_interval) or refresh_interval <= 0:
            logger.error(
                "Invalid GATEWAY_REFRESH_INTERVAL: %s (must be a positive, finite number)",
                refresh_interval_raw,
            )
            sys.exit(1)

    # Execution hooks.
    hooks = _load_hooks()

    # ASGI middleware (optional). Loaded from the
    # ``GATEWAY_MIDDLEWARE_MODULE`` env var under the same allowlist
    # guard that gates hook loading — any deployment that wants to
    # wrap the gateway's HTTP app with host-allowlist filtering,
    # rate limiting, CSP headers, etc. sets both the module path
    # and ``GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES``.
    middleware = _load_middleware()

    # Dynamic registration token (optional).
    registration_token = os.environ.get("GATEWAY_REGISTRATION_TOKEN") or None

    # Code mode (experimental, off by default).
    code_mode, code_mode_limits, code_mode_audit_verbatim = _load_code_mode_config()

    try:
        gateway = GatewayServer(
            upstreams,
            name=name,
            instructions=instructions,
            registry_auth_headers=registry_auth_headers,
            upstream_headers=upstream_headers,
            domain_descriptions=domain_descriptions,
            refresh_interval=refresh_interval,
            hooks=hooks,
            registration_token=registration_token,
            code_mode=code_mode,
            code_mode_limits=code_mode_limits,
            code_mode_audit_verbatim=code_mode_audit_verbatim,
            middleware=middleware,
        )
    except CodeModeUnavailableError as exc:
        # Friendly handling for the one construction-time error with a
        # clear operator action.  CodeModeRunner raises this lazily inside
        # GatewayServer._register_meta_tools when the [code-mode] extra
        # isn't installed.  Any other construction error propagates with
        # its full traceback so real bugs surface in logs.
        logger.error(
            "GATEWAY_CODE_MODE=true but the [code-mode] extra is not installed: %s",
            exc,
        )
        sys.exit(1)
    except CodeModeAuthorizerRequiredError:
        # code_mode=True requires an explicit code_mode_authorizer callback.
        # The old auto-discovery path was removed because it let any hook
        # silently open the gate. GATEWAY_CODE_MODE=true via env has no way
        # to supply a callback without also supplying an authorizer, so
        # CLI-driven code mode is no longer supported. Callers that need
        # code mode must construct GatewayServer programmatically.
        logger.error(
            "GATEWAY_CODE_MODE=true is no longer supported via the CLI. "
            "Code mode now requires an explicit code_mode_authorizer "
            "callback which cannot be supplied through environment "
            "variables. Construct GatewayServer programmatically with "
            "code_mode=True and code_mode_authorizer=<your callback>, "
            "or leave GATEWAY_CODE_MODE unset."
        )
        sys.exit(1)

    # Populate in its own event loop, then run the server (which creates its
    # own loop via anyio).  Calling gateway.run() from inside asyncio.run()
    # would fail with "Already running asyncio in this thread".
    asyncio.run(_populate(gateway))
    gateway.run(host=host, port=port, transport="streamable-http")


if __name__ == "__main__":
    main()

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

    GATEWAY_REGISTRATION_TOKEN
        Shared secret that protects the dynamic registration REST endpoints
        (POST/DELETE/GET /registry/servers).  When set, the gateway exposes
        these endpoints and requires callers to send
        ``Authorization: Bearer <token>``.  When not set, the endpoints are
        not mounted (default — backwards-compatible).

    GATEWAY_CODE_MODE
        Set to ``true`` to enable the experimental ``execute_code``
        meta-tool.  Off by default.  Requires the optional ``code-mode``
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
import importlib
import json
import logging
import math
import os
import sys
from typing import Any

from fastmcp_gateway.gateway import GatewayServer

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


def _bool_env(name: str, default: bool = False) -> bool:
    """Parse a boolean env var: ``"true"``/``"1"``/``"yes"`` (case-insensitive) → True."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"true", "1", "yes", "on"}


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

    # Defer the import so systems without the extra installed don't pay the cost.
    try:
        from fastmcp_gateway.code_mode import CodeModeLimits
    except RuntimeError as exc:
        logger.error("GATEWAY_CODE_MODE=true but code-mode extra is not installed: %s", exc)
        sys.exit(1)

    # Each override is optional; when omitted we keep the dataclass default.
    overrides: dict[str, Any] = {}
    duration = _float_env("GATEWAY_CODE_MODE_MAX_DURATION_SECS")
    if duration is not None:
        overrides["max_duration_secs"] = duration
    memory = _int_env("GATEWAY_CODE_MODE_MAX_MEMORY")
    if memory is not None:
        overrides["max_memory"] = memory
    allocations = _int_env("GATEWAY_CODE_MODE_MAX_ALLOCATIONS")
    if allocations is not None:
        overrides["max_allocations"] = allocations
    recursion = _int_env("GATEWAY_CODE_MODE_MAX_RECURSION_DEPTH")
    if recursion is not None:
        overrides["max_recursion_depth"] = recursion
    nested = _int_env("GATEWAY_CODE_MODE_MAX_NESTED_CALLS")
    if nested is not None:
        overrides["max_nested_calls"] = nested

    limits = CodeModeLimits(**overrides) if overrides else CodeModeLimits()
    verbatim = _bool_env("GATEWAY_CODE_MODE_AUDIT_VERBATIM", default=False)
    return True, limits, verbatim


def _load_hooks() -> list[Any] | None:
    """Load hooks from the GATEWAY_HOOK_MODULE environment variable.

    Expected format: ``module.path:function_name`` where the function
    takes no arguments and returns a list of hook instances.

    Returns ``None`` if the env var is not set.
    """
    raw = os.environ.get("GATEWAY_HOOK_MODULE", "")
    if not raw:
        return None

    if ":" not in raw:
        logger.error("GATEWAY_HOOK_MODULE must be in 'module.path:function_name' format, got: %s", raw)
        sys.exit(1)

    module_path, func_name = raw.rsplit(":", 1)
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        logger.error("Failed to import hook module '%s': %s", module_path, exc)
        sys.exit(1)

    factory = getattr(module, func_name, None)
    if factory is None:
        logger.error("Hook module '%s' has no attribute '%s'", module_path, func_name)
        sys.exit(1)

    if not callable(factory):
        logger.error("Hook factory '%s:%s' is not callable", module_path, func_name)
        sys.exit(1)

    hooks = factory()
    if not isinstance(hooks, list):
        logger.error("Hook factory '%s:%s' must return a list, got %s", module_path, func_name, type(hooks).__name__)
        sys.exit(1)

    logger.info("Loaded %d hook(s) from %s", len(hooks), raw)
    return hooks


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
        )
    except Exception as exc:
        # Friendly handling for the one construction-time error that has a
        # clear operator action: the [code-mode] extra is not installed.
        # `CodeModeRunner` raises CodeModeUnavailableError lazily inside
        # `GatewayServer._register_meta_tools`, so we can't catch it at the
        # earlier CodeModeLimits import site.  Any other construction error
        # is unexpected and re-raised so the traceback surfaces in logs.
        from fastmcp_gateway.code_mode import CodeModeUnavailableError

        if isinstance(exc, CodeModeUnavailableError):
            logger.error(
                "GATEWAY_CODE_MODE=true but the [code-mode] extra is not installed: %s",
                exc,
            )
            sys.exit(1)
        raise

    # Populate in its own event loop, then run the server (which creates its
    # own loop via anyio).  Calling gateway.run() from inside asyncio.run()
    # would fail with "Already running asyncio in this thread".
    asyncio.run(_populate(gateway))
    gateway.run(host=host, port=port, transport="streamable-http")


if __name__ == "__main__":
    main()

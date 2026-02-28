"""Entry point for running fastmcp-gateway as a standalone server.

Configure via environment variables:

    GATEWAY_UPSTREAMS (required)
        JSON object mapping domain names to upstream MCP server URLs.
        Example: {"apollo": "http://apollo:8080/mcp", "hubspot": "http://hubspot:8080/mcp"}

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
    )

    # Populate in its own event loop, then run the server (which creates its
    # own loop via anyio).  Calling gateway.run() from inside asyncio.run()
    # would fail with "Already running asyncio in this thread".
    asyncio.run(_populate(gateway))
    gateway.run(host=host, port=port, transport="streamable-http")


if __name__ == "__main__":
    main()

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

Usage::

    GATEWAY_UPSTREAMS='{"apollo": "http://localhost:8080/mcp"}' python -m fastmcp_gateway
"""

from __future__ import annotations

import asyncio
import json
import logging
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


async def _run() -> None:
    """Build, populate, and run the gateway."""
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

    gateway = GatewayServer(
        upstreams,
        name=name,
        instructions=instructions,
        registry_auth_headers=registry_auth_headers,
        upstream_headers=upstream_headers,
        domain_descriptions=domain_descriptions,
    )

    results = await gateway.populate()
    total = sum(results.values())
    logger.info(
        "Registry populated: %d tools across %d domains %s",
        total,
        len(results),
        dict(results),
    )

    gateway.run(host=host, port=port, transport="streamable-http")


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()

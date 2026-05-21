"""Internal helpers for env-driven inbound auth provider loading.

Mirrors :mod:`fastmcp_gateway._hook_loading` and
:mod:`fastmcp_gateway._middleware_loading` — same allowlist-gated import,
same operator-facing failure modes. Returns a single FastMCP
``AuthProvider`` instance (not a list) because the underlying
``FastMCP(auth=...)`` constructor accepts a single provider.

Operator use case: instantiate an ``OAuthProxy`` (or any other
``AuthProvider`` subclass) without modifying the gateway entry point —
useful when the upstream identity provider doesn't support RFC 7591
Dynamic Client Registration (DCR) and clients that require DCR
(Claude Code, Claude Desktop, VS Code) need a DCR-capable facade in
front of it.

The ``_`` prefix signals this is a package-internal helper — external
consumers should use ``GatewayServer(..., auth=...)`` directly rather
than relying on the env-driven entry point.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp.server.auth import AuthProvider

logger = logging.getLogger("fastmcp_gateway")


def _parse_allowed_auth_prefixes() -> list[str]:
    """Parse ``GATEWAY_ALLOWED_AUTH_PREFIXES`` into a normalised prefix list.

    Returns an empty list when the env var is unset or blank. Trims
    whitespace per entry and drops empties; rejects entries containing
    whitespace after trimming.
    """
    raw = os.environ.get("GATEWAY_ALLOWED_AUTH_PREFIXES", "").strip()
    if not raw:
        return []
    prefixes: list[str] = []
    for token in raw.split(","):
        prefix = token.strip()
        if not prefix:
            continue
        if any(ch.isspace() for ch in prefix):
            logger.error(
                "GATEWAY_ALLOWED_AUTH_PREFIXES contains a prefix with whitespace: %r",
                prefix,
            )
            sys.exit(1)
        prefixes.append(prefix)
    return prefixes


def _auth_module_allowed(module_path: str, allowed_prefixes: list[str]) -> bool:
    """Return True iff ``module_path`` matches one of ``allowed_prefixes``.

    A prefix matches when it equals the module path exactly, or when the
    module path starts with ``<prefix>.``. The dot boundary prevents a
    prefix like ``"my_org"`` from accidentally matching ``"my_org_evil"``.
    """
    for prefix in allowed_prefixes:
        if module_path == prefix:
            return True
        if module_path.startswith(prefix + "."):
            return True
    return False


def _load_auth() -> AuthProvider | None:
    """Load the inbound auth provider from ``GATEWAY_AUTH_MODULE``.

    Expected format: ``module.path:function_name`` where the function
    takes no arguments and returns either an
    :class:`fastmcp.server.auth.AuthProvider` instance or ``None``.
    Returning ``None`` from the factory is treated the same as not
    setting the env var — useful for factories that conditionally
    enable auth based on their own env inspection.

    **Security guard**: returns ``None`` (ignoring any
    GATEWAY_AUTH_MODULE value) unless ``GATEWAY_ALLOWED_AUTH_PREFIXES``
    is set and the requested module path matches one of its prefixes.
    Same rationale as the hook / middleware loaders: without the
    allowlist, an env-driven module path is a code-injection primitive
    for anyone with write access to the gateway pod's environment.
    """
    raw = os.environ.get("GATEWAY_AUTH_MODULE", "").strip()
    if not raw:
        return None

    allowed_prefixes = _parse_allowed_auth_prefixes()
    if not allowed_prefixes:
        logger.warning(
            "GATEWAY_AUTH_MODULE is set but GATEWAY_ALLOWED_AUTH_PREFIXES is "
            "not — refusing to import %r. Set GATEWAY_ALLOWED_AUTH_PREFIXES "
            "to a comma-separated allowlist, or pass auth programmatically "
            "via GatewayServer(auth=...).",
            raw,
        )
        return None

    if ":" not in raw:
        logger.error(
            "GATEWAY_AUTH_MODULE must be in 'module.path:function_name' format, got: %s",
            raw,
        )
        sys.exit(1)

    module_path, func_name = raw.rsplit(":", 1)

    if not _auth_module_allowed(module_path, allowed_prefixes):
        logger.error(
            "GATEWAY_AUTH_MODULE %r resolves to module %r which is not in "
            "GATEWAY_ALLOWED_AUTH_PREFIXES (%s). Refusing to import.",
            raw,
            module_path,
            ", ".join(allowed_prefixes),
        )
        sys.exit(1)

    try:
        module = importlib.import_module(module_path)
    except Exception as exc:
        # Broad ``Exception`` (not ``BaseException``) so ``SystemExit`` /
        # ``KeyboardInterrupt`` still propagate, but module-top-level
        # ``RuntimeError`` / ``SyntaxError`` / validation failures convert
        # to a clean operator-facing message and exit rather than a raw
        # traceback. Same posture as ``_load_hooks``.
        logger.exception("Failed to import auth module '%s': %s", module_path, exc)
        sys.exit(1)

    factory = getattr(module, func_name, None)
    if factory is None:
        logger.error("Auth module '%s' has no attribute '%s'", module_path, func_name)
        sys.exit(1)

    if not callable(factory):
        logger.error("Auth factory '%s:%s' is not callable", module_path, func_name)
        sys.exit(1)

    try:
        auth = factory()
    except Exception as exc:
        # Factory construction can fail for the same reason any callable
        # can fail (validation of its own env, network reachability to an
        # IdP discovery endpoint, etc.). Convert to ``SystemExit`` with
        # the same posture as the import-failure path above so the gateway
        # fails closed at startup with a clean operator-facing message
        # instead of propagating a raw traceback through uvicorn.
        logger.exception(
            "Auth factory '%s:%s' raised at construction time: %s",
            module_path,
            func_name,
            exc,
        )
        sys.exit(1)
    if auth is None:
        logger.info(
            "Auth factory %s returned None; running without inbound auth provider",
            raw,
        )
        return None

    # Validate against the FastMCP base class at runtime. Imported lazily
    # so the gateway library doesn't take an unconditional dependency on
    # fastmcp.server.auth at import time — the loader is only called when
    # GATEWAY_AUTH_MODULE is set, and at that point the operator has
    # opted into the fastmcp.server.auth contract.
    from fastmcp.server.auth import AuthProvider

    if not isinstance(auth, AuthProvider):
        logger.error(
            "Auth factory '%s:%s' must return a fastmcp.server.auth.AuthProvider instance (or None), got %s",
            module_path,
            func_name,
            type(auth).__name__,
        )
        sys.exit(1)

    logger.info("Loaded auth provider %s from %s", type(auth).__name__, raw)
    return auth

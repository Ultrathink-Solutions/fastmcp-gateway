"""Internal helper for env-driven registry token-provider loading.

Mirrors :mod:`fastmcp_gateway._auth_loading` (and the hook / middleware
loaders) — same allowlist-gated import, same operator-facing failure
modes. Returns a zero-argument callable that yields a bearer token
string, passed to ``UpstreamManager(registry_token_provider=...)`` so the
registry's ``Authorization`` header is refreshed before each fetch.

Operator use case: supply a short-lived / rotating registry credential
(e.g. a client-credentials token cache) without modifying the gateway
entry point. The static ``GATEWAY_REGISTRY_AUTH_TOKEN`` is captured once
at startup and expires mid-life; a provider is invoked before every
registry fetch, so the token is always current.

The ``_`` prefix signals this is a package-internal helper — external
consumers should use ``GatewayServer(..., registry_token_provider=...)``
directly rather than relying on the env-driven entry point.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger("fastmcp_gateway")

_MODULE_ENV = "GATEWAY_REGISTRY_TOKEN_PROVIDER_MODULE"
_PREFIXES_ENV = "GATEWAY_ALLOWED_REGISTRY_TOKEN_PROVIDER_PREFIXES"


def _parse_allowed_registry_token_provider_prefixes() -> list[str]:
    """Parse ``GATEWAY_ALLOWED_REGISTRY_TOKEN_PROVIDER_PREFIXES`` into a list.

    Returns an empty list when the env var is unset or blank. Trims
    whitespace per entry and drops empties; rejects entries containing
    whitespace after trimming.
    """
    raw = os.environ.get(_PREFIXES_ENV, "").strip()
    if not raw:
        return []
    prefixes: list[str] = []
    for token in raw.split(","):
        prefix = token.strip()
        if not prefix:
            continue
        if any(ch.isspace() for ch in prefix):
            logger.error("%s contains a prefix with whitespace: %r", _PREFIXES_ENV, prefix)
            sys.exit(1)
        prefixes.append(prefix)
    return prefixes


def _registry_token_provider_module_allowed(module_path: str, allowed_prefixes: list[str]) -> bool:
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


def _load_registry_token_provider() -> Callable[[], str | Awaitable[str]] | None:
    """Load a registry token provider from ``GATEWAY_REGISTRY_TOKEN_PROVIDER_MODULE``.

    Expected format: ``module.path:function_name`` where the function takes
    no arguments and returns a zero-argument callable yielding a bearer
    token string (the registry token provider), or ``None``. Returning
    ``None`` is treated the same as not setting the env var — the gateway
    falls back to the static ``GATEWAY_REGISTRY_AUTH_TOKEN`` (if any).

    **Security guard**: returns ``None`` (ignoring any module value) unless
    ``GATEWAY_ALLOWED_REGISTRY_TOKEN_PROVIDER_PREFIXES`` is set and the
    requested module path matches one of its prefixes. Same rationale as
    the auth / hook / middleware loaders: without the allowlist, an
    env-driven module path is a code-injection primitive for anyone with
    write access to the gateway pod's environment.
    """
    raw = os.environ.get(_MODULE_ENV, "").strip()
    if not raw:
        return None

    allowed_prefixes = _parse_allowed_registry_token_provider_prefixes()
    if not allowed_prefixes:
        logger.warning(
            "%s is set but %s is not — refusing to import %r. Set the allowlist, "
            "or pass the provider programmatically via "
            "GatewayServer(registry_token_provider=...).",
            _MODULE_ENV,
            _PREFIXES_ENV,
            raw,
        )
        return None

    if ":" not in raw:
        logger.error(
            "%s must be in 'module.path:function_name' format, got: %s",
            _MODULE_ENV,
            raw,
        )
        sys.exit(1)

    module_path, func_name = raw.rsplit(":", 1)

    if not _registry_token_provider_module_allowed(module_path, allowed_prefixes):
        logger.error(
            "%s %r resolves to module %r which is not in %s (%s). Refusing to import.",
            _MODULE_ENV,
            raw,
            module_path,
            _PREFIXES_ENV,
            ", ".join(allowed_prefixes),
        )
        sys.exit(1)

    try:
        module = importlib.import_module(module_path)
    except Exception as exc:
        # Broad ``Exception`` (not ``BaseException``) so ``SystemExit`` /
        # ``KeyboardInterrupt`` still propagate, but a module-top-level
        # ``RuntimeError`` / ``SyntaxError`` / validation failure converts
        # to a clean operator-facing message and exit. Same posture as the
        # auth / hook loaders.
        logger.exception(
            "Failed to import registry token provider module '%s': %s",
            module_path,
            exc,
        )
        sys.exit(1)

    factory = getattr(module, func_name, None)
    if factory is None:
        logger.error(
            "Registry token provider module '%s' has no attribute '%s'",
            module_path,
            func_name,
        )
        sys.exit(1)
    if not callable(factory):
        logger.error(
            "Registry token provider factory '%s:%s' is not callable",
            module_path,
            func_name,
        )
        sys.exit(1)

    try:
        provider = factory()
    except Exception as exc:
        # Factory construction can fail for the same reasons any callable can
        # (validating its own env, reaching a token endpoint, etc.). Convert to
        # ``SystemExit`` so the gateway fails closed at startup with a clean
        # operator-facing message instead of a raw traceback through uvicorn.
        logger.exception(
            "Registry token provider factory '%s:%s' raised at construction time: %s",
            module_path,
            func_name,
            exc,
        )
        sys.exit(1)

    if provider is None:
        logger.info(
            "Registry token provider factory %s returned None; falling back to GATEWAY_REGISTRY_AUTH_TOKEN (if set)",
            raw,
        )
        return None

    if not callable(provider):
        logger.error(
            "Registry token provider factory '%s:%s' must return a zero-argument "
            "callable returning a token string (or None), got %s",
            module_path,
            func_name,
            type(provider).__name__,
        )
        sys.exit(1)

    logger.info("Loaded registry token provider from %s", raw)
    # `callable()` can't verify the zero-arg / str-return signature at runtime;
    # the operator owns that contract. Cast so the declared return type holds.
    return cast("Callable[[], str | Awaitable[str]]", provider)

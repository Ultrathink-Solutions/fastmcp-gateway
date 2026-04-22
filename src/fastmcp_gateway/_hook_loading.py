"""Internal helpers for env-driven hook loading.

Lives in its own module (rather than ``__main__.py``) so tests can import
the helpers with normal package semantics. ``__main__.py`` re-exports
what callers need.

The ``_`` prefix signals this is a package-internal helper — external
consumers should use ``GatewayServer(..., hooks=[...])`` directly rather
than relying on these env-driven entry points.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from typing import Any

logger = logging.getLogger("fastmcp_gateway")


def _parse_allowed_hook_prefixes() -> list[str]:
    """Parse ``GATEWAY_ALLOWED_HOOK_PREFIXES`` into a normalised prefix list.

    Returns an empty list when the env var is unset or blank. Trims
    whitespace per entry and drops empties; rejects entries containing
    whitespace after trimming (which would indicate a malformed list).
    """
    raw = os.environ.get("GATEWAY_ALLOWED_HOOK_PREFIXES", "").strip()
    if not raw:
        return []
    prefixes: list[str] = []
    for token in raw.split(","):
        prefix = token.strip()
        if not prefix:
            continue
        if any(ch.isspace() for ch in prefix):
            logger.error(
                "GATEWAY_ALLOWED_HOOK_PREFIXES contains a prefix with whitespace: %r",
                prefix,
            )
            sys.exit(1)
        prefixes.append(prefix)
    return prefixes


def _hook_module_allowed(module_path: str, allowed_prefixes: list[str]) -> bool:
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


def _load_hooks() -> list[Any] | None:
    """Load hooks from the ``GATEWAY_HOOK_MODULE`` environment variable.

    Expected format: ``module.path:function_name`` where the function
    takes no arguments and returns a list of hook instances.

    **Security guard**: returns ``None`` (ignoring any GATEWAY_HOOK_MODULE
    value) unless ``GATEWAY_ALLOWED_HOOK_PREFIXES`` is set and the
    requested module path matches one of its prefixes. Without the
    allowlist, GATEWAY_HOOK_MODULE is a code-injection primitive for
    anyone who can write env vars on the gateway pod — we refuse to do
    the import at all.
    """
    # Strip so whitespace-only values (e.g. from a malformed .env file
    # or YAML escaping) are treated as disabled rather than falling
    # through to the format check and emitting a confusing "must be in
    # 'module.path:function_name' format" error. Matches the strip
    # posture already used by ``_parse_allowed_hook_prefixes``.
    raw = os.environ.get("GATEWAY_HOOK_MODULE", "").strip()
    if not raw:
        return None

    allowed_prefixes = _parse_allowed_hook_prefixes()
    if not allowed_prefixes:
        logger.warning(
            "GATEWAY_HOOK_MODULE is set but GATEWAY_ALLOWED_HOOK_PREFIXES is "
            "not — refusing to import %r. Set GATEWAY_ALLOWED_HOOK_PREFIXES "
            "to a comma-separated allowlist, or pass hooks programmatically.",
            raw,
        )
        return None

    if ":" not in raw:
        logger.error(
            "GATEWAY_HOOK_MODULE must be in 'module.path:function_name' format, got: %s",
            raw,
        )
        sys.exit(1)

    module_path, func_name = raw.rsplit(":", 1)

    if not _hook_module_allowed(module_path, allowed_prefixes):
        logger.error(
            "GATEWAY_HOOK_MODULE %r resolves to module %r which is not in "
            "GATEWAY_ALLOWED_HOOK_PREFIXES (%s). Refusing to import.",
            raw,
            module_path,
            ", ".join(allowed_prefixes),
        )
        sys.exit(1)

    try:
        module = importlib.import_module(module_path)
    except Exception as exc:
        # Broad ``Exception`` (not ``BaseException``) so
        # ``SystemExit`` / ``KeyboardInterrupt`` still propagate,
        # but module-top-level ``RuntimeError`` / ``SyntaxError`` /
        # validation failures convert to a clean operator-facing
        # message and exit rather than a raw traceback that's harder
        # to correlate under structured logging. ``logger.exception``
        # preserves the stack trace for diagnostics.
        logger.exception("Failed to import hook module '%s': %s", module_path, exc)
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
        logger.error(
            "Hook factory '%s:%s' must return a list, got %s",
            module_path,
            func_name,
            type(hooks).__name__,
        )
        sys.exit(1)

    logger.info("Loaded %d hook(s) from %s", len(hooks), raw)
    return hooks


__all__ = [
    "_hook_module_allowed",
    "_load_hooks",
    "_parse_allowed_hook_prefixes",
]

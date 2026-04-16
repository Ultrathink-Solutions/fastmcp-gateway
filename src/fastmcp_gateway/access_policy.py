"""Per-upstream allow/deny tool filtering with glob matching.

Configure which downstream tools are exposed through the gateway without
writing a custom hook or running an external policy engine.  Rules are
keyed by domain and evaluated against both the registered tool name and
its ``original_name`` so collision-prefix renames can't bypass policy.

Typical usage
-------------

Build a policy and pass it to :class:`GatewayServer`::

    from fastmcp_gateway import AccessPolicy, GatewayServer

    policy = AccessPolicy(
        allow={
            "crm": ["crm_search_*", "crm_contact_upsert"],
            "analytics": ["*"],
        },
        deny={"crm": ["*_delete"]},
    )
    gateway = GatewayServer(
        {"crm": "http://crm:8080/mcp", "analytics": "http://analytics:8080/mcp"},
        access_policy=policy,
    )

Or pass per-upstream filters inline::

    gateway = GatewayServer({
        "crm": {
            "url": "http://crm:8080/mcp",
            "allowed_tools": ["crm_search_*", "crm_contact_upsert"],
            "denied_tools": ["*_delete"],
        },
    })

Semantics
---------

- If ``allow`` is non-empty, a tool must be declared for its domain and
  match at least one pattern.  Domains absent from a non-empty ``allow``
  map are fully denied.
- ``deny`` is always applied after ``allow``.  A tool that matches a
  ``deny`` pattern is blocked even if it also matches an ``allow``
  pattern.
- Patterns use :func:`fnmatch.fnmatchcase` (case-sensitive ``*`` / ``?``
  globs).  Matched against both the registered name and the tool's
  ``original_name`` to defeat collision-rename bypass.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "AccessPolicy",
    "build_policy_from_upstreams",
    "normalize_upstreams",
]


@dataclass(frozen=True)
class AccessPolicy:
    """Allow/deny glob rules for gateway tool exposure.

    Parameters
    ----------
    allow:
        Mapping of domain -> list of glob patterns.  When non-empty, only
        domains listed here are exposed, and only their tools matching at
        least one pattern.  Leave empty to allow every domain by default.
    deny:
        Mapping of domain -> list of glob patterns.  Any tool matching a
        deny pattern is blocked, even if it also matches an allow pattern.
    """

    allow: dict[str, list[str]] = field(default_factory=dict)
    deny: dict[str, list[str]] = field(default_factory=dict)

    def is_allowed(self, domain: str, tool_name: str, original_name: str | None = None) -> bool:
        """Return ``True`` iff *tool_name* should be exposed from *domain*.

        Both *tool_name* and *original_name* are evaluated against the
        configured patterns to defeat collision-rename bypass.
        """
        if self.allow:
            patterns = self.allow.get(domain)
            if patterns is None:
                return False
            if not _matches_any(tool_name, original_name, patterns):
                return False

        deny_patterns = self.deny.get(domain)
        return not (deny_patterns and _matches_any(tool_name, original_name, deny_patterns))


def _matches_any(name: str, original_name: str | None, patterns: list[str]) -> bool:
    """Return ``True`` iff any of *patterns* matches *name* or *original_name*."""
    for pattern in patterns:
        if fnmatch.fnmatchcase(name, pattern):
            return True
        if original_name is not None and fnmatch.fnmatchcase(original_name, pattern):
            return True
    return False


def normalize_upstreams(
    upstreams: dict[str, Any],
) -> tuple[dict[str, Any], AccessPolicy | None]:
    """Split an upstreams mapping into (transport specs, policy).

    Accepts all existing transport forms unchanged::

        {"crm": "http://crm:8080/mcp"}                                 # URL string
        {"crm": <FastMCP instance>}                                    # in-process
        {"crm": {"mcpServers": ...}}                                   # MCP spec dict

    In addition, a filter object is recognised when the value is a dict
    containing a ``url`` key::

        {"crm": {"url": "http://crm:8080/mcp"}}
        {"crm": {"url": "http://crm:8080/mcp", "allowed_tools": [...]}}
        {"crm": {"url": "http://crm:8080/mcp", "denied_tools": [...]}}

    Mixed shapes are allowed.  When any filter dict specifies
    ``allowed_tools`` or ``denied_tools``, an :class:`AccessPolicy` is
    built and returned; otherwise returns ``None``.

    Returns a tuple ``(normalized, policy)`` where ``normalized`` has the
    same keys as *upstreams* with every filter dict replaced by its raw
    URL string.  Non-filter values (URLs, FastMCP instances, spec dicts)
    pass through untouched, preserving back-compat with every form
    :class:`fastmcp.Client` already accepts.

    Raises ``ValueError`` when a filter dict has malformed ``url`` or
    ``allowed_tools`` / ``denied_tools`` entries.
    """
    normalized: dict[str, Any] = {}
    allow: dict[str, list[str]] = {}
    deny: dict[str, list[str]] = {}

    for domain, value in upstreams.items():
        if _is_filter_dict(value):
            url = value.get("url")
            if not isinstance(url, str) or not url:
                raise ValueError(f"Upstream '{domain}' object must contain a non-empty 'url' key")
            normalized[domain] = url

            allowed = value.get("allowed_tools")
            if allowed is not None:
                if not _is_list_of_strings(allowed):
                    raise ValueError(f"Upstream '{domain}' 'allowed_tools' must be a list of strings")
                allow[domain] = list(allowed)

            denied = value.get("denied_tools")
            if denied is not None:
                if not _is_list_of_strings(denied):
                    raise ValueError(f"Upstream '{domain}' 'denied_tools' must be a list of strings")
                deny[domain] = list(denied)
        else:
            # Passes through: URL strings, FastMCP instances, MCP spec dicts,
            # and anything else :class:`fastmcp.Client` knows how to connect to.
            normalized[domain] = value

    policy = AccessPolicy(allow=allow, deny=deny) if (allow or deny) else None
    return normalized, policy


_FILTER_KEYS = frozenset({"url", "allowed_tools", "denied_tools"})


def _is_filter_dict(value: Any) -> bool:
    """Return ``True`` when *value* looks like a gateway filter config.

    A filter config is any ``dict`` containing at least one of
    :data:`_FILTER_KEYS`.  This distinguishes filter configs from MCP
    spec dicts (which typically have ``mcpServers`` at the top level and
    never any of these keys).
    """
    return isinstance(value, dict) and not _FILTER_KEYS.isdisjoint(value)


def build_policy_from_upstreams(upstreams: dict[str, Any]) -> AccessPolicy | None:
    """Build an :class:`AccessPolicy` from object-shaped upstream entries.

    Convenience wrapper around :func:`normalize_upstreams` that discards
    the URL mapping.  Returns ``None`` when no filters are configured.
    """
    _, policy = normalize_upstreams(upstreams)
    return policy


def _is_list_of_strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)

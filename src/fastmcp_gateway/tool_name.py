"""Tool-name validator: reject upstream names that would shadow
language primitives inside the ``execute_code`` sandbox.

An upstream MCP server advertises tool names via ``tools/list``. Those
names become keys in the registry and — when the ``code_mode``
meta-tool is enabled — identifiers bound inside the sandbox namespace
used by ``execute_code``. A malicious or misconfigured upstream that
registers a tool named ``eval``, ``exec``, ``__import__``, ``class``,
or any dunder would shadow the corresponding built-in binding in that
namespace, enabling sandbox escape or tool-impersonation attacks.

This module provides :func:`validate_tool_name`: a pure function that
returns ``None`` for a safe name or a short diagnostic string for an
unsafe one. The registry rejects invalid names at registration time
with a structured log; no exception propagates out of the registry
surface, so a single bad tool in a populate batch doesn't abort the
other tools from the same upstream.
"""

from __future__ import annotations

import builtins
import keyword
import re

# The shape allowed for tool names. Lowercase-first is intentional:
# it eliminates dunder names (``__foo__`` starts with ``_``, not
# ``[a-z]``) and all-uppercase forms (``CLASS``) in a single check.
# Mixed case is allowed in subsequent characters because upstream
# MCP servers advertise camelCase tool names (e.g. ``executeQuery``,
# ``listItems``); a strict snake-case rule would reject valid
# production tools while delivering no additional security benefit
# (both forms are valid Python identifiers that can't shadow
# builtins the denylist catches). 64 characters is generous without
# making the name unwieldy as a Python identifier.
_TOOL_NAME_RE = re.compile(r"^[a-z][a-zA-Z0-9_]{0,63}$")

# Names that pass the regex but would shadow Python keywords,
# soft-keywords, or builtins when the tool becomes a binding in the
# ``execute_code`` sandbox namespace. Compiled from the stdlib at
# import time so this set tracks Python version changes (e.g. soft
# keywords ``match``, ``case``, ``type`` added in 3.10; ``_``
# restored as a soft keyword in pattern matching).
#
# We intersect with the regex so the denylist only contains names
# that would otherwise slip through; names already rejected by the
# regex (dunders, uppercase identifiers) are excluded for clarity.
_DANGEROUS_IDENTIFIERS: frozenset[str] = frozenset(
    name
    for name in ({*dir(builtins)} | set(keyword.kwlist) | set(getattr(keyword, "softkwlist", ())))
    if _TOOL_NAME_RE.fullmatch(name)
)


def validate_tool_name(name: str) -> str | None:
    """Return None when ``name`` is a safe tool identifier, else a reason.

    A safe tool name satisfies every check:

    * Non-empty.
    * Matches ``[a-z][a-zA-Z0-9_]{0,63}`` — starts with a lowercase
      letter, contains only alphanumerics and underscores, length 1
      to 64. Mixed case is accepted so camelCase names advertised
      by upstream MCP servers (e.g. ``executeQuery``) validate.
    * Is not a Python keyword, soft-keyword, or builtin (which would
      shadow the binding inside the ``execute_code`` sandbox
      namespace).

    The regex already rejects dunders (``__foo__``) because they
    start with ``_``; the denylist catches the regex-legal shapes
    (``eval``, ``exec``, ``class``, ``type``).

    Args:
        name: The tool name to validate.

    Returns:
        ``None`` if safe. Otherwise a short, operator-facing reason
        string suitable for audit logging.
    """
    if not name:
        return "empty tool name"
    if not _TOOL_NAME_RE.fullmatch(name):
        return (
            "name does not match required shape "
            "^[a-z][a-zA-Z0-9_]{0,63}$ (lowercase first char, "
            "alphanumeric + underscore only, length 1 to 64)"
        )
    if name in _DANGEROUS_IDENTIFIERS:
        return f"name {name!r} shadows a Python keyword or builtin"
    return None


__all__: list[str] = []
# ``validate_tool_name`` is the only public symbol in this module
# but is intentionally not re-exported from the package root or
# pinned as stable public API — it's an internal helper consumed by
# ``ToolRegistry.register_tool``. Kept importable via the module
# path for tests, with no public-API commitment.

"""Structured error responses for the gateway."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Iterator


class OutputGuardError(ValueError):
    """Raised by the output guard in ``reject`` mode when prompt-injection
    markup is detected in a tool's result text.

    Subclasses :class:`ValueError` so existing callers catching the broad
    type keep working, while a dedicated type lets the hook pipeline
    surface the event as a structured ``ExecutionDenied`` without
    brittle string matching on error messages.
    """


class GatewayError(BaseModel):
    """Machine-parseable error returned by gateway meta-tools.

    Attributes
    ----------
    error:
        Human-readable error message.
    code:
        Machine-readable error code (e.g. ``"tool_not_found"``).
    details:
        Optional structured context (suggestions, domain names, etc.).
    """

    error: str
    code: str
    details: dict[str, Any] | None = None


def error_payload(code: str, message: str, **details: Any) -> dict[str, Any]:
    """Build a ``GatewayError`` as a plain dict.

    The dict form exists so a meta-tool can put the error on MCP's
    ``structuredContent`` channel without a ``json.dumps`` /
    ``json.loads`` round trip through :func:`error_response`. Both
    functions share this one construction, so the two channels can never
    disagree about an error's shape.

    Parameters
    ----------
    code:
        Machine-readable code such as ``"tool_not_found"``.
    message:
        Human-readable description of the error.
    **details:
        Arbitrary key-value pairs included in the ``details`` dict.
    """
    return GatewayError(
        error=message,
        code=code,
        details=details or None,
    ).model_dump()


def error_response(code: str, message: str, **details: Any) -> str:
    """Build a JSON-serialised ``GatewayError``.

    Parameters
    ----------
    code:
        Machine-readable code such as ``"tool_not_found"``.
    message:
        Human-readable description of the error.
    **details:
        Arbitrary key-value pairs included in the ``details`` dict.
    """
    return json.dumps(error_payload(code, message, **details))


# One element of a ``WWW-Authenticate`` challenge list (RFC 9110 section 11.6.1):
# an auth-param, ``name=token`` or ``name="quoted string"``, or else a bare
# token, which is the auth-scheme opening the next challenge.
_CHALLENGE_ITEM = re.compile(r'([^\s,=]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]*))|([^\s,]+)')
_QUOTED_PAIR = re.compile(r"\\(.)")


def _walk_wrapped_exceptions(exc: BaseException) -> Iterator[BaseException]:
    """Yield *exc*, then every exception it wraps, depth-first.

    An upstream failure can reach the gateway bare or wrapped -- chained as
    an explicit ``__cause__`` (``raise ... from``) or collected in an
    exception group -- so callers classify it by walking the wrapping:

    * *exc* itself comes first, before anything it wraps.
    * An exception group's members come in order, each followed by its own
      chain, before the group's ``__cause__``.

    Implicit ``__context__`` is deliberately not followed: an exception
    raised *while handling* an earlier failure (a connection failure on a
    retry, say) is its own failure, not that one. Each exception is yielded
    once, so a cycle in the chain terminates.
    """
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(reversed(current.exceptions))


def _find_upstream_response(exc: BaseException) -> Any | None:
    """Return the HTTP response carried by *exc* or by an exception it wraps.

    ``httpx.HTTPStatusError`` carries the upstream's ``response``. The
    search visits exceptions in :func:`_walk_wrapped_exceptions` order and
    returns the first response with an integer ``status_code``, so the
    outermost response wins: *exc*'s own response is returned before
    anything it wraps is examined.

    Handing back the response itself, not just its status, lets a caller
    read the status and the headers from the same exception.
    """
    for current in _walk_wrapped_exceptions(exc):
        response = getattr(current, "response", None)
        if isinstance(getattr(response, "status_code", None), int):
            return response
    return None


def parse_www_authenticate(header: str | None) -> tuple[str | None, str | None]:
    """Read ``error`` and ``scope`` from a ``Bearer`` challenge (RFC 6750 section 3).

    Returns ``(error, scope)``. Either is ``None`` when the challenge omits
    it, and both are ``None`` when *header* is absent or empty or carries no
    ``Bearer`` challenge. Scheme and parameter names match
    case-insensitively, and parameters may appear in any order, as bare
    tokens or as quoted strings (whose backslash escapes are removed).

    A header the upstream repeats reaches the caller joined into one
    comma-separated value, so only the first ``Bearer`` challenge is read:
    its parameters end at the next challenge's scheme, and a second
    ``Bearer`` challenge is ignored. When the ``insufficient_scope``
    challenge is not the first ``Bearer`` one, ``execute_tool`` therefore
    fails safe to ``upstream_unauthorized`` rather than guessing which
    challenge applies.
    """
    if not header:
        return None, None
    params: dict[str, str] | None = None
    for match in _CHALLENGE_ITEM.finditer(header):
        name, quoted, token, scheme = match.groups()
        if scheme is not None:
            if params is not None:
                break  # the Bearer challenge's parameters have ended
            if scheme.lower() == "bearer":
                params = {}
        elif params is not None:
            value = _QUOTED_PAIR.sub(r"\1", quoted) if quoted is not None else token
            params.setdefault(name.lower(), value)
    if params is None:
        return None, None
    return params.get("error"), params.get("scope")

"""Structured error responses for the gateway."""

from __future__ import annotations

import json
import re
import traceback
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote

from mcp import MCPError
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Iterator


class RegistrationSelectionError(ValueError):
    """Local registration resolution or admission failure."""

    def __init__(
        self, code: Literal["invalid_selector", "tool_not_found", "registration_changed"], message: str
    ) -> None:
        super().__init__(message)
        self.code = code


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


class UpstreamJsonRpcError(MCPError):
    """The JSON-RPC error response an upstream server sent for a call.

    The MCP client raises ``MCPError`` both for an error response the server
    sent and for failures it builds itself when no answer arrived (the session
    dropped with the request pending, a read deadline passed, a streamed
    response ended before its answer), and code and message do not tell them
    apart: a server may answer with ``-32000`` or with ``"Connection closed"``.
    ``UpstreamManager.execute_tool`` therefore marks the errors the upstream
    actually sent in reply to the call by raising this subclass (built with
    :meth:`~mcp.MCPError.from_error_data` and chained to the original), so a
    caller can trust its message as the upstream's own words. An ``MCPError``
    that is not one is not an answer.
    """


#: JSON-RPC 2.0 ``Invalid params``: the code an upstream answers when the
#: call's arguments fail the tool's input schema.
INVALID_PARAMS = -32602

#: Upper bound on an upstream error message copied into an envelope. Long
#: enough for any validation report; short enough that an upstream cannot
#: flood the caller's context through an error.
MAX_UPSTREAM_MESSAGE_CHARS = 2_000


def _find_jsonrpc_error(exc: BaseException) -> tuple[int, str] | None:
    """Return ``(code, message)`` of the upstream's JSON-RPC answer *exc* is or wraps, else ``None``.

    Only an :class:`UpstreamJsonRpcError` counts; any other ``MCPError`` is a
    failure the MCP client raised itself. The search visits exceptions in
    :func:`_walk_wrapped_exceptions` order, so the outermost answer wins, as in
    :func:`_find_upstream_response`.
    """
    for current in _walk_wrapped_exceptions(exc):
        if isinstance(current, UpstreamJsonRpcError):
            return current.error.code, current.error.message
    return None


def bounded_upstream_message(message: str) -> str:
    """*message* stripped and cut to :data:`MAX_UPSTREAM_MESSAGE_CHARS`, ending in ``…`` when cut."""
    text = message.strip()
    if len(text) <= MAX_UPSTREAM_MESSAGE_CHARS:
        return text
    return text[: MAX_UPSTREAM_MESSAGE_CHARS - 1] + "…"


def _find_upstream_response(exc: BaseException) -> Any | None:
    """Return the HTTP response carried by *exc* or by an exception it wraps.

    ``httpx2.HTTPStatusError`` carries the upstream's ``response``. The
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


def _scrub_url_for_diagnostics(url: str) -> str:
    """Return the host-level identity of *url*: ``scheme://host[:port]``.

    Registration error responses and log lines echo the failing URL
    back to the caller so operators can disambiguate which upstream
    triggered the error. Every other component can carry a secret
    (basic-auth in userinfo, a token in a path segment, a signed-URL
    token in the query string), so userinfo, path, query and fragment
    are all dropped before the URL reaches a log aggregator or a JSON
    response body. Scheme, host and port identify the upstream, and
    are never enough to replay against it.

    The authority ends at the first character no host or port can hold
    (see :func:`_cut_at_host_and_port`), before *url* is parsed: a URL
    found in free text can run on into the quote, parenthesis or comma
    of the text around it, which would otherwise cost the port or end up
    in the host.

    Malformed URLs that fail to parse fall back to the literal
    ``"<unparseable-url>"`` placeholder so we never leak the raw
    string by accident.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(_cut_at_host_and_port(url))
    except ValueError:
        return "<unparseable-url>"

    if not parts.scheme or not parts.hostname:
        return "<unparseable-url>"

    host = parts.hostname
    # IPv6 hosts come back from ``urlsplit`` without their brackets
    # (``[::1]:80`` → host ``"::1"``). The bracketing must be
    # restored before composing netloc, or downstream parsers would
    # misread the embedded colons as port separators. Detect IPv6 by
    # the presence of a colon in the host string.
    bracketed_host = f"[{host}]" if ":" in host else host

    # ``parts.port`` accesses an underlying property that raises
    # ``ValueError`` when the URL carries a non-numeric or
    # out-of-range port string (e.g. ``http://h:abc/``). Treat that as
    # "no port" rather than letting the exception propagate — the
    # scrubber's contract is to never raise on caller-supplied input.
    try:
        port = parts.port
    except ValueError:
        port = None

    netloc = f"{bracketed_host}:{port}" if port is not None else bracketed_host
    return f"{parts.scheme}://{netloc}"


#: How much of a failure's message is examined at all. An upstream's error
#: text is unbounded and is summarized on the event loop, so it is cut to
#: this length before any scanning or pattern matching.
_MAX_SCANNED_MESSAGE_CHARS = 4096

#: Upper bound on a one-line failure summary.
_MAX_FAILURE_SUMMARY_CHARS = 300

_ASCII_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
#: Characters a URL scheme may hold after its first letter (RFC 3986 section 3.1).
_URL_SCHEME_CHARS = _ASCII_LETTERS | frozenset("0123456789+.-")
#: Characters RFC 3986 never allows unescaped in a URL. One ends a URL in free
#: text, as whitespace does.
_URL_END_CHARS = frozenset('"<>\\^`{|}')
#: A JSON escape inside a URL that stands for a character the URL holds
#: (``\/``, or ``\u0026`` as Go writes ``&``): it does not end the URL.
_URL_JSON_ESCAPE = re.compile(r"\\(?:/|u[0-9A-Fa-f]{4})")


#: Characters a host or port can hold: those of a registered name (letters,
#: digits, ``-``, ``.``, ``_``, ``~``), ``%`` (an escape or an IPv6 zone),
#: and the brackets and colons of an IP literal and a port.
_HOST_PORT_CHARS = _ASCII_LETTERS | frozenset("0123456789-._~%:[]")


def _cut_at_host_and_port(url: str) -> str:
    """Return *url* up to the end of its authority's host and port.

    The authority runs from ``://`` to the first ``/``, ``?`` or ``#``;
    the host starts after its last ``@`` (userinfo comes before it). The
    host and port end at the first character neither can hold, such as a
    quote, a parenthesis or a comma, so ``https://upstream:8080'`` becomes
    ``https://upstream:8080`` and ``https://h.example)`` becomes
    ``https://h.example``. A period or colon right after a numeric port is
    sentence punctuation and is dropped too (``https://h:8443.`` becomes
    ``https://h:8443``); a hostname's own trailing dot is kept. Everything
    after that is dropped: the caller
    keeps only the scheme, host and port. A string without ``://`` is
    returned unchanged.
    """
    separator = url.find("://")
    if separator < 0:
        return url
    authority_start = separator + 3
    authority_end = authority_start
    while authority_end < len(url) and url[authority_end] not in "/?#":
        authority_end += 1
    at = url.rfind("@", authority_start, authority_end)
    host_start = authority_start if at < 0 else at + 1
    host_end = host_start
    while host_end < authority_end and url[host_end] in _HOST_PORT_CHARS:
        host_end += 1
    # A period or colon after a numeric port ends the sentence, not the port
    # (``…:8443.``). After a hostname a trailing dot is part of the name (an
    # absolute name), so only a run that follows a port is dropped.
    trimmed = host_end
    while trimmed > host_start and url[trimmed - 1] in ".:":
        trimmed -= 1
    port_separator = url.rfind(":", host_start, trimmed)
    if trimmed < host_end and port_separator >= 0 and url[port_separator + 1 : trimmed].isdigit():
        host_end = trimmed
    return url[:host_end]


def _url_spans(text: str) -> Iterator[tuple[int, int]]:
    """Yield the ``(start, end)`` span of each URL in *text*, left to right.

    A URL is a scheme (a letter, then letters, digits, ``+``, ``.`` or
    ``-``), ``://``, and at least one more character, up to whitespace or a
    character RFC 3986 never allows unescaped in a URL (``"`` ``<`` ``>``
    ``\\`` ``^`` `````` ``{`` ``|`` ``}``), except that a JSON escape standing
    for a character the URL holds (``\\/``, ``\\uXXXX``) does not end it, so a
    Go-encoded ``&`` in a password keeps the host after it. An apostrophe, a parenthesis or
    a bracket is legal in a path (and brackets frame an IPv6 host), so none
    of them ends a URL: stopping early would leave the rest of a
    token-bearing path in the text. Ending too late only drops text, since
    each span is cut to its host.

    The scan is linear in ``len(text)``: ``str.find`` locates each ``://``,
    a walk left over scheme characters never passes the end of the previous
    URL or ``://``, and the walk right runs only for a URL it yields, which
    the next search then starts after.
    """
    floor = 0
    while (separator := text.find("://", floor)) >= 0:
        start = separator
        while start > floor and text[start - 1] in _URL_SCHEME_CHARS:
            start -= 1
        # A scheme starts with a letter: skip the digits and symbols before it.
        while start < separator and text[start] not in _ASCII_LETTERS:
            start += 1
        end = separator + 3
        if start < separator:
            while end < len(text) and not text[end].isspace():
                if (escape := _URL_JSON_ESCAPE.match(text, end)) is not None:
                    end = escape.end()
                    continue
                if text[end] in _URL_END_CHARS:
                    break
                end += 1
        if end == separator + 3:
            # No scheme, or nothing after ``://``: not a URL.
            floor = separator + 1
            continue
        yield start, end
        floor = end


def _scrub_urls_in_text(text: str) -> str:
    """Return *text* with each URL in it cut to ``scheme://host[:port]`` (see :func:`_url_spans`)."""
    pieces: list[str] = []
    last = 0
    for start, end in _url_spans(text):
        pieces.append(text[last:start])
        pieces.append(_scrub_url_for_diagnostics(text[start:end]))
        last = end
    pieces.append(text[last:])
    return "".join(pieces)


_REDACTED = "[REDACTED]"

# Credentials an upstream's error text can carry. Each pattern starts at a
# fixed word, and its repeated parts are separated by character classes that
# do not overlap, so it never backtracks: matching is linear in the text.
_CREDENTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # An ``Authorization``, ``Cookie`` or ``Set-Cookie`` header's value, to the
    # end of the line: a Digest or custom credential has no shape to
    # recognize, and a cookie header carries every cookie of the session.
    (
        re.compile(r"(?i)(?<![a-z0-9])((?:authorization|(?:set-)?cookie)[\"']?[ \t]*[:=][ \t]*)[^\r\n]*"),
        rf"\1{_REDACTED}",
    ),
    # An HTTP library's report of a header value it refused (h11 "Illegal
    # header value b'...'", http.client "Invalid header value", requests
    # "... in header value: '...'"): the value is the configured secret.
    (
        re.compile(r"(?i)(?<![a-z0-9])(header[ \t]+value\b[ \t]*:?[ \t]*)[^\r\n]*"),
        rf"\1{_REDACTED}",
    ),
    # Credentials with a vendor prefix, anywhere, with or without a key.
    (
        re.compile(
            r"(?<![A-Za-z0-9_-])(?:(?:gh[pousr]_|github_pat_|glpat-|xox[abposr]-|xapp-|sk-|sk_live_|rk_live_|hf_|npm_"
            r"|ya29\.)[A-Za-z0-9_\-.]{12,}|(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])|AIza[0-9A-Za-z_\-]{30,})"
        ),
        _REDACTED,
    ),
    # A ``Bearer`` or ``Basic`` credential anywhere.
    (
        re.compile(r"(?i)(?<![a-z0-9])((?:bearer|basic)[ \t]+)[^\s\"',;]+"),
        rf"\1{_REDACTED}",
    ),
    # A JWT: base64url segments joined by dots, the first encoding ``{"``.
    (
        re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]*)*"),
        _REDACTED,
    ),
    # ``key=value`` or ``key: value`` (quoted or not) whose key names a secret,
    # as a whole word or as the last word of a snake_case or camelCase key.
    (
        re.compile(
            r"(?i)(?:(?<![a-z0-9])|(?-i:(?<=[a-z0-9])(?=[A-Z])))"
            r"((?:token|secret|password|passwd|pwd|passphrase|api[_ -]?key|access[_ -]?key|secret[_ -]?key"
            r"|private[_ -]?key|account[_-]?key|shared[_-]?access[_-]?key|subscription[_-]?key"
            r"|client[_ -]?secret|credentials?|signature|sig|session[_-]?id)"
            r"(?:[\"']?[ \t]*(?:=>|[:=>])|[\"'][ \t]*,)[ \t]*(?:b(?=[\"']))?)"
            # A quoted value runs to its closing quote, past any backslash
            # escape inside it, so an escaped quote doesn't end it early.
            r"(?:\"(?:[^\"\\\r\n]|\\.)*\"?|'(?:[^'\\\r\n]|\\.)*'?|[^\s\"',;&}<]+)"
        ),
        rf"\1{_REDACTED}",
    ),
    # ``PGPASSWORD=``, ``dbpassword=``, ``apitoken=``: a secret word glued to
    # the end of a longer key with no separator or case change.
    (
        re.compile(
            r"(?i)(?<=[a-z0-9])((?:token|secret|password|passwd)[\"']?[ \t]*[:=][ \t]*)"
            r"(?:\"(?:[^\"\\\r\n]|\\.)*\"?|'(?:[^'\\\r\n]|\\.)*'?|[^\s\"',;&}<]+)"
        ),
        rf"\1{_REDACTED}",
    ),
    # A command-line flag naming a secret, then its value after a space:
    # ``--password hunter2`` or, in an argv repr, ``'--token', 'abc'``.
    (
        re.compile(
            r"(?i)(?<![a-z0-9-])(--?[a-z0-9-]*(?:token|secret|password|passwd|api-key|pass)[\"']?(?:[ \t]*,)?[ \t]+)"
            r"(?:'[^'\r\n]*'?|\"[^\"\r\n]*\"?|[^\s\"',]+)"
        ),
        rf"\1{_REDACTED}",
    ),
)


def _redact_credentials(text: str) -> str:
    """Return *text* with every credential :data:`_CREDENTIAL_PATTERNS` recognizes replaced by ``[REDACTED]``.

    Covers an ``Authorization``, ``Cookie`` or ``Set-Cookie`` header's value,
    ``Bearer`` and ``Basic`` credentials, JWTs, and the value of a
    ``key=value`` or ``key: value`` pair whose key is ``token``, ``secret``,
    ``password``, ``passwd``, ``api_key`` (or ``apikey``, ``api-key``),
    ``access_key``, ``secret_key``, ``private_key``, ``client_secret``,
    ``credential``, ``credentials``, ``sig`` or ``signature``, in any case,
    including as the last word of a longer key such as ``access_token`` or
    ``accessToken``. Callers decode the text first (:func:`_decode_escapes`),
    so an escaped quote around a key or value does not hide it.
    """
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# A JSON escape that can hide a URL or a credential from the scans: any
# character as ``\uXXXX`` (Go's encoder writes ``&`` as ``\u0026``), or
# an escaped quote, slash or backslash. Each match has a fixed length, so a
# pass is linear in the text.
_JSON_ESCAPE = re.compile(r"\\(?:u([0-9A-Fa-f]{4})|([\"/\\]))")

# Each decoding pass undoes one level of encoding. Text encoded more deeply
# than this is no shape an upstream's error produces, and the passes keep the
# work linear.
_MAX_DECODE_PASSES = 4


def _decode_json_escape(match: re.Match[str]) -> str:
    code, char = match.groups()
    return chr(int(code, 16)) if code is not None else char


def _decode_escapes(text: str) -> str:
    """Return *text* with its JSON escapes and percent-encoding decoded, up to :data:`_MAX_DECODE_PASSES` levels deep.

    An upstream's error text can quote a URL or a credential as JSON
    (``https:\\/\\/…``, ``\\u0026token=…``, ``{\\"password\\": …}``) or
    percent-encoded (``https%3A%2F%2F…``, ``token%3D…``), which hides it from
    :func:`_url_spans` and :data:`_CREDENTIAL_PATTERNS`. Decoding first lets
    both scans see it. Each URL is cut to its host before each decoding
    step, JSON and percent alike, so an escaped ``/``, ``?``, ``#`` or ``@``
    inside a URL's userinfo is decoded only after the userinfo is gone and
    can never move where the host starts or ends. An escaped surrogate pair
    becomes its character and a lone surrogate becomes U+FFFD, so the result
    always encodes.
    """
    for _ in range(_MAX_DECODE_PASSES):
        text = _scrub_urls_in_text(text)
        decoded = _JSON_ESCAPE.sub(_decode_json_escape, text)
        decoded = unquote(_scrub_urls_in_text(decoded))
        if decoded == text:
            break
        text = decoded
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _sanitize(text: str) -> str:
    """Return *text* redacted, decoded (:func:`_decode_escapes`), redacted again, and each URL cut to its host.

    Redacting before decoding keeps a secret whole when decoding would turn
    an escaped delimiter inside it (``%26``, ``\\"``) into a real one;
    redacting after catches what decoding revealed. Redaction is idempotent.
    """
    return _scrub_urls_in_text(_redact_credentials(_decode_escapes(_redact_credentials(text))))


def _capped(text: str) -> str:
    """*text* cut to the scan cap, less the word the cut split (a URL or a credential cut mid-way)."""
    if len(text) <= _MAX_SCANNED_MESSAGE_CHARS:
        return text
    head = text[:_MAX_SCANNED_MESSAGE_CHARS]
    end = max(head.rfind(c) for c in " \t\r\n")
    return head[:end] if end >= 0 else ""


def _message(exc: BaseException) -> str:
    """*exc*'s message, capped (:func:`_capped`), or ``""`` when ``str(exc)`` itself raises.

    A failure to render the message must never fall back to logging the
    exception unredacted.
    """
    try:
        return _capped(str(exc))
    except Exception:
        return ""


def _cut(text: str) -> str:
    """Cut *text* to :data:`_MAX_FAILURE_SUMMARY_CHARS`, marking a cut with an ellipsis."""
    if len(text) > _MAX_FAILURE_SUMMARY_CHARS:
        return text[: _MAX_FAILURE_SUMMARY_CHARS - 1] + "…"
    return text


def _independent_failures(exc: BaseException) -> list[BaseException]:
    """The separate failures *exc* reports, outermost first.

    *exc* and everything it wraps (see :func:`_walk_wrapped_exceptions`),
    less exception groups (their members are the failures) and any
    exception that is only another's explicit ``__cause__`` (it is part of
    that failure, not a separate one).
    """
    walked = list(_walk_wrapped_exceptions(exc))
    # Collect every explicit cause before filtering: a group may list a
    # cause before the exception that wraps it.
    causes = {id(current.__cause__) for current in walked if current.__cause__ is not None}
    failures = [
        current for current in walked if not isinstance(current, BaseExceptionGroup) and id(current) not in causes
    ]
    return failures or [exc]


def _failure_summary(exc: BaseException) -> tuple[str, int]:
    """Return a one-line summary of *exc* for a log line, and how many separate failures it reports.

    The summary names the outermost failure (looking through exception
    groups) by type and the first line of its message, so a pydantic
    validation report or a traceback-shaped message stays one line, and
    adds how many more failures a group carries. The message is cut to
    :data:`_MAX_SCANNED_MESSAGE_CHARS` before anything else, has its
    credentials redacted (see :func:`_redact_credentials`), so decoding
    cannot split a secret at a delimiter it decodes to, and is decoded (see
    :func:`_decode_escapes`) before it is split into lines, so an encoded
    line break cannot start a second line; then credentials are redacted
    again, for the ones decoding revealed, each URL is cut to
    ``scheme://host[:port]`` (see :func:`_scrub_urls_in_text`), and the
    summary is cut to :data:`_MAX_FAILURE_SUMMARY_CHARS`.

    It carries text from an upstream's message, so it is for logs only. A
    response body names the failure with :func:`_failure_identity`.
    """
    failures = _independent_failures(exc)
    first = failures[0]
    lines = _decode_escapes(_redact_credentials(_message(first))).strip().splitlines()
    summary = f"{type(first).__name__}: {lines[0].strip()}" if lines else type(first).__name__
    if len(failures) > 1:
        summary += f" (and {len(failures) - 1} more)"
    return _cut(_scrub_urls_in_text(_redact_credentials(summary))), len(failures)


def _failure_traceback(exc: BaseException) -> str:
    """Render *exc* for a DEBUG log line with no unredacted text from any exception's message.

    The first line is :func:`_failure_summary`'s redacted, URL-scrubbed
    summary, the same one the ERROR line carries. Then come the type of
    every exception :func:`_walk_wrapped_exceptions` visits (*exc*, its
    explicit causes, and the members of any group), outermost first, and
    each one's stack frames from :func:`traceback.format_tb`. A frame
    names a file, a line, a function and that line's source, never an
    exception's message, so an upstream's error text, which can carry a
    credential, reaches the log only through the redacted summary. The
    whole text, frames included, is then decoded, has its credentials
    redacted and each URL cut to ``scheme://host[:port]`` (see
    :func:`_sanitize`), as the summary has.
    """
    summary, _ = _failure_summary(exc)
    chain = list(_walk_wrapped_exceptions(exc))
    lines = [summary, "Exceptions, outermost first: " + ", ".join(type(current).__name__ for current in chain)]
    for current in chain:
        lines.append(f"{type(current).__name__} frames, most recent call last:")
        lines.extend(entry.rstrip("\n") for entry in traceback.format_tb(current.__traceback__))
    # A frame's source line can hold a credential or a URL too, so the whole
    # text is sanitized as the summary was: decoded, credentials, then URLs.
    return _sanitize("\n".join(lines))


def _failure_identity(exc: BaseException) -> str:
    """Name the separate failures *exc* reports with no text from their messages, for a response body.

    Each failure, outermost first, is its exception type's name, followed
    in parentheses by the ``scheme://host[:port]`` of each distinct URL its
    message names, if any; failures are separated by ``"; "``. For
    example, ``RuntimeError (https://h.example)`` or
    ``ValueError; KeyError``. Nothing else from a message appears, because
    an upstream's error text can carry a credential. Each message is cut to
    :data:`_MAX_SCANNED_MESSAGE_CHARS`, has its credentials redacted, is
    decoded (see :func:`_decode_escapes`) and has them redacted again before
    its URLs are read, and the
    result is cut to :data:`_MAX_FAILURE_SUMMARY_CHARS`.
    """
    names: list[str] = []
    length = 0
    for failure in _independent_failures(exc):
        message = _redact_credentials(_decode_escapes(_redact_credentials(_message(failure))))
        hosts = dict.fromkeys(_scrub_url_for_diagnostics(message[start:end]) for start, end in _url_spans(message))
        hosts.pop("<unparseable-url>", None)
        name = type(failure).__name__
        names.append(f"{name} ({', '.join(hosts)})" if hosts else name)
        length += len(names[-1]) + 2
        if length > _MAX_FAILURE_SUMMARY_CHARS:
            break
    return _cut("; ".join(names))


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

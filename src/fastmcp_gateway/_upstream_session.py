"""Observe what one upstream MCP connection actually exchanged.

The gateway's upstream contract depends on four facts the MCP client
(FastMCP 4 on the MCP Python SDK v2) no longer hands back by itself:

* **An HTTP error status.** The SDK's streamable-HTTP client turns a non-2xx
  answer without a JSON-RPC body into a JSON-RPC error it builds itself
  (``-32603`` "Server returned an error response", ``-32601`` "Not Found"),
  so the status and its ``WWW-Authenticate`` challenge are lost. The
  gateway tells an authorization refusal apart from an outage, and a
  registration probe's auth failure from a slow start, by that status.
* **Whether an error is the upstream's answer.** The same client also
  builds errors when a streamed response ends before its answer, or cannot
  be parsed, and hands them to the waiting call exactly like an error the
  upstream sent. Only the upstream's own JSON-RPC error may reach a caller
  as the upstream's words.
* **The exact ``_meta`` of a forwarded call.** The SDK injects W3C trace
  context into every outgoing request's ``_meta`` (and FastMCP adds a
  progress token), overwriting a caller-supplied ``traceparent`` and making
  absent metadata present.
* **Annotation keys outside the spec.** The SDK's ``ToolAnnotations`` model
  drops unknown keys, so an upstream's ``x-raw-output-trusted`` annotation
  never reaches the registry through the parsed ``Tool``.

:class:`ObservedTransport` wraps the transport of a gateway-owned upstream
``Client`` and records, per connection, in an :class:`UpstreamExchange`
(see :func:`observable` for the transports it wraps):

* every JSON-RPC message the session sends and reads (any transport), via a
  ``ClientSession`` subclass handed to the transport through
  ``TransportOptions.session_class``; and
* for streamable HTTP, every HTTP response, via an ``httpx2`` response event
  hook installed through the transport's ``httpx_client_factory``. The hook
  raises ``httpx2.HTTPStatusError`` for a failed request that carries no
  JSON-RPC error (the documented ``httpx`` pattern for turning statuses into
  exceptions), so the client surfaces that status, as the MCP SDK v1 client
  did. The ``server/discover`` era probe is exempt: a handshake-era server
  rejects it, and the client falls back to the ``initialize`` handshake.
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

import httpx2
from fastmcp.client.transports import ClientTransport, FastMCPTransport, SSETransport, StreamableHttpTransport
from fastmcp.client.transports.base import TransportOptions
from mcp import ClientSession
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCError, JSONRPCRequest, JSONRPCResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from types import TracebackType
    from typing import Self

    from fastmcp.client.transports.base import SessionKwargs
    from mcp import MCPError
    from typing_extensions import Unpack

#: The request a ``mode="auto"`` client sends first to learn the server's era.
#: A handshake-era server rejects it, and the client then falls back to the
#: ``initialize`` handshake, so its failure is never the upstream's verdict.
ERA_PROBE_METHOD = "server/discover"

#: ``_meta`` keys under this prefix are reserved for the protocol itself. On
#: a 2026-07-28 connection the client stamps each request with them (its
#: protocol version, client info and capabilities), so they describe one
#: connection and are never forwarded from one hop to the next.
RESERVED_META_PREFIX = "io.modelcontextprotocol/"

_CALL_METHOD = "tools/call"
_LIST_METHOD = "tools/list"


class _Unpinned:
    """Sentinel: forward a call's ``_meta`` as the client builds it."""

    def __repr__(self) -> str:
        return "UNPINNED"


UNPINNED = _Unpinned()

#: Transports that open a new MCP session for every connection, so a
#: connection's records can be bound to the session it builds. A transport
#: that keeps one session alive across connections (stdio) rebuilds it
#: whenever the session options change, so it is left unwrapped: its calls
#: then forward ``_meta`` as the client builds it, and no error it reports
#: is taken for the upstream's answer.
_PER_CONNECTION_TRANSPORTS = (StreamableHttpTransport, SSETransport, FastMCPTransport)


def observable(transport: object) -> bool:
    """Whether :class:`ObservedTransport` can wrap *transport*."""
    return isinstance(transport, _PER_CONNECTION_TRANSPORTS)


#: An error as a comparable value: ``(code, message, data)``.
_ErrorKey = tuple[Any, Any, Any]


def application_meta(meta: Mapping[str, Any]) -> dict[str, Any]:
    """*meta* without the protocol-reserved ``io.modelcontextprotocol/`` keys."""
    return {key: value for key, value in meta.items() if not key.startswith(RESERVED_META_PREFIX)}


def _request_key(request_id: Any) -> str:
    # A server may echo ``1`` as ``"1"``; the SDK correlates the two, and so do we.
    return str(request_id)


def _error_key(error: Mapping[str, Any]) -> _ErrorKey:
    return error.get("code"), error.get("message"), error.get("data")


def _jsonrpc_error(message: Any) -> _ErrorKey | None:
    """``(code, message, data)`` of a JSON-RPC error response, else ``None``."""
    if not isinstance(message, dict) or "method" in message:
        return None
    error = message.get("error")
    if not isinstance(error, dict):
        return None
    return _error_key(error)


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None


def _pin_meta(params: dict[str, Any] | None, call_meta: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Rewrite a ``tools/call`` request's ``_meta`` to the caller's exact metadata.

    The result is the caller's metadata plus the protocol-reserved keys the
    session stamped for this connection; everything else the client stack
    added (trace context, a progress token) is dropped. Absent caller
    metadata on a connection that stamps nothing stays absent.
    """
    out = dict(params or {})
    sent = out.get("_meta")
    reserved = {
        key: value
        for key, value in (sent.items() if isinstance(sent, dict) else ())
        if key.startswith(RESERVED_META_PREFIX)
    }
    if call_meta is None and not reserved:
        out.pop("_meta", None)
    else:
        out["_meta"] = {**copy.deepcopy(dict(call_meta or {})), **reserved}
    return out or None


@dataclass
class UpstreamExchange:
    """What one connection to an upstream sent and received.

    One is created per connection by :class:`ObservedTransport`; the gateway
    reads it after the connection's call returned or failed.
    """

    call_meta: Mapping[str, Any] | _Unpinned | None = UNPINNED
    #: ``True`` when an ``httpx2.Auth`` flow answers ``401``/``403`` itself
    #: (it retries with fresh credentials), so the hook leaves those to it.
    auth_flow: bool = False
    #: ``True`` once an HTTP client carrying the response hook was built for
    #: this connection: error provenance is then judged by response bodies.
    observes_http: bool = False
    #: ``True`` when a ``tools/call`` response body could not be read as sent
    #: (a content-encoded event stream): provenance falls back to the session.
    opaque_http: bool = False
    _methods: dict[str, str] = field(default_factory=dict)
    _delivered_call_errors: list[_ErrorKey] = field(default_factory=list)
    _answered_call_errors: list[_ErrorKey] = field(default_factory=list)
    _listed_tools: list[dict[str, Any]] = field(default_factory=list)
    _auth_failure: httpx2.Response | None = None

    # -- session side -----------------------------------------------------

    def outgoing(self, item: SessionMessage) -> SessionMessage:
        """Record an outgoing request, pinning a ``tools/call``'s ``_meta``."""
        message = item.message
        if not isinstance(message, JSONRPCRequest):
            return item
        self._methods[_request_key(message.id)] = message.method
        if message.method != _CALL_METHOD or isinstance(self.call_meta, _Unpinned):
            return item
        pinned = message.model_copy(update={"params": _pin_meta(message.params, self.call_meta)})
        return replace(item, message=pinned)

    def incoming(self, item: object) -> None:
        """Record a ``tools/call`` error and ``tools/list`` page the session reads."""
        if not isinstance(item, SessionMessage):
            return
        message = item.message
        if not isinstance(message, JSONRPCError | JSONRPCResponse) or message.id is None:
            return
        method = self._methods.get(_request_key(message.id))
        if isinstance(message, JSONRPCError) and method == _CALL_METHOD:
            self._delivered_call_errors.append((message.error.code, message.error.message, message.error.data))
        elif isinstance(message, JSONRPCResponse) and method == _LIST_METHOD:
            tools = message.result.get("tools")
            if isinstance(tools, list):
                self._listed_tools.extend(t for t in tools if isinstance(t, dict))

    # -- HTTP side --------------------------------------------------------

    async def on_http_response(self, response: httpx2.Response) -> None:
        """``httpx2`` response hook: raise for an HTTP failure, note body errors."""
        request = response.request
        if request.method != "POST":
            return
        try:
            sent = json.loads(request.content)
        except (httpx2.RequestNotRead, ValueError):
            sent = None
        if not isinstance(sent, dict) or "jsonrpc" not in sent:
            return  # not an MCP message (an auth flow's token request, say)
        method = sent.get("method")
        if method is None or "id" not in sent:
            # A notification, or the client's answer to a server request: no
            # reply is due, and the SDK ignores the status of such a POST.
            return
        content_type = response.headers.get("content-type", "").lower()
        if self.auth_flow and response.status_code in (401, 403):
            # The auth flow answers this challenge itself and may retry. If it
            # gives up, this is the response the request ended on, and
            # auth_failure() reports it at the gateway's error boundary.
            self._auth_failure = response
            return
        self._auth_failure = None
        if response.status_code >= 400:
            if method == ERA_PROBE_METHOD:
                return
            if content_type.startswith("application/json"):
                try:
                    body = await response.aread()
                except (httpx2.TransportError, httpx2.StreamError):
                    body = b""  # the body is lost; the status still decides
                error = _jsonrpc_error(_parse_json(body))
                if error is not None:
                    # A JSON-RPC error at an error status (a 2026-07-28 server
                    # maps Invalid params to 400) is the server's answer.
                    if method == _CALL_METHOD:
                        self._answered_call_errors.append(error)
                    return
            response.raise_for_status()
        if method != _CALL_METHOD or not 200 <= response.status_code < 300:
            return
        if content_type.startswith("application/json"):
            error = _jsonrpc_error(_parse_json(await response.aread()))
            if error is not None:
                self._answered_call_errors.append(error)
        elif content_type.startswith("text/event-stream"):
            if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                self.opaque_http = True
                return
            response.stream = _EventStreamTap(response.stream, self._answered_call_errors.append)

    # -- verdicts ---------------------------------------------------------

    def answered(self, exc: MCPError) -> bool:
        """Whether *exc* is a JSON-RPC error the upstream sent for the call.

        On streamable HTTP the response bodies decide: an error counts only if
        it was in what the upstream sent in reply to ``tools/call``. On any
        other transport nothing between the upstream and the session builds
        errors, so an error the session read for ``tools/call`` counts. An
        error raised without either (the session closed with the call pending,
        the call timed out) is not an answer.
        """
        key = (exc.error.code, exc.error.message, exc.error.data)
        if self.observes_http and not self.opaque_http:
            return key in self._answered_call_errors
        return key in self._delivered_call_errors

    def auth_failure(self) -> httpx2.HTTPStatusError | None:
        """The ``401``/``403`` an auth flow ended on, when the connection's last request got one.

        The MCP client reports such a response as a JSON-RPC error of its own,
        which would hide the status the gateway classifies credentials on.
        """
        response = self._auth_failure
        if response is None:
            return None
        return httpx2.HTTPStatusError(
            f"upstream answered {response.status_code} after its auth flow",
            request=response.request,
            response=response,
        )

    def listed_tools(self) -> dict[str, dict[str, Any]]:
        """Each tool of this connection's ``tools/list`` answers, exactly as sent, by name."""
        return {t["name"]: t for t in self._listed_tools if isinstance(t.get("name"), str)}


class _ReadTap:
    """A session read stream that reports each item to its exchange."""

    def __init__(self, inner: Any, exchange: UpstreamExchange) -> None:
        self._inner = inner
        self._exchange = exchange

    @property
    def last_context(self) -> Any:
        # The dispatcher reads the sender's contextvars snapshot from here.
        return getattr(self._inner, "last_context", None)

    async def receive(self) -> Any:
        item = await self._inner.receive()
        self._exchange.incoming(item)
        return item

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Any:
        item = await self._inner.__anext__()
        self._exchange.incoming(item)
        return item

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def __aenter__(self) -> Self:
        await self._inner.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        return await self._inner.__aexit__(exc_type, exc_val, exc_tb)


class _WriteTap:
    """A session write stream that passes each item through its exchange."""

    def __init__(self, inner: Any, exchange: UpstreamExchange) -> None:
        self._inner = inner
        self._exchange = exchange

    async def send(self, item: SessionMessage, /) -> None:
        await self._inner.send(self._exchange.outgoing(item))

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def __aenter__(self) -> Self:
        await self._inner.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        return await self._inner.__aexit__(exc_type, exc_val, exc_tb)


class _ObservedSession(ClientSession):
    """A ``ClientSession`` whose streams report to an :class:`UpstreamExchange`.

    The exchange is bound per connection on a throwaway subclass (see
    :meth:`ObservedTransport.connect_session`), because a transport builds the
    session itself from ``TransportOptions.session_class``.
    """

    _exchange: UpstreamExchange

    def __init__(self, read_stream: Any = None, write_stream: Any = None, *args: Any, **kwargs: Any) -> None:
        if read_stream is not None and write_stream is not None:
            read_stream = _ReadTap(read_stream, self._exchange)
            write_stream = _WriteTap(write_stream, self._exchange)
        super().__init__(read_stream, write_stream, *args, **kwargs)


_LINE_END = re.compile(rb"\r\n|\r|\n")


class _EventStreamTap(httpx2.AsyncByteStream):
    """Pass an event stream through unchanged, noting each JSON-RPC error event."""

    def __init__(self, inner: Any, on_error: Callable[[_ErrorKey], None]) -> None:
        self._inner = inner
        self._on_error = on_error
        self._buffer = bytearray()
        self._event = ""
        self._data: list[str] = []

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._feed(chunk)
            yield chunk
        if self._buffer:
            self._line(bytes(self._buffer))
            self._buffer.clear()
        self._dispatch()

    async def aclose(self) -> None:
        await self._inner.aclose()

    def _feed(self, chunk: bytes) -> None:
        # The buffered tail holds no line end, except perhaps a final CR, so
        # only the new bytes need scanning: a long line costs linear time.
        pos = max(len(self._buffer) - 1, 0)
        self._buffer.extend(chunk)
        start = 0
        while (match := _LINE_END.search(self._buffer, pos)) is not None:
            if match.group() == b"\r" and match.end() == len(self._buffer):
                break  # possibly the first half of a CRLF split across chunks
            self._line(bytes(self._buffer[start : match.start()]))
            start = pos = match.end()
        del self._buffer[:start]

    def _line(self, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace")
        if not line:
            self._dispatch()
            return
        if line.startswith(":"):
            return
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "data":
            self._data.append(value)
        elif name == "event":
            self._event = value

    def _dispatch(self) -> None:
        data, event = "\n".join(self._data), self._event or "message"
        self._data, self._event = [], ""
        # The client handles only ``message`` events, and an error event is
        # small: skip parsing any payload that cannot be one.
        if event != "message" or '"error"' not in data:
            return
        error = _jsonrpc_error(_parse_json(data.encode()))
        if error is not None:
            self._on_error(error)


class ObservedTransport(ClientTransport):
    """Wrap a client transport so each connection records an :class:`UpstreamExchange`.

    *call_meta*, when given, is the exact ``_meta`` every ``tools/call`` on
    this transport carries (``None`` keeps it absent); see :func:`_pin_meta`.
    """

    def __init__(self, inner: ClientTransport, *, call_meta: Mapping[str, Any] | _Unpinned | None = UNPINNED) -> None:
        self.inner = inner
        self.call_meta = call_meta
        self.exchange: UpstreamExchange | None = None
        self._base_factory: Any = None
        if isinstance(inner, StreamableHttpTransport):
            self._base_factory = inner.httpx_client_factory
            inner.httpx_client_factory = self._http_client_factory

    def fork(self, *, call_meta: Mapping[str, Any] | _Unpinned | None = UNPINNED) -> ObservedTransport:
        """An independent copy for one connection, with its own headers and records."""
        inner = copy.copy(self.inner)
        if isinstance(inner, StreamableHttpTransport | SSETransport) and isinstance(inner.headers, dict):
            # The shallow copy shares the base transport's mapping; a change
            # to this connection's headers must never reach the base.
            inner.headers = dict(inner.headers)
        if isinstance(inner, StreamableHttpTransport):
            inner.httpx_client_factory = self._base_factory
        return ObservedTransport(inner, call_meta=call_meta)

    @property
    def legacy_only(self) -> bool:  # type: ignore[override]
        return self.inner.legacy_only

    @property
    def headers(self) -> dict[str, str]:
        """The wrapped transport's headers; ``AttributeError`` when it has none."""
        return cast("dict[str, str]", getattr(self.inner, "headers"))  # noqa: B009

    @headers.setter
    def headers(self, value: dict[str, str]) -> None:
        self.inner.headers = value  # type: ignore[attr-defined]

    def _http_client_factory(
        self,
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        **kwargs: Any,
    ) -> httpx2.AsyncClient:
        # Build the client exactly as the wrapped transport would without a
        # factory of ours: its own factory, else its TLS-verify factory, else
        # the MCP SDK's defaults (which leave redirects to the transport).
        client: httpx2.AsyncClient
        if self._base_factory is not None:
            client = self._base_factory(headers=headers, timeout=timeout, auth=auth, **kwargs)
        else:
            verify_factory: Callable[..., httpx2.AsyncClient] | None = None
            make_verify_factory = getattr(self.inner, "_make_verify_factory", None)
            if callable(make_verify_factory):
                verify_factory = cast("Callable[..., httpx2.AsyncClient] | None", make_verify_factory())
            make: Callable[..., httpx2.AsyncClient] = (
                verify_factory if verify_factory is not None else create_mcp_http_client
            )
            client = make(headers=headers, timeout=timeout, auth=auth)
        exchange = self.exchange
        if exchange is not None:
            exchange.observes_http = True
            exchange.auth_flow = auth is not None
            client.event_hooks.setdefault("response", []).append(exchange.on_http_response)
        return client

    @contextlib.asynccontextmanager
    async def connect_session(
        self,
        *,
        transport_options: TransportOptions | None = None,
        **session_kwargs: Unpack[SessionKwargs],
    ) -> AsyncIterator[ClientSession]:
        exchange = UpstreamExchange(call_meta=self.call_meta)
        self.exchange = exchange
        session_class = cast(
            "type[ClientSession]",
            type("_ObservedClientSession", (_ObservedSession,), {"_exchange": exchange}),
        )
        options = replace(transport_options or TransportOptions(), session_class=session_class)
        async with self.inner.connect_session(transport_options=options, **session_kwargs) as session:
            yield session

    def get_session_id(self) -> str | None:
        return self.inner.get_session_id()

    async def close(self) -> None:
        await self.inner.close()

    def _set_auth(self, auth: Any) -> None:
        self.inner._set_auth(auth)

    def __repr__(self) -> str:
        return f"<ObservedTransport({self.inner!r})>"

"""Serve MCP servers over real HTTP for tests, and shape what they answer.

An in-process ``FastMCP`` upstream exercises the MCP session but no HTTP: no
status codes, no ``MCP-Protocol-Version`` header, no event streams. The
helpers here run an ASGI app on an ephemeral local port with uvicorn and wrap
it with small ASGI middlewares that reproduce what a real upstream sends on
the wire in the cases the gateway has to tell apart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import uvicorn
from mcp.types.version import HANDSHAKE_PROTOCOL_VERSIONS
from sse_starlette.sse import AppStatus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    Scope = dict[str, Any]
    Message = dict[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


@asynccontextmanager
async def serve_http(app: Any) -> AsyncIterator[str]:
    """Run *app* on ``127.0.0.1`` at an ephemeral port; yield its base URL."""
    # ws="none": no WebSocket stack (MCP does not use one), so uvicorn imports none.
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on", ws="none")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
        # sse_starlette watches the running uvicorn server's ``should_exit``
        # and, on seeing it, sets its process-wide ``AppStatus.should_exit``,
        # which ends every event stream any later server opens at once. The
        # server has stopped: clear both, so the next one starts clean.
        server.should_exit = False
        AppStatus.should_exit = False


def _header(scope: Scope, name: bytes) -> str | None:
    return next((v.decode("latin-1") for k, v in scope["headers"] if k == name), None)


async def _read_body(receive: Receive) -> tuple[bytes, Receive]:
    """Read the request body, and return a ``receive`` that replays it."""
    messages: list[Message] = []
    body = b""
    while True:
        message = await receive()
        messages.append(message)
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    pending = iter(messages)

    async def replay() -> Message:
        try:
            return next(pending)
        except StopIteration:
            return await receive()

    return body, replay


def _jsonrpc_method(body: bytes) -> str | None:
    try:
        message = json.loads(body)
    except ValueError:
        return None
    return message.get("method") if isinstance(message, dict) else None


async def _send_json(send: Send, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
    body = json.dumps(payload).encode()
    raw_headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    raw_headers += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": raw_headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


class HandshakeEraOnly:
    """Serve an MCP app as a handshake-era (MCP SDK v1) server would.

    MCP SDK v1 servers (FastMCP 3) know only the handshake-era protocol
    versions. Their streamable-HTTP transport validates each request's
    ``MCP-Protocol-Version`` header and answers any other version, before
    the request reaches the server, with HTTP 400 and this JSON-RPC error
    body (verified against a FastMCP 3.4 server on MCP SDK 1.26 and 1.30).
    A 2026-07-28 request, such as the ``server/discover`` probe a
    ``mode="auto"`` client opens with, therefore never reaches the wrapped
    app, which serves the handshake-era traffic itself. :attr:`rejected`
    lists the JSON-RPC method of every request answered that way.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.rejected: list[str | None] = []
        self.methods: list[str | None] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body, receive = await _read_body(receive)
        method = _jsonrpc_method(body) if scope["method"] == "POST" else None
        if method is not None:
            self.methods.append(method)
        version = _header(scope, b"mcp-protocol-version")
        if version is not None and version not in HANDSHAKE_PROTOCOL_VERSIONS:
            self.rejected.append(method)
            supported = ", ".join(HANDSHAKE_PROTOCOL_VERSIONS)
            await _send_json(
                send,
                400,
                {
                    "jsonrpc": "2.0",
                    "id": "server-error",
                    "error": {
                        "code": -32600,
                        "message": (
                            f"Bad Request: Unsupported protocol version: {version}. Supported versions: {supported}"
                        ),
                    },
                },
            )
            return
        await self.app(scope, receive, send)


class RecordEras:
    """Pass every request through, recording the protocol version each one declares."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.versions: list[str | None] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST":
            self.versions.append(_header(scope, b"mcp-protocol-version"))
        await self.app(scope, receive, send)


class FailMethod:
    """Answer POSTs of one JSON-RPC *method* (``None``: every one) with an HTTP error.

    Mirrors an authorizing proxy or an upstream's auth middleware, or a route
    that is not mounted yet: the request is refused before it reaches the MCP
    server. By default the ``application/json`` body is not a JSON-RPC
    message. With *jsonrpc_error* (``(code, message)``) the body is a JSON-RPC
    error answering the request, as an upstream's own auth middleware may
    send with its ``401``.
    """

    def __init__(
        self,
        app: ASGIApp,
        method: str | None,
        status: int,
        headers: dict[str, str] | None = None,
        *,
        jsonrpc_error: tuple[int, str] | None = None,
    ) -> None:
        self.app = app
        self.method = method
        self.status = status
        self.headers = headers or {}
        self.jsonrpc_error = jsonrpc_error
        self.failed = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST":
            body, receive = await _read_body(receive)
            method = _jsonrpc_method(body)
            if method is not None and self.method in (None, method):
                self.failed += 1
                payload: Any = {"error": "refused"}
                if self.jsonrpc_error is not None:
                    code, message = self.jsonrpc_error
                    request_id = json.loads(body).get("id")
                    payload = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
                await _send_json(send, self.status, payload, self.headers)
                return
        await self.app(scope, receive, send)


class StallMethod:
    """Accept POSTs of one JSON-RPC *method* and never answer them.

    The response opens an event stream at once and then carries only
    keep-alive comments, so the HTTP connection stays busy while no answer
    arrives: the client's session deadline expires before any HTTP read
    timeout does, as with an upstream stuck behind a slow dependency. The
    stream ends when the client disconnects, or after *seconds*.
    :attr:`stalled` counts the requests held.

    Ending on disconnect keeps the server's shutdown prompt rather than
    holding it for *seconds*.
    """

    def __init__(self, app: ASGIApp, method: str, seconds: float = 5.0, *, keep_alive: float = 0.05) -> None:
        self.app = app
        self.method = method
        self.seconds = seconds
        self.keep_alive = keep_alive
        self.stalled = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST":
            body, receive = await _read_body(receive)
            if _jsonrpc_method(body) == self.method:
                self.stalled += 1
                await self._stall(receive, send)
                return
        await self.app(scope, receive, send)

    async def _stall(self, receive: Receive, send: Send) -> None:
        disconnected = asyncio.Event()

        async def watch() -> None:
            while (await receive())["type"] != "http.disconnect":
                pass
            disconnected.set()

        watcher = asyncio.create_task(watch())
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream"), (b"cache-control", b"no-cache")],
                }
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.seconds
            while not disconnected.is_set() and loop.time() < deadline:
                await send({"type": "http.response.body", "body": b": keep-alive\n\n", "more_body": True})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(disconnected.wait(), self.keep_alive)
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher


class CutResponse:
    """End the response to every ``tools/call`` POST before it carries an answer.

    The status line and headers go out as the server sent them; the body
    then ends empty, as when an upstream dies, or a proxy drops the
    connection, mid-call.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.cut = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        body, receive = await _read_body(receive)
        if _jsonrpc_method(body) != "tools/call":
            await self.app(scope, receive, send)
            return
        self.cut += 1
        state = {"ended": False}

        async def cut_send(message: Message) -> None:
            if state["ended"]:
                return
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"content-length"]
                await send({**message, "headers": headers})
            elif message["type"] == "http.response.body":
                state["ended"] = True
                await send({"type": "http.response.body", "body": b"", "more_body": False})

        await self.app(scope, receive, cut_send)


class AddAnnotationKey:
    """Add a key to every tool's ``annotations`` in ``tools/list`` answers.

    An MCP SDK v1 server (FastMCP 3) kept annotation keys the spec does not
    define, such as ``x-raw-output-trusted``, and sent them; an SDK v2
    server cannot. This rewrites the JSON answers of an app built with
    ``json_response=True`` to send what such a server sent.
    """

    def __init__(self, app: ASGIApp, key: str, value: Any) -> None:
        self.app = app
        self.key = key
        self.value = value

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        body, receive = await _read_body(receive)
        if _jsonrpc_method(body) != "tools/list":
            await self.app(scope, receive, send)
            return
        start: Message = {}
        chunks: list[bytes] = []

        async def rewrite_send(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            payload = json.loads(b"".join(chunks))
            for tool in payload.get("result", {}).get("tools", []):
                tool.setdefault("annotations", {})[self.key] = self.value
            new_body = json.dumps(payload).encode()
            headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(new_body)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": new_body, "more_body": False})

        await self.app(scope, receive, rewrite_send)

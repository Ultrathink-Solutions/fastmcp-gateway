"""Unit tests for the upstream-connection observer's pure parts.

The end-to-end behaviour is exercised over real HTTP in
``tests/test_upstream_eras.py``; these pin the edge cases that are awkward
to provoke from a real server.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from fastmcp import FastMCP
from fastmcp.client.transports import FastMCPTransport, SSETransport, StreamableHttpTransport
from mcp import MCPError

from fastmcp_gateway._upstream_session import (
    UNPINNED,
    ObservedTransport,
    UpstreamExchange,
    _EventStreamTap,
    _pin_meta,
    application_meta,
    observable,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_RESERVED = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


class TestPinMeta:
    def test_absent_caller_metadata_stays_absent(self) -> None:
        params = {"name": "t", "_meta": {"traceparent": "00-injected-01", "progressToken": 3}}
        assert _pin_meta(params, None) == {"name": "t"}

    def test_the_callers_metadata_replaces_what_the_client_added(self) -> None:
        caller = {"traceparent": "00-caller-01", "nested": {"a": [1]}}
        params = {"name": "t", "_meta": {"traceparent": "00-injected-01", "progressToken": 3}}
        assert _pin_meta(params, caller) == {"name": "t", "_meta": caller}

    def test_present_but_empty_caller_metadata_stays_present(self) -> None:
        assert _pin_meta({"name": "t"}, {}) == {"name": "t", "_meta": {}}

    def test_the_sessions_reserved_keys_are_kept(self) -> None:
        params = {"name": "t", "_meta": {**_RESERVED, "traceparent": "00-injected-01"}}
        assert _pin_meta(params, None) == {"name": "t", "_meta": _RESERVED}
        assert _pin_meta(params, {"k": "v"}) == {"name": "t", "_meta": {"k": "v", **_RESERVED}}

    def test_the_callers_copy_is_detached(self) -> None:
        caller: dict[str, Any] = {"nested": {"a": 1}}
        pinned = _pin_meta({"name": "t"}, caller)
        assert pinned is not None
        pinned["_meta"]["nested"]["a"] = 2
        assert caller == {"nested": {"a": 1}}


def test_application_meta_drops_only_reserved_keys() -> None:
    assert application_meta({**_RESERVED, "traceparent": "x", "io.example/k": 1}) == {
        "traceparent": "x",
        "io.example/k": 1,
    }


def test_only_per_connection_transports_are_observable() -> None:
    assert observable(StreamableHttpTransport("http://svc:8080/mcp"))
    assert observable(SSETransport("http://svc:8080/sse"))
    assert observable(FastMCPTransport(FastMCP("svc")))
    assert not observable(object())


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def _drain(*parts: bytes) -> tuple[bytes, list[tuple[Any, Any, Any]]]:
    seen: list[tuple[Any, Any, Any]] = []
    tap = _EventStreamTap(_chunks(*parts), seen.append)
    passed = b"".join([chunk async for chunk in tap])
    return passed, seen


def _event(message: dict[str, Any], *, eol: bytes = b"\n", event: str | None = None) -> bytes:
    head = f"event: {event}".encode() + eol if event else b""
    return head + b"data: " + json.dumps(message).encode() + eol + eol


_ERROR = {"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "Connection closed"}}
_RESULT = {"jsonrpc": "2.0", "id": 2, "result": {"content": [], "note": '"error" inside a result'}}


class TestEventStreamTap:
    @pytest.mark.asyncio
    async def test_passes_bytes_through_and_notes_an_error_event(self) -> None:
        stream = b": keep-alive\n\n" + _event(_RESULT) + _event(_ERROR)
        passed, seen = await _drain(stream)
        assert passed == stream
        assert seen == [(-32000, "Connection closed", None)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("eol", [b"\n", b"\r\n", b"\r"])
    async def test_any_line_ending_split_anywhere(self, eol: bytes) -> None:
        stream = _event(_ERROR, eol=eol)
        for cut in range(1, len(stream)):
            _, seen = await _drain(stream[:cut], stream[cut:])
            assert seen == [(-32000, "Connection closed", None)], cut

    @pytest.mark.asyncio
    async def test_multi_line_data_is_one_message(self) -> None:
        body = json.dumps(_ERROR, indent=1).splitlines()
        stream = b"".join(b"data: " + line.encode() + b"\n" for line in body) + b"\n"
        _, seen = await _drain(stream)
        assert seen == [(-32000, "Connection closed", None)]

    @pytest.mark.asyncio
    async def test_ignores_other_event_types_and_non_errors(self) -> None:
        _, seen = await _drain(_event(_ERROR, event="ping"), _event(_RESULT), b"data: not json\n\n")
        assert seen == []

    @pytest.mark.asyncio
    async def test_an_unterminated_final_event_still_counts(self) -> None:
        _, seen = await _drain(b"data: " + json.dumps(_ERROR).encode())
        assert seen == [(-32000, "Connection closed", None)]


class TestAnswered:
    def test_http_bodies_decide_when_observed(self) -> None:
        exchange = UpstreamExchange(call_meta=UNPINNED, observes_http=True)
        exchange._delivered_call_errors.append((-32000, "SSE stream ended without a response", None))
        assert not exchange.answered(MCPError(code=-32000, message="SSE stream ended without a response"))
        exchange._answered_call_errors.append((-32000, "Connection closed", None))
        assert exchange.answered(MCPError(code=-32000, message="Connection closed"))

    def test_session_delivery_decides_otherwise(self) -> None:
        exchange = UpstreamExchange()
        assert not exchange.answered(MCPError(code=-32001, message="Request 'tools/call' timed out"))
        exchange._delivered_call_errors.append((-32602, "bad", {"field": "x"}))
        assert exchange.answered(MCPError(code=-32602, message="bad", data={"field": "x"}))
        assert not exchange.answered(MCPError(code=-32602, message="bad"))


def _failed_post(sent: dict[str, Any], status: int = 500) -> httpx2.Response:
    request = httpx2.Request("POST", "http://svc/mcp", content=json.dumps(sent).encode())
    return httpx2.Response(status, request=request, text="upstream failure")


class TestHttpFailures:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sent",
        [
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
            {"jsonrpc": "2.0", "id": 7, "result": {}},
        ],
        ids=["notification", "answer-to-a-server-request"],
    )
    async def test_a_post_that_expects_no_reply_never_raises(self, sent: dict[str, Any]) -> None:
        # The SDK ignores the HTTP status of such a POST, so the hook does too.
        await UpstreamExchange(observes_http=True).on_http_response(_failed_post(sent))

    @pytest.mark.asyncio
    async def test_an_auth_flow_rejection_is_kept_until_a_later_answer(self) -> None:
        exchange = UpstreamExchange(observes_http=True, auth_flow=True)
        sent = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "widgets_get"}}
        # The flow answers the challenge itself, so the hook does not raise.
        await exchange.on_http_response(_failed_post(sent, status=401))
        failure = exchange.auth_failure()
        assert failure is not None and failure.response.status_code == 401
        # A later answer (the flow's retry succeeded) supersedes it.
        request = httpx2.Request("POST", "http://svc/mcp", content=json.dumps(sent).encode())
        answer = httpx2.Response(200, request=request, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        await exchange.on_http_response(answer)
        assert exchange.auth_failure() is None

    @pytest.mark.asyncio
    async def test_a_failed_request_raises(self) -> None:
        sent = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "widgets_get"}}
        with pytest.raises(httpx2.HTTPStatusError):
            await UpstreamExchange(observes_http=True).on_http_response(_failed_post(sent))

    @pytest.mark.asyncio
    async def test_an_error_body_that_cannot_be_read_still_raises_the_status(self) -> None:
        class _ResetStream(httpx2.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                raise httpx2.ReadError("connection reset")
                yield b""  # pragma: no cover - makes this an async generator

        sent = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "widgets_get"}}
        request = httpx2.Request("POST", "http://svc/mcp", content=json.dumps(sent).encode())
        response = httpx2.Response(
            502, request=request, headers={"content-type": "application/json"}, stream=_ResetStream()
        )
        with pytest.raises(httpx2.HTTPStatusError) as raised:
            await UpstreamExchange(observes_http=True).on_http_response(response)
        assert raised.value.response.status_code == 502


def test_a_fork_never_shares_the_base_transports_headers() -> None:
    base = ObservedTransport(StreamableHttpTransport("http://svc/mcp", headers={"authorization": "Bearer svc"}))
    fork = base.fork()
    fork.inner.headers["x-user-subject"] = "alice"  # type: ignore[attr-defined]
    assert base.inner.headers == {"authorization": "Bearer svc"}  # type: ignore[attr-defined]
    assert fork.inner.headers == {"authorization": "Bearer svc", "x-user-subject": "alice"}  # type: ignore[attr-defined]

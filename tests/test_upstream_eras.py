"""The gateway's upstream connections, over real HTTP, against servers of either era.

The gateway connects to each upstream with ``fastmcp.Client``. Its connect
mode (``upstream_client_mode``) decides which protocol era that connection
negotiates; the default is the ``initialize`` handshake, which every MCP
server serves. A handshake-era upstream (FastMCP 3 on MCP SDK v1) is
reproduced by :class:`~tests.http_upstreams.HandshakeEraOnly`: a real
FastMCP 4 server serves the handshake traffic, and the wrapper answers any
2026-07-28 request exactly as an SDK v1 server does, before it reaches the
server.

The rest of this module holds the HTTP-level contract the MCP SDK v2 client
no longer keeps by itself: an upstream's HTTP error status reaches the
gateway's classification, an error counts as the upstream's answer only when
the upstream sent it, and annotation keys outside the spec survive population.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated, Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from httpx2 import ASGITransport, AsyncClient
from mcp import MCPError
from mcp.types import CONNECTION_CLOSED, INVALID_PARAMS
from mcp.types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS
from pydantic import Field

from fastmcp_gateway.client_manager import UPSTREAM_CLIENT_MODES
from fastmcp_gateway.gateway import GatewayServer
from tests.conftest import result_text
from tests.http_upstreams import (
    AddAnnotationKey,
    CutResponse,
    FailMethod,
    HandshakeEraOnly,
    RecordEras,
    StallMethod,
    serve_http,
)

if TYPE_CHECKING:
    from httpx2 import Response
    from starlette.applications import Starlette

REGISTRATION_TOKEN = "test-registration-token-0123456789"
pytestmark = pytest.mark.filterwarnings("ignore:registration_token is deprecated:DeprecationWarning")

# The gateway's default connect mode, and the probing mode, which must both
# reach every upstream these tests stand up.
CONNECT_MODES = [pytest.param("legacy", id="legacy"), pytest.param("auto", id="auto")]


def _widgets_upstream(*, middleware: list[Middleware] | None = None) -> FastMCP:
    mcp = FastMCP("widgets", middleware=middleware or [])

    @mcp.tool
    def widgets_echo(text: str) -> str:
        """Echo the text back."""
        return f"echo: {text}"

    return mcp


async def _execute(gateway: GatewayServer, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run ``execute_tool`` through the gateway and return its JSON envelope."""
    async with Client(gateway.mcp) as client:
        result = await client.call_tool("execute_tool", {"tool_name": tool_name, "arguments": arguments})
    return json.loads(result_text(result))


# ---------------------------------------------------------------------------
# A handshake-era upstream
# ---------------------------------------------------------------------------


class TestHandshakeEraUpstream:
    @pytest.mark.asyncio
    async def test_the_default_mode_reaches_it_with_the_handshake_alone(self) -> None:
        """No 2026-07-28 request ever reaches a handshake-era upstream by default.

        Registry population and tool execution each open their own
        connection, and each opens with ``initialize``, as releases built on
        FastMCP 3 did.
        """
        upstream = HandshakeEraOnly(_widgets_upstream().http_app())
        async with serve_http(upstream) as base:
            gateway = GatewayServer({"widgets": f"{base}/mcp"})
            assert await gateway.populate() == {"widgets": 1}
            envelope = await _execute(gateway, "widgets_echo", {"text": "hi"})

        assert envelope == {"tool": "widgets_echo", "result": "echo: hi"}
        assert upstream.rejected == []
        assert "server/discover" not in upstream.methods
        assert upstream.methods.count("initialize") == 2

    @pytest.mark.asyncio
    async def test_auto_mode_falls_back_to_the_handshake(self) -> None:
        """``auto`` probes ``server/discover``, is refused, and completes the handshake instead.

        That costs one refused request on every connection: here one for
        population and one for the tool call.
        """
        upstream = HandshakeEraOnly(_widgets_upstream().http_app())
        async with serve_http(upstream) as base:
            gateway = GatewayServer({"widgets": f"{base}/mcp"}, upstream_client_mode="auto")
            assert await gateway.populate() == {"widgets": 1}
            envelope = await _execute(gateway, "widgets_echo", {"text": "hi"})

        assert envelope == {"tool": "widgets_echo", "result": "echo: hi"}
        assert upstream.rejected == ["server/discover", "server/discover"]
        assert upstream.methods.count("initialize") == 2

    @pytest.mark.asyncio
    async def test_a_pinned_2026_mode_cannot_reach_it(self) -> None:
        """Pinning the 2026-07-28 era skips the probe, so nothing falls back."""
        upstream = HandshakeEraOnly(_widgets_upstream().http_app())
        async with serve_http(upstream) as base:
            gateway = GatewayServer({"widgets": f"{base}/mcp"}, upstream_client_mode="2026-07-28")
            assert await gateway.populate() == {}

        assert upstream.rejected == ["tools/list"]
        assert "initialize" not in upstream.methods


class TestDualEraUpstream:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("mode", "eras"),
        [
            pytest.param("legacy", HANDSHAKE_PROTOCOL_VERSIONS, id="legacy"),
            pytest.param("auto", MODERN_PROTOCOL_VERSIONS, id="auto"),
        ],
    )
    async def test_the_mode_decides_the_era(self, mode: str, eras: tuple[str, ...]) -> None:
        """Against a FastMCP 4 upstream, ``legacy`` keeps the handshake and ``auto`` adopts 2026-07-28."""
        upstream = RecordEras(_widgets_upstream().http_app())
        async with serve_http(upstream) as base:
            gateway = GatewayServer({"widgets": f"{base}/mcp"}, upstream_client_mode=mode)
            assert await gateway.populate() == {"widgets": 1}
            upstream.versions.clear()
            envelope = await _execute(gateway, "widgets_echo", {"text": "hi"})

        assert envelope == {"tool": "widgets_echo", "result": "echo: hi"}
        declared = [v for v in upstream.versions if v is not None]
        assert declared
        assert all(v in eras for v in declared)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", CONNECT_MODES)
    async def test_a_header_mirrored_argument_reaches_the_tool(self, mode: str) -> None:
        """A 2026-07-28 server rejects a call whose ``x-mcp-header`` argument
        is not mirrored into its ``Mcp-Param-*`` header, and a client mirrors
        only the arguments of tools it has listed. The gateway's execution
        connection is new for every call, so it lists before calling."""
        mcp = FastMCP("regions")

        @mcp.tool
        def regions_route(region: Annotated[str, Field(json_schema_extra={"x-mcp-header": "Region"})]) -> str:
            """Route to a region."""
            return f"routed to {region}"

        async with serve_http(mcp.http_app()) as base:
            gateway = GatewayServer({"regions": f"{base}/mcp"}, upstream_client_mode=mode)
            assert await gateway.populate() == {"regions": 1}
            envelope = await _execute(gateway, "regions_route", {"region": "eu-west"})

        assert envelope == {"tool": "regions_route", "result": "routed to eu-west"}


def test_an_unknown_mode_is_refused_at_construction() -> None:
    assert "legacy" in UPSTREAM_CLIENT_MODES
    with pytest.raises(ValueError, match="upstream client mode"):
        GatewayServer({"widgets": "http://widgets:8080/mcp"}, upstream_client_mode="2025-11-25")


class TestPerCallHeaders:
    @pytest.mark.asyncio
    async def test_a_calls_headers_never_reach_a_later_call(self) -> None:
        """Each call gets its own copy of the upstream transport.

        ``Client.new()`` shares the transport, so headers merged onto one
        call's client used to stay on the shared transport and went out on
        every later call to that upstream, whoever made it.
        """
        upstream = FastMCP("widgets")
        seen: list[tuple[str | None, str | None]] = []

        @upstream.tool
        def widgets_whoami() -> str:
            """Report who is calling."""
            headers = get_http_headers(include_all=True)
            seen.append((headers.get("x-user-subject"), headers.get("x-api-key")))
            return "ok"

        async with serve_http(upstream.http_app()) as base:
            gateway = GatewayServer({"widgets": f"{base}/mcp"}, upstream_headers={"widgets": {"X-Api-Key": "k-1"}})
            await gateway.populate()
            manager = gateway.upstream_manager
            await manager.execute_tool("widgets_whoami", {}, extra_headers={"X-User-Subject": "alice"})
            await manager.execute_tool("widgets_whoami", {})

        assert seen == [("alice", "k-1"), (None, "k-1")]


# ---------------------------------------------------------------------------
# HTTP error statuses
# ---------------------------------------------------------------------------


_INSUFFICIENT_SCOPE = 'Bearer error="insufficient_scope", scope="widgets:write"'


class TestUpstreamHttpStatuses:
    """The MCP SDK v2 client reports an HTTP error without a JSON-RPC body as a
    JSON-RPC error of its own; the gateway still classifies by the status."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", CONNECT_MODES)
    @pytest.mark.parametrize(
        ("status", "headers", "code", "details"),
        [
            pytest.param(
                403,
                {"WWW-Authenticate": _INSUFFICIENT_SCOPE},
                "upstream_insufficient_scope",
                {"upstream_status": 403, "required_scope": "widgets:write"},
                id="403-insufficient-scope",
            ),
            pytest.param(401, {}, "upstream_unauthorized", {"upstream_status": 401}, id="401"),
            pytest.param(500, {}, "execution_error", {}, id="500"),
            pytest.param(404, {}, "execution_error", {}, id="404"),
        ],
    )
    async def test_a_refused_call(
        self, mode: str, status: int, headers: dict[str, str], code: str, details: dict[str, Any]
    ) -> None:
        upstream = FailMethod(_widgets_upstream().http_app(), "tools/call", status, headers)
        async with serve_http(upstream) as base:
            gateway = GatewayServer({"widgets": f"{base}/mcp"}, upstream_client_mode=mode)
            await gateway.populate()
            envelope = await _execute(gateway, "widgets_echo", {"text": "hi"})

        assert upstream.failed == 1
        assert envelope["code"] == code
        assert envelope["details"] == {"tool": "widgets_echo", "domain": "widgets", **details}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", CONNECT_MODES)
    @pytest.mark.parametrize(
        ("status", "headers", "code", "details"),
        [
            pytest.param(
                403,
                {"WWW-Authenticate": _INSUFFICIENT_SCOPE},
                "upstream_insufficient_scope",
                {"upstream_status": 403, "required_scope": "widgets:write"},
                id="403-insufficient-scope",
            ),
            pytest.param(403, {}, "upstream_unauthorized", {"upstream_status": 403}, id="403"),
            pytest.param(401, {}, "upstream_unauthorized", {"upstream_status": 401}, id="401"),
        ],
    )
    async def test_a_call_refused_with_a_jsonrpc_error_body(
        self, mode: str, status: int, headers: dict[str, str], code: str, details: dict[str, Any]
    ) -> None:
        """The client surfaces a JSON-RPC error body and drops the status; the status and its challenge decide."""
        upstream = FailMethod(
            _widgets_upstream().http_app(), "tools/call", status, headers, jsonrpc_error=(-32001, "Unauthorized")
        )
        async with serve_http(upstream) as base:
            gateway = GatewayServer({"widgets": f"{base}/mcp"}, upstream_client_mode=mode)
            await gateway.populate()
            envelope = await _execute(gateway, "widgets_echo", {"text": "hi"})

        assert upstream.failed == 1
        assert envelope["code"] == code
        assert envelope["details"] == {"tool": "widgets_echo", "domain": "widgets", **details}

    @staticmethod
    def _registration_app(gateway: GatewayServer) -> Starlette:
        return gateway.mcp.http_app(transport="streamable-http")

    async def _register(self, gateway: GatewayServer, url: str) -> Response:
        transport = ASGITransport(app=self._registration_app(gateway), raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            return await http.post(
                "/registry/servers",
                headers={"authorization": f"Bearer {REGISTRATION_TOKEN}"},
                json={"domain": "widgets", "url": url},
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", CONNECT_MODES)
    @pytest.mark.parametrize(
        ("method", "status", "answer", "code"),
        [
            pytest.param("tools/list", 401, 422, "upstream_auth_failed", id="401-auth"),
            pytest.param(None, 404, 503, "upstream_not_ready", id="404-not-mounted"),
            pytest.param("tools/list", 503, 503, "upstream_not_ready", id="503-no-backend"),
            pytest.param("tools/list", 500, 502, "registration_failed", id="500-failure"),
        ],
    )
    async def test_a_refused_registration_probe(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
        method: str | None,
        status: int,
        answer: int,
        code: str | None,
    ) -> None:
        monkeypatch.setenv("GATEWAY_URL_GUARD_ALLOW_PRIVATE_RANGES", "true")
        upstream = FailMethod(_widgets_upstream().http_app(), method, status)
        async with serve_http(upstream) as base:
            gateway = GatewayServer({}, registration_token=REGISTRATION_TOKEN, upstream_client_mode=mode)
            response = await self._register(gateway, f"{base}/mcp")

        assert upstream.failed >= 1
        assert response.status_code == answer
        if code is not None:
            body = response.json()
            assert body["code"] == code
            assert body["upstream_status"] == status

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", CONNECT_MODES)
    @pytest.mark.parametrize("status", [401, 403])
    async def test_a_registration_probe_refused_with_a_jsonrpc_error_body(
        self, monkeypatch: pytest.MonkeyPatch, mode: str, status: int
    ) -> None:
        """An upstream's auth middleware may refuse with a JSON-RPC error body.

        ``-32001`` is also the MCP SDK v2 client's own request-timeout code,
        so classifying the body's code would call a refused credential a
        slow start, and the caller would retry it forever. The status
        decides: the refusal is a caller-fixable 422.
        """
        monkeypatch.setenv("GATEWAY_URL_GUARD_ALLOW_PRIVATE_RANGES", "true")
        upstream = FailMethod(
            _widgets_upstream().http_app(), "tools/list", status, jsonrpc_error=(-32001, "Unauthorized")
        )
        async with serve_http(upstream) as base:
            gateway = GatewayServer({}, registration_token=REGISTRATION_TOKEN, upstream_client_mode=mode)
            response = await self._register(gateway, f"{base}/mcp")

        assert upstream.failed >= 1
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "upstream_auth_failed"
        assert body["upstream_status"] == status

    @pytest.mark.asyncio
    async def test_a_registration_probe_that_times_out_is_not_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The client's own ``-32001`` for a probe that missed its read deadline stays a retryable 503."""
        monkeypatch.setenv("GATEWAY_URL_GUARD_ALLOW_PRIVATE_RANGES", "true")

        def short_deadline_client(target: Any, **kwargs: Any) -> Client:
            return Client(target, timeout=0.2, **kwargs)

        monkeypatch.setattr("fastmcp_gateway.client_manager.Client", short_deadline_client)
        upstream = StallMethod(_widgets_upstream().http_app(), "tools/list")
        async with serve_http(upstream) as base:
            gateway = GatewayServer({}, registration_token=REGISTRATION_TOKEN)
            response = await self._register(gateway, f"{base}/mcp")

        assert upstream.stalled >= 1
        assert response.status_code == 503
        body = response.json()
        assert body["code"] == "upstream_not_ready"
        assert body["upstream_error_code"] == -32001
        assert "upstream_status" not in body


# ---------------------------------------------------------------------------
# Whether an error is the upstream's answer
# ---------------------------------------------------------------------------


class _RejectBeforeTheTool(Middleware):
    """Answer some calls with a JSON-RPC error response, as a validating framework does."""

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]) -> Any:
        text = (context.message.arguments or {}).get("text")
        if text == "closed":
            raise MCPError(code=CONNECTION_CLOSED, message="Connection closed")
        if text == "invalid":
            raise MCPError(code=INVALID_PARAMS, message="text: must not be 'invalid'")
        return await call_next(context)


# Event-stream answers (the server default) and plain JSON answers.
RESPONSE_FORMATS = [pytest.param(False, id="event-stream"), pytest.param(True, id="json")]


class TestErrorProvenance:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", CONNECT_MODES)
    @pytest.mark.parametrize("json_response", RESPONSE_FORMATS)
    @pytest.mark.parametrize(
        ("text", "code", "error"),
        [
            pytest.param("closed", "upstream_error", "Connection closed", id="connection-closed-answer"),
            pytest.param("invalid", "invalid_arguments", "text: must not be 'invalid'", id="invalid-params-answer"),
        ],
    )
    async def test_a_jsonrpc_error_the_upstream_sends_is_its_answer(
        self, mode: str, json_response: bool, text: str, code: str, error: str
    ) -> None:
        """Even ``-32000 "Connection closed"``, the client's own words for a
        dropped session, is the upstream's answer when the upstream sent it."""
        upstream = _widgets_upstream(middleware=[_RejectBeforeTheTool()])
        async with serve_http(upstream.http_app(json_response=json_response)) as base:
            gateway = GatewayServer({"widgets": f"{base}/mcp"}, upstream_client_mode=mode)
            await gateway.populate()
            envelope = await _execute(gateway, "widgets_echo", {"text": text})

        assert envelope["code"] == code
        assert envelope["error"] == error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", CONNECT_MODES)
    @pytest.mark.parametrize("json_response", RESPONSE_FORMATS)
    async def test_a_response_cut_before_its_answer_is_an_execution_error(self, mode: str, json_response: bool) -> None:
        """The client then hands the call an error it built itself (the stream
        ended without a response, or the body did not parse), which must not
        reach the caller as the upstream's words."""
        upstream = CutResponse(_widgets_upstream().http_app(json_response=json_response))
        async with serve_http(upstream) as base:
            gateway = GatewayServer({"widgets": f"{base}/mcp"}, upstream_client_mode=mode)
            await gateway.populate()
            envelope = await _execute(gateway, "widgets_echo", {"text": "hi"})

        assert upstream.cut == 1
        assert envelope["code"] == "execution_error"
        assert envelope["details"] == {"tool": "widgets_echo", "domain": "widgets"}


# ---------------------------------------------------------------------------
# Raw-output trust declared by the upstream
# ---------------------------------------------------------------------------


class TestDeclaredRawOutputTrust:
    @pytest.mark.asyncio
    async def test_an_annotation_key_from_a_handshake_era_upstream(self) -> None:
        """An SDK v1 upstream sends ``x-raw-output-trusted`` in ``annotations``;
        the SDK v2 ``ToolAnnotations`` model drops it, the listing as sent keeps it."""
        app = _widgets_upstream().http_app(json_response=True)
        upstream = HandshakeEraOnly(AddAnnotationKey(app, "x-raw-output-trusted", True))
        async with serve_http(upstream) as base:
            gateway = GatewayServer({"widgets": f"{base}/mcp"})
            await gateway.populate()

        entry = gateway.registry.lookup("widgets_echo")
        assert entry is not None
        assert entry.raw_output_trusted is True

    @pytest.mark.asyncio
    async def test_a_meta_key_from_an_sdk_v2_upstream(self) -> None:
        """An SDK v2 upstream cannot send the annotation key, so ``_meta`` carries it."""
        upstream = FastMCP("widgets")

        @upstream.tool(meta={"x-raw-output-trusted": True})
        def widgets_render(text: str) -> str:
            """Render prompt-like text."""
            return text

        @upstream.tool
        def widgets_echo(text: str) -> str:
            """Echo the text back."""
            return text

        gateway = GatewayServer({"widgets": upstream})  # type: ignore[dict-item]
        await gateway.populate()

        rendered = gateway.registry.lookup("widgets_render")
        echoed = gateway.registry.lookup("widgets_echo")
        assert rendered is not None
        assert echoed is not None
        assert rendered.raw_output_trusted is True
        assert echoed.raw_output_trusted is False

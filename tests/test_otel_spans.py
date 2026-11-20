"""Tests for OpenTelemetry span instrumentation across the gateway."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import Generator

    from fastmcp_gateway.hooks import ExecutionContext

import httpx2
import pytest
from fastmcp import Client, FastMCP
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import fastmcp_gateway.client_manager as cm_mod
import fastmcp_gateway.gateway as gw_mod
import fastmcp_gateway.meta_tools as mt_mod
import fastmcp_gateway.registry as reg_mod
from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.gateway import GatewayServer
from fastmcp_gateway.hooks import HookRunner
from fastmcp_gateway.meta_tools import register_meta_tools
from fastmcp_gateway.registry import ToolRegistry
from tests.conftest import client_session_failure, result_payload, upstream_jsonrpc_error, upstream_status_error


@pytest.fixture(autouse=True)
def _otel_setup() -> Generator[InMemorySpanExporter, None, None]:
    """Inject test tracers into gateway modules via monkey-patch.

    Avoids conflicts with the global OTel provider (which may be
    claimed by logfire or other plugins).
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Patch module-level _tracer with tracers from our test provider
    old_mt = mt_mod._tracer
    old_cm = cm_mod._tracer
    old_reg = reg_mod._tracer
    old_gw = gw_mod._tracer

    mt_mod._tracer = provider.get_tracer("test.meta_tools")
    cm_mod._tracer = provider.get_tracer("test.client_manager")
    reg_mod._tracer = provider.get_tracer("test.registry")
    gw_mod._tracer = provider.get_tracer("test.gateway")

    yield exporter

    mt_mod._tracer = old_mt
    cm_mod._tracer = old_cm
    reg_mod._tracer = old_reg
    gw_mod._tracer = old_gw
    provider.shutdown()


@pytest.fixture
def exporter(_otel_setup: InMemorySpanExporter) -> InMemorySpanExporter:
    """Expose the span exporter for assertions."""
    return _otel_setup


@pytest.fixture
def populated_registry() -> ToolRegistry:
    """A registry with sample tools."""
    registry = ToolRegistry()
    registry.set_domain_description("apollo", "Apollo.io CRM")
    registry.populate_domain(
        "apollo",
        "http://apollo:8080/mcp",
        [
            {"name": "apollo_people_search", "description": "Search people", "inputSchema": {"type": "object"}},
            {"name": "apollo_org_search", "description": "Search orgs", "inputSchema": {"type": "object"}},
        ],
    )
    return registry


@pytest.fixture
def manager(populated_registry: ToolRegistry) -> UpstreamManager:
    """Create an UpstreamManager with mocked clients."""
    with patch("fastmcp_gateway.client_manager.Client"):
        return UpstreamManager(
            {"apollo": "http://apollo:8080/mcp"},
            populated_registry,
        )


@pytest.fixture
def mcp_server(populated_registry: ToolRegistry, manager: UpstreamManager) -> FastMCP:
    """Create a FastMCP server with meta-tools registered."""
    mcp = FastMCP("test-gateway")
    register_meta_tools(mcp, populated_registry, manager)
    return mcp


def _get_spans(exporter: InMemorySpanExporter, name: str) -> list:
    """Filter finished spans by name."""
    return [s for s in exporter.get_finished_spans() if s.name == name]


# ---------------------------------------------------------------------------
# Meta-tool spans
# ---------------------------------------------------------------------------


class TestDiscoverToolsSpans:
    @pytest.mark.asyncio
    async def test_domain_summary_creates_span(self, mcp_server: FastMCP, exporter: InMemorySpanExporter) -> None:
        """Domain summary mode emits a discover_tools span."""
        async with Client(mcp_server) as client:
            await client.call_tool("discover_tools", {})

        spans = _get_spans(exporter, "gateway.discover_tools")
        assert len(spans) == 1
        attrs = dict(spans[0].attributes or {})
        assert "gateway.result_count" in attrs

    @pytest.mark.asyncio
    async def test_search_creates_span_with_query(self, mcp_server: FastMCP, exporter: InMemorySpanExporter) -> None:
        """Search mode records the query attribute on the span."""
        async with Client(mcp_server) as client:
            await client.call_tool("discover_tools", {"query": "search"})

        spans = _get_spans(exporter, "gateway.discover_tools")
        assert len(spans) == 1
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("gateway.query") == "search"
        assert "gateway.result_count" in attrs

    @pytest.mark.asyncio
    async def test_error_creates_span_with_error_code(
        self, mcp_server: FastMCP, exporter: InMemorySpanExporter
    ) -> None:
        """Error paths record the error_code attribute."""
        async with Client(mcp_server) as client:
            await client.call_tool("discover_tools", {"domain": "nonexistent"})

        spans = _get_spans(exporter, "gateway.discover_tools")
        assert len(spans) == 1
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("gateway.error_code") == "domain_not_found"


class TestGetToolSchemaSpans:
    @pytest.mark.asyncio
    async def test_success_creates_span(self, mcp_server: FastMCP, exporter: InMemorySpanExporter) -> None:
        """Successful schema lookup records tool_name and domain."""
        async with Client(mcp_server) as client:
            await client.call_tool("get_tool_schema", {"tool_name": "apollo_people_search"})

        spans = _get_spans(exporter, "gateway.get_tool_schema")
        assert len(spans) == 1
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("gateway.tool_name") == "apollo_people_search"
        assert attrs.get("gateway.domain") == "apollo"

    @pytest.mark.asyncio
    async def test_error_creates_span(self, mcp_server: FastMCP, exporter: InMemorySpanExporter) -> None:
        """Tool not found records error_code on the span."""
        async with Client(mcp_server) as client:
            await client.call_tool("get_tool_schema", {"tool_name": "nonexistent"})

        spans = _get_spans(exporter, "gateway.get_tool_schema")
        assert len(spans) == 1
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("gateway.error_code") == "tool_not_found"


class _ResponseCarryingError(Exception):
    """A non-httpx2 exception exposing an HTTP-shaped ``response``."""

    def __init__(self, response: object) -> None:
        super().__init__("upstream refused the call")
        self.response = response


class _RaisingHeaders:
    """Headers whose ``get`` is callable but raises."""

    def get(self, name: str) -> str:
        raise RuntimeError(f"cannot read header {name}")


class _ResponsePropertyRaisesError(Exception):
    """An exception whose ``response`` property raises something other than ``AttributeError``."""

    @property
    def response(self) -> object:
        raise RuntimeError("response unavailable")


class TestExecuteToolSpans:
    @pytest.mark.asyncio
    async def test_success_creates_span(
        self, mcp_server: FastMCP, manager: UpstreamManager, exporter: InMemorySpanExporter
    ) -> None:
        """Successful execution records tool_name and domain."""
        block = MagicMock()
        block.text = '{"people": []}'
        result = MagicMock()
        result.content = [block]
        result.is_error = False
        manager.execute_tool = AsyncMock(return_value=result)  # type: ignore[method-assign]

        async with Client(mcp_server) as client:
            await client.call_tool("execute_tool", {"tool_name": "apollo_people_search"})

        spans = _get_spans(exporter, "gateway.execute_tool")
        assert len(spans) == 1
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("gateway.tool_name") == "apollo_people_search"
        assert attrs.get("gateway.domain") == "apollo"

    @pytest.mark.asyncio
    async def test_error_records_exception(
        self, mcp_server: FastMCP, manager: UpstreamManager, exporter: InMemorySpanExporter
    ) -> None:
        """Execution failure records error_code and an exception event."""
        manager.execute_tool = AsyncMock(side_effect=ConnectionError("refused"))  # type: ignore[method-assign]

        async with Client(mcp_server) as client:
            await client.call_tool("execute_tool", {"tool_name": "apollo_people_search"})

        spans = _get_spans(exporter, "gateway.execute_tool")
        assert len(spans) == 1
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("gateway.error_code") == "execution_error"
        # Should have recorded the exception
        events = spans[0].events
        assert any(e.name == "exception" for e in events)

    @pytest.mark.asyncio
    async def test_scope_denial_records_its_error_code(
        self, mcp_server: FastMCP, manager: UpstreamManager, exporter: InMemorySpanExporter
    ) -> None:
        """An upstream insufficient_scope refusal records its own error_code and the exception."""
        denial = upstream_status_error(403, 'Bearer error="insufficient_scope", scope="people:read"')
        manager.execute_tool = AsyncMock(side_effect=denial)  # type: ignore[method-assign]

        async with Client(mcp_server) as client:
            await client.call_tool("execute_tool", {"tool_name": "apollo_people_search"})

        spans = _get_spans(exporter, "gateway.execute_tool")
        assert len(spans) == 1
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("gateway.error_code") == "upstream_insufficient_scope"
        assert any(e.name == "exception" for e in spans[0].events)

    @pytest.mark.parametrize(
        ("failure", "code"),
        [
            (upstream_jsonrpc_error(-32602, "limit: Too big"), "invalid_arguments"),
            (upstream_jsonrpc_error(-32603, "backend refused"), "upstream_error"),
            (client_session_failure(-32000, "Connection closed"), "execution_error"),
        ],
        ids=["invalid-params", "other-answer", "session-dropped"],
    )
    @pytest.mark.asyncio
    async def test_a_jsonrpc_error_records_its_error_code_and_the_exception(
        self,
        mcp_server: FastMCP,
        manager: UpstreamManager,
        exporter: InMemorySpanExporter,
        failure: Exception,
        code: str,
    ) -> None:
        manager.execute_tool = AsyncMock(side_effect=failure)  # type: ignore[method-assign]

        async with Client(mcp_server) as client:
            await client.call_tool("execute_tool", {"tool_name": "apollo_people_search"})

        spans = _get_spans(exporter, "gateway.execute_tool")
        assert len(spans) == 1
        assert dict(spans[0].attributes or {}).get("gateway.error_code") == code
        assert any(e.name == "exception" for e in spans[0].events)

    @pytest.mark.parametrize(
        "headers",
        [object(), {"WWW-Authenticate": b'Bearer error="insufficient_scope", scope="people:read"'}],
        ids=["headers-without-get", "bytes-challenge"],
    )
    @pytest.mark.asyncio
    async def test_unreadable_challenge_is_still_classified_recorded_and_hooked(
        self,
        populated_registry: ToolRegistry,
        manager: UpstreamManager,
        exporter: InMemorySpanExporter,
        headers: object,
    ) -> None:
        """The refusal path never raises: a status-bearing exception whose headers
        can't yield a ``str`` challenge is classified by status alone, and the
        exception still reaches the span and the ``on_error`` hook."""
        seen: list[Exception] = []

        class RecordingHook:
            async def on_error(self, context: ExecutionContext, error: Exception) -> None:
                seen.append(error)

        failure = _ResponseCarryingError(SimpleNamespace(status_code=403, headers=headers))
        manager.execute_tool = AsyncMock(side_effect=failure)  # type: ignore[method-assign]
        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, populated_registry, manager, HookRunner([RecordingHook()]))

        async with Client(mcp) as client:
            result = await client.call_tool("execute_tool", {"tool_name": "apollo_people_search"}, raise_on_error=False)

        assert result.is_error is False
        assert result_payload(result)["code"] == "upstream_unauthorized"
        assert seen == [failure]
        spans = _get_spans(exporter, "gateway.execute_tool")
        assert len(spans) == 1
        assert dict(spans[0].attributes or {}).get("gateway.error_code") == "upstream_unauthorized"
        assert any(e.name == "exception" for e in spans[0].events)

    @pytest.mark.parametrize(
        "failure",
        [
            _ResponseCarryingError(SimpleNamespace(status_code=403, headers=_RaisingHeaders())),
            _ResponsePropertyRaisesError("upstream call failed"),
        ],
        ids=["headers-get-raises", "response-property-raises"],
    )
    @pytest.mark.asyncio
    async def test_unreadable_response_falls_back_to_execution_error(
        self,
        populated_registry: ToolRegistry,
        manager: UpstreamManager,
        exporter: InMemorySpanExporter,
        failure: Exception,
    ) -> None:
        """If reading the response itself raises, the call is reported as the
        plain execution error it always was, and the exception still reaches the
        span and the ``on_error`` hook."""
        seen: list[Exception] = []

        class RecordingHook:
            async def on_error(self, context: ExecutionContext, error: Exception) -> None:
                seen.append(error)

        manager.execute_tool = AsyncMock(side_effect=failure)  # type: ignore[method-assign]
        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, populated_registry, manager, HookRunner([RecordingHook()]))

        async with Client(mcp) as client:
            result = await client.call_tool("execute_tool", {"tool_name": "apollo_people_search"}, raise_on_error=False)

        assert result.is_error is False
        payload = result_payload(result)
        assert payload["code"] == "execution_error"
        assert payload["details"] == {"tool": "apollo_people_search", "domain": "apollo"}
        assert seen == [failure]
        spans = _get_spans(exporter, "gateway.execute_tool")
        assert len(spans) == 1
        assert dict(spans[0].attributes or {}).get("gateway.error_code") == "execution_error"
        assert any(e.name == "exception" for e in spans[0].events)


# ---------------------------------------------------------------------------
# Registry spans
# ---------------------------------------------------------------------------


class TestRegistrySpans:
    def test_populate_domain_creates_span(self, exporter: InMemorySpanExporter) -> None:
        """populate_domain emits a span with domain and tool_count."""
        registry = ToolRegistry()
        registry.populate_domain(
            "test", "http://test:8080/mcp", [{"name": "test_tool", "inputSchema": {"type": "object"}}]
        )

        spans = _get_spans(exporter, "gateway.registry.populate_domain")
        assert len(spans) >= 1
        attrs = dict(spans[-1].attributes or {})
        assert attrs.get("gateway.domain") == "test"
        assert attrs.get("gateway.tool_count") == 1

    def test_search_creates_span(self, populated_registry: ToolRegistry, exporter: InMemorySpanExporter) -> None:
        """search emits a span with query and result_count."""
        populated_registry.search("search")

        spans = _get_spans(exporter, "gateway.registry.search")
        assert len(spans) >= 1
        attrs = dict(spans[-1].attributes or {})
        assert attrs.get("gateway.query") == "search"
        assert "gateway.result_count" in attrs


# ---------------------------------------------------------------------------
# Health-route spans
# ---------------------------------------------------------------------------


def _health_transport(gateway: GatewayServer) -> httpx2.ASGITransport:
    """Build an ASGI transport for a gateway's health routes."""
    app = gateway.mcp.http_app(transport="streamable-http")
    return httpx2.ASGITransport(app=app)


class TestHealthRouteSpans:
    """Kubelet-style liveness/readiness probes hit /healthz and /readyz every
    few seconds. Each call becomes its own root trace by default (matching
    prior behaviour); ``trace_health_routes=False`` opts a deployment out of
    that per-probe span volume entirely.
    """

    @pytest.mark.asyncio
    async def test_healthz_emits_span_by_default(self, exporter: InMemorySpanExporter) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            gateway = GatewayServer({})

        transport = _health_transport(gateway)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz")

        assert response.status_code == 200
        spans = _get_spans(exporter, "gateway.healthz")
        assert len(spans) == 1
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("http.route") == "/healthz"

    @pytest.mark.asyncio
    async def test_readyz_emits_span_by_default(self, exporter: InMemorySpanExporter) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            gateway = GatewayServer({})

        transport = _health_transport(gateway)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/readyz")

        assert response.status_code == 200
        spans = _get_spans(exporter, "gateway.readyz")
        assert len(spans) == 1
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("http.route") == "/readyz"
        assert "registry.tool_count" in attrs

    @pytest.mark.asyncio
    async def test_healthz_emits_no_span_when_disabled(self, exporter: InMemorySpanExporter) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            gateway = GatewayServer({}, trace_health_routes=False)

        transport = _health_transport(gateway)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert _get_spans(exporter, "gateway.healthz") == []

    @pytest.mark.asyncio
    async def test_readyz_emits_no_span_when_disabled(self, exporter: InMemorySpanExporter) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            gateway = GatewayServer({}, trace_health_routes=False)

        transport = _health_transport(gateway)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/readyz")

        assert response.status_code == 200
        assert response.json()["status"] == "ready"
        assert _get_spans(exporter, "gateway.readyz") == []


# ---------------------------------------------------------------------------
# Opt-in recording of a tool call's arguments and result
# ---------------------------------------------------------------------------

_CONTENT_KEYS = (
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "gateway.tool_call.arguments_bytes",
    "gateway.tool_call.result_bytes",
    "gateway.tool_call.arguments_truncated",
    "gateway.tool_call.result_truncated",
)


def _content_gateway(*, trace_tool_content: bool, hooks: list | None = None) -> tuple[FastMCP, UpstreamManager]:
    """A gateway with one ``echo`` tool in domain ``svc``, which takes a string ``q``."""
    registry = ToolRegistry()
    registry.populate_domain(
        "svc",
        "http://svc:8080/mcp",
        [
            {
                "name": "echo",
                "description": "Echo",
                "inputSchema": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                    "additionalProperties": False,
                },
            }
        ],
    )
    with patch("fastmcp_gateway.client_manager.Client"):
        manager = UpstreamManager({"svc": "http://svc:8080/mcp"}, registry)
    mcp = FastMCP("test-gateway")
    register_meta_tools(
        mcp, registry, manager, HookRunner(hooks) if hooks else None, trace_tool_content=trace_tool_content
    )
    return mcp, manager


def _upstream_result(text: str, *, is_error: bool = False) -> MagicMock:
    block = MagicMock()
    block.text = text
    result = MagicMock()
    result.content = [block]
    result.is_error = is_error
    result.structured_content = None
    return result


def _execute_attrs(exporter: InMemorySpanExporter) -> dict:
    spans = _get_spans(exporter, "gateway.execute_tool")
    assert len(spans) == 1
    return dict(spans[0].attributes or {})


class TestExecuteToolContentRecording:
    @pytest.mark.asyncio
    async def test_execute_tool_span_records_arguments_and_result_when_enabled(
        self, exporter: InMemorySpanExporter
    ) -> None:
        mcp, manager = _content_gateway(trace_tool_content=True)
        manager.execute_tool = AsyncMock(return_value=_upstream_result("ok"))  # type: ignore[method-assign]

        async with Client(mcp) as client:
            await client.call_tool("execute_tool", {"tool_name": "echo", "arguments": {"q": "héllo"}})

        attrs = _execute_attrs(exporter)
        assert attrs["gen_ai.tool.call.arguments"] == '{"q": "héllo"}'
        assert "ok" in attrs["gen_ai.tool.call.result"]
        assert attrs["gateway.tool_call.arguments_bytes"] == len(attrs["gen_ai.tool.call.arguments"].encode("utf-8"))
        assert attrs["gateway.tool_call.result_bytes"] == len(attrs["gen_ai.tool.call.result"].encode("utf-8"))
        assert "gateway.tool_call.arguments_truncated" not in attrs
        assert "gateway.tool_call.result_truncated" not in attrs

    @pytest.mark.asyncio
    async def test_execute_tool_span_records_nothing_by_default(self, exporter: InMemorySpanExporter) -> None:
        mcp, manager = _content_gateway(trace_tool_content=False)
        manager.execute_tool = AsyncMock(return_value=_upstream_result("ok"))  # type: ignore[method-assign]

        async with Client(mcp) as client:
            await client.call_tool("execute_tool", {"tool_name": "echo", "arguments": {"q": "x"}})

        attrs = _execute_attrs(exporter)
        assert [key for key in _CONTENT_KEYS if key in attrs] == []

    @pytest.mark.parametrize(
        "case",
        ["unknown-tool", "invalid-arguments", "hook-denial", "upstream-is-error", "upstream-raises"],
    )
    @pytest.mark.asyncio
    async def test_every_return_path_records_the_result(self, exporter: InMemorySpanExporter, case: str) -> None:
        from fastmcp_gateway.hooks import ExecutionDenied

        class Denier:
            async def before_execute(self, context: ExecutionContext) -> None:
                raise ExecutionDenied("no", code="forbidden")

        hooks = [Denier()] if case == "hook-denial" else None
        mcp, manager = _content_gateway(trace_tool_content=True, hooks=hooks)
        tool, arguments = "echo", {"q": "x"}
        if case == "unknown-tool":
            tool = "missing"
        elif case == "invalid-arguments":
            arguments = {"nope": 1}
        elif case == "upstream-is-error":
            manager.execute_tool = AsyncMock(return_value=_upstream_result("bad", is_error=True))  # type: ignore[method-assign]
        elif case == "upstream-raises":
            manager.execute_tool = AsyncMock(side_effect=ConnectionError("refused"))  # type: ignore[method-assign]

        async with Client(mcp) as client:
            result = await client.call_tool("execute_tool", {"tool_name": tool, "arguments": arguments})

        returned = result.content[0].text  # type: ignore[union-attr]
        attrs = _execute_attrs(exporter)
        assert attrs["gen_ai.tool.call.arguments"] == json.dumps(arguments, ensure_ascii=False)
        assert attrs["gen_ai.tool.call.result"] == returned
        assert attrs["gateway.tool_call.result_bytes"] == len(returned.encode("utf-8"))

    @pytest.mark.asyncio
    async def test_arguments_are_recorded_as_sent_when_a_hook_rewrites_them(
        self, exporter: InMemorySpanExporter
    ) -> None:
        class Rewriter:
            async def before_execute(self, context: ExecutionContext) -> None:
                context.arguments["q"] = "changed"

        mcp, manager = _content_gateway(trace_tool_content=True, hooks=[Rewriter()])
        manager.execute_tool = AsyncMock(return_value=_upstream_result("ok"))  # type: ignore[method-assign]

        async with Client(mcp) as client:
            await client.call_tool("execute_tool", {"tool_name": "echo", "arguments": {"q": "original"}})

        assert manager.execute_tool.await_args.args[1] == {"q": "changed"}  # type: ignore[attr-defined]
        assert _execute_attrs(exporter)["gen_ai.tool.call.arguments"] == '{"q": "original"}'

    @pytest.mark.asyncio
    async def test_a_value_over_the_bound_is_cut_and_its_size_kept(self, exporter: InMemorySpanExporter) -> None:
        bound = mt_mod._MAX_TRACED_CONTENT_BYTES
        assert bound == 1_048_576
        # The envelope adds bytes around the tool's text, so size the text for a
        # recorded result of exactly bound + 1 bytes.
        mcp, manager = _content_gateway(trace_tool_content=True)
        envelope = len(json.dumps({"tool": "echo", "result": ""}).encode("utf-8"))
        body = "a" * (bound + 1 - envelope)
        manager.execute_tool = AsyncMock(return_value=_upstream_result(body))  # type: ignore[method-assign]

        async with Client(mcp) as client:
            result = await client.call_tool("execute_tool", {"tool_name": "echo", "arguments": {"q": "x"}})

        returned = result.content[0].text  # type: ignore[union-attr]
        assert len(returned.encode("utf-8")) == bound + 1
        attrs = _execute_attrs(exporter)
        recorded = attrs["gen_ai.tool.call.result"]
        assert len(recorded.encode("utf-8")) <= bound
        assert returned.startswith(recorded)
        assert attrs["gateway.tool_call.result_bytes"] == 1_048_577
        assert attrs["gateway.tool_call.result_truncated"] is True
        assert "gateway.tool_call.arguments_truncated" not in attrs

    @pytest.mark.asyncio
    async def test_a_cut_never_splits_a_multi_byte_character(self, exporter: InMemorySpanExporter) -> None:
        bound = mt_mod._MAX_TRACED_CONTENT_BYTES
        mcp, manager = _content_gateway(trace_tool_content=True)
        manager.execute_tool = AsyncMock(return_value=_upstream_result("ok"))  # type: ignore[method-assign]
        # ``{"q": "`` is 7 bytes, so the bound falls in the middle of a 2-byte character.
        query = "\u00e9" * (bound // 2)

        async with Client(mcp) as client:
            await client.call_tool("execute_tool", {"tool_name": "echo", "arguments": {"q": query}})

        attrs = _execute_attrs(exporter)
        recorded = attrs["gen_ai.tool.call.arguments"]
        assert len(recorded.encode("utf-8")) <= bound
        assert recorded.startswith('{"q": "\u00e9')
        assert "\ufffd" not in recorded
        assert attrs["gateway.tool_call.arguments_bytes"] == len(f'{{"q": "{query}"}}'.encode())
        assert attrs["gateway.tool_call.arguments_truncated"] is True

    @pytest.mark.asyncio
    async def test_headers_and_meta_are_never_recorded(self, exporter: InMemorySpanExporter) -> None:
        seen_headers: list[str] = []

        class Passive:
            async def before_execute(self, context: ExecutionContext) -> None:
                seen_headers.append(context.headers["authorization"])

        mcp, manager = _content_gateway(trace_tool_content=True, hooks=[Passive()])
        manager.execute_tool = AsyncMock(return_value=_upstream_result("ok"))  # type: ignore[method-assign]

        with patch(
            "fastmcp_gateway.client_manager.get_http_headers",
            return_value={"authorization": "Bearer s3cr3t-token"},
        ):
            async with Client(mcp) as client:
                await client.call_tool(
                    "execute_tool",
                    {"tool_name": "echo", "arguments": {"q": "x"}},
                    meta={"trace_marker": "meta-value"},
                )

        # The header and the _meta did reach the gateway, so the checks below can fail.
        assert seen_headers == ["Bearer s3cr3t-token"]
        assert manager.execute_tool.await_args.kwargs["request_meta"]["trace_marker"] == "meta-value"  # type: ignore[attr-defined]
        attrs = _execute_attrs(exporter)
        assert attrs["gen_ai.tool.call.arguments"] == '{"q": "x"}'
        for value in attrs.values():
            assert "s3cr3t-token" not in str(value)
            assert "trace_marker" not in str(value)
            assert "meta-value" not in str(value)

    @pytest.mark.asyncio
    async def test_a_result_cut_by_a_hook_output_never_splits_a_multi_byte_character(
        self, exporter: InMemorySpanExporter
    ) -> None:
        bound = mt_mod._MAX_TRACED_CONTENT_BYTES
        big = "\u00e9" * (bound // 2 + 10)

        class Expander:
            async def after_execute(self, context: ExecutionContext, result: str, is_error: bool) -> str:
                return big

        mcp, manager = _content_gateway(trace_tool_content=True, hooks=[Expander()])
        manager.execute_tool = AsyncMock(return_value=_upstream_result("ok"))  # type: ignore[method-assign]

        async with Client(mcp) as client:
            await client.call_tool("execute_tool", {"tool_name": "echo", "arguments": {"q": "x"}})

        attrs = _execute_attrs(exporter)
        recorded = attrs["gen_ai.tool.call.result"]
        assert len(recorded.encode("utf-8")) <= bound
        assert recorded == "\u00e9" * (bound // 2)
        assert attrs["gateway.tool_call.result_bytes"] == len(big.encode("utf-8"))
        assert attrs["gateway.tool_call.result_truncated"] is True

    @pytest.mark.asyncio
    async def test_a_span_that_is_not_recording_does_no_serialization(self) -> None:
        from opentelemetry.trace import NonRecordingSpan

        class Unserializable:
            def __str__(self) -> str:
                raise AssertionError("serialized for a span that is not recording")

        # No SDK provider is wired in: the helper must return before touching the value.
        mt_mod._record_traced_content(NonRecordingSpan(trace.INVALID_SPAN_CONTEXT), "result", Unserializable())  # type: ignore[arg-type]

        mcp, manager = _content_gateway(trace_tool_content=True)
        manager.execute_tool = AsyncMock(return_value=_upstream_result("ok"))  # type: ignore[method-assign]
        mt_mod._tracer = trace.NoOpTracer()
        async with Client(mcp) as client:
            result = await client.call_tool("execute_tool", {"tool_name": "echo", "arguments": {"q": "x"}})
        assert "ok" in result.content[0].text  # type: ignore[union-attr]

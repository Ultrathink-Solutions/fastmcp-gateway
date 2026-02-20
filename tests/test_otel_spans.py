"""Tests for OpenTelemetry span instrumentation across the gateway."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import Generator

import pytest
from fastmcp import Client, FastMCP
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import fastmcp_gateway.client_manager as cm_mod
import fastmcp_gateway.meta_tools as mt_mod
import fastmcp_gateway.registry as reg_mod
from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.meta_tools import register_meta_tools
from fastmcp_gateway.registry import ToolRegistry


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

    mt_mod._tracer = provider.get_tracer("test.meta_tools")
    cm_mod._tracer = provider.get_tracer("test.client_manager")
    reg_mod._tracer = provider.get_tracer("test.registry")

    yield exporter

    mt_mod._tracer = old_mt
    cm_mod._tracer = old_cm
    reg_mod._tracer = old_reg
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
            {"name": "apollo_people_search", "description": "Search people", "inputSchema": {}},
            {"name": "apollo_org_search", "description": "Search orgs", "inputSchema": {}},
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


# ---------------------------------------------------------------------------
# Registry spans
# ---------------------------------------------------------------------------


class TestRegistrySpans:
    def test_populate_domain_creates_span(self, exporter: InMemorySpanExporter) -> None:
        """populate_domain emits a span with domain and tool_count."""
        registry = ToolRegistry()
        registry.populate_domain("test", "http://test:8080/mcp", [{"name": "test_tool", "inputSchema": {}}])

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

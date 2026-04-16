"""Tests for the experimental execute_code meta-tool and CodeModeRunner."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from fastmcp_gateway.code_mode import CodeModeLimits, CodeModeRunner
from fastmcp_gateway.gateway import GatewayServer
from fastmcp_gateway.hooks import ExecutionContext, ExecutionDenied, ListToolsContext

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeCallResult:
    def __init__(
        self,
        *,
        text: str = "",
        structured: Any | None = None,
        is_error: bool = False,
    ) -> None:
        self.content = [_FakeTextBlock(text)] if text else []
        # Attribute names mirror MCP camelCase field shape exactly.
        self.structuredContent = structured
        self.isError = is_error


def _make_gateway(code_mode: bool = True, **kwargs: Any) -> GatewayServer:
    """Construct a gateway with a stubbed Client so no upstream is contacted."""
    with patch("fastmcp_gateway.client_manager.Client"):
        return GatewayServer(
            {"crm": "http://crm:8080/mcp", "analytics": "http://analytics:8080/mcp"},
            code_mode=code_mode,
            **kwargs,
        )


def _seed_registry(gw: GatewayServer) -> None:
    """Populate the registry directly (no upstream HTTP calls)."""
    gw.registry.populate_domain(
        "crm",
        "http://crm:8080/mcp",
        [
            {
                "name": "crm_search",
                "description": "Search the CRM",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            {
                "name": "crm_count",
                "description": "Return a count",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ],
    )
    gw.registry.populate_domain(
        "analytics",
        "http://analytics:8080/mcp",
        [
            {
                "name": "analytics_aggregate",
                "description": "Aggregate a metric",
                "inputSchema": {
                    "type": "object",
                    "properties": {"metric": {"type": "string"}},
                    "required": ["metric"],
                },
            }
        ],
    )


def _stub_execute_tool(
    return_by_name: dict[str, _FakeCallResult],
) -> AsyncMock:
    """Build an AsyncMock that returns the right payload per tool name."""

    async def side_effect(tool_name: str, arguments: dict[str, Any], **_kwargs: Any) -> _FakeCallResult:
        return return_by_name.get(tool_name, _FakeCallResult(text=""))

    return AsyncMock(side_effect=side_effect)


def _runner(gw: GatewayServer, **overrides: Any) -> CodeModeRunner:
    """Expose a CodeModeRunner built against *gw*'s state (used for unit tests)."""
    return CodeModeRunner(
        gw.registry,
        gw.upstream_manager,
        gw.hook_runner,
        **overrides,
    )


# ---------------------------------------------------------------------------
# Registration gating
# ---------------------------------------------------------------------------


class TestCodeModeRegistration:
    @pytest.mark.asyncio
    async def test_disabled_by_default(self) -> None:
        """When code_mode=False, execute_code must NOT be registered."""
        gw = _make_gateway(code_mode=False)
        _seed_registry(gw)
        from fastmcp import Client

        async with Client(gw.mcp) as client:
            tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        assert "execute_code" not in tool_names
        # The other meta-tools stay registered.
        assert {"discover_tools", "get_tool_schema", "execute_tool"} <= tool_names

    @pytest.mark.asyncio
    async def test_registered_when_enabled(self) -> None:
        gw = _make_gateway(code_mode=True)
        _seed_registry(gw)
        from fastmcp import Client

        async with Client(gw.mcp) as client:
            tools = await client.list_tools()
        assert "execute_code" in {t.name for t in tools}


# ---------------------------------------------------------------------------
# Session-level authorizer (the code_mode_authorizer callback)
# ---------------------------------------------------------------------------


class TestAuthorizer:
    @pytest.mark.asyncio
    async def test_authorizer_denies(self) -> None:
        async def deny(_user: Any, _ctx: dict[str, Any]) -> bool:
            return False

        gw = _make_gateway(code_mode_authorizer=deny)
        _seed_registry(gw)
        runner = _runner(gw, authorizer=deny)

        with pytest.raises(ExecutionDenied) as exc_info:
            await runner.run("1 + 1", headers={}, user=None)
        assert exc_info.value.code == "forbidden"

    @pytest.mark.asyncio
    async def test_authorizer_allows_runs_code(self) -> None:
        calls: list[Any] = []

        async def allow(user: Any, ctx: dict[str, Any]) -> bool:
            calls.append((user, ctx))
            return True

        gw = _make_gateway()
        _seed_registry(gw)
        runner = _runner(gw, authorizer=allow)

        result = await runner.run("1 + 1", headers={"x-test": "yes"}, user="alice")
        assert result == "2"
        # Authorizer was consulted with the outer user + headers context.
        assert len(calls) == 1
        assert calls[0][0] == "alice"
        assert calls[0][1]["headers"] == {"x-test": "yes"}


# ---------------------------------------------------------------------------
# Finding 6.1 — callable namespace respects after_list_tools
# ---------------------------------------------------------------------------


class TestNamespaceFiltering:
    @pytest.mark.asyncio
    async def test_callable_namespace_respects_after_list_tools(self) -> None:
        """Tools filtered by after_list_tools must not appear in the sandbox
        namespace — not even as names.

        Prevents information leak where a user can enumerate unauthorized
        tool names by inspecting the sandbox scope.
        """
        hidden_domains = {"analytics"}

        class HideAnalyticsHook:
            async def after_list_tools(self, tools: list[Any], _ctx: ListToolsContext) -> list[Any]:
                return [t for t in tools if t.domain not in hidden_domains]

        gw = _make_gateway(hooks=[HideAnalyticsHook()])
        _seed_registry(gw)
        runner = _runner(gw)

        # Code that references the hidden callable should raise NameError
        # inside the sandbox (the symbol is simply not defined).
        with pytest.raises(Exception) as exc:
            await runner.run(
                "await analytics_aggregate(metric='x')",
                headers={},
                user="alice",
            )
        # pydantic-monty raises MontyRuntimeError wrapping a NameError.
        assert "analytics_aggregate" in str(exc.value) or "NameError" in str(exc.value)


# ---------------------------------------------------------------------------
# Finding 6.2 — outer headers propagate to each nested call
# ---------------------------------------------------------------------------


class TestHeaderPropagation:
    @pytest.mark.asyncio
    async def test_outer_headers_propagate(self) -> None:
        """Each nested call's ExecutionContext carries the OUTER headers.

        Monty's ``run_async`` may execute external functions off the main
        event loop, so reading the ContextVar from inside the wrapper is
        unreliable.  The runner must close over the outer headers at
        construction time.
        """
        captured: list[ExecutionContext] = []

        class CaptureHook:
            async def before_execute(self, ctx: ExecutionContext) -> None:
                captured.append(ctx)

        gw = _make_gateway(hooks=[CaptureHook()])
        _seed_registry(gw)
        runner = _runner(gw)

        stub = _stub_execute_tool({"crm_count": _FakeCallResult(structured={"n": 42})})
        with patch.object(gw.upstream_manager, "execute_tool", stub):
            await runner.run("await crm_count()", headers={"authorization": "Bearer outer"}, user="alice")

        assert len(captured) == 1
        assert captured[0].headers == {"authorization": "Bearer outer"}
        assert captured[0].user == "alice"


# ---------------------------------------------------------------------------
# Per-nested-call hook reuse
# ---------------------------------------------------------------------------


class TestNestedCallHooks:
    @pytest.mark.asyncio
    async def test_per_nested_call_before_execute_fires(self) -> None:
        counter = {"n": 0}

        class CountHook:
            async def before_execute(self, _ctx: ExecutionContext) -> None:
                counter["n"] += 1

        gw = _make_gateway(hooks=[CountHook()])
        _seed_registry(gw)
        runner = _runner(gw)

        stub = _stub_execute_tool(
            {
                "crm_count": _FakeCallResult(structured={"n": 1}),
                "crm_search": _FakeCallResult(structured={"results": []}),
            }
        )
        with patch.object(gw.upstream_manager, "execute_tool", stub):
            await runner.run(
                """
a = await crm_count()
b = await crm_search(query="x")
a
""",
                headers={},
                user="alice",
            )

        # before_execute fires once per nested call.
        assert counter["n"] == 2


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------


class TestResourceLimits:
    @pytest.mark.asyncio
    async def test_duration_limit_enforced(self) -> None:
        """A tight max_duration_secs must abort a long-running sandbox loop."""
        gw = _make_gateway()
        _seed_registry(gw)
        runner = _runner(gw, limits=CodeModeLimits(max_duration_secs=0.2))

        # pydantic-monty raises MontyRuntimeError on limit trips; importing it
        # would couple the test to a private module path, so we assert the
        # concrete fact we care about: *some* exception propagates out.
        with pytest.raises(Exception):  # noqa: B017 — see comment above
            await runner.run(
                """
i = 0
while i < 10**9:
    i = i + 1
i
""",
                headers={},
                user="alice",
            )

    @pytest.mark.asyncio
    async def test_nested_call_counter_enforced(self) -> None:
        """max_nested_calls trips after the Nth external-function invocation."""
        gw = _make_gateway()
        _seed_registry(gw)
        runner = _runner(gw, limits=CodeModeLimits(max_nested_calls=3))

        stub = _stub_execute_tool({"crm_count": _FakeCallResult(structured={"n": 1})})
        with patch.object(gw.upstream_manager, "execute_tool", stub), pytest.raises(Exception) as exc:
            await runner.run(
                """
for _ in range(5):
    await crm_count()
""",
                headers={},
                user="alice",
            )
        assert "nested_call_limit" in str(exc.value) or "max_nested_calls" in str(exc.value)


# ---------------------------------------------------------------------------
# Finding 6.3 — audit logging (hash+metadata default, verbatim opt-in)
# ---------------------------------------------------------------------------


class TestAudit:
    @pytest.mark.asyncio
    async def test_audit_hash_default(self, caplog: pytest.LogCaptureFixture) -> None:
        gw = _make_gateway()
        _seed_registry(gw)
        runner = _runner(gw)

        with caplog.at_level(logging.INFO, logger="fastmcp_gateway.code_mode"):
            await runner.run("41 + 1", headers={}, user="alice")

        # At least one INFO record with structured audit payload.
        audits = [r for r in caplog.records if r.message == "code_mode.invoked"]
        assert audits
        payload = audits[0].audit  # type: ignore[attr-defined]
        assert "code_sha256" in payload
        assert len(payload["code_sha256"]) == 64
        # Verbatim code body is NOT in this record.
        assert "41 + 1" not in caplog.text

    @pytest.mark.asyncio
    async def test_audit_verbatim_opt_in(self, caplog: pytest.LogCaptureFixture) -> None:
        gw = _make_gateway()
        _seed_registry(gw)
        runner = _runner(gw, audit_verbatim=True)

        with caplog.at_level(logging.DEBUG, logger="fastmcp_gateway.code_mode"):
            await runner.run("'hello world'", headers={}, user="alice")

        debug_msgs = [r for r in caplog.records if r.levelname == "DEBUG"]
        # With verbatim on, the raw code appears in the debug-level extras.
        assert any("hello world" in str(getattr(r, "code", "")) for r in debug_msgs)


# ---------------------------------------------------------------------------
# Structured content + asyncio.gather (core ergonomic guarantees)
# ---------------------------------------------------------------------------


class TestSandboxErgonomics:
    @pytest.mark.asyncio
    async def test_structured_content_roundtrips_as_dict(self) -> None:
        """Structured JSON from an upstream is indexable as a Python dict."""
        gw = _make_gateway()
        _seed_registry(gw)
        runner = _runner(gw)

        stub = _stub_execute_tool({"crm_search": _FakeCallResult(structured={"people": [{"email": "a@b.com"}]})})
        with patch.object(gw.upstream_manager, "execute_tool", stub):
            out = await runner.run(
                'result = await crm_search(query="x"); result["people"][0]["email"]',
                headers={},
                user="alice",
            )
        assert out == "a@b.com"

    @pytest.mark.asyncio
    async def test_asyncio_gather_inside_sandbox(self) -> None:
        gw = _make_gateway()
        _seed_registry(gw)
        runner = _runner(gw)

        stub = _stub_execute_tool({"crm_count": _FakeCallResult(structured={"n": 7})})
        with patch.object(gw.upstream_manager, "execute_tool", stub):
            out = await runner.run(
                """
import asyncio
results = await asyncio.gather(crm_count(), crm_count(), crm_count())
len(results)
""",
                headers={},
                user="alice",
            )
        assert out == "3"
        assert stub.await_count == 3

"""Integration tests: hooks wired through the full gateway execute_tool pipeline."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.hooks import ExecutionContext, ExecutionDenied, HookRunner
from fastmcp_gateway.meta_tools import register_meta_tools
from fastmcp_gateway.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Mock upstream MCP server
# ---------------------------------------------------------------------------


def _create_echo_server() -> FastMCP:
    """A simple upstream that echoes arguments back."""
    mcp = FastMCP("echo-upstream")

    @mcp.tool()
    def echo_ping(message: str = "pong") -> str:
        """Echo a message back."""
        return json.dumps({"echo": message})

    @mcp.tool()
    def echo_fail() -> str:
        """A tool that always fails."""
        raise RuntimeError("upstream boom")

    return mcp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def echo_server() -> FastMCP:
    return _create_echo_server()


@pytest.fixture
async def registry_and_manager(echo_server: FastMCP) -> tuple[ToolRegistry, UpstreamManager]:
    """Populated registry + manager wired to echo server."""
    registry = ToolRegistry()
    manager = UpstreamManager(
        {"echo": echo_server},  # type: ignore[dict-item]
        registry,
    )
    await manager.populate_all()
    return registry, manager


async def _call_tool(mcp: FastMCP, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a tool on the gateway and return parsed JSON."""
    async with Client(mcp) as client:
        result = await client.call_tool(name, args or {})
    text = str(result.data) if result.data is not None else result.content[0].text  # type: ignore[union-attr]
    return json.loads(text)


# ---------------------------------------------------------------------------
# Test: hook blocks execution
# ---------------------------------------------------------------------------


class TestHookBlocksExecution:
    @pytest.mark.asyncio
    async def test_denied_returns_error_response(
        self, registry_and_manager: tuple[ToolRegistry, UpstreamManager]
    ) -> None:
        registry, manager = registry_and_manager

        class DenyAllHook:
            async def before_execute(self, context: ExecutionContext) -> None:
                raise ExecutionDenied("You do not have permission", code="forbidden")

        hook_runner = HookRunner([DenyAllHook()])
        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, registry, manager, hook_runner)

        result = await _call_tool(
            mcp,
            "execute_tool",
            {"tool_name": "echo_ping", "arguments": {"message": "hello"}},
        )

        assert result["code"] == "forbidden"
        assert "You do not have permission" in result["error"]

    @pytest.mark.asyncio
    async def test_denied_with_custom_code(self, registry_and_manager: tuple[ToolRegistry, UpstreamManager]) -> None:
        registry, manager = registry_and_manager

        class RateLimitHook:
            async def before_execute(self, context: ExecutionContext) -> None:
                raise ExecutionDenied("Too many requests", code="rate_limited")

        hook_runner = HookRunner([RateLimitHook()])
        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, registry, manager, hook_runner)

        result = await _call_tool(
            mcp,
            "execute_tool",
            {"tool_name": "echo_ping", "arguments": {"message": "hello"}},
        )

        assert result["code"] == "rate_limited"
        assert "Too many requests" in result["error"]


# ---------------------------------------------------------------------------
# Test: hook transforms result
# ---------------------------------------------------------------------------


class TestHookTransformsResult:
    @pytest.mark.asyncio
    async def test_after_execute_modifies_result(
        self, registry_and_manager: tuple[ToolRegistry, UpstreamManager]
    ) -> None:
        registry, manager = registry_and_manager

        class RedactHook:
            async def after_execute(self, context: ExecutionContext, result: str, is_error: bool) -> str:
                data = json.loads(result)
                data["redacted"] = True
                return json.dumps(data)

        hook_runner = HookRunner([RedactHook()])
        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, registry, manager, hook_runner)

        result = await _call_tool(
            mcp,
            "execute_tool",
            {"tool_name": "echo_ping", "arguments": {"message": "secret"}},
        )

        assert result["redacted"] is True
        assert result["tool"] == "echo_ping"


# ---------------------------------------------------------------------------
# Test: hook adds extra headers
# ---------------------------------------------------------------------------


class TestHookAddsExtraHeaders:
    @pytest.mark.asyncio
    async def test_extra_headers_set_via_hook(self, registry_and_manager: tuple[ToolRegistry, UpstreamManager]) -> None:
        """Verify that extra_headers set by hooks are available in context.

        We verify the hook's mutation of ctx.extra_headers actually flows
        through the pipeline by confirming the after_execute hook can
        see the headers that before_execute set.
        """
        registry, manager = registry_and_manager
        captured_headers: dict[str, str] = {}

        class HeaderHook:
            async def before_execute(self, context: ExecutionContext) -> None:
                context.extra_headers["X-User-Subject"] = "test-user@example.com"
                context.extra_headers["X-Tenant-Id"] = "tenant-123"

            async def after_execute(self, context: ExecutionContext, result: str, is_error: bool) -> str:
                captured_headers.update(context.extra_headers)
                return result

        hook_runner = HookRunner([HeaderHook()])
        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, registry, manager, hook_runner)

        result = await _call_tool(
            mcp,
            "execute_tool",
            {"tool_name": "echo_ping", "arguments": {"message": "hello"}},
        )

        assert result["tool"] == "echo_ping"
        assert captured_headers["X-User-Subject"] == "test-user@example.com"
        assert captured_headers["X-Tenant-Id"] == "tenant-123"


# ---------------------------------------------------------------------------
# Test: multiple hooks in chain
# ---------------------------------------------------------------------------


class TestMultipleHooksChain:
    @pytest.mark.asyncio
    async def test_hooks_execute_in_order(self, registry_and_manager: tuple[ToolRegistry, UpstreamManager]) -> None:
        registry, manager = registry_and_manager
        call_order: list[str] = []

        class HookA:
            async def on_authenticate(self, headers: dict[str, str]) -> Any | None:
                call_order.append("A:auth")
                return {"sub": "user-a"}

            async def before_execute(self, context: ExecutionContext) -> None:
                call_order.append("A:before")

            async def after_execute(self, context: ExecutionContext, result: str, is_error: bool) -> str:
                call_order.append("A:after")
                return result

        class HookB:
            async def on_authenticate(self, headers: dict[str, str]) -> Any | None:
                call_order.append("B:auth")
                return None  # Does not overwrite user from A

            async def before_execute(self, context: ExecutionContext) -> None:
                call_order.append("B:before")

            async def after_execute(self, context: ExecutionContext, result: str, is_error: bool) -> str:
                call_order.append("B:after")
                data = json.loads(result)
                data["hook_chain"] = True
                return json.dumps(data)

        hook_runner = HookRunner([HookA(), HookB()])
        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, registry, manager, hook_runner)

        result = await _call_tool(
            mcp,
            "execute_tool",
            {"tool_name": "echo_ping", "arguments": {"message": "chain"}},
        )

        assert result["hook_chain"] is True
        assert call_order == ["A:auth", "B:auth", "A:before", "B:before", "A:after", "B:after"]

    @pytest.mark.asyncio
    async def test_authenticate_last_non_none_wins(
        self, registry_and_manager: tuple[ToolRegistry, UpstreamManager]
    ) -> None:
        registry, manager = registry_and_manager
        captured_users: list[Any] = []

        class HookA:
            async def on_authenticate(self, headers: dict[str, str]) -> Any | None:
                return {"sub": "user-a"}

        class HookB:
            async def on_authenticate(self, headers: dict[str, str]) -> Any | None:
                return {"sub": "user-b"}

        class VerifyHook:
            async def before_execute(self, context: ExecutionContext) -> None:
                captured_users.append(context.user)

        hook_runner = HookRunner([HookA(), HookB(), VerifyHook()])
        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, registry, manager, hook_runner)

        await _call_tool(
            mcp,
            "execute_tool",
            {"tool_name": "echo_ping", "arguments": {"message": "auth-test"}},
        )

        assert captured_users == [{"sub": "user-b"}]

    @pytest.mark.asyncio
    async def test_on_error_called_on_exception(
        self, registry_and_manager: tuple[ToolRegistry, UpstreamManager]
    ) -> None:
        """Verify on_error hooks are called when upstream execution raises.

        Note: FastMCP catches tool exceptions and returns is_error=True,
        so we mock execute_tool to simulate a real connection-level failure
        that propagates as an exception (e.g. upstream unreachable).
        """
        registry, manager = registry_and_manager
        captured_errors: list[str] = []

        class ErrorCaptureHook:
            async def on_error(self, context: ExecutionContext, error: Exception) -> None:
                captured_errors.append(str(error))

        hook_runner = HookRunner([ErrorCaptureHook()])
        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, registry, manager, hook_runner)

        # Mock execute_tool to raise (simulates connection failure)
        from unittest.mock import AsyncMock

        manager.execute_tool = AsyncMock(side_effect=ConnectionError("connection refused"))  # type: ignore[method-assign]

        result = await _call_tool(
            mcp,
            "execute_tool",
            {"tool_name": "echo_ping", "arguments": {"message": "test"}},
        )

        # Should be an error response, not a crash
        assert result["code"] == "execution_error"
        # on_error should have been called
        assert len(captured_errors) == 1
        assert "connection refused" in captured_errors[0]


# ---------------------------------------------------------------------------
# Test: no hooks = zero overhead
# ---------------------------------------------------------------------------


class TestNoHooksPassthrough:
    @pytest.mark.asyncio
    async def test_no_hooks_works_normally(self, registry_and_manager: tuple[ToolRegistry, UpstreamManager]) -> None:
        """With no hooks, execute_tool behaves identically to pre-hook code."""
        registry, manager = registry_and_manager
        hook_runner = HookRunner()  # Empty
        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, registry, manager, hook_runner)

        result = await _call_tool(
            mcp,
            "execute_tool",
            {"tool_name": "echo_ping", "arguments": {"message": "no hooks"}},
        )

        assert result["tool"] == "echo_ping"
        inner = json.loads(result["result"])
        assert inner["echo"] == "no hooks"


# ---------------------------------------------------------------------------
# Test: discover/schema tools unaffected by hooks
# ---------------------------------------------------------------------------


class TestNonExecuteToolsUnaffected:
    @pytest.mark.asyncio
    async def test_discover_tools_not_hooked(self, registry_and_manager: tuple[ToolRegistry, UpstreamManager]) -> None:
        """discover_tools should work even when hooks deny everything."""
        registry, manager = registry_and_manager

        class DenyAllHook:
            async def before_execute(self, context: ExecutionContext) -> None:
                raise ExecutionDenied("blocked")

        hook_runner = HookRunner([DenyAllHook()])
        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, registry, manager, hook_runner)

        result = await _call_tool(mcp, "discover_tools")
        assert "domains" in result
        assert result["total_tools"] > 0

    @pytest.mark.asyncio
    async def test_get_tool_schema_not_hooked(self, registry_and_manager: tuple[ToolRegistry, UpstreamManager]) -> None:
        """get_tool_schema should work even when hooks deny everything."""
        registry, manager = registry_and_manager

        class DenyAllHook:
            async def before_execute(self, context: ExecutionContext) -> None:
                raise ExecutionDenied("blocked")

        hook_runner = HookRunner([DenyAllHook()])
        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, registry, manager, hook_runner)

        result = await _call_tool(mcp, "get_tool_schema", {"tool_name": "echo_ping"})
        assert result["name"] == "echo_ping"
        assert "parameters" in result

"""Tests for the hooks module: HookRunner lifecycle, ExecutionContext, ExecutionDenied."""

from __future__ import annotations

from typing import Any

import pytest

from fastmcp_gateway.hooks import ExecutionContext, ExecutionDenied, Hook, HookRunner, ListToolsContext
from fastmcp_gateway.registry import ToolEntry


def _make_tool_entry(name: str = "test_tool") -> ToolEntry:
    """Create a minimal ToolEntry for testing."""
    return ToolEntry(
        name=name,
        domain="test",
        group="general",
        description="A test tool",
        input_schema={"type": "object", "properties": {}},
        upstream_url="http://test:8080/mcp",
    )


def _make_context(**overrides: Any) -> ExecutionContext:
    """Create an ExecutionContext with sensible defaults."""
    defaults: dict[str, Any] = {
        "tool": _make_tool_entry(),
        "arguments": {"key": "value"},
        "headers": {"authorization": "Bearer test-token"},
    }
    defaults.update(overrides)
    return ExecutionContext(**defaults)


# ---------------------------------------------------------------------------
# ExecutionContext
# ---------------------------------------------------------------------------


class TestExecutionContext:
    def test_defaults(self) -> None:
        ctx = _make_context()
        assert ctx.user is None
        assert ctx.extra_headers == {}
        assert ctx.metadata == {}

    def test_arguments_mutable(self) -> None:
        ctx = _make_context()
        ctx.arguments["new_key"] = "new_value"
        assert ctx.arguments["new_key"] == "new_value"

    def test_extra_headers_mutable(self) -> None:
        ctx = _make_context()
        ctx.extra_headers["X-Custom"] = "header-value"
        assert ctx.extra_headers["X-Custom"] == "header-value"

    def test_metadata_mutable(self) -> None:
        ctx = _make_context()
        ctx.metadata["request_id"] = "abc123"
        assert ctx.metadata["request_id"] == "abc123"

    def test_user_settable(self) -> None:
        ctx = _make_context()
        ctx.user = {"sub": "user@example.com"}
        assert ctx.user["sub"] == "user@example.com"


# ---------------------------------------------------------------------------
# ExecutionDenied
# ---------------------------------------------------------------------------


class TestExecutionDenied:
    def test_default_code(self) -> None:
        exc = ExecutionDenied("not allowed")
        assert exc.message == "not allowed"
        assert exc.code == "forbidden"
        assert str(exc) == "not allowed"

    def test_custom_code(self) -> None:
        exc = ExecutionDenied("rate limited", code="rate_limited")
        assert exc.code == "rate_limited"

    def test_is_exception(self) -> None:
        exc = ExecutionDenied("denied")
        assert isinstance(exc, Exception)


# ---------------------------------------------------------------------------
# Hook Protocol
# ---------------------------------------------------------------------------


class TestHookProtocol:
    def test_runtime_checkable(self) -> None:
        """An object with the right methods should satisfy isinstance(Hook)."""

        class FullHook:
            async def on_authenticate(self, headers: dict[str, str]) -> Any | None:
                return None

            async def before_execute(self, context: ExecutionContext) -> None:
                pass

            async def after_execute(self, context: ExecutionContext, result: str, is_error: bool) -> str:
                return result

            async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
                return tools

            async def on_error(self, context: ExecutionContext, error: Exception) -> None:
                pass

        assert isinstance(FullHook(), Hook)

    def test_partial_hook_does_not_match_protocol(self) -> None:
        """A hook with only some methods does NOT match the full protocol."""

        class PartialHook:
            async def before_execute(self, context: ExecutionContext) -> None:
                pass

        # Protocol requires all methods for isinstance to be True
        assert not isinstance(PartialHook(), Hook)


# ---------------------------------------------------------------------------
# HookRunner
# ---------------------------------------------------------------------------


class TestHookRunnerInit:
    def test_empty(self) -> None:
        runner = HookRunner()
        assert not runner.has_hooks

    def test_with_hooks(self) -> None:
        class MyHook:
            pass

        runner = HookRunner([MyHook()])
        assert runner.has_hooks

    def test_none_hooks(self) -> None:
        runner = HookRunner(None)
        assert not runner.has_hooks

    def test_add(self) -> None:
        runner = HookRunner()
        assert not runner.has_hooks

        class MyHook:
            pass

        runner.add(MyHook())
        assert runner.has_hooks


# ---------------------------------------------------------------------------
# run_authenticate
# ---------------------------------------------------------------------------


class TestRunAuthenticate:
    @pytest.mark.asyncio
    async def test_no_hooks_returns_none(self) -> None:
        runner = HookRunner()
        result = await runner.run_authenticate({"authorization": "Bearer x"})
        assert result is None

    @pytest.mark.asyncio
    async def test_single_hook_returns_user(self) -> None:
        class AuthHook:
            async def on_authenticate(self, headers: dict[str, str]) -> Any | None:
                return {"sub": "user@test.com"}

        runner = HookRunner([AuthHook()])
        result = await runner.run_authenticate({"authorization": "Bearer x"})
        assert result == {"sub": "user@test.com"}

    @pytest.mark.asyncio
    async def test_last_non_none_wins(self) -> None:
        class HookA:
            async def on_authenticate(self, headers: dict[str, str]) -> Any | None:
                return {"sub": "user-a"}

        class HookB:
            async def on_authenticate(self, headers: dict[str, str]) -> Any | None:
                return {"sub": "user-b"}

        runner = HookRunner([HookA(), HookB()])
        result = await runner.run_authenticate({})
        assert result == {"sub": "user-b"}

    @pytest.mark.asyncio
    async def test_none_results_skipped(self) -> None:
        class HookA:
            async def on_authenticate(self, headers: dict[str, str]) -> Any | None:
                return {"sub": "user-a"}

        class HookB:
            async def on_authenticate(self, headers: dict[str, str]) -> Any | None:
                return None  # Should not overwrite user-a

        runner = HookRunner([HookA(), HookB()])
        result = await runner.run_authenticate({})
        assert result == {"sub": "user-a"}

    @pytest.mark.asyncio
    async def test_hook_without_on_authenticate_skipped(self) -> None:
        class NoAuthHook:
            async def before_execute(self, context: ExecutionContext) -> None:
                pass

        runner = HookRunner([NoAuthHook()])
        result = await runner.run_authenticate({})
        assert result is None


# ---------------------------------------------------------------------------
# run_before_execute
# ---------------------------------------------------------------------------


class TestRunBeforeExecute:
    @pytest.mark.asyncio
    async def test_no_hooks(self) -> None:
        runner = HookRunner()
        ctx = _make_context()
        await runner.run_before_execute(ctx)  # Should not raise

    @pytest.mark.asyncio
    async def test_mutates_context(self) -> None:
        class MutatingHook:
            async def before_execute(self, context: ExecutionContext) -> None:
                context.arguments["injected"] = True
                context.extra_headers["X-Injected"] = "true"

        runner = HookRunner([MutatingHook()])
        ctx = _make_context()
        await runner.run_before_execute(ctx)
        assert ctx.arguments["injected"] is True
        assert ctx.extra_headers["X-Injected"] == "true"

    @pytest.mark.asyncio
    async def test_raises_execution_denied(self) -> None:
        class DenyHook:
            async def before_execute(self, context: ExecutionContext) -> None:
                raise ExecutionDenied("Access denied for this tool")

        runner = HookRunner([DenyHook()])
        ctx = _make_context()
        with pytest.raises(ExecutionDenied) as exc_info:
            await runner.run_before_execute(ctx)
        assert exc_info.value.message == "Access denied for this tool"
        assert exc_info.value.code == "forbidden"

    @pytest.mark.asyncio
    async def test_denied_stops_chain(self) -> None:
        call_order: list[str] = []

        class HookA:
            async def before_execute(self, context: ExecutionContext) -> None:
                call_order.append("A")
                raise ExecutionDenied("blocked")

        class HookB:
            async def before_execute(self, context: ExecutionContext) -> None:
                call_order.append("B")

        runner = HookRunner([HookA(), HookB()])
        ctx = _make_context()
        with pytest.raises(ExecutionDenied):
            await runner.run_before_execute(ctx)
        assert call_order == ["A"]  # B was never called

    @pytest.mark.asyncio
    async def test_hook_without_before_execute_skipped(self) -> None:
        class NoBeforeHook:
            async def on_authenticate(self, headers: dict[str, str]) -> Any | None:
                return None

        runner = HookRunner([NoBeforeHook()])
        ctx = _make_context()
        await runner.run_before_execute(ctx)  # Should not raise


# ---------------------------------------------------------------------------
# run_after_execute
# ---------------------------------------------------------------------------


class TestRunAfterExecute:
    @pytest.mark.asyncio
    async def test_no_hooks_returns_original(self) -> None:
        runner = HookRunner()
        ctx = _make_context()
        result = await runner.run_after_execute(ctx, "original", False)
        assert result == "original"

    @pytest.mark.asyncio
    async def test_pipeline_transforms(self) -> None:
        class UpperHook:
            async def after_execute(self, context: ExecutionContext, result: str, is_error: bool) -> str:
                return result.upper()

        class WrapHook:
            async def after_execute(self, context: ExecutionContext, result: str, is_error: bool) -> str:
                return f"[{result}]"

        runner = HookRunner([UpperHook(), WrapHook()])
        ctx = _make_context()
        result = await runner.run_after_execute(ctx, "hello", False)
        assert result == "[HELLO]"

    @pytest.mark.asyncio
    async def test_receives_is_error_flag(self) -> None:
        captured: list[bool] = []

        class CaptureHook:
            async def after_execute(self, context: ExecutionContext, result: str, is_error: bool) -> str:
                captured.append(is_error)
                return result

        runner = HookRunner([CaptureHook()])
        ctx = _make_context()
        await runner.run_after_execute(ctx, "result", True)
        assert captured == [True]

    @pytest.mark.asyncio
    async def test_hook_without_after_execute_skipped(self) -> None:
        class NoAfterHook:
            async def before_execute(self, context: ExecutionContext) -> None:
                pass

        runner = HookRunner([NoAfterHook()])
        ctx = _make_context()
        result = await runner.run_after_execute(ctx, "original", False)
        assert result == "original"


# ---------------------------------------------------------------------------
# run_on_error
# ---------------------------------------------------------------------------


class TestRunOnError:
    @pytest.mark.asyncio
    async def test_no_hooks(self) -> None:
        runner = HookRunner()
        ctx = _make_context()
        await runner.run_on_error(ctx, RuntimeError("boom"))  # Should not raise

    @pytest.mark.asyncio
    async def test_receives_error(self) -> None:
        captured: list[Exception] = []

        class ErrorHook:
            async def on_error(self, context: ExecutionContext, error: Exception) -> None:
                captured.append(error)

        error = RuntimeError("test error")
        runner = HookRunner([ErrorHook()])
        ctx = _make_context()
        await runner.run_on_error(ctx, error)
        assert captured == [error]

    @pytest.mark.asyncio
    async def test_fault_tolerant(self) -> None:
        """Exceptions in on_error hooks are suppressed."""
        call_order: list[str] = []

        class CrashHook:
            async def on_error(self, context: ExecutionContext, error: Exception) -> None:
                call_order.append("crash")
                raise RuntimeError("hook crashed")

        class SafeHook:
            async def on_error(self, context: ExecutionContext, error: Exception) -> None:
                call_order.append("safe")

        runner = HookRunner([CrashHook(), SafeHook()])
        ctx = _make_context()
        await runner.run_on_error(ctx, RuntimeError("original"))  # Should not raise
        assert call_order == ["crash", "safe"]  # Both were called

    @pytest.mark.asyncio
    async def test_hook_without_on_error_skipped(self) -> None:
        class NoErrorHook:
            async def before_execute(self, context: ExecutionContext) -> None:
                pass

        runner = HookRunner([NoErrorHook()])
        ctx = _make_context()
        await runner.run_on_error(ctx, RuntimeError("error"))  # Should not raise

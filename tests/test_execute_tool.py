"""Tests for the execute_tool meta-tool."""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastmcp import Client, FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.hooks import ExecutionContext, HookRunner
from fastmcp_gateway.meta_tools import register_meta_tools
from fastmcp_gateway.registry import ToolEntry
from tests.conftest import client_session_failure, upstream_jsonrpc_error, upstream_status_error

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastmcp_gateway.registry import ToolRegistry


@pytest.fixture
def manager(populated_registry: ToolRegistry) -> UpstreamManager:
    """An UpstreamManager with mocked Client constructor."""
    with patch("fastmcp_gateway.client_manager.Client"):
        return UpstreamManager(
            {"apollo": "http://apollo:8080/mcp", "hubspot": "http://hubspot:8080/mcp"},
            populated_registry,
        )


@pytest.fixture
def mcp_server(populated_registry: ToolRegistry, manager: UpstreamManager) -> FastMCP:
    """A FastMCP server with all 3 meta-tools registered."""
    mcp = FastMCP("test-gateway")
    register_meta_tools(mcp, populated_registry, manager)
    return mcp


def _fake_result(
    text: str,
    *,
    is_error: bool = False,
    structured_content: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a fake CallToolResult with text content.

    ``structured_content`` defaults to ``None`` (explicit, not MagicMock-auto)
    so ``execute_tool``'s passthrough respects the MCP-spec contract that
    ``structuredContent`` is an object-or-absent.
    """
    block = MagicMock()
    block.text = text
    result = MagicMock()
    result.content = [block]
    result.is_error = is_error
    result.structured_content = structured_content
    return result


async def _call_execute(
    mcp: FastMCP,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    meta: dict[str, Any] | None = None,
    exact_meta: bool = False,
) -> dict[str, Any]:
    """Call execute_tool via an in-process client and parse JSON."""
    params: dict[str, Any] = {"tool_name": tool_name}
    if arguments is not None:
        params["arguments"] = arguments
    async with Client(mcp) as client:
        if exact_meta:
            result = await client.session.call_tool(
                "execute_tool",
                params,
                meta=meta,
            )
            text = result.content[0].text  # type: ignore[union-attr]
        else:
            result = await client.call_tool("execute_tool", params, meta=meta)
            if result.data is not None:
                text = str(result.data)
            else:
                content_block = result.content[0]
                text = content_block.text  # type: ignore[union-attr]
    return json.loads(text)


# ---------------------------------------------------------------------------
# Successful execution
# ---------------------------------------------------------------------------


class TestExecuteToolSuccess:
    @pytest.mark.asyncio
    async def test_routes_and_returns_result(self, mcp_server: FastMCP, manager: UpstreamManager) -> None:
        manager.execute_tool = AsyncMock(return_value=_fake_result('{"people": []}'))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["tool"] == "apollo_people_search"
        assert data["result"] == '{"people": []}'
        manager.execute_tool.assert_called_once_with(
            "apollo_people_search",
            {"query": "Jane"},
            request_meta={"progressToken": 1},
        )

    @pytest.mark.asyncio
    async def test_no_arguments_sends_none(self, mcp_server: FastMCP, manager: UpstreamManager) -> None:
        manager.execute_tool = AsyncMock(return_value=_fake_result("ok"))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "hubspot_contacts_search")

        assert data["result"] == "ok"
        manager.execute_tool.assert_called_once_with(
            "hubspot_contacts_search",
            None,
            request_meta={"progressToken": 1},
        )


# ---------------------------------------------------------------------------
# Error: unknown tool
# ---------------------------------------------------------------------------


class TestExecuteToolUnknown:
    @pytest.mark.asyncio
    async def test_unknown_tool_with_suggestions(self, mcp_server: FastMCP) -> None:
        data = await _call_execute(mcp_server, "apollo_search")

        assert data["code"] == "tool_not_found"
        assert "apollo_search" in data["error"]
        assert "Did you mean" in data["error"]

    @pytest.mark.asyncio
    async def test_unknown_tool_no_suggestions(self, mcp_server: FastMCP) -> None:
        data = await _call_execute(mcp_server, "completely_unrelated_xyz_123")

        assert data["code"] == "tool_not_found"
        assert "discover_tools" in data["error"]


# ---------------------------------------------------------------------------
# Error: upstream unreachable
# ---------------------------------------------------------------------------


class TestExecuteToolUpstreamError:
    @pytest.mark.asyncio
    async def test_connectivity_error(self, mcp_server: FastMCP, manager: UpstreamManager) -> None:
        manager.execute_tool = AsyncMock(side_effect=ConnectionError("connection refused"))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == "execution_error"
        assert "failed" in data["error"]
        assert data["details"]["domain"] == "apollo"
        assert data["details"]["tool"] == "apollo_people_search"

    @pytest.mark.asyncio
    async def test_upstream_tool_error(self, mcp_server: FastMCP, manager: UpstreamManager) -> None:
        """Upstream tool returns is_error=True."""
        manager.execute_tool = AsyncMock(  # type: ignore[method-assign]
            return_value=_fake_result("Invalid parameter: limit must be > 0", is_error=True)
        )

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == "upstream_error"
        assert "Invalid parameter" in data["error"]
        assert data["details"]["tool"] == "apollo_people_search"
        assert "result" not in data


# ---------------------------------------------------------------------------
# Error: upstream refused the call
# ---------------------------------------------------------------------------

_SCOPE_CHALLENGE = 'Bearer realm="mcp", error="insufficient_scope", scope="people:read"'


def _wrapped_by_cause(inner: Exception) -> Exception:
    outer = RuntimeError("upstream call failed")
    outer.__cause__ = inner
    return outer


def _wrapped_in_group(inner: Exception) -> Exception:
    return ExceptionGroup("upstream call failed", [inner])


class TestExecuteToolUpstreamRefusal:
    """An upstream MCP server that enforces per-tool scopes refuses a call
    with 401/403, and a 403 carrying an RFC 6750 ``insufficient_scope``
    challenge names the scope the caller lacks. Neither is an outage, so the
    envelope says which one it is instead of a generic execution error."""

    @pytest.mark.asyncio
    async def test_insufficient_scope_challenge_is_a_scope_denial(
        self, mcp_server: FastMCP, manager: UpstreamManager
    ) -> None:
        manager.execute_tool = AsyncMock(side_effect=upstream_status_error(403, _SCOPE_CHALLENGE))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == "upstream_insufficient_scope"
        assert data["details"]["required_scope"] == "people:read"
        assert data["details"]["upstream_status"] == 403
        assert data["details"]["domain"] == "apollo"
        assert data["details"]["tool"] == "apollo_people_search"

    @pytest.mark.asyncio
    async def test_scope_denial_without_a_scope_parameter(self, mcp_server: FastMCP, manager: UpstreamManager) -> None:
        manager.execute_tool = AsyncMock(  # type: ignore[method-assign]
            side_effect=upstream_status_error(403, 'Bearer realm="mcp", error="insufficient_scope"')
        )

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == "upstream_insufficient_scope"
        assert data["details"]["required_scope"] is None
        assert data["details"]["upstream_status"] == 403

    @pytest.mark.parametrize(
        ("status_code", "www_authenticate"),
        [
            (401, None),
            (401, _SCOPE_CHALLENGE),
            (403, None),
            (403, 'Bearer realm="mcp", error="invalid_token"'),
        ],
        ids=["401", "401-with-scope-challenge", "403-without-challenge", "403-invalid-token"],
    )
    @pytest.mark.asyncio
    async def test_other_refusals_are_unauthorized(
        self,
        mcp_server: FastMCP,
        manager: UpstreamManager,
        status_code: int,
        www_authenticate: str | None,
    ) -> None:
        manager.execute_tool = AsyncMock(side_effect=upstream_status_error(status_code, www_authenticate))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == "upstream_unauthorized"
        assert data["details"]["upstream_status"] == status_code
        assert data["details"]["domain"] == "apollo"
        assert data["details"]["tool"] == "apollo_people_search"

    @pytest.mark.parametrize("wrap", [_wrapped_by_cause, _wrapped_in_group], ids=["cause", "exception-group"])
    @pytest.mark.asyncio
    async def test_wrapped_refusal_is_read_from_the_wrapped_response(
        self,
        mcp_server: FastMCP,
        manager: UpstreamManager,
        wrap: Callable[[Exception], Exception],
    ) -> None:
        """Status and challenge both come from the response of the exception
        that carries one, not from the wrapper around it."""
        manager.execute_tool = AsyncMock(side_effect=wrap(upstream_status_error(403, _SCOPE_CHALLENGE)))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == "upstream_insufficient_scope"
        assert data["details"]["required_scope"] == "people:read"
        assert data["details"]["upstream_status"] == 403

    @pytest.mark.parametrize(
        "error",
        [httpx.ConnectError("connection refused"), upstream_status_error(500)],
        ids=["connect-error", "http-500"],
    )
    @pytest.mark.asyncio
    async def test_other_upstream_failures_stay_execution_errors(
        self, mcp_server: FastMCP, manager: UpstreamManager, error: Exception
    ) -> None:
        manager.execute_tool = AsyncMock(side_effect=error)  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == "execution_error"
        assert data["details"] == {"tool": "apollo_people_search", "domain": "apollo"}

    @pytest.mark.parametrize(
        ("refusal", "code"),
        [
            (upstream_status_error(403, _SCOPE_CHALLENGE), "upstream_insufficient_scope"),
            (upstream_status_error(401), "upstream_unauthorized"),
        ],
        ids=["insufficient-scope", "unauthorized"],
    )
    @pytest.mark.asyncio
    async def test_refusals_run_on_error_hooks(
        self,
        populated_registry: ToolRegistry,
        manager: UpstreamManager,
        refusal: Exception,
        code: str,
    ) -> None:
        seen: list[Exception] = []

        class RecordingHook:
            async def on_error(self, context: ExecutionContext, error: Exception) -> None:
                seen.append(error)

        manager.execute_tool = AsyncMock(side_effect=refusal)  # type: ignore[method-assign]
        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, populated_registry, manager, HookRunner([RecordingHook()]))

        data = await _call_execute(mcp, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == code
        assert seen == [refusal]


# ---------------------------------------------------------------------------
# Error: upstream answered with a JSON-RPC error
# ---------------------------------------------------------------------------

_TOO_BIG = "Tool 'people_search' parameter validation failed: limit: Too big: expected number to be <=100."


class TestExecuteToolUpstreamJsonRpcError:
    """An upstream that answers ``tools/call`` with a JSON-RPC error response
    did receive and judge the call, so the caller gets the upstream's own
    words. Some MCP server frameworks report an argument that fails the tool's
    input schema this way (``-32602``) instead of as an ``isError`` result; a
    caller that sees ``invalid_arguments`` plus the signature can correct the
    call. Only a failure where no answer arrived stays ``execution_error``."""

    @pytest.mark.asyncio
    async def test_invalid_params_is_an_argument_error_with_the_upstream_message(
        self, mcp_server: FastMCP, manager: UpstreamManager
    ) -> None:
        manager.execute_tool = AsyncMock(side_effect=upstream_jsonrpc_error(-32602, _TOO_BIG))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == "invalid_arguments"
        assert data["error"] == _TOO_BIG
        assert data["details"]["tool"] == "apollo_people_search"
        assert data["details"]["domain"] == "apollo"
        assert data["details"]["upstream_error_code"] == -32602
        assert data["details"]["signature"].startswith("apollo_people_search(query: str)")

    @pytest.mark.parametrize(
        ("code", "message"),
        [
            (-32603, "Internal error: the backing service rejected the request"),
            (-32601, "Method not found"),
            (-32001, "Resource not available for this caller"),
            (-32000, "Rate limit exceeded for this tenant"),
            (-32000, "Connection closed"),
            (0, "KeyError: 'region'"),
            (0, "Request cancelled"),
        ],
        ids=[
            "internal-error",
            "method-not-found",
            "server-defined",
            "server-sent-32000",
            "server-sent-connection-closed",
            "server-sent-code-0",
            "server-sent-request-cancelled",
        ],
    )
    @pytest.mark.asyncio
    async def test_other_answers_carry_the_upstream_message(
        self, mcp_server: FastMCP, manager: UpstreamManager, code: int, message: str
    ) -> None:
        manager.execute_tool = AsyncMock(side_effect=upstream_jsonrpc_error(code, message))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == "upstream_error"
        assert data["error"] == message
        assert data["details"] == {"tool": "apollo_people_search", "domain": "apollo", "upstream_error_code": code}

    @pytest.mark.parametrize(
        "failure",
        [
            client_session_failure(-32000, "Connection closed"),
            client_session_failure(408, "Timed out while waiting for response to CallToolRequest. Waited 30 seconds."),
            client_session_failure(-32603, "Internal error"),
        ],
        ids=["connection-closed", "read-timeout", "any-code"],
    )
    @pytest.mark.asyncio
    async def test_client_side_session_failures_stay_execution_errors(
        self, mcp_server: FastMCP, manager: UpstreamManager, failure: Exception
    ) -> None:
        """An ``McpError`` that did not arrive from the upstream (the session
        dropped, a deadline passed) says nothing about the call, whatever its code
        or message, and is reported as the outage it is. The same code and message
        sent by the upstream is its answer
        (``test_other_answers_carry_the_upstream_message``)."""
        manager.execute_tool = AsyncMock(side_effect=failure)  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == "execution_error"
        assert data["details"] == {"tool": "apollo_people_search", "domain": "apollo"}

    @pytest.mark.asyncio
    async def test_a_wrapped_jsonrpc_error_is_read_from_the_wrapped_exception(
        self, mcp_server: FastMCP, manager: UpstreamManager
    ) -> None:
        manager.execute_tool = AsyncMock(  # type: ignore[method-assign]
            side_effect=_wrapped_in_group(upstream_jsonrpc_error(-32602, _TOO_BIG))
        )

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == "invalid_arguments"
        assert data["error"] == _TOO_BIG

    @pytest.mark.asyncio
    async def test_an_http_refusal_outranks_a_jsonrpc_error_it_wraps(
        self, mcp_server: FastMCP, manager: UpstreamManager
    ) -> None:
        refusal = upstream_status_error(403, _SCOPE_CHALLENGE)
        refusal.__cause__ = upstream_jsonrpc_error(-32602, _TOO_BIG)
        manager.execute_tool = AsyncMock(side_effect=refusal)  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == "upstream_insufficient_scope"

    @pytest.mark.asyncio
    async def test_an_overlong_upstream_message_is_bounded(self, mcp_server: FastMCP, manager: UpstreamManager) -> None:
        manager.execute_tool = AsyncMock(side_effect=upstream_jsonrpc_error(-32602, "x" * 50_000))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == "invalid_arguments"
        assert len(data["error"]) <= 2_000
        assert data["error"].endswith("…")

    @pytest.mark.asyncio
    async def test_a_jsonrpc_answer_still_runs_on_error_hooks(
        self, populated_registry: ToolRegistry, manager: UpstreamManager
    ) -> None:
        seen: list[Exception] = []

        class RecordingHook:
            async def on_error(self, context: ExecutionContext, error: Exception) -> None:
                seen.append(error)

        failure = upstream_jsonrpc_error(-32602, _TOO_BIG)
        manager.execute_tool = AsyncMock(side_effect=failure)  # type: ignore[method-assign]
        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, populated_registry, manager, HookRunner([RecordingHook()]))

        data = await _call_execute(mcp, "apollo_people_search", {"query": "Jane"})

        assert data["code"] == "invalid_arguments"
        assert seen == [failure]


# ---------------------------------------------------------------------------
# Argument validation: unknown/missing arguments are rejected before dispatch
# ---------------------------------------------------------------------------


class TestExecuteToolArgumentValidation:
    """A call whose arguments don't match the tool's declared schema is
    rejected before it ever reaches the upstream server, with the tool's
    full expected signature in the error -- so a caller that guessed wrong
    gets the correction in one hop instead of a second blind guess."""

    @pytest.mark.asyncio
    async def test_missing_required_argument_is_rejected_before_dispatch(
        self, mcp_server: FastMCP, manager: UpstreamManager
    ) -> None:
        manager.execute_tool = AsyncMock(return_value=_fake_result("should never be called"))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {})

        assert data["code"] == "invalid_arguments"
        assert "query" in data["error"]
        assert data["details"]["signature"] == (
            "apollo_people_search(query: str) -> any\n  Search for people by name, title, company, or other criteria"
        )
        manager.execute_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_argument_is_rejected_before_dispatch(
        self, mcp_server: FastMCP, manager: UpstreamManager
    ) -> None:
        """Isolates the UNKNOWN-argument branch only: the required `query`
        arg IS supplied, alongside one bogus extra key, so `missing` is
        empty and only the `unknown` branch of `_describe_argument_errors`
        can fire. `apollo_people_search`'s fixture schema
        (`required=["query"]`) means a call like `{"name": "Jane"}` (no
        `query` at all) would trip BOTH the unknown AND missing branches at
        once, which couldn't tell "unknown detection is broken" apart from
        "missing detection is broken" from "both work" -- see
        `test_missing_required_argument_is_rejected_before_dispatch` above
        for the separate missing-only case."""
        manager.execute_tool = AsyncMock(return_value=_fake_result("should never be called"))  # type: ignore[method-assign]

        data = await _call_execute(mcp_server, "apollo_people_search", {"query": "Jane", "bogus": "x"})

        assert data["code"] == "invalid_arguments"
        assert "bogus" in data["error"]
        assert "apollo_people_search(query: str)" in data["details"]["signature"]
        manager.execute_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_schema_without_properties_is_not_validated(self, registry: ToolRegistry) -> None:
        """A tool whose schema declares no 'properties' object makes no claim
        about what's valid -- every call passes through unchecked, exactly as
        before this feature existed (matches extract_params' own fallback)."""
        registry.set_domain_description("legacy", "Legacy passthrough tools")
        registry.register_tool(
            ToolEntry(
                name="legacy_passthrough",
                domain="legacy",
                group="misc",
                description="Accepts whatever the caller sends.",
                input_schema={"type": "object"},
                upstream_url="http://legacy-mcp:8080/mcp",
            )
        )
        with patch("fastmcp_gateway.client_manager.Client"):
            manager = UpstreamManager({"legacy": "http://legacy-mcp:8080/mcp"}, registry)
        manager.execute_tool = AsyncMock(return_value=_fake_result("ok"))  # type: ignore[method-assign]
        mcp = FastMCP("test-gateway")
        register_meta_tools(mcp, registry, manager)

        data = await _call_execute(mcp, "legacy_passthrough", {"anything": "goes"})

        assert data["result"] == "ok"
        manager.execute_tool.assert_called_once_with(
            "legacy_passthrough",
            {"anything": "goes"},
            request_meta={"progressToken": 1},
        )


class TestExecuteToolRequestMetadata:
    @pytest.mark.asyncio
    async def test_metadata_is_deep_isolated_from_hooks_and_business_arguments(
        self,
        populated_registry: ToolRegistry,
        manager: UpstreamManager,
    ) -> None:
        original_meta = {
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "io.ult.action_execution.v1": "eyJhbGciOiJFZERTQSJ9.payload.signature",
            "nested": {"items": [{"attempt": 1}]},
        }
        original_arguments = {"query": "Jane"}
        observed: dict[str, Any] = {}

        class MutatingHook:
            async def before_execute(self, ctx: ExecutionContext) -> None:
                observed["view"] = ctx.request_meta
                with pytest.raises(TypeError):
                    ctx.request_meta["traceparent"] = "changed"  # type: ignore[index]
                assert ctx.request_meta is not None
                ctx.request_meta["nested"]["items"].append({"attempt": 99})

        manager.execute_tool = AsyncMock(return_value=_fake_result("ok"))  # type: ignore[method-assign]
        mcp = FastMCP("test-gateway")
        register_meta_tools(
            mcp,
            populated_registry,
            manager,
            HookRunner([MutatingHook()]),
        )

        data = await _call_execute(
            mcp,
            "apollo_people_search",
            original_arguments,
            meta=original_meta,
            exact_meta=True,
        )

        assert data["result"] == "ok"
        assert isinstance(observed["view"], Mapping)
        assert isinstance(observed["view"], MappingProxyType)
        assert original_meta["nested"]["items"] == [{"attempt": 1}]
        assert original_arguments == {"query": "Jane"}
        manager.execute_tool.assert_awaited_once_with(
            "apollo_people_search",
            {"query": "Jane"},
            request_meta=original_meta,
        )

    @pytest.mark.asyncio
    async def test_absent_metadata_remains_none(
        self,
        mcp_server: FastMCP,
        manager: UpstreamManager,
    ) -> None:
        manager.execute_tool = AsyncMock(return_value=_fake_result("ok"))  # type: ignore[method-assign]

        await _call_execute(
            mcp_server,
            "apollo_people_search",
            {"query": "Jane"},
            exact_meta=True,
        )

        manager.execute_tool.assert_awaited_once_with(
            "apollo_people_search",
            {"query": "Jane"},
            request_meta=None,
        )


class TestExecuteCodeRequestMetadata:
    @pytest.mark.asyncio
    async def test_code_mode_receives_exact_request_metadata(
        self,
        populated_registry: ToolRegistry,
        manager: UpstreamManager,
    ) -> None:
        code_mode_runner = MagicMock()
        code_mode_runner.run = AsyncMock(return_value="42")
        mcp = FastMCP("test-gateway")
        register_meta_tools(
            mcp,
            populated_registry,
            manager,
            code_mode_runner=code_mode_runner,
        )
        request_meta = {
            "io.ult.action_execution.v1": "signed-code-mode",
            "nested": {"attempts": [1]},
        }

        async with Client(mcp) as client:
            result = await client.session.call_tool(
                "execute_code",
                {"code": "40 + 2"},
                meta=request_meta,
            )

        assert result.isError is False
        code_mode_runner.run.assert_awaited_once_with(
            "40 + 2",
            headers={},
            user=None,
            request_meta=request_meta,
        )

    @pytest.mark.asyncio
    async def test_code_mode_preserves_absent_request_metadata(
        self,
        populated_registry: ToolRegistry,
        manager: UpstreamManager,
    ) -> None:
        code_mode_runner = MagicMock()
        code_mode_runner.run = AsyncMock(return_value="42")
        mcp = FastMCP("test-gateway")
        register_meta_tools(
            mcp,
            populated_registry,
            manager,
            code_mode_runner=code_mode_runner,
        )

        async with Client(mcp) as client:
            result = await client.session.call_tool(
                "execute_code",
                {"code": "40 + 2"},
                meta=None,
            )

        assert result.isError is False
        code_mode_runner.run.assert_awaited_once_with(
            "40 + 2",
            headers={},
            user=None,
            request_meta=None,
        )

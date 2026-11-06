"""A bare name that more than one domain serves answers ``tool_not_found`` naming each domain's tool.

When two domains register the same tool name, the registry renames each to
``{domain}_{name}``, so the bare name no longer resolves. A caller that uses
it anyway, in ``execute_tool`` without ``domain`` or in ``get_tool_schema``,
learns why and what to call instead: the message names each renamed tool
the caller can see, ``details.candidates`` lists them with their domains,
and ``details.suggestions`` starts with them.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client, FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.hooks import HookRunner, ListToolsContext
from fastmcp_gateway.meta_tools import register_meta_tools
from fastmcp_gateway.registry import ToolEntry, ToolRegistry
from tests.conftest import result_payload

_TOOLS: list[tuple[str, str]] = [
    ("create_upload", "svc"),
    ("create_widget", "svc"),
    ("upload_status", "svc"),
    ("create_upload", "widgets"),
    ("finalize_upload", "widgets"),
    ("list_widgets", "widgets"),
    # Each of these contains the bare name, so fuzzy matching alone ranks
    # both above the renamed tools, and a cap of three drops one arm.
    ("bulk_create_upload", "acme"),
    ("create_upload_session", "acme"),
]

_BOTH_ARMS = [
    {"name": "svc_create_upload", "domain": "svc"},
    {"name": "widgets_create_upload", "domain": "widgets"},
]


def _ok() -> MagicMock:
    """A successful upstream call result whose text is ``ok``."""
    block = MagicMock()
    block.text = "ok"
    result = MagicMock()
    result.content = [block]
    result.is_error = False
    result.structured_content = None
    return result


def _gateway(*hooks: Any, tools: list[tuple[str, str]] = _TOOLS) -> tuple[FastMCP, AsyncMock]:
    """A gateway serving *tools* (name, domain) under *hooks*, and its stubbed upstream dispatch."""
    registry = ToolRegistry()
    for name, domain in tools:
        registry.register_tool(
            ToolEntry(
                name=name,
                domain=domain,
                group="general",
                description=f"{name} from {domain}",
                input_schema={"type": "object", "properties": {}},
                upstream_url=f"http://{domain}:8080/mcp",
            )
        )
    with patch("fastmcp_gateway.client_manager.Client"):
        manager = UpstreamManager({d: f"http://{d}:8080/mcp" for _, d in tools}, registry)
    dispatch = AsyncMock(return_value=_ok())
    manager.execute_tool = dispatch  # type: ignore[method-assign]
    mcp = FastMCP("test-gateway")
    register_meta_tools(mcp, registry, manager, HookRunner(list(hooks) or None))
    return mcp, dispatch


async def _execute(mcp: FastMCP, **params: Any) -> dict[str, Any]:
    """Call ``execute_tool`` with *params* and return its JSON text as a dict."""
    async with Client(mcp) as client:
        result = await client.call_tool("execute_tool", params)
    return json.loads(result.content[0].text)  # type: ignore[union-attr]


async def _schema(mcp: FastMCP, tool_name: str) -> dict[str, Any]:
    """Call ``get_tool_schema`` for *tool_name* and return its payload."""
    async with Client(mcp) as client:
        return result_payload(await client.call_tool("get_tool_schema", {"tool_name": tool_name}))


class _HideDomains:
    """Hides every tool of the given domains, in every listing."""

    def __init__(self, *domains: str) -> None:
        """Hide the tools of each of *domains*."""
        self.domains = set(domains)

    async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
        """Drop every tool of a hidden domain, whatever the listing's context."""
        return [t for t in tools if t.domain not in self.domains]


class _HideDomainWhenListed:
    """Hides a domain's tools only from a lookup scoped to that domain."""

    def __init__(self, domain: str) -> None:
        """Hide *domain*'s tools from lookups scoped to *domain*."""
        self.domain = domain

    async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
        """Drop the domain's tools only when the listing is scoped to that domain."""
        return [t for t in tools if not (context.domain == self.domain and t.domain == self.domain)]


class _CountingAuth:
    """Counts how many times the caller is authenticated."""

    def __init__(self) -> None:
        """Start the count at zero."""
        self.calls = 0

    async def on_authenticate(self, headers: dict[str, str]) -> Any:
        """Count this authentication and return a fixed user."""
        self.calls += 1
        return {"id": "u"}


class TestCollidedBareName:
    @pytest.mark.asyncio
    async def test_execute_tool_names_each_domains_tool(self) -> None:
        """``execute_tool`` without ``domain`` names both renamed tools and offers ``domain`` as a way out."""
        mcp, dispatch = _gateway()

        data = await _execute(mcp, tool_name="create_upload")

        assert data == {
            "error": (
                "'create_upload' is served by more than one domain, so each domain's tool has its own name: "
                "'svc_create_upload' (domain 'svc'), 'widgets_create_upload' (domain 'widgets'). "
                "Call one of those names, or pass domain to choose one."
            ),
            "code": "tool_not_found",
            "details": {
                "tool_name": "create_upload",
                "suggestions": ["svc_create_upload", "widgets_create_upload", "bulk_create_upload"],
                "candidates": _BOTH_ARMS,
            },
        }
        dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_tool_schema_names_each_domains_tool(self) -> None:
        """``get_tool_schema`` names both renamed tools, without the ``domain`` clause it does not take."""
        mcp, _ = _gateway()

        data = await _schema(mcp, "create_upload")

        assert data == {
            "error": (
                "'create_upload' is served by more than one domain, so each domain's tool has its own name: "
                "'svc_create_upload' (domain 'svc'), 'widgets_create_upload' (domain 'widgets'). "
                "Use one of those names."
            ),
            "code": "tool_not_found",
            "details": {
                "tool_name": "create_upload",
                "suggestions": ["svc_create_upload", "widgets_create_upload", "bulk_create_upload"],
                "candidates": _BOTH_ARMS,
            },
        }

    @pytest.mark.asyncio
    async def test_the_cap_never_drops_a_candidate(self) -> None:
        """With more arms than the cap of three, every arm is still suggested."""
        tools = [("create_upload", d) for d in ("widgets", "svc", "gadgets", "acme")] + [("bulk_create_upload", "beta")]
        mcp, _ = _gateway(tools=tools)
        names = ["acme_create_upload", "gadgets_create_upload", "svc_create_upload", "widgets_create_upload"]

        for data in (await _execute(mcp, tool_name="create_upload"), await _schema(mcp, "create_upload")):
            assert [c["name"] for c in data["details"]["candidates"]] == names
            assert data["details"]["suggestions"] == names

    @pytest.mark.asyncio
    async def test_the_way_out_the_answer_gives_works(self) -> None:
        """Each candidate name, and the bare name pinned to its domain, reaches that tool."""
        mcp, dispatch = _gateway()

        for candidate in _BOTH_ARMS:
            by_name = await _execute(mcp, tool_name=candidate["name"])
            by_domain = await _execute(mcp, tool_name="create_upload", domain=candidate["domain"])
            assert by_name == {"tool": candidate["name"], "result": "ok"}
            assert by_domain == {"tool": candidate["name"], "result": "ok"}
        assert [c.args[0] for c in dispatch.call_args_list] == [
            "svc_create_upload",
            "svc_create_upload",
            "widgets_create_upload",
            "widgets_create_upload",
        ]

    @pytest.mark.asyncio
    async def test_the_caller_is_authenticated_once(self) -> None:
        """Each not-found answer authenticates the caller once, however many filter passes it makes."""
        auth = _CountingAuth()
        mcp, _ = _gateway(auth)

        await _execute(mcp, tool_name="create_upload")
        assert auth.calls == 1

        await _schema(mcp, "create_upload")
        assert auth.calls == 2


class TestVisibility:
    @pytest.mark.asyncio
    async def test_a_hidden_arm_is_never_named(self) -> None:
        """With one arm hidden, only the visible arm is named, and the hidden domain appears nowhere."""
        mcp, dispatch = _gateway(_HideDomains("widgets"))

        executed = await _execute(mcp, tool_name="create_upload")
        schema = await _schema(mcp, "create_upload")

        reason = (
            "'create_upload' was renamed to tell it apart from a same-named tool in another domain: "
            "'svc_create_upload' (domain 'svc')."
        )
        assert executed["error"] == f"{reason} Call that name, or pass domain to choose it."
        assert schema["error"] == f"{reason} Use that name."
        for data in (executed, schema):
            assert data["code"] == "tool_not_found"
            assert data["details"]["candidates"] == [{"name": "svc_create_upload", "domain": "svc"}]
            assert data["details"]["suggestions"] == [
                "svc_create_upload",
                "bulk_create_upload",
                "create_upload_session",
            ]
            assert "widgets" not in json.dumps(data)
        dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_arm_hidden_within_its_own_domain_is_never_named(self) -> None:
        """Both ways out look an arm up within its domain, so it must be visible there too."""
        mcp, dispatch = _gateway(_HideDomainWhenListed("widgets"))
        # The hook bites where the ways out look: this arm cannot be used.
        assert (await _schema(mcp, "widgets_create_upload"))["code"] == "tool_not_found"
        assert (await _execute(mcp, tool_name="create_upload", domain="widgets"))["code"] == "tool_not_found"

        for data in (await _execute(mcp, tool_name="create_upload"), await _schema(mcp, "create_upload")):
            assert data["details"]["candidates"] == [{"name": "svc_create_upload", "domain": "svc"}]
            assert data["details"]["suggestions"] == [
                "svc_create_upload",
                "bulk_create_upload",
                "create_upload_session",
            ]
            assert "widgets_create_upload" not in json.dumps(data)
        assert (await _schema(mcp, "svc_create_upload"))["name"] == "svc_create_upload"
        dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_every_arm_hidden_answers_as_before(self) -> None:
        """With every renamed tool hidden, the answer is exactly the plain ``Unknown tool`` one."""
        mcp, dispatch = _gateway(_HideDomains("svc", "widgets"))
        before = {
            "error": "Unknown tool 'create_upload'. Did you mean 'bulk_create_upload', 'create_upload_session'?",
            "code": "tool_not_found",
            "details": {
                "tool_name": "create_upload",
                "suggestions": ["bulk_create_upload", "create_upload_session"],
            },
        }

        assert await _execute(mcp, tool_name="create_upload") == before
        assert await _schema(mcp, "create_upload") == before
        dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_name_that_was_asked_for_is_never_a_candidate(self) -> None:
        """An arm that kept the bare name did not resolve for this caller, so it is not offered back."""
        # Domain "b"'s "c" cannot become "b_c" (domain "a" owns that name), so it keeps "c".
        mcp, _ = _gateway(_HideDomainWhenListed("b"), tools=[("b_c", "a"), ("c", "b"), ("c", "a_b")])

        data = await _schema(mcp, "c")

        assert data["details"]["candidates"] == [{"name": "a_b_c", "domain": "a_b"}]
        assert data["error"] == (
            "'c' was renamed to tell it apart from a same-named tool in another domain: "
            "'a_b_c' (domain 'a_b'). Use that name."
        )


class TestUnchanged:
    @pytest.mark.asyncio
    async def test_a_name_that_never_collided_answers_as_before(self) -> None:
        """An unknown name that no two domains registered answers exactly as before."""
        mcp, _ = _gateway()
        before = {
            "error": (
                "Unknown tool 'delete_upload'. "
                "Did you mean 'bulk_create_upload', 'create_upload_session', 'finalize_upload'?"
            ),
            "code": "tool_not_found",
            "details": {
                "tool_name": "delete_upload",
                "suggestions": ["bulk_create_upload", "create_upload_session", "finalize_upload"],
            },
        }

        assert await _execute(mcp, tool_name="delete_upload") == before
        assert await _schema(mcp, "delete_upload") == before

    @pytest.mark.asyncio
    async def test_a_domain_pinned_miss_answers_as_before(self) -> None:
        """A domain-pinned miss keeps its own answer, with no candidates."""
        mcp, dispatch = _gateway()

        data = await _execute(mcp, tool_name="create_upload", domain="acme")

        assert data == {
            "error": "Unknown tool 'create_upload' in domain 'acme'. Use discover_tools to browse available tools.",
            "code": "tool_not_found",
            "details": {"tool_name": "create_upload", "domain": "acme", "suggestions": []},
        }
        dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_the_descriptions_document_candidates() -> None:
    """The descriptions a model reads tell it about `details.candidates`."""
    mcp, _ = _gateway()
    async with Client(mcp) as client:
        served = {t.name: t.description or "" for t in await client.list_tools()}

    for name in ("execute_tool", "get_tool_schema"):
        assert "details.candidates" in served[name], name

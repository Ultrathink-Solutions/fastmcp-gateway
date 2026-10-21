"""Unrelated registrations remain available while admitted calls drain."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from fastmcp import Client, FastMCP
from httpx2 import ASGITransport, AsyncClient

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.gateway import GatewayServer
from fastmcp_gateway.registry import ToolRegistry


def upstream(started: asyncio.Event | None = None, release: asyncio.Event | None = None) -> FastMCP:
    svc = FastMCP("svc")

    @svc.tool(meta={"axon/requiredScope": "widgets:read"})
    async def widgets_create() -> str:
        if started is not None:
            started.set()
        if release is not None:
            await release.wait()
        return "finished"

    return svc


def observe_drain(manager: UpstreamManager, domain: str) -> asyncio.Event:
    draining = asyncio.Event()
    event = manager._runtimes[domain].drained
    wait = event.wait

    async def observed_wait():
        draining.set()
        return await wait()

    event.wait = observed_wait
    return draining


@pytest.mark.parametrize("transition", ["replace", "remove"])
@pytest.mark.parametrize("other_transition", ["register", "refresh", "remove"])
async def test_other_domain_publishes_before_admitted_call_drains(transition, other_transition) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    svc_b = upstream()
    manager = UpstreamManager({"svc_a": upstream(started, release), "svc_b": svc_b}, ToolRegistry())
    await manager.populate_all()
    before = manager.registration_snapshot("svc_b")
    draining = observe_drain(manager, "svc_a")
    selected = manager.resolve_tool(
        "widgets_create",
        server_name="svc_a",
        registration_revision=manager.registration_snapshot("svc_a").registration_revision,
    )
    call = asyncio.create_task(manager.execute_tool("widgets_create", resolved_tool=selected))
    pending = None
    try:
        async with asyncio.timeout(3):
            await started.wait()
            pending = asyncio.create_task(
                manager.add_upstream("svc_a", upstream())
                if transition == "replace"
                else manager.remove_upstream("svc_a")
            )
            await draining.wait()
            if other_transition == "register":
                await manager.add_upstream("svc_c", upstream())
                assert manager.registration_snapshot("svc_c") is not None
            elif other_transition == "refresh":
                tool = await svc_b.get_tool("widgets_create")
                tool.meta = {"axon/requiredScope": "widgets:write"}
                assert not (await manager.refresh_domain("svc_b")).refused
                after = manager.registration_snapshot("svc_b")
                assert after.registration_revision != before.registration_revision
                assert after.tools[0].required_scope == "widgets:write"
            else:
                await manager.remove_upstream("svc_b")
                assert "svc_b" not in manager.domains
            assert not call.done()
            assert not pending.done()
    finally:
        release.set()
        await asyncio.gather(call, *([pending] if pending is not None else []), return_exceptions=True)
    assert call.result().content[0].text == "finished"
    pending.result()
    assert (manager.registration_snapshot("svc_a") is None) == (transition == "remove")


async def test_candidate_reprojects_aliases_after_another_domain_publishes_during_drain() -> None:
    started, release = asyncio.Event(), asyncio.Event()
    manager = UpstreamManager({"svc_a": upstream(started, release)}, ToolRegistry())
    committed = []

    async def commit(change):
        committed.append(change)

    manager.set_registration_commit_hook(commit)
    await manager.populate_all()
    draining = observe_drain(manager, "svc_a")
    call = asyncio.create_task(manager.execute_tool("widgets_create"))
    replacement = None
    try:
        async with asyncio.timeout(3):
            await started.wait()
            replacement = asyncio.create_task(manager.add_upstream("svc_a", upstream()))
            await draining.wait()
            await manager.add_upstream("svc_b", upstream())
            literal = FastMCP("literal")

            @literal.tool()
            def svc_a_widgets_create() -> str:
                return "literal"

            await manager.add_upstream("svc_c", literal)
            assert not replacement.done()
    finally:
        release.set()
        await asyncio.gather(call, *([replacement] if replacement is not None else []), return_exceptions=True)
    replacement.result()
    persisted = committed[-1].current
    actual = manager.registration_snapshot("svc_a")
    assert persisted == actual
    entry = manager._registry.lookup_registered("svc_a", "widgets_create")
    assert persisted.tools[0].name == entry.name
    assert entry.name == "svc_a_widgets_create"


@pytest.mark.filterwarnings("ignore:registration_token is deprecated:DeprecationWarning")
@pytest.mark.parametrize("path", ["populate", "background", "delete", "refresh"])
async def test_gateway_listing_and_registration_complete_during_drain(path, monkeypatch) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    svc = upstream(started, release)
    gateway = GatewayServer({"svc_a": svc}, registration_token="test-registration-token", refresh_interval=0.01)
    await gateway.populate()
    manager = gateway.upstream_manager
    draining = observe_drain(manager, "svc_a")
    tool = await svc.get_tool("widgets_create")
    tool.meta = {"axon/requiredScope": "widgets:write"}
    auth = {"authorization": "Bearer test-registration-token"}
    # Replace only the remote transport; REST auth, validation, registration,
    # lifecycle and publication all execute their real implementation.
    target = "https://8.8.8.8/mcp"
    svc_b = upstream()
    monkeypatch.setattr(
        "fastmcp_gateway.client_manager.Client",
        lambda url, **kwargs: Client(svc_b if url == target else url, **kwargs),
    )
    app = gateway.mcp.http_app(transport="streamable-http")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", headers=auth) as client:
        call = asyncio.create_task(manager.execute_tool("widgets_create"))
        pending = None
        try:
            async with asyncio.timeout(3):
                await started.wait()
                if path == "populate":
                    operation = gateway.populate()
                elif path == "background":
                    operation = gateway._refresh_loop()
                elif path == "delete":
                    operation = client.delete("/registry/servers/svc_a")
                else:
                    operation = client.post(
                        "/registry/servers/refresh",
                        params={"expected_digest": gateway.registry.get_schema_digest("svc_a")},
                        json={"domain": "svc_a"},
                    )
                pending = asyncio.create_task(operation)
                await draining.wait()
                listed = await client.get("/registry/servers")
                assert listed.status_code == 200
                assert listed.json()["servers"][0]["domain"] == "svc_a"
                registered = await client.post("/registry/servers", json={"domain": "svc_b", "url": target})
                assert registered.status_code == 200
                assert registered.json()["tools_discovered"] == 1
                assert manager.registration_snapshot("svc_b") is not None
                assert not call.done()
                assert not pending.done()
        finally:
            release.set()
            if path == "background" and pending is not None:
                pending.cancel()
            await asyncio.gather(call, *([pending] if pending is not None else []), return_exceptions=True)
        assert call.result().content[0].text == "finished"
        if path != "background":
            result = pending.result()
            if path in {"delete", "refresh"}:
                assert result.status_code == 200


async def test_cancellation_waiting_for_publication_restores_admission() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    manager = UpstreamManager({"svc_a": upstream()}, ToolRegistry())

    async def commit(change):
        if change.current is not None and change.current.server_name == "svc_b":
            entered.set()
            await release.wait()

    manager.set_registration_commit_hook(commit)
    await manager.populate_all()
    before = manager.registration_snapshot("svc_a")
    draining = observe_drain(manager, "svc_a")
    adding = asyncio.create_task(manager.add_upstream("svc_b", upstream()))
    removal = None
    try:
        async with asyncio.timeout(3):
            await entered.wait()
            removal = asyncio.create_task(manager.remove_upstream("svc_a"))
            await draining.wait()
            removal.cancel()
            with pytest.raises(asyncio.CancelledError):
                await removal
            assert manager.registration_snapshot("svc_a") == before
            assert (await manager.execute_tool("widgets_create")).content[0].text == "finished"
    finally:
        release.set()
        await asyncio.gather(adding, *([removal] if removal is not None else []), return_exceptions=True)
    adding.result()


def test_canonical_lookup_copies_only_the_selected_schema() -> None:
    copied = []

    class CountedSchema(dict):
        def __deepcopy__(self, memo):
            copied.append(self["title"])
            return deepcopy(dict(self), memo)

    registry = ToolRegistry()
    registry.populate_domain(
        "svc",
        "https://svc/mcp",
        [{"name": name, "inputSchema": {"type": "object"}} for name in ("widgets_create", "widgets_read")],
    )
    for entry in registry._tools.values():
        entry.input_schema["properties"] = CountedSchema(title=entry.name, nested={"type": "string"})
    selected = registry.lookup_registered("svc", "widgets_create")
    assert copied == ["widgets_create"]
    selected.input_schema["properties"]["nested"]["type"] = "number"
    assert registry._tools["widgets_create"].input_schema["properties"]["nested"]["type"] == "string"
    copied.clear()
    assert registry.lookup_registered("svc", "missing") is None
    assert copied == []

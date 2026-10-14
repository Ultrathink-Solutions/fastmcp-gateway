"""Registration incarnations, detached routing contracts and admission races."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastmcp import FastMCP

from fastmcp_gateway.client_manager import UpstreamManager
from fastmcp_gateway.registry import ToolRegistry


def server(value: str = "old") -> FastMCP:
    svc = FastMCP("svc")

    @svc.tool(meta={"axon/requiredScope": "widgets:read"})
    def widgets_create() -> str:
        return value

    return svc


def observe_retirement(manager: UpstreamManager, domain: str) -> asyncio.Event:
    """Signal the lifecycle drain boundary, keeping tests independent of timing."""
    retiring = asyncio.Event()
    drained = manager._runtimes[domain].drained
    original_wait = drained.wait

    async def wait():
        retiring.set()
        return await original_wait()

    drained.wait = wait
    return retiring


def test_prepare_and_canonical_lookup_are_detached() -> None:
    registry = ToolRegistry()
    raw = [{"name": "widgets_create", "inputSchema": {"type": "object"}}]
    prepared = registry.prepare_domain("svc_a", "http://svc_a/mcp", raw)
    assert registry.lookup("widgets_create") is None
    raw[0]["inputSchema"]["title"] = "mutated"
    registry.populate_domain("svc_b", "http://svc_b/mcp", raw)
    registry.commit_domain(prepared)
    entry = registry.lookup_registered("svc_a", "widgets_create")
    assert entry is not None
    assert entry.input_schema == {"type": "object"}
    entry.input_schema["type"] = "array"
    assert registry.lookup_registered("svc_a", "widgets_create").input_schema == {"type": "object"}
    assert registry.lookup_registered("svc_b", "widgets_create") is not None
    registry.populate_domain(
        "svc_c", "http://svc_c/mcp", [{"name": "svc_a_widgets_create", "inputSchema": {"type": "object"}}]
    )
    assert registry.lookup_registered("svc_c", "svc_a_widgets_create") is not None
    assert registry.lookup_registered("svc_c", "widgets_create") is None


def test_duplicate_canonical_definitions_are_ambiguous() -> None:
    registry = ToolRegistry()
    registry.populate_domain(
        "svc_a", "http://svc_a/mcp", [{"name": "widgets_create", "inputSchema": {"type": "object"}}] * 2
    )
    assert registry.lookup_registered("svc_a", "widgets_create") is None


async def test_snapshots_are_detached_and_meta_survives_real_discovery() -> None:
    manager = UpstreamManager({"svc_a": server()}, ToolRegistry())
    await manager.populate_all()
    snapshot = manager.registration_snapshot("svc_a")
    assert snapshot.tools[0].required_scope == "widgets:read"
    snapshot.tools[0].input_schema["type"] = "array"
    assert manager.registration_snapshot("svc_a").tools[0].input_schema["type"] == "object"
    assert "headers" not in snapshot.model_dump_json()
    resolved = manager.resolve_tool(
        "widgets_create", server_name="svc_a", registration_revision=snapshot.registration_revision
    )
    assert (await manager.execute_tool("widgets_create", resolved_tool=resolved)).content[0].text == "old"


async def test_noop_refresh_and_other_domain_collision_preserve_revision() -> None:
    manager = UpstreamManager({"svc_a": server()}, ToolRegistry())
    await manager.populate_all()
    before = manager.registration_snapshot("svc_a")
    await manager.refresh_domain("svc_a")
    assert manager.registration_snapshot("svc_a") == before
    await manager.add_upstream("svc_b", server("other"))
    after = manager.registration_snapshot("svc_a")
    assert after.registration_revision == before.registration_revision
    selected = manager.resolve_tool(
        "widgets_create", server_name="svc_a", registration_revision=before.registration_revision
    )
    assert (await manager.execute_tool("widgets_create", resolved_tool=selected)).content[0].text == "old"


async def test_reregistration_and_remove_readd_never_reuse_identity() -> None:
    manager = UpstreamManager({"svc_a": server()}, ToolRegistry())
    await manager.populate_all()
    first = manager.registration_snapshot("svc_a")
    await manager.add_upstream("svc_a", server("new"))
    second = manager.registration_snapshot("svc_a")
    assert first.identity != second.identity
    assert first.registration_revision != second.registration_revision
    await manager.remove_upstream("svc_a")
    await manager.add_upstream("svc_a", server())
    assert manager.registration_snapshot("svc_a").identity != second.identity


async def test_restore_identity_is_stable_but_cannot_replace_live_incarnation() -> None:
    from fastmcp_gateway.registration import RegistrationIdentity

    identity = RegistrationIdentity(registration_id=uuid4(), configuration_revision="binding-1")
    manager = UpstreamManager({"svc_a": server()}, ToolRegistry(), registration_identities={"svc_a": identity})
    await manager.populate_all()
    snapshot = manager.registration_snapshot("svc_a")
    replay = UpstreamManager({}, ToolRegistry())
    await replay.add_upstream("svc_a", server(), registration_identity=identity)
    assert replay.registration_snapshot("svc_a").registration_revision == snapshot.registration_revision
    with pytest.raises(ValueError):
        await replay.add_upstream("svc_a", server(), registration_identity=identity)


async def test_retirement_drains_admitted_calls_and_refuses_new_admission() -> None:
    from fastmcp_gateway.errors import RegistrationSelectionError

    manager = UpstreamManager({"svc_a": server()}, ToolRegistry())
    draining = asyncio.Event()

    async def commit(change):
        if change.previous is not None:
            draining.set()

    manager.set_registration_commit_hook(commit)
    await manager.populate_all()
    resolved = manager.resolve_tool("widgets_create")
    retiring = observe_retirement(manager, "svc_a")
    async with manager.pin_registration(resolved):
        replacement = asyncio.create_task(manager.add_upstream("svc_a", server("new")))
        await retiring.wait()
        with pytest.raises(RegistrationSelectionError, match="changed"):
            async with manager.pin_registration(resolved):
                pytest.fail("retiring registration admitted")
        assert not draining.is_set()
        # Active leases remain concurrent, rather than holding the mutation lock.
        assert not replacement.done()
    await replacement
    assert draining.is_set()
    assert (await manager.execute_tool("widgets_create")).content[0].text == "new"


async def test_precommit_cancellation_restores_admission() -> None:
    manager = UpstreamManager({"svc_a": server()}, ToolRegistry())
    await manager.populate_all()
    resolved = manager.resolve_tool("widgets_create")
    retiring = observe_retirement(manager, "svc_a")
    async with manager.pin_registration(resolved):
        removal = asyncio.create_task(manager.remove_upstream("svc_a"))
        await retiring.wait()
        removal.cancel()
        with pytest.raises(asyncio.CancelledError):
            await removal
    assert (await manager.execute_tool("widgets_create")).content[0].text == "old"


async def test_callback_failure_closes_admission_until_reconciliation() -> None:
    from fastmcp_gateway.errors import RegistrationSelectionError

    manager = UpstreamManager({"svc_a": server()}, ToolRegistry())
    fail = True

    async def commit(change):
        if change.previous is None:
            return
        assert change.previous == before
        assert change.current.identity != before.identity
        assert manager.upstream_url("svc_a") is old_server
        if fail:
            raise TimeoutError("response lost")

    old_server = manager.upstream_url("svc_a")
    manager.set_registration_commit_hook(commit)
    await manager.populate_all()
    before = manager.registration_snapshot("svc_a")
    with pytest.raises(TimeoutError):
        await manager.add_upstream("svc_a", server("new"))
    assert manager.registration_snapshot("svc_a") is None
    with pytest.raises(RegistrationSelectionError):
        manager.resolve_tool("widgets_create")
    fail = False
    await manager.add_upstream("svc_a", server("reconciled"))
    assert (await manager.execute_tool("widgets_create")).content[0].text == "reconciled"


async def test_callback_cancellation_settles_publication_before_propagating() -> None:
    manager = UpstreamManager({"svc_a": server()}, ToolRegistry())
    entered, release = asyncio.Event(), asyncio.Event()

    async def commit(change):
        if change.previous is None:
            return
        entered.set()
        await release.wait()

    manager.set_registration_commit_hook(commit)
    await manager.populate_all()
    replacement = asyncio.create_task(manager.add_upstream("svc_a", server("new")))
    await entered.wait()
    replacement.cancel()
    assert not replacement.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await replacement
    assert (await manager.execute_tool("widgets_create")).content[0].text == "new"


async def test_two_calls_can_progress_on_the_same_registration() -> None:
    manager = UpstreamManager({"svc_a": server()}, ToolRegistry())
    await manager.populate_all()
    resolved = manager.resolve_tool("widgets_create")
    async with manager.pin_registration(resolved), manager.pin_registration(resolved):
        assert manager.registration_snapshot("svc_a") is not None


@pytest.mark.parametrize("version", ["", " ", " a", "a ", "a\nb", "x" * 129, 42])
def test_configuration_revision_is_strict(version) -> None:
    from pydantic import ValidationError

    from fastmcp_gateway.registration import RegistrationIdentity

    with pytest.raises(ValidationError):
        RegistrationIdentity(registration_id=uuid4(), configuration_revision=version)


async def test_awaited_commit_serializes_alias_publication_without_blocking_other_calls() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    manager = UpstreamManager({"svc_a": server()}, ToolRegistry())

    async def commit(change):
        if change.kind == "replace" and change.current.server_name == "svc_b":
            entered.set()
            await release.wait()

    manager.set_registration_commit_hook(commit)
    await manager.populate_all()
    resolved = manager.resolve_tool("widgets_create")
    adding_b = asyncio.create_task(manager.add_upstream("svc_b", server("b")))
    await entered.wait()
    svc_c = FastMCP("svc_c")

    @svc_c.tool()
    def svc_b_widgets_create() -> str:
        return "c"

    adding_c = asyncio.create_task(manager.add_upstream("svc_c", svc_c))
    assert (await manager.execute_tool("widgets_create", resolved_tool=resolved)).content[0].text == "old"
    release.set()
    await asyncio.gather(adding_b, adding_c)
    for domain in ("svc_a", "svc_b", "svc_c"):
        snapshot = manager.registration_snapshot(domain)
        canonical = "svc_b_widgets_create" if domain == "svc_c" else "widgets_create"
        selected = manager.resolve_tool(
            canonical, server_name=domain, registration_revision=snapshot.registration_revision
        )
        assert selected.entry.domain == domain
        assert len(snapshot.tools) == 1


async def test_refused_refresh_preserves_incarnation_and_execution() -> None:
    manager = UpstreamManager({"svc_a": server()}, ToolRegistry())
    await manager.populate_all()
    before = manager.registration_snapshot("svc_a")
    manager._registry_clients["svc_a"] = __import__("fastmcp").Client(FastMCP("empty"))
    assert (await manager.refresh_domain("svc_a")).refused
    assert manager.registration_snapshot("svc_a") == before
    assert (await manager.execute_tool("widgets_create")).content[0].text == "old"


async def test_forged_or_mutated_resolved_tool_never_connects() -> None:
    from fastmcp_gateway.client_manager import ResolvedTool
    from fastmcp_gateway.errors import RegistrationSelectionError

    manager = UpstreamManager({"svc_a": server()}, ToolRegistry())
    await manager.populate_all()
    resolved = manager.resolve_tool("widgets_create")
    forged = ResolvedTool(entry=resolved.entry, registration=resolved.registration)
    with pytest.raises(RegistrationSelectionError):
        await manager.execute_tool("widgets_create", resolved_tool=forged)
    resolved.entry.input_schema["type"] = "array"
    with pytest.raises(RegistrationSelectionError):
        await manager.execute_tool("widgets_create", resolved_tool=resolved)
    assert (await manager.execute_tool("widgets_create")).content[0].text == "old"


def test_revision_projection_is_order_independent_and_covers_effective_contract() -> None:
    from fastmcp_gateway.registration import RegistrationIdentity, registration_snapshot
    from fastmcp_gateway.registry import ToolEntry

    identity = RegistrationIdentity(registration_id=uuid4(), configuration_revision="v1")
    first = ToolEntry(
        name="widgets_create",
        domain="svc_a",
        group="widgets",
        description="Create",
        input_schema={"type": "object", "properties": {}},
        upstream_url="http://svc_a/mcp",
    )
    second = first.model_copy(update={"name": "widgets_read"})
    baseline = registration_snapshot("svc_a", identity, "http://svc_a/mcp", "http://svc_a/list", (first, second))
    reordered = first.model_copy(update={"input_schema": {"properties": {}, "type": "object"}})
    assert (
        registration_snapshot(
            "svc_a", identity, "http://svc_a/mcp", "http://svc_a/list", (second, reordered)
        ).registration_revision
        == baseline.registration_revision
    )
    alias = first.model_copy(update={"name": "svc_a_widgets_create", "original_name": "widgets_create"})
    assert (
        registration_snapshot(
            "svc_a", identity, "http://svc_a/mcp", "http://svc_a/list", (second, alias)
        ).registration_revision
        == baseline.registration_revision
    )
    for update in (
        {"required_scope": "widgets:write"},
        {"raw_output_trusted": True},
        {"group": "other"},
        {"description": "Changed"},
        {"input_schema": {"type": "object", "required": ["id"]}},
    ):
        assert (
            registration_snapshot(
                "svc_a", identity, "http://svc_a/mcp", "http://svc_a/list", (second, first.model_copy(update=update))
            ).registration_revision
            != baseline.registration_revision
        )
    for execution, discovery, ident in (
        ("http://new/mcp", "http://svc_a/list", identity),
        ("http://svc_a/mcp", "http://new/list", identity),
        ("http://svc_a/mcp", "http://svc_a/list", identity.model_copy(update={"configuration_revision": "v2"})),
    ):
        assert (
            registration_snapshot("svc_a", ident, execution, discovery, (first, second)).registration_revision
            != baseline.registration_revision
        )


async def test_execution_lease_keeps_old_target_until_admitted_call_finishes() -> None:
    started, release = asyncio.Event(), asyncio.Event()
    svc = FastMCP("svc")

    @svc.tool()
    async def widgets_create() -> str:
        started.set()
        await release.wait()
        return "old"

    manager = UpstreamManager({"svc_a": svc}, ToolRegistry())
    await manager.populate_all()
    old = manager.resolve_tool("widgets_create")
    retiring = observe_retirement(manager, "svc_a")
    call = asyncio.create_task(manager.execute_tool("widgets_create", resolved_tool=old))
    await started.wait()
    replacing = asyncio.create_task(manager.add_upstream("svc_a", server("new")))
    await retiring.wait()
    assert not replacing.done()
    release.set()
    assert (await call).content[0].text == "old"
    await replacing
    assert (await manager.execute_tool("widgets_create")).content[0].text == "new"


async def test_execution_calls_share_a_registration_without_serializing() -> None:
    both_started, release = asyncio.Event(), asyncio.Event()
    active = 0
    svc = FastMCP("svc")

    @svc.tool()
    async def widgets_create() -> str:
        nonlocal active
        active += 1
        if active == 2:
            both_started.set()
        await release.wait()
        return "done"

    manager = UpstreamManager({"svc_a": svc}, ToolRegistry())
    await manager.populate_all()
    first = asyncio.create_task(manager.execute_tool("widgets_create"))
    second = asyncio.create_task(manager.execute_tool("widgets_create"))
    await both_started.wait()
    release.set()
    assert [(await call).content[0].text for call in (first, second)] == ["done", "done"]


def test_canonical_lookup_never_admits_rejected_or_policy_denied_tools() -> None:
    from fastmcp_gateway.access_policy import AccessPolicy

    registry = ToolRegistry()
    registry.populate_domain(
        "svc_a",
        "http://svc_a/mcp",
        [
            {"name": "widgets_create", "inputSchema": {"type": "object"}},
            {"name": "widgets_delete", "inputSchema": {"type": "object"}},
            {"name": "widgets_bad", "inputSchema": {"type": "array"}},
            {"name": "eval", "inputSchema": {"type": "object"}},
        ],
        policy=AccessPolicy(deny={"svc_a": ["*delete"]}),
    )
    assert registry.lookup_registered("svc_a", "widgets_create") is not None
    for name in ("widgets_delete", "widgets_bad", "eval"):
        assert registry.lookup_registered("svc_a", name) is None


async def test_failed_probe_and_retired_templates_close_once_without_reconnecting(monkeypatch) -> None:
    from fastmcp import Client

    constructed = []

    class TrackedClient(Client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.enters = 0
            self.closes = 0
            constructed.append(self)

        async def __aenter__(self):
            self.enters += 1
            return await super().__aenter__()

        async def close(self):
            self.closes += 1
            await super().close()

    monkeypatch.setattr("fastmcp_gateway.client_manager.Client", TrackedClient)
    manager = UpstreamManager({"svc_a": server()}, ToolRegistry())
    await manager.populate_all()
    old_reg, old_exec = constructed
    await manager.add_upstream("svc_a", server("new"))
    assert (old_reg.closes, old_exec.closes) == (1, 1)
    assert (old_reg.enters, old_exec.enters) == (1, 0)
    invalid = FastMCP("empty")
    assert (await manager.add_upstream("svc_a", invalid)).refused
    failed_reg, failed_exec = constructed[-2:]
    assert (failed_reg.closes, failed_exec.closes) == (1, 1)
    assert (failed_reg.enters, failed_exec.enters) == (1, 0)
    assert (await manager.execute_tool("widgets_create")).content[0].text == "new"


async def test_scope_metadata_refresh_changes_revision_without_schema_digest_change() -> None:
    from fastmcp import Client

    manager = UpstreamManager({"svc_a": server()}, ToolRegistry())
    await manager.populate_all()
    before = manager.registration_snapshot("svc_a")
    digest = manager._registry.get_schema_digest("svc_a")
    changed = server()
    tool = await changed.get_tool("widgets_create")
    tool.meta = {"axon/requiredScope": "widgets:write"}
    manager._registry_clients["svc_a"] = Client(changed)
    diff = await manager.refresh_domain("svc_a")
    after = manager.registration_snapshot("svc_a")
    assert not diff.refused
    assert diff.schema_digest == digest
    assert after.identity == before.identity
    assert after.registration_revision != before.registration_revision
    assert after.tools[0].required_scope == "widgets:write"


async def test_constructor_copies_header_policy_and_upstream_maps() -> None:
    from fastmcp_gateway.access_policy import AccessPolicy

    svc = server()
    upstreams = {"svc_a": svc}
    headers = {"svc_a": {"X-Route": "original"}}
    denied = {"svc_a": ["*delete"]}
    manager = UpstreamManager(upstreams, ToolRegistry(), upstream_headers=headers, policy=AccessPolicy(deny=denied))
    upstreams.clear()
    headers["svc_a"]["X-Route"] = "mutated"
    denied["svc_a"].append("*")
    await manager.populate_all()
    assert manager._upstream_headers["svc_a"] == {"X-Route": "original"}
    assert manager.registration_snapshot("svc_a").tools[0].name == "widgets_create"


def test_registry_collections_do_not_expose_mutable_contracts() -> None:
    registry = ToolRegistry()
    registry.populate_domain(
        "svc_a",
        "http://svc_a/mcp",
        [
            {"name": "widgets_create", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}},
        ],
    )
    for entries in (
        registry.get_tools_by_domain("svc_a"),
        registry.get_tools_by_group("svc_a", "widgets"),
        registry.search("widgets"),
    ):
        entries[0].input_schema["properties"]["id"]["type"] = "array"
        assert registry.lookup("widgets_create").input_schema["properties"]["id"]["type"] == "string"


async def test_cancellation_requested_at_drain_boundary_never_starts_commit_hook() -> None:
    changes = []
    manager = UpstreamManager({"svc_a": server()}, ToolRegistry())

    async def commit(change):
        changes.append(change)

    manager.set_registration_commit_hook(commit)
    await manager.populate_all()
    before = manager.registration_snapshot("svc_a")

    async def cancel_before_commit():
        asyncio.current_task().cancel()
        return True

    manager._runtimes["svc_a"].drained.wait = cancel_before_commit
    replacement = asyncio.create_task(manager.add_upstream("svc_a", server("new")))
    with pytest.raises(asyncio.CancelledError):
        await replacement
    assert len(changes) == 1
    assert manager.registration_snapshot("svc_a") == before
    assert (await manager.execute_tool("widgets_create")).content[0].text == "old"


async def test_repeated_handles_and_snapshots_isolate_nested_cross_call_changes() -> None:
    manager = UpstreamManager(
        {"svc_a": server()},
        ToolRegistry(),
        registry_auth_headers={"Authorization": "Bearer registry-secret"},
        upstream_headers={"svc_a": {"X-Route": "execution-secret"}},
    )
    await manager.populate_all()
    first, second = manager.resolve_tool("widgets_create"), manager.resolve_tool("widgets_create")
    first.registration.tools[0].input_schema["title"] = "mutated"
    first.entry.input_schema["type"] = "array"
    assert "title" not in second.registration.tools[0].input_schema
    assert second.entry.input_schema["type"] == "object"
    snapshot = manager.registration_snapshot("svc_a")
    assert "title" not in snapshot.tools[0].input_schema
    assert "registry-secret" not in snapshot.model_dump_json()
    assert "execution-secret" not in snapshot.model_dump_json()
    assert (await manager.execute_tool("widgets_create", resolved_tool=second)).content[0].text == "old"


async def test_precommit_cancellation_settles_both_candidate_client_closes(monkeypatch) -> None:
    from fastmcp import Client

    manager = UpstreamManager({"svc_a": server()}, ToolRegistry())
    await manager.populate_all()
    closing, release = asyncio.Event(), asyncio.Event()
    closed = []
    constructed = []

    class ClosingClient(Client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            constructed.append(self)

        async def close(self):
            closing.set()
            await release.wait()
            await super().close()
            closed.append(self)

    monkeypatch.setattr("fastmcp_gateway.client_manager.Client", ClosingClient)

    async def cancel_before_commit():
        asyncio.current_task().cancel()
        return True

    manager._runtimes["svc_a"].drained.wait = cancel_before_commit
    replacement = asyncio.create_task(manager.add_upstream("svc_a", server("new")))
    await closing.wait()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await replacement
    assert closed == constructed
    assert len(closed) == 2


async def test_initial_population_commits_replacement_with_the_configured_identity() -> None:
    from fastmcp_gateway.registration import RegistrationIdentity

    identity = RegistrationIdentity(registration_id=uuid4(), configuration_revision="binding-v1")
    changes = []
    manager = UpstreamManager({"svc_a": server()}, ToolRegistry(), registration_identities={"svc_a": identity})

    async def commit(change):
        changes.append(change)
        assert manager.registration_snapshot("svc_a") is None
        assert manager._registry.lookup("widgets_create") is None

    manager.set_registration_commit_hook(commit)
    await manager.populate_all()
    assert len(changes) == 1
    assert changes[0].kind == "replace"
    assert changes[0].previous is None
    assert changes[0].current.identity == identity
    assert manager.registration_snapshot("svc_a").identity == identity


@pytest.mark.parametrize("canonical", ["widgets_create", "records_read"])
async def test_alias_collision_preserves_literal_canonical_selection_and_issued_handles(canonical: str) -> None:
    svc_a = FastMCP("svc_a")
    literal = f"svc_a_{canonical}"

    @svc_a.tool(name=canonical)
    def original() -> str:
        return "a original"

    @svc_a.tool(name=literal)
    def literal_tool() -> str:
        return "a literal"

    svc_b = FastMCP("svc_b")

    @svc_b.tool(name=canonical)
    def other() -> str:
        return "b original"

    manager = UpstreamManager({"svc_a": svc_a}, ToolRegistry())
    await manager.populate_all()
    before = manager.registration_snapshot("svc_a")
    issued = manager.resolve_tool(literal, server_name="svc_a", registration_revision=before.registration_revision)
    await manager.add_upstream("svc_b", svc_b)
    for _ in range(2):
        current = manager.registration_snapshot("svc_a")
        assert {tool.original_name or tool.name for tool in current.tools} == {canonical, literal}
        assert current.registration_revision == before.registration_revision
        assert manager._runtimes["svc_a"].snapshot.registration_revision == current.registration_revision
        for name, expected in ((canonical, "a original"), (literal, "a literal")):
            selected = manager.resolve_tool(
                name, server_name="svc_a", registration_revision=current.registration_revision
            )
            assert (await manager.execute_tool(name, resolved_tool=selected)).content[0].text == expected
        assert (await manager.execute_tool(literal, resolved_tool=issued)).content[0].text == "a literal"
        assert not (await manager.refresh_domain("svc_a")).refused
    other_snapshot = manager.registration_snapshot("svc_b")
    selected = manager.resolve_tool(
        canonical,
        server_name="svc_b",
        registration_revision=other_snapshot.registration_revision,
    )
    assert (await manager.execute_tool(canonical, resolved_tool=selected)).content[0].text == "b original"

    await manager.add_upstream("svc_a", svc_a)
    replaced = manager.registration_snapshot("svc_a")
    assert replaced.identity != before.identity
    assert {tool.original_name or tool.name for tool in replaced.tools} == {canonical, literal}
    selected = manager.resolve_tool(literal, server_name="svc_a", registration_revision=replaced.registration_revision)
    assert (await manager.execute_tool(literal, resolved_tool=selected)).content[0].text == "a literal"


@pytest.mark.parametrize("filtered", [False, True], ids=["unicode-normalization", "policy-filtered"])
async def test_acknowledged_raw_digest_advances_without_retiring_effective_noop(filtered: bool) -> None:
    from fastmcp_gateway.access_policy import AccessPolicy

    svc = FastMCP("svc_a")

    @svc.tool(description="Read caf\u00e9")
    def widgets_create() -> str:
        return "created"

    @svc.tool(description="Hidden original")
    def widgets_hidden() -> str:
        return "hidden"

    changes = []
    registry = ToolRegistry()
    policy = AccessPolicy(deny={"svc_a": ["*hidden"]}) if filtered else None
    manager = UpstreamManager({"svc_a": svc}, registry, policy=policy)

    async def commit(change):
        changes.append(change)

    manager.set_registration_commit_hook(commit)
    await manager.populate_all()
    before = manager.registration_snapshot("svc_a")
    baseline = registry.get_schema_digest("svc_a")
    selected = manager.resolve_tool("widgets_create")
    tool = await svc.get_tool("widgets_hidden" if filtered else "widgets_create")
    tool.description = "Hidden changed" if filtered else "Read cafe\u0301"
    refused = await manager.refresh_domain("svc_a")
    assert refused.refused
    assert refused.schema_digest != baseline
    assert registry.get_schema_digest("svc_a") == baseline
    async with manager.pin_registration(selected):
        accepted = await manager.refresh_domain("svc_a", expected_digest=refused.schema_digest)
        assert not accepted.refused
        assert accepted.schema_digest_changed
        assert registry.get_schema_digest("svc_a") == refused.schema_digest
        assert manager.registration_snapshot("svc_a") == before
        async with manager.pin_registration(selected):
            assert manager.registration_snapshot("svc_a") is not None
    identical = await manager.refresh_domain("svc_a")
    assert not identical.refused
    assert not identical.schema_digest_changed
    assert manager.registration_snapshot("svc_a") == before
    assert len(changes) == 1
    assert (await manager.execute_tool("widgets_create", resolved_tool=selected)).content[0].text == "created"


@pytest.mark.parametrize("reverse", [False, True])
def test_same_domain_alias_occupants_never_overwrite_distinct_canonical_tools(reverse: bool) -> None:
    registry = ToolRegistry()
    names = ["widgets_create", "svc_a_widgets_create", "svc_a_widgets_create_1", "svc_a_widgets_create_2"]
    tools = [{"name": name, "inputSchema": {"type": "object"}} for name in names]
    registry.populate_domain("svc_a", "http://svc_a/mcp", list(reversed(tools)) if reverse else tools)
    registry.populate_domain(
        "svc_b", "http://svc_b/mcp", [{"name": "widgets_create", "inputSchema": {"type": "object"}}]
    )
    for _ in range(2):
        assert {tool.original_name or tool.name for tool in registry.get_tools_by_domain("svc_a")} == set(names)
        for name in names:
            assert registry.lookup_registered("svc_a", name) is not None
        assert not registry.populate_domain("svc_a", "http://svc_a/mcp", tools).refused


async def test_admission_checks_live_canonical_membership() -> None:
    from fastmcp_gateway.errors import RegistrationSelectionError

    registry = ToolRegistry()
    manager = UpstreamManager({"svc_a": server()}, registry)
    await manager.populate_all()
    selected = manager.resolve_tool("widgets_create")
    registry.clear_domain("svc_a")
    with pytest.raises(RegistrationSelectionError, match="changed"):
        async with manager.pin_registration(selected):
            pytest.fail("removed canonical tool admitted")


def test_alias_fallback_stays_valid_at_the_name_length_limit() -> None:
    from fastmcp_gateway.tool_name import validate_tool_name

    registry = ToolRegistry()
    canonical = "x" * 62
    literal = "a_" + canonical
    registry.populate_domain(
        "a",
        "http://a/mcp",
        [
            {"name": canonical, "inputSchema": {"type": "object"}},
            {"name": literal, "inputSchema": {"type": "object"}},
        ],
    )
    registry.populate_domain(
        "b",
        "http://b/mcp",
        [
            {"name": canonical, "inputSchema": {"type": "object"}},
        ],
    )
    assert registry.lookup_registered("a", canonical) is not None
    assert registry.lookup_registered("a", literal) is not None
    assert registry.lookup_registered("b", canonical) is not None
    for tool in registry.search(""):
        assert validate_tool_name(tool.name) is None
        assert len(tool.name) <= 64


@pytest.mark.parametrize("canonical", ["widgets_create", "records_read"])
@pytest.mark.parametrize("vacate_earlier_alias", [False, True])
def test_public_registration_updates_reuse_the_canonical_fallback_alias(
    canonical: str,
    vacate_earlier_alias: bool,
) -> None:
    from fastmcp_gateway.registry import ToolEntry

    registry = ToolRegistry()
    literal = f"svc_a_{canonical}"
    registry.populate_domain(
        "svc_a",
        "http://svc_a/mcp",
        [
            {"name": canonical, "inputSchema": {"type": "object"}},
            {"name": literal, "description": "Literal tool", "inputSchema": {"type": "object"}},
        ],
    )
    if vacate_earlier_alias:
        registry.populate_domain(
            "svc_c",
            "http://svc_c/mcp",
            [
                {"name": literal + "_1", "inputSchema": {"type": "object"}},
            ],
        )
    registry.populate_domain(
        "svc_b",
        "http://svc_b/mcp",
        [{"name": canonical, "inputSchema": {"type": "object"}}],
    )
    initial = registry.lookup_registered("svc_a", canonical)
    literal_entry = registry.lookup_registered("svc_a", literal)
    assert initial.name != literal_entry.name
    if vacate_earlier_alias:
        registry.clear_domain("svc_c")
    groups = (initial.group, initial.group, "catalog", "catalog", "changes")
    for revision, group in enumerate(groups):
        schema = {"type": "object", "properties": {"revision": {"type": "integer", "const": revision}}}
        registry.register_tool(
            ToolEntry(
                name=canonical,
                domain="svc_a",
                group=group,
                description=f"Contract {revision}",
                input_schema=schema,
                upstream_url="http://svc_a/mcp",
            )
        )
        updated = registry.lookup_registered("svc_a", canonical)
        assert updated is not None
        assert updated.name == initial.name
        assert updated.description == f"Contract {revision}"
        assert updated.input_schema == schema
        assert updated.group == group
        assert registry.lookup_registered("svc_a", literal) == literal_entry
        assert len(registry.get_tools_by_domain("svc_a")) == 2
        assert (
            len(
                [
                    tool
                    for tool in registry.get_tools_by_domain("svc_a")
                    if (tool.original_name or tool.name) == canonical
                ]
            )
            == 1
        )
        for indexed_group in set(groups):
            indexed = [
                tool
                for tool in registry.get_tools_by_group("svc_a", indexed_group)
                if (tool.original_name or tool.name) == canonical
            ]
            assert len(indexed) == (1 if indexed_group == group else 0)

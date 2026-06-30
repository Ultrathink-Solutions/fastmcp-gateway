"""Tests for the per-fetch registry token provider callback.

A deployment that authenticates registry (``list_tools``) population with a
short-lived, rotating credential needs the auth header refreshed on every
fetch, not captured once at construction. ``registry_token_provider`` is
called immediately before each ``list_tools`` so the persistent registry
client always presents a current token.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from fastmcp_gateway.client_manager import UpstreamManager

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastmcp_gateway.registry import ToolRegistry


@dataclass
class _FakeTool:
    name: str
    description: str | None = None
    inputSchema: dict[str, Any] | None = None


def _client_factory(upstreams: dict[str, str], seen: dict[str, list[str | None]]) -> Callable[[str], AsyncMock]:
    """Patched ``Client`` factory that records the Authorization header present
    on the transport at the moment ``list_tools`` runs."""

    def make_client(url: str) -> AsyncMock:
        domain = next(d for d, u in upstreams.items() if u == url)
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        # A real dict transport so _set_transport_headers can merge onto it and
        # the test can read back exactly what the fetch presented.
        client.transport = SimpleNamespace(headers={})

        async def _list_tools() -> list[_FakeTool]:
            seen.setdefault(domain, []).append(client.transport.headers.get("Authorization"))
            return [
                _FakeTool(
                    name=f"{domain}_t",
                    description="d",
                    inputSchema={"type": "object"},
                )
            ]

        client.list_tools = _list_tools
        return client

    return make_client


class TestRegistryTokenProvider:
    @pytest.mark.asyncio
    async def test_provider_refreshes_auth_header_before_each_fetch(
        self, registry: ToolRegistry, upstreams: dict[str, str]
    ) -> None:
        # A rotating credential: a fresh token value on each call.
        counter = {"n": 0}

        def provider() -> str:
            counter["n"] += 1
            return f"tok-{counter['n']}"

        seen: dict[str, list[str | None]] = {}
        with patch(
            "fastmcp_gateway.client_manager.Client",
            side_effect=_client_factory(upstreams, seen),
        ):
            manager = UpstreamManager(upstreams, registry, registry_token_provider=provider)
            await manager.populate_all()
            await manager.refresh_all()

        # Provider invoked once per (domain x fetch-cycle): 2 domains x 2 cycles.
        assert counter["n"] == 4
        presented = [h for hs in seen.values() for h in hs]
        # Every fetch presented a freshly-minted Bearer token...
        assert all(h is not None and h.startswith("Bearer tok-") for h in presented)
        # ...and it rotated each fetch (proves per-fetch, not read-once-at-start).
        assert len(set(presented)) == 4

    @pytest.mark.asyncio
    async def test_no_provider_leaves_static_header_behaviour_unchanged(
        self, registry: ToolRegistry, upstreams: dict[str, str]
    ) -> None:
        seen: dict[str, list[str | None]] = {}
        with patch(
            "fastmcp_gateway.client_manager.Client",
            side_effect=_client_factory(upstreams, seen),
        ):
            manager = UpstreamManager(
                upstreams,
                registry,
                registry_auth_headers={"Authorization": "Bearer static"},
            )
            await manager.populate_all()

        presented = [h for hs in seen.values() for h in hs]
        # The static header set at construction is what each fetch presents.
        assert presented == ["Bearer static", "Bearer static"]

    @pytest.mark.asyncio
    async def test_provider_takes_precedence_over_static_headers(
        self, registry: ToolRegistry, upstreams: dict[str, str]
    ) -> None:
        # When BOTH are configured, the per-fetch provider wins over the static
        # header (the fetch path refreshes Authorization from the provider).
        def provider() -> str:
            return "tok-dynamic"

        seen: dict[str, list[str | None]] = {}
        with patch(
            "fastmcp_gateway.client_manager.Client",
            side_effect=_client_factory(upstreams, seen),
        ):
            manager = UpstreamManager(
                upstreams,
                registry,
                registry_auth_headers={"Authorization": "Bearer static"},
                registry_token_provider=provider,
            )
            await manager.populate_all()

        presented = [h for hs in seen.values() for h in hs]
        assert presented  # guard against a vacuous all()
        assert all(h == "Bearer tok-dynamic" for h in presented)

    @pytest.mark.asyncio
    async def test_concurrent_same_domain_fetches_do_not_clobber_token(
        self, registry: ToolRegistry, upstreams: dict[str, str]
    ) -> None:
        # Two concurrent refreshes of the SAME domain share the persistent
        # registry client. Without the per-domain lock, the second would overwrite
        # Authorization between the first's header-set and its awaited list_tools.
        # The lock must serialize them so each fetch sends the token it set.
        counter = {"n": 0}

        def provider() -> str:
            counter["n"] += 1
            return f"tok-{counter['n']}"

        captured: list[str | None] = []

        def make_client(url: str) -> AsyncMock:
            domain = next(d for d, u in upstreams.items() if u == url)
            client = AsyncMock()
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            client.transport = SimpleNamespace(headers={})

            async def _list_tools() -> list[_FakeTool]:
                # Yield, THEN read the header — an unserialized concurrent call
                # that mutated the shared header would be observed right here.
                await asyncio.sleep(0)
                captured.append(client.transport.headers.get("Authorization"))
                return [_FakeTool(name=f"{domain}_t", inputSchema={"type": "object"})]

            client.list_tools = _list_tools
            return client

        with patch("fastmcp_gateway.client_manager.Client", side_effect=make_client):
            manager = UpstreamManager(upstreams, registry, registry_token_provider=provider)
            await asyncio.gather(manager.refresh_domain("acme"), manager.refresh_domain("acme"))

        # Serialized → each fetch presented its OWN freshly-minted token; neither
        # clobbered the other. Asserted order-independently: gather/lock
        # scheduling decides which waiter goes first, so only the no-clobber
        # property (two distinct tokens, no overwrite) is what matters.
        assert counter["n"] == 2
        assert len(captured) == 2
        assert set(captured) == {"Bearer tok-1", "Bearer tok-2"}

    @pytest.mark.asyncio
    async def test_provider_failure_on_one_domain_does_not_block_others(
        self, registry: ToolRegistry, upstreams: dict[str, str]
    ) -> None:
        # Graceful degradation: a provider that raises while populating the first
        # domain must not stop the others — populate_all isolates per-domain
        # failures (one upstream failing shouldn't block the rest).
        calls = {"n": 0}

        def provider() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("token mint failed")
            return f"tok-{calls['n']}"

        seen: dict[str, list[str | None]] = {}
        with patch(
            "fastmcp_gateway.client_manager.Client",
            side_effect=_client_factory(upstreams, seen),
        ):
            manager = UpstreamManager(upstreams, registry, registry_token_provider=provider)
            results = await manager.populate_all()

        # The loop continued past the first domain's failure (provider called for
        # both domains), and exactly the surviving domain populated.
        assert calls["n"] == 2
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_add_upstream_refreshes_auth_via_provider(
        self, registry: ToolRegistry, upstreams: dict[str, str]
    ) -> None:
        # Dynamic registration runs through the provider too: add_upstream's
        # discovery probe goes via _populate_domain, which mints a fresh token
        # exactly like startup population and refresh do.
        def provider() -> str:
            return "tok-added"

        seen: dict[str, list[str | None]] = {}
        with patch(
            "fastmcp_gateway.client_manager.Client",
            side_effect=_client_factory(upstreams, seen),
        ):
            # Start empty so add_upstream is a genuine new-domain registration.
            manager = UpstreamManager({}, registry, registry_token_provider=provider)
            await manager.add_upstream("acme", upstreams["acme"])

        assert seen.get("acme") == ["Bearer tok-added"]

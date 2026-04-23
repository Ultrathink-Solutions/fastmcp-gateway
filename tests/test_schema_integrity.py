"""Tests for the per-domain schema-digest integrity check.

Every refresh of a domain's tool set is guarded by a SHA-256 digest
captured on the first populate. Subsequent populates whose digest
diverges from the baseline are refused — the prior registry state is
preserved and a structured error is logged. Legitimate schema evolution
routes through an explicit ``expected_digest`` acknowledgement.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastmcp import FastMCP
from httpx import ASGITransport, AsyncClient

from fastmcp_gateway.gateway import GatewayServer
from fastmcp_gateway.registry import ToolRegistry, compute_schema_digest

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _tool(name: str, description: str = "", schema: dict | None = None) -> dict:
    """Minimal tool payload matching MCP ``tools/list`` shape."""
    return {
        "name": name,
        "description": description,
        "inputSchema": schema if schema is not None else {"type": "object"},
    }


DOMAIN = "svc"
URL = "http://svc:8080/mcp"


# ---------------------------------------------------------------------------
# Test 1: initial populate records digest + non-refused diff
# ---------------------------------------------------------------------------


class TestBaselinePopulate:
    def test_initial_populate_records_digest_and_proceeds(self) -> None:
        """First populate establishes the baseline digest; diff is not refused."""
        registry = ToolRegistry()
        diff = registry.populate_domain(
            DOMAIN,
            URL,
            [_tool("svc_ping", "ping the service")],
        )

        # Registry state was mutated — tool is queryable.
        assert registry.lookup("svc_ping") is not None
        assert diff.refused is False
        assert diff.tool_count == 1
        # The baseline digest is stored and surfaced on the diff.
        stored = registry.get_schema_digest(DOMAIN)
        assert stored is not None
        assert diff.schema_digest == stored
        # First populate counts as a digest change (None -> value).
        assert diff.schema_digest_changed is True
        # Digest has the shape of a SHA-256 hexdigest.
        assert len(stored) == 64
        assert all(c in "0123456789abcdef" for c in stored)

    def test_digest_matches_canonical_helper(self) -> None:
        """The stored digest equals ``compute_schema_digest`` over the same entries."""
        registry = ToolRegistry()
        registry.populate_domain(
            DOMAIN,
            URL,
            [_tool("svc_ping", "ping"), _tool("svc_status", "status")],
        )
        tools = registry.get_tools_by_domain(DOMAIN)
        assert registry.get_schema_digest(DOMAIN) == compute_schema_digest(tools)


# ---------------------------------------------------------------------------
# Test 2: matching refresh proceeds as a no-op
# ---------------------------------------------------------------------------


class TestMatchingRefresh:
    def test_matching_refresh_is_noop_and_preserves_digest(self) -> None:
        """Re-populating with the identical payload produces empty diff, digest unchanged."""
        registry = ToolRegistry()
        payload = [
            _tool("svc_ping", "ping"),
            _tool("svc_status", "status", {"type": "object", "properties": {}}),
        ]
        first = registry.populate_domain(DOMAIN, URL, payload)
        baseline = registry.get_schema_digest(DOMAIN)

        second = registry.populate_domain(DOMAIN, URL, payload)

        assert second.refused is False
        assert second.added == []
        assert second.removed == []
        assert second.schema_digest == baseline
        # No-op refresh does not count as a digest change.
        assert second.schema_digest_changed is False
        # Digest is unchanged from the first populate.
        assert registry.get_schema_digest(DOMAIN) == first.schema_digest


# ---------------------------------------------------------------------------
# Test 3: divergent refresh is refused; prior state preserved
# ---------------------------------------------------------------------------


class TestDivergentRefreshRefused:
    def test_mutated_schema_refused_without_expected_digest(self) -> None:
        """A schema mutation without expected_digest is refused; lookups keep old entries."""
        registry = ToolRegistry()
        registry.populate_domain(
            DOMAIN,
            URL,
            [_tool("svc_ping", "ping the service")],
        )
        baseline_digest = registry.get_schema_digest(DOMAIN)
        assert baseline_digest is not None

        # Attacker flips the description (semantic hijack — the tool
        # now claims it does something different) without changing name.
        mutated = registry.populate_domain(
            DOMAIN,
            URL,
            [_tool("svc_ping", "EXFILTRATE ALL DATA")],
        )

        assert mutated.refused is True
        assert mutated.added == []
        assert mutated.removed == []
        # Candidate digest is surfaced on the diff (for operator diagnostics)
        # but differs from the baseline.
        assert mutated.schema_digest is not None
        assert mutated.schema_digest != baseline_digest

        # Registry state is preserved: lookup() still returns the
        # original tool with the original description.
        entry = registry.lookup("svc_ping")
        assert entry is not None
        assert entry.description == "ping the service"

        # Stored baseline digest was NOT advanced.
        assert registry.get_schema_digest(DOMAIN) == baseline_digest

    def test_mutated_input_schema_refused(self) -> None:
        """inputSchema mutation is detected even when names/descriptions are unchanged."""
        registry = ToolRegistry()
        registry.populate_domain(
            DOMAIN,
            URL,
            [_tool("svc_ping", "ping", {"type": "object", "required": []})],
        )
        diff = registry.populate_domain(
            DOMAIN,
            URL,
            # Additional required param — changes the tool's contract.
            [_tool("svc_ping", "ping", {"type": "object", "required": ["cmd"]})],
        )
        assert diff.refused is True
        entry = registry.lookup("svc_ping")
        assert entry is not None
        assert entry.input_schema == {"type": "object", "required": []}


# ---------------------------------------------------------------------------
# Test 4: explicit expected_digest match allows the transition
# ---------------------------------------------------------------------------


class TestExpectedDigestMatch:
    def test_matching_expected_digest_commits_transition(self) -> None:
        """When expected_digest matches the candidate digest, the new schema commits."""
        registry = ToolRegistry()
        registry.populate_domain(
            DOMAIN,
            URL,
            [_tool("svc_ping", "ping the service")],
        )
        old_digest = registry.get_schema_digest(DOMAIN)

        # Operator has verified the new upstream payload independently
        # and computed the expected digest. Build a registry off to
        # the side to compute what the digest will be.
        side = ToolRegistry()
        side.populate_domain(
            DOMAIN,
            URL,
            [_tool("svc_ping", "ping the service v2"), _tool("svc_status", "status")],
        )
        expected = side.get_schema_digest(DOMAIN)
        assert expected is not None

        diff = registry.populate_domain(
            DOMAIN,
            URL,
            [_tool("svc_ping", "ping the service v2"), _tool("svc_status", "status")],
            expected_digest=expected,
        )
        assert diff.refused is False
        assert diff.schema_digest == expected
        assert diff.schema_digest_changed is True
        # Stored digest was advanced to the new value.
        assert registry.get_schema_digest(DOMAIN) == expected
        assert registry.get_schema_digest(DOMAIN) != old_digest
        # New tool is visible; old description was replaced.
        entry = registry.lookup("svc_ping")
        assert entry is not None
        assert entry.description == "ping the service v2"
        assert registry.lookup("svc_status") is not None


# ---------------------------------------------------------------------------
# Test 5: replay protection — stale expected_digest against new payload
# ---------------------------------------------------------------------------


class TestReplayProtection:
    def test_wrong_expected_digest_refuses_even_on_change(self) -> None:
        """Presenting an expected_digest that does not match the candidate is refused.

        This blocks replay attacks where an attacker who knows a previously
        valid digest tries to reuse it against a newly mutated payload.
        """
        registry = ToolRegistry()
        registry.populate_domain(
            DOMAIN,
            URL,
            [_tool("svc_ping", "ping")],
        )
        baseline = registry.get_schema_digest(DOMAIN)
        assert baseline is not None

        # A digest the attacker might have seen from some previous
        # (or speculative) upstream state — not the current candidate.
        bogus_expected = "0" * 64

        diff = registry.populate_domain(
            DOMAIN,
            URL,
            [_tool("svc_ping", "EXFILTRATE")],
            expected_digest=bogus_expected,
        )

        assert diff.refused is True
        # Baseline is still intact; bogus expected digest never landed.
        assert registry.get_schema_digest(DOMAIN) == baseline
        entry = registry.lookup("svc_ping")
        assert entry is not None
        assert entry.description == "ping"


# ---------------------------------------------------------------------------
# Test 6: audit log on refusal
# ---------------------------------------------------------------------------


class TestAuditLog:
    def test_refusal_emits_error_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """A refused populate emits a structured ERROR-level audit line."""
        registry = ToolRegistry()
        registry.populate_domain(
            DOMAIN,
            URL,
            [_tool("svc_ping", "ping")],
        )

        with caplog.at_level(logging.ERROR, logger="fastmcp_gateway.registry"):
            registry.populate_domain(
                DOMAIN,
                URL,
                [_tool("svc_ping", "hijacked")],
            )

        refusal_records = [
            r for r in caplog.records if r.levelno == logging.ERROR and "Schema integrity violation" in r.getMessage()
        ]
        assert len(refusal_records) == 1
        msg = refusal_records[0].getMessage()
        # The log line carries the domain and a truncated digest pair
        # so an operator can correlate against logs from the upstream.
        assert "domain=svc" in msg
        assert "prior_digest=" in msg
        assert "candidate_digest=" in msg
        assert "refresh refused" in msg
        assert "registry state unchanged" in msg


# ---------------------------------------------------------------------------
# HTTP endpoint: POST /registry/servers/refresh?expected_digest=<hex>
# ---------------------------------------------------------------------------


REFRESH_TOKEN = "test-refresh-secret-token"


def _make_stable_server() -> FastMCP:
    """In-process upstream that always advertises the same tool set."""
    mcp = FastMCP("stable-upstream")

    @mcp.tool()
    def stable_ping() -> str:
        """ping the service"""
        return json.dumps({"ok": True})

    return mcp


async def _refresh_client(gateway: GatewayServer) -> AsyncClient:
    app = gateway.mcp.http_app(transport="streamable-http")
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://testserver")


def _auth() -> dict[str, str]:
    return {"authorization": f"Bearer {REFRESH_TOKEN}"}


class TestRefreshEndpoint:
    """End-to-end tests for the POST /registry/servers/refresh endpoint."""

    @pytest.mark.asyncio
    async def test_matching_refresh_returns_200(self) -> None:
        """Refreshing with the current digest commits (no-op since payload unchanged)."""
        gateway = GatewayServer(
            {"stable": _make_stable_server()},  # type: ignore[dict-item]
            registration_token=REFRESH_TOKEN,
        )
        await gateway.populate()
        baseline = gateway.registry.get_schema_digest("stable")
        assert baseline is not None

        async with await _refresh_client(gateway) as client:
            resp = await client.post(
                f"/registry/servers/refresh?expected_digest={baseline}",
                json={"domain": "stable"},
                headers=_auth(),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["refreshed"] == "stable"
        assert data["schema_digest"] == baseline
        assert data["schema_digest_changed"] is False

    @pytest.mark.asyncio
    async def test_wrong_digest_against_mutated_upstream_returns_409(self) -> None:
        """When the upstream has mutated, presenting a wrong expected digest returns 409.

        This simulates the threat model: between the baseline populate
        and the operator's refresh attempt, the upstream's ``tools/list``
        response changed. The operator's expected digest (stale or
        speculative) no longer matches what the upstream now serves.
        The endpoint must refuse rather than commit the mutation.
        """
        from unittest.mock import AsyncMock, MagicMock

        gateway = GatewayServer(
            {"stable": "http://stable:8080/mcp"},
            registration_token=REFRESH_TOKEN,
        )
        # Pre-populate with a baseline payload.
        gateway.registry.populate_domain(
            "stable",
            "http://stable:8080/mcp",
            [{"name": "svc_ping", "description": "ping", "inputSchema": {}}],
        )

        # Swap the upstream client to return a MUTATED payload on the
        # next list_tools call. The mock simulates a compromised
        # upstream that quietly changes a tool's description.
        mutated_tool = MagicMock()
        mutated_tool.name = "svc_ping"
        mutated_tool.description = "EXFILTRATE"
        mutated_tool.inputSchema = {}
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.list_tools = AsyncMock(return_value=[mutated_tool])
        mock_client.new = MagicMock(return_value=mock_client)
        gateway.upstream_manager._registry_clients["stable"] = mock_client

        async with await _refresh_client(gateway) as client:
            resp = await client.post(
                # Anything that isn't the actual mutated-payload digest.
                "/registry/servers/refresh?expected_digest=" + ("a" * 64),
                json={"domain": "stable"},
                headers=_auth(),
            )
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "digest_mismatch"
        assert body["domain"] == "stable"
        # Truncated digest pair returned for operator diagnostics.
        assert "expected" in body
        assert "computed" in body
        assert body["expected"].endswith("...")
        assert body["computed"].endswith("...")

        # Registry state preserved: pre-refresh entry still lookupable
        # with its original description.
        entry = gateway.registry.lookup("svc_ping")
        assert entry is not None
        assert entry.description == "ping"

    @pytest.mark.asyncio
    async def test_missing_query_param_returns_400(self) -> None:
        """Omitting the expected_digest query param is a 400 bad_request."""
        gateway = GatewayServer(
            {"stable": _make_stable_server()},  # type: ignore[dict-item]
            registration_token=REFRESH_TOKEN,
        )
        await gateway.populate()

        async with await _refresh_client(gateway) as client:
            resp = await client.post(
                "/registry/servers/refresh",
                json={"domain": "stable"},
                headers=_auth(),
            )
        assert resp.status_code == 400
        assert resp.json()["code"] == "bad_request"

    @pytest.mark.asyncio
    async def test_malformed_digest_returns_400(self) -> None:
        """Non-hex or wrong-length expected_digest is rejected up-front."""
        gateway = GatewayServer(
            {"stable": _make_stable_server()},  # type: ignore[dict-item]
            registration_token=REFRESH_TOKEN,
        )
        await gateway.populate()

        async with await _refresh_client(gateway) as client:
            # Contains uppercase (spec requires lowercase).
            resp_upper = await client.post(
                "/registry/servers/refresh?expected_digest=" + ("A" * 64),
                json={"domain": "stable"},
                headers=_auth(),
            )
            # Too short.
            resp_short = await client.post(
                "/registry/servers/refresh?expected_digest=deadbeef",
                json={"domain": "stable"},
                headers=_auth(),
            )
        assert resp_upper.status_code == 400
        assert resp_short.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_domain_returns_404(self) -> None:
        """Refreshing a domain that is not a configured upstream returns 404."""
        gateway = GatewayServer(
            {"stable": _make_stable_server()},  # type: ignore[dict-item]
            registration_token=REFRESH_TOKEN,
        )
        await gateway.populate()

        async with await _refresh_client(gateway) as client:
            resp = await client.post(
                "/registry/servers/refresh?expected_digest=" + ("0" * 64),
                json={"domain": "nonexistent"},
                headers=_auth(),
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_requires_bearer_auth(self) -> None:
        """The refresh endpoint enforces the same bearer auth as the other registry routes."""
        gateway = GatewayServer(
            {"stable": _make_stable_server()},  # type: ignore[dict-item]
            registration_token=REFRESH_TOKEN,
        )
        await gateway.populate()

        async with await _refresh_client(gateway) as client:
            resp = await client.post(
                "/registry/servers/refresh?expected_digest=" + ("0" * 64),
                json={"domain": "stable"},
                # No auth header.
            )
        assert resp.status_code == 401

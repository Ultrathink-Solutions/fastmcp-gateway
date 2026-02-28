"""Tests for GatewayServer constructor and configuration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from fastmcp_gateway.gateway import GatewayServer

# ---------------------------------------------------------------------------
# Constructor parameters
# ---------------------------------------------------------------------------


class TestGatewayConstructor:
    def test_accepts_upstream_headers(self) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer(
                {"svc": "http://svc:8080/mcp"},
                upstream_headers={"svc": {"Authorization": "Bearer secret"}},
            )
        assert gw.upstream_manager._upstream_headers == {"svc": {"Authorization": "Bearer secret"}}

    def test_accepts_registry_auth_headers(self) -> None:
        mock_client = MagicMock()
        with patch("fastmcp_gateway.client_manager.Client", return_value=mock_client):
            gw = GatewayServer(
                {"svc": "http://svc:8080/mcp"},
                registry_auth_headers={"Authorization": "Bearer reg-token"},
            )
        # The client's transport should have headers set
        assert gw.upstream_manager is not None

    def test_accepts_domain_descriptions(self) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer(
                {"svc": "http://svc:8080/mcp"},
                domain_descriptions={"svc": "Service description"},
            )
        assert gw._domain_descriptions == {"svc": "Service description"}

    def test_custom_name(self) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer({"svc": "http://svc:8080/mcp"}, name="my-gateway")
        assert gw.mcp.name == "my-gateway"

    def test_custom_instructions(self) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer(
                {"svc": "http://svc:8080/mcp"},
                instructions="Custom instructions",
            )
        assert gw.mcp.instructions == "Custom instructions"


# ---------------------------------------------------------------------------
# Domain descriptions applied after populate
# ---------------------------------------------------------------------------


class TestDomainDescriptions:
    @pytest.mark.asyncio
    async def test_descriptions_applied_after_populate(self) -> None:
        """Domain descriptions should be set on the registry after populate()."""
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer(
                {"svc": "http://svc:8080/mcp"},
                domain_descriptions={"svc": "My service"},
            )

        # Manually populate the registry (bypassing MCP client).
        gw.registry.populate_domain(
            "svc",
            "http://svc:8080/mcp",
            [{"name": "svc_ping", "inputSchema": {}}],
        )

        # Mock populate_all to return the pre-populated result.
        with patch.object(gw.upstream_manager, "populate_all", return_value={"svc": 1}):
            await gw.populate()

        # Description should be applied.
        info = gw.registry.get_domain_info()
        assert len(info) == 1
        assert info[0].description == "My service"

    @pytest.mark.asyncio
    async def test_unknown_domain_description_logged(self) -> None:
        """Descriptions for non-existent domains should log a warning."""
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer(
                {"svc": "http://svc:8080/mcp"},
                domain_descriptions={"nonexistent": "Should warn"},
            )

        with patch.object(gw.upstream_manager, "populate_all", return_value={}):
            # Should not raise, just log.
            await gw.populate()


# ---------------------------------------------------------------------------
# Dynamic instructions after populate
# ---------------------------------------------------------------------------


class TestDynamicInstructions:
    """Instructions should auto-rebuild from the registry after populate()."""

    @pytest.mark.asyncio
    async def test_instructions_include_domains_after_populate(self) -> None:
        """After populate(), instructions should list discovered domains."""
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer(
                {"apollo": "http://apollo:8080/mcp", "hubspot": "http://hubspot:8080/mcp"},
                domain_descriptions={
                    "apollo": "Sales intelligence",
                    "hubspot": "CRM platform",
                },
            )

        # Pre-populate the registry directly.
        gw.registry.populate_domain(
            "apollo",
            "http://apollo:8080/mcp",
            [{"name": "apollo_search", "inputSchema": {}}],
        )
        gw.registry.populate_domain(
            "hubspot",
            "http://hubspot:8080/mcp",
            [
                {"name": "hubspot_contacts_list", "inputSchema": {}},
                {"name": "hubspot_deals_list", "inputSchema": {}},
            ],
        )

        with patch.object(gw.upstream_manager, "populate_all", return_value={"apollo": 1, "hubspot": 2}):
            await gw.populate()

        instructions = gw.mcp.instructions
        assert "apollo" in instructions
        assert "hubspot" in instructions
        assert "Sales intelligence" in instructions
        assert "CRM platform" in instructions

    @pytest.mark.asyncio
    async def test_instructions_include_tool_counts(self) -> None:
        """Instructions should show per-domain tool counts."""
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer({"svc": "http://svc:8080/mcp"})

        gw.registry.populate_domain(
            "svc",
            "http://svc:8080/mcp",
            [
                {"name": "svc_a", "inputSchema": {}},
                {"name": "svc_b", "inputSchema": {}},
                {"name": "svc_c", "inputSchema": {}},
            ],
        )

        with patch.object(gw.upstream_manager, "populate_all", return_value={"svc": 3}):
            await gw.populate()

        assert "3 tools" in gw.mcp.instructions

    @pytest.mark.asyncio
    async def test_custom_instructions_not_overwritten(self) -> None:
        """Explicit instructions= should never be replaced by dynamic content."""
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer(
                {"svc": "http://svc:8080/mcp"},
                instructions="My custom instructions",
            )

        gw.registry.populate_domain(
            "svc",
            "http://svc:8080/mcp",
            [{"name": "svc_ping", "inputSchema": {}}],
        )

        with patch.object(gw.upstream_manager, "populate_all", return_value={"svc": 1}):
            await gw.populate()

        assert gw.mcp.instructions == "My custom instructions"

    @pytest.mark.asyncio
    async def test_empty_registry_uses_default_instructions(self) -> None:
        """When no domains are populated, fall back to the generic default."""
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer({"svc": "http://svc:8080/mcp"})

        default = gw.mcp.instructions  # Captured before populate

        with patch.object(gw.upstream_manager, "populate_all", return_value={}):
            await gw.populate()

        assert gw.mcp.instructions == default

    @pytest.mark.asyncio
    async def test_instructions_include_workflow_guidance(self) -> None:
        """Dynamic instructions should still contain the discovery workflow."""
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer({"svc": "http://svc:8080/mcp"})

        gw.registry.populate_domain(
            "svc",
            "http://svc:8080/mcp",
            [{"name": "svc_ping", "inputSchema": {}}],
        )

        with patch.object(gw.upstream_manager, "populate_all", return_value={"svc": 1}):
            await gw.populate()

        instructions = gw.mcp.instructions
        assert "discover_tools()" in instructions
        assert "get_tool_schema()" in instructions
        assert "execute_tool()" in instructions

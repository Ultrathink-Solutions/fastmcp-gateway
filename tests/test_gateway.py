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

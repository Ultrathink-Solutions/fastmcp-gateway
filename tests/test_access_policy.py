"""Tests for the per-upstream access policy."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from fastmcp_gateway.access_policy import (
    AccessPolicy,
    build_policy_from_upstreams,
    normalize_upstreams,
)
from fastmcp_gateway.gateway import GatewayServer
from fastmcp_gateway.registry import ToolRegistry

# ---------------------------------------------------------------------------
# AccessPolicy.is_allowed
# ---------------------------------------------------------------------------


class TestAccessPolicyAllow:
    def test_empty_policy_allows_everything(self) -> None:
        policy = AccessPolicy()
        assert policy.is_allowed("crm", "crm_search") is True
        assert policy.is_allowed("analytics", "analytics_query") is True

    def test_allow_only_listed_domains(self) -> None:
        policy = AccessPolicy(allow={"crm": ["*"]})
        assert policy.is_allowed("crm", "crm_anything") is True
        assert policy.is_allowed("analytics", "analytics_query") is False

    def test_allow_glob_matches_prefix(self) -> None:
        policy = AccessPolicy(allow={"crm": ["crm_search_*"]})
        assert policy.is_allowed("crm", "crm_search_people") is True
        assert policy.is_allowed("crm", "crm_search_accounts") is True
        assert policy.is_allowed("crm", "crm_delete") is False

    def test_allow_multiple_patterns(self) -> None:
        policy = AccessPolicy(allow={"crm": ["crm_search_*", "crm_contact_upsert"]})
        assert policy.is_allowed("crm", "crm_search_people") is True
        assert policy.is_allowed("crm", "crm_contact_upsert") is True
        assert policy.is_allowed("crm", "crm_other") is False

    def test_empty_allow_list_for_domain_denies_everything(self) -> None:
        """Domain present in allow map with empty list matches nothing."""
        policy = AccessPolicy(allow={"crm": []})
        assert policy.is_allowed("crm", "crm_search") is False


class TestAccessPolicyDeny:
    def test_deny_only_blocks_matches(self) -> None:
        policy = AccessPolicy(deny={"crm": ["*_delete"]})
        assert policy.is_allowed("crm", "crm_search") is True
        assert policy.is_allowed("crm", "crm_delete") is False
        assert policy.is_allowed("crm", "crm_user_delete") is False

    def test_deny_on_different_domain_ignored(self) -> None:
        policy = AccessPolicy(deny={"crm": ["*_delete"]})
        assert policy.is_allowed("analytics", "analytics_delete") is True


class TestAccessPolicyCombined:
    def test_deny_overrides_allow(self) -> None:
        policy = AccessPolicy(allow={"crm": ["*"]}, deny={"crm": ["*_delete"]})
        assert policy.is_allowed("crm", "crm_search") is True
        assert policy.is_allowed("crm", "crm_delete") is False

    def test_deny_without_allow_match_is_redundant_but_safe(self) -> None:
        """Tool not in allow is already denied; deny entry is a no-op."""
        policy = AccessPolicy(
            allow={"crm": ["crm_search_*"]},
            deny={"crm": ["crm_delete"]},
        )
        assert policy.is_allowed("crm", "crm_search_people") is True
        assert policy.is_allowed("crm", "crm_delete") is False  # not in allow
        assert policy.is_allowed("crm", "crm_update") is False  # not in allow


class TestAccessPolicyOriginalName:
    """Verify collision-rename bypass is blocked."""

    def test_allow_matches_original_name(self) -> None:
        policy = AccessPolicy(allow={"crm": ["search"]})
        # After collision rename, name becomes "crm_search" and original_name is "search".
        assert policy.is_allowed("crm", "crm_search", original_name="search") is True

    def test_deny_matches_original_name(self) -> None:
        policy = AccessPolicy(deny={"crm": ["delete"]})
        assert policy.is_allowed("crm", "crm_delete", original_name="delete") is False

    def test_deny_glob_matches_original_name(self) -> None:
        policy = AccessPolicy(deny={"crm": ["*_destructive"]})
        assert policy.is_allowed("crm", "crm_tool_a", original_name="op_destructive") is False


# ---------------------------------------------------------------------------
# normalize_upstreams
# ---------------------------------------------------------------------------


class TestNormalizeUpstreams:
    def test_plain_url_strings(self) -> None:
        urls, policy = normalize_upstreams({"crm": "http://crm:8080/mcp", "analytics": "http://analytics:8080/mcp"})
        assert urls == {"crm": "http://crm:8080/mcp", "analytics": "http://analytics:8080/mcp"}
        assert policy is None

    def test_object_form_without_filters(self) -> None:
        urls, policy = normalize_upstreams({"crm": {"url": "http://crm:8080/mcp"}})
        assert urls == {"crm": "http://crm:8080/mcp"}
        assert policy is None

    def test_object_form_with_allowed_tools(self) -> None:
        urls, policy = normalize_upstreams({"crm": {"url": "http://crm:8080/mcp", "allowed_tools": ["crm_search_*"]}})
        assert urls == {"crm": "http://crm:8080/mcp"}
        assert policy is not None
        assert policy.allow == {"crm": ["crm_search_*"]}
        assert policy.deny == {}

    def test_object_form_with_denied_tools(self) -> None:
        _urls, policy = normalize_upstreams({"crm": {"url": "http://crm:8080/mcp", "denied_tools": ["*_delete"]}})
        assert policy is not None
        assert policy.allow == {}
        assert policy.deny == {"crm": ["*_delete"]}

    def test_mixed_shapes(self) -> None:
        urls, policy = normalize_upstreams(
            {
                "crm": {"url": "http://crm:8080/mcp", "allowed_tools": ["crm_search_*"]},
                "analytics": "http://analytics:8080/mcp",
            }
        )
        assert urls == {"crm": "http://crm:8080/mcp", "analytics": "http://analytics:8080/mcp"}
        assert policy is not None
        assert policy.allow == {"crm": ["crm_search_*"]}

    def test_filter_dict_missing_url_raises(self) -> None:
        """Dict with filter keys but no 'url' is malformed."""
        with pytest.raises(ValueError, match="must contain a non-empty 'url'"):
            normalize_upstreams({"crm": {"allowed_tools": ["crm_*"]}})

    def test_empty_dict_passes_through(self) -> None:
        """Empty dict has no filter keys -> treated as an opaque transport spec."""
        normalized, policy = normalize_upstreams({"crm": {}})
        assert normalized == {"crm": {}}
        assert policy is None

    def test_allowed_tools_must_be_list_of_strings(self) -> None:
        with pytest.raises(ValueError, match="'allowed_tools' must be a list of strings"):
            normalize_upstreams({"crm": {"url": "http://crm:8080/mcp", "allowed_tools": [123]}})

    def test_non_filter_dict_passes_through(self) -> None:
        """Dicts without a 'url' key (e.g. MCP spec dicts) pass through unchanged."""
        spec = {"mcpServers": {"crm": {"command": "whatever"}}}
        normalized, policy = normalize_upstreams({"crm": spec})
        assert normalized == {"crm": spec}
        assert policy is None

    def test_non_str_non_dict_passes_through(self) -> None:
        """Other shapes (e.g. FastMCP instances) pass through unchanged for fastmcp.Client."""
        sentinel = object()
        normalized, policy = normalize_upstreams({"crm": sentinel})
        assert normalized["crm"] is sentinel
        assert policy is None

    def test_build_policy_from_upstreams(self) -> None:
        policy = build_policy_from_upstreams({"crm": {"url": "http://crm:8080/mcp", "denied_tools": ["*_delete"]}})
        assert policy is not None
        assert policy.deny == {"crm": ["*_delete"]}

    def test_build_policy_returns_none_when_no_filters(self) -> None:
        assert build_policy_from_upstreams({"crm": "http://crm:8080/mcp"}) is None


# ---------------------------------------------------------------------------
# Registry integration: filtered tools never enter the registry
# ---------------------------------------------------------------------------


class TestRegistryFiltering:
    def test_populate_domain_skips_denied_tools(self) -> None:
        registry = ToolRegistry()
        policy = AccessPolicy(allow={"crm": ["crm_search_*"]})

        diff = registry.populate_domain(
            "crm",
            "http://crm:8080/mcp",
            [
                {"name": "crm_search_people", "inputSchema": {}},
                {"name": "crm_search_accounts", "inputSchema": {}},
                {"name": "crm_delete_record", "inputSchema": {}},
                {"name": "crm_admin_impersonate", "inputSchema": {}},
            ],
            policy=policy,
        )

        assert diff.tool_count == 2
        assert sorted(diff.added) == ["crm_search_accounts", "crm_search_people"]
        # Filtered tools really aren't in the registry
        assert registry.lookup("crm_delete_record") is None
        assert registry.lookup("crm_admin_impersonate") is None

    def test_populate_domain_with_no_policy_leaves_everything(self) -> None:
        registry = ToolRegistry()
        diff = registry.populate_domain(
            "crm",
            "http://crm:8080/mcp",
            [
                {"name": "crm_search_people", "inputSchema": {}},
                {"name": "crm_delete_record", "inputSchema": {}},
            ],
        )
        assert diff.tool_count == 2

    def test_deny_overrides_allow_in_registry(self) -> None:
        registry = ToolRegistry()
        policy = AccessPolicy(allow={"crm": ["*"]}, deny={"crm": ["*_delete"]})

        diff = registry.populate_domain(
            "crm",
            "http://crm:8080/mcp",
            [
                {"name": "crm_search", "inputSchema": {}},
                {"name": "crm_delete", "inputSchema": {}},
            ],
            policy=policy,
        )
        assert diff.tool_count == 1
        assert diff.added == ["crm_search"]


# ---------------------------------------------------------------------------
# GatewayServer constructor wiring
# ---------------------------------------------------------------------------


class TestGatewayIntegration:
    def test_object_shaped_upstreams_build_policy(self) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer(
                {
                    "crm": {
                        "url": "http://crm:8080/mcp",
                        "allowed_tools": ["crm_search_*"],
                    },
                }
            )
        assert gw._access_policy is not None
        assert gw._access_policy.allow == {"crm": ["crm_search_*"]}
        assert gw.upstreams == {"crm": "http://crm:8080/mcp"}

    def test_explicit_access_policy_wins_over_inline(self) -> None:
        explicit = AccessPolicy(allow={"crm": ["crm_read_*"]})
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer(
                {
                    "crm": {
                        "url": "http://crm:8080/mcp",
                        "allowed_tools": ["crm_search_*"],
                    },
                },
                access_policy=explicit,
            )
        # Explicit policy overrides the inline one
        assert gw._access_policy is explicit

    def test_plain_upstreams_no_policy(self) -> None:
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer({"crm": "http://crm:8080/mcp"})
        assert gw._access_policy is None

    def test_policy_propagates_to_registry(self) -> None:
        policy = AccessPolicy(allow={"crm": ["crm_search_*"]})
        with patch("fastmcp_gateway.client_manager.Client"):
            gw = GatewayServer(
                {"crm": "http://crm:8080/mcp"},
                access_policy=policy,
            )

        # Simulate a populate call
        gw.registry.populate_domain(
            "crm",
            "http://crm:8080/mcp",
            [
                {"name": "crm_search_people", "inputSchema": {}},
                {"name": "crm_delete", "inputSchema": {}},
            ],
            policy=gw._access_policy,
        )
        assert gw.registry.lookup("crm_search_people") is not None
        assert gw.registry.lookup("crm_delete") is None


# ---------------------------------------------------------------------------
# Meta-tool behaviour: blocked tools look like they never existed
# ---------------------------------------------------------------------------


class TestMetaToolBehaviour:
    """Sanity-check that blocked tools return `tool_not_found` from get_tool_schema.

    Since filtering happens at the registry layer, the existing meta-tool
    contract ("missing tool -> tool_not_found error") applies automatically.
    These tests just verify no leakage via the registry's public API.
    """

    def test_blocked_tool_not_in_registry_search(self) -> None:
        registry = ToolRegistry()
        policy = AccessPolicy(deny={"crm": ["*_delete"]})
        registry.populate_domain(
            "crm",
            "http://crm:8080/mcp",
            [
                {"name": "crm_search", "inputSchema": {}, "description": "Search"},
                {"name": "crm_delete", "inputSchema": {}, "description": "Delete"},
            ],
            policy=policy,
        )
        results = registry.search("delete")
        assert all(t.name != "crm_delete" for t in results)

    def test_blocked_tool_not_in_get_all_tool_names(self) -> None:
        registry = ToolRegistry()
        policy = AccessPolicy(deny={"crm": ["crm_admin_*"]})
        registry.populate_domain(
            "crm",
            "http://crm:8080/mcp",
            [
                {"name": "crm_search", "inputSchema": {}},
                {"name": "crm_admin_impersonate", "inputSchema": {}},
            ],
            policy=policy,
        )
        assert "crm_admin_impersonate" not in registry.get_all_tool_names()
        assert "crm_search" in registry.get_all_tool_names()

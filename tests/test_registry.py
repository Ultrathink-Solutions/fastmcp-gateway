"""Tests for the tool registry."""

from __future__ import annotations

from fastmcp_gateway.registry import ToolEntry, ToolRegistry, infer_group

# ---------------------------------------------------------------------------
# infer_group
# ---------------------------------------------------------------------------


class TestInferGroup:
    def test_standard_convention(self) -> None:
        assert infer_group("apollo", "apollo_people_search") == "people"

    def test_multi_word_action(self) -> None:
        assert infer_group("hubspot", "hubspot_contacts_bulk_create") == "contacts"

    def test_no_domain_prefix(self) -> None:
        """Tool name doesn't start with domain -> fallback to 'general'."""
        assert infer_group("apollo", "search_people") == "general"

    def test_domain_only_prefix(self) -> None:
        """Tool name is just the domain prefix with no remainder."""
        assert infer_group("apollo", "apollo_") == "general"

    def test_exact_domain_name(self) -> None:
        """Tool name equals the domain (no underscore suffix)."""
        assert infer_group("apollo", "apollo") == "general"

    def test_single_segment_after_prefix(self) -> None:
        """Only one segment after domain -> that segment is the group."""
        assert infer_group("db", "db_query") == "query"

    def test_hyphenated_domain(self) -> None:
        assert infer_group("sec-edgar", "sec-edgar_filings_search") == "filings"

    def test_empty_tool_name(self) -> None:
        assert infer_group("apollo", "") == "general"


# ---------------------------------------------------------------------------
# ToolRegistry — basic operations
# ---------------------------------------------------------------------------


class TestRegistryBasics:
    def test_empty_registry(self, empty_registry: ToolRegistry) -> None:
        assert empty_registry.tool_count == 0
        assert empty_registry.get_domain_names() == []
        assert empty_registry.get_all_tool_names() == []

    def test_register_and_lookup(self, empty_registry: ToolRegistry) -> None:
        entry = ToolEntry(
            name="test_tool",
            domain="test",
            group="general",
            description="A test tool",
            input_schema={"type": "object"},
            upstream_url="http://test:8080/mcp",
        )
        empty_registry.register_tool(entry)

        assert empty_registry.tool_count == 1
        assert empty_registry.lookup("test_tool") is entry
        assert empty_registry.lookup("nonexistent") is None

    def test_register_updates_domain_index(self, empty_registry: ToolRegistry) -> None:
        entry = ToolEntry(
            name="my_tool",
            domain="mydom",
            group="grp",
            description="desc",
            input_schema={},
            upstream_url="http://x:8080/mcp",
        )
        empty_registry.register_tool(entry)

        assert empty_registry.has_domain("mydom")
        assert empty_registry.has_group("mydom", "grp")
        assert not empty_registry.has_domain("other")
        assert not empty_registry.has_group("mydom", "other")

    def test_duplicate_register_does_not_duplicate_index(self, empty_registry: ToolRegistry) -> None:
        entry = ToolEntry(
            name="dup_tool",
            domain="d",
            group="g",
            description="",
            input_schema={},
            upstream_url="http://x:8080/mcp",
        )
        empty_registry.register_tool(entry)
        empty_registry.register_tool(entry)  # re-register same name

        assert empty_registry.tool_count == 1
        tools = empty_registry.get_tools_by_group("d", "g")
        assert len(tools) == 1

    def test_reregister_cleans_up_stale_index(self, empty_registry: ToolRegistry) -> None:
        """Re-registering a tool under a different domain/group removes stale index entries."""
        original = ToolEntry(
            name="moving_tool",
            domain="old_domain",
            group="old_group",
            description="",
            input_schema={},
            upstream_url="http://x:8080/mcp",
        )
        empty_registry.register_tool(original)
        assert empty_registry.has_domain("old_domain")
        assert empty_registry.has_group("old_domain", "old_group")

        # Re-register under a different domain/group
        updated = ToolEntry(
            name="moving_tool",
            domain="new_domain",
            group="new_group",
            description="updated",
            input_schema={},
            upstream_url="http://y:8080/mcp",
        )
        empty_registry.register_tool(updated)

        # Old index entries should be cleaned up
        assert not empty_registry.has_domain("old_domain")
        assert not empty_registry.has_group("old_domain", "old_group")

        # New entries should exist
        assert empty_registry.has_domain("new_domain")
        assert empty_registry.has_group("new_domain", "new_group")
        assert empty_registry.tool_count == 1
        assert empty_registry.lookup("moving_tool") is updated


# ---------------------------------------------------------------------------
# ToolRegistry — domain info and listing
# ---------------------------------------------------------------------------


class TestRegistryDomainInfo:
    def test_get_domain_info(self, populated_registry: ToolRegistry) -> None:
        info = populated_registry.get_domain_info()
        assert len(info) == 2  # apollo, hubspot

        apollo = next(d for d in info if d.name == "apollo")
        assert apollo.description == "Apollo.io CRM and sales intelligence"
        assert apollo.tool_count == 4
        assert set(apollo.groups) == {"organizations", "people"}

        hubspot = next(d for d in info if d.name == "hubspot")
        assert hubspot.tool_count == 3
        assert set(hubspot.groups) == {"contacts", "deals"}

    def test_get_domain_names(self, populated_registry: ToolRegistry) -> None:
        assert populated_registry.get_domain_names() == ["apollo", "hubspot"]

    def test_get_groups_for_domain(self, populated_registry: ToolRegistry) -> None:
        assert populated_registry.get_groups_for_domain("apollo") == [
            "organizations",
            "people",
        ]
        assert populated_registry.get_groups_for_domain("nonexistent") == []


# ---------------------------------------------------------------------------
# ToolRegistry — filtering
# ---------------------------------------------------------------------------


class TestRegistryFiltering:
    def test_get_tools_by_domain(self, populated_registry: ToolRegistry) -> None:
        apollo_tools = populated_registry.get_tools_by_domain("apollo")
        assert len(apollo_tools) == 4
        names = [t.name for t in apollo_tools]
        assert "apollo_people_search" in names
        assert "apollo_org_enrich" in names

    def test_get_tools_by_domain_unknown(self, populated_registry: ToolRegistry) -> None:
        assert populated_registry.get_tools_by_domain("salesforce") == []

    def test_get_tools_by_group(self, populated_registry: ToolRegistry) -> None:
        people_tools = populated_registry.get_tools_by_group("apollo", "people")
        assert len(people_tools) == 2
        names = {t.name for t in people_tools}
        assert names == {"apollo_people_search", "apollo_people_enrich"}

    def test_get_tools_by_group_unknown(self, populated_registry: ToolRegistry) -> None:
        assert populated_registry.get_tools_by_group("apollo", "nonexistent") == []
        assert populated_registry.get_tools_by_group("nonexistent", "people") == []

    def test_results_sorted_by_name(self, populated_registry: ToolRegistry) -> None:
        tools = populated_registry.get_tools_by_domain("apollo")
        names = [t.name for t in tools]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# ToolRegistry — search
# ---------------------------------------------------------------------------


class TestRegistrySearch:
    def test_single_token(self, populated_registry: ToolRegistry) -> None:
        results = populated_registry.search("enrich")
        names = {t.name for t in results}
        assert names == {"apollo_people_enrich", "apollo_org_enrich"}

    def test_multi_token_and_semantics(self, populated_registry: ToolRegistry) -> None:
        results = populated_registry.search("search contacts")
        assert len(results) == 1
        assert results[0].name == "hubspot_contacts_search"

    def test_case_insensitive(self, populated_registry: ToolRegistry) -> None:
        results = populated_registry.search("ENRICH")
        assert len(results) == 2

    def test_no_results(self, populated_registry: ToolRegistry) -> None:
        assert populated_registry.search("nonexistent_xyz") == []

    def test_empty_query(self, populated_registry: ToolRegistry) -> None:
        results = populated_registry.search("")
        assert len(results) == populated_registry.tool_count

    def test_search_matches_description(self, populated_registry: ToolRegistry) -> None:
        results = populated_registry.search("firmographic")
        assert len(results) == 1
        assert results[0].name == "apollo_org_enrich"


# ---------------------------------------------------------------------------
# ToolRegistry — clear_domain
# ---------------------------------------------------------------------------


class TestRegistryClearDomain:
    def test_clear_removes_tools(self, populated_registry: ToolRegistry) -> None:
        populated_registry.clear_domain("apollo")

        assert not populated_registry.has_domain("apollo")
        assert populated_registry.lookup("apollo_people_search") is None
        # HubSpot tools remain
        assert populated_registry.has_domain("hubspot")
        assert populated_registry.tool_count == 3

    def test_clear_removes_description(self, populated_registry: ToolRegistry) -> None:
        populated_registry.clear_domain("apollo")
        info = populated_registry.get_domain_info()
        domain_names = [d.name for d in info]
        assert "apollo" not in domain_names

    def test_clear_nonexistent_domain(self, populated_registry: ToolRegistry) -> None:
        """Clearing a domain that doesn't exist is a no-op."""
        count_before = populated_registry.tool_count
        populated_registry.clear_domain("nonexistent")
        assert populated_registry.tool_count == count_before


# ---------------------------------------------------------------------------
# ToolRegistry — populate_domain
# ---------------------------------------------------------------------------


class TestPopulateDomain:
    def test_populate_basic(self, empty_registry: ToolRegistry) -> None:
        raw_tools = [
            {
                "name": "acme_users_list",
                "description": "List all users",
                "inputSchema": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer"}},
                },
            },
            {
                "name": "acme_users_create",
                "description": "Create a user",
                "inputSchema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            },
            {
                "name": "acme_billing_invoice",
                "description": "Generate an invoice",
                "inputSchema": {"type": "object"},
            },
        ]

        count = empty_registry.populate_domain(
            domain="acme",
            upstream_url="http://acme:8080/mcp",
            tools=raw_tools,
            description="Acme Corp API",
        )

        assert count == 3
        assert empty_registry.tool_count == 3
        assert empty_registry.get_domain_names() == ["acme"]
        assert set(empty_registry.get_groups_for_domain("acme")) == {"billing", "users"}

        # Check a specific tool
        tool = empty_registry.lookup("acme_users_list")
        assert tool is not None
        assert tool.domain == "acme"
        assert tool.group == "users"
        assert tool.description == "List all users"
        assert tool.upstream_url == "http://acme:8080/mcp"

    def test_populate_infers_groups(self, empty_registry: ToolRegistry) -> None:
        raw_tools = [
            {"name": "mydom_alpha_one", "inputSchema": {}},
            {"name": "mydom_alpha_two", "inputSchema": {}},
            {"name": "mydom_beta_three", "inputSchema": {}},
        ]
        empty_registry.populate_domain("mydom", "http://x:8080/mcp", raw_tools)

        assert set(empty_registry.get_groups_for_domain("mydom")) == {"alpha", "beta"}

    def test_populate_fallback_group(self, empty_registry: ToolRegistry) -> None:
        """Tools without the domain prefix get assigned to 'general'."""
        raw_tools = [
            {"name": "standalone_tool", "inputSchema": {}},
            {"name": "another_one", "inputSchema": {}},
        ]
        empty_registry.populate_domain("mydom", "http://x:8080/mcp", raw_tools)

        assert empty_registry.get_groups_for_domain("mydom") == ["general"]

    def test_populate_with_group_overrides(self, empty_registry: ToolRegistry) -> None:
        raw_tools = [
            {"name": "svc_foo_bar", "inputSchema": {}},
            {"name": "svc_foo_baz", "inputSchema": {}},
        ]
        empty_registry.populate_domain(
            "svc",
            "http://x:8080/mcp",
            raw_tools,
            group_overrides={"svc_foo_bar": "custom_group"},
        )

        tool_bar = empty_registry.lookup("svc_foo_bar")
        tool_baz = empty_registry.lookup("svc_foo_baz")
        assert tool_bar is not None and tool_bar.group == "custom_group"
        assert tool_baz is not None and tool_baz.group == "foo"

    def test_populate_clears_previous(self, empty_registry: ToolRegistry) -> None:
        """Re-populating a domain replaces its tools entirely."""
        empty_registry.populate_domain(
            "dom",
            "http://x:8080/mcp",
            [{"name": "dom_old_tool", "inputSchema": {}}],
        )
        assert empty_registry.lookup("dom_old_tool") is not None

        empty_registry.populate_domain(
            "dom",
            "http://x:8080/mcp",
            [{"name": "dom_new_tool", "inputSchema": {}}],
        )

        assert empty_registry.lookup("dom_old_tool") is None
        assert empty_registry.lookup("dom_new_tool") is not None
        assert empty_registry.tool_count == 1

    def test_populate_skips_empty_names(self, empty_registry: ToolRegistry) -> None:
        raw_tools = [
            {"name": "", "inputSchema": {}},
            {"name": "valid_tool", "inputSchema": {}},
        ]
        count = empty_registry.populate_domain("dom", "http://x:8080/mcp", raw_tools)
        assert count == 1
        assert empty_registry.tool_count == 1

    def test_populate_missing_fields_use_defaults(self, empty_registry: ToolRegistry) -> None:
        """Tools with missing description/inputSchema get sensible defaults."""
        raw_tools = [{"name": "dom_minimal"}]
        empty_registry.populate_domain("dom", "http://x:8080/mcp", raw_tools)

        tool = empty_registry.lookup("dom_minimal")
        assert tool is not None
        assert tool.description == ""
        assert tool.input_schema == {}

    def test_populate_empty_tool_list(self, empty_registry: ToolRegistry) -> None:
        count = empty_registry.populate_domain(
            "empty",
            "http://x:8080/mcp",
            [],
            description="An empty upstream",
        )
        assert count == 0
        # Domain shouldn't appear since it has no tools
        assert not empty_registry.has_domain("empty")

    def test_populate_sets_description(self, empty_registry: ToolRegistry) -> None:
        empty_registry.populate_domain(
            "dom",
            "http://x:8080/mcp",
            [{"name": "dom_tool", "inputSchema": {}}],
            description="My domain description",
        )
        info = empty_registry.get_domain_info()
        assert len(info) == 1
        assert info[0].description == "My domain description"


# ---------------------------------------------------------------------------
# ToolRegistry — get_all_tool_names
# ---------------------------------------------------------------------------


class TestGetAllToolNames:
    def test_returns_sorted(self, populated_registry: ToolRegistry) -> None:
        names = populated_registry.get_all_tool_names()
        assert names == sorted(names)
        assert len(names) == 7

    def test_empty_registry(self, empty_registry: ToolRegistry) -> None:
        assert empty_registry.get_all_tool_names() == []

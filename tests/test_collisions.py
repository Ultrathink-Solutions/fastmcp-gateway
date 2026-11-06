"""Tests for tool name collision handling across domains."""

from __future__ import annotations

from fastmcp_gateway.registry import ToolEntry, ToolRegistry


def _make_tool(name: str, domain: str, *, group: str = "general") -> ToolEntry:
    return ToolEntry(
        name=name,
        domain=domain,
        group=group,
        description=f"{name} from {domain}",
        input_schema={"type": "object"},
        upstream_url=f"http://{domain}:8080/mcp",
    )


class TestCollisionDetection:
    def test_no_collision_different_names(self, empty_registry: ToolRegistry) -> None:
        """Tools with different names register normally."""
        empty_registry.register_tool(_make_tool("search_contacts", "crm"))
        empty_registry.register_tool(_make_tool("send_email", "marketing"))

        assert empty_registry.lookup("search_contacts") is not None
        assert empty_registry.lookup("send_email") is not None
        assert empty_registry.tool_count == 2

    def test_same_domain_overwrites(self, empty_registry: ToolRegistry) -> None:
        """Re-registering from the same domain just updates the tool."""
        empty_registry.register_tool(_make_tool("search", "crm", group="contacts"))
        empty_registry.register_tool(_make_tool("search", "crm", group="deals"))

        assert empty_registry.tool_count == 1
        assert empty_registry.lookup("search") is not None
        assert empty_registry.lookup("search").group == "deals"  # type: ignore[union-attr]

    def test_two_domains_both_prefixed(self, empty_registry: ToolRegistry) -> None:
        """Two domains registering the same name both get prefixed."""
        empty_registry.register_tool(_make_tool("search_contacts", "crm"))
        empty_registry.register_tool(_make_tool("search_contacts", "marketing"))

        # Original name no longer exists
        assert empty_registry.lookup("search_contacts") is None

        # Both exist with domain prefix
        crm = empty_registry.lookup("crm_search_contacts")
        mkt = empty_registry.lookup("marketing_search_contacts")
        assert crm is not None
        assert mkt is not None
        assert crm.domain == "crm"
        assert mkt.domain == "marketing"
        assert crm.original_name == "search_contacts"
        assert mkt.original_name == "search_contacts"
        assert empty_registry.tool_count == 2

    def test_third_domain_auto_prefixed(self, empty_registry: ToolRegistry) -> None:
        """A third domain with the same name is also auto-prefixed."""
        empty_registry.register_tool(_make_tool("search_contacts", "crm"))
        empty_registry.register_tool(_make_tool("search_contacts", "marketing"))
        empty_registry.register_tool(_make_tool("search_contacts", "sales"))

        assert empty_registry.lookup("crm_search_contacts") is not None
        assert empty_registry.lookup("marketing_search_contacts") is not None
        assert empty_registry.lookup("sales_search_contacts") is not None
        assert empty_registry.lookup("sales_search_contacts").original_name == "search_contacts"  # type: ignore[union-attr]
        assert empty_registry.tool_count == 3

    def test_secondary_collision_does_not_overwrite(self, empty_registry: ToolRegistry) -> None:
        """Prefixed name colliding with an existing tool does not overwrite it."""
        # Domain "a" registers "b_c" — stored normally
        empty_registry.register_tool(_make_tool("b_c", "a"))
        assert empty_registry.lookup("b_c") is not None
        assert empty_registry.lookup("b_c").domain == "a"  # type: ignore[union-attr]

        # Domain "b" registers "c" — stored normally
        empty_registry.register_tool(_make_tool("c", "b"))

        # Domain "a_b" registers "c" — collides with domain "b"'s "c".
        # Normal collision handling would rename both to "b_c" and "a_b_c",
        # but "b_c" already exists from domain "a".  The pre-check must
        # detect this and keep domain "b"'s tool under its original name.
        empty_registry.register_tool(_make_tool("c", "a_b"))

        # Domain "a"'s original tool must still be intact
        original = empty_registry.lookup("b_c")
        assert original is not None
        assert original.domain == "a"

        # Domain "b"'s tool is kept under its original name (cannot be prefixed)
        b_tool = empty_registry.lookup("c")
        assert b_tool is not None
        assert b_tool.domain == "b"

        # Domain "a_b"'s tool should still be registered with prefix
        assert empty_registry.lookup("a_b_c") is not None
        assert empty_registry.lookup("a_b_c").domain == "a_b"  # type: ignore[union-attr]

        # No tools lost — all three domains have exactly one tool
        assert empty_registry.tool_count == 3

    def test_same_domain_update_after_secondary_collision(self, empty_registry: ToolRegistry) -> None:
        """Same-domain re-registration still works after a secondary collision."""
        # Set up the secondary collision scenario
        empty_registry.register_tool(_make_tool("b_c", "a"))
        empty_registry.register_tool(_make_tool("c", "b"))
        empty_registry.register_tool(_make_tool("c", "a_b"))

        # Domain "b" re-registers "c" — must succeed as a same-domain update
        updated = _make_tool("c", "b", group="updated")
        empty_registry.register_tool(updated)

        b_tool = empty_registry.lookup("c")
        assert b_tool is not None
        assert b_tool.domain == "b"
        assert b_tool.group == "updated"

        # Other tools remain untouched
        assert empty_registry.lookup("b_c") is not None
        assert empty_registry.lookup("b_c").domain == "a"  # type: ignore[union-attr]
        assert empty_registry.lookup("a_b_c") is not None
        assert empty_registry.tool_count == 3


class TestCollisionSearch:
    def test_search_by_original_name(self, empty_registry: ToolRegistry) -> None:
        """Search finds renamed tools via their original name."""
        empty_registry.register_tool(_make_tool("search_contacts", "crm"))
        empty_registry.register_tool(_make_tool("search_contacts", "marketing"))

        results = empty_registry.search("search_contacts")
        names = {t.name for t in results}
        assert names == {"crm_search_contacts", "marketing_search_contacts"}

    def test_search_by_prefixed_name(self, empty_registry: ToolRegistry) -> None:
        """Search also works with the domain-prefixed name."""
        empty_registry.register_tool(_make_tool("search_contacts", "crm"))
        empty_registry.register_tool(_make_tool("search_contacts", "marketing"))

        results = empty_registry.search("crm_search")
        assert len(results) == 1
        assert results[0].name == "crm_search_contacts"


class TestCollisionWithPopulateDomain:
    def test_populate_detects_collisions(self, empty_registry: ToolRegistry) -> None:
        """populate_domain correctly handles collisions with existing tools."""
        empty_registry.populate_domain(
            "crm",
            "http://crm:8080/mcp",
            [{"name": "list_items", "inputSchema": {"type": "object"}}],
        )
        empty_registry.populate_domain(
            "marketing",
            "http://marketing:8080/mcp",
            [{"name": "list_items", "inputSchema": {"type": "object"}}],
        )

        assert empty_registry.lookup("list_items") is None
        assert empty_registry.lookup("crm_list_items") is not None
        assert empty_registry.lookup("marketing_list_items") is not None


class TestCollisionDomainIndex:
    def test_prefixed_tools_appear_in_domain(self, empty_registry: ToolRegistry) -> None:
        """Prefixed tools still belong to their original domain."""
        empty_registry.register_tool(_make_tool("search", "crm"))
        empty_registry.register_tool(_make_tool("search", "marketing"))

        crm_tools = empty_registry.get_tools_by_domain("crm")
        mkt_tools = empty_registry.get_tools_by_domain("marketing")

        assert len(crm_tools) == 1
        assert crm_tools[0].name == "crm_search"
        assert len(mkt_tools) == 1
        assert mkt_tools[0].name == "marketing_search"

    def test_tool_count_preserved(self, empty_registry: ToolRegistry) -> None:
        """Collision renaming preserves total tool count."""
        empty_registry.register_tool(_make_tool("search", "crm"))
        assert empty_registry.tool_count == 1

        empty_registry.register_tool(_make_tool("search", "marketing"))
        assert empty_registry.tool_count == 2

        empty_registry.register_tool(_make_tool("search", "sales"))
        assert empty_registry.tool_count == 3


class TestCollisionArms:
    """``collision_arms`` returns the tools a cross-domain collision renamed."""

    def test_both_arms_in_domain_order(self, empty_registry: ToolRegistry) -> None:
        """Both renamed tools are returned, sorted by domain, with their original name."""
        empty_registry.register_tool(_make_tool("create_upload", "widgets"))
        empty_registry.register_tool(_make_tool("create_upload", "svc"))

        arms = empty_registry.collision_arms("create_upload")

        assert [(t.name, t.domain, t.original_name) for t in arms] == [
            ("svc_create_upload", "svc", "create_upload"),
            ("widgets_create_upload", "widgets", "create_upload"),
        ]

    def test_a_later_domain_joins_the_arms(self, empty_registry: ToolRegistry) -> None:
        """A third domain registering the collided name is renamed and joins the arms."""
        for domain in ("svc", "widgets", "acme"):
            empty_registry.register_tool(_make_tool("create_upload", domain))

        assert [t.name for t in empty_registry.collision_arms("create_upload")] == [
            "acme_create_upload",
            "svc_create_upload",
            "widgets_create_upload",
        ]

    def test_a_name_that_never_collided_has_no_arms(self, empty_registry: ToolRegistry) -> None:
        """A name no two domains registered, including a renamed tool's own name, has no arms."""
        empty_registry.register_tool(_make_tool("create_upload", "svc"))
        empty_registry.register_tool(_make_tool("create_upload", "widgets"))
        empty_registry.register_tool(_make_tool("list_widgets", "widgets"))

        assert empty_registry.collision_arms("list_widgets") == []
        assert empty_registry.collision_arms("delete_upload") == []
        # A renamed tool's own name is not a collided name.
        assert empty_registry.collision_arms("svc_create_upload") == []

    def test_an_arm_kept_under_its_bare_name_is_still_an_arm(self, empty_registry: ToolRegistry) -> None:
        """When the first arm cannot be renamed, it keeps the bare name and still counts."""
        empty_registry.register_tool(_make_tool("b_c", "a"))
        empty_registry.register_tool(_make_tool("c", "b"))
        empty_registry.register_tool(_make_tool("c", "a_b"))

        assert [(t.name, t.domain) for t in empty_registry.collision_arms("c")] == [("a_b_c", "a_b"), ("c", "b")]

    def test_a_cleared_domain_drops_out(self, empty_registry: ToolRegistry) -> None:
        """Clearing a domain removes its arm, and clearing both leaves none."""
        empty_registry.register_tool(_make_tool("create_upload", "svc"))
        empty_registry.register_tool(_make_tool("create_upload", "widgets"))

        empty_registry.clear_domain("widgets")
        assert [t.name for t in empty_registry.collision_arms("create_upload")] == ["svc_create_upload"]

        empty_registry.clear_domain("svc")
        assert empty_registry.collision_arms("create_upload") == []

    def test_a_refresh_keeps_the_arms(self, empty_registry: ToolRegistry) -> None:
        """Re-populating a domain with the same tools keeps both arms under their renamed names."""
        payload = [{"name": "create_upload", "inputSchema": {"type": "object"}}]
        empty_registry.populate_domain("svc", "http://svc:8080/mcp", payload)
        empty_registry.populate_domain("widgets", "http://widgets:8080/mcp", payload)

        diff = empty_registry.populate_domain("svc", "http://svc:8080/mcp", payload)

        assert not diff.refused
        assert [(t.name, t.domain) for t in empty_registry.collision_arms("create_upload")] == [
            ("svc_create_upload", "svc"),
            ("widgets_create_upload", "widgets"),
        ]

    def test_a_refresh_that_drops_the_tool_drops_its_arm(self, empty_registry: ToolRegistry) -> None:
        """An acknowledged refresh that no longer lists the tool removes that domain's arm."""
        payload = [{"name": "create_upload", "inputSchema": {"type": "object"}}]
        empty_registry.populate_domain("svc", "http://svc:8080/mcp", payload)
        empty_registry.populate_domain("widgets", "http://widgets:8080/mcp", payload)
        changed = [{"name": "list_widgets", "inputSchema": {"type": "object"}}]
        digest = empty_registry.prepare_domain("widgets", "http://widgets:8080/mcp", changed).diff.schema_digest

        diff = empty_registry.populate_domain("widgets", "http://widgets:8080/mcp", changed, expected_digest=digest)

        assert not diff.refused
        assert [t.name for t in empty_registry.collision_arms("create_upload")] == ["svc_create_upload"]

    def test_arms_are_copies(self, empty_registry: ToolRegistry) -> None:
        """Mutating a returned arm does not change the registry."""
        empty_registry.register_tool(_make_tool("create_upload", "svc"))
        empty_registry.register_tool(_make_tool("create_upload", "widgets"))

        arm = empty_registry.collision_arms("create_upload")[0]
        arm.input_schema["mutated"] = True

        assert "mutated" not in empty_registry.collision_arms("create_upload")[0].input_schema

"""Shared test fixtures for fastmcp-gateway tests."""

from __future__ import annotations

import pytest

from fastmcp_gateway.registry import ToolEntry, ToolRegistry


@pytest.fixture
def empty_registry() -> ToolRegistry:
    """An empty tool registry."""
    return ToolRegistry()


@pytest.fixture
def populated_registry() -> ToolRegistry:
    """A registry with sample tools across multiple domains."""
    registry = ToolRegistry()

    # Apollo domain
    registry.set_domain_description("apollo", "Apollo.io CRM and sales intelligence")
    for name, group, desc in [
        ("apollo_people_search", "people", "Search for people by name, title, company, or other criteria"),
        ("apollo_people_enrich", "people", "Enrich a person record with full contact and company data"),
        ("apollo_org_search", "organizations", "Search for organizations by name, industry, or size"),
        ("apollo_org_enrich", "organizations", "Enrich an organization with firmographic data"),
    ]:
        registry.register_tool(
            ToolEntry(
                name=name,
                domain="apollo",
                group=group,
                description=desc,
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                upstream_url="http://apollo-mcp:8080/mcp",
            )
        )

    # HubSpot domain
    registry.set_domain_description("hubspot", "HubSpot CRM and marketing")
    for name, group, desc in [
        ("hubspot_contacts_search", "contacts", "Search HubSpot contacts by name, email, or properties"),
        ("hubspot_contacts_create", "contacts", "Create a new contact in HubSpot"),
        ("hubspot_deals_list", "deals", "List deals with optional filters"),
    ]:
        registry.register_tool(
            ToolEntry(
                name=name,
                domain="hubspot",
                group=group,
                description=desc,
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
                upstream_url="http://hubspot-mcp:8080/mcp",
            )
        )

    return registry

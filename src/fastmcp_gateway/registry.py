"""Tool registry: in-memory store for discovered tools from upstream MCP servers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def infer_group(domain: str, tool_name: str) -> str:
    """Infer a tool's group from its name by stripping the domain prefix.

    Convention: tool names follow ``{domain}_{group}_{action}`` pattern.
    Examples:
        infer_group("apollo", "apollo_people_search")   -> "people"
        infer_group("hubspot", "hubspot_contacts_create") -> "contacts"
        infer_group("apollo", "search")                  -> "general"
    """
    prefix = f"{domain}_"
    if tool_name.startswith(prefix):
        remainder = tool_name[len(prefix) :]
        parts = remainder.split("_", 1)
        if parts[0]:
            return parts[0]
    return "general"


@dataclass
class ToolEntry:
    """A single tool in the registry."""

    name: str
    domain: str
    group: str
    description: str
    input_schema: dict[str, Any]
    upstream_url: str


@dataclass
class DomainInfo:
    """Summary information about a domain."""

    name: str
    description: str
    groups: list[str]
    tool_count: int


class ToolRegistry:
    """In-memory tool registry with domain/group organization.

    Stores tool metadata from upstream MCP servers and provides
    lookup, filtering, and search operations.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}
        self._domains: dict[str, dict[str, list[str]]] = {}  # domain -> group -> [tool_names]
        self._domain_descriptions: dict[str, str] = {}

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    def register_tool(self, tool: ToolEntry) -> None:
        """Register a single tool in the registry."""
        # Remove stale index entries if the tool was previously registered
        # under a different domain/group.
        old = self._tools.get(tool.name)
        if old is not None and (old.domain != tool.domain or old.group != tool.group):
            self._remove_from_index(old.name, old.domain, old.group)

        self._tools[tool.name] = tool

        if tool.domain not in self._domains:
            self._domains[tool.domain] = {}
        if tool.group not in self._domains[tool.domain]:
            self._domains[tool.domain][tool.group] = []
        if tool.name not in self._domains[tool.domain][tool.group]:
            self._domains[tool.domain][tool.group].append(tool.name)

    def _remove_from_index(self, tool_name: str, domain: str, group: str) -> None:
        """Remove a tool name from the domain/group index."""
        if domain in self._domains and group in self._domains[domain]:
            names = self._domains[domain][group]
            if tool_name in names:
                names.remove(tool_name)
            # Clean up empty group
            if not names:
                del self._domains[domain][group]
            # Clean up empty domain
            if not self._domains[domain]:
                del self._domains[domain]

    def populate_domain(
        self,
        domain: str,
        upstream_url: str,
        tools: list[dict[str, Any]],
        *,
        description: str = "",
        group_overrides: dict[str, str] | None = None,
    ) -> int:
        """Populate the registry with tools from an upstream server.

        Each tool dict should have at minimum ``name`` and ``inputSchema`` keys,
        matching the shape returned by MCP ``tools/list``.  An optional
        ``description`` key provides the tool's one-line summary.

        Groups are inferred from tool name prefixes unless overridden via
        *group_overrides* (mapping tool name -> explicit group).

        Returns the number of tools registered.
        """
        self.clear_domain(domain)

        if description:
            self.set_domain_description(domain, description)

        overrides = group_overrides or {}
        count = 0
        for raw in tools:
            name: str = raw.get("name", "")
            if not name:
                logger.warning("Skipping tool with empty name in domain %s", domain)
                continue

            group = overrides.get(name, infer_group(domain, name))

            self.register_tool(
                ToolEntry(
                    name=name,
                    domain=domain,
                    group=group,
                    description=raw.get("description", ""),
                    input_schema=raw.get("inputSchema", {}),
                    upstream_url=upstream_url,
                )
            )
            count += 1

        return count

    def set_domain_description(self, domain: str, description: str) -> None:
        """Set a human-readable description for a domain."""
        self._domain_descriptions[domain] = description

    def clear_domain(self, domain: str) -> None:
        """Remove all tools for a domain (used during refresh)."""
        if domain in self._domains:
            for group_tools in self._domains[domain].values():
                for tool_name in group_tools:
                    self._tools.pop(tool_name, None)
            del self._domains[domain]
        self._domain_descriptions.pop(domain, None)

    def lookup(self, tool_name: str) -> ToolEntry | None:
        """Look up a tool by exact name."""
        return self._tools.get(tool_name)

    def get_domain_names(self) -> list[str]:
        """Get all registered domain names."""
        return sorted(self._domains.keys())

    def get_domain_info(self) -> list[DomainInfo]:
        """Get summary info for all domains."""
        result = []
        for domain_name in sorted(self._domains.keys()):
            groups = self._domains[domain_name]
            tool_count = sum(len(tools) for tools in groups.values())
            result.append(
                DomainInfo(
                    name=domain_name,
                    description=self._domain_descriptions.get(domain_name, ""),
                    groups=sorted(groups.keys()),
                    tool_count=tool_count,
                )
            )
        return result

    def get_tools_by_domain(self, domain: str) -> list[ToolEntry]:
        """Get all tools in a domain."""
        if domain not in self._domains:
            return []
        tool_names = []
        for group_tools in self._domains[domain].values():
            tool_names.extend(group_tools)
        return [self._tools[name] for name in sorted(tool_names) if name in self._tools]

    def get_tools_by_group(self, domain: str, group: str) -> list[ToolEntry]:
        """Get all tools in a specific domain/group."""
        if domain not in self._domains or group not in self._domains[domain]:
            return []
        tool_names = self._domains[domain][group]
        return [self._tools[name] for name in sorted(tool_names) if name in self._tools]

    def search(self, query: str) -> list[ToolEntry]:
        """Keyword search across tool names and descriptions.

        All whitespace-separated tokens must appear somewhere in the
        tool's name or description (AND semantics).
        """
        query_lower = query.lower()
        tokens = query_lower.split()
        results = []
        for tool in self._tools.values():
            searchable = f"{tool.name} {tool.description}".lower()
            if all(token in searchable for token in tokens):
                results.append(tool)
        return sorted(results, key=lambda t: t.name)

    def get_all_tool_names(self) -> list[str]:
        """Get all registered tool names (for fuzzy matching)."""
        return sorted(self._tools.keys())

    def has_domain(self, domain: str) -> bool:
        return domain in self._domains

    def has_group(self, domain: str, group: str) -> bool:
        return domain in self._domains and group in self._domains[domain]

    def get_groups_for_domain(self, domain: str) -> list[str]:
        """Get group names for a domain."""
        if domain not in self._domains:
            return []
        return sorted(self._domains[domain].keys())

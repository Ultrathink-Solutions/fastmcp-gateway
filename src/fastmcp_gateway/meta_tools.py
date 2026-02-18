"""Meta-tools: the 3 tools exposed to the LLM by the gateway."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from fastmcp_gateway.client_manager import UpstreamManager
    from fastmcp_gateway.registry import ToolRegistry


def _suggest_tool_names(query: str, all_names: list[str], max_suggestions: int = 3) -> list[str]:
    """Return tool names similar to *query* for error messages.

    Scores based on substring containment, shared prefix segments,
    and shared word segments (order-independent).
    """
    query_lower = query.lower()
    q_parts = set(query_lower.split("_"))
    scored: list[tuple[int, str]] = []
    for name in all_names:
        name_lower = name.lower()
        score = 0
        # Substring match (either direction)
        if query_lower in name_lower or name_lower in query_lower:
            score += 3
        # Shared word segments (order-independent)
        n_parts = set(name_lower.split("_"))
        shared = q_parts & n_parts
        score += len(shared)
        if score > 0:
            scored.append((score, name))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [name for _, name in scored[:max_suggestions]]


def register_meta_tools(mcp: FastMCP, registry: ToolRegistry, upstream_manager: UpstreamManager) -> None:
    """Register the 3 meta-tools on the FastMCP server."""

    @mcp.tool()
    async def discover_tools(
        domain: str | None = None,
        group: str | None = None,
        query: str | None = None,
    ) -> str:
        """Browse available tools by domain, group, or keyword.

        Call with no arguments to see all available domains and their tool counts.
        Call with a domain to see groups and tools within that domain.
        Call with a domain and group to see tools in that specific group.
        Call with a query to search across all tools by keyword.
        """
        # Mode 4: keyword search (takes priority when query is provided)
        if query is not None and query.strip():
            results = registry.search(query)
            return json.dumps(
                {
                    "query": query,
                    "results": [
                        {
                            "name": t.name,
                            "domain": t.domain,
                            "group": t.group,
                            "description": t.description,
                        }
                        for t in results
                    ],
                }
            )

        # Mode 1: no arguments -> domain summary
        if domain is None:
            domain_info = registry.get_domain_info()
            return json.dumps(
                {
                    "domains": [
                        {
                            "name": d.name,
                            "description": d.description,
                            "tool_count": d.tool_count,
                            "groups": d.groups,
                        }
                        for d in domain_info
                    ],
                    "total_tools": registry.tool_count,
                }
            )

        # Validate domain
        if not registry.has_domain(domain):
            available = registry.get_domain_names()
            return json.dumps(
                {
                    "error": f"Unknown domain '{domain}'. Available domains: {', '.join(available)}"
                    if available
                    else f"Unknown domain '{domain}'. No domains are registered."
                }
            )

        # Mode 3: domain + group -> tools in that group
        if group is not None:
            if not registry.has_group(domain, group):
                available_groups = registry.get_groups_for_domain(domain)
                return json.dumps(
                    {
                        "error": f"Unknown group '{group}' in domain '{domain}'. "
                        f"Available groups: {', '.join(available_groups)}"
                    }
                )
            tools = registry.get_tools_by_group(domain, group)
            return json.dumps(
                {
                    "domain": domain,
                    "group": group,
                    "tools": [{"name": t.name, "description": t.description} for t in tools],
                }
            )

        # Mode 2: domain only -> all tools in domain
        tools = registry.get_tools_by_domain(domain)
        return json.dumps(
            {
                "domain": domain,
                "tools": [
                    {
                        "name": t.name,
                        "group": t.group,
                        "description": t.description,
                    }
                    for t in tools
                ],
            }
        )

    @mcp.tool()
    async def get_tool_schema(tool_name: str) -> str:
        """Get the full parameter schema for a specific tool.

        Call this after discover_tools to get the complete input schema
        before calling execute_tool. Returns the JSON Schema that describes
        what arguments the tool accepts.
        """
        entry = registry.lookup(tool_name)
        if entry is not None:
            return json.dumps(
                {
                    "name": entry.name,
                    "domain": entry.domain,
                    "group": entry.group,
                    "description": entry.description,
                    "parameters": entry.input_schema,
                }
            )

        # Unknown tool — suggest similar names
        suggestions = _suggest_tool_names(tool_name, registry.get_all_tool_names())
        if suggestions:
            hint = f"Did you mean {', '.join(repr(s) for s in suggestions)}?"
        else:
            hint = "Use discover_tools to browse available tools."
        return json.dumps({"error": f"Unknown tool '{tool_name}'. {hint}"})

    @mcp.tool()
    async def execute_tool(
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Execute a tool by name with the given arguments.

        Use discover_tools to find available tools, then get_tool_schema
        to see what arguments a tool accepts, then call this to execute it.
        """
        raise NotImplementedError("execute_tool not yet implemented")

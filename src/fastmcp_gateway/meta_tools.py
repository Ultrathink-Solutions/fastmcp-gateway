"""Meta-tools: the 3 tools exposed to the LLM by the gateway."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from fastmcp_gateway.client_manager import UpstreamManager
    from fastmcp_gateway.registry import ToolRegistry


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
        raise NotImplementedError("discover_tools not yet implemented")

    @mcp.tool()
    async def get_tool_schema(tool_name: str) -> str:
        """Get the full parameter schema for a specific tool.

        Call this after discover_tools to get the complete input schema
        before calling execute_tool. Returns the JSON Schema that describes
        what arguments the tool accepts.
        """
        raise NotImplementedError("get_tool_schema not yet implemented")

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

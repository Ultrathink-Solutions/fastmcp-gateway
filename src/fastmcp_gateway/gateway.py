"""Gateway server: the main entry point for fastmcp-gateway."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from fastmcp_gateway.registry import ToolRegistry


class GatewayServer:
    """Progressive tool discovery gateway for MCP.

    Aggregates tools from multiple upstream MCP servers and exposes them
    through 3 meta-tools: discover_tools, get_tool_schema, execute_tool.

    Usage:
        gateway = GatewayServer({
            "apollo": "http://apollo-mcp:8080/mcp",
            "hubspot": "http://hubspot-mcp:8080/mcp",
        })
        gateway.run()
    """

    def __init__(
        self,
        upstreams: dict[str, str],
        *,
        name: str = "fastmcp-gateway",
        instructions: str | None = None,
    ) -> None:
        self.upstreams = upstreams
        self.registry = ToolRegistry()
        self._mcp = FastMCP(
            name,
            instructions=instructions if instructions is not None else self._default_instructions(),
        )
        self._register_meta_tools()

    @property
    def mcp(self) -> FastMCP:
        """Access the underlying FastMCP server instance."""
        return self._mcp

    def run(self, **kwargs: Any) -> None:
        """Run the gateway server."""
        self._mcp.run(**kwargs)

    def _register_meta_tools(self) -> None:
        """Register the 3 meta-tools on the FastMCP server."""
        # Import here to avoid circular imports
        from fastmcp_gateway.meta_tools import register_meta_tools

        register_meta_tools(self._mcp, self.registry, self.upstreams)

    @staticmethod
    def _default_instructions() -> str:
        return (
            "You have access to a tool discovery gateway with 3 tools:\n"
            "1. discover_tools - Browse available tools. Call with no arguments to see domains, "
            "or with a domain to see specific tools.\n"
            "2. get_tool_schema - Get a tool's parameter schema before using it.\n"
            "3. execute_tool - Run any discovered tool.\n"
            "Workflow: discover_tools -> get_tool_schema -> execute_tool. "
            "Skip discovery for tools you've already used in this conversation."
        )

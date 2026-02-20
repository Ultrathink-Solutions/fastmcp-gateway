"""Progressive tool discovery gateway for MCP, built on FastMCP."""

from fastmcp_gateway.errors import GatewayError
from fastmcp_gateway.gateway import GatewayServer

__all__ = ["GatewayError", "GatewayServer"]
__version__ = "0.1.0"

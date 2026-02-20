"""Progressive tool discovery gateway for MCP, built on FastMCP."""

from fastmcp_gateway.client_manager import get_user_headers
from fastmcp_gateway.errors import GatewayError
from fastmcp_gateway.gateway import GatewayServer

__all__ = ["GatewayError", "GatewayServer", "get_user_headers"]
__version__ = "0.1.0"

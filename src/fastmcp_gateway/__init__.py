"""Progressive tool discovery gateway for MCP, built on FastMCP."""

from fastmcp_gateway.client_manager import get_user_headers
from fastmcp_gateway.errors import GatewayError
from fastmcp_gateway.gateway import GatewayServer
from fastmcp_gateway.hooks import ExecutionContext, ExecutionDenied, Hook, HookRunner

__all__ = [
    "ExecutionContext",
    "ExecutionDenied",
    "GatewayError",
    "GatewayServer",
    "Hook",
    "HookRunner",
    "get_user_headers",
]
__version__ = "0.3.0"

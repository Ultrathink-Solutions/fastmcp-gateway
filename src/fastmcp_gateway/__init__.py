"""Progressive tool discovery gateway for MCP, built on FastMCP."""

from fastmcp_gateway.access_policy import AccessPolicy
from fastmcp_gateway.client_manager import get_user_headers
from fastmcp_gateway.errors import GatewayError
from fastmcp_gateway.gateway import GatewayServer
from fastmcp_gateway.hooks import ExecutionContext, ExecutionDenied, Hook, HookRunner, ListToolsContext

# Note: ``CodeModeAuthorizerRequiredError`` is deliberately not re-exported
# at the package root. It's an internal routing signal between
# ``gateway.py`` (which raises it when ``code_mode=True`` is supplied
# without an authorizer) and ``__main__.py`` (which catches it to emit
# a CLI-friendly operator message). External callers who want to catch
# it can still ``from fastmcp_gateway.gateway import
# CodeModeAuthorizerRequiredError``; it's also a ``ValueError`` subclass,
# so broad ``except ValueError`` blocks continue to work.
__all__ = [
    "AccessPolicy",
    "ExecutionContext",
    "ExecutionDenied",
    "GatewayError",
    "GatewayServer",
    "Hook",
    "HookRunner",
    "ListToolsContext",
    "get_user_headers",
]
__version__ = "0.13.0"

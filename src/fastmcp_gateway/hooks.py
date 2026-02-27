"""Execution hooks: lifecycle callbacks around the tool execution pipeline.

The hooks system enables middleware-style interception of tool execution
without adding any new dependencies.  Implement any subset of the
:class:`Hook` protocol methods and register via
``GatewayServer(hooks=[...])`` or ``gateway.add_hook(hook)``.

Lifecycle order for ``execute_tool``::

    on_authenticate(headers) -> ctx.user
    before_execute(context)  -> may raise ExecutionDenied
    upstream call
    after_execute(context, result, is_error) -> transformed result
    on_error(context, error) -> observability only (on exception)

Lifecycle order for ``discover_tools`` / ``get_tool_schema``::

    on_authenticate(headers) -> ctx.user
    registry lookup
    after_list_tools(tools, context) -> filtered tool list
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, runtime_checkable

from typing_extensions import Protocol

if TYPE_CHECKING:
    from fastmcp_gateway.registry import ToolEntry

logger = logging.getLogger(__name__)


@dataclass
class ExecutionContext:
    """Mutable carrier that flows through the hook pipeline for one tool execution.

    Attributes
    ----------
    tool:
        Resolved tool entry from the registry.
    arguments:
        Tool arguments -- hooks may modify in-place.
    headers:
        Incoming HTTP request headers (lowercase keys).
    user:
        User identity set by ``on_authenticate`` (type defined by hook).
    extra_headers:
        Additional headers forwarded to the upstream server.
    metadata:
        Hook-to-hook communication channel.
    """

    tool: ToolEntry
    arguments: dict[str, Any]
    headers: dict[str, str]
    user: Any | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ListToolsContext:
    """Context for tool list filtering in ``after_list_tools`` hooks.

    Attributes
    ----------
    domain:
        Domain being listed (``None`` when listing all domains or searching).
    headers:
        Incoming HTTP request headers (lowercase keys).
    user:
        User identity set by ``on_authenticate`` (type defined by hook).
    """

    domain: str | None
    headers: dict[str, str]
    user: Any | None = None


class ExecutionDenied(Exception):
    """Raised by hooks to deny tool execution.

    The gateway catches this and returns a structured error response.

    Attributes
    ----------
    message:
        Human-readable error returned to the client.
    code:
        Machine-readable error code (default: ``"forbidden"``).
    """

    def __init__(self, message: str, *, code: str = "forbidden") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@runtime_checkable
class Hook(Protocol):
    """Structural typing interface for execution hooks.

    Implement any subset of lifecycle methods.  All methods are async
    and optional -- simply omit the ones you don't need.

    Methods
    -------
    on_authenticate(headers)
        Called once per request before tool execution.  Return a user
        identity (any type) or ``None``.  Last non-None result wins
        across multiple hooks.
    before_execute(context)
        Called before each tool execution.  Raise :class:`ExecutionDenied`
        to block execution.  Modify ``context`` in-place for mutations.
    after_execute(context, result, is_error)
        Called after each tool execution.  Return a (possibly transformed)
        result string.  Each hook receives the previous hook's output.
    after_list_tools(tools, context)
        Called after ``discover_tools`` / ``get_tool_schema`` build their
        tool list.  Return a (possibly filtered) list.  Each hook receives
        the previous hook's output.
    on_error(context, error)
        Called when execution raises an exception.  Observability only --
        exceptions in hooks are logged, not raised.
    """

    async def on_authenticate(self, headers: dict[str, str]) -> Any | None: ...
    async def before_execute(self, context: ExecutionContext) -> None: ...
    async def after_execute(self, context: ExecutionContext, result: str, is_error: bool) -> str: ...
    async def after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]: ...
    async def on_error(self, context: ExecutionContext, error: Exception) -> None: ...


class HookRunner:
    """Manages hook registration and orchestrates lifecycle execution.

    Parameters
    ----------
    hooks:
        Optional initial list of hook instances.
    """

    def __init__(self, hooks: list[Any] | None = None) -> None:
        self._hooks: list[Any] = list(hooks) if hooks else []

    def add(self, hook: Any) -> None:
        """Register a hook (appended to the end of the list)."""
        self._hooks.append(hook)

    @property
    def has_hooks(self) -> bool:
        """Return True if any hooks are registered."""
        return len(self._hooks) > 0

    async def run_authenticate(self, headers: dict[str, str]) -> Any | None:
        """Execute all ``on_authenticate`` hooks.  Last non-None result wins."""
        user: Any | None = None
        for hook in self._hooks:
            method = getattr(hook, "on_authenticate", None)
            if method is not None:
                result = await method(headers)
                if result is not None:
                    user = result
        return user

    async def run_before_execute(self, context: ExecutionContext) -> None:
        """Execute all ``before_execute`` hooks.

        Any hook can raise :class:`ExecutionDenied` to stop the chain.
        """
        for hook in self._hooks:
            method = getattr(hook, "before_execute", None)
            if method is not None:
                await method(context)

    async def run_after_execute(self, context: ExecutionContext, result: str, is_error: bool) -> str:
        """Execute all ``after_execute`` hooks.  Pipelines the result string."""
        current = result
        for hook in self._hooks:
            method = getattr(hook, "after_execute", None)
            if method is not None:
                current = await method(context, current, is_error)
        return current

    async def run_after_list_tools(self, tools: list[ToolEntry], context: ListToolsContext) -> list[ToolEntry]:
        """Execute all ``after_list_tools`` hooks.  Pipelines the tool list."""
        current = list(tools)
        for hook in self._hooks:
            method = getattr(hook, "after_list_tools", None)
            if method is not None:
                current = await method(current, context)
        return current

    async def run_on_error(self, context: ExecutionContext, error: Exception) -> None:
        """Execute all ``on_error`` hooks.  Fault-tolerant: exceptions are logged."""
        for hook in self._hooks:
            method = getattr(hook, "on_error", None)
            if method is not None:
                try:
                    await method(context, error)
                except Exception:
                    logger.exception(
                        "Hook %s.on_error raised an exception (suppressed)",
                        type(hook).__name__,
                    )

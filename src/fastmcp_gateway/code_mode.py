"""Sandboxed Python orchestration across registered tools (experimental).

Expose every registered tool as a named async callable inside a Monty
sandbox so an LLM can write Python that chains tool calls in a single
round-trip (and use ``asyncio.gather`` to fan-out).  Collapses the
cost of multi-step agent flows where each intermediate payload would
otherwise have to pass through the agent's context window.

.. warning::

    This module is **experimental** and **off by default**.  It is not
    intended for large-payload analytical workloads -- those belong in
    a dedicated analytics server with a full Python sandbox.  The
    gateway's Monty sandbox is for *small-payload cross-tool chaining*
    (dozens of rows, kilobytes of JSON), not for crunching datasets.

Safety model
------------

- Every nested tool call runs the same ``before_execute`` /
  ``after_execute`` hook pipeline as a direct :func:`execute_tool`
  invocation, so access policies, authn/authz, and audit all apply
  identically.
- The *callable namespace* the sandbox sees is the result of
  ``after_list_tools`` hook filtering: tools a user can't see will
  not even appear as function names (no information leak).
- Outer-request headers and user identity are captured once at the
  boundary and closed over in each wrapper -- never read via
  ContextVar from inside Monty's worker thread.
- Resource limits (duration, memory, allocations, recursion depth,
  nested-call count) are enforced by Monty and by a per-call counter.
- Audit logging records hash + metadata by default; verbatim code is
  only written when explicitly opted-in via *audit_verbatim*.

Opting in
---------

Construct :class:`GatewayServer` with ``code_mode=True`` (plus an
optional ``code_mode_authorizer`` callback).  In env-var form, set
``GATEWAY_CODE_MODE=true``.  The :mod:`pydantic-monty` optional extra
must be installed (``pip install "fastmcp-gateway[code-mode]"``);
otherwise importing this module raises a clear ``ImportError``.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from fastmcp_gateway.hooks import ExecutionContext, ExecutionDenied, HookRunner, ListToolsContext

if TYPE_CHECKING:
    from fastmcp_gateway.client_manager import UpstreamManager
    from fastmcp_gateway.registry import ToolEntry, ToolRegistry


# Import pydantic-monty lazily so the import error surfaces at the
# point where code mode is actually used, not at module load time.
try:
    import pydantic_monty as _monty  # type: ignore[import-not-found]

    _MONTY_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the extra installed
    _monty = None  # type: ignore[assignment]
    _MONTY_AVAILABLE = False


logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("fastmcp_gateway.code_mode")


__all__ = [
    "CodeModeLimits",
    "CodeModeRunner",
    "CodeModeUnavailableError",
]


class CodeModeUnavailableError(RuntimeError):
    """Raised when code mode is requested but :mod:`pydantic-monty` is not installed."""


@dataclass(frozen=True)
class CodeModeLimits:
    """Resource caps applied to each ``execute_code`` invocation.

    All fields are optional; ``None`` means no limit on that axis.
    Values are passed straight to
    :class:`pydantic_monty.ResourceLimits` except *max_nested_calls*
    which is enforced by :class:`CodeModeRunner` itself (Monty doesn't
    count external-function invocations).
    """

    max_duration_secs: float | None = 30.0
    max_memory: int | None = 268_435_456  # 256 MiB
    max_allocations: int | None = 10_000_000
    max_recursion_depth: int | None = 200
    max_nested_calls: int | None = 50

    def to_monty(self) -> Any:
        """Project to the TypedDict shape Monty's ``limits`` kwarg expects.

        Return type is :class:`Any` so that callers don't take a hard
        dependency on :class:`pydantic_monty.ResourceLimits` — that type
        lives behind the optional ``code-mode`` extra.
        """
        out: dict[str, Any] = {}
        if self.max_duration_secs is not None:
            out["max_duration_secs"] = self.max_duration_secs
        if self.max_memory is not None:
            out["max_memory"] = self.max_memory
        if self.max_allocations is not None:
            out["max_allocations"] = self.max_allocations
        if self.max_recursion_depth is not None:
            out["max_recursion_depth"] = self.max_recursion_depth
        return out


AuthorizerFn = Callable[[Any, dict[str, Any]], Awaitable[bool]]
"""Session-level permission check.

Signature is intentionally typed with :class:`Any` and a plain dict so
enterprise extensions can bind their own identity / policy types without
those types leaking into the OSS surface.
"""


@dataclass
class _CodeModeAudit:
    """Structured metadata written to the audit log after each run."""

    code_session_id: str
    code_sha256: str
    code_byte_len: int
    code_line_count: int
    user_subject: Any
    step_count: int = 0
    tool_names_invoked: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    is_error: bool = False


class CodeModeRunner:
    """Runs LLM-authored Python against an authorized tool namespace.

    Parameters
    ----------
    registry:
        Shared tool registry.  Used to look up metadata for each named
        callable the sandbox exposes.
    upstream_manager:
        Used to route each nested tool call to its upstream, reusing
        the same code path as direct :func:`execute_tool` dispatch.
    hook_runner:
        Hook pipeline re-used per nested call for policy + audit.
    limits:
        Resource caps; see :class:`CodeModeLimits`.
    authorizer:
        Optional session-level permission check.  When provided and the
        callback returns ``False``, the run raises
        :class:`~fastmcp_gateway.hooks.ExecutionDenied` before entering
        the sandbox.  When ``None``, every authenticated caller may use
        code mode.  Enterprises bind this to their own policy engine.
    audit_verbatim:
        When ``True``, the full code body is recorded at DEBUG level in
        addition to the default hash+metadata INFO record.  Only enable
        for non-prod debugging; raw LLM-authored code is high-PII-risk.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        upstream_manager: UpstreamManager,
        hook_runner: HookRunner,
        *,
        limits: CodeModeLimits | None = None,
        authorizer: AuthorizerFn | None = None,
        audit_verbatim: bool = False,
    ) -> None:
        if not _MONTY_AVAILABLE:
            raise CodeModeUnavailableError(
                "Code mode requires pydantic-monty. Install the optional extra: "
                'pip install "fastmcp-gateway[code-mode]"'
            )
        self._registry = registry
        self._upstream_manager = upstream_manager
        self._hook_runner = hook_runner
        self._limits = limits or CodeModeLimits()
        self._authorizer = authorizer
        self._audit_verbatim = audit_verbatim

    async def run(
        self,
        code: str,
        *,
        headers: dict[str, str],
        user: Any,
    ) -> str:
        """Execute *code* in the sandbox and return its final expression.

        Builds the callable namespace from tools visible to *user*
        (after ``after_list_tools`` filtering), then hands control to
        Monty.  Raises :class:`ExecutionDenied` if the session-level
        authorizer denies the request.
        """
        if not isinstance(code, str) or not code.strip():
            raise ExecutionDenied("Empty code body", code="invalid_argument")

        # Session-level gate.  The callback signature is intentionally
        # loose (plain dict) so enterprise implementations can pass
        # extra context without leaking their types into the OSS API.
        if self._authorizer is not None:
            ctx: dict[str, Any] = {"headers": headers}
            allowed = await self._authorizer(user, ctx)
            if not allowed:
                raise ExecutionDenied("code_mode is not permitted for this user", code="forbidden")

        with _tracer.start_as_current_span("gateway.execute_code") as span:
            code_bytes = code.encode("utf-8")
            code_sha = hashlib.sha256(code_bytes).hexdigest()
            audit = _CodeModeAudit(
                code_session_id=str(uuid.uuid4()),
                code_sha256=code_sha,
                code_byte_len=len(code_bytes),
                code_line_count=code.count("\n") + (1 if code and not code.endswith("\n") else 0),
                user_subject=_subject_of(user),
            )
            span.set_attribute("gateway.code_sha256", code_sha)
            span.set_attribute("gateway.code_byte_len", audit.code_byte_len)
            span.set_attribute("gateway.code_session_id", audit.code_session_id)

            if self._audit_verbatim:
                # Debug-only: raw LLM code is PII-sensitive.
                logger.debug("code_mode.code_body", extra={"code": code, "session": audit.code_session_id})

            # 1. Filter tools via after_list_tools, using the OUTER user/headers.
            #    This is the Finding 6.1 hardening — unauthorized tool names
            #    never appear in the sandbox callable namespace.
            all_tools: list[ToolEntry] = []
            for d in self._registry.get_domain_names():
                all_tools.extend(self._registry.get_tools_by_domain(d))
            visible = await self._hook_runner.run_after_list_tools(
                all_tools,
                ListToolsContext(domain=None, headers=headers, user=user),
            )
            span.set_attribute("gateway.visible_tool_count", len(visible))

            # 2. Build the callable namespace.  Headers and user are
            #    captured once here and closed over — no ContextVar
            #    reads from Monty's worker thread (Finding 6.2).
            callables = self._build_callables(visible, headers, user, audit)

            # 3. Invoke Monty.
            start = time.monotonic()
            output: Any = None
            assert _monty is not None  # guarded at construction
            try:
                vm = _monty.Monty(code, script_name="code_mode.py")
                output = await vm.run_async(
                    external_functions=callables,
                    limits=self._limits.to_monty() or None,
                )
            except Exception:
                audit.is_error = True
                audit.duration_ms = round((time.monotonic() - start) * 1000, 2)
                span.set_attribute("gateway.is_error", True)
                self._log_audit(audit)
                raise
            audit.duration_ms = round((time.monotonic() - start) * 1000, 2)
            span.set_attribute("gateway.duration_ms", audit.duration_ms)
            span.set_attribute("gateway.step_count", audit.step_count)
            self._log_audit(audit)

            return "" if output is None else str(output)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_callables(
        self,
        visible: list[ToolEntry],
        outer_headers: dict[str, str],
        outer_user: Any,
        audit: _CodeModeAudit,
    ) -> dict[str, Callable[..., Awaitable[Any]]]:
        """Create a Python callable for each authorized tool."""
        namespace: dict[str, Callable[..., Awaitable[Any]]] = {}
        call_cap = self._limits.max_nested_calls

        for tool in visible:
            namespace[tool.name] = self._make_wrapper(tool, outer_headers, outer_user, audit, call_cap)
        return namespace

    def _make_wrapper(
        self,
        tool: ToolEntry,
        outer_headers: dict[str, str],
        outer_user: Any,
        audit: _CodeModeAudit,
        call_cap: int | None,
    ) -> Callable[..., Awaitable[Any]]:
        """Return an async callable that dispatches one tool through the hook pipeline."""
        hook_runner = self._hook_runner
        upstream_manager = self._upstream_manager

        async def _invoke(**kwargs: Any) -> Any:
            if call_cap is not None and audit.step_count >= call_cap:
                raise ExecutionDenied(
                    f"Code mode exceeded max_nested_calls ({call_cap})",
                    code="nested_call_limit",
                )
            audit.step_count += 1
            audit.tool_names_invoked.append(tool.name)

            ctx = ExecutionContext(
                tool=tool,
                arguments=dict(kwargs),
                headers=outer_headers,
                user=outer_user,
                metadata={"code_session_id": audit.code_session_id},
            )

            with _tracer.start_as_current_span("gateway.code_mode.step") as span:
                span.set_attribute("gateway.tool_name", tool.name)
                span.set_attribute("gateway.code_session_id", audit.code_session_id)
                try:
                    await hook_runner.run_before_execute(ctx)
                except ExecutionDenied:
                    raise
                result = await upstream_manager.execute_tool(
                    tool.name,
                    ctx.arguments,
                    extra_headers=ctx.extra_headers or None,
                )
                # ``call_tool`` returns ``fastmcp.client.client.CallToolResult``
                # whose fields are snake_case (``is_error`` / ``structured_content``),
                # not the MCP-spec camelCase. Reading the camelCase
                # names here silently returned defaults on every real
                # upstream response — errors were never surfaced, and
                # structured content always fell through to the text-
                # extraction branch. Match ``meta_tools.execute_tool``'s
                # direct-attribute read pattern for consistency.
                is_error = bool(getattr(result, "is_error", False))
                # Prefer structured output — gives Python code dict access.
                payload: Any = getattr(result, "structured_content", None)
                if payload is None:
                    # Fall back to concatenated text content.
                    payload = _extract_text(result)
                text_for_hook = payload if isinstance(payload, str) else str(payload)
                await hook_runner.run_after_execute(ctx, text_for_hook, is_error)
                return payload

        _invoke.__name__ = tool.name
        _invoke.__doc__ = tool.description or ""
        return _invoke

    def _log_audit(self, audit: _CodeModeAudit) -> None:
        payload = {
            "code_session_id": audit.code_session_id,
            "code_sha256": audit.code_sha256,
            "code_byte_len": audit.code_byte_len,
            "code_line_count": audit.code_line_count,
            "user_subject": audit.user_subject,
            "step_count": audit.step_count,
            "tool_names_invoked": audit.tool_names_invoked,
            "duration_ms": audit.duration_ms,
            "is_error": audit.is_error,
        }
        logger.info("code_mode.invoked", extra={"audit": payload})


def _extract_text(result: Any) -> str:
    """Pull the first text content block out of an MCP call-tool result."""
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    return ""


def _subject_of(user: Any) -> Any:
    """Best-effort extraction of a stable subject identifier for audit."""
    if user is None:
        return None
    for attr in ("subject", "sub", "email", "id"):
        value = getattr(user, attr, None)
        if value is not None:
            return value
    return repr(user)

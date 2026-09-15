"""Meta-tools: the 3 tools exposed to the LLM by the gateway."""

from __future__ import annotations

import json
from copy import deepcopy
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from fastmcp import Context  # noqa: TC002 - FastMCP resolves Context for injection at runtime.
from fastmcp.tools import ToolResult
from mcp.types import TextContent, ToolAnnotations
from opentelemetry import trace

from fastmcp_gateway.errors import _find_upstream_response, error_payload, error_response, parse_www_authenticate
from fastmcp_gateway.hooks import ExecutionContext, ExecutionDenied, HookRunner, ListToolsContext
from fastmcp_gateway.signatures import extract_params, tool_to_signature

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from fastmcp_gateway.client_manager import UpstreamManager
    from fastmcp_gateway.registry import ToolEntry, ToolRegistry

_tracer = trace.get_tracer("fastmcp_gateway.meta_tools")


def _request_meta_snapshot(request_ctx: Context | None) -> dict[str, Any] | None:
    """Copy the exact inbound MCP request metadata, preserving wire aliases."""
    if request_ctx is None or request_ctx.request_context is None:
        return None
    meta_model = request_ctx.request_context.meta
    if meta_model is None:
        return None
    return deepcopy(
        meta_model.model_dump(
            mode="python",
            by_alias=True,
            exclude_unset=True,
        )
    )


def _signatures_block(tools: list[ToolEntry]) -> str:
    """Render *tools* as a plain-text block of Python-style signatures.

    Each tool spans up to two lines (signature plus optional description),
    separated by blank lines to keep the listing scannable.
    """
    if not tools:
        return ""
    return "\n\n".join(tool_to_signature(t) for t in tools)


def _json_result(payload: dict[str, Any]) -> ToolResult:
    """Carry *payload* on both MCP result channels.

    The text block stays exactly what a caller reading ``content[0].text``
    has always received -- ``json.dumps`` of the payload -- while
    ``structured_content`` carries the same data as an object.

    Returning a ``ToolResult`` rather than the JSON string is what keeps
    the two channels honest. A meta-tool annotated ``-> str`` returns a
    *non-object*, and MCP requires ``structuredContent`` to be an object,
    so FastMCP wraps the return in ``{"result": <the JSON string>}``
    (``fastmcp.tools.function_parsing`` sets ``x-fastmcp-wrap-result`` on
    the derived output schema; ``fastmcp.tools.base`` applies it). The
    payload is then encoded twice: once here, once by the client
    rendering that wrapper object -- so every quote inside it reaches the
    caller as ``\\"`` and every newline as ``\\n``. Consumers pay the
    escaping in tokens and have to ``json.loads(data["result"])`` to
    reach data the structured channel was supposed to hand them
    directly.

    Every call site must also pass ``output_schema=None`` to
    ``@mcp.tool``; without it FastMCP derives a schema from the
    ``-> ToolResult`` annotation and re-applies the same wrap to the
    result's text.
    """
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        structured_content=payload,
    )


def _prose_result(text: str) -> ToolResult:
    """Carry free text on the text channel only.

    ``structured_content`` is left unset because there is no object to
    put there -- the signatures block is prose for a model to read, not
    data. That is a deliberate contrast with :func:`_json_result`: the
    ``-> str`` annotation this replaces used to publish the prose as
    ``{"result": "<the whole block>"}``, a structured channel whose only
    field was the text already present on the text channel.
    """
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=None,
    )


def _gateway_error_result(code: str, message: str, **details: Any) -> ToolResult:
    """A :class:`~fastmcp_gateway.errors.GatewayError` on both channels.

    Discovery errors are authored by the gateway itself, so unlike
    ``execute_tool``'s ``_error_result`` -- which withholds
    ``structured_content`` because a gateway-level failure has no
    upstream ``CallToolResult`` to source it from -- there is a
    first-party object to publish here, and a caller can branch on
    ``code`` without parsing text.
    """
    return _json_result(error_payload(code, message, **details))


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


def _describe_argument_errors(entry: ToolEntry, arguments: dict[str, Any]) -> str | None:
    """Return a human-readable argument-validation error, or ``None`` if *arguments* is valid.

    Checks *arguments* against *entry*'s declared JSON-Schema ``properties``
    for two failure modes: a key absent from ``properties`` ("unknown"), and
    a key in ``required`` absent from *arguments* ("missing"). Reuses
    :func:`~fastmcp_gateway.signatures.extract_params` (the same param
    extraction the signature renderer uses) so this check can never drift
    from what that renderer reports as the tool's shape.

    A tool whose schema has no proper ``properties`` object makes no claim
    about what's valid -- every call to it passes through unchecked, exactly
    as before this function existed.
    """
    schema = entry.input_schema
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        return None

    params = extract_params(schema)
    known_names = {p.name for p in params}
    required_names = {p.name for p in params if p.required}

    unknown = sorted(name for name in arguments if name not in known_names)
    missing = sorted(required_names - arguments.keys())
    if not unknown and not missing:
        return None

    parts: list[str] = []
    if unknown:
        parts.append(f"unknown argument(s) {', '.join(repr(n) for n in unknown)}")
    if missing:
        parts.append(f"missing required argument(s) {', '.join(repr(n) for n in missing)}")
    return f"Invalid arguments for {entry.name!r}: {'; '.join(parts)}."


def register_meta_tools(
    mcp: FastMCP,
    registry: ToolRegistry,
    upstream_manager: UpstreamManager,
    hook_runner: HookRunner | None = None,
    *,
    code_mode_runner: Any | None = None,
) -> None:
    """Register the meta-tools on the FastMCP server.

    Always registers ``discover_tools``, ``get_tool_schema``,
    ``execute_tool``, and ``refresh_registry``.

    When *code_mode_runner* is provided (see
    :class:`~fastmcp_gateway.code_mode.CodeModeRunner`), additionally
    registers an experimental ``execute_code`` meta-tool.
    """
    if hook_runner is None:
        hook_runner = HookRunner()

    async def _filter_tools(tools: list[ToolEntry], domain: str | None) -> list[ToolEntry]:
        """Authenticate and apply ``after_list_tools`` hooks if any are registered."""
        if not hook_runner.has_hooks:
            return tools
        from fastmcp_gateway.client_manager import get_user_headers

        headers = get_user_headers()
        user = await hook_runner.run_authenticate(headers)
        ctx = ListToolsContext(domain=domain, headers=headers, user=user)
        return await hook_runner.run_after_list_tools(tools, ctx)

    async def _visible_tool_names() -> list[str]:
        """Return the sorted list of tool names visible to the caller.

        Collects every registered tool, routes them through the same
        ``_filter_tools`` closure that ``discover_tools`` uses for the
        domain-summary mode, and returns just the names. This keeps the
        fuzzy-match "did you mean" suggestion surface aligned with the
        tools/list visibility filter so a caller with narrow scopes can
        never probe the full registry via garbage tool-name lookups.
        """
        all_tools: list[ToolEntry] = []
        for d in registry.get_domain_names():
            all_tools.extend(registry.get_tools_by_domain(d))
        visible = await _filter_tools(all_tools, None)
        return sorted(t.name for t in visible)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        # output_schema=None stops FastMCP deriving an output schema from
        # the return annotation and re-wrapping the result's text as
        # {"result": ...}; see _json_result for why that wrap
        # double-encodes the payload.
        output_schema=None,
    )
    async def discover_tools(
        domain: str | None = None,
        group: str | None = None,
        query: str | None = None,
        format: Literal["schema", "signatures"] = "signatures",
    ) -> ToolResult:
        """Browse available tools by domain, group, or keyword.

        Call with no arguments to see all available domains and their tool counts.
        Call with a domain to see groups and tools within that domain.
        Call with a domain and group to see tools in that specific group.
        Call with a query to search across all tools by keyword.

        Default ``signatures`` renders each tool as ``name(arg: type, ...)``
        so a caller can call ``execute_tool`` without a second
        ``get_tool_schema`` round-trip; pass ``format="schema"`` for the
        JSON summary. The domain summary (no-arguments form) ignores
        ``format`` and always returns JSON.
        """
        with _tracer.start_as_current_span("gateway.discover_tools") as span:
            if domain:
                span.set_attribute("gateway.domain", domain)
            if group:
                span.set_attribute("gateway.group", group)
            if query:
                span.set_attribute("gateway.query", query)
            if format != "schema":
                span.set_attribute("gateway.format", format)

            # Mode 4: keyword search (takes priority when query is provided)
            if query is not None and query.strip():
                results = await _filter_tools(registry.search(query), None)
                span.set_attribute("gateway.result_count", len(results))
                if format == "signatures":
                    return _prose_result(_signatures_block(results))
                return _json_result(
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
                # Collect all tools, apply hook filtering, rebuild summary.
                all_tools: list[ToolEntry] = []
                for d in registry.get_domain_names():
                    all_tools.extend(registry.get_tools_by_domain(d))
                filtered = await _filter_tools(all_tools, None)

                # Rebuild domain info from (potentially filtered) tools.
                domain_info = registry.get_domain_info()
                desc_map = {d.name: d.description for d in domain_info}
                by_domain: dict[str, list[ToolEntry]] = {}
                for t in filtered:
                    by_domain.setdefault(t.domain, []).append(t)

                result_domains = []
                for dname in sorted(by_domain):
                    dtools = by_domain[dname]
                    result_domains.append(
                        {
                            "name": dname,
                            "description": desc_map.get(dname, ""),
                            "tool_count": len(dtools),
                            "groups": sorted({t.group for t in dtools}),
                        }
                    )

                span.set_attribute("gateway.result_count", len(result_domains))
                return _json_result(
                    {
                        "domains": result_domains,
                        "total_tools": len(filtered),
                    }
                )

            # Validate domain
            if not registry.has_domain(domain):
                available = registry.get_domain_names()
                span.set_attribute("gateway.error_code", "domain_not_found")
                return _gateway_error_result(
                    "domain_not_found",
                    f"Unknown domain '{domain}'. Available domains: {', '.join(available)}"
                    if available
                    else f"Unknown domain '{domain}'. No domains are registered.",
                    domain=domain,
                    available_domains=available,
                )

            # Mode 3: domain + group -> tools in that group
            if group is not None:
                if not registry.has_group(domain, group):
                    available_groups = registry.get_groups_for_domain(domain)
                    span.set_attribute("gateway.error_code", "group_not_found")
                    msg = (
                        f"Unknown group '{group}' in domain '{domain}'. Available groups: {', '.join(available_groups)}"
                    )
                    return _gateway_error_result(
                        "group_not_found",
                        msg,
                        domain=domain,
                        group=group,
                        available_groups=available_groups,
                    )
                tools = await _filter_tools(registry.get_tools_by_group(domain, group), domain)
                span.set_attribute("gateway.result_count", len(tools))
                if format == "signatures":
                    return _prose_result(_signatures_block(tools))
                return _json_result(
                    {
                        "domain": domain,
                        "group": group,
                        "tools": [{"name": t.name, "description": t.description} for t in tools],
                    }
                )

            # Mode 2: domain only -> all tools in domain
            tools = await _filter_tools(registry.get_tools_by_domain(domain), domain)
            span.set_attribute("gateway.result_count", len(tools))
            if format == "signatures":
                return _prose_result(_signatures_block(tools))
            return _json_result(
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

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        # output_schema=None stops FastMCP deriving an output schema from
        # the return annotation and re-wrapping the result's text as
        # {"result": ...}; see _json_result for why that wrap
        # double-encodes the payload.
        output_schema=None,
    )
    async def get_tool_schema(tool_name: str) -> ToolResult:
        """Get the full parameter schema for a specific tool.

        Call this after discover_tools to get the complete input schema
        before calling execute_tool. Returns the JSON Schema that describes
        what arguments the tool accepts.
        """
        with _tracer.start_as_current_span("gateway.get_tool_schema") as span:
            span.set_attribute("gateway.tool_name", tool_name)

            entry = registry.lookup(tool_name)
            if entry is not None:
                # Verify the tool is visible after hook filtering.
                filtered = await _filter_tools([entry], entry.domain)
                if not filtered:
                    entry = None  # Treat as not found

            if entry is not None:
                span.set_attribute("gateway.domain", entry.domain)
                return _json_result(
                    {
                        "name": entry.name,
                        "domain": entry.domain,
                        "group": entry.group,
                        "description": entry.description,
                        "parameters": entry.input_schema,
                    }
                )

            # Unknown tool (or filtered out) — suggest similar names
            suggestions = _suggest_tool_names(tool_name, await _visible_tool_names())
            if suggestions:
                hint = f"Did you mean {', '.join(repr(s) for s in suggestions)}?"
            else:
                hint = "Use discover_tools to browse available tools."
            span.set_attribute("gateway.error_code", "tool_not_found")
            return _gateway_error_result(
                "tool_not_found",
                f"Unknown tool '{tool_name}'. {hint}",
                tool_name=tool_name,
                suggestions=suggestions,
            )

    def _error_result(error_text: str) -> ToolResult:
        """Wrap an ``error_response`` string in a :class:`ToolResult`.

        ``execute_tool`` declares ``-> ToolResult`` (strict, not a union
        with ``str``) so FastMCP does not infer a structured-content
        schema from the union and silently auto-populate
        ``structuredContent`` from dict-shaped TextContent.  All error
        paths therefore route their error envelope through this helper.
        """
        return ToolResult(
            content=[TextContent(type="text", text=error_text)],
            structured_content=None,
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True),
        # output_schema=None disables FastMCP's auto-derivation of an
        # output schema from the ``-> ToolResult`` return annotation.
        # Without this, FastMCP wraps the returned ToolResult's content
        # text into a ``{"result": "..."}`` dict and writes that to
        # ``structured_content``, overriding the explicit None we set
        # for the no-upstream-structured case (and corrupting the
        # upstream's real ``structured_content`` when a hook did set it).
        # Explicit None preserves the contract: ``structured_content``
        # on the wire is exactly what ``transform_result`` deposited.
        output_schema=None,
    )
    async def execute_tool(
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        request_ctx: Context | None = None,
    ) -> ToolResult:
        """Execute a tool by name with the given arguments.

        Use discover_tools to find available tools, then get_tool_schema
        to see what arguments a tool accepts, then call this to execute it.

        Returns a :class:`fastmcp.tools.tool.ToolResult` on success and on
        upstream errors -- the result carries the legacy
        ``{"tool": ..., "result": ...}`` string envelope as a TextContent
        block (for agents parsing the inner ``result`` field) and forwards
        the upstream ``CallToolResult.structured_content`` (if a
        :meth:`Hook.transform_result` hook populated it) on the standard
        MCP ``structuredContent`` channel.  Gateway-level failures
        (tool not found, hook denial, upstream exception) return the
        legacy string envelope only -- there is no upstream
        ``CallToolResult`` to source ``structured_content`` from.
        """
        request_meta = _request_meta_snapshot(request_ctx)
        inbound_meta_snapshot = None if request_meta is None else MappingProxyType(request_meta)
        with _tracer.start_as_current_span("gateway.execute_tool") as span:
            span.set_attribute("gateway.tool_name", tool_name)

            # Validate tool exists
            entry = registry.lookup(tool_name)
            if entry is None:
                suggestions = _suggest_tool_names(tool_name, await _visible_tool_names())
                if suggestions:
                    hint = f"Did you mean {', '.join(repr(s) for s in suggestions)}?"
                else:
                    hint = "Use discover_tools to browse available tools."
                span.set_attribute("gateway.error_code", "tool_not_found")
                return _error_result(
                    error_response(
                        "tool_not_found",
                        f"Unknown tool '{tool_name}'. {hint}",
                        tool_name=tool_name,
                        suggestions=suggestions,
                    )
                )

            span.set_attribute("gateway.domain", entry.domain)

            # Build execution context and run hooks
            execution_ctx: ExecutionContext | None = None
            if hook_runner.has_hooks:
                from fastmcp_gateway.client_manager import get_user_headers

                execution_ctx = ExecutionContext(
                    tool=entry,
                    arguments=arguments or {},
                    headers=get_user_headers(),
                    request_meta=(
                        None
                        if inbound_meta_snapshot is None
                        else MappingProxyType(deepcopy(dict(inbound_meta_snapshot)))
                    ),
                )

                # Authenticate
                execution_ctx.user = await hook_runner.run_authenticate(execution_ctx.headers)

                # Before execute — may raise ExecutionDenied
                try:
                    await hook_runner.run_before_execute(execution_ctx)
                except ExecutionDenied as denied:
                    span.set_attribute("gateway.error_code", denied.code)
                    return _error_result(
                        error_response(
                            denied.code,
                            denied.message,
                            tool=tool_name,
                            domain=entry.domain,
                        )
                    )

                # Use potentially mutated arguments from context
                arguments = execution_ctx.arguments

            # Reject a call whose arguments don't match the tool's declared
            # schema before it ever reaches the upstream server -- turns a
            # caller's guessed argument name into an immediate, self-
            # correcting error instead of a wasted upstream round trip.
            arg_error = _describe_argument_errors(entry, arguments or {})
            if arg_error is not None:
                span.set_attribute("gateway.error_code", "invalid_arguments")
                return _error_result(
                    error_response(
                        "invalid_arguments",
                        arg_error,
                        tool=tool_name,
                        domain=entry.domain,
                        signature=tool_to_signature(entry),
                    )
                )

            # Route to upstream via fresh client
            execute_kwargs: dict[str, Any] = {
                "request_meta": (None if inbound_meta_snapshot is None else deepcopy(dict(inbound_meta_snapshot)))
            }
            if execution_ctx and execution_ctx.extra_headers:
                execute_kwargs["extra_headers"] = execution_ctx.extra_headers
            try:
                result = await upstream_manager.execute_tool(
                    tool_name,
                    arguments,
                    **execute_kwargs,
                )
            except Exception as exc:  # Broad catch: gateway must not crash from upstream failures
                # An upstream that enforces per-tool scopes refuses a call with
                # 401/403, and a 403 carrying an RFC 6750 ``insufficient_scope``
                # challenge also names the scope the caller lacks. Neither is an
                # upstream failure, so each gets its own code. Status and
                # challenge are read from the same response.
                # Classification must never raise, or the span, the on_error hook
                # and the envelope are all lost. Only a ``str`` challenge is parsed
                # (other header shapes classify by status alone), and a response
                # that can't be read at all falls back to ``execution_error``.
                try:
                    response = _find_upstream_response(exc)
                    status = None if response is None else response.status_code
                    get_header = getattr(getattr(response, "headers", None), "get", None)
                    challenge = get_header("WWW-Authenticate") if callable(get_header) else None
                    auth_error, required_scope = parse_www_authenticate(
                        challenge if isinstance(challenge, str) else None
                    )
                except Exception:  # Unreadable upstream response: classify as a plain execution error.
                    status, auth_error, required_scope = None, None, None

                details: dict[str, Any] = {"tool": tool_name, "domain": entry.domain}
                if status == 403 and auth_error == "insufficient_scope":
                    code = "upstream_insufficient_scope"
                    message = f"Tool '{tool_name}' was refused by upstream server '{entry.domain}': insufficient scope."
                    details.update(upstream_status=status, required_scope=required_scope)
                elif status in (401, 403):
                    code = "upstream_unauthorized"
                    message = f"Tool '{tool_name}' was refused by upstream server '{entry.domain}': not authorized."
                    details.update(upstream_status=status)
                else:
                    code = "execution_error"
                    message = (
                        f"Tool '{tool_name}' failed: "
                        f"upstream server '{entry.domain}' returned an error. "
                        "Other domains may still be available."
                    )

                span.set_attribute("gateway.error_code", code)
                span.record_exception(exc)

                if execution_ctx is not None and hook_runner.has_hooks:
                    await hook_runner.run_on_error(execution_ctx, exc)

                return _error_result(error_response(code, message, **details))

            # Allow hooks to transform the raw ``CallToolResult`` before
            # any content-block flattening or envelope wrapping.  Hooks
            # may preserve ``structuredContent``, rewrite content
            # blocks, or replace the result with a domain-specific
            # envelope while the structured payload is still intact.
            # The string-based ``after_execute`` hook still runs later
            # on the post-flatten result for backward compatibility.
            #
            # A ``transform_result`` hook may raise ``ExecutionDenied``
            # (e.g. an operator hook inspecting the upstream payload to
            # surface a policy rejection). Catch it here and route
            # through the same structured error envelope used by
            # ``before_execute`` / ``after_execute`` denials -- meta-tools
            # must never raise ``ExecutionDenied`` to the LLM.
            if execution_ctx is not None and hook_runner.has_hooks:
                try:
                    result = await hook_runner.run_transform_result(execution_ctx, result)
                except ExecutionDenied as denied:
                    span.set_attribute("gateway.error_code", denied.code)
                    return _error_result(
                        error_response(
                            denied.code,
                            denied.message,
                            tool=tool_name,
                            domain=entry.domain,
                        )
                    )

            # Serialize content blocks to text
            content_parts: list[str] = []
            for block in result.content:
                if hasattr(block, "text"):
                    content_parts.append(block.text)  # type: ignore[union-attr]
                else:
                    content_parts.append(str(block))

            result_text = "\n".join(content_parts)

            # Capture upstream structured_content (set by a
            # ``transform_result`` hook, or by FastMCP-server-side
            # promotion of a typed upstream return) before any further
            # processing.  ``getattr`` with a default keeps this safe
            # against duck-typed upstream stand-ins that don't expose
            # the attribute.  The ``isinstance`` narrowing enforces the
            # MCP-spec contract (``structuredContent`` is an object) at
            # the gateway boundary, so a misbehaving upstream that emits
            # a list or scalar can't poison ``ToolResult`` validation
            # downstream.
            upstream_structured = getattr(result, "structured_content", None)
            if upstream_structured is not None and not isinstance(upstream_structured, dict):
                upstream_structured = None

            if result.is_error:
                span.set_attribute("gateway.error_code", "upstream_error")
                result_text = error_response(
                    "upstream_error",
                    result_text,
                    tool=tool_name,
                )
                # Run after_execute even on upstream errors
                if execution_ctx is not None and hook_runner.has_hooks:
                    try:
                        result_text = await hook_runner.run_after_execute(execution_ctx, result_text, True)
                    except ExecutionDenied as denied:
                        # An after_execute hook (output guard in
                        # reject mode, or any operator hook) may opt
                        # to surface a policy rejection. Route it
                        # through the same structured error envelope
                        # used by before_execute denials.
                        span.set_attribute("gateway.error_code", denied.code)
                        return _error_result(
                            error_response(
                                denied.code,
                                denied.message,
                                tool=tool_name,
                                domain=entry.domain,
                            )
                        )
                return ToolResult(
                    content=[TextContent(type="text", text=result_text)],
                    structured_content=upstream_structured,
                )

            result_text = json.dumps({"tool": tool_name, "result": result_text})

            # After execute — pipeline transforms. An
            # ``after_execute`` hook may raise ``ExecutionDenied``
            # (e.g., the output guard's reject mode catches prompt-
            # injection markup in the tool result). Surface that as
            # a structured error, not an uncaught exception.
            if execution_ctx is not None and hook_runner.has_hooks:
                try:
                    result_text = await hook_runner.run_after_execute(execution_ctx, result_text, False)
                except ExecutionDenied as denied:
                    span.set_attribute("gateway.error_code", denied.code)
                    return _error_result(
                        error_response(
                            denied.code,
                            denied.message,
                            tool=tool_name,
                            domain=entry.domain,
                        )
                    )

            return ToolResult(
                content=[TextContent(type="text", text=result_text)],
                structured_content=upstream_structured,
            )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=False))
    async def refresh_registry() -> str:
        """Refresh the tool registry by re-querying all upstream MCP servers.

        Returns a summary of changes (tools added/removed per domain).
        Use this if you suspect the available tools have changed since
        the gateway started.
        """
        with _tracer.start_as_current_span("gateway.refresh_registry") as span:
            try:
                diffs = await upstream_manager.refresh_all()
            except Exception as exc:
                span.set_attribute("gateway.error_code", "refresh_error")
                span.record_exception(exc)
                return error_response(
                    "refresh_error",
                    "Failed to refresh the tool registry. Some or all upstreams may be unreachable.",
                )

            span.set_attribute("gateway.domains_refreshed", len(diffs))
            return json.dumps(
                {
                    "refreshed": [
                        {
                            "domain": d.domain,
                            "added": d.added,
                            "removed": d.removed,
                            "tool_count": d.tool_count,
                        }
                        for d in diffs
                    ],
                }
            )

    # ------------------------------------------------------------------
    # Optional experimental meta-tool: execute_code
    # ------------------------------------------------------------------
    if code_mode_runner is not None:

        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True))
        async def execute_code(
            code: str,
            request_ctx: Context | None = None,
        ) -> str:
            """Run LLM-authored Python that orchestrates multiple tool calls.

            Experimental.  Every registered tool is exposed as a named
            async callable inside a secure Monty sandbox; write code that
            chains them together and returns a compact result so
            intermediate payloads do not pass through the agent's
            context.

            Example::

                people = await apollo_search(query="...", limit=5)
                {
                    "emails": [p["email"] for p in people["people"]],
                }

            - Call ``discover_tools(domain=..., format="signatures")``
              first to learn each tool's signature.
            - Do **not** use this for large analytical datasets; it is
              sized for small-payload cross-tool chaining.
            - All access control and audit hooks that apply to
              ``execute_tool`` also apply per nested call here.
            """
            request_meta = _request_meta_snapshot(request_ctx)

            from fastmcp_gateway.client_manager import get_user_headers

            with _tracer.start_as_current_span("gateway.execute_code") as span:
                headers = get_user_headers()
                user = await hook_runner.run_authenticate(headers)
                try:
                    return await code_mode_runner.run(
                        code,
                        headers=headers,
                        user=user,
                        request_meta=request_meta,
                    )
                except ExecutionDenied as exc:
                    span.set_attribute("gateway.error_code", exc.code)
                    return error_response(exc.code, exc.message)

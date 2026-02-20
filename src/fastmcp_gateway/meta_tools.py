"""Meta-tools: the 3 tools exposed to the LLM by the gateway."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mcp.types import ToolAnnotations
from opentelemetry import trace

from fastmcp_gateway.errors import error_response
from fastmcp_gateway.hooks import ExecutionContext, ExecutionDenied, HookRunner

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from fastmcp_gateway.client_manager import UpstreamManager
    from fastmcp_gateway.registry import ToolRegistry

_tracer = trace.get_tracer("fastmcp_gateway.meta_tools")


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


def register_meta_tools(
    mcp: FastMCP,
    registry: ToolRegistry,
    upstream_manager: UpstreamManager,
    hook_runner: HookRunner | None = None,
) -> None:
    """Register the 3 meta-tools on the FastMCP server."""
    if hook_runner is None:
        hook_runner = HookRunner()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
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
        with _tracer.start_as_current_span("gateway.discover_tools") as span:
            if domain:
                span.set_attribute("gateway.domain", domain)
            if group:
                span.set_attribute("gateway.group", group)
            if query:
                span.set_attribute("gateway.query", query)

            # Mode 4: keyword search (takes priority when query is provided)
            if query is not None and query.strip():
                results = registry.search(query)
                span.set_attribute("gateway.result_count", len(results))
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
                span.set_attribute("gateway.result_count", len(domain_info))
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
                span.set_attribute("gateway.error_code", "domain_not_found")
                return error_response(
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
                    return error_response(
                        "group_not_found",
                        msg,
                        domain=domain,
                        group=group,
                        available_groups=available_groups,
                    )
                tools = registry.get_tools_by_group(domain, group)
                span.set_attribute("gateway.result_count", len(tools))
                return json.dumps(
                    {
                        "domain": domain,
                        "group": group,
                        "tools": [{"name": t.name, "description": t.description} for t in tools],
                    }
                )

            # Mode 2: domain only -> all tools in domain
            tools = registry.get_tools_by_domain(domain)
            span.set_attribute("gateway.result_count", len(tools))
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

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def get_tool_schema(tool_name: str) -> str:
        """Get the full parameter schema for a specific tool.

        Call this after discover_tools to get the complete input schema
        before calling execute_tool. Returns the JSON Schema that describes
        what arguments the tool accepts.
        """
        with _tracer.start_as_current_span("gateway.get_tool_schema") as span:
            span.set_attribute("gateway.tool_name", tool_name)

            entry = registry.lookup(tool_name)
            if entry is not None:
                span.set_attribute("gateway.domain", entry.domain)
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
            span.set_attribute("gateway.error_code", "tool_not_found")
            return error_response(
                "tool_not_found",
                f"Unknown tool '{tool_name}'. {hint}",
                tool_name=tool_name,
                suggestions=suggestions,
            )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True))
    async def execute_tool(
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Execute a tool by name with the given arguments.

        Use discover_tools to find available tools, then get_tool_schema
        to see what arguments a tool accepts, then call this to execute it.
        """
        with _tracer.start_as_current_span("gateway.execute_tool") as span:
            span.set_attribute("gateway.tool_name", tool_name)

            # Validate tool exists
            entry = registry.lookup(tool_name)
            if entry is None:
                suggestions = _suggest_tool_names(tool_name, registry.get_all_tool_names())
                if suggestions:
                    hint = f"Did you mean {', '.join(repr(s) for s in suggestions)}?"
                else:
                    hint = "Use discover_tools to browse available tools."
                span.set_attribute("gateway.error_code", "tool_not_found")
                return error_response(
                    "tool_not_found",
                    f"Unknown tool '{tool_name}'. {hint}",
                    tool_name=tool_name,
                    suggestions=suggestions,
                )

            span.set_attribute("gateway.domain", entry.domain)

            # Build execution context and run hooks
            ctx: ExecutionContext | None = None
            if hook_runner.has_hooks:
                from fastmcp_gateway.client_manager import get_user_headers

                ctx = ExecutionContext(
                    tool=entry,
                    arguments=arguments or {},
                    headers=get_user_headers(),
                )

                # Authenticate
                ctx.user = await hook_runner.run_authenticate(ctx.headers)

                # Before execute — may raise ExecutionDenied
                try:
                    await hook_runner.run_before_execute(ctx)
                except ExecutionDenied as denied:
                    span.set_attribute("gateway.error_code", denied.code)
                    return error_response(
                        denied.code,
                        denied.message,
                        tool=tool_name,
                        domain=entry.domain,
                    )

                # Use potentially mutated arguments from context
                arguments = ctx.arguments

            # Route to upstream via fresh client
            execute_kwargs: dict[str, Any] = {}
            if ctx and ctx.extra_headers:
                execute_kwargs["extra_headers"] = ctx.extra_headers
            try:
                result = await upstream_manager.execute_tool(
                    tool_name,
                    arguments,
                    **execute_kwargs,
                )
            except Exception as exc:  # Broad catch: gateway must not crash from upstream failures
                span.set_attribute("gateway.error_code", "execution_error")
                span.record_exception(exc)

                if ctx is not None and hook_runner.has_hooks:
                    await hook_runner.run_on_error(ctx, exc)

                return error_response(
                    "execution_error",
                    f"Tool '{tool_name}' failed: "
                    f"upstream server '{entry.domain}' returned an error. "
                    "Other domains may still be available.",
                    tool=tool_name,
                    domain=entry.domain,
                )

            # Serialize content blocks to text
            content_parts: list[str] = []
            for block in result.content:
                if hasattr(block, "text"):
                    content_parts.append(block.text)  # type: ignore[union-attr]
                else:
                    content_parts.append(str(block))

            result_text = "\n".join(content_parts)

            if result.is_error:
                span.set_attribute("gateway.error_code", "upstream_error")
                result_text = error_response(
                    "upstream_error",
                    result_text,
                    tool=tool_name,
                )
                # Run after_execute even on upstream errors
                if ctx is not None and hook_runner.has_hooks:
                    result_text = await hook_runner.run_after_execute(ctx, result_text, True)
                return result_text

            result_text = json.dumps({"tool": tool_name, "result": result_text})

            # After execute — pipeline transforms
            if ctx is not None and hook_runner.has_hooks:
                result_text = await hook_runner.run_after_execute(ctx, result_text, False)

            return result_text

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

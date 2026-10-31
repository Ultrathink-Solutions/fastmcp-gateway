"""Meta-tools: the 3 tools exposed to the LLM by the gateway."""

from __future__ import annotations

import json
from copy import deepcopy
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastmcp import Context  # noqa: TC002 - FastMCP resolves Context for injection at runtime.
from fastmcp.tools import ToolResult
from mcp.types import TextContent, ToolAnnotations
from opentelemetry import trace
from pydantic import Field

from fastmcp_gateway._upstream_session import RESERVED_META_PREFIX, application_meta
from fastmcp_gateway.errors import (
    INVALID_PARAMS,
    _find_jsonrpc_error,
    _find_upstream_response,
    bounded_upstream_message,
    error_payload,
    error_response,
    parse_www_authenticate,
)
from fastmcp_gateway.hooks import ExecutionContext, ExecutionDenied, HookRunner, ListToolsContext
from fastmcp_gateway.signatures import declared_types, extract_params, tool_to_signature, type_admits

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastmcp import FastMCP

    from fastmcp_gateway.client_manager import UpstreamManager
    from fastmcp_gateway.registry import ToolEntry, ToolRegistry

_tracer = trace.get_tracer("fastmcp_gateway.meta_tools")


def _request_meta_snapshot(request_ctx: Context | None) -> dict[str, Any] | None:
    """Copy the caller's inbound MCP request metadata, exactly as sent on the wire.

    The protocol-reserved ``io.modelcontextprotocol/`` keys are left out: a
    2026-07-28 client stamps every request with them (its protocol version,
    client info and capabilities), so they describe the caller's connection
    to the gateway, not the call. A request whose ``_meta`` holds nothing
    else carried no metadata of its own, and yields ``None`` like a request
    without ``_meta``.
    """
    if request_ctx is None or request_ctx.request_context is None:
        return None
    raw = request_ctx.request_context.meta
    if not isinstance(raw, dict):
        return None
    meta = application_meta(raw)
    if not meta and any(key.startswith(RESERVED_META_PREFIX) for key in raw):
        return None
    return deepcopy(meta)


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


def _is_number(value: Any) -> bool:
    """``True`` for an int or float that is not a bool (JSON has no bool/number overlap)."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


#: The most characters of a caller's rejected string an error message repeats.
_MAX_ECHOED_VALUE_CHARS = 80

#: The most allowed values an ``enum`` error lists before summarising the rest.
_MAX_LISTED_ENUM_VALUES = 20


def _echo(value: str) -> str:
    """*value* as the error message repeats it: its repr, cut to a bound with its real length noted."""
    if len(value) <= _MAX_ECHOED_VALUE_CHARS:
        return repr(value)
    return f"{value[:_MAX_ECHOED_VALUE_CHARS]!r}… ({len(value)} characters)"


def _allowed(values: list[str]) -> str:
    """The allowed values of an ``enum`` as the error message lists them, bounded."""
    shown = ", ".join(_echo(v) for v in values[:_MAX_LISTED_ENUM_VALUES])
    hidden = len(values) - _MAX_LISTED_ENUM_VALUES
    return shown if hidden <= 0 else f"{shown}, … ({hidden} more)"


def _describe_value_error(name: str, schema: Any, value: Any) -> str | None:
    """Return why *value* violates *schema*'s value constraints, or ``None``.

    Checks only keywords whose verdict is unambiguous for a value of the type
    they constrain, the way a JSON-Schema validator applies them:

    * ``enum`` / ``const`` -- for a string value only (a non-string value of a
      string-typed enum is a type mismatch the upstream may coerce);
    * ``minimum`` / ``maximum`` / ``exclusiveMinimum`` / ``exclusiveMaximum``
      (draft-06+ numeric form) -- for a number value only;
    * ``type: integer`` (alone or with ``null``) -- a number with a fractional part;
    * ``minLength`` / ``maxLength`` -- for a string value only;
    * ``minItems`` / ``maxItems`` -- for an array value only.

    A value the declared ``type`` does not admit (the keyword's own type
    included: a string sent to a ``type: integer`` parameter), ``None``, a
    nested object's members and a schema built from ``anyOf`` / ``oneOf`` /
    ``allOf`` are not judged: the upstream owns the final verdict (it may
    coerce ``"5"`` to ``5``), and this check only spares it a call it would
    certainly reject.
    """
    if not isinstance(schema, dict) or value is None:
        return None
    if any(key in schema for key in ("anyOf", "oneOf", "allOf", "not")):
        return None
    names = declared_types(schema)
    if (
        isinstance(value, float)
        and not value.is_integer()
        and names is not None
        and "integer" in names
        and "number" not in names
    ):
        return f"{name!r} must be an integer (got {value!r})"
    if not type_admits(schema, value):
        return None

    if isinstance(value, str):
        enum = schema.get("enum")
        if isinstance(enum, list) and enum and all(isinstance(v, str) for v in enum) and value not in enum:
            return f"{name!r} must be one of {_allowed(enum)} (got {_echo(value)})"
        const = schema.get("const")
        if isinstance(const, str) and value != const:
            return f"{name!r} must be {_echo(const)} (got {_echo(value)})"
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and not isinstance(min_length, bool) and len(value) < min_length:
            return f"{name!r} must be at least {_plural(min_length, 'character')} (got {len(value)})"
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and not isinstance(max_length, bool) and len(value) > max_length:
            return f"{name!r} must be at most {_plural(max_length, 'character')} (got {len(value)})"
        return None

    if _is_number(value):
        bounds = (
            ("minimum", ">=", lambda limit: value < limit),
            ("exclusiveMinimum", ">", lambda limit: value <= limit),
            ("maximum", "<=", lambda limit: value > limit),
            ("exclusiveMaximum", "<", lambda limit: value >= limit),
        )
        for keyword, relation, violates in bounds:
            limit = schema.get(keyword)
            if _is_number(limit) and violates(limit):
                return f"{name!r} must be {relation} {limit!r} (got {value!r})"
        return None

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and not isinstance(min_items, bool) and len(value) < min_items:
            return f"{name!r} must have at least {_plural(min_items, 'item')} (got {len(value)})"
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and not isinstance(max_items, bool) and len(value) > max_items:
            return f"{name!r} must have at most {_plural(max_items, 'item')} (got {len(value)})"
    return None


def _describe_argument_errors(entry: ToolEntry, arguments: dict[str, Any]) -> str | None:
    """Return a human-readable argument-validation error, or ``None`` if *arguments* is valid.

    Checks *arguments* against *entry*'s declared JSON-Schema ``properties``
    for three failure modes: a key absent from ``properties`` ("unknown"), a
    key in ``required`` absent from *arguments* ("missing"), and a top-level
    value outside its property's declared bounds or allowed values (see
    :func:`_describe_value_error`). Reuses
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
    value_errors = [
        error
        for p in params
        if p.name in arguments and (error := _describe_value_error(p.name, p.schema, arguments[p.name])) is not None
    ]
    if not unknown and not missing and not value_errors:
        return None

    parts: list[str] = []
    if unknown:
        parts.append(f"unknown argument(s) {', '.join(repr(n) for n in unknown)}")
    if missing:
        parts.append(f"missing required argument(s) {', '.join(repr(n) for n in missing)}")
    parts.extend(value_errors)
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

    Each meta-tool's docstring is served verbatim as its MCP description,
    and each parameter's ``Field(description=...)`` is served in its input
    schema. Both are written for the model that calls the tool, so they
    use Markdown rather than reST and say only what the code does.
    ``tests/test_meta_tool_descriptions.py`` holds them to that.
    """
    if hook_runner is None:
        hook_runner = HookRunner()

    async def _caller_filter() -> Callable[[list[ToolEntry], str | None], Awaitable[list[ToolEntry]]]:
        """Authenticate the caller once and return an ``after_list_tools`` filter bound to that identity.

        A request that filters more than once runs every pass through the
        returned function, so ``on_authenticate`` runs once per request, as
        the ``Hook`` contract says, and every pass sees the same user.
        Without hooks the returned filter passes tools through unchanged.
        """
        if not hook_runner.has_hooks:

            async def unfiltered(tools: list[ToolEntry], domain: str | None) -> list[ToolEntry]:
                return tools

            return unfiltered

        from fastmcp_gateway.client_manager import get_user_headers

        headers = get_user_headers()
        user = await hook_runner.run_authenticate(headers)

        async def filtered(tools: list[ToolEntry], domain: str | None) -> list[ToolEntry]:
            ctx = ListToolsContext(domain=domain, headers=headers, user=user)
            return await hook_runner.run_after_list_tools(tools, ctx)

        return filtered

    async def _filter_tools(tools: list[ToolEntry], domain: str | None) -> list[ToolEntry]:
        """Authenticate and apply ``after_list_tools`` hooks, for a request that filters once."""
        return await (await _caller_filter())(tools, domain)

    async def _visible_tool_names(
        caller_filter: Callable[[list[ToolEntry], str | None], Awaitable[list[ToolEntry]]],
    ) -> list[str]:
        """Return the sorted list of tool names visible to the caller.

        Collects every registered tool, filters them as the
        ``discover_tools`` domain summary does (``ListToolsContext.domain``
        unset), and returns just the names. This keeps the fuzzy-match
        "did you mean" suggestion surface aligned with the tools/list
        visibility filter so a caller with narrow scopes can never probe
        the full registry via garbage tool-name lookups. *caller_filter*
        is the request's filter, so a request that already filtered does
        not authenticate again.
        """
        all_tools: list[ToolEntry] = []
        for d in registry.get_domain_names():
            all_tools.extend(registry.get_tools_by_domain(d))
        visible = await caller_filter(all_tools, None)
        return sorted(t.name for t in visible)

    async def _visible_domain_names(
        caller_filter: Callable[[list[ToolEntry], str | None], Awaitable[list[ToolEntry]]],
        rejected: str,
    ) -> list[str]:
        """Return the sorted names of the domains a ``discover_tools(domain=...)`` lookup accepts for the caller.

        Each domain is filtered on its own, with ``ListToolsContext.domain``
        set to it as that lookup sets it, so a ``domain_not_found`` answer
        never offers a domain the lookup would reject. *caller_filter* is
        the request's filter, so the caller is authenticated once however
        many domains there are. *rejected* is the domain the lookup just
        rejected; it is never offered.
        """
        names: list[str] = []
        for d in registry.get_domain_names():
            if d != rejected and await caller_filter(registry.get_tools_by_domain(d), d):
                names.append(d)
        return sorted(names)

    @mcp.tool(
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
        # output_schema=None stops FastMCP deriving an output schema from
        # the return annotation and re-wrapping the result's text as
        # {"result": ...}; see _json_result for why that wrap
        # double-encodes the payload.
        output_schema=None,
    )
    async def discover_tools(
        domain: Annotated[
            str | None,
            Field(
                description=(
                    "A domain name from the no-argument listing, such as `widgets`. Lists that domain's tools. "
                    "Omit it to list the domains."
                )
            ),
        ] = None,
        group: Annotated[
            str | None,
            Field(
                description=(
                    "A group name from the domain's `groups`. Narrows the listing to that group. "
                    "Ignored unless `domain` is also given."
                )
            ),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                description=(
                    "Keywords to search for across every domain. A tool matches when every space-separated "
                    "word appears, ignoring case, in its name or description (or in the name its own server "
                    "gives it). When given, `domain` and `group` are ignored."
                )
            ),
        ] = None,
        format: Annotated[
            Literal["schema", "signatures"],
            Field(
                description=(
                    "`signatures` (the default) returns one callable signature per tool. "
                    "`schema` returns JSON instead. The no-argument domain listing is always JSON."
                )
            ),
        ] = "signatures",
    ) -> ToolResult:
        """Find the tools this gateway can run, and learn how to call them.

        Start here when you need a tool you have not used yet in this conversation. Call it with no
        arguments to list the domains (each a set of related tools from one server). Then pass `domain`
        to list one domain's tools, add `group` to narrow that list, or pass `query` to search every
        domain by keyword. Tools the gateway hides from you are not listed.

        With no arguments, it returns JSON:
        `{"domains": [{"name", "description", "tool_count", "groups"}], "total_tools"}`.

        Otherwise it returns one Python-style signature per tool, followed by an indented line with
        the tool's description when it has one, for example
        `widgets_list(status: Literal['open', 'closed'], limit: Annotated[int, '1 <= limit <= 100'] = None) -> any`.
        Required parameters come first, and optional ones end in `= None`. Pass those names and values
        to `execute_tool` as `arguments`. With `format="schema"`, it returns JSON that lists each tool's
        `name` and `description`, plus its `domain` and `group` where your request did not already fix
        them. An empty listing means no tool matched.

        An unknown `domain` or `group`, or one with no tool you can use, returns
        `{"error": "...", "code": "domain_not_found"}` (or `"group_not_found"`), with the valid names
        in `details.available_domains` (or `details.available_groups`).
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

            # Modes 2 and 3 answer from the domain as the caller sees it, as
            # the summary does: a domain (or group) whose tools the hooks
            # hide entirely does not exist for this caller. One filter
            # serves every pass, so the caller is authenticated once.
            caller_filter = await _caller_filter()
            visible = (
                await caller_filter(registry.get_tools_by_domain(domain), domain) if registry.has_domain(domain) else []
            )
            if not visible:
                available = await _visible_domain_names(caller_filter, domain)
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
                tools = [t for t in visible if t.group == group]
                if not tools:
                    available_groups = sorted({t.group for t in visible})
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
            tools = visible
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
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        # output_schema=None stops FastMCP deriving an output schema from
        # the return annotation and re-wrapping the result's text as
        # {"result": ...}; see _json_result for why that wrap
        # double-encodes the payload.
        output_schema=None,
    )
    async def get_tool_schema(
        tool_name: Annotated[
            str,
            Field(description="The tool's exact name, as `discover_tools` lists it, such as `widgets_list`."),
        ],
    ) -> ToolResult:
        """Get one tool's full JSON Schema for its arguments.

        Use it only when the signature from `discover_tools` is not enough. A signature shows each
        parameter's name, type, and top-level allowed values and bounds. It does not show what each
        parameter means, or which fields of a nested object are required and what values they accept.

        Returns JSON: `{"name", "domain", "group", "description", "parameters"}`, where `parameters`
        is the tool's input JSON Schema. Build `execute_tool`'s `arguments` to match it.

        The name must match exactly. An unknown name, or a tool hidden from you, returns
        `{"error": "...", "code": "tool_not_found", "details": {"tool_name", "suggestions"}}`, where
        `suggestions` lists up to three similar tool names.
        """
        with _tracer.start_as_current_span("gateway.get_tool_schema") as span:
            span.set_attribute("gateway.tool_name", tool_name)

            # One filter serves the visibility check and the suggestions,
            # so the caller is authenticated once.
            caller_filter = await _caller_filter()
            entry = registry.lookup(tool_name)
            if entry is not None:
                # Verify the tool is visible after hook filtering.
                filtered = await caller_filter([entry], entry.domain)
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
            suggestions = _suggest_tool_names(tool_name, await _visible_tool_names(caller_filter))
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
        annotations=ToolAnnotations(read_only_hint=False, open_world_hint=True),
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
    # Result channels (for maintainers; the docstring below is what the model
    # reads). Success and an upstream ``isError`` result carry the text block
    # built below and forward the upstream's ``structured_content`` (as the
    # upstream sent it, or as a ``transform_result`` hook rewrote it) on MCP's
    # ``structuredContent``. A failure the gateway itself reports (unknown tool,
    # hook denial, local argument check, upstream exception) has no upstream
    # result to source ``structuredContent`` from, so it carries the
    # ``GatewayError`` JSON text only (see ``_error_result``).
    async def execute_tool(
        tool_name: Annotated[
            str,
            Field(description="The tool's exact name, as `discover_tools` lists it, such as `widgets_list`."),
        ],
        arguments: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "The tool's arguments as a JSON object, keyed by the parameter names in its signature, "
                    'such as `{"status": "open", "limit": 10}`. Leave out optional parameters you do not '
                    "need. Omit `arguments` entirely for a tool that takes none."
                )
            ),
        ] = None,
        request_ctx: Context | None = None,
    ) -> ToolResult:
        """Run one tool and return its result.

        Call it once you know the tool's name and parameters, from `discover_tools` (or from an earlier
        call in this conversation). The gateway sends the call to the server that owns the tool, so this
        can change data there.

        On success the text is JSON, `{"tool": "<name>", "result": "<the tool's own output>"}`. The
        `result` value is a string, so a tool that returns JSON has its JSON inside that string. When
        the tool also returns a structured object, it is passed through as the result's structured
        content.

        On failure the text is JSON, `{"error": "<what went wrong>", "code": "<code>", "details": {...}}`.
        The error comes back as an ordinary result, not as a failed call, so read `code`:
        - `tool_not_found`: no tool has that name. `details.suggestions` lists up to three similar
          names you can use.
        - `invalid_arguments`: the arguments do not fit the tool. Before sending the call, the gateway
          checks for unknown names, missing required ones, and values outside a parameter's allowed
          values or bounds. The tool's server can reject other mistakes with the same code.
          `details.signature` shows the expected call. Fix the arguments and call again.
        - `upstream_error`: the tool's server received the call and answered with an error, whose
          message is in `error`.
        - `upstream_unauthorized` or `upstream_insufficient_scope`: the tool's server refused you.
          `details.required_scope` names the missing scope when the server gave one. Retrying will not
          help until your access changes.
        - `execution_error`: the call failed before the tool's server answered it (unreachable, timed
          out, or an HTTP failure). Tools from other domains may still work.

        The gateway can also refuse a call by its own policy, with that policy's code, for example
        `forbidden`.
        """
        request_meta = _request_meta_snapshot(request_ctx)
        inbound_meta_snapshot = None if request_meta is None else MappingProxyType(request_meta)
        with _tracer.start_as_current_span("gateway.execute_tool") as span:
            span.set_attribute("gateway.tool_name", tool_name)

            # Validate tool exists
            entry = registry.lookup(tool_name)
            if entry is None:
                suggestions = _suggest_tool_names(tool_name, await _visible_tool_names(await _caller_filter()))
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
                # An upstream that answered with a JSON-RPC error response did
                # receive and judge the call, so its own message reaches the
                # caller: ``-32602`` (some MCP server frameworks report a call
                # that fails the tool's input schema this way rather than as an
                # ``isError`` result) is ``invalid_arguments`` with the tool's
                # signature, any other answer is ``upstream_error``. Only an
                # error the upstream sent counts (``UpstreamJsonRpcError``, marked
                # where it arrived): an ``McpError`` the MCP client raised itself
                # means no answer arrived.
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
                try:
                    jsonrpc_error = _find_jsonrpc_error(exc)
                except Exception:  # Unreadable error chain: classify as a plain execution error.
                    jsonrpc_error = None

                details: dict[str, Any] = {"tool": tool_name, "domain": entry.domain}
                if status == 403 and auth_error == "insufficient_scope":
                    code = "upstream_insufficient_scope"
                    message = f"Tool '{tool_name}' was refused by upstream server '{entry.domain}': insufficient scope."
                    details.update(upstream_status=status, required_scope=required_scope)
                elif status in (401, 403):
                    code = "upstream_unauthorized"
                    message = f"Tool '{tool_name}' was refused by upstream server '{entry.domain}': not authorized."
                    details.update(upstream_status=status)
                elif status is None and jsonrpc_error is not None and jsonrpc_error[0] == INVALID_PARAMS:
                    code = "invalid_arguments"
                    message = bounded_upstream_message(jsonrpc_error[1]) or (
                        f"Upstream server '{entry.domain}' rejected the arguments for {tool_name!r}."
                    )
                    details.update(upstream_error_code=jsonrpc_error[0], signature=tool_to_signature(entry))
                elif status is None and jsonrpc_error is not None:
                    # Same shape as the ``upstream_error`` an ``isError`` result
                    # yields below: the upstream's own words are the message.
                    code = "upstream_error"
                    message = bounded_upstream_message(jsonrpc_error[1]) or (
                        f"Upstream server '{entry.domain}' answered {tool_name!r} with error {jsonrpc_error[0]}."
                    )
                    details.update(upstream_error_code=jsonrpc_error[0])
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

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, open_world_hint=False))
    async def refresh_registry() -> str:
        """Ask every server behind the gateway for its current tool list.

        Use it when `discover_tools` is missing a domain or tool you expected, for example a server
        that was down when the gateway started. It takes no arguments. A server whose tools were
        already loaded keeps that list: if its tools have changed since, the gateway keeps the old list
        until an operator approves the change, and reports no tools added or removed.

        Returns JSON: `{"refreshed": [{"domain", "added", "removed", "tool_count"}]}`, one entry per
        server that answered, where `added` and `removed` list tool names. A server that could not be
        reached is left out. If the refresh fails as a whole, it returns
        `{"error": "...", "code": "refresh_error"}`.
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

        @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, open_world_hint=True))
        async def execute_code(
            code: Annotated[
                str,
                Field(
                    description=(
                        "A Python script for the sandbox. Call each tool as an async function, by its exact "
                        'name with keyword arguments, such as `await widgets_list(status="open")`. The value '
                        "of the script's last line is returned."
                    )
                ),
            ],
            request_ctx: Context | None = None,
        ) -> str:
            """Run a short Python script that calls several tools and returns only the result you need.

            Experimental. Use it when one step needs several tool calls, or a tool's output feeds the
            next call, and the intermediate data does not need to come back to you. Call
            `discover_tools` first for the exact tool names and parameters. Each tool you can see in
            `discover_tools` is an async function in the sandbox. Call it with `await` and keyword
            arguments. It returns the tool's structured object when the tool provides one, and its
            text otherwise. A tool that reports an error returns its error the same way, without
            raising, so check what it returned. After `import asyncio`, `asyncio.gather` runs several
            calls at once. The sandbox is meant for small results (dozens of rows), not for analyzing
            large datasets, and the gateway can limit its run time, memory and number of tool calls.

            Example:

                widgets = await widgets_list(status="open")
                {"count": len(widgets["items"])}

            Returns the value of the script's last line, converted to text with Python's `str()`, so a
            dict comes back in Python notation rather than JSON. A script whose last line is not an
            expression returns empty text. A script that raises an error, calls a function that does
            not exist, or exceeds a limit fails the call with that error's message. Each tool call
            inside the script is checked by the same policies as `execute_tool`, and a refused one
            also fails the call. If you may not run code at all, it returns
            `{"error": "...", "code": "forbidden"}`.
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

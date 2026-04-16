"""Render tool metadata as Python-style function signatures.

Many LLMs write valid Python more reliably than they translate JSON Schema
into Python call-sites.  When the caller wants a human-readable catalog of
downstream tools rather than the raw schema, render each tool as::

    apollo_search(query: str, limit: int = None) -> dict
      Short description of what the tool does.

Signatures are a read-only view of the registry — nothing here mutates
state.  See :meth:`ToolRegistry.populate_domain` for how tools are
ingested in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp_gateway.registry import ToolEntry

__all__ = [
    "ParamInfo",
    "extract_params",
    "format_schema",
    "tool_to_signature",
]


@dataclass(frozen=True)
class ParamInfo:
    """One parameter extracted from a tool's JSON Schema.

    Attributes
    ----------
    name:
        Parameter name.
    schema:
        Raw JSON-Schema subtree describing this parameter's type.
    required:
        Whether the parameter is declared in the schema's ``required`` array.
    """

    name: str
    schema: Any
    required: bool


def extract_params(input_schema: Any) -> list[ParamInfo]:
    """Extract an ordered parameter list from a tool's JSON Schema.

    Ordering rule (deterministic positional binding):

    1. Required params first, in the order they appear in the schema's
       ``required`` array.
    2. Optional params next, sorted lexicographically.

    Returns an empty list for schemas without a ``properties`` object.
    """
    if not isinstance(input_schema, dict):
        return []

    raw_props = input_schema.get("properties")
    if not isinstance(raw_props, dict):
        return []

    required_arr = input_schema.get("required")
    required_names: list[str] = []
    required_set: set[str] = set()
    if isinstance(required_arr, list):
        for entry in required_arr:
            if isinstance(entry, str) and entry in raw_props and entry not in required_set:
                required_names.append(entry)
                required_set.add(entry)

    optional_names = sorted(name for name in raw_props if name not in required_set)

    params: list[ParamInfo] = []
    for name in required_names:
        params.append(ParamInfo(name=name, schema=raw_props[name], required=True))
    for name in optional_names:
        params.append(ParamInfo(name=name, schema=raw_props[name], required=False))
    return params


def format_schema(schema: Any) -> str:
    """Render a JSON Schema fragment as a Python type annotation string.

    Handles union types (``"type": ["string", "null"]`` → ``str | None``),
    nested arrays (``array<object>`` → ``list[{...}]``), and objects with
    inline ``properties`` (rendered as ``{"k": type, ...}``).  Unknown
    types fall back to ``any``.
    """
    if not isinstance(schema, dict):
        return "any"

    raw_type = schema.get("type")

    if isinstance(raw_type, str):
        return _format_single_type(raw_type, schema)

    if isinstance(raw_type, list):
        nullable = False
        rendered: list[str] = []
        for item in raw_type:
            if not isinstance(item, str):
                continue
            if item == "null":
                nullable = True
            else:
                rendered.append(_format_single_type(item, schema))
        if not rendered:
            return "None" if nullable else "any"
        out = " | ".join(rendered)
        if nullable:
            out += " | None"
        return out

    return "any"


def _format_single_type(type_name: str, schema: dict[str, Any]) -> str:
    match type_name:
        case "string":
            return "str"
        case "integer":
            return "int"
        case "number":
            return "float"
        case "boolean":
            return "bool"
        case "null":
            return "None"
        case "array":
            items = schema.get("items")
            if items is not None:
                return f"list[{format_schema(items)}]"
            return "list"
        case "object":
            props = schema.get("properties")
            if isinstance(props, dict) and props:
                return _format_object_props(props)
            return "dict"
        case _:
            return type_name


def _format_object_props(props: dict[str, Any]) -> str:
    parts = [f'"{key}": {format_schema(props[key])}' for key in sorted(props)]
    return "{" + ", ".join(parts) + "}"


def tool_to_signature(tool: ToolEntry) -> str:
    """Render a :class:`ToolEntry` as a Python function-signature block.

    The block is two lines: the signature itself, then a single indented
    line with the tool's description (or omitted when there is no
    description).  LLMs can paste the result directly into scripts.
    """
    params = extract_params(tool.input_schema)
    parts: list[str] = []
    for p in params:
        py_type = format_schema(p.schema)
        part = f"{p.name}: {py_type}"
        if not p.required:
            part += " = None"
        parts.append(part)

    sig = f"{tool.name}({', '.join(parts)}) -> any"
    if tool.description:
        sig += f"\n  {tool.description}"
    return sig

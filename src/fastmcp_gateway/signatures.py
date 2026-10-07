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


#: The JSON-Schema ``type`` names that describe a value of each JSON kind.
#: ``integer`` counts for a number only when the value has no fractional part.
_TYPE_NAMES_FOR_KIND: dict[str, frozenset[str]] = {
    "string": frozenset({"string"}),
    "number": frozenset({"number", "integer"}),
    "boolean": frozenset({"boolean"}),
    "null": frozenset({"null"}),
    "array": frozenset({"array"}),
    "object": frozenset({"object"}),
}


def declared_types(schema: Any) -> frozenset[str] | None:
    """The ``type`` names *schema* declares, or ``None`` when it declares none (any type).

    ``"type": "string"`` and ``"type": ["string", "null"]`` both count; names
    that are not strings are ignored, and a list naming no string at all is
    treated as declaring none.
    """
    if not isinstance(schema, dict):
        return None
    raw = schema.get("type")
    if isinstance(raw, str):
        return frozenset({raw})
    if isinstance(raw, list):
        names = frozenset(item for item in raw if isinstance(item, str))
        return names or None
    return None


def _json_kind(value: Any) -> str | None:
    """The JSON kind of a decoded value (``"string"``, ``"number"``, ...), else ``None``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return None


def type_admits(schema: Any, value: Any) -> bool:
    """Whether *schema*'s declared ``type`` admits *value*; ``True`` when it declares none.

    ``integer`` admits a number without a fractional part, as JSON Schema
    specifies (``1.0`` is an integer).
    """
    names = declared_types(schema)
    if names is None:
        return True
    kind = _json_kind(value)
    if kind is None:
        return False
    if kind == "number" and "number" not in names and "integer" in names:
        return isinstance(value, int) or float(value).is_integer()
    return bool(_TYPE_NAMES_FOR_KIND[kind] & names)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _allowed_values(schema: dict[str, Any]) -> list[Any] | None:
    """The scalar values a schema's ``enum`` / ``const`` allows, else ``None``.

    ``type`` applies alongside ``enum`` / ``const``, so a listed value the
    declared type does not admit can never be sent and is left out:
    ``{"type": "string", "enum": ["open", 1]}`` allows only ``'open'``. When
    no listed value is admitted, the schema has no valid value and nothing is
    listed.
    """
    if "const" in schema:
        values: Any = [schema["const"]]
    else:
        values = schema.get("enum")
    if not isinstance(values, list) or not values:
        return None
    if not all(v is None or isinstance(v, str | int | float | bool) for v in values):
        return None
    admitted = [v for v in values if type_admits(schema, v)]
    return admitted or None


def _literal(values: list[Any]) -> str | None:
    """``Literal[...]`` of *values*, or ``None`` when one is a float (``Literal`` admits no float)."""
    if any(isinstance(v, float) for v in values):
        return None
    return "Literal[" + ", ".join(repr(v) for v in values) + "]"


def _range(subject: str, low: tuple[Any, bool] | None, high: tuple[Any, bool] | None) -> str | None:
    """``low <= subject < high`` from ``(limit, exclusive)`` sides, either of which may be absent."""
    if low is not None and high is not None:
        return f"{low[0]!r} {'<' if low[1] else '<='} {subject} {'<' if high[1] else '<='} {high[0]!r}"
    if low is not None:
        return f"{subject} {'>' if low[1] else '>='} {low[0]!r}"
    if high is not None:
        return f"{subject} {'<' if high[1] else '<='} {high[0]!r}"
    return None


#: Which keyword pairs bound a value of each declared ``type``, and what they
#: bound: the value itself or its ``len``.
_BOUND_KEYWORDS: tuple[tuple[frozenset[str], bool, tuple[str, str | None], tuple[str, str | None]], ...] = (
    (frozenset({"number", "integer"}), False, ("minimum", "exclusiveMinimum"), ("maximum", "exclusiveMaximum")),
    (frozenset({"string"}), True, ("minLength", None), ("maxLength", None)),
    (frozenset({"array"}), True, ("minItems", None), ("maxItems", None)),
)

#: The Python name a bound is labelled with when several types are bounded.
_BOUND_LABELS = {"number": "float", "integer": "int", "string": "str", "array": "list"}


def _bound_label(applies_to: frozenset[str], declared: frozenset[str] | None) -> str:
    """The label for the bounds of *applies_to*: ``int`` only when integers are all that is admitted."""
    admitted = applies_to if declared is None else declared & applies_to
    return _BOUND_LABELS["number" if "number" in admitted else min(admitted)]


def _bounds(name: str, schema: Any) -> str | None:
    """The declared value bounds of parameter *name* as one Python-style expression, else ``None``.

    Numeric bounds read ``0 < top <= 100``; length and item-count bounds read
    ``1 <= len(name) <= 80``. Only numeric limit values count, so a malformed
    bound is never rendered. A keyword bounds only a value of the type it
    applies to, so one is rendered only when the declared ``type`` admits that
    type (any ``type`` when none is declared). A parameter whose ``type`` admits
    several bounded types shows each one's bounds, labelled with the type:
    ``float: x >= 0; list: len(x) <= 5``.
    """
    if not isinstance(schema, dict):
        return None

    def side(inclusive: str, exclusive: str | None, *, lower: bool) -> tuple[Any, bool] | None:
        """The binding ``(limit, exclusive)`` of one side: the stricter limit, the exclusive one on a tie."""
        candidates: list[tuple[Any, bool]] = []
        if _is_number(schema.get(inclusive)):
            candidates.append((schema[inclusive], False))
        if exclusive is not None and _is_number(schema.get(exclusive)):
            candidates.append((schema[exclusive], True))
        if not candidates:
            return None
        # A lower bound binds at its largest value, an upper bound at its smallest.
        return max(candidates, key=lambda c: (c[0] if lower else -c[0], c[1]))

    declared = declared_types(schema)
    rendered: list[tuple[str, str]] = []
    for applies_to, of_length, low_keys, high_keys in _BOUND_KEYWORDS:
        if declared is not None and not (declared & applies_to):
            continue
        subject = f"len({name})" if of_length else name
        text = _range(subject, side(*low_keys, lower=True), side(*high_keys, lower=False))
        if text is not None:
            rendered.append((_bound_label(applies_to, declared), text))
    if not rendered:
        return None
    if len(rendered) == 1:
        return rendered[0][1]
    return "; ".join(f"{label}: {text}" for label, text in rendered)


def format_schema(schema: Any) -> str:
    """Render a JSON Schema fragment as a Python type annotation string.

    Handles union types (``"type": ["string", "null"]`` → ``str | None``),
    nested arrays (``array<object>`` → ``list[{...}]``), objects with
    inline ``properties`` (rendered as ``{"k": type, ...}``), and a scalar
    ``enum`` / ``const`` (rendered as ``Literal[...]``, the allowed values in
    place of their type; as ``Annotated[<type>, 'one of ...']`` when a value is
    a float, which ``Literal`` does not admit).  Unknown types fall back to
    ``any``.
    """
    if not isinstance(schema, dict):
        return "any"

    allowed = _allowed_values(schema)
    if allowed is not None:
        literal = _literal(allowed)
        if literal is not None:
            return literal
        base = format_schema({k: v for k, v in schema.items() if k not in ("enum", "const")})
        listed = "one of " + ", ".join(repr(v) for v in allowed)
        return f"Annotated[{base}, {listed!r}]"

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

    A top-level parameter with declared value bounds is annotated with them,
    ``top: Annotated[int, '0 < top <= 100']``, so a caller sees the limit
    before it calls rather than from the rejection.
    """
    params = extract_params(tool.input_schema)
    parts: list[str] = []
    for p in params:
        py_type = format_schema(p.schema)
        bounds = _bounds(p.name, p.schema)
        if bounds is not None:
            py_type = f"Annotated[{py_type}, {bounds!r}]"
        part = f"{p.name}: {py_type}"
        if not p.required:
            part += " = None"
        parts.append(part)

    sig = f"{tool.name}({', '.join(parts)}) -> any"
    if tool.description:
        sig += f"\n  {tool.description}"
    return sig

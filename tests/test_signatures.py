"""Tests for schema-to-Python-signature rendering."""

from __future__ import annotations

from fastmcp_gateway.registry import ToolEntry
from fastmcp_gateway.signatures import (
    ParamInfo,
    extract_params,
    format_schema,
    tool_to_signature,
)

# ---------------------------------------------------------------------------
# format_schema: primitive types
# ---------------------------------------------------------------------------


class TestFormatSchemaPrimitives:
    def test_string(self) -> None:
        assert format_schema({"type": "string"}) == "str"

    def test_integer(self) -> None:
        assert format_schema({"type": "integer"}) == "int"

    def test_number(self) -> None:
        assert format_schema({"type": "number"}) == "float"

    def test_boolean(self) -> None:
        assert format_schema({"type": "boolean"}) == "bool"

    def test_null(self) -> None:
        assert format_schema({"type": "null"}) == "None"

    def test_unknown_type_falls_back_to_any(self) -> None:
        assert format_schema({"type": "what-is-this"}) == "what-is-this"

    def test_missing_type_is_any(self) -> None:
        assert format_schema({}) == "any"

    def test_non_dict_schema_is_any(self) -> None:
        assert format_schema(None) == "any"
        assert format_schema("string") == "any"
        assert format_schema(42) == "any"


# ---------------------------------------------------------------------------
# format_schema: arrays
# ---------------------------------------------------------------------------


class TestFormatSchemaArrays:
    def test_plain_array(self) -> None:
        assert format_schema({"type": "array"}) == "list"

    def test_array_of_strings(self) -> None:
        assert format_schema({"type": "array", "items": {"type": "string"}}) == "list[str]"

    def test_array_of_objects(self) -> None:
        assert (
            format_schema(
                {
                    "type": "array",
                    "items": {"type": "object", "properties": {"id": {"type": "integer"}}},
                }
            )
            == 'list[{"id": int}]'
        )


# ---------------------------------------------------------------------------
# format_schema: objects
# ---------------------------------------------------------------------------


class TestFormatSchemaObjects:
    def test_empty_object(self) -> None:
        assert format_schema({"type": "object"}) == "dict"

    def test_object_no_properties(self) -> None:
        assert format_schema({"type": "object", "properties": {}}) == "dict"

    def test_object_with_props_sorted_keys(self) -> None:
        # Keys sorted alphabetically for deterministic output.
        out = format_schema(
            {
                "type": "object",
                "properties": {
                    "z_last": {"type": "integer"},
                    "a_first": {"type": "string"},
                },
            }
        )
        assert out == '{"a_first": str, "z_last": int}'


# ---------------------------------------------------------------------------
# format_schema: unions (type arrays)
# ---------------------------------------------------------------------------


class TestFormatSchemaUnions:
    def test_nullable_string(self) -> None:
        assert format_schema({"type": ["string", "null"]}) == "str | None"

    def test_nullable_integer(self) -> None:
        assert format_schema({"type": ["integer", "null"]}) == "int | None"

    def test_multi_type_union(self) -> None:
        assert format_schema({"type": ["string", "integer"]}) == "str | int"

    def test_only_null_in_union(self) -> None:
        assert format_schema({"type": ["null"]}) == "None"

    def test_empty_type_array(self) -> None:
        assert format_schema({"type": []}) == "any"


# ---------------------------------------------------------------------------
# extract_params: ordering and required/optional
# ---------------------------------------------------------------------------


class TestExtractParams:
    def test_required_before_optional(self) -> None:
        """Required params first, in the order declared by ``required``; optional sorted."""
        schema = {
            "type": "object",
            "properties": {
                "z": {"type": "integer"},
                "query": {"type": "string"},
                "limit": {"type": "integer"},
                "a": {"type": "boolean"},
            },
            "required": ["query", "limit"],
        }
        params = extract_params(schema)
        names = [p.name for p in params]
        # required in declaration order, then optional lexicographic.
        assert names == ["query", "limit", "a", "z"]
        assert params[0].required is True
        assert params[1].required is True
        assert params[2].required is False
        assert params[3].required is False

    def test_missing_required_array_all_optional(self) -> None:
        schema = {
            "type": "object",
            "properties": {"b": {"type": "integer"}, "a": {"type": "string"}},
        }
        params = extract_params(schema)
        assert [p.name for p in params] == ["a", "b"]
        assert all(not p.required for p in params)

    def test_required_references_missing_prop_is_dropped(self) -> None:
        """A ``required`` entry that isn't in ``properties`` is silently skipped."""
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a", "ghost"],
        }
        params = extract_params(schema)
        assert [p.name for p in params] == ["a"]
        assert params[0].required is True

    def test_no_properties_returns_empty(self) -> None:
        assert extract_params({}) == []
        assert extract_params({"type": "object"}) == []
        assert extract_params(None) == []
        assert extract_params({"type": "object", "properties": "not a dict"}) == []

    def test_deduplicates_required(self) -> None:
        """A ``required`` array with duplicate entries should not produce duplicate params."""
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a", "a"],
        }
        params = extract_params(schema)
        assert params == [ParamInfo(name="a", schema={"type": "string"}, required=True)]


# ---------------------------------------------------------------------------
# tool_to_signature: integration
# ---------------------------------------------------------------------------


def _make_tool(
    name: str = "crm_search",
    description: str = "",
    input_schema: dict | None = None,
) -> ToolEntry:
    return ToolEntry(
        name=name,
        domain="crm",
        group="search",
        description=description,
        input_schema=input_schema or {},
        upstream_url="http://crm:8080/mcp",
    )


class TestToolToSignature:
    def test_no_params(self) -> None:
        sig = tool_to_signature(_make_tool())
        assert sig == "crm_search() -> any"

    def test_required_only(self) -> None:
        tool = _make_tool(
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            }
        )
        assert tool_to_signature(tool) == "crm_search(query: str) -> any"

    def test_required_and_optional(self) -> None:
        tool = _make_tool(
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            }
        )
        # Optional params get `= None` and come after required ones.
        assert tool_to_signature(tool) == "crm_search(query: str, limit: int = None) -> any"

    def test_with_description(self) -> None:
        tool = _make_tool(
            description="Search for matching records.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )
        assert tool_to_signature(tool) == "crm_search(query: str) -> any\n  Search for matching records."

    def test_nullable_and_array_params(self) -> None:
        tool = _make_tool(
            input_schema={
                "type": "object",
                "properties": {
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "owner": {"type": ["string", "null"]},
                },
                "required": ["tags"],
            }
        )
        assert tool_to_signature(tool) == "crm_search(tags: list[str], owner: str | None = None) -> any"


# ---------------------------------------------------------------------------
# Allowed values and bounds
# ---------------------------------------------------------------------------


class TestFormatSchemaAllowedValues:
    def test_string_enum_is_a_literal(self) -> None:
        assert format_schema({"type": "string", "enum": ["concise", "detailed"]}) == "Literal['concise', 'detailed']"

    def test_enum_without_a_type_is_a_literal(self) -> None:
        assert format_schema({"enum": ["a", "b"]}) == "Literal['a', 'b']"

    def test_mixed_scalar_enum_is_a_literal(self) -> None:
        assert format_schema({"enum": ["auto", 1, True, None]}) == "Literal['auto', 1, True, None]"

    def test_const_is_a_one_value_literal(self) -> None:
        assert format_schema({"const": "open"}) == "Literal['open']"

    def test_enum_inside_an_array_is_a_literal(self) -> None:
        schema = {"type": "array", "items": {"type": "string", "enum": ["x", "y"]}}
        assert format_schema(schema) == "list[Literal['x', 'y']]"

    def test_nullable_enum_keeps_none(self) -> None:
        assert format_schema({"type": ["string", "null"], "enum": ["a", "b", None]}) == "Literal['a', 'b', None]"

    def test_enum_of_non_scalars_falls_back_to_the_type(self) -> None:
        assert format_schema({"type": "object", "enum": [{"a": 1}]}) == "dict"

    def test_empty_enum_falls_back_to_the_type(self) -> None:
        assert format_schema({"type": "string", "enum": []}) == "str"


class TestToolToSignatureBounds:
    def test_numeric_bounds_annotate_the_parameter(self) -> None:
        tool = _make_tool(
            input_schema={
                "type": "object",
                "properties": {"top": {"type": "integer", "exclusiveMinimum": 0, "maximum": 100}},
            }
        )
        assert tool_to_signature(tool) == "crm_search(top: Annotated[int, '0 < top <= 100'] = None) -> any"

    def test_one_sided_bounds(self) -> None:
        tool = _make_tool(
            input_schema={
                "type": "object",
                "properties": {
                    "min_value": {"type": "number", "minimum": 0},
                    "ratio": {"type": "number", "exclusiveMaximum": 1},
                },
            }
        )
        assert tool_to_signature(tool) == (
            "crm_search(min_value: Annotated[float, 'min_value >= 0'] = None,"
            " ratio: Annotated[float, 'ratio < 1'] = None) -> any"
        )

    def test_length_and_item_bounds(self) -> None:
        tool = _make_tool(
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 80},
                    "ids": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                },
                "required": ["name"],
            }
        )
        assert tool_to_signature(tool) == (
            "crm_search(name: Annotated[str, '1 <= len(name) <= 80'],"
            " ids: Annotated[list[str], 'len(ids) <= 50'] = None) -> any"
        )

    def test_enum_parameter_is_a_literal(self) -> None:
        tool = _make_tool(
            input_schema={
                "type": "object",
                "properties": {"response_format": {"type": "string", "enum": ["concise", "detailed"]}},
            }
        )
        assert tool_to_signature(tool) == "crm_search(response_format: Literal['concise', 'detailed'] = None) -> any"

    def test_a_parameter_without_bounds_is_unchanged(self) -> None:
        tool = _make_tool(
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["query"],
            }
        )
        assert tool_to_signature(tool) == "crm_search(query: str, limit: int = None) -> any"

    def test_non_numeric_bounds_are_ignored(self) -> None:
        tool = _make_tool(
            input_schema={
                "type": "object",
                "properties": {"top": {"type": "integer", "maximum": "100", "minimum": True}},
            }
        )
        assert tool_to_signature(tool) == "crm_search(top: int = None) -> any"


class TestSignatureReviewCases:
    def test_a_float_enum_is_annotated_not_a_literal(self) -> None:
        """``Literal`` admits no float, so the allowed values ride as annotation metadata."""
        assert format_schema({"type": "number", "enum": [1.5, 2.5]}) == "Annotated[float, 'one of 1.5, 2.5']"

    def test_a_mixed_enum_with_a_float_is_annotated(self) -> None:
        assert format_schema({"enum": ["auto", 0.5]}) == "Annotated[any, \"one of 'auto', 0.5\"]"

    def test_an_integral_enum_stays_a_literal(self) -> None:
        assert format_schema({"type": "integer", "enum": [1, 2, 3]}) == "Literal[1, 2, 3]"

    def test_the_stricter_lower_limit_wins(self) -> None:
        tool = _make_tool(
            input_schema={
                "type": "object",
                "properties": {"top": {"type": "integer", "minimum": 10, "exclusiveMinimum": 0}},
            }
        )
        assert tool_to_signature(tool) == "crm_search(top: Annotated[int, 'top >= 10'] = None) -> any"

    def test_the_stricter_upper_limit_wins(self) -> None:
        tool = _make_tool(
            input_schema={
                "type": "object",
                "properties": {"top": {"type": "integer", "maximum": 50, "exclusiveMaximum": 100}},
            }
        )
        assert tool_to_signature(tool) == "crm_search(top: Annotated[int, 'top <= 50'] = None) -> any"

    def test_an_exclusive_limit_wins_a_tie(self) -> None:
        tool = _make_tool(
            input_schema={
                "type": "object",
                "properties": {"top": {"type": "integer", "minimum": 0, "exclusiveMinimum": 0, "maximum": 100}},
            }
        )
        assert tool_to_signature(tool) == "crm_search(top: Annotated[int, '0 < top <= 100'] = None) -> any"

    def test_every_admitted_type_shows_its_bounds(self) -> None:
        """A union of bounded types keeps each one's limit, labelled with its type."""
        tool = _make_tool(
            input_schema={
                "type": "object",
                "properties": {"x": {"type": ["number", "array"], "minimum": 0, "maxItems": 5}},
            }
        )
        signature = tool_to_signature(tool)
        assert "x >= 0" in signature
        assert "len(x) <= 5" in signature

    def test_a_bound_for_an_undeclared_type_is_not_shown(self) -> None:
        """``maximum`` bounds only a number, so it says nothing about a string parameter."""
        tool = _make_tool(input_schema={"type": "object", "properties": {"x": {"type": "string", "maximum": 5}}})
        assert tool_to_signature(tool) == "crm_search(x: str = None) -> any"

    def test_a_nullable_parameter_keeps_its_bounds_unlabelled(self) -> None:
        tool = _make_tool(
            input_schema={"type": "object", "properties": {"x": {"type": ["integer", "null"], "maximum": 9}}}
        )
        assert tool_to_signature(tool) == "crm_search(x: Annotated[int | None, 'x <= 9'] = None) -> any"

    def test_enum_values_the_type_rejects_are_not_listed(self) -> None:
        assert format_schema({"type": "string", "enum": ["open", 1]}) == "Literal['open']"

    def test_an_enum_the_type_rejects_entirely_falls_back_to_the_type(self) -> None:
        assert format_schema({"type": "string", "enum": [1, 2]}) == "str"

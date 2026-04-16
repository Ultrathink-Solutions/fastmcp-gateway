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

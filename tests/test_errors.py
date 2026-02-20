"""Tests for the GatewayError model and error_response helper."""

from __future__ import annotations

import json

from fastmcp_gateway.errors import GatewayError, error_response


class TestGatewayError:
    def test_serialization_without_details(self) -> None:
        err = GatewayError(error="Something broke", code="test_error")
        data = json.loads(err.model_dump_json())

        assert data["error"] == "Something broke"
        assert data["code"] == "test_error"
        assert data["details"] is None

    def test_serialization_with_details(self) -> None:
        err = GatewayError(
            error="Not found",
            code="tool_not_found",
            details={"tool_name": "foo", "suggestions": ["bar", "baz"]},
        )
        data = json.loads(err.model_dump_json())

        assert data["code"] == "tool_not_found"
        assert data["details"]["tool_name"] == "foo"
        assert data["details"]["suggestions"] == ["bar", "baz"]

    def test_roundtrip(self) -> None:
        err = GatewayError(error="Oops", code="test", details={"key": "val"})
        restored = GatewayError.model_validate_json(err.model_dump_json())

        assert restored == err


class TestErrorResponse:
    def test_basic(self) -> None:
        raw = error_response("domain_not_found", "Unknown domain 'x'")
        data = json.loads(raw)

        assert data["error"] == "Unknown domain 'x'"
        assert data["code"] == "domain_not_found"
        assert data["details"] is None

    def test_with_kwargs(self) -> None:
        raw = error_response(
            "tool_not_found",
            "Unknown tool 'foo'",
            tool_name="foo",
            suggestions=["bar"],
        )
        data = json.loads(raw)

        assert data["code"] == "tool_not_found"
        assert data["details"]["tool_name"] == "foo"
        assert data["details"]["suggestions"] == ["bar"]

    def test_parseable_as_gateway_error(self) -> None:
        raw = error_response("execution_error", "Failed", tool="t", domain="d")
        err = GatewayError.model_validate_json(raw)

        assert err.code == "execution_error"
        assert err.details is not None
        assert err.details["tool"] == "t"

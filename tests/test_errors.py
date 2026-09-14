"""Tests for the GatewayError model and error_response helper."""

from __future__ import annotations

import json

import httpx

from fastmcp_gateway.errors import GatewayError, _find_upstream_response, error_response, parse_www_authenticate
from tests.conftest import upstream_status_error


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


class TestParseWwwAuthenticate:
    def test_error_and_scope(self) -> None:
        header = 'Bearer realm="mcp", error="insufficient_scope", scope="widgets:read"'

        assert parse_www_authenticate(header) == ("insufficient_scope", "widgets:read")

    def test_parameters_in_another_order(self) -> None:
        header = 'Bearer scope="widgets:read", error="insufficient_scope", realm="mcp"'

        assert parse_www_authenticate(header) == ("insufficient_scope", "widgets:read")

    def test_missing_scope(self) -> None:
        assert parse_www_authenticate('Bearer realm="mcp", error="invalid_token"') == ("invalid_token", None)

    def test_no_header(self) -> None:
        assert parse_www_authenticate(None) == (None, None)

    def test_empty_header(self) -> None:
        assert parse_www_authenticate("") == (None, None)

    def test_space_delimited_scopes_are_returned_verbatim(self) -> None:
        header = 'Bearer error="insufficient_scope", scope="widgets:read widgets:write"'

        assert parse_www_authenticate(header) == ("insufficient_scope", "widgets:read widgets:write")

    def test_unquoted_token_values(self) -> None:
        header = "Bearer error=insufficient_scope, scope=widgets.read"

        assert parse_www_authenticate(header) == ("insufficient_scope", "widgets.read")

    def test_escaped_quotes_inside_a_quoted_value_are_unescaped(self) -> None:
        header = 'Bearer error="insufficient_scope", scope="widgets:\\"read\\""'

        assert parse_www_authenticate(header) == ("insufficient_scope", 'widgets:"read"')

    def test_scheme_and_parameter_names_are_case_insensitive(self) -> None:
        header = 'bearer ERROR="insufficient_scope", Scope="widgets:read"'

        assert parse_www_authenticate(header) == ("insufficient_scope", "widgets:read")

    def test_non_bearer_challenge(self) -> None:
        assert parse_www_authenticate('Basic realm="legacy"') == (None, None)

    def test_parameters_stop_at_the_next_challenge(self) -> None:
        """A later challenge's parameters never leak into the Bearer result."""
        header = 'Bearer error="insufficient_scope", DPoP scope="other"'

        assert parse_www_authenticate(header) == ("insufficient_scope", None)

    def test_a_second_bearer_challenge_is_ignored(self) -> None:
        """Only the first Bearer challenge is read -- the documented fail-safe."""
        header = 'Bearer error="invalid_token", Bearer error="insufficient_scope", scope="widgets:read"'

        assert parse_www_authenticate(header) == ("invalid_token", None)

    def test_only_the_bearer_challenge_is_read(self) -> None:
        """Repeated ``WWW-Authenticate`` headers reach the caller joined into one value."""
        response = httpx.Response(
            403,
            headers=[
                ("WWW-Authenticate", 'Basic realm="legacy", error="ignored"'),
                ("WWW-Authenticate", 'Bearer error="insufficient_scope", scope="widgets:read"'),
            ],
        )

        assert parse_www_authenticate(response.headers.get("WWW-Authenticate")) == (
            "insufficient_scope",
            "widgets:read",
        )


class TestFindUpstreamResponse:
    def test_response_on_the_exception(self) -> None:
        error = upstream_status_error(403)

        assert _find_upstream_response(error) is error.response

    def test_response_on_an_explicit_cause(self) -> None:
        inner = upstream_status_error(403)
        outer = RuntimeError("upstream call failed")
        outer.__cause__ = inner

        assert _find_upstream_response(outer) is inner.response

    def test_response_inside_an_exception_group(self) -> None:
        inner = upstream_status_error(401)
        group = ExceptionGroup("upstream call failed", [ValueError("unrelated"), inner])

        assert _find_upstream_response(group) is inner.response

    def test_the_outermost_response_wins(self) -> None:
        inner = upstream_status_error(403)
        outer = upstream_status_error(502)
        outer.__cause__ = inner

        assert _find_upstream_response(outer) is outer.response

    def test_group_members_are_searched_before_the_groups_cause(self) -> None:
        member = upstream_status_error(403)
        group = ExceptionGroup("upstream call failed", [member])
        group.__cause__ = upstream_status_error(401)

        assert _find_upstream_response(group) is member.response

    def test_implicit_context_is_not_followed(self) -> None:
        """A failure raised while handling an earlier refusal is its own failure."""
        later = httpx.ConnectError("connection refused")
        later.__context__ = upstream_status_error(401)

        assert _find_upstream_response(later) is None

    def test_no_response(self) -> None:
        assert _find_upstream_response(ValueError("boom")) is None

    def test_cause_cycle_terminates(self) -> None:
        first = RuntimeError("first")
        second = RuntimeError("second")
        first.__cause__ = second
        second.__cause__ = first

        assert _find_upstream_response(first) is None

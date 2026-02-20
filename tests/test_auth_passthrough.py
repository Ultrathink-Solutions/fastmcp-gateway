"""Tests for the get_user_headers auth passthrough helper."""

from __future__ import annotations

from fastmcp_gateway.client_manager import get_user_headers


class TestGetUserHeaders:
    def test_returns_empty_outside_request_context(self) -> None:
        """Outside an HTTP request context, returns an empty dict."""
        headers = get_user_headers()
        assert headers == {}

    def test_include_all_returns_empty_outside_context(self) -> None:
        headers = get_user_headers(include_all=True)
        assert headers == {}

    def test_importable_from_package(self) -> None:
        """get_user_headers is re-exported from the package root."""
        from fastmcp_gateway import get_user_headers as imported

        assert imported is get_user_headers

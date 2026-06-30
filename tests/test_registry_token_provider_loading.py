"""Tests for the GATEWAY_REGISTRY_TOKEN_PROVIDER_MODULE allowlist + loader.

Mirrors ``test_auth_allowlist.py`` — the registry-token-provider loader shares
the same allowlist-gated security invariants as the auth / hook / middleware
loaders, so the tests follow the same layout (parse helper, module-matcher,
loader integration). The loader differs in its contract: the factory returns a
zero-argument *callable* yielding a token string (validated via ``callable()``),
not an ``AuthProvider`` instance.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import patch

import pytest

from fastmcp_gateway import gateway
from fastmcp_gateway._registry_token_provider_loading import (
    _load_registry_token_provider,
    _parse_allowed_registry_token_provider_prefixes,
    _registry_token_provider_module_allowed,
)

_MODULE_ENV = "GATEWAY_REGISTRY_TOKEN_PROVIDER_MODULE"
_PREFIXES_ENV = "GATEWAY_ALLOWED_REGISTRY_TOKEN_PROVIDER_PREFIXES"


# --- _parse_allowed_registry_token_provider_prefixes ----------------------


def test_parse_prefixes_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_PREFIXES_ENV, raising=False)
    assert _parse_allowed_registry_token_provider_prefixes() == []


def test_parse_prefixes_blank_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_PREFIXES_ENV, "   ")
    assert _parse_allowed_registry_token_provider_prefixes() == []


def test_parse_prefixes_multiple_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_PREFIXES_ENV, " a.b , c.d ,  , e.f ")
    assert _parse_allowed_registry_token_provider_prefixes() == ["a.b", "c.d", "e.f"]


def test_parse_prefixes_whitespace_inside_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_PREFIXES_ENV, "a.b, bad prefix")
    with pytest.raises(SystemExit):
        _parse_allowed_registry_token_provider_prefixes()


# --- _registry_token_provider_module_allowed ------------------------------


def test_module_allowed_exact_match() -> None:
    assert _registry_token_provider_module_allowed("my_org.auth", ["my_org.auth"])


def test_module_allowed_dot_boundary() -> None:
    assert _registry_token_provider_module_allowed("my_org.auth.x", ["my_org.auth"])


def test_module_allowed_dot_boundary_rejects_prefix_without_dot() -> None:
    # Without the dot boundary, ``my_org`` would match ``my_org_evil``.
    assert not _registry_token_provider_module_allowed("my_org_evil", ["my_org"])


def test_module_allowed_no_match() -> None:
    assert not _registry_token_provider_module_allowed("other.pkg", ["my_org.auth"])


# --- _load_registry_token_provider integration ----------------------------


def test_load_unset_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_MODULE_ENV, raising=False)
    monkeypatch.delenv(_PREFIXES_ENV, raising=False)
    assert _load_registry_token_provider() is None


def test_load_whitespace_only_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_MODULE_ENV, "   ")
    monkeypatch.setenv(_PREFIXES_ENV, "my_org.auth")
    assert _load_registry_token_provider() is None


def test_load_set_without_allowlist_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Security guard: the module env alone does nothing without the allowlist —
    # an attacker with env-write access can't turn it into a code-injection
    # primitive.
    monkeypatch.setenv(_MODULE_ENV, "any.module:factory")
    monkeypatch.delenv(_PREFIXES_ENV, raising=False)
    assert _load_registry_token_provider() is None


def test_load_mismatched_prefix_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_MODULE_ENV, "untrusted.module:factory")
    monkeypatch.setenv(_PREFIXES_ENV, "my_org.auth")
    with pytest.raises(SystemExit):
        _load_registry_token_provider()


def test_load_malformed_path_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_MODULE_ENV, "my_org.auth_no_colon")
    monkeypatch.setenv(_PREFIXES_ENV, "my_org.auth_no_colon")
    with pytest.raises(SystemExit):
        _load_registry_token_provider()


def test_load_returns_provider_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _provider() -> str:
        return "tok"

    def _factory() -> object:
        return _provider

    module = ModuleType("test_rtp_module")
    module.build_provider = _factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "test_rtp_module", module)
    monkeypatch.setenv(_MODULE_ENV, "test_rtp_module:build_provider")
    monkeypatch.setenv(_PREFIXES_ENV, "test_rtp_module")

    assert _load_registry_token_provider() is _provider


def test_load_factory_returning_none_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # None is a legitimate factory return ("not configured") — the loader treats
    # it the same as the env being unset, so the gateway falls back to the
    # static GATEWAY_REGISTRY_AUTH_TOKEN.
    def _factory() -> None:
        return None

    module = ModuleType("test_rtp_none")
    module.build_provider = _factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "test_rtp_none", module)
    monkeypatch.setenv(_MODULE_ENV, "test_rtp_none:build_provider")
    monkeypatch.setenv(_PREFIXES_ENV, "test_rtp_none")

    assert _load_registry_token_provider() is None


def test_load_factory_returning_non_callable_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Contract: the factory must return a callable (or None). A bare dict would
    # otherwise reach UpstreamManager and fail confusingly at the first fetch.
    def _bad_factory() -> object:
        return {"not": "callable"}

    module = ModuleType("test_rtp_bad_return")
    module.build = _bad_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "test_rtp_bad_return", module)
    monkeypatch.setenv(_MODULE_ENV, "test_rtp_bad_return:build")
    monkeypatch.setenv(_PREFIXES_ENV, "test_rtp_bad_return")

    with pytest.raises(SystemExit):
        _load_registry_token_provider()


def test_load_missing_factory_attr_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("test_rtp_no_factory")
    monkeypatch.setitem(sys.modules, "test_rtp_no_factory", module)
    monkeypatch.setenv(_MODULE_ENV, "test_rtp_no_factory:does_not_exist")
    monkeypatch.setenv(_PREFIXES_ENV, "test_rtp_no_factory")

    with pytest.raises(SystemExit):
        _load_registry_token_provider()


def test_load_non_callable_factory_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("test_rtp_not_callable")
    module.build_provider = "not a function"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "test_rtp_not_callable", module)
    monkeypatch.setenv(_MODULE_ENV, "test_rtp_not_callable:build_provider")
    monkeypatch.setenv(_PREFIXES_ENV, "test_rtp_not_callable")

    with pytest.raises(SystemExit):
        _load_registry_token_provider()


def test_load_import_time_error_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raising_import(name: str, _package: str | None = None) -> None:
        if name == "rtp_raises_at_import":
            raise RuntimeError("simulated import-time config failure")
        return None

    monkeypatch.setattr("importlib.import_module", _raising_import)
    monkeypatch.setenv(_MODULE_ENV, "rtp_raises_at_import:build")
    monkeypatch.setenv(_PREFIXES_ENV, "rtp_raises_at_import")

    with pytest.raises(SystemExit):
        _load_registry_token_provider()


def test_load_factory_raising_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raising_factory() -> object:
        raise RuntimeError("simulated factory-time misconfig")

    module = ModuleType("test_rtp_factory_raises")
    module.build_provider = _raising_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "test_rtp_factory_raises", module)
    monkeypatch.setenv(_MODULE_ENV, "test_rtp_factory_raises:build_provider")
    monkeypatch.setenv(_PREFIXES_ENV, "test_rtp_factory_raises")

    with pytest.raises(SystemExit):
        _load_registry_token_provider()


# --- GatewayServer forwarding ---------------------------------------------


def test_gateway_server_forwards_provider_to_manager() -> None:
    def provider() -> str:
        return "tok"

    # Assert the public forwarding behavior — GatewayServer constructs
    # UpstreamManager with our provider — rather than inspecting private state.
    # wraps= keeps the real construction so the gateway still wires up fully.
    with patch.object(gateway, "UpstreamManager", wraps=gateway.UpstreamManager) as spy:
        gateway.GatewayServer({"crm": "http://crm:8080/mcp"}, registry_token_provider=provider)

    assert spy.call_args is not None
    assert spy.call_args.kwargs["registry_token_provider"] is provider

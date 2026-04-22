"""Tests for the GATEWAY_MIDDLEWARE_MODULE allowlist.

Mirrors the shape of ``test_hook_allowlist.py`` — the two env-driven
loaders share the same security invariants, so the tests follow the
same layout (parse helpers, module-matcher, loader integration).
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from fastmcp_gateway._middleware_loading import (
    _load_middleware,
    _middleware_module_allowed,
    _parse_allowed_middleware_prefixes,
)

# --- _parse_allowed_middleware_prefixes -----------------------------------


def test_parse_allowed_prefixes_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES", raising=False)
    assert _parse_allowed_middleware_prefixes() == []


def test_parse_allowed_prefixes_blank_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES", "   ")
    assert _parse_allowed_middleware_prefixes() == []


def test_parse_allowed_prefixes_single(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES", "my_org.middleware")
    assert _parse_allowed_middleware_prefixes() == ["my_org.middleware"]


def test_parse_allowed_prefixes_multiple_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES", " a.b , c.d ,  , e.f ")
    assert _parse_allowed_middleware_prefixes() == ["a.b", "c.d", "e.f"]


def test_parse_allowed_prefixes_whitespace_inside_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES", "a.b, bad prefix")
    with pytest.raises(SystemExit):
        _parse_allowed_middleware_prefixes()


# --- _middleware_module_allowed -------------------------------------------


def test_module_allowed_exact_match() -> None:
    assert _middleware_module_allowed("my_org.middleware", ["my_org.middleware"]) is True


def test_module_allowed_dot_boundary() -> None:
    assert _middleware_module_allowed("my_org.middleware.host_guard", ["my_org.middleware"]) is True


def test_module_allowed_dot_boundary_rejects_prefix_without_dot() -> None:
    # Without the dot-boundary check, ``my_org`` would match ``my_org_evil``.
    assert _middleware_module_allowed("my_org_evil", ["my_org"]) is False


def test_module_allowed_no_match() -> None:
    assert _middleware_module_allowed("other.pkg", ["my_org.middleware"]) is False


def test_module_allowed_empty_prefix_list() -> None:
    assert _middleware_module_allowed("anything", []) is False


def test_module_allowed_first_matching_prefix_wins() -> None:
    assert _middleware_module_allowed("other.pkg", ["my_org.middleware", "other.pkg", "unused"]) is True


# --- _load_middleware integration -----------------------------------------


def test_load_middleware_unset_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_MIDDLEWARE_MODULE", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES", raising=False)
    assert _load_middleware() is None


def test_load_middleware_whitespace_only_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Whitespace-only is treated as disabled, not as a format error.
    # Common in malformed ``.env`` files or YAML escaping.
    monkeypatch.setenv("GATEWAY_MIDDLEWARE_MODULE", "   ")
    monkeypatch.setenv("GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES", "my_org.middleware")
    assert _load_middleware() is None


def test_load_middleware_set_without_allowlist_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The security guard: GATEWAY_MIDDLEWARE_MODULE alone does nothing.
    # Same posture as GATEWAY_HOOK_MODULE — an attacker with env-write
    # access can't turn this into a code-injection primitive.
    monkeypatch.setenv("GATEWAY_MIDDLEWARE_MODULE", "any.module:factory")
    monkeypatch.delenv("GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES", raising=False)
    assert _load_middleware() is None


def test_load_middleware_mismatched_prefix_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_MIDDLEWARE_MODULE", "untrusted.module:factory")
    monkeypatch.setenv("GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES", "my_org.middleware")
    with pytest.raises(SystemExit):
        _load_middleware()


def test_load_middleware_malformed_path_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Missing ``:`` between module and function.
    monkeypatch.setenv("GATEWAY_MIDDLEWARE_MODULE", "my_org.middleware_no_colon")
    monkeypatch.setenv("GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES", "my_org.middleware_no_colon")
    with pytest.raises(SystemExit):
        _load_middleware()


def test_load_middleware_loads_and_returns_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Register a fake module with a factory so the loader can import it
    # without touching disk.
    sentinel_middleware = [object(), object()]
    module = ModuleType("test_mw_module")

    def _factory() -> list[object]:
        return sentinel_middleware

    module.build_middleware = _factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "test_mw_module", module)
    monkeypatch.setenv("GATEWAY_MIDDLEWARE_MODULE", "test_mw_module:build_middleware")
    monkeypatch.setenv("GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES", "test_mw_module")

    result = _load_middleware()
    assert result is sentinel_middleware


def test_load_middleware_factory_returning_non_list_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Contract: factory must return a list, not e.g. a tuple or a
    # single middleware instance. Enforced so a typo in the shim
    # fails at startup, not at first request with a confusing
    # ``TypeError`` deep inside uvicorn.
    module = ModuleType("test_mw_bad_return")

    def _bad_factory() -> object:
        return object()  # not a list

    module.build = _bad_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "test_mw_bad_return", module)
    monkeypatch.setenv("GATEWAY_MIDDLEWARE_MODULE", "test_mw_bad_return:build")
    monkeypatch.setenv("GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES", "test_mw_bad_return")

    with pytest.raises(SystemExit):
        _load_middleware()


def test_load_middleware_missing_factory_attr_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("test_mw_no_factory")
    monkeypatch.setitem(sys.modules, "test_mw_no_factory", module)
    monkeypatch.setenv("GATEWAY_MIDDLEWARE_MODULE", "test_mw_no_factory:does_not_exist")
    monkeypatch.setenv("GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES", "test_mw_no_factory")

    with pytest.raises(SystemExit):
        _load_middleware()


def test_load_middleware_import_time_runtime_error_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Contract: an import-time exception raised DURING module top-level
    # execution (not an ImportError — e.g. a ``RuntimeError("config missing")``
    # in the factory module's module-level code) must convert to a
    # clean ``SystemExit`` with a logged error, not a raw traceback.
    # Regression test for the broadened ``except Exception`` — the
    # narrow ``except ImportError`` would have let this propagate.
    def _raising_finder(name: str, _package: str | None = None) -> None:
        if name == "mw_raises_at_import":
            raise RuntimeError("simulated import-time config failure")
        return None

    monkeypatch.setattr("importlib.import_module", _raising_finder)
    monkeypatch.setenv("GATEWAY_MIDDLEWARE_MODULE", "mw_raises_at_import:build")
    monkeypatch.setenv("GATEWAY_ALLOWED_MIDDLEWARE_PREFIXES", "mw_raises_at_import")

    with pytest.raises(SystemExit):
        _load_middleware()

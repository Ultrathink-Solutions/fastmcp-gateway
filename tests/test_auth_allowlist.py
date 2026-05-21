"""Tests for the GATEWAY_AUTH_MODULE allowlist.

Mirrors the shape of ``test_hook_allowlist.py`` and
``test_middleware_allowlist.py`` — all three env-driven loaders share
the same security invariants, so the tests follow the same layout
(parse helpers, module-matcher, loader integration).

The auth loader differs from the hook / middleware loaders in two
contract details:

1. It returns a single ``AuthProvider`` instance (not a list). The
   underlying ``FastMCP(auth=...)`` constructor takes one provider.
2. The factory may legitimately return ``None`` — meaning "auth not
   configured for this deployment" — which the loader treats the same
   as not setting the env var.

Both differences get dedicated tests below.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest
from fastmcp.server.auth import AuthProvider

from fastmcp_gateway._auth_loading import (
    _auth_module_allowed,
    _load_auth,
    _parse_allowed_auth_prefixes,
)


class _FakeAuthProvider(AuthProvider):
    """Minimal AuthProvider subclass used as a factory return value.

    AuthProvider is itself instantiable on a recent fastmcp, but we
    subclass it explicitly so the tests stay readable: ``isinstance``
    check against the real base class passes, and the test never has
    to construct a fully-configured real provider (JWKS fetches,
    upstream URLs, etc.) just to exercise the loader's plumbing.
    """

    def __init__(self) -> None:  # pragma: no cover - trivial
        # AuthProvider's __init__ in fastmcp expects base_url + optional
        # routes; bypass it for the test by NOT calling super().__init__.
        # The loader only validates isinstance() and identity, so a
        # bare-bones subclass is sufficient.
        pass

    async def verify_token(self, token: str) -> None:  # pragma: no cover
        return None


# --- _parse_allowed_auth_prefixes -----------------------------------------


def test_parse_allowed_prefixes_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_ALLOWED_AUTH_PREFIXES", raising=False)
    assert _parse_allowed_auth_prefixes() == []


def test_parse_allowed_prefixes_blank_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_ALLOWED_AUTH_PREFIXES", "   ")
    assert _parse_allowed_auth_prefixes() == []


def test_parse_allowed_prefixes_single(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_ALLOWED_AUTH_PREFIXES", "my_org.auth")
    assert _parse_allowed_auth_prefixes() == ["my_org.auth"]


def test_parse_allowed_prefixes_multiple_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_ALLOWED_AUTH_PREFIXES", " a.b , c.d ,  , e.f ")
    assert _parse_allowed_auth_prefixes() == ["a.b", "c.d", "e.f"]


def test_parse_allowed_prefixes_whitespace_inside_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_ALLOWED_AUTH_PREFIXES", "a.b, bad prefix")
    with pytest.raises(SystemExit):
        _parse_allowed_auth_prefixes()


# --- _auth_module_allowed -------------------------------------------------


def test_module_allowed_exact_match() -> None:
    assert _auth_module_allowed("my_org.auth", ["my_org.auth"]) is True


def test_module_allowed_dot_boundary() -> None:
    assert _auth_module_allowed("my_org.auth.proxy", ["my_org.auth"]) is True


def test_module_allowed_dot_boundary_rejects_prefix_without_dot() -> None:
    # Without the dot-boundary check, ``my_org`` would match ``my_org_evil``.
    assert _auth_module_allowed("my_org_evil", ["my_org"]) is False


def test_module_allowed_no_match() -> None:
    assert _auth_module_allowed("other.pkg", ["my_org.auth"]) is False


def test_module_allowed_empty_prefix_list() -> None:
    assert _auth_module_allowed("anything", []) is False


def test_module_allowed_first_matching_prefix_wins() -> None:
    assert _auth_module_allowed("other.pkg", ["my_org.auth", "other.pkg", "unused"]) is True


# --- _load_auth integration -----------------------------------------------


def test_load_auth_unset_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_AUTH_MODULE", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_AUTH_PREFIXES", raising=False)
    assert _load_auth() is None


def test_load_auth_whitespace_only_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_AUTH_MODULE", "   ")
    monkeypatch.setenv("GATEWAY_ALLOWED_AUTH_PREFIXES", "my_org.auth")
    assert _load_auth() is None


def test_load_auth_set_without_allowlist_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Security guard: GATEWAY_AUTH_MODULE alone does nothing without
    # the allowlist. Same posture as the hook / middleware loaders —
    # an attacker with env-write access can't turn this into a
    # code-injection primitive.
    monkeypatch.setenv("GATEWAY_AUTH_MODULE", "any.module:factory")
    monkeypatch.delenv("GATEWAY_ALLOWED_AUTH_PREFIXES", raising=False)
    assert _load_auth() is None


def test_load_auth_mismatched_prefix_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_AUTH_MODULE", "untrusted.module:factory")
    monkeypatch.setenv("GATEWAY_ALLOWED_AUTH_PREFIXES", "my_org.auth")
    with pytest.raises(SystemExit):
        _load_auth()


def test_load_auth_malformed_path_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_AUTH_MODULE", "my_org.auth_no_colon")
    monkeypatch.setenv("GATEWAY_ALLOWED_AUTH_PREFIXES", "my_org.auth_no_colon")
    with pytest.raises(SystemExit):
        _load_auth()


def test_load_auth_loads_and_returns_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Register a fake module with a factory so the loader can import it
    # without touching disk.
    sentinel = _FakeAuthProvider()
    module = ModuleType("test_auth_module")

    def _factory() -> AuthProvider:
        return sentinel

    module.build_auth = _factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "test_auth_module", module)
    monkeypatch.setenv("GATEWAY_AUTH_MODULE", "test_auth_module:build_auth")
    monkeypatch.setenv("GATEWAY_ALLOWED_AUTH_PREFIXES", "test_auth_module")

    result = _load_auth()
    assert result is sentinel


def test_load_auth_factory_returning_none_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The factory contract permits ``None`` as a legitimate return —
    # "auth not configured for this deployment." The loader treats it
    # the same as not setting GATEWAY_AUTH_MODULE; the gateway boots
    # with the prior unauthenticated behaviour. Distinct from the
    # hook / middleware loaders, which require a list.
    module = ModuleType("test_auth_none")

    def _factory() -> None:
        return None

    module.build_auth = _factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "test_auth_none", module)
    monkeypatch.setenv("GATEWAY_AUTH_MODULE", "test_auth_none:build_auth")
    monkeypatch.setenv("GATEWAY_ALLOWED_AUTH_PREFIXES", "test_auth_none")

    assert _load_auth() is None


def test_load_auth_factory_returning_non_provider_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Contract: factory must return an AuthProvider instance (or None).
    # Returning a bare dict / random object would otherwise reach
    # ``FastMCP(auth=...)`` and blow up with a confusing TypeError or,
    # worse, silently disable auth depending on FastMCP's validation
    # posture.
    module = ModuleType("test_auth_bad_return")

    def _bad_factory() -> object:
        return {"not": "an_auth_provider"}

    module.build = _bad_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "test_auth_bad_return", module)
    monkeypatch.setenv("GATEWAY_AUTH_MODULE", "test_auth_bad_return:build")
    monkeypatch.setenv("GATEWAY_ALLOWED_AUTH_PREFIXES", "test_auth_bad_return")

    with pytest.raises(SystemExit):
        _load_auth()


def test_load_auth_missing_factory_attr_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("test_auth_no_factory")
    monkeypatch.setitem(sys.modules, "test_auth_no_factory", module)
    monkeypatch.setenv("GATEWAY_AUTH_MODULE", "test_auth_no_factory:does_not_exist")
    monkeypatch.setenv("GATEWAY_ALLOWED_AUTH_PREFIXES", "test_auth_no_factory")

    with pytest.raises(SystemExit):
        _load_auth()


def test_load_auth_non_callable_factory_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("test_auth_not_callable")
    module.build_auth = "not a function"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "test_auth_not_callable", module)
    monkeypatch.setenv("GATEWAY_AUTH_MODULE", "test_auth_not_callable:build_auth")
    monkeypatch.setenv("GATEWAY_ALLOWED_AUTH_PREFIXES", "test_auth_not_callable")

    with pytest.raises(SystemExit):
        _load_auth()


def test_load_auth_import_time_runtime_error_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Contract: an import-time exception raised DURING module top-level
    # execution converts to a clean ``SystemExit`` with a logged error,
    # not a raw traceback. Same posture as the hook / middleware
    # loaders — narrow ``except ImportError`` would have let this
    # propagate.
    def _raising_finder(name: str, _package: str | None = None) -> None:
        if name == "auth_raises_at_import":
            raise RuntimeError("simulated import-time config failure")
        return None

    monkeypatch.setattr("importlib.import_module", _raising_finder)
    monkeypatch.setenv("GATEWAY_AUTH_MODULE", "auth_raises_at_import:build")
    monkeypatch.setenv("GATEWAY_ALLOWED_AUTH_PREFIXES", "auth_raises_at_import")

    with pytest.raises(SystemExit):
        _load_auth()


def test_load_auth_factory_raising_runtime_error_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Contract: an exception raised by the factory itself (e.g. validating
    # its own env vars and bailing before returning an AuthProvider) is
    # caught, logged with full traceback, and converted to ``SystemExit``.
    # The alternative — letting the exception propagate — surfaces as a
    # raw traceback inside uvicorn at server start, which is harder to
    # correlate under structured logging and gives no operator-friendly
    # signal that the misconfig is in the auth factory specifically.
    module = ModuleType("test_auth_factory_raises")

    def _raising_factory() -> object:
        raise RuntimeError("simulated factory-time misconfig")

    module.build_auth = _raising_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "test_auth_factory_raises", module)
    monkeypatch.setenv("GATEWAY_AUTH_MODULE", "test_auth_factory_raises:build_auth")
    monkeypatch.setenv("GATEWAY_ALLOWED_AUTH_PREFIXES", "test_auth_factory_raises")

    with pytest.raises(SystemExit):
        _load_auth()

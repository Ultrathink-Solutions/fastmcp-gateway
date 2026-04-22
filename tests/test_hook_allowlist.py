"""Tests for the GATEWAY_HOOK_MODULE allowlist and code-mode authorizer gate."""

from __future__ import annotations

import pytest

from fastmcp_gateway._hook_loading import (
    _hook_module_allowed,
    _load_hooks,
    _parse_allowed_hook_prefixes,
)
from fastmcp_gateway.gateway import CodeModeAuthorizerRequiredError, GatewayServer

# --- _parse_allowed_hook_prefixes -----------------------------------------


def test_parse_allowed_prefixes_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_ALLOWED_HOOK_PREFIXES", raising=False)
    assert _parse_allowed_hook_prefixes() == []


def test_parse_allowed_prefixes_blank_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_ALLOWED_HOOK_PREFIXES", "   ")
    assert _parse_allowed_hook_prefixes() == []


def test_parse_allowed_prefixes_single(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_ALLOWED_HOOK_PREFIXES", "my_org.hooks")
    assert _parse_allowed_hook_prefixes() == ["my_org.hooks"]


def test_parse_allowed_prefixes_multiple_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_ALLOWED_HOOK_PREFIXES", " a.b , c.d ,  , e.f ")
    assert _parse_allowed_hook_prefixes() == ["a.b", "c.d", "e.f"]


def test_parse_allowed_prefixes_whitespace_inside_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_ALLOWED_HOOK_PREFIXES", "a.b, bad prefix")
    with pytest.raises(SystemExit):
        _parse_allowed_hook_prefixes()


# --- _hook_module_allowed -------------------------------------------------


def test_module_allowed_exact_match() -> None:
    assert _hook_module_allowed("my_org.hooks", ["my_org.hooks"]) is True


def test_module_allowed_dot_boundary() -> None:
    assert _hook_module_allowed("my_org.hooks.auth", ["my_org.hooks"]) is True


def test_module_allowed_dot_boundary_rejects_prefix_without_dot() -> None:
    """Without the dot-boundary check, ``my_org`` would match ``my_org_evil``."""
    assert _hook_module_allowed("my_org_evil", ["my_org"]) is False


def test_module_allowed_no_match() -> None:
    assert _hook_module_allowed("other.pkg", ["my_org.hooks"]) is False


def test_module_allowed_empty_prefix_list() -> None:
    assert _hook_module_allowed("anything", []) is False


def test_module_allowed_first_matching_prefix_wins() -> None:
    assert _hook_module_allowed("other.pkg", ["my_org.hooks", "other.pkg", "unused"]) is True


# --- _load_hooks integration ----------------------------------------------


def test_load_hooks_unset_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_HOOK_MODULE", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_HOOK_PREFIXES", raising=False)
    assert _load_hooks() is None


def test_load_hooks_whitespace_only_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Whitespace-only is treated as disabled, not as a format error.
    # Parity with ``_middleware_loading._load_middleware``.
    monkeypatch.setenv("GATEWAY_HOOK_MODULE", "   ")
    monkeypatch.setenv("GATEWAY_ALLOWED_HOOK_PREFIXES", "my_org.hooks")
    assert _load_hooks() is None


def test_load_hooks_import_time_runtime_error_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Parity regression test for the broadened ``except Exception``:
    # an import-time ``RuntimeError`` in the factory module's
    # top-level code must convert to ``SystemExit`` with a logged
    # error, not a raw traceback. Same contract as
    # ``test_load_middleware_import_time_runtime_error_exits``.
    def _raising_finder(name: str, _package: str | None = None) -> None:
        if name == "hook_raises_at_import":
            raise RuntimeError("simulated import-time config failure")
        return None

    monkeypatch.setattr("importlib.import_module", _raising_finder)
    monkeypatch.setenv("GATEWAY_HOOK_MODULE", "hook_raises_at_import:build")
    monkeypatch.setenv("GATEWAY_ALLOWED_HOOK_PREFIXES", "hook_raises_at_import")

    with pytest.raises(SystemExit):
        _load_hooks()


def test_load_hooks_set_without_allowlist_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The security guard: GATEWAY_HOOK_MODULE alone does nothing."""
    monkeypatch.setenv("GATEWAY_HOOK_MODULE", "any.module:factory")
    monkeypatch.delenv("GATEWAY_ALLOWED_HOOK_PREFIXES", raising=False)
    assert _load_hooks() is None


def test_load_hooks_mismatched_prefix_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_HOOK_MODULE", "untrusted.module:factory")
    monkeypatch.setenv("GATEWAY_ALLOWED_HOOK_PREFIXES", "my_org.hooks")
    with pytest.raises(SystemExit):
        _load_hooks()


# --- GatewayServer code_mode_authorizer gate ------------------------------


def test_code_mode_true_without_authorizer_raises() -> None:
    """Auto-discovery removal: code_mode=True requires an explicit callback."""
    # Loose ``match`` — we assert the exception *type* and a minimal
    # stable hint, not the full error prose. Message wording can evolve
    # without breaking this regression shield.
    with pytest.raises(CodeModeAuthorizerRequiredError, match=r"code_mode"):
        GatewayServer(
            {"dom": "http://upstream:8080/mcp"},
            code_mode=True,
            code_mode_authorizer=None,
        )


def test_code_mode_true_non_callable_authorizer_raises() -> None:
    with pytest.raises(TypeError, match=r"async function"):
        GatewayServer(
            {"dom": "http://upstream:8080/mcp"},
            code_mode=True,
            code_mode_authorizer="not-callable",  # type: ignore[arg-type]
        )


def test_code_mode_true_sync_callable_authorizer_raises() -> None:
    """Regression: a sync function must be rejected at construction time,
    not at the first await. ``callable()`` returns True for
    ``lambda u, c: True``, but awaiting it blows up with
    ``TypeError: object bool can't be used in 'await' expression``
    the first time ``execute_code`` runs — a runtime-only landmine the
    old ``callable()`` check let through. ``iscoroutinefunction`` fails
    fast here instead.
    """
    with pytest.raises(TypeError, match=r"async function"):
        GatewayServer(
            {"dom": "http://upstream:8080/mcp"},
            code_mode=True,
            code_mode_authorizer=lambda _u, _c: True,  # sync — wrong shape  # type: ignore[arg-type]
        )


def test_code_mode_false_with_no_authorizer_is_fine() -> None:
    """The gate only fires when code_mode is actually on."""
    gateway = GatewayServer(
        {"dom": "http://upstream:8080/mcp"},
        code_mode=False,
    )
    assert gateway is not None


def test_code_mode_false_accepts_sync_authorizer() -> None:
    """``code_mode=False`` skips the async-shape validation entirely.

    The authorizer is only dereferenced inside the code-mode execution
    path, so a sync (or otherwise wrong-shape) authorizer passed
    alongside ``code_mode=False`` is never invoked and must not be
    rejected at construction. Accepting it lets callers pass a single
    authorizer object regardless of whether code mode is on this time.
    """
    gateway = GatewayServer(
        {"dom": "http://upstream:8080/mcp"},
        code_mode=False,
        code_mode_authorizer=lambda _u, _c: True,  # sync; not used when code_mode=False  # type: ignore[arg-type]
    )
    assert gateway is not None


# --- main() CLI graceful exit on code-mode misconfig ----------------------


def test_main_exits_with_friendly_error_when_code_mode_without_authorizer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """GATEWAY_CODE_MODE=true via CLI should exit cleanly, not traceback.

    The ValueError raised by GatewayServer when code_mode=True without an
    authorizer would otherwise bubble out of main() and produce a
    confusing traceback for operators. The catch in main() turns it into
    sys.exit(1) with an actionable log message.
    """
    import logging

    from fastmcp_gateway.__main__ import main

    monkeypatch.setenv(
        "GATEWAY_UPSTREAMS",
        '{"dom": "http://upstream:8080/mcp"}',
    )
    monkeypatch.setenv("GATEWAY_CODE_MODE", "true")
    monkeypatch.delenv("GATEWAY_HOOK_MODULE", raising=False)
    monkeypatch.delenv("GATEWAY_REGISTRATION_TOKEN", raising=False)

    with caplog.at_level(logging.ERROR, logger="fastmcp_gateway"), pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1
    assert any(
        "GATEWAY_CODE_MODE=true is no longer supported via the CLI" in record.message for record in caplog.records
    )

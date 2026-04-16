"""Tests for the __main__ entry point configuration."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from fastmcp_gateway.__main__ import _load_json_env


class TestLoadJsonEnv:
    """Tests for _load_json_env helper."""

    def test_returns_none_when_not_set(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            assert _load_json_env("MISSING_VAR") is None

    def test_returns_none_when_empty(self) -> None:
        with patch.dict("os.environ", {"MY_VAR": ""}):
            assert _load_json_env("MY_VAR") is None

    def test_parses_valid_json(self) -> None:
        data = {"key": "value", "count": 42}
        with patch.dict("os.environ", {"MY_VAR": json.dumps(data)}):
            result = _load_json_env("MY_VAR")
        assert result == data

    def test_exits_on_invalid_json(self) -> None:
        with patch.dict("os.environ", {"MY_VAR": "not-json"}), pytest.raises(SystemExit):
            _load_json_env("MY_VAR")

    def test_exits_on_non_object(self) -> None:
        with patch.dict("os.environ", {"MY_VAR": '["list", "not", "dict"]'}), pytest.raises(SystemExit):
            _load_json_env("MY_VAR")

    def test_exits_when_required_and_missing(self) -> None:
        with patch.dict("os.environ", {}, clear=True), pytest.raises(SystemExit):
            _load_json_env("REQUIRED_VAR", required=True)

    def test_required_with_valid_json(self) -> None:
        with patch.dict("os.environ", {"MY_VAR": '{"a": 1}'}):
            result = _load_json_env("MY_VAR", required=True)
        assert result == {"a": 1}


class TestBoolEnv:
    """Tests for _bool_env strict-token parser."""

    def test_recognises_true_tokens(self) -> None:
        from fastmcp_gateway.__main__ import _bool_env

        for token in ("true", "True", "TRUE", "1", "yes", "YES", "on", "On"):
            with patch.dict("os.environ", {"MY_VAR": token}):
                assert _bool_env("MY_VAR") is True, f"{token!r} should be True"

    def test_recognises_false_tokens(self) -> None:
        from fastmcp_gateway.__main__ import _bool_env

        for token in ("false", "False", "FALSE", "0", "no", "off"):
            with patch.dict("os.environ", {"MY_VAR": token}):
                assert _bool_env("MY_VAR") is False, f"{token!r} should be False"

    def test_empty_returns_default(self) -> None:
        from fastmcp_gateway.__main__ import _bool_env

        with patch.dict("os.environ", {"MY_VAR": ""}):
            assert _bool_env("MY_VAR", default=True) is True
            assert _bool_env("MY_VAR", default=False) is False

    def test_unknown_token_exits(self) -> None:
        """Typos must fail fast rather than silently disabling a feature."""
        from fastmcp_gateway.__main__ import _bool_env

        # A plausible typo for "true" — the old behaviour was to silently
        # return False, which disabled the feature without any warning.
        with patch.dict("os.environ", {"MY_VAR": "treu"}), pytest.raises(SystemExit):
            _bool_env("MY_VAR")


class TestLoadCodeModeConfig:
    """Tests for _load_code_mode_config limit-validation paths."""

    def test_disabled_short_circuits(self) -> None:
        from fastmcp_gateway.__main__ import _load_code_mode_config

        with patch.dict("os.environ", {"GATEWAY_CODE_MODE": "false"}, clear=True):
            enabled, limits, verbatim = _load_code_mode_config()
        assert enabled is False
        assert limits is None
        assert verbatim is False

    def test_zero_duration_rejected(self) -> None:
        """Zero or negative caps would degrade the sandbox to no-limit."""
        from fastmcp_gateway.__main__ import _load_code_mode_config

        with (
            patch.dict(
                "os.environ",
                {
                    "GATEWAY_CODE_MODE": "true",
                    "GATEWAY_CODE_MODE_MAX_DURATION_SECS": "0",
                },
                clear=True,
            ),
            pytest.raises(SystemExit),
        ):
            _load_code_mode_config()

    def test_negative_nested_calls_rejected(self) -> None:
        from fastmcp_gateway.__main__ import _load_code_mode_config

        with (
            patch.dict(
                "os.environ",
                {
                    "GATEWAY_CODE_MODE": "true",
                    "GATEWAY_CODE_MODE_MAX_NESTED_CALLS": "-1",
                },
                clear=True,
            ),
            pytest.raises(SystemExit),
        ):
            _load_code_mode_config()

    def test_nan_duration_rejected(self) -> None:
        from fastmcp_gateway.__main__ import _load_code_mode_config

        with (
            patch.dict(
                "os.environ",
                {
                    "GATEWAY_CODE_MODE": "true",
                    "GATEWAY_CODE_MODE_MAX_DURATION_SECS": "nan",
                },
                clear=True,
            ),
            pytest.raises(SystemExit),
        ):
            _load_code_mode_config()

    def test_valid_overrides_accepted(self) -> None:
        from fastmcp_gateway.__main__ import _load_code_mode_config

        with patch.dict(
            "os.environ",
            {
                "GATEWAY_CODE_MODE": "true",
                "GATEWAY_CODE_MODE_MAX_DURATION_SECS": "10",
                "GATEWAY_CODE_MODE_MAX_NESTED_CALLS": "25",
            },
            clear=True,
        ):
            enabled, limits, verbatim = _load_code_mode_config()
        assert enabled is True
        assert limits is not None
        assert limits.max_duration_secs == 10
        assert limits.max_nested_calls == 25
        assert verbatim is False

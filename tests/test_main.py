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

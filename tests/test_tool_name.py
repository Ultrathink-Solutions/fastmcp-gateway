"""Tests for the tool-name validator (pure function).

Integration tests that exercise ``ToolRegistry.register_tool`` rejecting
invalid names live in ``tests/test_registry.py`` alongside the rest of
the registry suite.
"""

from __future__ import annotations

import builtins
import keyword

import pytest

from fastmcp_gateway.tool_name import validate_tool_name

# ---------------------------------------------------------------------------
# validate_tool_name — unit tests
# ---------------------------------------------------------------------------


class TestValidateToolNameAccepts:
    """Positive cases — legitimate tool names must validate."""

    @pytest.mark.parametrize(
        "name",
        [
            "search",
            "apollo_people_search",
            "hubspot_contacts_bulk_create",
            "get_tool_schema",
            "list_tools",
            "x",  # minimum length
            "a" * 64,  # maximum length
            "a1_b2_c3",
            "my_tool_v2",
            # Upstream MCP servers advertise camelCase tool names,
            # so the validator must accept them — a strict
            # snake-case rule would reject valid production tools
            # while delivering no additional security benefit.
            "domain_executeQuery",
            "domain_listItems",
            "executeQuery",
        ],
    )
    def test_accepts_legitimate_name(self, name: str) -> None:
        assert validate_tool_name(name) is None


class TestValidateToolNameRejects:
    """Negative cases — unsafe names must be rejected with a reason."""

    def test_empty_string(self) -> None:
        reason = validate_tool_name("")
        assert reason is not None
        assert "empty" in reason

    @pytest.mark.parametrize(
        "name",
        [
            "_leading_underscore",  # underscore first
            "1_leading_digit",  # digit first
            "Capitalized",  # uppercase first
            "has-hyphen",  # hyphen not allowed
            "has space",  # space not allowed
            "has.dot",  # dot not allowed
            "has/slash",  # slash not allowed
            "UPPER_CASE",  # any uppercase
            "a" * 65,  # over length limit
        ],
    )
    def test_rejects_non_conformant(self, name: str) -> None:
        reason = validate_tool_name(name)
        assert reason is not None
        assert "shape" in reason

    @pytest.mark.parametrize(
        "name",
        [
            "__class__",
            "__init__",
            "__import__",
            "__dict__",
            "__getattribute__",
            "_single_leading_underscore",
        ],
    )
    def test_rejects_underscore_prefixed(self, name: str) -> None:
        # Regex alone covers this — first char must be [a-z].
        # Regression guard against a future refactor that relaxes the
        # regex without re-establishing the dunder ban.
        assert validate_tool_name(name) is not None

    @pytest.mark.parametrize(
        "name",
        [
            "eval",
            "exec",
            "compile",
            "open",
            "input",
            "type",
            "globals",
            "locals",
            "setattr",
            "getattr",
            "hasattr",
            "delattr",
        ],
    )
    def test_rejects_builtin_shadowing(self, name: str) -> None:
        reason = validate_tool_name(name)
        assert reason is not None
        assert "keyword or builtin" in reason

    @pytest.mark.parametrize(
        "name",
        [
            "class",
            "def",
            "for",
            "if",
            "else",
            "while",
            "return",
            "yield",
            "import",
            "from",
            "lambda",
            "pass",
            "break",
            "continue",
            "try",
            "except",
            "finally",
            "raise",
            "with",
            "global",
            "nonlocal",
            "assert",
            "del",
        ],
    )
    def test_rejects_keyword_shadowing(self, name: str) -> None:
        reason = validate_tool_name(name)
        assert reason is not None
        assert "keyword or builtin" in reason

    def test_rejects_every_regex_legal_builtin(self) -> None:
        """Every builtin that would otherwise pass the regex is on the denylist.

        Compiles the same intersection the validator uses so the test
        doesn't silently drift when a future Python adds a new
        regex-legal builtin. If a newly-added builtin somehow gets
        past the validator, this test flags it.
        """
        regex_legal_names = {n for n in dir(builtins) if n and n[0].islower() and n.isidentifier()}
        for name in regex_legal_names:
            if len(name) > 64 or not name.replace("_", "").isalnum():
                continue
            assert validate_tool_name(name) is not None, f"Regex-legal builtin {name!r} slipped past the validator"

    def test_rejects_every_keyword(self) -> None:
        """Every regex-legal Python keyword (hard + soft) is on the denylist.

        The validator compiles its denylist from ``keyword.kwlist`` +
        ``keyword.softkwlist`` + ``dir(builtins)`` at import time. Hard
        keywords (``class``, ``return``, ``import``) and soft keywords
        (``match``, ``case``, ``type`` — introduced in Python 3.10+ for
        structural pattern matching and ``type`` statement) must both be
        rejected so a future ``kw.list`` or interpreter-version change
        can't silently regress coverage.
        """
        for kw in keyword.kwlist:
            # Skip keywords that wouldn't pass the regex anyway (e.g. None, True, False)
            if not kw or not kw[0].islower() or not kw.replace("_", "").isalnum():
                continue
            assert validate_tool_name(kw) is not None, f"Keyword {kw!r} slipped past"

        for soft_kw in getattr(keyword, "softkwlist", ()):
            # Same skip conditions — a soft keyword like ``_`` (reinstated
            # in 3.10 for pattern matching) doesn't start with a lowercase
            # letter and is rejected by the regex, not the denylist.
            if not soft_kw or not soft_kw[0].islower() or not soft_kw.replace("_", "").isalnum():
                continue
            assert validate_tool_name(soft_kw) is not None, f"Soft keyword {soft_kw!r} slipped past"

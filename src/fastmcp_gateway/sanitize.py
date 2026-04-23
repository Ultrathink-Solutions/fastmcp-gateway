"""Description sanitizer + inputSchema validator for registry ingest.

Runs per-tool during ToolRegistry.populate_domain, before ToolEntry
construction. Description sanitation is strip-and-log (never reject) so
one poisoned tool does not DoS sibling tools on the same upstream.
Schema validation IS strict — a malformed inputSchema is rejected and
the offending tool is skipped (blast radius bounded to that one tool).

Upstream MCP tool descriptions flow verbatim into discover_tools output
and (when code_mode=True) into Python type rendering inside the sandbox
namespace. A compromised upstream can inject LLM-targeted instructions
into the description field, and a malformed inputSchema can introduce
sandbox-escape primitives via schema shape. This module is the protection
floor that runs before either of those surfaces sees upstream data.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

from fastmcp_gateway.injection_patterns import INJECTION_FLAGS, INJECTION_PATTERNS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Description sanitizer
# ---------------------------------------------------------------------------

# Compile the injection pattern denylist once at import time. The flags
# (INJECTION_FLAGS) carry IGNORECASE so case-variant attempts like
# ``IGNORE ALL PREVIOUS INSTRUCTIONS`` also match; the DOTALL flag lets
# attacker-inserted newlines inside ``<system ...>`` tags still match.
_INJECTION_PATTERN: re.Pattern[str] = re.compile("|".join(INJECTION_PATTERNS), INJECTION_FLAGS)

# Zero-width / bidi-control / format characters that adversaries use to
# smuggle patterns past a surface-string denylist. Stripping these BEFORE
# the injection scan means ``ig​nore previous instructions`` collapses
# to ``ignore previous instructions`` and is caught by the pattern pass.
# Coverage:
#   * Zero-width joiners / non-joiners / spaces and BOM — invisible
#     between-letter splitters that keep the visual form readable.
#   * Word joiner (U+2060) — older mechanism for invisible adjacency.
#   * Line / paragraph separators — can end the visible line mid-phrase.
#   * LRE / RLE / PDF / LRO / RLO / LRI / RLI / FSI / PDI — bidi /
#     directional format controls (U+202A..U+202E, U+2066..U+2069) that
#     let an attacker visually reorder text so the denylist scan sees
#     one string while a human reviewer sees another.
# Explicit \u escapes so lint doesn't flag these literals as ambiguous.
_ZERO_WIDTH_CHARS: frozenset[str] = frozenset(
    {
        "\u200b",  # ZERO WIDTH SPACE
        "\u200c",  # ZERO WIDTH NON-JOINER
        "\u200d",  # ZERO WIDTH JOINER
        "\u2060",  # WORD JOINER
        "\ufeff",  # ZERO WIDTH NO-BREAK SPACE / BOM
        "\u2028",  # LINE SEPARATOR
        "\u2029",  # PARAGRAPH SEPARATOR
        "\u202a",  # LEFT-TO-RIGHT EMBEDDING
        "\u202b",  # RIGHT-TO-LEFT EMBEDDING
        "\u202c",  # POP DIRECTIONAL FORMATTING
        "\u202d",  # LEFT-TO-RIGHT OVERRIDE
        "\u202e",  # RIGHT-TO-LEFT OVERRIDE
        "\u2066",  # LEFT-TO-RIGHT ISOLATE
        "\u2067",  # RIGHT-TO-LEFT ISOLATE
        "\u2068",  # FIRST STRONG ISOLATE
        "\u2069",  # POP DIRECTIONAL ISOLATE
    }
)

# Maximum description length. Keeps oversized payloads from bloating the
# gateway's ``discover_tools`` response and from occupying gratuitous
# LLM context. 2048 is enough for legitimate tool descriptions (typical
# upstream tools use 80-400 chars) and small enough to keep log lines
# and traces tractable when we audit-log a sanitation event.
_MAX_DESCRIPTION_LEN = 2048
_TRUNCATION_MARKER = " [truncated]"


def _strip_control_chars(raw: str) -> str:
    """Remove C0 controls (except tab/newline), DEL, and zero-width chars.

    Unicode category ``Cc`` covers C0 + C1 controls. We keep ``\\t``,
    ``\\n``, and ``\\r`` because they occur in legitimate multi-line tool
    descriptions; everything else in that category is stripped. Zero-width
    and bidi-control characters (category ``Cf`` subset) are stripped
    explicitly so they can't be used to split a denylist pattern across
    an invisible boundary.
    """
    out_chars: list[str] = []
    for ch in raw:
        if ch in _ZERO_WIDTH_CHARS:
            continue
        if ch in ("\t", "\n", "\r"):
            out_chars.append(ch)
            continue
        if unicodedata.category(ch) == "Cc":
            # C0/C1 controls (including U+007F DEL) — drop.
            continue
        out_chars.append(ch)
    return "".join(out_chars)


def sanitize_description(raw: Any, *, skip_pattern_scan: bool = False) -> str:
    """Sanitize a tool description for safe registry ingest.

    Pipeline:

    1. Type-guard: non-str input returns empty string (with WARNING log)
       rather than raising — a poisoned upstream returning a non-string
       must not DoS the whole populate batch.
    2. Unicode NFC normalization so combining-character sequences (e.g.
       decomposed ``é``) collapse to their canonical composed form
       before the pattern scan.
    3. Strip C0 controls (except tab/newline/CR), DEL, and zero-width
       characters. Adversaries use zero-width chars to smuggle patterns
       past a surface-string denylist.
    4. Scan against the shared injection-pattern denylist; replace each
       match with a single space and emit a WARNING audit log per strip.
    5. Cap at :data:`_MAX_DESCRIPTION_LEN` characters; append
       ``" [truncated]"`` when truncation was applied so downstream
       consumers can tell the description is elided.

    Never raises. Always returns a string.

    Parameters
    ----------
    raw:
        Value from the upstream tool's ``description`` field. May be any
        type (``None``, ``int``, ``dict``…); non-str inputs produce an
        empty-string result.
    skip_pattern_scan:
        When ``True``, skip step 4 (the injection-pattern scrub) only.
        Unicode normalization, control-character stripping, and length
        capping still apply. Operators use this for legitimate
        prompt-processing tools whose descriptions intentionally
        contain denylist tokens — the override is scoped to the
        pattern scan so always-on hygiene (NFC, control strip, length
        cap) is never weakened.

    Returns
    -------
    The sanitized description.
    """
    if not isinstance(raw, str):
        # Log only the type name, not ``raw`` itself: a poisoned upstream
        # could return any object (including one whose ``repr()`` injects
        # attacker-controlled text into ops log aggregators or pollutes
        # audit trails with oversized payloads).
        logger.warning(
            "Tool description is not a string: type=%s — substituting empty string",
            type(raw).__name__,
        )
        return ""

    # NFC normalize before stripping so decomposed-combining-char forms
    # are canonicalized (``é`` → ``é``). This matters for the
    # downstream pattern scan when an attacker builds the keyword from
    # canonical-equivalent code points.
    normalized = unicodedata.normalize("NFC", raw)
    stripped = _strip_control_chars(normalized)

    if skip_pattern_scan:
        # Operator-configured trusted domain. Hygiene steps above still
        # ran; only the denylist scrub is skipped. The length cap below
        # remains in force.
        scrubbed = stripped
    else:
        # Injection-pattern scan. ``sub`` with a callback replaces every
        # match with a single space so the scrubbed form keeps
        # whitespace-delimited tokens intact (``ignore previous
        # instructions foo`` → ``  foo``, still parseable as words).
        #
        # Audit-log strategy: emit exactly ONE log line per sanitize
        # invocation regardless of match count.  A long adversarial
        # description could contain dozens of denylist hits; logging
        # per-match would let an upstream amplify its message into ops
        # log aggregators by N times (log-line-flood DoS).  The closure-
        # scoped counter records the first match's offset + length for
        # triage, then swallows subsequent matches silently.  A match
        # count is appended to the single log line so the pattern-
        # density signal is preserved for incident analysis.
        match_count = 0
        first_offset = -1
        first_length = 0

        def _replace(match: re.Match[str]) -> str:
            nonlocal match_count, first_offset, first_length
            if match_count == 0:
                first_offset = match.start()
                first_length = match.end() - match.start()
            match_count += 1
            return " "

        scrubbed = _INJECTION_PATTERN.sub(_replace, stripped)
        if match_count > 0:
            # Log only safe metadata.  The ``match.group(0)`` payload is
            # attacker-controlled (credential strings, oversized content,
            # etc.), so we emit only offset / length of the first match
            # plus the total match count.
            logger.warning(
                "Stripped injection pattern from tool description: first_offset=%d first_length=%d match_count=%d",
                first_offset,
                first_length,
                match_count,
            )

    # Length cap. Checked on the post-scrub form so oversized inputs
    # that compress under the scrub step aren't truncated unnecessarily.
    if len(scrubbed) > _MAX_DESCRIPTION_LEN:
        # Reserve room for the marker so the final string fits within
        # the advertised cap — we don't want ``len(result) > cap`` when
        # the caller is budgeting bytes.
        keep = _MAX_DESCRIPTION_LEN - len(_TRUNCATION_MARKER)
        return scrubbed[:keep] + _TRUNCATION_MARKER

    return scrubbed


# ---------------------------------------------------------------------------
# inputSchema validator
# ---------------------------------------------------------------------------


class SchemaValidationError(ValueError):
    """Raised when an upstream inputSchema fails ingest validation.

    Subclasses :class:`ValueError` so existing callers that catch the
    broad type keep working, while a dedicated type lets registry
    population route the error into a structured audit log without
    brittle string matching.
    """


# Depth at which schema recursion is considered pathological. Legitimate
# MCP tool schemas almost never exceed 3 levels of nesting
# (``properties.foo.items.properties.bar`` is already depth 4). A cap of
# 5 accommodates edge cases without giving adversaries room to build
# gigantic nested structures designed to exhaust the sandbox's type
# renderer.
_MAX_SCHEMA_DEPTH = 5


def _contains_ref(node: Any, depth: int = 0) -> bool:
    """Recursively check whether any dict in *node* contains a ``$ref`` key.

    ``$ref`` is rejected outright: resolving it requires fetching an
    external document (or walking back to the root schema), which
    introduces a TOCTOU window between validation and the time the
    sandbox type renderer reads the schema. Static inline schemas are
    strictly more auditable.
    """
    if depth > _MAX_SCHEMA_DEPTH:
        # Fail closed: conservatively treat an unexplored subtree as
        # potentially containing a ``$ref``. In the current caller
        # ordering inside :func:`validate_input_schema`, the depth
        # check fires first and rejects over-deep schemas before this
        # function is invoked on them — so this branch is
        # unreachable in practice today. But returning ``False`` here
        # would create an ordering dependency: a future refactor that
        # reordered the checks (or reused ``_contains_ref`` from a
        # different callsite that did not gate on depth) could let a
        # pathologically-deep schema with a hidden ``$ref`` pass the
        # ref check silently. Returning ``True`` closes that class of
        # bug regardless of caller ordering.
        return True
    if isinstance(node, dict):
        if "$ref" in node:
            return True
        return any(_contains_ref(v, depth + 1) for v in node.values())
    if isinstance(node, list):
        return any(_contains_ref(item, depth + 1) for item in node)
    return False


def _schema_depth(node: Any, depth: int = 0) -> int:
    """Return the maximum nesting depth of dicts/lists in *node*.

    Each dict or list increments the depth counter. Used to reject
    pathologically nested schemas before they reach the sandbox type
    renderer.

    Bounded by :data:`_MAX_SCHEMA_DEPTH` to avoid ``RecursionError`` on
    adversarially deep inputs. Once ``depth`` exceeds the cap we stop
    recursing and return the current depth — :func:`validate_input_schema`
    already rejects any depth > cap, so an exact measurement past the
    cap is unnecessary and unsafe (Python's default recursion limit
    would fire on ~1000 levels).
    """
    if depth > _MAX_SCHEMA_DEPTH:
        return depth
    if isinstance(node, dict):
        if not node:
            return depth
        return max(_schema_depth(v, depth + 1) for v in node.values())
    if isinstance(node, list):
        if not node:
            return depth
        return max(_schema_depth(item, depth + 1) for item in node)
    return depth


def validate_input_schema(schema: Any) -> dict[str, Any]:
    """Validate an upstream inputSchema and return it unchanged on pass.

    Rejects:

    * A non-dict root (MCP requires an object schema).
    * A root without ``"type": "object"`` — the meta-tool surface
      assumes every tool's argument payload is a JSON object.
    * Root-level ``"additionalProperties": true``. This keeps adversary
      upstreams from declaring an open schema that lets them smuggle
      arbitrary keys into the ``execute_tool`` forwarded call.
    * Nesting deeper than :data:`_MAX_SCHEMA_DEPTH`.
    * Any ``$ref`` key at any depth — the static-inline discipline is
      strictly easier to audit than a ref-resolution layer.

    Returns the *schema* unchanged when all checks pass. Raises
    :class:`SchemaValidationError` with a human-readable reason
    otherwise; callers in the registry log the reason and skip the
    offending tool.
    """
    if not isinstance(schema, dict):
        raise SchemaValidationError(f"inputSchema must be a JSON object; got {type(schema).__name__}")

    root_type = schema.get("type")
    if root_type != "object":
        raise SchemaValidationError(f"inputSchema root 'type' must be 'object'; got {root_type!r}")

    # Explicit ``additionalProperties: true`` at the root is the only
    # form we reject. Omitting the key (JSON Schema default is True)
    # is tolerated because a huge number of legitimate upstream tools
    # simply don't declare it — rejecting omission would break
    # compatibility with every schema authored before this PR. The
    # explicit form indicates intent and is the attack surface we
    # actually want to close.
    if schema.get("additionalProperties") is True:
        raise SchemaValidationError(
            "inputSchema must not declare 'additionalProperties: true' at "
            "the root — open schemas let an upstream smuggle unvalidated "
            "keys through execute_tool"
        )

    depth = _schema_depth(schema)
    if depth > _MAX_SCHEMA_DEPTH:
        raise SchemaValidationError(f"inputSchema nesting depth {depth} exceeds cap of {_MAX_SCHEMA_DEPTH}")

    if _contains_ref(schema):
        raise SchemaValidationError(
            "inputSchema must not contain '$ref' at any depth — inline schemas are required for auditability"
        )

    return schema


__all__ = [
    "SchemaValidationError",
    "sanitize_description",
    "validate_input_schema",
]

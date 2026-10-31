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
from typing import TYPE_CHECKING, Any

from fastmcp_gateway.injection_patterns import INJECTION_FLAGS, INJECTION_PATTERNS

if TYPE_CHECKING:
    from collections.abc import Iterator

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


class _PhysicalRecursionExceeded(SchemaValidationError):
    """Raised when traversal exhausts :data:`_MAX_PHYSICAL_RECURSION`.

    A :class:`SchemaValidationError` subclass, so the registry's
    per-tool ingest loop keeps skipping just the offending tool and the
    fail-closed contract is unchanged. It exists as a distinct type so
    the rejection *reason* stays truthful: a schema whose raw JSON nests
    deeper than the traversal will follow (through wrappers that cost no
    level of data, such as a chain of ``items``) is not the same defect
    as one that nests deeper than the cap in data, and the operator
    reading the audit log should not be handed a nesting depth that
    describes neither.
    """


# Depth at which schema nesting is considered pathological. Depth counts
# levels of data (see :func:`_schema_depth`): a flat object of scalar
# fields measures 2, and each level of fields nested inside another adds 1,
# so ``{"a": {"b": {"c": str}}}`` measures 4. A cap of 5 accommodates edge
# cases without giving adversaries room to build gigantic nested structures
# designed to exhaust the sandbox's type renderer.
#
# Deliberately not in ``__all__``: ``GatewayServer`` remains the only
# public API surface. These names exist so the internal call chain (and
# the tests) can reference the shipped bounds instead of hardcoding them
# in four places.
DEFAULT_MAX_SCHEMA_DEPTH = 5

# Upper ceiling any caller may configure *max_depth* to, and the furthest
# :func:`validate_input_schema` measures a schema's depth: a rejection
# names the depth it measured, up to this ceiling. Generous relative to
# the default -- no legitimate schema should ever need to approach it.
# Enforced at every site that accepts a caller-supplied depth via
# :func:`_validate_max_schema_depth`.
MAX_ALLOWED_SCHEMA_DEPTH = 50

# Keywords whose value holds the schemas of the SAME level of data as the
# schema that carries them: the elements of an array (``items``, and
# ``prefixItems`` for a tuple's positions) and alternatives or parts of the
# same value (``anyOf``, ``oneOf``, ``allOf``). Their wrappers add no depth.
# ``properties`` is handled on its own: its map is no level either, but
# each field it names is one level below the object that carries it.
_SAME_LEVEL_KEYWORDS = frozenset({"items", "prefixItems", "anyOf", "oneOf", "allOf"})

# Hard ceiling on the RAW JSON depth the traversal follows, independent of
# the depth cap. Wrappers cost no depth (above), so depth alone cannot bound
# the real recursion: a chain of ``items`` (or of ``anyOf`` members) around
# one leaf keeps the depth at 1 or 2 while each level is one more Python
# stack frame. Past Python's default recursion limit that would raise an
# uncaught ``RecursionError`` inside registry population -- an exception
# the per-tool ``except SchemaValidationError`` catch in
# ``ToolRegistry.populate_domain`` does NOT catch, so it would abort the
# whole domain's registration instead of skipping the one malformed tool.
# The traversal counts every dict and list it enters, wrapper or not, and
# rejects the schema past this many. It must stay well above
# MAX_ALLOWED_SCHEMA_DEPTH: a field level costs two raw levels (the
# ``properties`` map and the field's schema), so 100 still admits a schema
# of nested fields at the depth ceiling, while leaving ample headroom under
# Python's default recursion limit (~1000).
_MAX_PHYSICAL_RECURSION = 100


def _validate_max_schema_depth(value: object, *, param: str = "max_schema_depth") -> int:
    """Return *value* if it's a valid schema-depth cap, else raise ``ValueError``.

    The single strict check reused by every entry point that accepts a
    caller-supplied cap (:func:`validate_input_schema`,
    ``ToolRegistry.populate_domain``, ``UpstreamManager.__init__``,
    ``GatewayServer.__init__``, and the ``GATEWAY_MAX_SCHEMA_DEPTH`` env
    parser), so the accepted range can't drift between them.

    The type test is ``type(value) is int`` rather than
    ``isinstance(value, int)``: ``bool`` is an ``int`` subclass, so
    ``isinstance`` would silently accept ``max_schema_depth=True`` as a
    cap of 1 — quietly rejecting nearly every real schema. Floats are
    rejected for the same reason: ``1.5`` compares fine against the
    range but is not a depth.

    A bad value here is an operator misconfiguration, so it raises
    :class:`ValueError` — distinct from :class:`SchemaValidationError`,
    which the registry's per-tool ingest loop catches and treats as
    "skip this one tool".
    """
    if type(value) is not int or not (1 <= value <= MAX_ALLOWED_SCHEMA_DEPTH):
        raise ValueError(f"{param} must be an int between 1 and {MAX_ALLOWED_SCHEMA_DEPTH} (inclusive); got {value!r}")
    return value


def _contains_ref(node: Any, physical_depth: int = 0) -> bool:
    """Recursively check whether any dict in *node* contains a ``$ref`` key.

    ``$ref`` is rejected outright: resolving it requires fetching an
    external document (or walking back to the root schema), which
    introduces a TOCTOU window between validation and the time the
    sandbox type renderer reads the schema. Static inline schemas are
    strictly more auditable.

    Walks every dict and list, so a field map naming a property ``$ref``
    counts too. *physical_depth* is the raw JSON depth, counted exactly as
    :func:`_schema_depth` counts it, so a schema that passed the depth
    check is explored completely. Past :data:`_MAX_PHYSICAL_RECURSION` it
    fails closed: an unexplored subtree is treated as containing a
    ``$ref``, so a caller that skips the depth check (or runs it after this
    one) can never admit a hidden ``$ref`` or hit a ``RecursionError``.
    """
    if physical_depth > _MAX_PHYSICAL_RECURSION:
        return True
    if isinstance(node, dict):
        if "$ref" in node:
            return True
        return any(_contains_ref(value, physical_depth + 1) for value in node.values())
    if isinstance(node, list):
        return any(_contains_ref(item, physical_depth + 1) for item in node)
    return False


def _schema_children(node: dict[str, Any]) -> Iterator[tuple[Any, int, int]]:
    """Yield ``(value, depth, raw)`` for each value below the schema *node*.

    *depth* is the levels of data the value sits below *node*; *raw* is
    the JSON levels it sits below *node* (a field's schema is two raw
    levels down: the ``properties`` map, then the field).
    """
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            for field in value.values():
                yield field, 1, 2
        elif key in _SAME_LEVEL_KEYWORDS and isinstance(value, dict):
            yield value, 0, 1
        elif key in _SAME_LEVEL_KEYWORDS and isinstance(value, list):
            for member in value:
                yield member, 0, 2
        else:
            yield value, 1, 1


def _schema_depth(
    node: Any,
    depth: int = 0,
    *,
    max_depth: int = MAX_ALLOWED_SCHEMA_DEPTH,
    physical_depth: int = 0,
) -> int:
    """Return how many levels of data *node* nests.

    Used to reject pathologically nested schemas before they reach the
    sandbox type renderer.

    Levels of data, not JSON wrappers
    ---------------------------------
    A JSON Schema spends several JSON levels on each level of data: an
    object's field sits inside a ``properties`` map, an array's elements
    inside ``items`` (or ``prefixItems``), and a union's or composition's
    branches inside a list under ``anyOf``, ``oneOf`` or ``allOf``. Only
    the data levels describe how complex a tool's arguments are, so:

    * each field named in ``properties`` is one level below the object
      that carries it, and the map itself costs nothing;
    * the schemas under ``items``, ``prefixItems``, ``anyOf``, ``oneOf``
      and ``allOf`` sit at the level of the schema that carries them, so
      an array of objects, an optional field (``anyOf [X, null]``) and a
      tagged union cost what the object, the field or one branch costs;
    * any other keyword's value counts one level per dict, list or value,
      as before (so a field's own ``"type": "string"`` is the level below
      the field, and a flat object of scalar fields measures 2).

    For example, an object field holding an array of objects, each with an
    array of tagged-union objects whose ``params`` field is a two-position
    tuple, is four field levels deep and measures 5, although its raw JSON
    nests 17 levels.

    Bounded by *max_depth*: once ``depth`` exceeds it, the walk stops and
    returns the current depth, so the result is exact up to *max_depth*
    and ``max_depth + 1`` beyond it. :func:`validate_input_schema`
    measures up to :data:`MAX_ALLOWED_SCHEMA_DEPTH` whatever its cap, so a
    rejection names the depth it found.

    Physical recursion guard
    -------------------------
    Wrappers cost no depth, so *depth* alone cannot bound the real
    recursion: a chain of ``items`` or of union members around one leaf
    keeps *depth* at 1 or 2 while each JSON level is one more Python call.
    *physical_depth* is the raw JSON depth of *node* (every dict and list
    entered, wrapper or not), checked independently against
    :data:`_MAX_PHYSICAL_RECURSION`. Past it this raises
    :class:`_PhysicalRecursionExceeded` rather than returning a depth at
    all: a fabricated over-the-cap depth would reject the schema correctly
    but describe it wrongly, and the registry logs the exception text
    verbatim. Raising a distinct :class:`SchemaValidationError` subclass
    keeps the fail-closed contract (the per-tool ingest loop still skips
    just this tool) while naming the actual defect.
    """
    if depth > max_depth:
        return depth
    if physical_depth > _MAX_PHYSICAL_RECURSION:
        raise _PhysicalRecursionExceeded(
            f"inputSchema nests more than {_MAX_PHYSICAL_RECURSION} JSON levels deep, "
            "the most the validator will follow"
        )
    if isinstance(node, dict):
        return max(
            (
                _schema_depth(value, depth + levels, max_depth=max_depth, physical_depth=physical_depth + raw)
                for value, levels, raw in _schema_children(node)
            ),
            default=depth,
        )
    if isinstance(node, list):
        return max(
            (_schema_depth(item, depth + 1, max_depth=max_depth, physical_depth=physical_depth + 1) for item in node),
            default=depth,
        )
    return depth


def validate_input_schema(schema: Any, *, max_depth: int = DEFAULT_MAX_SCHEMA_DEPTH) -> dict[str, Any]:
    """Validate an upstream inputSchema and return it unchanged on pass.

    Rejects:

    * A non-dict root (MCP requires an object schema).
    * A root without ``"type": "object"`` — the meta-tool surface
      assumes every tool's argument payload is a JSON object.
    * Root-level ``"additionalProperties": true``. This keeps adversary
      upstreams from declaring an open schema that lets them smuggle
      arbitrary keys into the ``execute_tool`` forwarded call.
    * Nesting deeper than *max_depth* levels of data (see
      :func:`_schema_depth`), or more than
      :data:`_MAX_PHYSICAL_RECURSION` levels of raw JSON.
    * Any ``$ref`` key at any depth — the static-inline discipline is
      strictly easier to audit than a ref-resolution layer.

    Parameters
    ----------
    max_depth:
        Overrides :data:`DEFAULT_MAX_SCHEMA_DEPTH` for this call. The
        cap exists to bound schema complexity for LLM consumers, not
        to reject any particular shape -- raise it only deliberately,
        and prefer flattening an over-deep upstream schema instead.
        Must be an int between ``1`` and
        :data:`MAX_ALLOWED_SCHEMA_DEPTH` (inclusive), enforced by
        :func:`_validate_max_schema_depth`. The depth is measured up to
        that ceiling whatever the cap, so a rejection names the depth
        found; the raw-JSON ceiling bounds the recursion on its own.

    Returns the *schema* unchanged when all checks pass. Raises
    :class:`SchemaValidationError` with a human-readable reason
    otherwise; callers in the registry log the reason and skip the
    offending tool.
    """
    _validate_max_schema_depth(max_depth, param="max_depth")

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

    # Measured past the cap, up to the ceiling, so the rejection names the
    # schema's actual depth rather than the first level over the cap.
    depth = _schema_depth(schema, max_depth=MAX_ALLOWED_SCHEMA_DEPTH)
    if depth > MAX_ALLOWED_SCHEMA_DEPTH:
        raise SchemaValidationError(
            f"inputSchema nesting depth exceeds {MAX_ALLOWED_SCHEMA_DEPTH}, the most the validator measures "
            f"(cap of {max_depth})"
        )
    if depth > max_depth:
        raise SchemaValidationError(f"inputSchema nesting depth {depth} exceeds cap of {max_depth}")

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

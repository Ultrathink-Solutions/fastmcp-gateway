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
#
# Deliberately not in ``__all__``: ``GatewayServer`` remains the only
# public API surface. These names exist so the internal call chain (and
# the tests) can reference the shipped bounds instead of hardcoding them
# in four places.
DEFAULT_MAX_SCHEMA_DEPTH = 5

# Upper ceiling any caller may configure *max_depth* to. Generous relative
# to the "3-4 levels is typical" reasoning behind DEFAULT_MAX_SCHEMA_DEPTH
# -- no legitimate schema should ever need to approach it -- but bounded
# well below the point at which the ingest-time depth/$ref recursion
# itself becomes a stack-overflow risk on adversarial input (measured
# uncaught RecursionError from a few hundred levels of plain nesting in
# this recursion-limit environment). Enforced at every site that accepts
# a caller-supplied depth via :func:`_validate_max_schema_depth`.
MAX_ALLOWED_SCHEMA_DEPTH = 50

# JSON-Schema keys that encode a union of alternative schemas. Pydantic (and
# most JSON-Schema generators) emit ``anyOf`` for ``Optional[X]`` /
# ``X | None``; ``oneOf`` is JSON Schema's mutually-exclusive-union sibling
# and is treated identically here since the null-optionality shape is the
# same either way.
_UNION_KEYS = ("anyOf", "oneOf")

# Hard ceiling on REAL recursive call count, independent of the logical
# depth cap. The optional-union transparency below deliberately recurses
# into a wrapper's non-null member WITHOUT advancing the logical ``depth``
# counter -- so a schema built entirely from chained
# ``{"anyOf": [<inner>, {"type": "null"}]}`` wrappers around a single leaf
# can climb arbitrarily many real Python stack frames while ``depth`` never
# exceeds the cap, defeating the ``depth > max_depth`` bail-out below and
# eventually raising an uncaught ``RecursionError`` deep inside registry
# population -- an exception the per-tool ``except SchemaValidationError``
# catch in ``ToolRegistry.populate_domain`` does NOT catch, so it would
# abort the whole domain's registration instead of just skipping the one
# malformed tool. This counter increments on EVERY recursive call,
# transparent or not, and is checked independently of ``depth``. It must
# stay comfortably above MAX_ALLOWED_SCHEMA_DEPTH, or it would pre-empt
# the logical cap for a legitimately deep schema at a raised
# max_schema_depth; 100 clears the ceiling of 50 by 2x while leaving ample
# headroom under Python's default recursion limit (~1000).
_MAX_PHYSICAL_RECURSION = 100


def _is_null_schema(node: Any) -> bool:
    """Strict test for the JSON-Schema null-type sentinel ``{"type": "null"}``.

    Matches ONLY a dict whose single key is ``"type"`` with value
    ``"null"`` -- the exact shape Pydantic (and most JSON-Schema
    generators) emit for the null branch of ``Optional[X]``. A dict
    that additionally carries annotation keys (``title``,
    ``description``, a sibling ``default``, ...) alongside
    ``"type": "null"`` does NOT match. This is the strictest reading of
    "the null member": a schema author who attached an explicit
    annotation to the null branch presumably wants it treated as
    meaningful structure, not silently made transparent.
    """
    return isinstance(node, dict) and node == {"type": "null"}


def _optional_wrapper_target(members: Any) -> Any | None:
    """Return the sole non-null member of a 2-member null union, else ``None``.

    *members* is the raw value of an ``anyOf``/``oneOf`` key. Returns
    the other member when *members* is a list of exactly two schemas,
    exactly one of which is :func:`_is_null_schema`. Any other shape —
    not a list, not exactly 2 items, zero null members, or two null
    members — returns ``None``, and the caller falls back to counting
    the union normally. This deliberately narrow match means a
    3+-branch union (``Union[A, B, None]``) or a union with no null
    member is never treated as an optionality wrapper.
    """
    if not isinstance(members, list) or len(members) != 2:
        return None
    null_flags = [_is_null_schema(m) for m in members]
    if sum(null_flags) != 1:
        return None
    return members[1] if null_flags[0] else members[0]


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


def _contains_ref(
    node: Any,
    depth: int = 0,
    *,
    max_depth: int = DEFAULT_MAX_SCHEMA_DEPTH,
    physical_depth: int = 0,
) -> bool:
    """Recursively check whether any dict in *node* contains a ``$ref`` key.

    ``$ref`` is rejected outright: resolving it requires fetching an
    external document (or walking back to the root schema), which
    introduces a TOCTOU window between validation and the time the
    sandbox type renderer reads the schema. Static inline schemas are
    strictly more auditable.

    Recurses through an ``Optional[X]`` union (see :func:`_schema_depth`)
    at the SAME logical *depth* as its wrapper dict, mirroring the
    depth-counting transparency below. *physical_depth*, in contrast,
    increments on EVERY recursive call including transparent ones -- see
    :data:`_MAX_PHYSICAL_RECURSION` for why a second, always-advancing
    counter is required and cannot be replaced by *depth* alone. The null
    member itself is never checked for ``$ref`` because
    :func:`_is_null_schema` only matches the fixed, ref-free shape
    ``{"type": "null"}``.
    """
    if depth > max_depth:
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
        # bug regardless of caller ordering. *max_depth* must match the
        # depth check's cap for this fail-closed guarantee to hold —
        # :func:`validate_input_schema` always passes the same value to
        # both.
        return True
    if physical_depth > _MAX_PHYSICAL_RECURSION:
        # Same fail-closed contract as the logical-depth bail-out above,
        # but keyed on real recursive-call count instead of logical
        # depth -- reachable ONLY via a chain of transparent
        # optional-wrapper hops, since every non-transparent path already
        # advances ``depth`` in lockstep with ``physical_depth`` and
        # would hit the check above first.
        return True
    if isinstance(node, dict):
        if "$ref" in node:
            return True
        for key, value in node.items():
            if key in _UNION_KEYS:
                target = _optional_wrapper_target(value)
                if target is not None:
                    if _contains_ref(target, depth, max_depth=max_depth, physical_depth=physical_depth + 1):
                        return True
                    continue
            if _contains_ref(value, depth + 1, max_depth=max_depth, physical_depth=physical_depth + 1):
                return True
        return False
    if isinstance(node, list):
        return any(
            _contains_ref(item, depth + 1, max_depth=max_depth, physical_depth=physical_depth + 1) for item in node
        )
    return False


def _schema_depth(
    node: Any,
    depth: int = 0,
    *,
    max_depth: int = DEFAULT_MAX_SCHEMA_DEPTH,
    physical_depth: int = 0,
) -> int:
    """Return the maximum nesting depth of dicts/lists in *node*.

    Each dict or list increments the depth counter. Used to reject
    pathologically nested schemas before they reach the sandbox type
    renderer.

    Bounded by *max_depth* to avoid ``RecursionError`` on adversarially
    deep inputs. Once ``depth`` exceeds *max_depth* we stop recursing
    and return the current depth — :func:`validate_input_schema`
    already rejects any depth > cap, so an exact measurement past the
    cap is unnecessary and unsafe (Python's default recursion limit
    would fire on ~1000 levels). *max_depth* must be the same value the
    caller will compare the result against, or the early bail-out
    stops short of (or overshoots) the caller's actual cap.

    Optional-union transparency
    ----------------------------
    Pydantic's ``Optional[X]`` / ``X | None`` encoding —
    ``{"anyOf": [X, {"type": "null"}]}`` — costs 2 extra levels versus
    a required ``X`` at the same position (the wrapper dict's ``anyOf``
    key, plus the union array, plus recursing into the list member),
    despite identical real complexity for an LLM consumer: an optional
    list field would otherwise start 2 levels closer to the cap than a
    required one for no structural reason. When a dict's
    ``anyOf``/``oneOf`` value matches :func:`_optional_wrapper_target`
    (exactly one non-null member alongside exactly one
    ``{"type": "null"}`` member), that key's contribution to this
    dict's depth is computed at the SAME *depth* as the dict itself — as
    if the wrapper were replaced by the non-null member directly —
    instead of the usual ``depth + 1``. Every other key in the same
    dict (e.g. a sibling ``title``) is still counted normally, and this
    composes: an optional field nested inside another optional field's
    non-null branch keeps discounting each wrapper it crosses. A
    3+-branch union or a union with no null member never matches
    :func:`_optional_wrapper_target`, so it keeps the pre-existing,
    non-transparent counting.

    Physical recursion guard
    -------------------------
    Because the transparency above recurses WITHOUT advancing *depth*,
    *depth* alone can no longer bound the real recursive-call count: a
    schema built entirely from chained optional-wrapper hops around one
    leaf keeps *depth* at (or near) 0 forever while still making one real
    Python call per hop. *physical_depth* is a second counter that
    increments on every recursive call — transparent or not — and is
    checked independently via :data:`_MAX_PHYSICAL_RECURSION`. When it
    fires, this returns ``max_depth + 1`` — a value guaranteed to exceed
    the caller's cap — rather than the (possibly still-small, and
    therefore misleading) *depth* value, so :func:`validate_input_schema`
    reliably rejects the schema instead of silently admitting it. Without
    this, a sufficiently long chain would either be wrongly admitted (low
    reported depth) or, past Python's default recursion limit, raise an
    uncaught ``RecursionError`` that escapes the registry's per-tool
    ``SchemaValidationError`` catch and aborts the whole upstream's
    registration.
    """
    if depth > max_depth:
        return depth
    if physical_depth > _MAX_PHYSICAL_RECURSION:
        return max_depth + 1
    if isinstance(node, dict):
        if not node:
            return depth
        contributions: list[int] = []
        for key, value in node.items():
            if key in _UNION_KEYS:
                target = _optional_wrapper_target(value)
                if target is not None:
                    contributions.append(
                        _schema_depth(target, depth, max_depth=max_depth, physical_depth=physical_depth + 1)
                    )
                    continue
            contributions.append(
                _schema_depth(value, depth + 1, max_depth=max_depth, physical_depth=physical_depth + 1)
            )
        return max(contributions)
    if isinstance(node, list):
        if not node:
            return depth
        return max(
            _schema_depth(item, depth + 1, max_depth=max_depth, physical_depth=physical_depth + 1) for item in node
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
    * Nesting deeper than *max_depth*.
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
        :func:`_validate_max_schema_depth`. The upper bound isn't
        arbitrary: the depth/``$ref`` recursion itself becomes a
        stack-overflow risk on adversarial input well before
        four-figure depths, so *max_depth* can't be raised without
        limit even for a deployment that genuinely wants deeper
        schemas.

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

    depth = _schema_depth(schema, max_depth=max_depth)
    if depth > max_depth:
        raise SchemaValidationError(f"inputSchema nesting depth {depth} exceeds cap of {max_depth}")

    if _contains_ref(schema, max_depth=max_depth):
        raise SchemaValidationError(
            "inputSchema must not contain '$ref' at any depth — inline schemas are required for auditability"
        )

    return schema


__all__ = [
    "SchemaValidationError",
    "sanitize_description",
    "validate_input_schema",
]

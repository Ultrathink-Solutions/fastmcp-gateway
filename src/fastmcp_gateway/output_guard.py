"""Gateway-level output guard for prompt-injection markup in tool results.

Hook-based integration runs before the result reaches the LLM context.
Scanning is opt-in via :class:`OutputGuardConfig` — when ``enabled`` is
``True`` (the default when the config is passed to
:class:`~fastmcp_gateway.gateway.GatewayServer`) an
:class:`OutputGuardHook` is **prepended** to the hook chain so
downstream operator-supplied ``after_execute`` hooks see already-
scrubbed output.

Per-tool bypass is provided for tools that legitimately return
prompt-like content (e.g., a prompt-rewriting service). A tool can be
trusted via either source:

* Upstream-declared ``annotations: {"x-raw-output-trusted": true}``
  custom extension (recorded on :class:`~fastmcp_gateway.registry.ToolEntry`
  at registry-populate time).
* Operator override ``GatewayServer(trusted_output_tools=[...])``
  (``fnmatch`` glob patterns applied at populate time).

The pattern source is imported from :mod:`fastmcp_gateway.injection_patterns`
— the same list the registry-ingest description sanitizer uses. Single
source of truth guarantees parity: any security-team addition to the
pattern list propagates to both inbound (description) and outbound
(tool-output) scanning with one PR.

Code-mode exemption
-------------------

The experimental ``execute_code`` path routes every nested tool call
through :class:`~fastmcp_gateway.code_mode.CodeModeRunner` which hands
the raw tool result (either structured content or concatenated text)
back to the sandbox. Operator-written sandbox code then produces a
compact final expression that becomes the ``execute_code`` return
value — the LLM never sees the raw upstream content directly. This
layer of indirection is the intended design; the output guard's
scrubbing of the hook-chain result does not reach the sandbox, and
that is deliberate.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from fastmcp_gateway.errors import OutputGuardError
from fastmcp_gateway.hooks import ExecutionDenied
from fastmcp_gateway.injection_patterns import INJECTION_FLAGS, INJECTION_PATTERNS

if TYPE_CHECKING:
    from fastmcp_gateway.hooks import ExecutionContext
    from fastmcp_gateway.registry import ToolRegistry

logger = logging.getLogger(__name__)


__all__ = [
    "OutputGuardConfig",
    "OutputGuardHook",
    "sanitize_output",
]


# Compile the injection pattern denylist once at import time. The flags
# (INJECTION_FLAGS) carry IGNORECASE so case-variant attempts like
# ``<SYSTEM>`` also match; DOTALL lets attacker-inserted newlines
# inside ``<system ...>`` tags still match.
_COMPILED_PATTERN: re.Pattern[str] = re.compile("|".join(INJECTION_PATTERNS), INJECTION_FLAGS)

# Marker substituted for matches when mode="marker". Chosen to be
# unambiguous in the LLM's view: the string ``[OUTPUT SCRUBBED BY
# GATEWAY]`` is both machine-detectable (for audit-log correlation) and
# human-readable (operators debugging a misbehaving tool can see at a
# glance why the LLM is responding oddly).
_MARKER = "[OUTPUT SCRUBBED BY GATEWAY]"


class OutputGuardConfig(BaseModel):
    """Configuration for the gateway-level output guard.

    Attributes
    ----------
    enabled:
        When ``True`` (default), an :class:`OutputGuardHook` is
        prepended to the gateway's hook chain at construction time.
        When ``False``, the config object is otherwise inert — useful
        for feature-flagging via static config without restructuring
        the gateway wiring.
    mode:
        Sanitation strategy. See :func:`sanitize_output` for the
        per-mode semantics.
    max_scan_bytes:
        Upper bound (in UTF-8 bytes) on the slice of result text
        actually scanned by the pattern regex. Tool results larger
        than this are scanned up to the cap and the remainder is
        passed through unchanged. Default 512 KiB; tool payloads in
        typical MCP deployments are well under this.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    mode: Literal["strip", "reject", "marker"] = "strip"
    # ``gt=0`` closes a silent-disable bypass: ``max_scan_bytes=0``
    # would otherwise pass ``int`` type validation, make
    # ``encoded[:0]`` an empty bytes slice, and turn the guard into a
    # no-op with no error surfaced. Reject at construction.
    max_scan_bytes: int = Field(default=512_000, gt=0)


def sanitize_output(
    text: str,
    *,
    tool_name: str,
    mode: Literal["strip", "reject", "marker"] = "strip",
    max_scan_bytes: int = 512_000,
) -> tuple[str, bool]:
    """Sanitize *text* against the shared injection-pattern denylist.

    Parameters
    ----------
    text:
        Raw result text from a tool invocation.
    tool_name:
        The registered tool name — included in audit log records so
        operators can trace a scrub event back to its source.
    mode:
        One of:

        * ``"strip"`` (default): replace each match with a single
          space; surrounding benign content is preserved so the LLM
          still sees useful data.
        * ``"reject"``: raise :class:`~fastmcp_gateway.errors.OutputGuardError`
          on any match. The calling hook surfaces this as an
          :class:`~fastmcp_gateway.hooks.ExecutionDenied` so the meta-
          tool layer returns a structured error envelope.
        * ``"marker"``: replace each match with
          ``"[OUTPUT SCRUBBED BY GATEWAY]"``. Makes the intervention
          visible to the LLM for debugging.
    max_scan_bytes:
        Only the first *max_scan_bytes* bytes of *text* (UTF-8) are
        scanned by the regex engine. The suffix beyond that is
        forwarded verbatim. A pathologically large payload therefore
        costs O(cap) regex work, not O(n). An audit log is emitted
        when the cap truncates the scan region so operators can tell
        a large tool result may have carried unscanned markup past
        the scanner.

    Returns
    -------
    ``(processed_text, was_modified)`` where *was_modified* is
    ``True`` iff the sanitizer actually mutated the input.

    Raises
    ------
    OutputGuardError
        Only in ``mode="reject"`` when any injection pattern matches.
    """
    # Same silent-disable concern as on ``OutputGuardConfig`` — a
    # direct caller of ``sanitize_output`` must not be able to pass
    # ``max_scan_bytes=0`` and neutralize the guard. ``bool`` is a
    # subclass of ``int`` in Python, so the explicit bool reject
    # prevents ``sanitize_output(..., max_scan_bytes=True)`` from
    # coercing to 1 (a single-byte scan window) without notice.
    if isinstance(max_scan_bytes, bool) or not isinstance(max_scan_bytes, int) or max_scan_bytes <= 0:
        raise ValueError(f"max_scan_bytes must be a positive int; got {max_scan_bytes!r}")

    if not isinstance(text, str):
        # Defensive: upstream deserializers should always hand us str,
        # but a misbehaving integration could pass bytes/None. A
        # non-str input has no injection surface (the LLM won't see
        # it directly), so we emit an audit record and return empty.
        logger.warning(
            "output_guard received non-str input; tool=%s type=%s — substituting empty string",
            tool_name,
            type(text).__name__,
        )
        return "", False

    # Trim the scan region by bytes (not characters) so a caller
    # who passed a tight byte budget gets exactly that. To avoid
    # losing a multibyte UTF-8 character that straddles the cap, we
    # back off from the cap to the last complete code-point
    # boundary using the UTF-8 continuation-byte marker: any byte
    # matching ``10xxxxxx`` (``0xC0 & b == 0x80``) is a continuation
    # byte, which means we're mid-character and must walk back one
    # step. Worst case the walk-back takes 3 bytes (UTF-8 characters
    # are at most 4 bytes), so this is O(1). A naive split +
    # ``decode(errors="ignore")`` on both halves would drop the
    # straddling character from *both* the scanned head and the
    # forwarded tail, silently corrupting benign output at the seam.
    encoded = text.encode("utf-8")
    if len(encoded) > max_scan_bytes:
        split = max_scan_bytes
        while split > 0 and (encoded[split] & 0xC0) == 0x80:
            split -= 1
        head = encoded[:split].decode("utf-8")
        tail = encoded[split:].decode("utf-8")
        logger.info(
            "output_guard scan truncated by byte cap: tool=%s scanned_bytes=%d total_bytes=%d",
            tool_name,
            split,
            len(encoded),
        )
    else:
        head = text
        tail = ""

    if mode == "reject":
        match = _COMPILED_PATTERN.search(head)
        if match is not None:
            # Log only safe metadata — ``match.group(0)`` is
            # attacker-controlled upstream content and may carry
            # credential strings, oversized payloads, or other text
            # an operator would not want propagated into their audit
            # log aggregator. Mirrors the description sanitizer's
            # hygiene discipline (see ``sanitize.py``).
            logger.warning(
                "output_guard reject: tool=%s pattern=%r offset=%d match_length=%d",
                tool_name,
                match.re.pattern,
                match.start(),
                match.end() - match.start(),
            )
            raise OutputGuardError(
                f"Tool '{tool_name}' output contains prompt-injection markup",
            )
        return text, False

    replacement = _MARKER if mode == "marker" else " "
    modified = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal modified
        modified = True
        # Safe metadata only — see matching rationale in the reject
        # branch above. Intentionally omits ``match.group(0)``.
        logger.warning(
            "output_guard strip: tool=%s pattern=%r offset=%d match_length=%d",
            tool_name,
            match.re.pattern,
            match.start(),
            match.end() - match.start(),
        )
        return replacement

    scrubbed_head = _COMPILED_PATTERN.sub(_replace, head)
    if not modified:
        # Fast path: no mutation — return the exact original string so
        # callers can use identity comparison to skip re-encoding.
        return text, False

    return scrubbed_head + tail, True


class OutputGuardHook:
    """Hook that scrubs prompt-injection markup from tool result text.

    Implements :class:`~fastmcp_gateway.hooks.Hook`'s ``after_execute``
    method only. Constructed and prepended by
    :class:`~fastmcp_gateway.gateway.GatewayServer` when an
    :class:`OutputGuardConfig` with ``enabled=True`` is passed.

    The hook is aware of :attr:`ToolEntry.raw_output_trusted
    <fastmcp_gateway.registry.ToolEntry.raw_output_trusted>` — trusted
    tools (e.g., legitimate prompt-processing services) bypass the
    scrub entirely. Error results also pass through untouched because
    they carry an opaque error envelope the LLM treats as a terminal
    condition; scrubbing that envelope would obscure the failure
    without any safety benefit.

    Parameters
    ----------
    registry:
        The gateway's :class:`~fastmcp_gateway.registry.ToolRegistry`.
        Consulted on each invocation to check the executed tool's
        :attr:`ToolEntry.raw_output_trusted` flag, which is the
        authoritative source of per-tool bypass state (keeps the hook
        stateless — if the registry is repopulated mid-flight the
        hook picks up the new flags on the next call).
    mode:
        Forwarded to :func:`sanitize_output`. See that function's
        docstring for the per-mode semantics.
    max_scan_bytes:
        Forwarded to :func:`sanitize_output`.
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        mode: Literal["strip", "reject", "marker"] = "strip",
        max_scan_bytes: int = 512_000,
    ) -> None:
        self._registry = registry
        # Explicit annotation so the narrow ``Literal`` type survives
        # pyright's attribute-widening — without it the attribute is
        # inferred as plain ``str`` and the re-pass into
        # ``sanitize_output(mode=...)`` fails its Literal check.
        self._mode: Literal["strip", "reject", "marker"] = mode
        self._max_scan_bytes = max_scan_bytes

    async def after_execute(
        self,
        context: ExecutionContext,
        result: str,
        is_error: bool,
    ) -> str:
        """Scrub ``result`` and return the processed string.

        Short-circuits (returns ``result`` unchanged) when:

        * ``is_error`` is ``True`` — the result is the structured error
          envelope built by :mod:`fastmcp_gateway.meta_tools`; scrubbing
          it would complicate operator debugging without closing any
          real attack vector (the LLM treats the envelope as a
          terminal failure, not as instructions).
        * The executed tool is flagged
          :attr:`ToolEntry.raw_output_trusted`.
        * The result is not a JSON object with a string ``result``
          field — this shape is what :func:`execute_tool` always
          constructs, so if we see something else the payload is
          foreign and we pass it through untouched rather than risk
          corrupting it.
        """
        if is_error:
            return result

        tool = self._registry.lookup(context.tool.name)
        if tool is not None and tool.raw_output_trusted:
            logger.debug(
                "output_guard bypass: tool=%s is marked raw_output_trusted",
                tool.name,
            )
            return result

        # The meta-tool layer always wraps a successful tool result in
        # ``{"tool": name, "result": text}``. A non-JSON or
        # non-matching shape is not something this guard was designed
        # to mutate — pass it through and log so operators can notice
        # an unexpected contract change.
        try:
            envelope = json.loads(result)
        except (TypeError, ValueError):
            logger.debug(
                "output_guard skip: tool=%s result is not JSON — passing through",
                context.tool.name,
            )
            return result

        if not isinstance(envelope, dict) or not isinstance(envelope.get("result"), str):
            logger.debug(
                "output_guard skip: tool=%s result envelope has unexpected shape — passing through",
                context.tool.name,
            )
            return result

        inner = envelope["result"]
        try:
            processed, modified = sanitize_output(
                inner,
                tool_name=context.tool.name,
                mode=self._mode,
                max_scan_bytes=self._max_scan_bytes,
            )
        except OutputGuardError as exc:
            # Lift to ExecutionDenied so the meta-tool layer returns
            # a structured error envelope (same routing path as any
            # other policy-denial). The ``ValueError`` subclass remains
            # catchable by broader except blocks for callers that
            # haven't migrated to the new exception type.
            raise ExecutionDenied(str(exc), code="output_guard_reject") from exc

        if not modified:
            return result

        envelope["result"] = processed
        envelope["_output_guard"] = {"mode": self._mode, "modified": True}
        return json.dumps(envelope)

    # Expose for introspection / tests. The ``mode`` annotation keeps
    # the narrow ``Literal`` type so callers that assign from it into
    # another ``mode`` slot don't need a ``cast``.
    @property
    def mode(self) -> Literal["strip", "reject", "marker"]:
        return self._mode

    @property
    def max_scan_bytes(self) -> int:
        return self._max_scan_bytes

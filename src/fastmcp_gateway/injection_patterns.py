"""Prompt-injection pattern source shared across registry-ingest and
output-guard paths.

Both the tool-description sanitizer (sanitize.py) and the output-side
guard (separate PR) import INJECTION_PATTERNS + INJECTION_FLAGS from
here. Security-team additions to the list propagate to both consumers
automatically.

Not a complete defense — adversaries evolve patterns faster than any
static denylist. This is a protection floor, not a ceiling. Future work:
Unicode homoglyph detection, base64-encoded payloads, multi-hop
indirect injection. PATTERN_VERSION is bumped on every list update so
operators can detect stale deployments.
"""

from __future__ import annotations

import re

PATTERN_VERSION = "2026-04-22.1"

INJECTION_PATTERNS: list[str] = [
    r"<\s*system\s*[^>]*>",
    r"</\s*system\s*>",
    r"<\s*assistant\s*[^>]*>",
    r"</\s*assistant\s*>",
    r"<\s*user\s*[^>]*>",
    r"</\s*user\s*>",
    r"\[INST\]",
    r"\[/INST\]",
    r"<\|im_start\|>",
    r"<\|im_end\|>",
    r"<<SYS>>",
    r"<</SYS>>",
    r"<\s*tool_use\s*[^>]*>",
    r"<\s*tool_result\s*[^>]*>",
    r"ignore\s+(?:all\s+)?previous\s+instructions?",
    r"disregard\s+(?:all\s+)?(?:prior|previous)\s+(?:context|instructions?)",
    r"you\s+are\s+now",
    r"act\s+as\s+(?:a|an)\s+",
]

INJECTION_FLAGS: int = re.IGNORECASE | re.UNICODE | re.DOTALL

__all__ = ["INJECTION_FLAGS", "INJECTION_PATTERNS", "PATTERN_VERSION"]

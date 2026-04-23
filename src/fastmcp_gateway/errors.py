"""Structured error responses for the gateway."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel


class OutputGuardError(ValueError):
    """Raised by the output guard in ``reject`` mode when prompt-injection
    markup is detected in a tool's result text.

    Subclasses :class:`ValueError` so existing callers catching the broad
    type keep working, while a dedicated type lets the hook pipeline
    surface the event as a structured ``ExecutionDenied`` without
    brittle string matching on error messages.
    """


class GatewayError(BaseModel):
    """Machine-parseable error returned by gateway meta-tools.

    Attributes
    ----------
    error:
        Human-readable error message.
    code:
        Machine-readable error code (e.g. ``"tool_not_found"``).
    details:
        Optional structured context (suggestions, domain names, etc.).
    """

    error: str
    code: str
    details: dict[str, Any] | None = None


def error_response(code: str, message: str, **details: Any) -> str:
    """Build a JSON-serialised ``GatewayError``.

    Parameters
    ----------
    code:
        Machine-readable code such as ``"tool_not_found"``.
    message:
        Human-readable description of the error.
    **details:
        Arbitrary key-value pairs included in the ``details`` dict.
    """
    return json.dumps(
        GatewayError(
            error=message,
            code=code,
            details=details or None,
        ).model_dump()
    )

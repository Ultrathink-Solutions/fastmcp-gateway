"""Non-secret registration incarnations and versioned routing contracts."""

import hashlib
import json
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from fastmcp_gateway.registry import ToolEntry


class RegistrationIdentity(BaseModel):
    """Trusted restore identity and external configuration version."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    registration_id: UUID
    configuration_revision: str | None = None

    @field_validator("configuration_revision", mode="before")
    @classmethod
    def validate_configuration_revision(cls, value: object) -> str | None:
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 128
            or value != value.strip()
            or any(unicodedata.category(char).startswith("C") for char in value)
        ):
            raise ValueError("configuration_revision must be a nonblank version of 1-128 characters")
        return value


class RegistrationSnapshot(BaseModel):
    """Detached effective routing contract; contains no clients or credentials."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    server_name: str
    identity: RegistrationIdentity
    registration_revision: str
    execution_url: str | None
    discovery_url: str | None
    tools: tuple[ToolEntry, ...]


class RegistrationChange(BaseModel):
    """Validated transition supplied to the durable commit hook."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["replace", "refresh", "remove"]
    previous: RegistrationSnapshot | None
    current: RegistrationSnapshot | None


RegistrationCommitHook = Callable[[RegistrationChange], Awaitable[None]]


def registration_snapshot(
    server_name: str,
    identity: RegistrationIdentity,
    execution_url: str | None,
    discovery_url: str | None,
    tools: tuple[ToolEntry, ...],
) -> RegistrationSnapshot:
    """Hash only the effective, admitted contract, excluding collision aliases."""
    projections = [
        {
            "name": tool.original_name or tool.name,
            "group": tool.group,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "required_scope": tool.required_scope,
            "raw_output_trusted": tool.raw_output_trusted,
        }
        for tool in tools
    ]
    projections.sort(key=lambda tool: tool["name"])
    payload = json.dumps(
        {
            "identity": identity.model_dump(mode="json"),
            "server_name": server_name,
            "execution_url": execution_url,
            "discovery_url": discovery_url,
            "tools": projections,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return RegistrationSnapshot(
        server_name=server_name,
        identity=identity,
        registration_revision="reg-v1:" + hashlib.sha256(payload.encode()).hexdigest(),
        execution_url=execution_url,
        discovery_url=discovery_url,
        tools=tuple(tool.model_copy(deep=True) for tool in tools),
    )

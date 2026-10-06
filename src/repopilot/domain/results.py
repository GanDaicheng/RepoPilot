"""Stable result envelopes shared by RepoPilot tools."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Generic, Mapping, TypeVar


T = TypeVar("T")


def _frozen_metadata(metadata: Mapping[str, object] | None) -> Mapping[str, object]:
    return MappingProxyType(dict(metadata or {}))


@dataclass(frozen=True, slots=True)
class ToolResult(Generic[T]):
    """An immutable success or failure returned by a controlled tool."""

    ok: bool
    data: T | None
    error_code: str | None
    message: str
    duration_ms: int
    metadata: Mapping[str, object]

    @classmethod
    def success(
        cls,
        data: T,
        *,
        message: str = "",
        duration_ms: int = 0,
        metadata: Mapping[str, object] | None = None,
    ) -> ToolResult[T]:
        return cls(
            ok=True,
            data=data,
            error_code=None,
            message=message,
            duration_ms=duration_ms,
            metadata=_frozen_metadata(metadata),
        )

    @classmethod
    def failure(
        cls,
        error_code: str,
        message: str,
        *,
        duration_ms: int = 0,
        metadata: Mapping[str, object] | None = None,
    ) -> ToolResult[T]:
        return cls(
            ok=False,
            data=None,
            error_code=error_code,
            message=message,
            duration_ms=duration_ms,
            metadata=_frozen_metadata(metadata),
        )

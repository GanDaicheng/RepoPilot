"""Strict schemas for every structured repository-agent stage."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


MAX_PATCH_BYTES = 1_000_000
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _relative_posix_path(value: str) -> str:
    if not value or "\\" in value or ":" in value:
        raise ValueError("path must be a non-empty relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path must be a relative POSIX path without traversal")
    return value


RelativePath = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
    AfterValidator(_relative_posix_path),
]


class _StrictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ChangePlan(_StrictOutput):
    goal: NonEmptyText
    relevant_files: tuple[RelativePath, ...]
    steps: tuple[NonEmptyText, ...] = Field(min_length=1)
    risks: tuple[NonEmptyText, ...]
    suggested_tests: tuple[NonEmptyText, ...]


class PatchProposal(_StrictOutput):
    patch_text: str
    summary: NonEmptyText
    expected_files: tuple[RelativePath, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_patch(self) -> PatchProposal:
        patch = self.patch_text
        if not patch or not any(line.startswith("--- ") for line in patch.splitlines()):
            raise ValueError("patch_text must contain a unified diff source header")
        if not any(line.startswith("+++ ") for line in patch.splitlines()):
            raise ValueError("patch_text must contain a unified diff target header")
        if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
            raise ValueError("patch_text exceeds the size limit")
        return self


class FailureAnalysis(_StrictOutput):
    root_cause: NonEmptyText
    evidence: tuple[NonEmptyText, ...]
    fixable: bool
    strategy: NonEmptyText | None
    suggested_files: tuple[RelativePath, ...]

    @model_validator(mode="after")
    def validate_strategy(self) -> FailureAnalysis:
        if self.fixable and self.strategy is None:
            raise ValueError("strategy is required when fixable is true")
        return self


class ReviewFinding(_StrictOutput):
    severity: Literal["critical", "warning", "suggestion"]
    message: NonEmptyText
    path: RelativePath | None
    line: int | None = Field(default=None, ge=1)


class ReviewDecision(_StrictOutput):
    approved: bool
    critical_findings: tuple[ReviewFinding, ...]
    warnings: tuple[ReviewFinding, ...]
    conclusion: NonEmptyText

    @model_validator(mode="after")
    def validate_finding_buckets(self) -> ReviewDecision:
        if any(item.severity != "critical" for item in self.critical_findings):
            raise ValueError("critical_findings may contain only critical findings")
        if any(item.severity == "critical" for item in self.warnings):
            raise ValueError("warnings may not contain critical findings")
        if self.approved and self.critical_findings:
            raise ValueError("approved cannot be true when critical findings exist")
        return self

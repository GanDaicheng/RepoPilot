from __future__ import annotations

import pytest
from pydantic import ValidationError

from repopilot.agent.schemas import (
    MAX_PATCH_BYTES,
    ChangePlan,
    FailureAnalysis,
    PatchProposal,
    ReviewDecision,
    ReviewFinding,
)


def test_change_plan_is_strict_and_converts_lists_to_immutable_tuples() -> None:
    plan = ChangePlan.model_validate(
        {
            "goal": "Reject division by zero",
            "relevant_files": ["calculator.py"],
            "steps": ["Add a guard", "Run tests"],
            "risks": ["Existing callers may rely on ZeroDivisionError"],
            "suggested_tests": ["pytest -q"],
        }
    )

    assert plan.relevant_files == ("calculator.py",)
    assert plan.steps == ("Add a guard", "Run tests")
    with pytest.raises(ValidationError, match="extra"):
        ChangePlan.model_validate(
            {
                **plan.model_dump(mode="json"),
                "unexpected": "not allowed",
            }
        )


@pytest.mark.parametrize(
    "path",
    ["../outside.py", "/absolute.py", "C:/absolute.py", "folder\\escape.py", ""],
)
def test_structured_outputs_reject_non_relative_posix_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        ChangePlan(
            goal="Change code",
            relevant_files=(path,),
            steps=("Edit",),
            risks=(),
            suggested_tests=("pytest -q",),
        )


@pytest.mark.parametrize("patch", ["", "not a patch", "--- a.py\nmissing target\n"])
def test_patch_proposal_requires_unified_diff(patch: str) -> None:
    with pytest.raises(ValidationError):
        PatchProposal(
            patch_text=patch,
            summary="Change a file",
            expected_files=("a.py",),
        )


def test_patch_proposal_enforces_byte_limit() -> None:
    patch = "--- a/a.py\n+++ b/a.py\n" + ("+x" * MAX_PATCH_BYTES)

    with pytest.raises(ValidationError, match="size limit"):
        PatchProposal(
            patch_text=patch,
            summary="Too large",
            expected_files=("a.py",),
        )


def test_patch_proposal_preserves_diff_terminal_newline() -> None:
    patch = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"

    proposal = PatchProposal(
        patch_text=patch,
        summary="Change",
        expected_files=("a.py",),
    )

    assert proposal.patch_text == patch


def test_failure_analysis_requires_strategy_only_when_fixable() -> None:
    fixable = FailureAnalysis(
        root_cause="Missing guard",
        evidence=("test_zero failed",),
        fixable=True,
        strategy="Add validation",
        suggested_files=("calculator.py",),
    )
    assert fixable.strategy == "Add validation"

    with pytest.raises(ValidationError, match="strategy"):
        FailureAnalysis(
            root_cause="Missing guard",
            evidence=("test_zero failed",),
            fixable=True,
            strategy=None,
            suggested_files=("calculator.py",),
        )


def test_review_decision_constrains_finding_severity_by_bucket() -> None:
    decision = ReviewDecision(
        approved=False,
        critical_findings=(
            ReviewFinding(
                severity="critical",
                message="The patch bypasses validation",
                path="calculator.py",
                line=2,
            ),
        ),
        warnings=(
            ReviewFinding(
                severity="warning",
                message="Add a comment",
                path=None,
                line=None,
            ),
        ),
        conclusion="Fix the critical issue",
    )
    assert decision.critical_findings[0].severity == "critical"

    with pytest.raises(ValidationError, match="critical_findings"):
        ReviewDecision(
            approved=False,
            critical_findings=(
                ReviewFinding(
                    severity="warning",
                    message="Wrong bucket",
                    path=None,
                    line=None,
                ),
            ),
            warnings=(),
            conclusion="Invalid",
        )
    with pytest.raises(ValidationError, match="approved"):
        ReviewDecision(
            approved=True,
            critical_findings=decision.critical_findings,
            warnings=(),
            conclusion="Contradictory",
        )

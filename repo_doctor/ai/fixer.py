"""One-issue AI repair with verification and automatic rollback."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..fixer import verify_clean_git
from ..models import ScanResult
from ..scanner import scan
from .errors import PatchValidationError, VerificationError
from .models import PatchProposal, PatchRequest, SemanticFinding, Severity
from .patching import (
    MIN_PATCH_CONFIDENCE,
    PreparedPatch,
    apply_patch,
    prepare_patch,
    render_patch_diff,
    rollback_patch,
)
from .provider import LLMProvider
from .workflow import analyze_repository

FixStatus = Literal["no_candidate", "dry_run", "kept", "rolled_back"]
SEVERITY_ORDER = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
}


@dataclass(frozen=True)
class AIFixOutcome:
    """User-facing state from one constrained repair attempt."""

    status: FixStatus
    finding: SemanticFinding | None = None
    proposal: PatchProposal | None = None
    diff: str = ""
    verification: str = ""


def select_fix_candidate(findings: tuple[SemanticFinding, ...]) -> SemanticFinding | None:
    """Choose at most one high-confidence issue by severity then confidence."""
    eligible = [item for item in findings if item.confidence >= MIN_PATCH_CONFIDENCE]
    if not eligible:
        return None
    return max(eligible, key=lambda item: (SEVERITY_ORDER[item.severity], item.confidence, item.id))


def _verification_passed(before: ScanResult, after: ScanResult) -> tuple[bool, str]:
    if not before.commands:
        return False, "No test or lint commands were available for verification."
    expected = [(item.name, item.command) for item in before.commands]
    actual = [(item.name, item.command) for item in after.commands]
    if actual != expected:
        return False, "The verification command set changed after the patch."
    failures = [item.name for item in after.commands if not item.passed]
    if failures:
        return False, "Verification failed: " + ", ".join(failures) + "."
    if after.deterministic_score < before.deterministic_score:
        return False, "Deterministic health score regressed after the patch."
    return True, "All discovered tests and linters passed without score regression."


def execute_ai_fix(
    root: Path,
    provider: LLMProvider,
    *,
    timeout: int = 120,
    dry_run: bool = False,
    scan_function: Callable[[Path, int], ScanResult] = scan,
) -> AIFixOutcome:
    """Analyze, propose one patch, and either verify it or restore exact bytes."""
    root = root.resolve()
    verify_clean_git(root)
    baseline = scan_function(root, timeout)
    if not baseline.commands:
        raise VerificationError(
            "AI fix requires at least one discovered test or lint command for verification."
        )
    response, contexts = analyze_repository(baseline, provider)
    finding = select_fix_candidate(response.findings)
    if finding is None:
        return AIFixOutcome("no_candidate")
    context_by_path = {item.path: item for item in contexts}
    context = context_by_path[finding.file]
    proposal = provider.generate_patch(PatchRequest(finding, context))
    if proposal.file != finding.file:
        raise PatchValidationError("AI patch file does not match the selected finding.")
    prepared: PreparedPatch = prepare_patch(root, proposal, expected_sha256=context.sha256)
    preview = render_patch_diff(prepared)
    if dry_run:
        return AIFixOutcome("dry_run", finding, proposal, preview)

    apply_patch(prepared)
    try:
        after = scan_function(root, timeout)
        passed, verification = _verification_passed(baseline, after)
    except Exception as error:
        rollback_patch(prepared)
        return AIFixOutcome(
            "rolled_back", finding, proposal, preview, f"Verification could not complete: {error}"
        )
    if passed:
        return AIFixOutcome("kept", finding, proposal, preview, verification)
    rollback_patch(prepared)
    return AIFixOutcome("rolled_back", finding, proposal, preview, verification)

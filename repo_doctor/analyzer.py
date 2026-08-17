"""Small, deterministic health analysis rules."""

from pathlib import Path

from .ai.models import Severity
from .models import ScanResult

AI_CONFIDENCE_THRESHOLD = 0.85
AI_SCORE_PENALTIES = {
    Severity.CRITICAL: 20,
    Severity.HIGH: 12,
    Severity.MEDIUM: 5,
    Severity.LOW: 0,
}


def analyze(result: ScanResult) -> None:
    """Annotate and score collected evidence."""
    if not any(name.lower().startswith("readme") for name in result.inspected_files):
        result.maintainability_issues.append("No README was found at the repository root.")
    if not result.technologies:
        result.maintainability_issues.append("No supported project manifest was detected.")
    failed = [item for item in result.commands if not item.passed]
    for item in failed:
        result.potential_bugs.append(f"{item.name} failed with exit code {item.exit_code}.")
    if not result.commands:
        result.maintainability_issues.append("No supported test or lint commands were discovered.")
    result.deterministic_score = max(
        0, 100 - 25 * len(failed) - 10 * len(result.maintainability_issues)
    )
    result.score = result.deterministic_score


def apply_ai_score(result: ScanResult) -> None:
    """Apply transparent, bounded penalties for validated high-confidence findings."""
    penalty = sum(
        AI_SCORE_PENALTIES[finding.severity]
        for finding in result.ai_findings
        if finding.confidence >= AI_CONFIDENCE_THRESHOLD
    )
    result.score = max(0, result.deterministic_score - penalty)


def text_files(root: Path):
    """Yield small source/config text files while skipping tool metadata."""
    ignored = {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
    }
    for path in root.rglob("*"):
        if (
            not path.is_symlink()
            and path.is_file()
            and not ignored.intersection(path.parts)
            and path.stat().st_size <= 1_000_000
        ):
            yield path

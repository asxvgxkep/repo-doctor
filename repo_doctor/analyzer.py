"""Small, deterministic health analysis rules."""

from pathlib import Path

from .models import ScanResult


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
    result.score = max(0, 100 - 25 * len(failed) - 10 * len(result.maintainability_issues))


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
            path.is_file()
            and not ignored.intersection(path.parts)
            and path.stat().st_size <= 1_000_000
        ):
            yield path

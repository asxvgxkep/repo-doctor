"""Conservative, auditable repository fixes."""

import subprocess
from pathlib import Path

from .analyzer import text_files


def verify_clean_git(root: Path) -> None:
    """Require a clean Git worktree before edits."""
    check = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode or check.stdout.strip() != "true":
        raise ValueError("Fix mode requires a Git repository.")
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode or status.stdout.strip():
        raise ValueError("Fix mode requires a clean worktree; commit or stash changes first.")


def apply_high_confidence_fix(root: Path) -> str | None:
    """Remove trailing horizontal whitespace, a semantics-preserving fix."""
    for path in text_files(root):
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        fixed = "\n".join(line.rstrip(" \t") for line in original.split("\n"))
        if fixed != original:
            path.write_text(fixed, encoding="utf-8")
            return f"Removed trailing whitespace from {path.relative_to(root)}"
    return None

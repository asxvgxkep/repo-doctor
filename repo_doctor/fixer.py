"""Conservative, auditable repository fixes."""

import ast
import io
import subprocess
import tokenize
from pathlib import Path

from .analyzer import text_files


def verify_clean_git(root: Path) -> None:
    """Require a clean Git worktree before edits."""
    try:
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
    except FileNotFoundError as error:
        raise ValueError("Git is required for fix mode but was not found.") from error
    if status.returncode or status.stdout.strip():
        raise ValueError("Fix mode requires a clean worktree; commit or stash changes first.")


def _python_whitespace_fix(original: str) -> str | None:
    """Return a conservative Python-only whitespace fix, or None when unsafe/unneeded."""
    try:
        ast.parse(original)
        tokens = tuple(tokenize.generate_tokens(io.StringIO(original).readline))
    except (SyntaxError, tokenize.TokenError):
        return None

    protected_lines: set[int] = set()
    for token in tokens:
        token_name = tokenize.tok_name[token.type]
        if (token_name == "STRING" or token_name.startswith("FSTRING")) and (
            token.start[0] != token.end[0]
        ):
            protected_lines.update(range(token.start[0], token.end[0] + 1))

    fixed_lines: list[str] = []
    for line_number, line in enumerate(original.splitlines(keepends=True), 1):
        if line.endswith("\r\n"):
            body, ending = line[:-2], "\r\n"
        elif line.endswith(("\n", "\r")):
            body, ending = line[:-1], line[-1]
        else:
            body, ending = line, ""
        candidate = body.rstrip(" \t")
        if line_number in protected_lines or candidate.endswith("\\"):
            fixed_lines.append(line)
        else:
            fixed_lines.append(candidate + ending)
    fixed = "".join(fixed_lines)
    return fixed if fixed != original else None


def apply_high_confidence_fix(root: Path) -> str | None:
    """Remove safe trailing whitespace from parseable Python source."""
    root = root.resolve()
    for path in text_files(root):
        if path.suffix.lower() != ".py":
            continue
        try:
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
                continue
            original = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError):
            continue
        fixed = _python_whitespace_fix(original)
        if fixed is not None:
            path.write_bytes(fixed.encode("utf-8"))
            return f"Removed trailing whitespace from {path.relative_to(root)}"
    return None

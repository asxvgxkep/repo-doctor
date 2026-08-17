"""Read-only repository scanning orchestration."""

import shutil
import tempfile
from pathlib import Path

from .analyzer import analyze, text_files
from .detector import detect_technologies, discover_commands
from .models import ScanResult
from .runner import run_command

CONFIG_NAMES = {
    "README.md",
    "README.rst",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "pytest.ini",
    "ruff.toml",
    ".eslintrc",
    ".eslintrc.json",
}


def scan(root: Path, timeout: int = 120) -> ScanResult:
    """Inspect *root* and verify a temporary copy, leaving the source untouched."""
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"Repository path is not a directory: {root}")
    files = list(text_files(root))
    lines = 0
    for path in files:
        try:
            lines += sum(1 for _ in path.open(encoding="utf-8"))
        except (OSError, UnicodeError):
            pass
    inspected = sorted(
        path.name for path in files if path.parent == root and path.name in CONFIG_NAMES
    )
    technologies = detect_technologies(root)
    result = ScanResult(root, technologies, len(files), lines, inspected)
    with tempfile.TemporaryDirectory(prefix="repo-doctor-") as temporary:
        copy = Path(temporary) / "repository"
        shutil.copytree(
            root,
            copy,
            ignore=shutil.ignore_patterns(
                ".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"
            ),
        )
        for name, command in discover_commands(copy, technologies):
            result.commands.append(run_command(name, command, copy, timeout))
    analyze(result)
    return result

"""Deterministic, bounded, secret-aware repository context selection."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..models import ScanResult
from ..security import redact_sensitive_text
from .errors import ContextSelectionError
from .models import FileContext
from .paths import has_excluded_directory, is_secret_path

SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".swift",
    ".ts",
    ".tsx",
}
STACK_SUFFIXES = {
    "Python": {".py"},
    "Node.js": {".js", ".jsx", ".ts", ".tsx"},
}
CONFIG_NAMES = {
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "package.json",
    "tsconfig.json",
    "pytest.ini",
    "ruff.toml",
    ".eslintrc",
    ".eslintrc.json",
}
GENERATED_SUFFIXES = {".map", ".min.js", ".min.css", ".lock"}


@dataclass(frozen=True)
class SelectionLimits:
    """Hard limits applied before any content can reach a provider."""

    max_files: int = 20
    max_file_bytes: int = 100_000
    max_total_characters: int = 200_000


def _failed_evidence(result: ScanResult) -> str:
    return "\n".join(
        f"{item.name}\n{item.stdout[-8000:]}\n{item.stderr[-8000:]}"
        for item in result.commands
        if not item.passed
    ).lower()


def _priority(relative: str, size: int, result: ScanResult, evidence: str) -> tuple[int, str]:
    path = Path(relative)
    lower = relative.lower()
    suffix = path.suffix.lower()
    score = 0
    if lower in evidence or lower.replace("/", "\\") in evidence:
        score += 1_000
    elif path.name.lower() in evidence:
        score += 800
    stack_suffixes = set().union(*(STACK_SUFFIXES.get(item, set()) for item in result.technologies))
    if suffix in stack_suffixes:
        score += 400
    elif suffix in SOURCE_SUFFIXES:
        score += 250
    if path.name.lower() in CONFIG_NAMES:
        score += 300
    if path.name.lower().startswith("readme"):
        score += 50
    score += max(0, 100 - size // 1_000)
    return (-score, lower)


def _is_generated(relative: str) -> bool:
    lower = relative.lower()
    return any(lower.endswith(suffix) for suffix in GENERATED_SUFFIXES)


def select_context(
    root: Path, result: ScanResult, limits: SelectionLimits | None = None
) -> tuple[FileContext, ...]:
    """Select the most relevant safe text files without exceeding hard limits."""
    limits = limits or SelectionLimits()
    if min(limits.max_files, limits.max_file_bytes, limits.max_total_characters) <= 0:
        raise ContextSelectionError("Context selection limits must be positive.")
    root = root.resolve()
    evidence = _failed_evidence(result)
    candidates: list[tuple[tuple[int, str], Path, str, int]] = []
    try:
        paths = root.rglob("*")
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if (
                has_excluded_directory(relative)
                or is_secret_path(relative)
                or _is_generated(relative)
            ):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > limits.max_file_bytes:
                continue
            candidates.append((_priority(relative, size, result, evidence), path, relative, size))
    except OSError as error:
        raise ContextSelectionError(f"Could not inspect repository context: {error}.") from error

    selected: list[FileContext] = []
    total = 0
    for _, path, relative, _ in sorted(candidates):
        if len(selected) >= limits.max_files:
            break
        try:
            data = path.read_bytes()
            if len(data) > limits.max_file_bytes or b"\x00" in data:
                continue
            content = redact_sensitive_text(data.decode("utf-8"))
        except (OSError, UnicodeError):
            continue
        if total + len(content) > limits.max_total_characters:
            continue
        selected.append(FileContext(relative, content, hashlib.sha256(data).hexdigest()))
        total += len(content)
    return tuple(selected)

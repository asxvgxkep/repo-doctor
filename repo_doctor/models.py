"""Shared, immutable data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ai.models import SemanticFinding


@dataclass(frozen=True)
class CommandResult:
    """Captured result of a verification command."""

    name: str
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class ScanResult:
    """All evidence collected during a scan."""

    path: Path
    technologies: list[str]
    files: int
    lines: int
    inspected_files: list[str]
    commands: list[CommandResult] = field(default_factory=list)
    potential_bugs: list[str] = field(default_factory=list)
    maintainability_issues: list[str] = field(default_factory=list)
    deterministic_score: int = 100
    score: int = 100
    ai_requested: bool = False
    ai_findings: list[SemanticFinding] = field(default_factory=list)
    ai_error: str | None = None
    ai_context_files: list[str] = field(default_factory=list)

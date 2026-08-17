"""Shared, immutable data models."""

from dataclasses import dataclass, field
from pathlib import Path


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
    score: int = 100

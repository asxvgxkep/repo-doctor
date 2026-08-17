"""Structured request and response models for semantic analysis."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    """Controlled severity vocabulary accepted from providers."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class FileContext:
    """A bounded text file sent to a provider."""

    path: str
    content: str
    sha256: str


@dataclass(frozen=True)
class VerificationEvidence:
    """Sanitized command evidence included in an analysis request."""

    name: str
    command: tuple[str, ...]
    passed: bool
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


@dataclass(frozen=True)
class SemanticFinding:
    """A validated semantic issue tied to concrete source code."""

    id: str
    title: str
    category: str
    severity: Severity
    confidence: float
    file: str
    line_start: int
    line_end: int
    explanation: str
    evidence: str
    suggested_fix: str


@dataclass(frozen=True)
class AnalysisRequest:
    """Deterministic evidence and selected files for one provider request."""

    repository_name: str
    technologies: tuple[str, ...]
    file_count: int
    line_count: int
    verifications: tuple[VerificationEvidence, ...]
    deterministic_findings: tuple[str, ...]
    files: tuple[FileContext, ...]


@dataclass(frozen=True)
class AnalysisResponse:
    """Validated provider analysis."""

    findings: tuple[SemanticFinding, ...]


@dataclass(frozen=True)
class PatchRequest:
    """One selected finding and its exact source context."""

    finding: SemanticFinding
    file: FileContext


@dataclass(frozen=True)
class PatchProposal:
    """Constrained single-file replacement returned by a provider."""

    file: str
    old_text: str
    new_text: str
    reason: str
    confidence: float

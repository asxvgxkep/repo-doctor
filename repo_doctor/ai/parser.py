"""Strict JSON parsing for provider-controlled program state."""

from __future__ import annotations

import json
from typing import Any

from .errors import ResponseValidationError
from .models import AnalysisResponse, PatchProposal, SemanticFinding, Severity
from .paths import normalize_relative_path

FINDING_FIELDS = {
    "id",
    "title",
    "category",
    "severity",
    "confidence",
    "file",
    "line_start",
    "line_end",
    "explanation",
    "evidence",
    "suggested_fix",
}
PATCH_FIELDS = {"file", "old_text", "new_text", "reason", "confidence"}


def _decode(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise ResponseValidationError("AI provider returned malformed JSON.") from error


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResponseValidationError(f"{label} must be a JSON object.")
    missing = fields - value.keys()
    extra = value.keys() - fields
    if missing:
        raise ResponseValidationError(f"{label} is missing fields: {', '.join(sorted(missing))}.")
    if extra:
        raise ResponseValidationError(f"{label} has unexpected fields: {', '.join(sorted(extra))}.")
    return value


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ResponseValidationError(f"AI field '{field}' must be a non-empty string.")
    if any(ord(character) < 32 and character not in "\r\n\t" for character in value):
        raise ResponseValidationError(f"AI field '{field}' contains unsafe control characters.")
    return value


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResponseValidationError("AI field 'confidence' must be a number between 0.0 and 1.0.")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ResponseValidationError("AI field 'confidence' must be between 0.0 and 1.0.")
    return result


def _line(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ResponseValidationError(f"AI field '{field}' must be a positive integer.")
    return value


def _finding(value: Any, index: int) -> SemanticFinding:
    item = _object(value, FINDING_FIELDS, f"Finding {index}")
    try:
        severity = Severity(item["severity"])
    except (TypeError, ValueError) as error:
        allowed = ", ".join(level.value for level in Severity)
        raise ResponseValidationError(f"AI severity must be one of: {allowed}.") from error
    line_start = _line(item["line_start"], "line_start")
    line_end = _line(item["line_end"], "line_end")
    if line_end < line_start:
        raise ResponseValidationError("AI field 'line_end' must not precede 'line_start'.")
    try:
        path = normalize_relative_path(_text(item["file"], "file"))
    except ValueError as error:
        raise ResponseValidationError(f"AI finding path is unsafe: {error}.") from error
    return SemanticFinding(
        id=_text(item["id"], "id"),
        title=_text(item["title"], "title"),
        category=_text(item["category"], "category"),
        severity=severity,
        confidence=_confidence(item["confidence"]),
        file=path,
        line_start=line_start,
        line_end=line_end,
        explanation=_text(item["explanation"], "explanation"),
        evidence=_text(item["evidence"], "evidence"),
        suggested_fix=_text(item["suggested_fix"], "suggested_fix"),
    )


def parse_analysis_response(raw: str) -> AnalysisResponse:
    """Parse the exact analysis schema, rejecting missing or invented fields."""
    data = _object(_decode(raw), {"findings"}, "AI analysis response")
    values = data["findings"]
    if not isinstance(values, list):
        raise ResponseValidationError("AI field 'findings' must be a JSON array.")
    if len(values) > 50:
        raise ResponseValidationError("AI response contains too many findings (maximum 50).")
    findings = tuple(_finding(value, index) for index, value in enumerate(values, 1))
    identifiers = [finding.id for finding in findings]
    if len(identifiers) != len(set(identifiers)):
        raise ResponseValidationError("AI finding IDs must be unique.")
    return AnalysisResponse(findings)


def parse_patch_response(raw: str) -> PatchProposal:
    """Parse a constrained single-file replacement schema."""
    data = _object(_decode(raw), PATCH_FIELDS, "AI patch response")
    try:
        path = normalize_relative_path(_text(data["file"], "file"))
    except ValueError as error:
        raise ResponseValidationError(f"AI patch path is unsafe: {error}.") from error
    return PatchProposal(
        file=path,
        old_text=_text(data["old_text"], "old_text"),
        new_text=_text(data["new_text"], "new_text", allow_empty=True),
        reason=_text(data["reason"], "reason"),
        confidence=_confidence(data["confidence"]),
    )

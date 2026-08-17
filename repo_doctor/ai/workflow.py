"""Semantic analysis orchestration independent of any model vendor."""

from __future__ import annotations

from ..analyzer import apply_ai_score
from ..models import ScanResult
from ..security import redact_sensitive_text
from .errors import ResponseValidationError
from .models import AnalysisRequest, AnalysisResponse, FileContext, VerificationEvidence
from .parser import parse_analysis_response
from .provider import LLMProvider
from .selector import SelectionLimits, select_context


def _validated_response(response: AnalysisResponse) -> AnalysisResponse:
    """Revalidate protocol implementations before their data becomes program state."""
    if not isinstance(response, AnalysisResponse):
        raise ResponseValidationError("AI provider returned an unexpected analysis object.")
    # The strict parser is also used for fakes/custom providers, not just HTTP responses.
    import json

    try:
        payload = {
            "findings": [
                {
                    "id": item.id,
                    "title": item.title,
                    "category": item.category,
                    "severity": item.severity.value
                    if hasattr(item.severity, "value")
                    else item.severity,
                    "confidence": item.confidence,
                    "file": item.file,
                    "line_start": item.line_start,
                    "line_end": item.line_end,
                    "explanation": item.explanation,
                    "evidence": item.evidence,
                    "suggested_fix": item.suggested_fix,
                }
                for item in response.findings
            ]
        }
        return parse_analysis_response(json.dumps(payload))
    except ResponseValidationError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise ResponseValidationError(
            "AI provider returned an unexpected analysis schema."
        ) from error


def analyze_repository(
    result: ScanResult,
    provider: LLMProvider,
    limits: SelectionLimits | None = None,
) -> tuple[AnalysisResponse, tuple[FileContext, ...]]:
    """Select context, invoke a provider, validate findings, and update the score."""
    result.ai_requested = True
    contexts = select_context(result.path, result, limits)
    request = AnalysisRequest(
        repository_name=result.path.name,
        technologies=tuple(result.technologies),
        file_count=result.files,
        line_count=result.lines,
        verifications=tuple(
            VerificationEvidence(
                name=item.name,
                command=item.command,
                passed=item.passed,
                exit_code=item.exit_code,
                stdout=redact_sensitive_text(item.stdout[-8_000:]),
                stderr=redact_sensitive_text(item.stderr[-8_000:]),
                timed_out=item.timed_out,
            )
            for item in result.commands
        ),
        deterministic_findings=tuple(result.potential_bugs + result.maintainability_issues),
        files=contexts,
    )
    response = _validated_response(provider.analyze(request))
    allowed = {item.path for item in contexts}
    invalid = sorted({item.file for item in response.findings} - allowed)
    if invalid:
        raise ResponseValidationError(
            "AI findings referenced files that were not provided: " + ", ".join(invalid) + "."
        )
    context_by_path = {item.path: item for item in contexts}
    invalid_lines = [
        item.id
        for item in response.findings
        if item.line_end > max(1, len(context_by_path[item.file].content.splitlines()))
    ]
    if invalid_lines:
        raise ResponseValidationError(
            "AI findings referenced lines outside supplied files: " + ", ".join(invalid_lines) + "."
        )
    result.ai_findings = list(response.findings)
    result.ai_context_files = [item.path for item in contexts]
    result.ai_error = None
    apply_ai_score(result)
    return response, contexts

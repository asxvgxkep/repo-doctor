"""Semantic analysis orchestration independent of any model vendor."""

from __future__ import annotations

from ..analyzer import apply_ai_score
from ..models import ScanResult
from ..security import bounded_sensitive_text
from .errors import ResponseValidationError
from .models import (
    AnalysisRequest,
    AnalysisResponse,
    BehavioralContract,
    FileContext,
    SemanticFinding,
    VerificationEvidence,
)
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
        payload: dict[str, object] = {
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
        if response.behavioral_contract is not None:
            payload["behavioral_contract"] = {
                "must_fix": response.behavioral_contract.must_fix,
                "must_preserve": response.behavioral_contract.must_preserve,
                "evidence": response.behavioral_contract.evidence,
                "rationale": response.behavioral_contract.rationale,
            }
        return parse_analysis_response(json.dumps(payload))
    except ResponseValidationError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise ResponseValidationError(
            "AI provider returned an unexpected analysis schema."
        ) from error


def _unique_bounded(items: list[str], *, limit: int = 100) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        safe = bounded_sensitive_text(item, 2_000).strip()
        if safe and safe not in seen:
            seen.add(safe)
            result.append(safe)
        if len(result) == limit:
            break
    return tuple(result)


def build_behavioral_contract(
    request: AnalysisRequest,
    findings: tuple[SemanticFinding, ...],
    proposed: BehavioralContract | None = None,
) -> BehavioralContract:
    """Complete a provider contract with every authoritative evidence source."""
    must_fix: list[str] = []
    must_preserve: list[str] = []
    evidence: list[str] = []

    if request.task:
        must_fix.append(f"Satisfy the user task: {request.task}")
        evidence.append(f"User task: {request.task}")

    for item in request.verifications:
        command = " ".join(item.command)
        output = "\n".join(value for value in (item.stdout, item.stderr) if value).strip()
        status = "passed" if item.passed else "failed"
        evidence.append(
            f"{item.name} ({command}) {status} with return code {item.exit_code}."
            + (f"\n{output}" if output else "")
        )
        if item.passed:
            must_preserve.append(
                f"Preserve behavior covered by the passing baseline verification {item.name}."
            )
        else:
            must_fix.append(f"Make the failing baseline verification {item.name} pass.")
            if "passed" in output.casefold():
                must_preserve.append(
                    f"Preserve unrelated behavior already passing within {item.name}."
                )
            else:
                must_preserve.append(
                    f"Preserve baseline behavior not implicated by failures in {item.name}."
                )

    for finding in findings:
        must_fix.append(f"Resolve finding {finding.id} ({finding.title}): {finding.suggested_fix}")
        evidence.append(f"Finding {finding.id}: {finding.evidence}")

    for deterministic in request.deterministic_findings:
        must_fix.append(f"Address deterministic finding: {deterministic}")
        evidence.append(f"Deterministic finding: {deterministic}")

    if proposed is not None:
        must_fix.extend(proposed.must_fix)
        must_preserve.extend(proposed.must_preserve)
        evidence.extend(proposed.evidence)

    rationale = (
        proposed.rationale
        if proposed is not None
        else (
            "The repair must satisfy the task and all failing evidence together while "
            "preserving behavior already demonstrated by the baseline."
        )
    )
    return BehavioralContract(
        must_fix=_unique_bounded(must_fix),
        must_preserve=_unique_bounded(must_preserve),
        evidence=_unique_bounded(evidence),
        rationale=bounded_sensitive_text(rationale, 2_000),
    )


def analyze_repository(
    result: ScanResult,
    provider: LLMProvider,
    limits: SelectionLimits | None = None,
    *,
    task: str | None = None,
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
                stdout=bounded_sensitive_text(item.stdout),
                stderr=bounded_sensitive_text(item.stderr),
                timed_out=item.timed_out,
            )
            for item in result.commands
        ),
        deterministic_findings=tuple(result.potential_bugs + result.maintainability_issues),
        files=contexts,
        task=bounded_sensitive_text(task) if task and task.strip() else None,
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
    response = AnalysisResponse(
        response.findings,
        build_behavioral_contract(request, response.findings, response.behavioral_contract),
    )
    result.ai_findings = list(response.findings)
    result.ai_context_files = [item.path for item in contexts]
    result.ai_error = None
    apply_ai_score(result)
    return response, contexts

from pathlib import Path

import pytest

from repo_doctor.ai.errors import ResponseValidationError
from repo_doctor.ai.models import (
    AnalysisResponse,
    BehavioralContract,
    PatchProposal,
    SemanticFinding,
    Severity,
)
from repo_doctor.ai.workflow import analyze_repository
from repo_doctor.models import CommandResult, ScanResult
from repo_doctor.report import render_report


def finding(file: str = "app.py", confidence: float = 0.91) -> SemanticFinding:
    return SemanticFinding(
        id="finding-1",
        title="Incorrect boundary",
        category="control-flow",
        severity=Severity.HIGH,
        confidence=confidence,
        file=file,
        line_start=1,
        line_end=1,
        explanation="The equality case is rejected.",
        evidence="The comparison uses < rather than <=.",
        suggested_fix="Accept equality.",
    )


class FakeProvider:
    def __init__(self, response: AnalysisResponse):
        self.response = response
        self.request = None

    def analyze(self, request):
        self.request = request
        return self.response

    def generate_patch(self, request):
        return PatchProposal(request.file.path, "<", "<=", "Accept equality", 0.95)


def scan_result(root: Path) -> ScanResult:
    return ScanResult(root, ["Python"], 1, 1, [], deterministic_score=100, score=100)


def test_ai_is_disabled_by_default_in_report(tmp_path: Path) -> None:
    report = render_report(scan_result(tmp_path))
    assert "AI Semantic Analysis: Not requested" in report


def test_provider_abstraction_receives_bounded_context_and_updates_score(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("return requested < stock\n", encoding="utf-8")
    provider = FakeProvider(AnalysisResponse((finding(),)))
    result = scan_result(tmp_path)
    response, contexts = analyze_repository(result, provider)
    assert response.findings == (finding(),)
    assert provider.request.files == contexts
    assert [item.path for item in contexts] == ["app.py"]
    assert result.deterministic_score == 100
    assert result.score == 88
    assert "Severity: High" in render_report(result)


def test_low_confidence_finding_does_not_change_score(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("pass\n", encoding="utf-8")
    result = scan_result(tmp_path)
    analyze_repository(result, FakeProvider(AnalysisResponse((finding(confidence=0.5),))))
    assert result.score == result.deterministic_score


def test_finding_for_unsent_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("pass\n", encoding="utf-8")
    response = AnalysisResponse((finding(file="not-sent.py"),))
    with pytest.raises(ResponseValidationError, match="not provided"):
        analyze_repository(scan_result(tmp_path), FakeProvider(response))


def test_behavioral_contract_combines_task_findings_and_verification_behavior(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("return requested < stock\n", encoding="utf-8")
    result = scan_result(tmp_path)
    result.commands = [
        CommandResult("Focused tests", ("pytest", "focused"), 0, "2 passed", "", 0.1),
        CommandResult(
            "Full tests",
            ("pytest",),
            1,
            "1 passed, 1 failed",
            "boundary failure",
            0.2,
        ),
    ]
    proposed = BehavioralContract(
        ("Treat related symptoms as one repair.",),
        ("Keep the public API stable.",),
        ("Provider synthesized all evidence.",),
        "One coherent patch is required.",
    )
    provider = FakeProvider(AnalysisResponse((finding(),), proposed))

    response, _ = analyze_repository(
        result,
        provider,
        task="Accept exact stock while preserving smaller requests.",
    )

    contract = response.behavioral_contract
    assert contract is not None
    assert provider.request.task == "Accept exact stock while preserving smaller requests."
    assert "Treat related symptoms as one repair." in contract.must_fix
    assert any("Satisfy the user task" in item for item in contract.must_fix)
    assert any("finding-1" in item for item in contract.must_fix)
    assert "Keep the public API stable." in contract.must_preserve
    assert any("Focused tests" in item for item in contract.must_preserve)
    assert any("already passing within Full tests" in item for item in contract.must_preserve)
    assert any("1 passed, 1 failed" in item for item in contract.evidence)

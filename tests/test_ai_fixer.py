import subprocess
from pathlib import Path

import pytest

from repo_doctor.ai.errors import VerificationError
from repo_doctor.ai.fixer import execute_ai_fix
from repo_doctor.ai.models import AnalysisResponse, PatchProposal, SemanticFinding, Severity
from repo_doctor.models import CommandResult, ScanResult


class RepairProvider:
    def analyze(self, request):
        return AnalysisResponse(
            (
                SemanticFinding(
                    "boundary-1",
                    "Exact stock is rejected",
                    "control-flow",
                    Severity.HIGH,
                    0.95,
                    "inventory.py",
                    2,
                    2,
                    "Equality is a valid fulfillment case.",
                    "The code uses requested < stock.",
                    "Change the comparison to <=.",
                ),
            )
        )

    def generate_patch(self, request):
        return PatchProposal(
            "inventory.py",
            "return requested < stock",
            "return requested <= stock",
            "Accept the equality boundary",
            0.96,
        )


def initialize_repository(root: Path) -> bytes:
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='1.0'\n")
    target = root / "inventory.py"
    target.write_bytes(b"def can_fulfill(stock, requested):\r\n    return requested < stock\r\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return target.read_bytes()


def verification_result(root: Path, passed: bool = True) -> ScanResult:
    command = CommandResult("Python tests", ("pytest",), 0 if passed else 1, "", "", 0.01)
    score = 100 if passed else 75
    return ScanResult(
        root,
        ["Python"],
        2,
        3,
        ["pyproject.toml"],
        commands=[command],
        deterministic_score=score,
        score=score,
    )


def test_successful_ai_fix_runs_verification_and_keeps_change(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    calls = 0

    def scanner(root: Path, timeout: int) -> ScanResult:
        nonlocal calls
        calls += 1
        return verification_result(root)

    outcome = execute_ai_fix(tmp_path, RepairProvider(), scan_function=scanner)
    assert outcome.status == "kept"
    assert calls == 2
    assert "requested <= stock" in (tmp_path / "inventory.py").read_text(encoding="utf-8")


def test_failed_verification_rolls_back_exact_bytes(tmp_path: Path) -> None:
    original = initialize_repository(tmp_path)
    calls = 0

    def scanner(root: Path, timeout: int) -> ScanResult:
        nonlocal calls
        calls += 1
        return verification_result(root, passed=calls == 1)

    outcome = execute_ai_fix(tmp_path, RepairProvider(), scan_function=scanner)
    assert outcome.status == "rolled_back"
    assert (tmp_path / "inventory.py").read_bytes() == original
    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == ""


def test_verification_exception_rolls_back(tmp_path: Path) -> None:
    original = initialize_repository(tmp_path)
    calls = 0

    def scanner(root: Path, timeout: int) -> ScanResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("verification unavailable")
        return verification_result(root)

    outcome = execute_ai_fix(tmp_path, RepairProvider(), scan_function=scanner)
    assert outcome.status == "rolled_back"
    assert "could not complete" in outcome.verification
    assert (tmp_path / "inventory.py").read_bytes() == original


def test_verification_timeout_rolls_back(tmp_path: Path) -> None:
    original = initialize_repository(tmp_path)
    calls = 0

    def scanner(root: Path, timeout: int) -> ScanResult:
        nonlocal calls
        calls += 1
        result = verification_result(root)
        if calls == 2:
            result.commands[0] = CommandResult(
                "Python tests", ("pytest",), 124, "", "timed out", 1.0, timed_out=True
            )
            result.deterministic_score = 75
            result.score = 75
        return result

    outcome = execute_ai_fix(tmp_path, RepairProvider(), scan_function=scanner)
    assert outcome.status == "rolled_back"
    assert (tmp_path / "inventory.py").read_bytes() == original


def test_dry_run_performs_no_modification(tmp_path: Path) -> None:
    original = initialize_repository(tmp_path)
    calls = 0

    def scanner(root: Path, timeout: int) -> ScanResult:
        nonlocal calls
        calls += 1
        return verification_result(root)

    outcome = execute_ai_fix(tmp_path, RepairProvider(), dry_run=True, scan_function=scanner)
    assert outcome.status == "dry_run"
    assert "-    return requested < stock" in outcome.diff
    assert calls == 1
    assert (tmp_path / "inventory.py").read_bytes() == original


def test_ai_fix_refuses_to_patch_without_verification_commands(tmp_path: Path) -> None:
    initialize_repository(tmp_path)

    def scanner(root: Path, timeout: int) -> ScanResult:
        return ScanResult(root, ["Python"], 2, 3, ["pyproject.toml"])

    with pytest.raises(VerificationError, match="at least one"):
        execute_ai_fix(tmp_path, RepairProvider(), scan_function=scanner)

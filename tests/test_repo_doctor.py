import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repo_doctor.cli import app
from repo_doctor.detector import detect_technologies, discover_commands
from repo_doctor.fixer import apply_high_confidence_fix, verify_clean_git
from repo_doctor.report import render_report
from repo_doctor.scanner import scan

FIXTURE = Path(__file__).parent / "fixtures" / "python_project"


def test_detect_and_discover() -> None:
    technologies = detect_technologies(FIXTURE)
    assert technologies == ["Python"]
    assert ("Python tests", ("pytest",)) in discover_commands(FIXTURE, technologies)


def test_scan_and_report_does_not_modify_source(tmp_path: Path) -> None:
    target = tmp_path / "project"
    shutil.copytree(FIXTURE, target)
    before = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    result = scan(target)
    after = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    report = render_report(result)
    assert before == after
    assert result.score == 100
    assert "## Health Score (0-100)" in report
    assert "Python tests: PASS" in report


def test_cli_writes_report(tmp_path: Path) -> None:
    output = tmp_path / "health.md"
    response = CliRunner().invoke(app, ["scan", str(FIXTURE), "--output", str(output)])
    assert response.exit_code == 0, response.output
    assert "Health score: 100/100" in response.output
    assert output.exists()


def test_fixer_requires_git_and_fixes_one_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Git repository"):
        verify_clean_git(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    file = tmp_path / "example.py"
    file.write_text("value = 1  \n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    verify_clean_git(tmp_path)
    assert apply_high_confidence_fix(tmp_path) == "Removed trailing whitespace from example.py"
    assert file.read_text(encoding="utf-8") == "value = 1\n"

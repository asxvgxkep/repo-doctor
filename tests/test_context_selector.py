from pathlib import Path

from repo_doctor.ai.selector import SelectionLimits, select_context
from repo_doctor.models import CommandResult, ScanResult


def result_for(root: Path, command: CommandResult | None = None) -> ScanResult:
    result = ScanResult(root, ["Python"], 0, 0, ["pyproject.toml"])
    if command:
        result.commands.append(command)
    return result


def test_context_selection_prioritizes_failed_output(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "core.py").write_text("CORE = True\n", encoding="utf-8")
    (source / "bug.py").write_text("BUG = True\n", encoding="utf-8")
    failure = CommandResult(
        "Python tests", ("pytest",), 1, "src/bug.py:1: assertion failed", "", 0.1
    )
    selected = select_context(tmp_path, result_for(tmp_path, failure), SelectionLimits(max_files=1))
    assert [item.path for item in selected] == ["src/bug.py"]


def test_context_selection_excludes_secrets_generated_and_binary_files(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('safe')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("TOKEN=placeholder\n", encoding="utf-8")
    (tmp_path / "credentials.json").write_text('{"key":"secret"}', encoding="utf-8")
    (tmp_path / ".npmrc").write_text("//registry/:_authToken=secret", encoding="utf-8")
    (tmp_path / "bundle.min.js").write_text("secret", encoding="utf-8")
    (tmp_path / "image.bin").write_bytes(b"text\x00binary")
    dependency = tmp_path / "node_modules"
    dependency.mkdir()
    (dependency / "package.js").write_text("secret", encoding="utf-8")
    selected = select_context(tmp_path, result_for(tmp_path))
    paths = {item.path for item in selected}
    assert paths == {"app.py"}
    assert all("secret" not in item.content for item in selected)


def test_context_redacts_inline_secret_assignments(tmp_path: Path) -> None:
    (tmp_path / "config.py").write_text(
        "API_KEY = 'literal-secret-value'\nSETTING = True\n", encoding="utf-8"
    )
    selected = select_context(tmp_path, result_for(tmp_path))
    assert len(selected) == 1
    assert "literal-secret-value" not in selected[0].content
    assert "[REDACTED]" in selected[0].content


def test_context_selection_enforces_file_and_repository_limits(tmp_path: Path) -> None:
    for index in range(5):
        (tmp_path / f"module_{index}.py").write_text("x" * 30, encoding="utf-8")
    (tmp_path / "oversized.py").write_text("x" * 101, encoding="utf-8")
    limits = SelectionLimits(max_files=2, max_file_bytes=100, max_total_characters=60)
    selected = select_context(tmp_path, result_for(tmp_path), limits)
    assert len(selected) == 2
    assert sum(len(item.content) for item in selected) <= 60
    assert "oversized.py" not in {item.path for item in selected}

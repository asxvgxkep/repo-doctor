import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repo_doctor.analyzer import is_utf8_text_file
from repo_doctor.backends import (
    FileReadError,
    LocalToolBackend,
    MCPToolBackend,
    ToolBackendKind,
    ToolBackendStartupError,
    ToolCallError,
    create_tool_backend,
)
from repo_doctor.cli import app
from repo_doctor.models import ScanResult
from repo_doctor.report import render_report
from repo_doctor.scanner import scan
from repo_doctor.sessions import load_session_file


class FakeMCPClient:
    def __init__(self, responses=None, *, startup_error=None, call_error=None):
        self.responses = responses or {}
        self.startup_error = startup_error
        self.call_error = call_error
        self.started = False
        self.closed = False
        self.calls = []

    def start(self):
        self.started = True
        if self.startup_error:
            raise self.startup_error

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.call_error:
            raise self.call_error
        return self.responses[name]

    def close(self):
        self.closed = True


def make_backend(tmp_path: Path, client: FakeMCPClient):
    captured = {}

    def factory(process):
        captured["process"] = process
        return client

    backend = MCPToolBackend(
        tmp_path,
        toolhub_project=tmp_path / "toolhub",
        client_factory=factory,
    )
    return backend, captured


def file_response(content: str = "hello\n") -> dict:
    data = content.encode()
    return {
        "path": "app.py",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content": content,
    }


def status_response() -> dict:
    return {
        "path": ".",
        "branch": "main",
        "clean": False,
        "entries": [{"code": " M", "path": "app.py"}],
        "raw": "## main\n M app.py\n",
    }


def diff_response() -> dict:
    return {
        "path": "app.py",
        "staged": False,
        "additions": 1,
        "deletions": 0,
        "binary": False,
        "raw": "+fixed\n",
    }


def shell_response(*, pending: bool = False) -> dict:
    if pending:
        return {
            "program": "pytest",
            "args": [],
            "cwd": ".",
            "risk": "MEDIUM",
            "risk_reason": "Running tests executes repository code.",
            "executed": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "request_id": "req_pending",
            "approval_status": "PENDING",
            "message": "Approval required (PENDING).",
        }
    return {
        "program": "python",
        "args": ["--version"],
        "cwd": ".",
        "risk": "LOW",
        "risk_reason": "Interpreter version query.",
        "executed": True,
        "returncode": 0,
        "stdout": "Python 3.13\n",
        "stderr": "",
        "timed_out": False,
        "request_id": None,
        "approval_status": None,
        "message": "",
    }


def approved_shell_response(*, status: str = "CONSUMED", executed: bool = True) -> dict:
    return {
        "program": "pytest",
        "args": [],
        "cwd": ".",
        "risk": "MEDIUM",
        "risk_reason": "Running tests executes repository code.",
        "executed": executed,
        "returncode": 0 if executed else None,
        "stdout": "1 passed\n" if executed else "",
        "stderr": "",
        "timed_out": False,
        "request_id": "req_pending",
        "approval_status": status,
        "message": "" if executed else f"Request is {status}; cannot execute.",
    }


def test_local_backend_is_default_and_preserves_local_cli(tmp_path: Path, monkeypatch) -> None:
    observed = {}

    def fake_scan(root, timeout):
        observed["root"] = root
        return ScanResult(root.resolve(), [], 0, 0, [])

    def unexpected_factory(*args, **kwargs):
        raise AssertionError("default local CLI must not initialize MCP")

    monkeypatch.setattr("repo_doctor.cli.scan", fake_scan)
    monkeypatch.setattr("repo_doctor.cli.create_tool_backend", unexpected_factory)

    response = CliRunner().invoke(app, ["scan", str(tmp_path)])

    assert response.exit_code == 0, response.output
    assert observed["root"] == tmp_path
    assert isinstance(create_tool_backend(ToolBackendKind.LOCAL, tmp_path), LocalToolBackend)


def test_cli_backend_selection_passes_mcp_backend_to_scan(tmp_path: Path, monkeypatch) -> None:
    sentinel = object()
    observed = {}

    monkeypatch.setattr(
        "repo_doctor.cli.create_tool_backend",
        lambda kind, root: observed.update(kind=kind, root=root) or sentinel,
    )

    def fake_scan(root, timeout, backend=None):
        observed["backend"] = backend
        return ScanResult(root.resolve(), [], 0, 0, [])

    monkeypatch.setattr("repo_doctor.cli.scan", fake_scan)

    response = CliRunner().invoke(
        app,
        ["scan", str(tmp_path), "--tool-backend", "mcp"],
    )

    assert response.exit_code == 0, response.output
    assert observed == {
        "kind": ToolBackendKind.MCP,
        "root": tmp_path,
        "backend": sentinel,
    }


def test_mcp_launch_configuration_injects_one_absolute_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    inherited = tmp_path / "wrong"
    monkeypatch.setenv("TOOLHUB_WORKSPACE_ROOT", str(inherited))
    client = FakeMCPClient()
    backend, captured = make_backend(tmp_path, client)
    process = captured["process"]

    assert Path(process.env["TOOLHUB_WORKSPACE_ROOT"]).is_absolute()
    assert Path(process.env["TOOLHUB_WORKSPACE_ROOT"]) == tmp_path.resolve()
    assert process.cwd.is_absolute()
    assert process.args == ("run", "server.py:mcp", "--transport", "stdio")
    backend.close()


def test_mcp_result_mapping_and_calls_never_include_a_root(tmp_path: Path) -> None:
    client = FakeMCPClient(
        {
            "filesystem.read_file": file_response(),
            "git.status": status_response(),
            "git.diff": diff_response(),
            "shell.run": shell_response(),
        }
    )
    backend, _ = make_backend(tmp_path, client)

    with backend:
        read = backend.read_file("app.py")
        status = backend.git_status()
        diff = backend.git_diff("app.py")
        command = backend.run_command("Python version", ("python", "--version"))

    assert read.content == "hello\n"
    assert read.sha256 == file_response()["sha256"]
    assert status.branch == "main"
    assert status.entries[0].path == "app.py"
    assert diff.additions == 1
    assert "+fixed" in diff.raw
    assert command.passed
    assert command.stdout == "Python 3.13\n"
    assert client.started and client.closed
    assert all(
        "root" not in arguments and "workspace_root" not in arguments
        for _, arguments in client.calls
    )


def test_pending_approval_is_structured_and_visible_in_report(tmp_path: Path) -> None:
    client = FakeMCPClient({"shell.run": shell_response(pending=True)})
    backend, _ = make_backend(tmp_path, client)

    with backend:
        result = backend.run_command("Python tests", ("pytest",))

    assert not result.passed
    assert result.approval_required
    assert result.request_id == "req_pending"
    assert result.approval_status == "PENDING"
    scan = ScanResult(tmp_path, ["Python"], 0, 0, [], commands=[result])
    report = render_report(scan)
    assert "APPROVAL REQUIRED" in report
    assert "req_pending" in report


def test_mcp_run_approved_calls_only_request_id_and_maps_real_result(tmp_path: Path) -> None:
    client = FakeMCPClient({"shell.run_approved": approved_shell_response()})
    backend, _ = make_backend(tmp_path, client)

    with backend:
        result = backend.run_approved("req_pending", name="Python tests")

    assert client.calls == [("shell.run_approved", {"request_id": "req_pending"})]
    assert result.passed
    assert result.executed
    assert result.command == ("pytest",)
    assert result.request_id == "req_pending"
    assert result.approval_status == "CONSUMED"
    assert result.stdout == "1 passed\n"


def test_mcp_scan_routes_reads_and_discovered_commands_through_backend(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Fixture\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    client = FakeMCPClient(
        {
            "filesystem.read_file": file_response("line\n"),
            "shell.run": shell_response(pending=True),
        }
    )
    backend, _ = make_backend(tmp_path, client)

    result = scan(tmp_path, backend=backend)

    names = [name for name, _ in client.calls]
    assert names.count("filesystem.read_file") == 3
    assert names.count("shell.run") == 2
    assert all(command.approval_required for command in result.commands)
    assert result.deterministic_score == 100
    assert all(arguments["cwd"] == "." for name, arguments in client.calls if name == "shell.run")
    assert client.closed


def test_local_and_mcp_scans_share_text_and_binary_eligibility(tmp_path: Path) -> None:
    text = tmp_path / "README.md"
    text.write_text("# Café\n", encoding="utf-8")
    binary = tmp_path / "image.png"
    binary.write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")

    class RecordingLocalBackend(LocalToolBackend):
        def __init__(self, root: Path):
            super().__init__(root)
            self.read_paths = []

        def read_file(self, path: str):
            self.read_paths.append(path)
            return super().read_file(path)

    local_backend = RecordingLocalBackend(tmp_path)
    local_result = scan(tmp_path, backend=local_backend)
    client = FakeMCPClient({"filesystem.read_file": file_response("# Café\n")})
    mcp_backend, _ = make_backend(tmp_path, client)
    mcp_result = scan(tmp_path, backend=mcp_backend)

    mcp_read_paths = [
        arguments["path"] for name, arguments in client.calls if name == "filesystem.read_file"
    ]
    assert local_backend.read_paths == ["README.md"]
    assert mcp_read_paths == ["README.md"]
    assert local_result.files == mcp_result.files == 2
    assert local_result.lines == mcp_result.lines == 1
    assert is_utf8_text_file(text)
    assert not is_utf8_text_file(binary)


def test_mcp_startup_failure_is_actionable_and_cleanup_is_attempted(tmp_path: Path) -> None:
    client = FakeMCPClient(startup_error=FileNotFoundError("mcp executable missing"))
    backend, _ = make_backend(tmp_path, client)

    with pytest.raises(ToolBackendStartupError, match="Could not start MCP ToolHub"):
        backend.read_file("app.py")

    assert client.closed


def test_mcp_call_failure_is_actionable(tmp_path: Path) -> None:
    client = FakeMCPClient(call_error=RuntimeError("connection closed"))
    backend, _ = make_backend(tmp_path, client)

    with (
        backend,
        pytest.raises(
            FileReadError,
            match=(
                "Could not read repository file 'app.py' via MCP ToolHub: "
                "ToolHub call filesystem.read_file failed: connection closed"
            ),
        ),
    ):
        backend.read_file("app.py")

    assert client.closed


def test_mcp_scan_does_not_mistake_transport_failure_for_binary(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Fixture\n", encoding="utf-8")
    client = FakeMCPClient(call_error=RuntimeError("connection closed"))
    backend, _ = make_backend(tmp_path, client)

    with pytest.raises(FileReadError, match="README.md.*connection closed"):
        scan(tmp_path, backend=backend)

    assert client.calls == [("filesystem.read_file", {"path": "README.md"})]
    assert client.closed


def test_mcp_workspace_rejection_remains_fatal_and_names_path(tmp_path: Path) -> None:
    client = FakeMCPClient(call_error=RuntimeError("workspace access denied"))
    backend, _ = make_backend(tmp_path, client)

    with (
        backend,
        pytest.raises(FileReadError, match="app.py.*workspace access denied"),
    ):
        backend.read_file("app.py")

    assert client.closed


def test_mcp_malformed_read_result_names_attempted_path(tmp_path: Path) -> None:
    client = FakeMCPClient({"filesystem.read_file": {"content": "hello"}})
    backend, _ = make_backend(tmp_path, client)

    with (
        backend,
        pytest.raises(FileReadError, match="invalid filesystem.read_file result.*app.py"),
    ):
        backend.read_file("app.py")

    assert client.closed


def test_mcp_context_closes_after_caller_exception(tmp_path: Path) -> None:
    client = FakeMCPClient()
    backend, _ = make_backend(tmp_path, client)

    with pytest.raises(RuntimeError, match="caller failed"):
        with backend:
            raise RuntimeError("caller failed")

    assert client.closed


def test_mcp_backend_rejects_absolute_or_traversing_call_paths(tmp_path: Path) -> None:
    client = FakeMCPClient()
    backend, _ = make_backend(tmp_path, client)

    with pytest.raises(ToolCallError, match="relative"):
        backend.read_file(str((tmp_path / "app.py").resolve()))
    with pytest.raises(ToolCallError, match="traverse"):
        backend.read_file("../app.py")

    assert client.calls == []


@pytest.mark.integration
def test_real_toolhub_read_and_git_round_trip(tmp_path: Path, monkeypatch) -> None:
    if os.environ.get("REPO_DOCTOR_RUN_TOOLHUB_INTEGRATION") != "1":
        pytest.skip("set REPO_DOCTOR_RUN_TOOLHUB_INTEGRATION=1 to run real ToolHub integration")
    toolhub = Path(r"D:\mcp-toolhub")
    executable = toolhub / ".venv" / "Scripts" / "mcp.exe"
    if not (toolhub / "server.py").is_file() or not executable.is_file():
        pytest.skip(r"real ToolHub is unavailable at D:\mcp-toolhub")
    try:
        import mcp  # noqa: F401
    except ImportError:
        pytest.skip("the Repo Doctor test interpreter does not have the MCP client installed")

    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    target = repository / "app.py"
    target.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "app.py"], check=True)
    target.write_text("before\nafter\n", encoding="utf-8")

    state = tmp_path / "toolhub-state"
    monkeypatch.setenv("TOOLHUB_APPROVAL_STORE", str(state / "approvals.json"))
    monkeypatch.setenv("TOOLHUB_AUDIT_PATH", str(state / "audit.jsonl"))

    with MCPToolBackend(repository, toolhub_project=toolhub) as backend:
        read = backend.read_file("app.py")
        status = backend.git_status()
        diff = backend.git_diff("app.py")

    assert read.content == "before\nafter\n"
    assert any(entry.path == "app.py" for entry in status.entries)
    assert "+after" in diff.raw


@pytest.mark.integration
def test_real_toolhub_approval_resume_and_replay_protection(tmp_path: Path, monkeypatch) -> None:
    if os.environ.get("REPO_DOCTOR_RUN_TOOLHUB_INTEGRATION") != "1":
        pytest.skip("set REPO_DOCTOR_RUN_TOOLHUB_INTEGRATION=1 to run real ToolHub integration")
    toolhub = Path(r"D:\mcp-toolhub")
    mcp_executable = toolhub / ".venv" / "Scripts" / "mcp.exe"
    admin_python = toolhub / ".venv" / "Scripts" / "python.exe"
    if not (toolhub / "server.py").is_file() or not all(
        path.is_file() for path in (mcp_executable, admin_python)
    ):
        pytest.skip(r"real ToolHub is unavailable at D:\mcp-toolhub")

    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "requirements.txt").write_text("", encoding="utf-8")
    tests = repository / "tests"
    tests.mkdir()
    (tests / "test_smoke.py").write_text(
        "def test_real_resume():\n    assert 2 + 2 == 4\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", str(repository)], check=True)

    state = tmp_path / "toolhub-state"
    approval_store = state / "approvals.json"
    monkeypatch.setenv("TOOLHUB_APPROVAL_STORE", str(approval_store))
    monkeypatch.setenv("TOOLHUB_AUDIT_PATH", str(state / "audit.jsonl"))
    monkeypatch.setenv("REPO_DOCTOR_STATE_ROOT", str(state / "repo-doctor-state"))

    monkeypatch.chdir(repository)
    runner = CliRunner()
    scan_response = runner.invoke(app, ["scan", ".", "--tool-backend", "mcp"])
    assert scan_response.exit_code == 0, scan_response.output
    assert "Approval required" in scan_response.output
    session_files = list((state / "repo-doctor-state" / "sessions").glob("*.json"))
    assert len(session_files) == 1
    assert not (repository / ".repo-doctor").exists()
    session = load_session_file(session_files[0])
    request_id = session.operations[0].request_id

    subprocess.run(
        [str(admin_python), "-m", "toolhub.admin", "approve", request_id],
        cwd=toolhub,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=True,
    )

    resume_response = runner.invoke(app, ["resume", session.session_id])
    assert resume_response.exit_code == 0, resume_response.output
    assert "Python tests: PASS" in resume_response.output
    assert "1 passed" in resume_response.output
    assert "All pending verification completed" in resume_response.output
    store = json.loads(approval_store.read_text(encoding="utf-8"))
    assert store["requests"][request_id]["status"] == "CONSUMED"

    repeated = runner.invoke(app, ["resume", session.session_id])
    assert repeated.exit_code == 0, repeated.output
    audit_events = [
        json.loads(line)
        for line in (state / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    executions = [
        event
        for event in audit_events
        if event.get("tool") == "shell.run_approved" and event.get("action") == "execute_approved"
    ]
    assert len(executions) == 1
    assert not any(thread.name == "repo-doctor-mcp" for thread in threading.enumerate())

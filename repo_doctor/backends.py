"""Root-bound local and MCP tool execution backends."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Protocol, Self

from .models import (
    CommandResult,
    FileReadResult,
    GitDiffResult,
    GitStatusEntry,
    GitStatusResult,
    PatchMutationResult,
)
from .runner import run_command as run_local_command
from .security import verification_environment

DEFAULT_TOOLHUB_PROJECT = Path(r"D:\mcp-toolhub")
MCP_CLEANUP_TIMEOUT_SECONDS = 10.0


class ToolBackendError(Exception):
    """Base class for actionable tool backend failures."""


class ToolBackendStartupError(ToolBackendError):
    """A tool backend could not initialize."""


class ToolCallError(ToolBackendError):
    """A backend tool call failed or returned an invalid result."""


class FileReadError(ToolCallError):
    """A text file selected by Repo Doctor could not be read."""


class MutationConflictError(ToolCallError):
    """ToolHub refused a mutation because its optimistic hash was stale."""


class ToolBackendKind(StrEnum):
    """CLI-selectable execution backends."""

    LOCAL = "local"
    MCP = "mcp"


class ToolBackend(Protocol):
    """Operations currently needed from Repo Doctor execution backends."""

    root: Path
    verification_in_place: bool

    def __enter__(self) -> Self: ...

    def __exit__(self, *exc_info: object) -> None: ...

    def read_file(self, path: str) -> FileReadResult: ...

    def run_command(
        self,
        name: str,
        command: tuple[str, ...],
        cwd: str = ".",
        timeout: int = 120,
    ) -> CommandResult: ...

    def git_status(self) -> GitStatusResult: ...

    def git_diff(self, path: str | None = None, staged: bool = False) -> GitDiffResult: ...

    def close(self) -> None: ...


def _canonical_repository(root: Path) -> Path:
    try:
        resolved = root.expanduser().resolve(strict=True)
    except OSError as error:
        raise ToolBackendStartupError(f"Repository path does not exist: {root}") from error
    if not resolved.is_dir():
        raise ToolBackendStartupError(f"Repository path is not a directory: {resolved}")
    return resolved


def _relative_path(value: str, *, label: str = "path") -> str:
    if not value or "\x00" in value:
        raise ToolCallError(f"Backend {label} must be a non-empty relative path.")
    if PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute():
        raise ToolCallError(f"Backend {label} must be relative to the configured repository.")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if any(part in {"", ".."} for part in parts):
        raise ToolCallError(f"Backend {label} cannot traverse outside the configured repository.")
    return PurePosixPath(*(part for part in parts if part != ".")).as_posix() or "."


def _resolve_local(root: Path, value: str) -> Path:
    relative = _relative_path(value)
    target = root.joinpath(*PurePosixPath(relative).parts).resolve()
    if not target.is_relative_to(root):
        raise ToolCallError("Backend path escapes the configured repository.")
    return target


class LocalToolBackend:
    """Adapter preserving Repo Doctor's existing local execution behavior."""

    verification_in_place = False

    def __init__(self, root: Path):
        self.root = _canonical_repository(root)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """The local backend owns no persistent resources."""

    def read_file(self, path: str) -> FileReadResult:
        relative = _relative_path(path)
        target = _resolve_local(self.root, relative)
        try:
            data = target.read_bytes()
            content = data.decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise FileReadError(
                f"Could not read repository file '{relative}' locally: {error}"
            ) from error
        return FileReadResult(
            relative,
            len(data),
            hashlib.sha256(data).hexdigest(),
            content,
        )

    def run_command(
        self,
        name: str,
        command: tuple[str, ...],
        cwd: str = ".",
        timeout: int = 120,
    ) -> CommandResult:
        if not command:
            raise ToolCallError("Backend command cannot be empty.")
        working_directory = _resolve_local(self.root, cwd)
        if not working_directory.is_dir():
            raise ToolCallError(f"Backend working directory is not a directory: {cwd}")
        return run_local_command(name, command, working_directory, timeout)

    def git_status(self) -> GitStatusResult:
        completed = self._git("status", "--porcelain=v1", "--branch")
        branch, entries = _parse_git_status(completed.stdout)
        return GitStatusResult(".", branch, not entries, entries, completed.stdout)

    def git_diff(self, path: str | None = None, staged: bool = False) -> GitDiffResult:
        arguments = ["diff", "--no-ext-diff", "--no-textconv"]
        if staged:
            arguments.append("--cached")
        normalized_path = None
        if path is not None:
            normalized_path = _relative_path(path)
            arguments.extend(("--", normalized_path))
        completed = self._git(*arguments)
        additions, deletions, binary = _count_git_diff(completed.stdout)
        return GitDiffResult(
            normalized_path,
            staged,
            additions,
            deletions,
            binary,
            completed.stdout,
        )

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=self.root,
                env=verification_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ToolCallError(f"Local Git operation failed: {error}") from error
        if completed.returncode:
            detail = completed.stderr.strip() or "git failed"
            raise ToolCallError(f"Local Git operation failed: {detail}")
        return completed


@dataclass(frozen=True)
class MCPServerProcess:
    """Complete stdio launch configuration for ToolHub."""

    command: str
    args: tuple[str, ...]
    cwd: Path
    env: dict[str, str]


class MCPClient(Protocol):
    """Transport seam used by MCPToolBackend and unit-test fakes."""

    def start(self) -> None: ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...

    def close(self) -> None: ...


@dataclass
class _WorkerRequest:
    name: str | None
    arguments: dict[str, Any]
    response: queue.Queue[object]


class _StdioMCPClient:
    """Persistent official MCP client session hosted by one worker task."""

    def __init__(self, process: MCPServerProcess):
        self.process = process
        self._requests: queue.Queue[_WorkerRequest] = queue.Queue()
        self._startup: queue.Queue[object] = queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._thread_main,
            name="repo-doctor-mcp",
            daemon=True,
        )
        self._thread.start()
        outcome = self._startup.get(timeout=MCP_CLEANUP_TIMEOUT_SECONDS)
        if isinstance(outcome, BaseException):
            self._thread.join(timeout=MCP_CLEANUP_TIMEOUT_SECONDS)
            raise outcome

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._thread is None or not self._thread.is_alive():
            raise RuntimeError("MCP client session is not running")
        response: queue.Queue[object] = queue.Queue(maxsize=1)
        self._requests.put(_WorkerRequest(name, dict(arguments), response))
        outcome = response.get()
        if isinstance(outcome, BaseException):
            raise outcome
        if not isinstance(outcome, dict):
            raise RuntimeError("MCP tool returned an unexpected result")
        return outcome

    def close(self) -> None:
        thread = self._thread
        if thread is None:
            return
        if thread.is_alive():
            self._requests.put(_WorkerRequest(None, {}, queue.Queue(maxsize=1)))
            thread.join(timeout=MCP_CLEANUP_TIMEOUT_SECONDS)
        self._thread = None
        if thread.is_alive():
            raise RuntimeError("MCP ToolHub subprocess did not shut down cleanly")

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as error:
            if self._startup.empty():
                self._startup.put(error)

    async def _run(self) -> None:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as error:
            raise RuntimeError(
                "MCP backend requires the 'mcp' Python package; reinstall Repo Doctor."
            ) from error

        parameters = StdioServerParameters(
            command=self.process.command,
            args=list(self.process.args),
            cwd=self.process.cwd,
            env=self.process.env,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                self._startup.put(None)
                while True:
                    request = await asyncio.to_thread(self._requests.get)
                    if request.name is None:
                        break
                    try:
                        result = await session.call_tool(request.name, request.arguments)
                        request.response.put(_tool_payload(result))
                    except BaseException as error:
                        request.response.put(error)


def _tool_payload(result: Any) -> dict[str, Any]:
    if getattr(result, "is_error", False):
        detail = _tool_text(result) or "ToolHub reported an MCP tool error."
        raise RuntimeError(detail)
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    text = _tool_text(result)
    try:
        decoded = json.loads(text)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("ToolHub returned neither structured output nor JSON text.") from error
    if not isinstance(decoded, dict):
        raise RuntimeError("ToolHub returned a non-object tool result.")
    return decoded


def _tool_text(result: Any) -> str:
    return "\n".join(
        item.text
        for item in getattr(result, "content", ())
        if getattr(item, "type", None) == "text" and isinstance(getattr(item, "text", None), str)
    )


def _toolhub_process(root: Path, toolhub_project: Path) -> MCPServerProcess:
    project = toolhub_project.expanduser().resolve()
    if os.name == "nt":
        bundled = project / ".venv" / "Scripts" / "mcp.exe"
    else:
        bundled = project / ".venv" / "bin" / "mcp"
    command = str(bundled) if bundled.is_file() else "mcp"
    environment = verification_environment()
    environment["TOOLHUB_WORKSPACE_ROOT"] = str(root)
    return MCPServerProcess(
        command=command,
        args=("run", "server.py:mcp", "--transport", "stdio"),
        cwd=project,
        env=environment,
    )


class MCPToolBackend:
    """Root-bound Repo Doctor adapter for MCP ToolHub over stdio."""

    verification_in_place = True

    def __init__(
        self,
        root: Path,
        *,
        toolhub_project: Path = DEFAULT_TOOLHUB_PROJECT,
        client_factory: Callable[[MCPServerProcess], MCPClient] = _StdioMCPClient,
    ):
        self.root = _canonical_repository(root)
        self.server_process = _toolhub_process(self.root, toolhub_project)
        self._client = client_factory(self.server_process)
        self._started = False

    def __enter__(self) -> Self:
        self._start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _start(self) -> None:
        if self._started:
            return
        try:
            self._client.start()
        except Exception as error:
            try:
                self._client.close()
            except Exception:
                pass
            raise ToolBackendStartupError(f"Could not start MCP ToolHub: {error}") from error
        self._started = True

    def close(self) -> None:
        try:
            self._client.close()
        except Exception as error:
            raise ToolBackendError(f"Could not cleanly stop MCP ToolHub: {error}") from error
        finally:
            self._started = False

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._start()
        try:
            return self._client.call_tool(name, arguments)
        except ToolBackendError:
            raise
        except Exception as error:
            raise ToolCallError(f"ToolHub call {name} failed: {error}") from error

    def read_file(self, path: str) -> FileReadResult:
        relative = _relative_path(path)
        try:
            self._start()
        except ToolBackendStartupError as error:
            raise ToolBackendStartupError(
                f"{error} (while preparing to read '{relative}')"
            ) from error
        try:
            payload = self._call("filesystem.read_file", {"path": relative})
        except ToolCallError as error:
            raise FileReadError(
                f"Could not read repository file '{relative}' via MCP ToolHub: {error}"
            ) from error
        try:
            return FileReadResult(
                path=str(payload["path"]),
                size=int(payload["size"]),
                sha256=str(payload["sha256"]),
                content=str(payload["content"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise FileReadError(
                f"ToolHub returned an invalid filesystem.read_file result for '{relative}'."
            ) from error

    def run_command(
        self,
        name: str,
        command: tuple[str, ...],
        cwd: str = ".",
        timeout: int = 120,
    ) -> CommandResult:
        if not command:
            raise ToolCallError("Backend command cannot be empty.")
        relative_cwd = _relative_path(cwd, label="working directory")
        started = time.monotonic()
        payload = self._call(
            "shell.run",
            {
                "program": command[0],
                "args": list(command[1:]),
                "cwd": relative_cwd,
                "timeout_seconds": timeout,
            },
        )
        return _shell_command_result(payload, name, command, started)

    def run_approved(self, request_id: str, *, name: str = "Verification") -> CommandResult:
        """Ask ToolHub to execute one already-approved, immutable shell request."""
        if not request_id or "\x00" in request_id:
            raise ToolCallError("ToolHub approval request ID must be non-empty.")
        started = time.monotonic()
        payload = self._call("shell.run_approved", {"request_id": request_id})
        return _shell_command_result(
            payload,
            name,
            (),
            started,
            fallback_request_id=request_id,
        )

    def apply_patch(
        self,
        path: str,
        patch: str,
        expected_hash: str,
    ) -> PatchMutationResult:
        """Submit one validated patch with mandatory optimistic concurrency."""
        relative = _relative_path(path)
        _sha256(expected_hash)
        if not patch or "\x00" in patch:
            raise ToolCallError("Backend patch must be non-empty text without null bytes.")
        try:
            payload = self._call(
                "filesystem.apply_patch",
                {
                    "path": relative,
                    "patch": patch,
                    "expected_hash": expected_hash,
                },
            )
        except ToolCallError as error:
            if "conflict:" in str(error).casefold():
                raise MutationConflictError(str(error)) from error
            raise
        return _patch_mutation_result(payload, fallback_path=relative)

    def run_approved_mutation(self, request_id: str) -> PatchMutationResult:
        """Execute only the immutable mutation snapshot stored by ToolHub."""
        _approval_request_id(request_id)
        try:
            payload = self._call(
                "filesystem.apply_patch_approved",
                {"request_id": request_id},
            )
        except ToolCallError as error:
            if "conflict:" in str(error).casefold():
                raise MutationConflictError(str(error)) from error
            raise
        return _patch_mutation_result(
            payload,
            fallback_path="",
            fallback_request_id=request_id,
        )

    def git_status(self) -> GitStatusResult:
        payload = self._call("git.status", {})
        try:
            entries = tuple(
                GitStatusEntry(code=str(item["code"]), path=str(item["path"]))
                for item in payload["entries"]
            )
            return GitStatusResult(
                path=str(payload["path"]),
                branch=payload.get("branch"),
                clean=bool(payload["clean"]),
                entries=entries,
                raw=str(payload["raw"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ToolCallError("ToolHub returned an invalid git.status result.") from error

    def git_diff(self, path: str | None = None, staged: bool = False) -> GitDiffResult:
        normalized_path = _relative_path(path) if path is not None else None
        payload = self._call("git.diff", {"path": normalized_path, "staged": staged})
        try:
            return GitDiffResult(
                path=payload.get("path"),
                staged=bool(payload["staged"]),
                additions=payload.get("additions"),
                deletions=payload.get("deletions"),
                binary=bool(payload["binary"]),
                raw=str(payload["raw"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ToolCallError("ToolHub returned an invalid git.diff result.") from error


def _shell_command_result(
    payload: dict[str, Any],
    name: str,
    fallback_command: tuple[str, ...],
    started: float,
    *,
    fallback_request_id: str | None = None,
) -> CommandResult:
    """Validate and map a ToolHub shell response into Repo Doctor's model."""
    try:
        executed = payload["executed"]
        if not isinstance(executed, bool):
            raise TypeError("executed must be a boolean")
        timed_out = payload.get("timed_out", False)
        if not isinstance(timed_out, bool):
            raise TypeError("timed_out must be a boolean")
        program = payload.get("program", "")
        arguments = payload.get("args", [])
        if (
            not isinstance(program, str)
            or not isinstance(arguments, list)
            or not all(isinstance(item, str) for item in arguments)
        ):
            raise TypeError("program and args must describe a command")
        command = (program, *arguments) if program else fallback_command
        if executed:
            returncode = payload.get("returncode")
            if returncode is None and timed_out:
                exit_code = 124
            else:
                exit_code = int(returncode)
        else:
            exit_code = 126
        approval_status = payload.get("approval_status")
        if approval_status is not None and not isinstance(approval_status, str):
            raise TypeError("approval_status must be text or null")
        request_id = payload.get("request_id", fallback_request_id)
        if request_id is not None and not isinstance(request_id, str):
            raise TypeError("request_id must be text or null")
        trace_id = payload.get("trace_id")
        if trace_id is not None and not isinstance(trace_id, str):
            raise TypeError("trace_id must be text or null")
        return CommandResult(
            name=name,
            command=command,
            exit_code=exit_code,
            stdout=str(payload.get("stdout", "")),
            stderr=str(payload.get("stderr", "")),
            duration=time.monotonic() - started,
            timed_out=timed_out,
            approval_required=not executed and approval_status == "PENDING",
            request_id=request_id or fallback_request_id,
            approval_status=approval_status,
            message=str(payload.get("message", "")),
            executed=executed,
            trace_id=trace_id,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ToolCallError("ToolHub returned an invalid shell result.") from error


def _approval_request_id(request_id: str) -> str:
    if (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > 512
        or any(ord(character) < 32 for character in request_id)
    ):
        raise ToolCallError("ToolHub approval request ID must be non-empty bounded text.")
    return request_id


def _sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ToolCallError("Expected hash must be a lowercase SHA-256 digest.")
    return value


def _patch_mutation_result(
    payload: dict[str, Any],
    *,
    fallback_path: str,
    fallback_request_id: str | None = None,
) -> PatchMutationResult:
    """Validate and map a ToolHub filesystem mutation response."""
    try:
        executed = payload["executed"]
        if not isinstance(executed, bool):
            raise TypeError("executed must be a boolean")
        changed = payload.get("changed", False)
        if not isinstance(changed, bool):
            raise TypeError("changed must be a boolean")
        path = payload.get("path", fallback_path)
        if not isinstance(path, str):
            raise TypeError("path must be text")
        request_id = payload.get("request_id", fallback_request_id)
        if request_id is not None and not isinstance(request_id, str):
            raise TypeError("request_id must be text or null")
        approval_status = payload.get("approval_status")
        if approval_status is not None and not isinstance(approval_status, str):
            raise TypeError("approval_status must be text or null")
        trace_id = payload.get("trace_id")
        if trace_id is not None and not isinstance(trace_id, str):
            raise TypeError("trace_id must be text or null")
        previous_hash = payload.get("previous_hash")
        new_hash = payload.get("new_hash")
        if previous_hash is not None:
            _sha256(str(previous_hash))
        if new_hash is not None:
            _sha256(str(new_hash))
        return PatchMutationResult(
            path=path or fallback_path,
            executed=executed,
            changed=changed,
            additions=int(payload.get("additions", 0)),
            deletions=int(payload.get("deletions", 0)),
            bytes_before=int(payload.get("bytes_before", 0)),
            bytes_after=int(payload.get("bytes_after", 0)),
            previous_hash=str(previous_hash) if previous_hash is not None else None,
            new_hash=str(new_hash) if new_hash is not None else None,
            trace_id=trace_id,
            request_id=request_id or fallback_request_id,
            approval_status=approval_status,
            message=str(payload.get("message", "")),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ToolCallError("ToolHub returned an invalid filesystem patch result.") from error


def create_tool_backend(kind: ToolBackendKind, root: Path) -> ToolBackend:
    """Create the selected root-bound backend; local remains the default in CLI callers."""

    if kind is ToolBackendKind.LOCAL:
        return LocalToolBackend(root)
    if kind is ToolBackendKind.MCP:
        return MCPToolBackend(root)
    raise ToolBackendStartupError(f"Unsupported tool backend: {kind}")


def _parse_git_status(raw: str) -> tuple[str | None, tuple[GitStatusEntry, ...]]:
    branch = None
    entries: list[GitStatusEntry] = []
    for line in raw.splitlines():
        if line.startswith("## "):
            branch = line[3:].split("...", 1)[0].split(" [", 1)[0].strip()
        elif len(line) >= 3:
            entries.append(GitStatusEntry(line[:2], line[3:].strip()))
    return branch, tuple(entries)


def _count_git_diff(raw: str) -> tuple[int | None, int | None, bool]:
    if "Binary files" in raw:
        return None, None, True
    additions = sum(
        1 for line in raw.splitlines() if line.startswith("+") and not line.startswith("+++")
    )
    deletions = sum(
        1 for line in raw.splitlines() if line.startswith("-") and not line.startswith("---")
    )
    return additions, deletions, False

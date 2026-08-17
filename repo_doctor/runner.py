"""Safe subprocess execution."""

import os
import subprocess
import time
from pathlib import Path

from .models import CommandResult


def run_command(
    name: str, command: tuple[str, ...], cwd: Path, timeout: int = 120
) -> CommandResult:
    """Run an argument-vector command without a shell and capture all output."""
    started = time.monotonic()
    env = {**os.environ, "CI": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        process = subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
        )
        return CommandResult(
            name,
            command,
            process.returncode,
            process.stdout,
            process.stderr,
            time.monotonic() - started,
        )
    except FileNotFoundError:
        return CommandResult(
            name, command, 127, "", f"Command not found: {command[0]}", time.monotonic() - started
        )
    except subprocess.TimeoutExpired as error:
        return CommandResult(
            name,
            command,
            124,
            error.stdout or "",
            error.stderr or "",
            time.monotonic() - started,
            True,
        )

"""Project type and verification command discovery."""

import json
import os
from pathlib import Path


def detect_technologies(root: Path) -> list[str]:
    """Return technologies indicated by conventional manifests."""
    found: list[str] = []
    if any((root / name).exists() for name in ("pyproject.toml", "requirements.txt", "setup.py")):
        found.append("Python")
    if (root / "package.json").exists():
        found.append("Node.js")
    return found


def discover_commands(root: Path, technologies: list[str]) -> list[tuple[str, tuple[str, ...]]]:
    """Discover only commands supported by project configuration."""
    commands: list[tuple[str, tuple[str, ...]]] = []
    if "Python" in technologies:
        if (root / "tests").is_dir() or any(root.glob("test_*.py")):
            commands.append(("Python tests", ("pytest",)))
        if (root / "pyproject.toml").exists() or (root / "ruff.toml").exists():
            commands.append(("Python lint", ("ruff", "check", ".")))
    package = root / "package.json"
    if "Node.js" in technologies and package.exists():
        try:
            manifest = json.loads(package.read_text(encoding="utf-8"))
            scripts = manifest.get("scripts", {}) if isinstance(manifest, dict) else {}
            if not isinstance(scripts, dict):
                scripts = {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            scripts = {}
        npm = "npm.cmd" if os.name == "nt" else "npm"
        if "test" in scripts:
            commands.append(("Node tests", (npm, "test")))
        if "lint" in scripts:
            commands.append(("Node lint", (npm, "run", "lint")))
    return commands

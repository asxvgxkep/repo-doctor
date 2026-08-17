"""Shared path and secret filtering rules for untrusted AI data."""

from pathlib import PurePosixPath, PureWindowsPath

EXCLUDED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "coverage",
    "htmlcov",
}


def normalize_relative_path(value: str) -> str:
    """Return a safe POSIX-style repository-relative path or raise ValueError."""
    if not value or "\x00" in value:
        raise ValueError("path must be a non-empty relative path")
    if PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute():
        raise ValueError("absolute paths are not allowed")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("path traversal and empty path segments are not allowed")
    if any(":" in part for part in parts):
        raise ValueError("drive-qualified paths are not allowed")
    return PurePosixPath(*parts).as_posix()


def is_secret_path(value: str) -> bool:
    """Return whether a relative path is likely to contain credentials."""
    try:
        path = PurePosixPath(normalize_relative_path(value))
    except ValueError:
        return True
    name = path.name.lower()
    return (
        name == ".env"
        or name.startswith(".env.")
        or name in {".netrc", ".npmrc", ".pypirc"}
        or name.startswith(("id_rsa", "id_ed25519"))
        or name.endswith((".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"))
        or name.startswith("credentials")
        or name.startswith("secrets")
    )


def has_excluded_directory(value: str) -> bool:
    """Return whether any path component belongs to ignored tool/generated data."""
    try:
        parts = PurePosixPath(normalize_relative_path(value)).parts
    except ValueError:
        return True
    return bool({part.lower() for part in parts[:-1]} & EXCLUDED_DIRECTORIES)

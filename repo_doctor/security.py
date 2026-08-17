"""Small secret-handling helpers shared by reports and AI requests."""

from __future__ import annotations

import os
import re

PRIVATE_PROVIDER_VARIABLES = {"REPO_DOCTOR_API_KEY"}
MIN_GLOBAL_SECRET_LENGTH = 8
SENSITIVE_NAME = re.compile(r"(?i)(api[_-]?key|token|password|passwd|secret|authorization)")
ASSIGNMENT = re.compile(
    r"(?im)^(\s*(?:[a-z0-9_-]*(?:api[_-]?key|token|password|passwd|secret)|authorization)\s*[:=]\s*)([^\r\n]+)(\r?)$"
)
BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")


def verification_environment() -> dict[str, str]:
    """Build a verification environment without credential-like variables."""
    env = dict(os.environ)
    for name in list(env):
        if name in PRIVATE_PROVIDER_VARIABLES or SENSITIVE_NAME.search(name):
            env.pop(name)
    env.update({"CI": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def redact_sensitive_text(value: str) -> str:
    """Redact known secret values and conventional inline secret assignments."""
    redacted = value
    for name, secret in os.environ.items():
        if SENSITIVE_NAME.search(name) and len(secret) >= MIN_GLOBAL_SECRET_LENGTH:
            secret_pattern = re.compile(rf"(?<!\w){re.escape(secret)}(?!\w)")
            redacted = secret_pattern.sub("[REDACTED]", redacted)
    redacted = ASSIGNMENT.sub(r"\1[REDACTED]\3", redacted)
    return BEARER.sub(r"\1[REDACTED]", redacted)

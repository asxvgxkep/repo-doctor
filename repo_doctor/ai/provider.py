"""Vendor-neutral provider contract and environment configuration."""

from __future__ import annotations

import os
from math import isfinite
from typing import Protocol

from .errors import AIConfigurationError
from .models import AnalysisRequest, AnalysisResponse, PatchProposal, PatchRequest
from .prompts import DEFAULT_PROMPT_VARIANT, prompt_profile


class LLMProvider(Protocol):
    """Minimum behavior needed by Repo Doctor's AI workflow."""

    def analyze(self, request: AnalysisRequest) -> AnalysisResponse:
        """Return validated semantic findings for selected context."""
        ...

    def generate_patch(self, request: PatchRequest) -> PatchProposal:
        """Return one validated, constrained replacement."""
        ...


def provider_from_env(
    prompt_variant: str = DEFAULT_PROMPT_VARIANT,
) -> LLMProvider:
    """Build the OpenAI-compatible provider without exposing configuration values."""
    prompt_profile(prompt_variant)
    names = ("REPO_DOCTOR_API_KEY", "REPO_DOCTOR_BASE_URL", "REPO_DOCTOR_MODEL")
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name in names if not values[name]]
    if missing:
        raise AIConfigurationError(
            "AI analysis requested, but required configuration is missing: "
            + ", ".join(missing)
            + "."
        )
    from .openai_compatible import DEFAULT_REQUEST_TIMEOUT, OpenAICompatibleProvider

    raw_timeout = os.environ.get("REPO_DOCTOR_REQUEST_TIMEOUT")
    timeout = DEFAULT_REQUEST_TIMEOUT
    if raw_timeout is not None:
        try:
            timeout = float(raw_timeout.strip())
        except ValueError as error:
            raise AIConfigurationError(
                "REPO_DOCTOR_REQUEST_TIMEOUT must be a positive finite number of seconds."
            ) from error
        if not isfinite(timeout) or timeout <= 0:
            raise AIConfigurationError(
                "REPO_DOCTOR_REQUEST_TIMEOUT must be a positive finite number of seconds."
            )

    return OpenAICompatibleProvider(
        api_key=values["REPO_DOCTOR_API_KEY"],
        base_url=values["REPO_DOCTOR_BASE_URL"],
        model=values["REPO_DOCTOR_MODEL"],
        timeout=timeout,
        prompt_variant=prompt_variant,
    )

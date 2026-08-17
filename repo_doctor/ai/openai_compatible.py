"""OpenAI-compatible chat-completions HTTP provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ProviderError, ResponseValidationError
from .models import AnalysisRequest, AnalysisResponse, PatchProposal, PatchRequest
from .parser import parse_analysis_response, parse_patch_response
from .prompts import analysis_messages, patch_messages

MAX_RESPONSE_BYTES = 2_000_000
DEFAULT_REQUEST_TIMEOUT = 180.0


@dataclass(frozen=True)
class OpenAICompatibleProvider:
    """Call a configured endpoint without hardcoding vendor or credentials."""

    api_key: str = field(repr=False)
    base_url: str
    model: str
    timeout: float = DEFAULT_REQUEST_TIMEOUT
    transport: Any = None

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else f"{base}/chat/completions"

    def _complete(self, messages: list[dict[str, str]]) -> str:
        try:
            import httpx
        except ImportError as error:
            raise ProviderError(
                "AI support requires the project dependency 'httpx'; reinstall Repo Doctor."
            ) from error

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        try:
            with httpx.Client(
                timeout=self.timeout, follow_redirects=False, transport=self.transport
            ) as client:
                response = client.post(self.endpoint, headers=headers, json=payload)
        except httpx.TimeoutException as error:
            raise ProviderError("AI provider request timed out.") from error
        except httpx.HTTPError as error:
            raise ProviderError("AI provider request failed due to a network error.") from error
        if response.status_code == 429:
            raise ProviderError("AI provider rate limit was reached; retry later.")
        if response.status_code >= 400:
            raise ProviderError(f"AI provider returned HTTP {response.status_code}.")
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ResponseValidationError("AI provider response exceeded the safe size limit.")
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise ResponseValidationError(
                "AI provider returned an unexpected response envelope."
            ) from error
        if not isinstance(content, str):
            raise ResponseValidationError("AI provider response content was not JSON text.")
        return content

    def analyze(self, request: AnalysisRequest) -> AnalysisResponse:
        return parse_analysis_response(self._complete(analysis_messages(request)))

    def generate_patch(self, request: PatchRequest) -> PatchProposal:
        return parse_patch_response(self._complete(patch_messages(request)))

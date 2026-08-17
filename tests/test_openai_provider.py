import json

import httpx
import pytest

from repo_doctor.ai.errors import ProviderError, ResponseValidationError
from repo_doctor.ai.models import AnalysisRequest
from repo_doctor.ai.openai_compatible import (
    DEFAULT_REQUEST_TIMEOUT,
    MAX_RESPONSE_BYTES,
    OpenAICompatibleProvider,
)


def request() -> AnalysisRequest:
    return AnalysisRequest("fixture", ("Python",), 1, 1, (), (), ())


def response_content() -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "id": "one",
                    "title": "Boundary bug",
                    "category": "control-flow",
                    "severity": "high",
                    "confidence": 0.9,
                    "file": "app.py",
                    "line_start": 1,
                    "line_end": 1,
                    "explanation": "Equality fails.",
                    "evidence": "The comparison is strict.",
                    "suggested_fix": "Accept equality.",
                }
            ]
        }
    )


def test_openai_compatible_provider_default_timeout_is_180_seconds() -> None:
    provider = OpenAICompatibleProvider("key", "https://provider.invalid/v1", "model")
    assert provider.timeout == DEFAULT_REQUEST_TIMEOUT == 180.0


def test_openai_compatible_provider_sends_authorization_and_parses_json() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.url == "https://provider.invalid/v1/chat/completions"
        assert http_request.headers["authorization"] == "Bearer test-key"
        payload = json.loads(http_request.content)
        assert payload["model"] == "test-model"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": response_content()}}]},
        )

    provider = OpenAICompatibleProvider(
        "test-key",
        "https://provider.invalid/v1",
        "test-model",
        transport=httpx.MockTransport(handler),
    )
    result = provider.analyze(request())
    assert result.findings[0].id == "one"


@pytest.mark.parametrize(
    ("status", "message"),
    [(429, "rate limit"), (500, "HTTP 500")],
)
def test_provider_http_failures_are_actionable(status: int, message: str) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(status))
    provider = OpenAICompatibleProvider(
        "not-printed", "https://provider.invalid/v1", "model", transport=transport
    )
    with pytest.raises(ProviderError, match=message) as captured:
        provider.analyze(request())
    assert "not-printed" not in str(captured.value)


def test_provider_timeout_is_actionable() -> None:
    def timeout(http_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=http_request)

    provider = OpenAICompatibleProvider(
        "key",
        "https://provider.invalid/v1",
        "model",
        transport=httpx.MockTransport(timeout),
    )
    with pytest.raises(ProviderError, match="timed out"):
        provider.analyze(request())


def test_provider_rejects_unexpected_envelope() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"unexpected": True}))
    provider = OpenAICompatibleProvider(
        "key", "https://provider.invalid/v1", "model", transport=transport
    )
    with pytest.raises(ResponseValidationError, match="unexpected response envelope"):
        provider.analyze(request())


def test_provider_rejects_oversized_response() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
    )
    provider = OpenAICompatibleProvider(
        "key", "https://provider.invalid/v1", "model", transport=transport
    )
    with pytest.raises(ResponseValidationError, match="safe size limit"):
        provider.analyze(request())

import json

import pytest

from repo_doctor.ai.errors import AIConfigurationError, ResponseValidationError
from repo_doctor.ai.models import Severity
from repo_doctor.ai.parser import parse_analysis_response, parse_patch_response
from repo_doctor.ai.provider import provider_from_env

PROVIDER_ENV = {
    "REPO_DOCTOR_API_KEY": "test-key",
    "REPO_DOCTOR_BASE_URL": "https://provider.invalid/v1",
    "REPO_DOCTOR_MODEL": "test-model",
}


def configure_provider(monkeypatch) -> None:
    for name, value in PROVIDER_ENV.items():
        monkeypatch.setenv(name, value)


def valid_finding(**overrides):
    finding = {
        "id": "boundary-1",
        "title": "Exact stock cannot be fulfilled",
        "category": "control-flow",
        "severity": "high",
        "confidence": 0.93,
        "file": "inventory.py",
        "line_start": 6,
        "line_end": 6,
        "explanation": "Equality should be accepted.",
        "evidence": "requested < stock rejects equal values.",
        "suggested_fix": "Use <= for the boundary.",
    }
    finding.update(overrides)
    return finding


def test_valid_structured_response_parsing() -> None:
    response = parse_analysis_response(json.dumps({"findings": [valid_finding()]}))
    assert response.findings[0].severity is Severity.HIGH
    assert response.findings[0].confidence == 0.93


def test_analysis_response_parses_optional_behavioral_contract() -> None:
    response = parse_analysis_response(
        json.dumps(
            {
                "findings": [valid_finding()],
                "behavioral_contract": {
                    "must_fix": ["Accept equality."],
                    "must_preserve": ["Keep smaller requests valid."],
                    "evidence": ["The boundary test fails."],
                    "rationale": "One edit must satisfy both behaviors.",
                },
            }
        )
    )

    assert response.behavioral_contract is not None
    assert response.behavioral_contract.must_fix == ("Accept equality.",)
    assert response.behavioral_contract.must_preserve == ("Keep smaller requests valid.",)


def test_malformed_json_is_rejected() -> None:
    with pytest.raises(ResponseValidationError, match="malformed JSON"):
        parse_analysis_response("not json")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [("severity", "urgent", "severity"), ("confidence", 1.2, "confidence")],
)
def test_invalid_finding_values_are_rejected(field: str, value, message: str) -> None:
    with pytest.raises(ResponseValidationError, match=message):
        parse_analysis_response(json.dumps({"findings": [valid_finding(**{field: value})]}))


def test_missing_fields_are_not_invented() -> None:
    finding = valid_finding()
    finding.pop("evidence")
    with pytest.raises(ResponseValidationError, match="missing fields: evidence"):
        parse_analysis_response(json.dumps({"findings": [finding]}))


def test_unsafe_control_characters_are_rejected() -> None:
    with pytest.raises(ResponseValidationError, match="control characters"):
        parse_analysis_response(
            json.dumps({"findings": [valid_finding(explanation="unsafe\u001b[31m text")]})
        )


def test_patch_path_traversal_is_rejected() -> None:
    payload = {
        "file": "../../outside.py",
        "old_text": "before",
        "new_text": "after",
        "reason": "test",
        "confidence": 0.95,
    }
    with pytest.raises(ResponseValidationError, match="unsafe"):
        parse_patch_response(json.dumps(payload))


def test_missing_provider_configuration_is_actionable(monkeypatch) -> None:
    for name in ("REPO_DOCTOR_API_KEY", "REPO_DOCTOR_BASE_URL", "REPO_DOCTOR_MODEL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(AIConfigurationError) as captured:
        provider_from_env()
    message = str(captured.value)
    assert "REPO_DOCTOR_API_KEY" in message
    assert "REPO_DOCTOR_BASE_URL" in message
    assert "REPO_DOCTOR_MODEL" in message


def test_provider_configuration_does_not_expose_api_key(monkeypatch) -> None:
    configure_provider(monkeypatch)
    monkeypatch.setenv("REPO_DOCTOR_API_KEY", "super-secret-value")
    monkeypatch.delenv("REPO_DOCTOR_REQUEST_TIMEOUT", raising=False)
    provider = provider_from_env()
    assert "super-secret-value" not in repr(provider)


def test_provider_request_timeout_defaults_to_180_seconds(monkeypatch) -> None:
    configure_provider(monkeypatch)
    monkeypatch.delenv("REPO_DOCTOR_REQUEST_TIMEOUT", raising=False)
    assert provider_from_env().timeout == 180.0


def test_provider_request_timeout_accepts_custom_positive_seconds(monkeypatch) -> None:
    configure_provider(monkeypatch)
    monkeypatch.setenv("REPO_DOCTOR_REQUEST_TIMEOUT", "240.5")
    assert provider_from_env().timeout == 240.5


@pytest.mark.parametrize("value", ["", "not-a-number", "nan", "inf", "-inf"])
def test_provider_request_timeout_rejects_malformed_or_nonfinite_values(
    monkeypatch, value: str
) -> None:
    configure_provider(monkeypatch)
    monkeypatch.setenv("REPO_DOCTOR_REQUEST_TIMEOUT", value)
    with pytest.raises(AIConfigurationError, match="positive finite number of seconds"):
        provider_from_env()


@pytest.mark.parametrize("value", ["0", "-1", "-0.25"])
def test_provider_request_timeout_rejects_zero_or_negative_values(monkeypatch, value: str) -> None:
    configure_provider(monkeypatch)
    monkeypatch.setenv("REPO_DOCTOR_REQUEST_TIMEOUT", value)
    with pytest.raises(AIConfigurationError, match="positive finite number of seconds"):
        provider_from_env()

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from repo_doctor.ai.models import (
    AnalysisRequest,
    FileContext,
    PatchRequest,
    SemanticFinding,
    Severity,
)
from repo_doctor.ai.openai_compatible import OpenAICompatibleProvider
from repo_doctor.ai.prompts import (
    ANALYSIS_SYSTEM_PROMPT,
    DEFAULT_PROMPT_VARIANT,
    PATCH_SYSTEM_PROMPT,
    PROMPT_VARIANTS,
    analysis_messages,
    patch_messages,
)
from repo_doctor.ai.provider import provider_from_env
from repo_doctor.cli import app


def analysis_request() -> AnalysisRequest:
    return AnalysisRequest(
        repository_name="fixture",
        technologies=("Python",),
        file_count=1,
        line_count=2,
        verifications=(),
        deterministic_findings=(),
        files=(FileContext("parser.py", "def parse(value):\n    return value\n", "hash"),),
    )


def patch_request() -> PatchRequest:
    context = FileContext("parser.py", "return value\n", "hash")
    finding = SemanticFinding(
        id="finding-1",
        title="Boundary behavior",
        category="correctness",
        severity=Severity.HIGH,
        confidence=0.95,
        file="parser.py",
        line_start=1,
        line_end=1,
        explanation="A boundary is not handled.",
        evidence="A failing test exercises the boundary.",
        suggested_fix="Preserve the expected boundary behavior.",
    )
    return PatchRequest(finding, context)


def test_baseline_v1_preserves_existing_default_prompt_behavior() -> None:
    default_analysis = analysis_messages(analysis_request())
    explicit_analysis = analysis_messages(analysis_request(), "baseline-v1")
    default_patch = patch_messages(patch_request())
    explicit_patch = patch_messages(patch_request(), "baseline-v1")

    assert DEFAULT_PROMPT_VARIANT == "baseline-v1"
    assert default_analysis == explicit_analysis
    assert default_patch == explicit_patch
    assert default_analysis[0] == {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT}
    assert default_patch[0] == {"role": "system", "content": PATCH_SYSTEM_PROMPT}


def test_candidate_v2_is_selectable_for_analysis_and_patch_messages() -> None:
    analysis = analysis_messages(analysis_request(), "candidate-v2")
    patch = patch_messages(patch_request(), "candidate-v2")

    assert tuple(PROMPT_VARIANTS) == ("baseline-v1", "candidate-v2")
    assert analysis[0]["content"] != ANALYSIS_SYSTEM_PROMPT
    assert patch[0]["content"] != PATCH_SYSTEM_PROMPT
    assert "complete behavior implied by failing tests" in analysis[0]["content"]
    assert "meaningful empty values" in analysis[0]["content"]
    assert "Do not modify tests" in patch[0]["content"]
    combined = analysis[0]["content"] + patch[0]["content"]
    assert "settings_parser_001" not in combined
    assert "AgentLab" not in combined


@pytest.mark.parametrize("builder", [analysis_messages, patch_messages])
def test_message_builders_reject_unknown_variant(builder) -> None:
    request = analysis_request() if builder is analysis_messages else patch_request()

    with pytest.raises(ValueError, match="Unknown prompt variant 'unknown-v9'"):
        builder(request, "unknown-v9")


def test_provider_plumbs_candidate_variant_to_analysis_and_patch_requests() -> None:
    system_prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        system_prompt = payload["messages"][0]["content"]
        system_prompts.append(system_prompt)
        if "You generate one minimal source edit" in system_prompt:
            content = json.dumps(
                {
                    "file": "parser.py",
                    "old_text": "return value",
                    "new_text": "return value.strip()",
                    "reason": "Honor the validated finding.",
                    "confidence": 0.95,
                }
            )
        else:
            content = json.dumps({"findings": []})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    provider = OpenAICompatibleProvider(
        "key",
        "https://provider.invalid/v1",
        "model",
        transport=httpx.MockTransport(handler),
        prompt_variant="candidate-v2",
    )

    provider.analyze(analysis_request())
    provider.generate_patch(patch_request())

    assert len(system_prompts) == 2
    assert "complete behavior implied by failing tests" in system_prompts[0]
    assert "Do not modify tests" in system_prompts[1]


def test_provider_from_env_rejects_unknown_variant_before_configuration(monkeypatch) -> None:
    for name in ("REPO_DOCTOR_API_KEY", "REPO_DOCTOR_BASE_URL", "REPO_DOCTOR_MODEL"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="Unknown prompt variant 'unknown-v9'"):
        provider_from_env("unknown-v9")


@pytest.mark.parametrize(
    ("arguments", "expected_variant"),
    [
        ([], "baseline-v1"),
        (["--prompt-variant", "candidate-v2"], "candidate-v2"),
    ],
)
def test_fix_cli_selects_default_or_explicit_prompt_variant(
    tmp_path,
    monkeypatch,
    arguments: list[str],
    expected_variant: str,
) -> None:
    provider_variants: list[str] = []

    def fake_provider_from_env(*, prompt_variant: str):
        provider_variants.append(prompt_variant)
        return object()

    monkeypatch.setattr("repo_doctor.cli.provider_from_env", fake_provider_from_env)
    monkeypatch.setattr(
        "repo_doctor.cli.execute_ai_fix",
        lambda *_args, **_kwargs: SimpleNamespace(status="no_candidate"),
    )

    response = CliRunner().invoke(app, ["fix", str(tmp_path), "--ai", *arguments])

    assert response.exit_code == 0, response.output
    assert provider_variants == [expected_variant]


def test_fix_cli_rejects_unknown_variant_without_calling_llm(tmp_path, monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("provider and LLM workflow must not be called")

    monkeypatch.setattr("repo_doctor.cli.provider_from_env", forbidden)
    monkeypatch.setattr("repo_doctor.cli.execute_ai_fix", forbidden)

    response = CliRunner().invoke(
        app,
        ["fix", str(tmp_path), "--ai", "--prompt-variant", "unknown-v9"],
    )

    assert response.exit_code == 2
    assert "Unknown prompt variant 'unknown-v9'" in response.output

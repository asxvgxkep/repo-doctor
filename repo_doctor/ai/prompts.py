"""Provider prompts that demand narrow JSON-only responses."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType

from .models import AnalysisRequest, PatchRequest

ANALYSIS_SYSTEM_PROMPT = """You are a conservative semantic code reviewer.
Use only the supplied evidence and files. Report concrete bugs, unsafe assumptions,
resource or exception handling problems, incorrect control flow, edge cases,
cross-platform faults, misleading API behavior, or substantial maintainability risks.
Do not report vague style preferences. Every finding must cite one supplied file.
Return JSON only with exactly this schema:
{"findings":[{"id":"...","title":"...","category":"...","severity":"low|medium|high|critical","confidence":0.0,"file":"relative/path","line_start":1,"line_end":1,"explanation":"...","evidence":"...","suggested_fix":"..."}]}
Use forward slashes in relative paths. Return {"findings":[]} when no concrete issue exists.
"""

PATCH_SYSTEM_PROMPT = """You generate one minimal source edit for one validated finding.
Return JSON only with exactly these fields:
{"file":"relative/path","old_text":"exact unique source text",
"new_text":"replacement text","reason":"...","confidence":0.0}
The file must match the supplied file. old_text must be copied exactly and identify one
small region. Do not return shell commands, unified diffs, multiple files, or prose.
"""

CANDIDATE_ANALYSIS_SYSTEM_PROMPT = ANALYSIS_SYSTEM_PROMPT + """Before finalizing findings,
infer the complete behavior implied by failing tests and surrounding code, not only the first
visible symptom. For parsers and string transformations, check blank input and blank lines,
key and value whitespace separately, meaningful empty values, boundary conditions, and whether
normalization would discard user data. Prefer minimal fixes that preserve compatible behavior.
"""

CANDIDATE_PATCH_SYSTEM_PROMPT = PATCH_SYSTEM_PROMPT + """Before proposing the edit, check it
against the complete test-derived behavior and relevant boundary cases. For parsers and string
transformations, treat key and value whitespace separately, preserve meaningful empty values,
and avoid unnecessary normalization. Keep the patch minimal and compatible with existing
behavior. Do not modify tests or verification configuration merely to make verification pass.
"""

DEFAULT_PROMPT_VARIANT = "baseline-v1"


@dataclass(frozen=True)
class PromptProfile:
    """The analysis and patch prompts selected by one stable variant identifier."""

    analysis_system_prompt: str
    patch_system_prompt: str


PROMPT_VARIANTS: Mapping[str, PromptProfile] = MappingProxyType(
    {
        DEFAULT_PROMPT_VARIANT: PromptProfile(
            analysis_system_prompt=ANALYSIS_SYSTEM_PROMPT,
            patch_system_prompt=PATCH_SYSTEM_PROMPT,
        ),
        "candidate-v2": PromptProfile(
            analysis_system_prompt=CANDIDATE_ANALYSIS_SYSTEM_PROMPT,
            patch_system_prompt=CANDIDATE_PATCH_SYSTEM_PROMPT,
        ),
    }
)


def prompt_profile(prompt_variant: str = DEFAULT_PROMPT_VARIANT) -> PromptProfile:
    """Resolve a stable prompt variant or reject it with an actionable error."""
    try:
        return PROMPT_VARIANTS[prompt_variant]
    except KeyError as error:
        available = ", ".join(PROMPT_VARIANTS)
        raise ValueError(
            f"Unknown prompt variant {prompt_variant!r}. Available variants: {available}."
        ) from error


def analysis_messages(
    request: AnalysisRequest,
    prompt_variant: str = DEFAULT_PROMPT_VARIANT,
) -> list[dict[str, str]]:
    """Build messages without adding any environment configuration or secrets."""
    profile = prompt_profile(prompt_variant)
    payload = {
        "repository": {
            "name": request.repository_name,
            "technologies": request.technologies,
            "file_count": request.file_count,
            "line_count": request.line_count,
        },
        "verifications": [asdict(item) for item in request.verifications],
        "deterministic_findings": request.deterministic_findings,
        "selected_files": [{"path": item.path, "content": item.content} for item in request.files],
    }
    return [
        {"role": "system", "content": profile.analysis_system_prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def patch_messages(
    request: PatchRequest,
    prompt_variant: str = DEFAULT_PROMPT_VARIANT,
) -> list[dict[str, str]]:
    """Build a patch request containing only one finding and one source file."""
    profile = prompt_profile(prompt_variant)
    finding = asdict(request.finding)
    finding["severity"] = request.finding.severity.value
    payload = {
        "finding": finding,
        "source_file": {"path": request.file.path, "content": request.file.content},
    }
    return [
        {"role": "system", "content": profile.patch_system_prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

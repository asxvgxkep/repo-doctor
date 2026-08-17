"""Provider prompts that demand narrow JSON-only responses."""

from __future__ import annotations

import json
from dataclasses import asdict

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


def analysis_messages(request: AnalysisRequest) -> list[dict[str, str]]:
    """Build messages without adding any environment configuration or secrets."""
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
        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def patch_messages(request: PatchRequest) -> list[dict[str, str]]:
    """Build a patch request containing only one finding and one source file."""
    finding = asdict(request.finding)
    finding["severity"] = request.finding.severity.value
    payload = {
        "finding": finding,
        "source_file": {"path": request.file.path, "content": request.file.content},
    }
    return [
        {"role": "system", "content": PATCH_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

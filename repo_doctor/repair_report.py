"""Bounded, secret-safe machine-readable repair diagnostics."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import CommandResult
from .security import SENSITIVE_NAME, bounded_sensitive_text

REPAIR_REPORT_SCHEMA_VERSION = 1
MAX_REPORT_ITEMS = 100
MAX_REPAIR_REPORT_BYTES = 2_000_000


@dataclass
class RepairReport:
    """Progressively persisted facts from one repair attempt."""

    prompt_variant: str
    task_provided: bool
    schema_version: int = REPAIR_REPORT_SCHEMA_VERSION
    analysis_summary: str | None = None
    selected_finding: dict[str, Any] | None = None
    behavioral_contract: dict[str, Any] | None = None
    patch: dict[str, Any] | None = None
    patch_applied: bool = False
    verification: dict[str, Any] = field(default_factory=lambda: {"summary": None, "commands": []})
    rollback_attempted: bool = False
    rollback_succeeded: bool | None = None
    final_status: str = "started"


def command_report(item: CommandResult) -> dict[str, Any]:
    """Render one real verification result without unbounded process output."""
    return {
        "name": item.name,
        "command": list(item.command),
        "returncode": item.exit_code,
        "passed": item.passed,
        "timed_out": item.timed_out,
        "stdout_summary": item.stdout,
        "stderr_summary": item.stderr,
    }


def _safe_value(
    value: Any,
    *,
    key: str | None = None,
    text_limit: int = 8_000,
    item_limit: int = MAX_REPORT_ITEMS,
) -> Any:
    if key is not None and SENSITIVE_NAME.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _safe_value(
                item,
                key=str(item_key),
                text_limit=text_limit,
                item_limit=item_limit,
            )
            for item_key, item in list(value.items())[:item_limit]
        }
    if isinstance(value, (list, tuple)):
        return [
            _safe_value(item, text_limit=text_limit, item_limit=item_limit)
            for item in value[:item_limit]
        ]
    if isinstance(value, str):
        return bounded_sensitive_text(value, text_limit)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return bounded_sensitive_text(value)


def repair_report_data(
    report: RepairReport,
    *,
    text_limit: int = 8_000,
    item_limit: int = MAX_REPORT_ITEMS,
) -> dict[str, Any]:
    """Return a JSON-safe report after recursive redaction and truncation."""
    return _safe_value(
        asdict(report),
        text_limit=text_limit,
        item_limit=item_limit,
    )


def write_repair_report(path: Path | None, report: RepairReport) -> None:
    """Atomically persist the latest repair state when reporting was requested."""
    if path is None:
        return
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = repair_report_data(report)
    payload = json.dumps(
        data,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > MAX_REPAIR_REPORT_BYTES:
        payload = json.dumps(
            repair_report_data(report, text_limit=2_000, item_limit=50),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
    if len(payload) > MAX_REPAIR_REPORT_BYTES:
        payload = json.dumps(
            repair_report_data(report, text_limit=500, item_limit=20),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".repo-doctor-report-",
        suffix=".json",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

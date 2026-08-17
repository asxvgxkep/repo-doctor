"""Markdown report rendering."""

from .models import CommandResult, ScanResult


def _results(items: list[CommandResult], kind: str) -> str:
    selected = [item for item in items if kind.lower() in item.name.lower()]
    if not selected:
        return "No commands discovered."
    blocks = []
    for item in selected:
        status = "PASS" if item.passed else "FAIL"
        output = (item.stdout + item.stderr).strip()[-4000:] or "(no output)"
        heading = f"### {item.name}: {status}"
        detail = f"`{' '.join(item.command)}` — {item.duration:.2f}s, exit {item.exit_code}"
        blocks.append(f"{heading}\n\n{detail}\n\n```text\n{output}\n```")
    return "\n\n".join(blocks)


def render_report(result: ScanResult) -> str:
    """Render the complete health report as Markdown."""
    recs = result.potential_bugs + result.maintainability_issues
    recommendations = (
        "\n".join(f"{i}. {value}" for i, value in enumerate(recs, 1))
        or "1. Keep dependencies and verification tools current."
    )
    inspected = ", ".join(result.inspected_files) or "none"
    return f"""# Repo Doctor Health Report

## Project Overview

Analyzed `{result.path.name}` locally. Inspected configuration: {inspected}.

## Detected Technology Stack

{", ".join(result.technologies) or "Unknown"}

## Repository Statistics

- Files: {result.files}
- Text lines: {result.lines}

## Test Results

{_results(result.commands, "test")}

## Lint Results

{_results(result.commands, "lint")}

## Potential Bugs

{chr(10).join(f"- {x}" for x in result.potential_bugs) or "- None detected."}

## Maintainability Issues

{chr(10).join(f"- {x}" for x in result.maintainability_issues) or "- None detected."}

## Prioritized Recommendations

{recommendations}

## Health Score (0-100)

**{result.score}/100**
"""

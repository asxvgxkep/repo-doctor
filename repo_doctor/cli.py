"""Command-line interface for Repo Doctor."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from .ai.errors import AIError
from .ai.fixer import execute_ai_fix
from .ai.prompts import DEFAULT_PROMPT_VARIANT, prompt_profile
from .ai.provider import provider_from_env
from .ai.workflow import analyze_repository
from .backends import ToolBackendError, ToolBackendKind, create_tool_backend
from .fixer import apply_high_confidence_fix, verify_clean_git
from .report import render_report
from .scanner import scan
from .sessions import (
    ScanSession,
    SessionStatus,
    create_scan_session,
    load_session,
    resume_scan_session,
    save_session,
)

app = typer.Typer(help="Safely diagnose and repair a local repository.", no_args_is_help=True)
console = Console()


@app.command("scan")
def scan_command(
    repository_path: Path,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    timeout: int = 120,
    ai: Annotated[bool, typer.Option("--ai", help="Enable configured semantic analysis.")] = False,
    tool_backend: Annotated[
        ToolBackendKind,
        typer.Option("--tool-backend", help="Tool execution backend: local or mcp."),
    ] = ToolBackendKind.LOCAL,
) -> None:
    """Scan REPOSITORY_PATH without modifying repository source files."""
    try:
        session = None
        with console.status("[cyan]Examining repository…"):
            if tool_backend is ToolBackendKind.LOCAL:
                result = scan(repository_path, timeout)
            else:
                backend = create_tool_backend(tool_backend, repository_path)
                result = scan(repository_path, timeout, backend=backend)
            if ai:
                result.ai_requested = True
                try:
                    analyze_repository(result, provider_from_env())
                except AIError as error:
                    result.ai_error = str(error)
            if tool_backend is ToolBackendKind.MCP and any(
                item.approval_required for item in result.commands
            ):
                session = create_scan_session(result)
                save_session(session)
        report = render_report(result)
        if output:
            output.write_text(report, encoding="utf-8")
            console.print(f"[green]Report written to {output}[/green]")
        else:
            console.print(report)
        console.print(f"[bold]Health score: {result.score}/100[/bold]")
        if session is not None:
            _print_approval_instructions(session)
    except (OSError, ToolBackendError, ValueError) as error:
        console.print(f"[red]Error: {error}[/red]")
        raise typer.Exit(2) from error


@app.command("resume")
def resume_command(
    session_id: str,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Resume pending ToolHub verification requests for a saved MCP scan."""
    try:
        session = load_session(session_id)
        with console.status("[cyan]Checking ToolHub approvals…"):
            resume_scan_session(session)
        report = render_report(session.result)
        if output:
            output.write_text(report, encoding="utf-8")
            console.print(f"[green]Report written to {output}[/green]")
        else:
            console.print(report)
        console.print(f"[bold]Health score: {session.result.score}/100[/bold]")
        if session.pending_operations:
            console.print("[yellow]Partial completion; approvals are still required.[/yellow]")
            _print_approval_instructions(session)
        elif session.status is SessionStatus.COMPLETED:
            console.print("[green]All pending verification completed.[/green]")
        else:
            console.print(
                "[yellow]Verification could not continue for one or more requests. "
                "See the report for ToolHub states.[/yellow]"
            )
    except (OSError, ToolBackendError, ValueError) as error:
        console.print(f"[red]Error: {error}[/red]")
        raise typer.Exit(2) from error


def _print_approval_instructions(session: ScanSession) -> None:
    console.print("\n[yellow]Approval required.[/yellow]")
    console.print("\nSession:")
    console.print(f"  {session.session_id}", markup=False)
    for operation in session.pending_operations:
        console.print(f"\n{operation.name}:", markup=False)
        console.print(f"  request_id: {operation.request_id}", markup=False)
    console.print("\nApprove the required requests with ToolHub's trusted admin CLI:")
    console.print(r"  cd D:\mcp-toolhub", markup=False)
    console.print("  uv run python -m toolhub.admin approve <request_id>", markup=False)
    console.print(f"\nThen run from {session.target_repository}:", markup=False)
    console.print(f"  repo-doctor resume {session.session_id}", markup=False)


@app.command()
def fix(
    repository_path: Path,
    timeout: int = 120,
    ai: Annotated[bool, typer.Option("--ai", help="Enable one verified semantic repair.")] = False,
    prompt_variant: Annotated[
        str,
        typer.Option(
            "--prompt-variant",
            help="Prompt variant used for AI analysis and patch generation.",
        ),
    ] = DEFAULT_PROMPT_VARIANT,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview an AI patch without modifying files.")
    ] = False,
) -> None:
    """Apply one safe fix to a clean Git repository, then verify it."""
    root = repository_path.resolve()
    try:
        prompt_profile(prompt_variant)
        if dry_run and not ai:
            raise ValueError("--dry-run is available only with --ai.")
        if ai:
            outcome = execute_ai_fix(
                root,
                provider_from_env(prompt_variant=prompt_variant),
                timeout=timeout,
                dry_run=dry_run,
            )
            if outcome.status == "no_candidate":
                console.print("[yellow]No high-confidence AI fix is available.[/yellow]")
            elif outcome.status == "dry_run":
                console.print("[cyan]Proposed patch (dry run; no files changed):[/cyan]")
                console.print(outcome.diff)
            elif outcome.status == "kept":
                console.print("[green]Patch applied[/green]")
                console.print("[green]Verification passed[/green]")
                console.print("[green]Change kept[/green]")
            else:
                console.print("[yellow]Patch applied[/yellow]")
                console.print(f"[red]Verification failed:[/red] {outcome.verification}")
                console.print("[yellow]Rolling back[/yellow]")
                console.print("[green]Repository restored successfully[/green]")
                raise typer.Exit(1)
            return
        verify_clean_git(root)
        before = scan(root, timeout)
        change = apply_high_confidence_fix(root)
        if not change:
            console.print("[yellow]No high-confidence fix is available.[/yellow]")
            return
        after = scan(root, timeout)
        succeeded = after.score >= before.score and all(item.passed for item in after.commands)
        if succeeded:
            console.print(f"[green]Fix succeeded:[/green] {change}")
        else:
            console.print(f"[red]Verification failed after fix:[/red] {change}")
            raise typer.Exit(1)
    except (AIError, OSError, ValueError) as error:
        console.print(f"[red]Error: {error}[/red]")
        raise typer.Exit(2) from error


if __name__ == "__main__":
    app()

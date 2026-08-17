"""Command-line interface for Repo Doctor."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from .ai.errors import AIError
from .ai.fixer import execute_ai_fix
from .ai.provider import provider_from_env
from .ai.workflow import analyze_repository
from .fixer import apply_high_confidence_fix, verify_clean_git
from .report import render_report
from .scanner import scan

app = typer.Typer(help="Safely diagnose and repair a local repository.", no_args_is_help=True)
console = Console()


@app.command("scan")
def scan_command(
    repository_path: Path,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    timeout: int = 120,
    ai: Annotated[bool, typer.Option("--ai", help="Enable configured semantic analysis.")] = False,
) -> None:
    """Scan REPOSITORY_PATH without modifying it."""
    try:
        with console.status("[cyan]Examining repository…"):
            result = scan(repository_path, timeout)
            if ai:
                result.ai_requested = True
                try:
                    analyze_repository(result, provider_from_env())
                except AIError as error:
                    result.ai_error = str(error)
        report = render_report(result)
        if output:
            output.write_text(report, encoding="utf-8")
            console.print(f"[green]Report written to {output}[/green]")
        else:
            console.print(report)
        console.print(f"[bold]Health score: {result.score}/100[/bold]")
    except (OSError, ValueError) as error:
        console.print(f"[red]Error: {error}[/red]")
        raise typer.Exit(2) from error


@app.command()
def fix(
    repository_path: Path,
    timeout: int = 120,
    ai: Annotated[bool, typer.Option("--ai", help="Enable one verified semantic repair.")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview an AI patch without modifying files.")
    ] = False,
) -> None:
    """Apply one safe fix to a clean Git repository, then verify it."""
    root = repository_path.resolve()
    try:
        if dry_run and not ai:
            raise ValueError("--dry-run is available only with --ai.")
        if ai:
            outcome = execute_ai_fix(root, provider_from_env(), timeout=timeout, dry_run=dry_run)
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

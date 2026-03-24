"""CLI command: sfs audit <session_id>."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

import typer
from rich.panel import Panel
from rich.table import Table

from sessionfs.cli.common import (
    console,
    err_console,
    get_session_dir_or_exit,
    open_store,
    resolve_session_id,
)


def audit(
    session_id: str = typer.Argument(..., help="Session ID or prefix"),
    model: str = typer.Option("claude-sonnet-4", "--model", help="Judge LLM model"),
    api_key: Optional[str] = typer.Option(None, "--api-key", help="LLM API key"),
    provider: Optional[str] = typer.Option(None, "--provider", help="LLM provider (anthropic, openai, google)"),
    consensus: bool = typer.Option(False, "--consensus", help="Run 3 passes, report only where 2+ agree"),
    report_only: bool = typer.Option(False, "--report", help="Show existing report only"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Audit a session for hallucinations using LLM-as-a-Judge."""
    store = open_store()
    session_id = resolve_session_id(store, session_id)
    session_dir = get_session_dir_or_exit(store, session_id)

    if report_only:
        _show_existing_report(session_dir, session_id, json_output)
        return

    resolved_key = _resolve_api_key(api_key, model)
    if resolved_key is None:
        err_console.print(
            "[red]No API key found. Provide --api-key, set it in config.toml [judge], "
            "or set ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY env var.[/red]"
        )
        raise typer.Exit(1)

    try:
        if consensus:
            console.print("[dim]Running 3 consensus passes (3x cost)...[/dim]")
        report = asyncio.run(
            _run_judge(session_id, session_dir, model, resolved_key, provider, consensus)
        )
    except KeyboardInterrupt:
        raise typer.Exit(1)
    except Exception as exc:
        err_console.print(f"[red]Audit failed: {exc}[/red]")
        raise typer.Exit(1)

    _display_report(report, json_output)


def _resolve_api_key(explicit_key: str | None, model: str) -> str | None:
    """Resolve API key from flag, config, or environment."""
    if explicit_key:
        return explicit_key

    # Try config.toml [judge] section
    try:
        from sessionfs.daemon.config import load_config

        config = load_config()
        raw = config.model_dump()
        judge_config = raw.get("judge", {})
        if isinstance(judge_config, dict):
            config_key = judge_config.get("api_key", "")
            if config_key:
                return config_key
    except Exception:
        pass

    # Try environment variables based on model/provider
    model_lower = model.lower()
    if model_lower.startswith("claude"):
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            return key
    elif model_lower.startswith(("gpt-", "o1", "o3")):
        key = os.environ.get("OPENAI_API_KEY")
        if key:
            return key
    elif model_lower.startswith("gemini"):
        key = os.environ.get("GOOGLE_API_KEY")
        if key:
            return key

    # Fallback: try all env vars
    for env_var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        key = os.environ.get(env_var)
        if key:
            return key

    return None


async def _run_judge(
    session_id: str,
    session_dir,
    model: str,
    api_key: str,
    provider: str | None,
    consensus: bool = False,
):
    """Run the judge pipeline asynchronously."""
    if consensus:
        from sessionfs.judge.judge import judge_with_consensus

        return await judge_with_consensus(
            session_id=session_id,
            sfs_dir=session_dir,
            model=model,
            api_key=api_key,
            provider=provider,
            passes=3,
            threshold=2,
        )

    from sessionfs.judge.judge import judge_session

    return await judge_session(
        session_id=session_id,
        sfs_dir=session_dir,
        model=model,
        api_key=api_key,
        provider=provider,
    )


def _show_existing_report(session_dir, session_id: str, json_output: bool) -> None:
    """Load and display an existing audit report."""
    from sessionfs.judge.report import load_report

    report = load_report(session_dir)
    if report is None:
        err_console.print(
            f"[yellow]No audit report found for {session_id}. "
            f"Run 'sfs audit {session_id}' to generate one.[/yellow]"
        )
        raise typer.Exit(1)

    _display_report(report, json_output)


def _display_report(report, json_output: bool) -> None:
    """Display an audit report with Rich formatting."""
    from dataclasses import asdict

    if json_output:
        console.print(json.dumps(asdict(report), indent=2))
        return

    # Summary panel
    s = report.summary
    trust_color = "green" if s.trust_score >= 0.8 else "yellow" if s.trust_score >= 0.5 else "red"
    summary_text = (
        f"Trust Score: [{trust_color}]{s.trust_score:.0%}[/{trust_color}]\n"
        f"Total Claims: {s.total_claims}\n"
        f"[green]Verified: {s.verified}[/green]  "
        f"[yellow]Unverified: {s.unverified}[/yellow]  "
        f"[red]Hallucinations: {s.hallucinations}[/red]\n"
        f"Severity — Major: {s.major_findings}  Moderate: {s.moderate_findings}  Minor: {s.minor_findings}"
    )
    console.print(Panel(summary_text, title=f"Audit: {report.session_id}", subtitle=f"Model: {report.model}"))

    if not report.findings:
        console.print("[dim]No verifiable claims found in this session.[/dim]")
        return

    # Findings table
    table = Table(show_lines=True)
    table.add_column("Msg", style="dim", width=5)
    table.add_column("Verdict", width=14)
    table.add_column("Severity", width=10)
    table.add_column("Claim", max_width=50)
    table.add_column("Explanation", max_width=50)

    verdict_colors = {
        "verified": "green",
        "unverified": "yellow",
        "hallucination": "red",
    }
    severity_colors = {
        "minor": "dim",
        "moderate": "yellow",
        "major": "red bold",
    }

    for f in report.findings:
        vc = verdict_colors.get(f.verdict, "white")
        sc = severity_colors.get(f.severity, "white")
        table.add_row(
            str(f.message_index),
            f"[{vc}]{f.verdict}[/{vc}]",
            f"[{sc}]{f.severity}[/{sc}]",
            f.claim[:100],
            f.explanation[:100],
        )

    console.print(table)

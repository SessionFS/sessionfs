"""`sfs loop …` — Review Loop watch + merge readiness (Review Loop B2).

Commands:
- sfs loop ready <ticket> --sha <head>   — is this ticket's work safe to merge
                                           at <head>? Pure read; a human still
                                           does the merge (CEO rule).
"""

from __future__ import annotations

import asyncio

import typer
from rich.panel import Panel

from sessionfs.cli.cmd_rules import (
    _api_request,
    _get_api_config,
    _get_git_remote,
    _normalize_remote,
    _resolve_project_id,
)
from sessionfs.cli.common import console, err_console, handle_errors

loop_app = typer.Typer(
    name="loop",
    help="Review Loop watch + merge readiness (TEAM+).",
    no_args_is_help=True,
)


def _resolve_project() -> tuple[str, str, str]:
    remote = _get_git_remote()
    if not remote:
        err_console.print(
            "[red]No git remote found.[/red] "
            "Run inside a git repo with an `origin` remote."
        )
        raise typer.Exit(1)
    normalized = _normalize_remote(remote)
    if not normalized:
        err_console.print(f"[red]Could not normalize remote: {remote}[/red]")
        raise typer.Exit(1)
    api_url, api_key = _get_api_config()
    project_id = _resolve_project_id(api_url, api_key, normalized)
    return api_url, api_key, project_id


@loop_app.command("ready")
@handle_errors
def loop_ready(
    ticket_id: str = typer.Argument(..., help="Ticket id (tk_...)."),
    sha: str = typer.Option(
        ..., "--sha", help="Head commit id (full 40- or 64-char lowercase hex)."
    ),
) -> None:
    """Check whether this ticket's work is safe to merge at --sha.

    Green only when a trusted strict VERIFIED-CLEAN verdict is bound to exactly
    that commit, came from a reviewer identity different from every
    implementer, and no harmful finding is open. A human still merges.
    """
    api_url, api_key, project_id = _resolve_project()

    async def _fetch() -> dict | None:
        status, body, _ = await _api_request(
            "GET",
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}/readiness",
            api_url,
            api_key,
            params={"sha": sha.strip()},
        )
        if status == 404:
            err_console.print(f"[red]Ticket '{ticket_id}' not found.[/red]")
            raise typer.Exit(1)
        if status >= 400 or not isinstance(body, dict):
            err_console.print(f"[red]API error ({status}): {body}[/red]")
            raise typer.Exit(1)
        return body

    payload = asyncio.run(_fetch())
    if payload is None:
        return

    ready = bool(payload.get("ready"))
    reasons = payload.get("reasons") or []
    verdict = payload.get("verdict") or {}

    if ready:
        line = "[green]READY[/green]"
    else:
        line = "[red]NOT READY[/red]"
    bits = [line]
    if verdict.get("reviewed_commit"):
        bits.append(f"[dim]reviewed {verdict['reviewed_commit']}[/dim]")
    if verdict.get("reviewer"):
        bits.append(f"[dim]by {verdict['reviewer']}[/dim]")
    console.print(Panel(
        " ".join(bits),
        title=f"Merge readiness — {ticket_id} @ {sha.strip()}",
        title_align="left",
    ))
    for reason in reasons:
        console.print(f"[yellow]•[/yellow] {reason}")
    if not ready:
        raise typer.Exit(1)

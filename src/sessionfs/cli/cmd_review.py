"""`sfs review …` — personal auto-reviewer requests (Auto-reviewer P1).

Commands:
- sfs review request [--ticket ID | --repo owner/repo --pr 42 --head-sha SHA]
                     — ask your runner to draft a review of a PR/merge request.
- sfs review list    — your review requests, newest first.
- sfs review show ID — one request, including the draft findings.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import typer
from rich.panel import Panel
from rich.table import Table

from sessionfs.cli.cmd_rules import (
    _api_request,
    _get_api_config,
    _get_git_remote,
    _normalize_remote,
    _resolve_project_id,
)
from sessionfs.cli.common import console, err_console, format_api_error, handle_errors

review_app = typer.Typer(
    name="review",
    help="Ask your personal reviewer to draft a review of a PR/merge request.",
    no_args_is_help=True,
)


def _resolve_project(project: str | None) -> tuple[str, str, str]:
    """Resolve (api_url, api_key, project_id) from --project or the git remote.

    ``--project`` may be a ``proj_…`` id directly, or a git remote to look up.
    With neither, fall back to the current repo's ``origin`` remote."""
    api_url, api_key = _get_api_config()
    if project and project.startswith("proj_"):
        return api_url, api_key, project
    if project:
        normalized = _normalize_remote(project)
        if not normalized:
            err_console.print(f"[red]Could not normalize remote: {project}[/red]")
            raise typer.Exit(1)
        return api_url, api_key, _resolve_project_id(api_url, api_key, normalized)
    remote = _get_git_remote()
    if not remote:
        err_console.print(
            "[red]No git remote found.[/red] Pass [bold]--project[/bold] or run "
            "inside a git repo with an `origin` remote."
        )
        raise typer.Exit(1)
    normalized = _normalize_remote(remote)
    if not normalized:
        err_console.print(f"[red]Could not normalize remote: {remote}[/red]")
        raise typer.Exit(1)
    return api_url, api_key, _resolve_project_id(api_url, api_key, normalized)


def _print_request(req: dict) -> None:
    status = req.get("status", "")
    verdict = req.get("verdict")
    status_line = f"[bold]{status}[/bold]"
    if verdict:
        status_line += f" — [bold]{verdict}[/bold]"
    console.print(Panel(
        f"{req.get('repo')}#{req.get('pr_number')}  [dim]{req.get('head_sha', '')[:12]}[/dim]\n"
        f"{status_line}  [dim]{req.get('findings_count', 0)} finding(s)[/dim]",
        title=f"Review request {req.get('id')}",
        title_align="left",
    ))
    for label, key in (
        ("Profile", "profile"),
        ("Project", "project_id"),
        ("Ticket", "ticket_id"),
        ("Provider", "provider"),
    ):
        value = req.get(key)
        if value:
            console.print(f"[dim]{label}:[/dim] {value}")
    if req.get("created_at"):
        console.print(f"[dim]Created:[/dim] {req['created_at']}")
    error = req.get("error")
    if error:
        console.print(f"[red]Error: {error}[/red]")


@review_app.command("request")
@handle_errors
def review_request(
    ticket: str | None = typer.Option(None, "--ticket", help="Ticket id (tk_…) with a linked PR."),
    repo: str | None = typer.Option(None, "--repo", "-r", help="Repository slug (owner/repo)."),
    pr_number: int | None = typer.Option(None, "--pr", help="Pull request / merge request number."),
    head_sha: str | None = typer.Option(None, "--head-sha", help="Full 40- or 64-char head commit SHA."),
    profile: str = typer.Option("default", "--profile", help="Reviewer profile (persona name or 'default')."),
    provider: str = typer.Option("github", "--provider", help="github | gitlab"),
    base_sha: str | None = typer.Option(None, "--base-sha", help="Full base commit SHA."),
    project: str | None = typer.Option(None, "--project", help="Project id (proj_…) or git remote."),
    budget_tokens: int | None = typer.Option(None, "--budget-tokens", help="Token budget (≤ 2,000,000)."),
    budget_seconds: int | None = typer.Option(None, "--budget-seconds", help="Time budget in seconds (≤ 86400)."),
) -> None:
    """Create (or reuse) a personal review request.

    Give either a ``--ticket`` with a linked PR, or a target
    (``--repo`` + ``--pr`` + ``--head-sha``). Re-requesting the same head sha
    returns the existing row; a new sha supersedes any still-pending request
    for the same PR and profile."""
    api_url, api_key, project_id = _resolve_project(project)

    has_target = repo is not None and pr_number is not None and head_sha is not None
    if ticket and has_target:
        err_console.print("[red]Give either --ticket or --repo/--pr/--head-sha, not both.[/red]")
        raise typer.Exit(1)
    if not ticket and not has_target:
        err_console.print(
            "[red]Give --ticket, or --repo + --pr + --head-sha.[/red]"
        )
        raise typer.Exit(1)

    body: dict = {"profile": profile}
    if ticket:
        body["ticket_id"] = ticket.strip()
    if has_target:
        assert head_sha is not None  # has_target already guarantees it
        body.update(
            {
                "provider": provider,
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": head_sha.strip(),
            }
        )
        if base_sha:
            body["base_sha"] = base_sha.strip()
    if budget_tokens is not None:
        body["budget_tokens"] = budget_tokens
    if budget_seconds is not None:
        body["budget_seconds"] = budget_seconds

    async def _create() -> dict | None:
        status, payload, _ = await _api_request(
            "POST",
            f"/api/v1/projects/{quote(project_id, safe='')}/review-requests",
            api_url,
            api_key,
            json_data=body,
        )
        if status >= 400 or not isinstance(payload, dict):
            err_console.print(f"[red]API error ({status}): {format_api_error(payload, status)}[/red]")
            raise typer.Exit(1)
        return payload

    req = asyncio.run(_create())
    if req is None:
        return
    console.print(f"[green]Review request queued:[/green] {req.get('id')}")
    _print_request(req)


@review_app.command("list")
@handle_errors
def review_list(
    limit: int = typer.Option(20, "--limit", help="Rows per page (≤ 100)."),
    before: str | None = typer.Option(None, "--before", help="Keyset cursor (ISO-8601 datetime)."),
    before_id: str | None = typer.Option(None, "--before-id", help="Keyset cursor id."),
) -> None:
    """List your review requests, newest first."""
    api_url, api_key = _get_api_config()

    async def _fetch() -> dict | None:
        params = {"limit": min(max(limit, 1), 100)}
        if before:
            params["before"] = before
        if before_id:
            params["before_id"] = before_id
        status, body, _ = await _api_request(
            "GET", "/api/v1/me/review-requests", api_url, api_key, params=params
        )
        if status >= 400 or not isinstance(body, dict):
            err_console.print(f"[red]API error ({status}): {format_api_error(body, status)}[/red]")
            raise typer.Exit(1)
        return body

    payload = asyncio.run(_fetch())
    if payload is None:
        return
    items = payload.get("items") or []
    if not items:
        console.print("[dim]No review requests yet.[/dim]")
        return

    table = Table(title="Review requests")
    table.add_column("id")
    table.add_column("repo")
    table.add_column("pr")
    table.add_column("profile")
    table.add_column("status")
    table.add_column("verdict")
    table.add_column("findings")
    for req in items:
        table.add_row(
            req.get("id", ""),
            req.get("repo", ""),
            str(req.get("pr_number", "")),
            req.get("profile", ""),
            req.get("status", ""),
            req.get("verdict") or "",
            str(req.get("findings_count", 0)),
        )
    console.print(table)
    if payload.get("has_more"):
        nxt = payload.get("next_before")
        nxt_id = payload.get("next_before_id")
        if nxt:
            console.print(
                f"[dim]More: --before {nxt} --before-id {nxt_id}[/dim]"
            )


@review_app.command("show")
@handle_errors
def review_show(
    request_id: str = typer.Argument(..., help="Review request id (rvq_…)."),
) -> None:
    """Show one review request, including its draft findings."""
    api_url, api_key = _get_api_config()

    async def _fetch() -> dict | None:
        status, body, _ = await _api_request(
            "GET", f"/api/v1/review-requests/{quote(request_id, safe='')}", api_url, api_key
        )
        if status == 404:
            err_console.print(f"[red]Review request '{request_id}' not found.[/red]")
            raise typer.Exit(1)
        if status >= 400 or not isinstance(body, dict):
            err_console.print(f"[red]API error ({status}): {format_api_error(body, status)}[/red]")
            raise typer.Exit(1)
        return body

    req = asyncio.run(_fetch())
    if req is None:
        return
    _print_request(req)

    findings = req.get("findings") or []
    if findings:
        console.print(f"\n[bold]{len(findings)} finding(s)[/bold]")
        for f in findings:
            loc = ""
            if f.get("path"):
                loc = f"{f['path']}"
                if f.get("line"):
                    loc += f":{f['line']}"
            head = f"[bold]{f.get('class', '')}[/bold]"
            if f.get("severity"):
                head += f" [yellow]{f['severity']}[/yellow]"
            console.print(f"• {head}")
            if loc:
                console.print(f"  [dim]{loc}[/dim]")
            console.print(f"  {f.get('message', '')}")
    packet = req.get("packet_manifest")
    if packet:
        console.print(
            f"\n[dim]Packet manifest:[/dim] {json.dumps(packet, sort_keys=True)}"
        )

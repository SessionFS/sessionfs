"""`sfs release …` — pull requests, releases, gates, deployments, Ship Report.

Commands:
- sfs release record <version> --tag-sha <sha> [--previous <sha> --pr N
  --ticket TK --gate kind=status --source S]  — record a release, link PRs and
  tickets, record gates
- sfs release link <version> --ticket TK      — link tickets directly (repeatable)
- sfs release deploy <version> --env <env> [--source S]  — record a deployment
- sfs release list                       — newest-first release listing
- sfs release ship-report <version> | --month YYYY-MM [--markdown]
                                         — the Ship Report
"""

from __future__ import annotations

import asyncio

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
from sessionfs.url_path_safety import _path_segment

release_app = typer.Typer(
    name="release",
    help="Pull requests, releases, gates, deployments and the Ship Report.",
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


def _api_error(status: int, body: object) -> None:
    err_console.print(
        f"[red]API error ({status}): {format_api_error(body, status)}[/red]"
    )
    raise typer.Exit(1)


@release_app.command("record")
@handle_errors
def release_record(
    version: str = typer.Argument(..., help="Release version."),
    tag_sha: str = typer.Option(..., "--tag-sha", help="Commit the release is tagged at."),
    previous: str = typer.Option(None, "--previous", help="Previous tag SHA."),
    pr: list[int] = typer.Option(None, "--pr", help="PR number shipped (repeatable)."),
    ticket: list[str] = typer.Option(
        None, "--ticket", help="Ticket id to link directly (repeatable)."
    ),
    gate: list[str] = typer.Option(
        None, "--gate", help="Gate as kind=status, e.g. --gate ci=passed (repeatable)."
    ),
    source: str = typer.Option(
        "manual", "--source", help="github_release | gitlab_release | helm_sync | manual"
    ),
) -> None:
    """Record a release, link its PRs and tickets, and record its gates."""
    api_url, api_key, project_id = _resolve_project()
    version = version.strip()
    body: dict = {"version": version, "tag_sha": tag_sha.strip()}
    if previous and previous.strip():
        body["previous_tag_sha"] = previous.strip()
    if pr:
        body["pr_numbers"] = list(pr)
    if ticket:
        body["ticket_ids"] = [t.strip() for t in ticket if t.strip()]
    if source and source.strip():
        body["source"] = source.strip().lower()

    gates: list[tuple[str, str]] = []
    for spec in gate or []:
        kind, _, status = spec.partition("=")
        kind = kind.strip()
        if not kind:
            err_console.print(f"[red]--gate {spec!r} is missing a kind.[/red]")
            raise typer.Exit(1)
        gates.append((kind, (status.strip() or "passed").lower()))

    async def _record() -> dict:
        st, payload, _ = await _api_request(
            "POST",
            f"/api/v1/projects/{project_id}/releases",
            api_url,
            api_key,
            json_data=body,
        )
        if st >= 400 or not isinstance(payload, dict):
            _api_error(st, payload)
        return payload

    async def _gate(release_version: str, kind: str, status: str) -> None:
        st, payload, _ = await _api_request(
            "POST",
            f"/api/v1/projects/{project_id}/releases/{_path_segment(release_version)}/gates",
            api_url,
            api_key,
            json_data={"kind": kind, "status": status},
        )
        if st >= 400:
            _api_error(st, payload)

    result = asyncio.run(_record())
    for kind, status in gates:
        asyncio.run(_gate(version, kind, status))

    console.print(Panel(
        f"version=[bold]{result.get('version')}[/bold]  "
        f"tag_sha={result.get('tag_sha')}  source={result.get('source')}",
        title="Release recorded",
        title_align="left",
    ))
    if result.get("pr_ids"):
        console.print(f"  PRs linked: {', '.join(result['pr_ids'])}")
    if result.get("ticket_ids"):
        console.print(f"  Tickets linked: {', '.join(result['ticket_ids'])}")
    for kind, status in gates:
        console.print(f"  gate [cyan]{kind}[/cyan] = {status}")


@release_app.command("link")
@handle_errors
def release_link(
    version: str = typer.Argument(..., help="Release version."),
    ticket: list[str] = typer.Option(
        ..., "--ticket", help="Ticket id to link directly (repeatable)."
    ),
) -> None:
    """Link tickets directly to a release (PR-less release flow)."""
    if not ticket:
        err_console.print("[red]Pass at least one --ticket.[/red]")
        raise typer.Exit(1)
    api_url, api_key, project_id = _resolve_project()
    ticket_ids = [t.strip() for t in ticket if t.strip()]
    if not ticket_ids:
        err_console.print("[red]Pass at least one --ticket.[/red]")
        raise typer.Exit(1)

    async def _link() -> dict:
        st, payload, _ = await _api_request(
            "POST",
            f"/api/v1/projects/{project_id}/releases/{_path_segment(version.strip())}/tickets",
            api_url,
            api_key,
            json_data={"ticket_ids": ticket_ids},
        )
        if st == 404:
            err_console.print("[red]Release or ticket not found.[/red]")
            raise typer.Exit(1)
        if st >= 400 or not isinstance(payload, dict):
            _api_error(st, payload)
        return payload

    result = asyncio.run(_link())
    console.print(Panel(
        f"version=[bold]{result.get('version')}[/bold]  "
        f"tickets=[bold]{', '.join(result.get('ticket_ids') or [])}[/bold]",
        title="Tickets linked",
        title_align="left",
    ))


@release_app.command("deploy")
@handle_errors
def release_deploy(
    version: str = typer.Argument(..., help="Release version."),
    env: str = typer.Option(..., "--env", help="Environment (e.g. prod, staging)."),
    source: str = typer.Option("manual", "--source", help="argocd | helm | manual"),
) -> None:
    """Record a deployment of a release to an environment."""
    api_url, api_key, project_id = _resolve_project()

    async def _deploy() -> dict:
        st, payload, _ = await _api_request(
            "POST",
            f"/api/v1/projects/{project_id}/releases/{_path_segment(version.strip())}/deployments",
            api_url,
            api_key,
            json_data={"environment": env.strip(), "source": source.strip().lower()},
        )
        if st == 404:
            err_console.print("[red]Release not found.[/red]")
            raise typer.Exit(1)
        if st >= 400 or not isinstance(payload, dict):
            _api_error(st, payload)
        return payload

    result = asyncio.run(_deploy())
    console.print(
        f"[green]deployed[/green] {version.strip()} → "
        f"[bold]{result.get('environment')}[/bold] ({result.get('source')})"
    )


@release_app.command("list")
@handle_errors
def release_list() -> None:
    """List the project's releases, newest first."""
    api_url, api_key, project_id = _resolve_project()

    async def _fetch() -> dict:
        st, payload, _ = await _api_request(
            "GET",
            f"/api/v1/projects/{project_id}/releases",
            api_url,
            api_key,
        )
        if st >= 400 or not isinstance(payload, dict):
            _api_error(st, payload)
        return payload

    payload = asyncio.run(_fetch())
    rows = payload.get("releases") or []
    if not rows:
        console.print("[dim]No releases recorded.[/dim]")
        return

    table = Table(title="Releases", title_justify="left")
    table.add_column("Version", style="bold")
    table.add_column("Released")
    table.add_column("Source")
    table.add_column("Tag SHA")
    for r in rows:
        table.add_row(
            r.get("version", ""),
            str(r.get("released_at") or "—"),
            r.get("source", ""),
            r.get("tag_sha", ""),
        )
    console.print(table)


@release_app.command("ship-report")
@handle_errors
def release_ship_report(
    version: str = typer.Argument(None, help="Release version."),
    month: str = typer.Option(None, "--month", help="YYYY-MM (mutually exclusive with VERSION)."),
    markdown: bool = typer.Option(
        False, "--markdown", help="Print the markdown release notes instead."
    ),
) -> None:
    """The Ship Report for one release, or one month."""
    if bool(version) == bool(month):
        err_console.print("[red]Pass exactly one of VERSION or --month.[/red]")
        raise typer.Exit(1)
    api_url, api_key, project_id = _resolve_project()

    if version:
        path = f"/api/v1/projects/{project_id}/releases/{_path_segment(version.strip())}/ship-report"
    else:
        path = f"/api/v1/projects/{project_id}/ship-report"
        month = (month or "").strip()

    async def _fetch() -> dict:
        st, payload, _ = await _api_request(
            "GET",
            path,
            api_url,
            api_key,
            params={"month": month} if month else None,
        )
        if st == 404:
            err_console.print("[red]Release not found.[/red]")
            raise typer.Exit(1)
        if st >= 400 or not isinstance(payload, dict):
            _api_error(st, payload)
        return payload

    payload = asyncio.run(_fetch())

    if markdown:
        console.print(payload.get("markdown") or "")
        return

    console.print(Panel(
        f"loops=[bold]{payload.get('loops', 0)}[/bold]  "
        f"tickets=[bold]{payload.get('tickets', 0)}[/bold]  "
        f"review_rounds=[bold]{payload.get('review_rounds', 0)}[/bold]",
        title=f"Ship Report — {payload.get('version') or month}",
        title_align="left",
    ))
    console.print(
        f"findings={payload.get('findings', 0)}  harmful={payload.get('harmful', 0)}  "
        f"all_fixed_before_merge={'yes' if payload.get('all_fixed_before_merge') else 'no'}"
    )
    if payload.get("harmful_by_harm"):
        by_harm = ", ".join(
            f"{k}={v}" for k, v in sorted((payload.get("harmful_by_harm") or {}).items())
        )
        console.print(f"[dim]harmful by harm:[/dim] {by_harm}")

    cost = payload.get("cost_per_accepted_change") or {}
    bits = [f"tokens {cost.get('tokens', 0):g}"]
    if "cost_usd" in cost:
        bits.append(f"${cost['cost_usd']:.2f}")
    console.print(f"[dim]cost per accepted change:[/dim] {', '.join(bits)}")

    learned = payload.get("checklist_classes_learned") or []
    if learned:
        names = ", ".join(str(c.get("title") or c.get("class")) for c in learned)
        console.print(f"[dim]classes learned:[/dim] {names}")
    gates = payload.get("gates") or []
    for g in gates:
        console.print(f"  gate [cyan]{g.get('kind')}[/cyan] = {g.get('status')}")
    deployments = payload.get("deployments") or []
    for d in deployments:
        console.print(f"  deployed → [bold]{d.get('environment')}[/bold] ({d.get('source')})")

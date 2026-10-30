"""`sfs loop …` — Review Loop start, status, rollups, merge readiness and checklist.

Commands:
- sfs loop start <issue-or-title> --reviewer-persona P --ticket T…
                                             — create (or adopt) a loop parent,
                                               file one child task per --ticket
                                               and stand up a review_until_clean
                                               queue.
- sfs loop status [<loop-id>]                — one loop, or every loop.
- sfs loop ready <ticket> --sha <head>       — is this ticket's work safe to
                                               merge at <head>? Pure read; a
                                               human still does the merge.
- sfs loop report <ticket>                   — per-round findings/harm/tokens rollup
- sfs loop insights [--from --to]            — caught findings, cost per change, learning
- sfs loop checklist list [--status S]       — list the project's checklist
- sfs loop checklist seed                    — offer the six generic seed traps
- sfs loop checklist add <title> [--check]   — propose an item by hand
- sfs loop checklist accept <slug>           — accept a proposed item
- sfs loop checklist retire <slug>           — retire an item
"""

from __future__ import annotations

import asyncio
import json

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

loop_app = typer.Typer(
    name="loop",
    help="Review Loop start, status, rollups, merge readiness and checklist (TEAM+).",
    no_args_is_help=True,
)

checklist_app = typer.Typer(
    name="checklist",
    help="The project checklist that learns.",
    no_args_is_help=True,
)
loop_app.add_typer(checklist_app)


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


@loop_app.command("start")
@handle_errors
def loop_start(
    target: str = typer.Argument(
        ..., help="Existing issue ticket id (tk_...) to adopt, or the new loop's title."
    ),
    reviewer_persona: str = typer.Option(
        ..., "--reviewer-persona", help="Persona that reviews the children."
    ),
    ticket: list[str] = typer.Option(
        ...,
        "--ticket",
        help="Child brief: 'title' or 'title::description' (repeatable).",
    ),
    acceptance: list[str] = typer.Option(
        None,
        "--acceptance",
        help="Acceptance criterion applied to every child (repeatable).",
    ),
    token_budget: int | None = typer.Option(None, "--token-budget", help="Token budget for the loop."),
    cost_budget_usd: float | None = typer.Option(None, "--cost-budget-usd", help="Dollar budget (org owner/admin only)."),
    max_review_rounds: int | None = typer.Option(None, "--max-review-rounds", help="Review-round cap (no default)."),
) -> None:
    """Start a review loop: create (or adopt) the parent issue, file one child
    task per --ticket, and stand up a review_until_clean queue for the reviewer.
    """
    api_url, api_key, project_id = _resolve_project()

    tickets: list[dict] = []
    for spec in ticket:
        title, _, description = spec.partition("::")
        title = title.strip()
        if not title:
            err_console.print(f"[red]--ticket {spec!r} has an empty title.[/red]")
            raise typer.Exit(1)
        tickets.append(
            {
                "title": title,
                "description": description.strip(),
                "acceptance_criteria": [a.strip() for a in (acceptance or []) if a.strip()],
            }
        )

    body: dict = {
        "tickets": tickets,
        "reviewer_persona": reviewer_persona.strip(),
    }
    if target.strip().startswith("tk_"):
        body["from_ticket_id"] = target.strip()
    else:
        body["title"] = target.strip()
    if token_budget is not None:
        body["token_budget"] = token_budget
    if cost_budget_usd is not None:
        body["cost_budget_usd"] = cost_budget_usd
    if max_review_rounds is not None:
        body["max_review_rounds"] = max_review_rounds

    async def _create() -> dict | None:
        status, payload, _ = await _api_request(
            "POST",
            f"/api/v1/projects/{project_id}/loops",
            api_url,
            api_key,
            json_data=body,
        )
        if status >= 400 or not isinstance(payload, dict):
            err_console.print(f"[red]API error ({status}): {payload}[/red]")
            raise typer.Exit(1)
        return payload

    result = asyncio.run(_create())
    if result is None:
        return

    reviewer = result.get("reviewer") or {}
    console.print(Panel(
        f"[green]Loop started[/green]  loop_id=[bold]{result.get('loop_id')}[/bold]  "
        f"queue_id=[bold]{result.get('queue_id')}[/bold]",
        title=f"Review loop — {result.get('loop_id')}",
        title_align="left",
    ))
    if reviewer.get("trusted"):
        console.print(
            f"[green]✓[/green] reviewer [bold]{reviewer.get('persona')}[/bold] is trusted"
        )
    else:
        console.print(
            f"[yellow]![/yellow] reviewer [bold]{reviewer.get('persona')}[/bold] "
            f"is not trusted yet: {reviewer.get('setup_hint')}"
        )
    for t in result.get("tickets") or []:
        console.print(f"  [bold]{t.get('id')}[/bold]  {t.get('title')}")
    env = result.get("env") or {}
    console.print(
        "[dim]implementer env:[/dim] "
        + json.dumps(env.get("implementer") or {}),
        "[dim]reviewer env:[/dim] "
        + json.dumps(env.get("reviewer") or {}),
    )


def _render_child(child: dict) -> str:
    bits = [f"[bold]{child.get('title')}[/bold]"]
    if child.get("branch"):
        bits.append(f"[dim]{child['branch']}@{child.get('head_sha') or ''}[/dim]")
    bits.append(f"status={child.get('status')} round={child.get('round')}")
    if child.get("harmful_open"):
        bits.append(f"[red]{child['harmful_open']} harmful open[/red]")
    if child.get("ready"):
        bits.append("[green]ready[/green]")
    elif child.get("verdict"):
        bits.append("[yellow]reviewed[/yellow]")
    return "  ".join(bits)


@loop_app.command("status")
@handle_errors
def loop_status(
    loop_id: str | None = typer.Argument(None, help="Loop (parent issue) id (tk_...). Omit to list all loops."),
) -> None:
    """Show one loop, or every loop in the project."""
    api_url, api_key, project_id = _resolve_project()

    async def _fetch(path: str) -> list | dict | None:
        status, payload, _ = await _api_request(
            "GET", path, api_url, api_key
        )
        if status == 404:
            err_console.print("[red]Loop not found.[/red]")
            raise typer.Exit(1)
        if status >= 400:
            err_console.print(f"[red]API error ({status}): {payload}[/red]")
            raise typer.Exit(1)
        return payload

    if loop_id:
        loop = asyncio.run(_fetch(f"/api/v1/projects/{project_id}/loops/{loop_id}"))
        if not isinstance(loop, dict):
            return
        console.print(Panel(
            f"loop_id=[bold]{loop.get('id')}[/bold]",
            title=f"Review loop — {loop.get('title')}",
            title_align="left",
        ))
        for child in loop.get("children") or []:
            console.print(f"  {_render_child(child)}")
        return

    loops = asyncio.run(_fetch(f"/api/v1/projects/{project_id}/loops"))
    if not isinstance(loops, list):
        return
    table = Table(title="Review loops", title_justify="left")
    table.add_column("loop")
    table.add_column("children")
    table.add_column("ready")
    for loop in loops:
        children = loop.get("children") or []
        ready = sum(1 for c in children if c.get("ready"))
        table.add_row(
            loop.get("id", ""),
            str(len(children)),
            f"{ready}/{len(children)}" if children else "—",
        )
    console.print(table)
    for loop in loops:
        console.print(f"[bold]{loop.get('id')}[/bold]  {loop.get('title')}")
        for child in loop.get("children") or []:
            console.print(f"  {_render_child(child)}")


@loop_app.command("update")
@handle_errors
def loop_update(
    loop_id: str = typer.Argument(..., help="Loop (parent issue) id (tk_...)."),
    token_budget: int | None = typer.Option(None, "--token-budget", help="New token budget."),
    max_review_rounds: int | None = typer.Option(None, "--max-review-rounds", help="New review-round cap."),
    cost_budget_usd: float | None = typer.Option(None, "--cost-budget-usd", help="New dollar budget (org owner/admin only)."),
) -> None:
    """Raise a review loop's budget caps. Only the flags you pass are changed."""
    api_url, api_key, project_id = _resolve_project()

    body: dict = {}
    if token_budget is not None:
        body["token_budget"] = token_budget
    if max_review_rounds is not None:
        body["max_review_rounds"] = max_review_rounds
    if cost_budget_usd is not None:
        body["cost_budget_usd"] = cost_budget_usd

    async def _patch() -> dict | None:
        status, payload, _ = await _api_request(
            "PATCH",
            f"/api/v1/projects/{project_id}/loops/{_path_segment(loop_id)}",
            api_url,
            api_key,
            json_data=body,
        )
        if status == 404:
            err_console.print("[red]Loop not found.[/red]")
            raise typer.Exit(1)
        if status >= 400:
            err_console.print(f"[red]API error ({status}): {payload}[/red]")
            raise typer.Exit(1)
        return payload

    loop = asyncio.run(_patch())
    if not isinstance(loop, dict):
        return
    console.print(Panel(
        f"loop_id=[bold]{loop.get('id')}[/bold]",
        title=f"Review loop — {loop.get('title')}",
        title_align="left",
    ))
    for child in loop.get("children") or []:
        console.print(f"  {_render_child(child)}")


@loop_app.command("close-item")
@handle_errors
def loop_close_item(
    loop_id: str = typer.Argument(..., help="Loop (parent issue) id (tk_...)."),
    ticket_id: str = typer.Argument(..., help="Child ticket id (tk_...) to settle as done-by-human."),
) -> None:
    """Settle a needs_human child as done-by-human so the loop can stop waiting."""
    api_url, api_key, project_id = _resolve_project()

    async def _close() -> dict | None:
        status, payload, _ = await _api_request(
            "POST",
            f"/api/v1/projects/{project_id}/loops/{_path_segment(loop_id)}/children/{_path_segment(ticket_id)}/close",
            api_url,
            api_key,
        )
        if status == 404:
            err_console.print("[red]Loop or child not found.[/red]")
            raise typer.Exit(1)
        if status == 409:
            err_console.print(f"[red]{payload}[/red]")
            raise typer.Exit(1)
        if status >= 400:
            err_console.print(f"[red]API error ({status}): {payload}[/red]")
            raise typer.Exit(1)
        return payload

    result = asyncio.run(_close())
    if isinstance(result, dict):
        console.print(
            f"[green]✓[/green] {result.get('ticket_id')} settled as "
            f"{result.get('item_status')}"
        )


# ── Checklist (Review Loop E1) ──


def _cli_slugify(title: str) -> str:
    """Derive a checklist slug client-side (mirrors the server's _slugify).

    Lowercase, collapse runs of non-alphanumerics to a single hyphen. The
    server validates the slug and 422s on a mismatch, so this only needs to be
    good enough to save a keystroke, not authoritative.
    """
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:63].rstrip("-")


def _checklist_api_error(status: int, body: object) -> None:
    from sessionfs.cli.common import format_api_error

    err_console.print(
        f"[red]API error ({status}): {format_api_error(body, status)}[/red]"
    )
    raise typer.Exit(1)


def _get_checklist(api_url: str, api_key: str, project_id: str, status: str | None) -> list:
    async def _fetch() -> list:
        st, body, _ = await _api_request(
            "GET",
            f"/api/v1/projects/{project_id}/loop-checklist",
            api_url,
            api_key,
            params={"status": status} if status else None,
        )
        if st >= 400:
            _checklist_api_error(st, body)
        if not isinstance(body, list):
            err_console.print("[red]Unexpected API response.[/red]")
            raise typer.Exit(1)
        return body

    return asyncio.run(_fetch())


def _find_item_by_slug(
    api_url: str, api_key: str, project_id: str, slug: str
) -> dict:
    slug = (slug or "").strip().lower()
    items = _get_checklist(api_url, api_key, project_id, None)
    for item in items:
        if (item.get("slug") or "").lower() == slug:
            return item
    err_console.print(f"[red]No checklist item with slug '{slug}'.[/red]")
    raise typer.Exit(1)


def _patch_status(
    api_url: str, api_key: str, project_id: str, item: dict, status: str
) -> None:
    item_id = item.get("id")
    slug = item.get("slug")

    async def _patch() -> None:
        st, body, _ = await _api_request(
            "PATCH",
            f"/api/v1/projects/{project_id}/loop-checklist/{item_id}",
            api_url,
            api_key,
            json_data={"status": status},
        )
        if st >= 400:
            _checklist_api_error(st, body)

    asyncio.run(_patch())
    console.print(
        f"[green]{status.title()}[/green] checklist item "
        f"[bold]{slug}[/bold]."
    )


@checklist_app.command("list")
@handle_errors
def checklist_list(
    status: str = typer.Option(
        None, "--status", help="Filter: proposed | active | retired"
    ),
) -> None:
    """List the project's checklist items (optionally by status)."""
    api_url, api_key, project_id = _resolve_project()
    items = _get_checklist(api_url, api_key, project_id, status)

    if not items:
        console.print("[dim]No checklist items.[/dim]")
        return

    from rich.table import Table

    table = Table(show_header=True, header_style="bold")
    table.add_column("Slug", style="bold")
    table.add_column("Status")
    table.add_column("Source")
    table.add_column("Found")
    table.add_column("Title")
    for item in items:
        table.add_row(
            item.get("slug", ""),
            item.get("status", ""),
            item.get("source", ""),
            str(item.get("times_found", 0)),
            item.get("title", ""),
        )
    console.print(table)


@checklist_app.command("seed")
@handle_errors
def checklist_seed() -> None:
    """Offer the six generic seed traps as proposed items (idempotent)."""
    api_url, api_key, project_id = _resolve_project()

    async def _seed() -> dict:
        st, body, _ = await _api_request(
            "POST",
            f"/api/v1/projects/{project_id}/loop-checklist/seed",
            api_url,
            api_key,
        )
        if st >= 400:
            _checklist_api_error(st, body)
        if not isinstance(body, dict):
            err_console.print("[red]Unexpected API response.[/red]")
            raise typer.Exit(1)
        return body

    payload = asyncio.run(_seed())
    created = payload.get("created") or []
    skipped = payload.get("skipped") or []
    for item in created:
        console.print(f"[green]proposed[/green] {item.get('slug', '')}")
    for slug in skipped:
        console.print(f"[dim]skipped (already present)[/dim] {slug}")
    console.print(
        f"\nCreated {len(created)}, skipped {len(skipped)} seed items."
    )


@checklist_app.command("add")
@handle_errors
def checklist_add(
    title: str = typer.Argument(..., help="The trap (item title)."),
    check: str = typer.Option(
        "", "--check", help="How to check for it. Defaults to the title."
    ),
    slug: str = typer.Option(
        "", "--slug", help="Slug key. Derived from the title if omitted."
    ),
) -> None:
    """Propose a checklist item by hand (source=manual)."""
    api_url, api_key, project_id = _resolve_project()
    title = title.strip()
    check_text = (check or "").strip() or title
    slug = (slug or "").strip().lower() or _cli_slugify(title)

    async def _add() -> dict:
        st, body, _ = await _api_request(
            "POST",
            f"/api/v1/projects/{project_id}/loop-checklist",
            api_url,
            api_key,
            json_data={"slug": slug, "title": title, "check_text": check_text},
        )
        if st >= 400:
            _checklist_api_error(st, body)
        if not isinstance(body, dict):
            err_console.print("[red]Unexpected API response.[/red]")
            raise typer.Exit(1)
        return body

    item = asyncio.run(_add())
    console.print(
        f"[green]proposed[/green] [bold]{item.get('slug', slug)}[/bold] "
        f"({item.get('status', 'proposed')})."
    )


@checklist_app.command("accept")
@handle_errors
def checklist_accept(
    slug: str = typer.Argument(..., help="Slug of the item to accept."),
) -> None:
    """Accept a proposed checklist item (→ active)."""
    api_url, api_key, project_id = _resolve_project()
    item = _find_item_by_slug(api_url, api_key, project_id, slug)
    _patch_status(api_url, api_key, project_id, item, "active")


@checklist_app.command("retire")
@handle_errors
def checklist_retire(
    slug: str = typer.Argument(..., help="Slug of the item to retire."),
) -> None:
    """Retire a checklist item (→ retired)."""
    api_url, api_key, project_id = _resolve_project()
    item = _find_item_by_slug(api_url, api_key, project_id, slug)
    _patch_status(api_url, api_key, project_id, item, "retired")
@loop_app.command("report")
@handle_errors
def loop_report_cmd(
    ticket_id: str = typer.Argument(..., help="Ticket id (tk_...)."),
) -> None:
    """Per-round review-loop report: findings by harm, class/design counts, tokens."""
    api_url, api_key, project_id = _resolve_project()

    async def _fetch() -> dict | None:
        status, body, _ = await _api_request(
            "GET",
            f"/api/v1/projects/{project_id}/tickets/{_path_segment(ticket_id)}/loop-report",
            api_url,
            api_key,
        )
        if status == 404:
            err_console.print(f"[red]Ticket '{ticket_id}' not found.[/red]")
            raise typer.Exit(1)
        if status >= 400 or not isinstance(body, dict):
            err_console.print(
                f"[red]API error ({status}): {format_api_error(body, status)}[/red]"
            )
            raise typer.Exit(1)
        return body

    payload = asyncio.run(_fetch())
    if payload is None:
        return

    rounds = payload.get("rounds") or []
    if not rounds:
        console.print("[dim]No review rounds recorded.[/dim]")
        return

    table = Table(title=f"Loop report — {ticket_id}")
    table.add_column("Round", justify="right")
    table.add_column("Stage", style="cyan")
    table.add_column("Harm (count)", justify="left")
    table.add_column("Classes", justify="right")
    table.add_column("New", justify="right")
    table.add_column("Design", justify="right")
    table.add_column("Tokens", justify="right")
    show_cost = any("cost_usd" in r for r in rounds)
    if show_cost:
        table.add_column("Cost", justify="right")
    for r in rounds:
        harm = ", ".join(f"{k}={v}" for k, v in sorted((r.get("findings_by_harm") or {}).items()))
        row = [
            str(r.get("round", "-")),
            str(r.get("stage") or "-"),
            harm or "-",
            str(r.get("checklist_class_count", 0)),
            str(r.get("new_class_count", 0)),
            str(r.get("design_stage_count", 0)),
            str(r.get("tokens", 0)),
        ]
        if show_cost:
            cost = r.get("cost_usd")
            row.append(f"${cost:.2f}" if cost is not None else "-")
        table.add_row(*row)
    console.print(table)


@loop_app.command("insights")
@handle_errors
def loop_insights_cmd(
    from_raw: str = typer.Option(None, "--from", help="Start of window (ISO datetime)."),
    to_raw: str = typer.Option(None, "--to", help="End of window (ISO datetime)."),
) -> None:
    """Project-wide loop insights: caught findings, cost per accepted change, learning."""
    api_url, api_key, project_id = _resolve_project()
    params: dict[str, str] = {}
    if from_raw and from_raw.strip():
        params["from"] = from_raw.strip()
    if to_raw and to_raw.strip():
        params["to"] = to_raw.strip()

    async def _fetch() -> dict | None:
        status, body, _ = await _api_request(
            "GET",
            f"/api/v1/projects/{project_id}/loop-insights",
            api_url,
            api_key,
            params=params,
        )
        if status == 404:
            err_console.print("[red]Project not found.[/red]")
            raise typer.Exit(1)
        if status >= 400 or not isinstance(body, dict):
            err_console.print(
                f"[red]API error ({status}): {format_api_error(body, status)}[/red]"
            )
            raise typer.Exit(1)
        return body

    payload = asyncio.run(_fetch())
    if payload is None:
        return

    caught = payload.get("caught") or []
    console.print(f"[bold]Caught findings[/bold] ({len(caught)})")
    if caught:
        table = Table()
        table.add_column("Harm", style="cyan")
        table.add_column("Class")
        table.add_column("Finding")
        table.add_column("File", style="dim")
        for c in caught:
            table.add_row(
                str(c.get("harm") or "-"),
                str(c.get("class") or "-"),
                str(c.get("finding") or "-"),
                str(c.get("file") or "-"),
            )
        console.print(table)
    else:
        console.print("[dim]None.[/dim]")

    cost = payload.get("cost_per_accepted_change") or []
    console.print("[bold]Cost per accepted change[/bold]")
    if cost:
        table = Table()
        table.add_column("Month", style="cyan")
        table.add_column("Tokens / change", justify="right")
        if any("cost_usd" in c for c in cost):
            table.add_column("Cost / change", justify="right")
        for c in cost:
            row = [str(c.get("period") or "-"), f"{c.get('tokens', 0):g}"]
            if "cost_usd" in c:
                row.append(f"${c['cost_usd']:.2f}")
            table.add_row(*row)
        console.print(table)
    else:
        console.print("[dim]None.[/dim]")

    learning = payload.get("learning") or []
    console.print("[bold]Learning by checklist class[/bold]")
    if learning:
        for cls in learning:
            joined = cls.get("joined_at")
            total = sum(p.get("count", 0) for p in (cls.get("series") or []))
            joined_bits = f" [dim]joined {joined}[/dim]" if joined else " [dim](no checklist item yet)[/dim]"
            console.print(f"[cyan]{cls.get('class')}[/cyan] — {total} finding(s){joined_bits}")
    else:
        console.print("[dim]None.[/dim]")

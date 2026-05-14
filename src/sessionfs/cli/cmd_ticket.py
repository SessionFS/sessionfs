"""`sfs ticket …` — manage agent tickets for the current project.

Commands:
- sfs ticket list [--assigned-to ...] [--status ...] [--priority ...]
- sfs ticket show <id>
- sfs ticket create --title T [--assigned-to ...] [--criteria ...]
- sfs ticket start <id> [--force] [--tool ...]
- sfs ticket complete <id> --notes N [--files ...]
- sfs ticket comment <id> --content C [--as PERSONA]
- sfs ticket status                — show the active ticket (from bundle)
- sfs ticket block | unblock | reopen | approve | dismiss <id>

`start` writes ~/.sessionfs/active_ticket.json so the daemon (Phase 6)
can tag captured sessions; `complete` removes it only if it points at
this ticket. Tier-gated TEAM+ via the `agent_tickets` feature flag.
"""

from __future__ import annotations

import asyncio

import typer
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from sessionfs.active_ticket import (
    bundle_path,
    clear_bundle_if_owned,
    read_bundle,
    write_bundle,
)
from sessionfs.cli.cmd_rules import (
    _api_request,
    _get_api_config,
    _get_git_remote,
    _normalize_remote,
    _resolve_project_id,
)
from sessionfs.cli.common import console, err_console, handle_errors

ticket_app = typer.Typer(
    name="ticket",
    help="Manage agent tickets for this project (TEAM+).",
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


def _priority_style(p: str) -> str:
    return {
        "critical": "bold red",
        "high": "red",
        "medium": "yellow",
        "low": "dim",
    }.get(p, "")


def _status_style(s: str) -> str:
    return {
        "suggested": "magenta",
        "open": "cyan",
        "in_progress": "bold cyan",
        "blocked": "red",
        "review": "yellow",
        "done": "green",
        "cancelled": "dim",
    }.get(s, "")


def _print_ticket_table(rows: list[dict]) -> None:
    if not rows:
        console.print("[dim]No tickets match.[/dim]")
        return
    table = Table()
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Priority")
    table.add_column("Assignee", style="dim")
    for t in rows:
        table.add_row(
            t.get("id", ""),
            t.get("title", ""),
            f"[{_status_style(t.get('status',''))}]{t.get('status','')}[/]",
            f"[{_priority_style(t.get('priority',''))}]{t.get('priority','')}[/]",
            t.get("assigned_to") or "—",
        )
    console.print(table)


@ticket_app.command("list")
@handle_errors
def list_tickets(
    assigned_to: str | None = typer.Option(None, "--assigned-to", "-a"),
    status: str | None = typer.Option(None, "--status", "-s"),
    priority: str | None = typer.Option(None, "--priority", "-p"),
) -> None:
    """List tickets in the current project."""
    api_url, api_key, project_id = _resolve_project()
    params = []
    for k, v in (("assigned_to", assigned_to), ("status", status), ("priority", priority)):
        if v:
            params.append(f"{k}={v}")
    suffix = ("?" + "&".join(params)) if params else ""
    s, body, _ = asyncio.run(
        _api_request(
            "GET", f"/api/v1/projects/{project_id}/tickets{suffix}", api_url, api_key
        )
    )
    if s >= 400:
        err_console.print(f"[red]API error ({s}): {body}[/red]")
        raise typer.Exit(1)
    _print_ticket_table(body if isinstance(body, list) else [])


@ticket_app.command("show")
@handle_errors
def show_ticket(
    ticket_id: str = typer.Argument(..., help="Ticket id (e.g. tk_...)"),
) -> None:
    """Print full ticket details."""
    api_url, api_key, project_id = _resolve_project()
    s, body, _ = asyncio.run(
        _api_request(
            "GET", f"/api/v1/projects/{project_id}/tickets/{ticket_id}",
            api_url, api_key,
        )
    )
    if s == 404:
        err_console.print(f"[red]Ticket '{ticket_id}' not found.[/red]")
        raise typer.Exit(1)
    if s >= 400 or not isinstance(body, dict):
        err_console.print(f"[red]API error ({s}): {body}[/red]")
        raise typer.Exit(1)

    console.print(Panel(
        f"[bold]{body['title']}[/bold]\n"
        f"[dim]{body['id']}[/dim]  "
        f"[{_status_style(body['status'])}]{body['status']}[/]  "
        f"[{_priority_style(body['priority'])}]{body['priority']}[/]  "
        f"assignee: {body.get('assigned_to') or '—'}",
        expand=False,
    ))
    desc = body.get("description") or ""
    if desc:
        console.print(Markdown(desc))
    criteria = body.get("acceptance_criteria") or []
    if criteria:
        console.print("\n[bold]Acceptance criteria:[/bold]")
        for c in criteria:
            console.print(f"  - [ ] {c}")
    files = body.get("file_refs") or []
    if files:
        console.print("\n[bold]Files:[/bold]")
        for f in files:
            console.print(f"  - {f}")
    deps = body.get("depends_on") or []
    if deps:
        console.print("\n[bold]Depends on:[/bold] " + ", ".join(deps))
    if body.get("completion_notes"):
        console.print("\n[bold green]Completion notes:[/bold green]")
        console.print(Markdown(body["completion_notes"]))


@ticket_app.command("create")
@handle_errors
def create_ticket(
    title: str = typer.Option(..., "--title", "-t", help="Short ticket title"),
    description: str = typer.Option("", "--description", "-d"),
    assigned_to: str | None = typer.Option(None, "--assigned-to", "-a"),
    priority: str = typer.Option("medium", "--priority", "-p"),
    criteria: list[str] = typer.Option([], "--criteria", help="Acceptance criterion (repeatable)"),
    file_ref: list[str] = typer.Option([], "--file", "-f", help="File reference (repeatable)"),
    depends_on: list[str] = typer.Option([], "--depends-on", help="Upstream ticket id (repeatable)"),
    output_id: bool = typer.Option(
        False, "--output-id",
        help="Print ONLY the new ticket id to stdout (no decorations). "
             "CI-safe — use `$(sfs ticket create ... --output-id)` to capture.",
    ),
) -> None:
    """Create a new ticket. Defaults to source='human' (lands as 'open')."""
    api_url, api_key, project_id = _resolve_project()
    payload = {
        "title": title,
        "description": description,
        "priority": priority,
        "acceptance_criteria": list(criteria),
        "file_refs": list(file_ref),
        "depends_on": list(depends_on),
    }
    if assigned_to:
        payload["assigned_to"] = assigned_to
    s, body, _ = asyncio.run(
        _api_request(
            "POST", f"/api/v1/projects/{project_id}/tickets", api_url, api_key,
            json_data=payload,
        )
    )
    if s >= 400 or not isinstance(body, dict):
        err_console.print(f"[red]API error ({s}): {body}[/red]")
        raise typer.Exit(1)
    if output_id:
        # Machine-safe: stdout = ticket id, nothing else. Confirmation
        # goes to stderr so `$(sfs ticket create ... --output-id)` is
        # safe to capture.
        print(body["id"])
        err_console.print(f"[dim]Created ticket {body['id']} — {body['title']}[/dim]")
    else:
        console.print(f"[green]Created ticket {body['id']}[/green] — {body['title']}")


@ticket_app.command("start")
@handle_errors
def start_ticket(
    ticket_id: str = typer.Argument(...),
    force: bool = typer.Option(False, "--force", help="Recover a blocked ticket"),
    tool: str | None = typer.Option(
        None, "--tool", help="Target tool for token budget (cursor/claude-code/...)"
    ),
    print_context: bool = typer.Option(
        True, "--print-context/--no-print-context",
        help="Print the compiled persona+ticket context",
    ),
) -> None:
    """Start a ticket. Writes ~/.sessionfs/active_ticket.json."""
    api_url, api_key, project_id = _resolve_project()
    qs = []
    if force:
        qs.append("force=true")
    if tool:
        qs.append(f"tool={tool}")
    suffix = ("?" + "&".join(qs)) if qs else ""
    s, body, _ = asyncio.run(
        _api_request(
            "POST",
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}/start{suffix}",
            api_url, api_key,
        )
    )
    if s == 404:
        err_console.print(f"[red]Ticket '{ticket_id}' not found.[/red]")
        raise typer.Exit(1)
    if s == 409:
        err_console.print(
            "[red]Ticket already started.[/red] "
            "Pass --force to recover a blocked ticket."
        )
        raise typer.Exit(1)
    if s >= 400 or not isinstance(body, dict):
        err_console.print(f"[red]API error ({s}): {body}[/red]")
        raise typer.Exit(1)

    ticket = body.get("ticket", {})
    bundle_ok = write_bundle(
        ticket_id=ticket_id,
        persona_name=ticket.get("assigned_to"),
        project_id=project_id,
    )
    if bundle_ok:
        console.print(
            f"[green]Started {ticket_id}.[/green] "
            f"Active ticket bundle written to {bundle_path()}."
        )
    else:
        # KB 339 LOW — never lie that provenance was written. The
        # server-side state really did transition to in_progress, but
        # the daemon will not tag captured sessions until the next
        # successful start writes the bundle.
        console.print(
            f"[yellow]Started {ticket_id} — but could not write "
            f"{bundle_path()}.[/yellow]"
        )
        err_console.print(
            "[yellow]Warning: subsequent sessions will NOT be tagged "
            "with this ticket/persona until the bundle can be written. "
            f"Check permissions on {bundle_path().parent}.[/yellow]"
        )
    if print_context:
        ctx = body.get("compiled_context") or ""
        if ctx:
            console.print()
            console.print(Markdown(ctx))


@ticket_app.command("complete")
@handle_errors
def complete_ticket(
    ticket_id: str = typer.Argument(...),
    notes: str = typer.Option(..., "--notes", "-n", help="Completion notes"),
    file_changed: list[str] = typer.Option(
        [], "--file", "-f", help="Changed file path (repeatable)"
    ),
    knowledge_entry_id: list[str] = typer.Option(
        [], "--kb-entry", help="Knowledge entry id (repeatable)"
    ),
) -> None:
    """Mark a ticket complete. Moves to review; removes the active bundle."""
    api_url, api_key, project_id = _resolve_project()
    payload = {
        "notes": notes,
        "changed_files": list(file_changed),
        "knowledge_entry_ids": list(knowledge_entry_id),
    }
    s, body, _ = asyncio.run(
        _api_request(
            "POST", f"/api/v1/projects/{project_id}/tickets/{ticket_id}/complete",
            api_url, api_key, json_data=payload,
        )
    )
    if s == 404:
        err_console.print(f"[red]Ticket '{ticket_id}' not found.[/red]")
        raise typer.Exit(1)
    if s >= 400 or not isinstance(body, dict):
        err_console.print(f"[red]API error ({s}): {body}[/red]")
        raise typer.Exit(1)

    cleared = clear_bundle_if_owned(ticket_id=ticket_id, project_id=project_id)
    if cleared:
        console.print(f"[green]Completed {ticket_id}.[/green] Active bundle cleared.")
    else:
        console.print(
            f"[green]Completed {ticket_id}.[/green] "
            "[dim]Active bundle untouched (points at a different ticket).[/dim]"
        )


@ticket_app.command("comment")
@handle_errors
def comment_ticket(
    ticket_id: str = typer.Argument(...),
    content: str = typer.Option(..., "--content", "-c"),
    as_persona: str | None = typer.Option(None, "--as", help="Attribute to persona"),
) -> None:
    """Add a comment to a ticket."""
    api_url, api_key, project_id = _resolve_project()
    payload: dict = {"content": content}
    if as_persona:
        payload["author_persona"] = as_persona
    s, body, _ = asyncio.run(
        _api_request(
            "POST", f"/api/v1/projects/{project_id}/tickets/{ticket_id}/comments",
            api_url, api_key, json_data=payload,
        )
    )
    if s == 404:
        err_console.print(f"[red]Ticket '{ticket_id}' not found.[/red]")
        raise typer.Exit(1)
    if s >= 400 or not isinstance(body, dict):
        err_console.print(f"[red]API error ({s}): {body}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Commented on {ticket_id}.[/green]")


@ticket_app.command("status")
@handle_errors
def active_ticket_status() -> None:
    """Show which ticket the local provenance bundle currently points at."""
    bundle = read_bundle()
    if not bundle:
        console.print("[dim]No active ticket.[/dim]")
        return
    console.print(Panel(
        f"[bold]ticket_id:[/bold] {bundle.get('ticket_id')}\n"
        f"[bold]persona:[/bold]   {bundle.get('persona_name') or '—'}\n"
        f"[bold]project:[/bold]   {bundle.get('project_id')}\n"
        f"[bold]started:[/bold]   {bundle.get('started_at')}",
        title="Active ticket bundle",
        expand=False,
    ))


def _post_transition(ticket_id: str, suffix: str, success_label: str) -> None:
    api_url, api_key, project_id = _resolve_project()
    s, body, _ = asyncio.run(
        _api_request(
            "POST",
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}/{suffix}",
            api_url, api_key,
        )
    )
    if s == 404:
        err_console.print(f"[red]Ticket '{ticket_id}' not found.[/red]")
        raise typer.Exit(1)
    if s >= 400 or not isinstance(body, dict):
        err_console.print(f"[red]API error ({s}): {body}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]{success_label}[/green] {ticket_id}")


@ticket_app.command("block")
@handle_errors
def block_ticket_cmd(ticket_id: str = typer.Argument(...)) -> None:
    """Move in_progress → blocked."""
    _post_transition(ticket_id, "block", "Blocked")


@ticket_app.command("unblock")
@handle_errors
def unblock_ticket_cmd(ticket_id: str = typer.Argument(...)) -> None:
    """Move blocked → in_progress."""
    _post_transition(ticket_id, "unblock", "Unblocked")


@ticket_app.command("reopen")
@handle_errors
def reopen_ticket_cmd(ticket_id: str = typer.Argument(...)) -> None:
    """Move review → open (reporter requests changes)."""
    _post_transition(ticket_id, "reopen", "Reopened")


@ticket_app.command("approve")
@handle_errors
def approve_ticket_cmd(ticket_id: str = typer.Argument(...)) -> None:
    """Move suggested → open (approve an agent-created ticket)."""
    _post_transition(ticket_id, "approve", "Approved")


@ticket_app.command("dismiss")
@handle_errors
def dismiss_ticket_cmd(ticket_id: str = typer.Argument(...)) -> None:
    """Move suggested/open → cancelled."""
    _post_transition(ticket_id, "dismiss", "Dismissed")


@ticket_app.command("assign")
@handle_errors
def assign_ticket_cmd(
    ticket_id: str = typer.Argument(...),
    persona: str = typer.Option(..., "--to", help="Persona to assign the ticket to"),
) -> None:
    """Assign or re-assign a ticket to a persona."""
    api_url, api_key, project_id = _resolve_project()
    s, body, _ = asyncio.run(
        _api_request(
            "PUT",
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}",
            api_url, api_key,
            json_data={"assigned_to": persona},
        )
    )
    if s == 404:
        err_console.print(f"[red]Ticket '{ticket_id}' not found.[/red]")
        raise typer.Exit(1)
    if s >= 400 or not isinstance(body, dict):
        err_console.print(f"[red]API error ({s}): {body}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Assigned {ticket_id} to {persona}.[/green]")


@ticket_app.command("resolve")
@handle_errors
def resolve_ticket_cmd(ticket_id: str = typer.Argument(...)) -> None:
    """Mark a ticket resolved (review → done). Triggers dependency
    enrichment + auto-unblock for tickets waiting on this one.
    """
    api_url, api_key, project_id = _resolve_project()
    s, body, _ = asyncio.run(
        _api_request(
            "POST",
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}/accept",
            api_url, api_key,
        )
    )
    if s == 404:
        err_console.print(f"[red]Ticket '{ticket_id}' not found.[/red]")
        raise typer.Exit(1)
    if s == 409:
        err_console.print(
            "[red]Cannot resolve — ticket is not in 'review' state.[/red]"
        )
        raise typer.Exit(1)
    if s >= 400 or not isinstance(body, dict):
        err_console.print(f"[red]API error ({s}): {body}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Resolved {ticket_id}.[/green]")


_PRIORITY_ESCALATION = {"low": "medium", "medium": "high", "high": "critical"}


@ticket_app.command("escalate")
@handle_errors
def escalate_ticket_cmd(
    ticket_id: str = typer.Argument(...),
    reason: str | None = typer.Option(
        None, "--reason", "-r",
        help="Optional rationale — posted as an audit-trail comment.",
    ),
) -> None:
    """Bump a ticket's priority one level (low→medium→high→critical)."""
    api_url, api_key, project_id = _resolve_project()
    s, body, _ = asyncio.run(
        _api_request(
            "GET",
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}",
            api_url, api_key,
        )
    )
    if s == 404:
        err_console.print(f"[red]Ticket '{ticket_id}' not found.[/red]")
        raise typer.Exit(1)
    if s >= 400 or not isinstance(body, dict):
        err_console.print(f"[red]API error ({s}): {body}[/red]")
        raise typer.Exit(1)

    current = body.get("priority", "medium")
    new_priority = _PRIORITY_ESCALATION.get(current)
    if new_priority is None:
        err_console.print(
            f"[yellow]{ticket_id} is already at '{current}' — "
            "cannot escalate further.[/yellow]"
        )
        raise typer.Exit(0)

    s2, body2, _ = asyncio.run(
        _api_request(
            "PUT",
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}",
            api_url, api_key,
            json_data={"priority": new_priority},
        )
    )
    if s2 >= 400 or not isinstance(body2, dict):
        err_console.print(f"[red]API error ({s2}): {body2}[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]Escalated {ticket_id}: {current} → {new_priority}.[/green]"
    )

    if reason:
        # Best-effort audit-trail comment. Failure here doesn't undo the
        # priority bump — surface but don't fail the command.
        s3, body3, _ = asyncio.run(
            _api_request(
                "POST",
                f"/api/v1/projects/{project_id}/tickets/{ticket_id}/comments",
                api_url, api_key,
                json_data={
                    "content": f"Escalated {current} → {new_priority}: {reason}",
                },
            )
        )
        if s3 >= 400:
            err_console.print(
                f"[yellow]Priority bumped, but audit comment failed ({s3}).[/yellow]"
            )

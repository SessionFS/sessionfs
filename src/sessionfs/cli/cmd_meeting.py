"""`sfs meeting …` — meeting transcripts, action items and promotion to tickets.

Commands:
- sfs meeting upload <file>|--stdin --title T [--project proj_… | --group pg_…] [--date ISO] [--extract]
- sfs meeting list [--project | --group] [--query Q] [--limit N] [--before ID]
- sfs meeting search <q> [--project | --group]
- sfs meeting show <id>            — numbered transcript, participants, items
- sfs meeting delete <id> [--yes]
- sfs meeting extract <id>         — extraction with YOUR OWN LLM key
- sfs meeting participants <id>
- sfs meeting resolve-participant <id> <participant_id> --user ID | --persona NAME
- sfs meeting action-items <id>
- sfs meeting promote <id> <item_id> [--title …] [--assignee PERSONA] [--user ID]
                                     [--due DATE] [--target-project ID] [--jira] [--yes]
- sfs meeting dismiss | restore <id> <item_id>
- sfs meeting link <id> <item_id> [<ticket_id>] [--yes]  — tie an item to an EXISTING
                                     ticket (default: the item's suggestion)
- sfs meeting unlink <id> <item_id>

The CLI is a thin client of the meeting routes; every access rule (project
edit / group editor, DLP, retention) is enforced server-side.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from sessionfs.cli.cmd_rules import (
    _api_request,
    _get_api_config,
    _get_git_remote,
    _normalize_remote,
    _resolve_project_id,
)
from sessionfs.cli.common import (
    confirm_or_exit,
    console,
    err_console,
    format_api_error,
    handle_errors,
)

meeting_app = typer.Typer(
    name="meeting",
    help="Store meeting transcripts, extract action items and promote them to tickets.",
    no_args_is_help=True,
)

# Extraction has a 240 s server deadline (and a Jira publish up to a minute),
# so those calls must outlast the default 30 s request timeout.
_LONG_TIMEOUT = 270

# A meeting file is capped server-side at 512 KiB after normalisation and the
# request at 1 MiB; refuse an obviously oversized file before reading it all.
_MAX_FILE_BYTES = 4 * 1024 * 1024


def _fail(status: int, body: Any) -> None:
    err_console.print(f"[red]API error ({status}): {format_api_error(body, status)}[/red]")
    raise typer.Exit(1)


def _call(
    method: str,
    path: str,
    *,
    json_data: dict | None = None,
    params: dict | None = None,
    api: tuple[str, str] | None = None,
    timeout: float = 30,
) -> Any:
    api_url, api_key = api or _get_api_config()
    status, body, _ = asyncio.run(
        _api_request(
            method, path, api_url, api_key, json_data=json_data, params=params,
            timeout=timeout,
        )
    )
    if status >= 400:
        _fail(status, body)
    return body


def _scope_base(project: str | None, group: str | None) -> tuple[tuple[str, str], str]:
    """((api_url, api_key), '/api/v1/projects/<id>' | '/api/v1/groups/<id>')."""
    if project and group:
        err_console.print("[red]Use either --project or --group, not both.[/red]")
        raise typer.Exit(1)
    api = _get_api_config()
    if group:
        return api, f"/api/v1/groups/{group}"
    if project:
        return api, f"/api/v1/projects/{project}"
    remote = _get_git_remote()
    if not remote:
        err_console.print(
            "[red]No git remote found.[/red] Run inside a git repo with an "
            "`origin` remote, or pass --project / --group."
        )
        raise typer.Exit(1)
    normalized = _normalize_remote(remote)
    if not normalized:
        err_console.print(f"[red]Could not normalize remote: {remote}[/red]")
        raise typer.Exit(1)
    project_id = _resolve_project_id(api[0], api[1], normalized)
    return api, f"/api/v1/projects/{project_id}"


def _kind_params(kind: str | None) -> dict[str, str]:
    """``?kind=`` for the shared meeting routes (``sfs chat`` passes chat)."""
    return {"kind": kind} if kind else {}


def _noun(kind: str | None) -> str:
    return "chat thread" if kind == "chat" else "meeting"


def _meeting_table(rows: list[dict], kind: str | None = None) -> None:
    if not rows:
        console.print(f"[dim]No {_noun(kind)}s match.[/dim]")
        return
    chat = kind == "chat"
    table = Table()
    table.add_column("ID", style="cyan")
    if chat:
        table.add_column("Channel")
    table.add_column("Title")
    table.add_column("Date", style="dim")
    table.add_column("Messages" if chat else "Lines", justify="right")
    table.add_column("Extraction", style="dim")
    for m in rows:
        when = (m.get("meeting_at") or m.get("created_at") or "")[:10]
        cells = [m.get("id", "")]
        if chat:
            cells.append(escape(str(m.get("channel_name") or "")))
        cells += [
            escape(str(m.get("title", ""))),
            when,
            str(m.get("line_count", "")),
            str(m.get("extraction_status", "")),
        ]
        table.add_row(*cells)
    console.print(table)


def _participant_label(p: dict) -> str:
    resolved = p.get("persona_name") or p.get("user_id")
    if p.get("persona_name") and p.get("user_id"):
        resolved = f"{p['persona_name']} (user {p['user_id']})"
    return resolved or "unresolved"


def _print_participants(participants: list[dict]) -> None:
    if not participants:
        console.print("[dim]No participants.[/dim]")
        return
    table = Table()
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Email", style="dim")
    table.add_column("Resolved to")
    table.add_column("How", style="dim")
    for p in participants:
        table.add_row(
            p.get("id", ""),
            escape(str(p.get("name_raw", ""))),
            escape(str(p.get("email_raw") or "")),
            escape(_participant_label(p)),
            p.get("resolution_method", ""),
        )
    console.print(table)


def _print_items(items: list[dict], participants: list[dict], kind: str | None = None) -> None:
    if not items:
        console.print("[dim]No action items or decisions.[/dim]")
        return
    names = {p.get("id"): p.get("name_raw", "") for p in participants}
    table = Table()
    table.add_column("ID", style="cyan")
    table.add_column("Kind")
    table.add_column("Status")
    table.add_column("Owner", style="dim")
    table.add_column("Due", style="dim")
    table.add_column("Messages" if kind == "chat" else "Lines", style="dim")
    table.add_column("Description")
    for it in items:
        table.add_row(
            it.get("id", ""),
            it.get("kind", ""),
            it.get("status", ""),
            escape(
                str(
                    names.get(it.get("owner_participant_id"))
                    or (f"unknown ({it['owner_hint']})" if it.get("owner_hint") else "—")
                )
            ),
            str(it.get("due_date") or "—"),
            f"{'M' if kind == 'chat' else 'L'}{it.get('line_start')}-{it.get('line_end')}",
            escape(str(it.get("description", ""))),
        )
    console.print(table)
    promoted = [it for it in items if it.get("promoted_ticket_id")]
    for it in promoted:
        console.print(f"[dim]{it['id']} promoted to ticket[/dim] {it['promoted_ticket_id']}")
    for it in items:
        linked = it.get("linked_ticket")
        if it.get("status") == "linked" and linked:
            console.print(
                f"[dim]{it['id']} linked to ticket[/dim] {linked['id']} "
                f"[dim]· {escape(str(linked.get('title', '')))} · {linked.get('status')}[/dim]"
            )
        suggested = it.get("suggested_ticket")
        if it.get("status") == "pending" and suggested:
            console.print(
                f"[yellow]{it['id']} looks like[/yellow] {suggested['id']} "
                f"[dim]· {escape(str(suggested.get('title', '')))} · {suggested.get('status')}[/dim]"
            )


def _print_extraction_summary(body: dict) -> None:
    status = body.get("extraction_status")
    items = body.get("action_items") or []
    parts = body.get("participants") or []
    console.print(
        f"Extraction: [bold]{status}[/bold] — {len(parts)} participant(s), "
        f"{len(items)} item(s)"
        + (f", {body['dropped_items']} dropped" if body.get("dropped_items") else "")
    )


@meeting_app.command("upload")
@handle_errors
def upload_meeting(
    file: Path | None = typer.Argument(None, help="Transcript file (.txt, .md, .vtt, .srt)."),
    stdin: bool = typer.Option(False, "--stdin", help="Read the transcript from standard input."),
    title: str = typer.Option(..., "--title", "-t", help="Meeting title (max 200 chars)."),
    project: str | None = typer.Option(None, "--project", "-p", help="Project id (proj_…). Default: this repo's project."),
    group: str | None = typer.Option(None, "--group", "-g", help="Project group id (pg_…)."),
    date: str | None = typer.Option(None, "--date", help="When the meeting took place (ISO 8601)."),
    duration: int | None = typer.Option(None, "--duration", help="Length in minutes."),
    extract: bool = typer.Option(False, "--extract", help="Also extract action items now (uses your own LLM key)."),
) -> None:
    """Upload a meeting transcript. DLP-redacted server-side before storage."""
    if (file is None) == (not stdin):
        err_console.print("[red]Give exactly one of <file> or --stdin.[/red]")
        raise typer.Exit(1)
    if stdin:
        content = sys.stdin.read()
        source = "manual_paste"
    else:
        assert file is not None
        try:
            if file.stat().st_size > _MAX_FILE_BYTES:
                err_console.print(f"[red]{file} is too large for a meeting transcript.[/red]")
                raise typer.Exit(1)
            content = file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            err_console.print(f"[red]Cannot read {file}: {exc.strerror or exc}[/red]")
            raise typer.Exit(1) from exc
        source = "file_upload"
    if not content.strip():
        err_console.print("[red]The transcript is empty.[/red]")
        raise typer.Exit(1)

    api, base = _scope_base(project, group)
    payload: dict[str, Any] = {"title": title, "content": content, "source": source}
    if date:
        payload["meeting_at"] = date
    if duration is not None:
        payload["duration_minutes"] = duration
    if extract:
        payload["extract"] = True
    body = _call(
        "POST", f"{base}/meetings", json_data=payload, api=api,
        timeout=_LONG_TIMEOUT if extract else 30,
    )
    console.print(
        f"[green]Stored[/green] [cyan]{body['id']}[/cyan] — {body.get('line_count', '?')} lines"
        + (f", {body['dlp_findings']} redaction(s)" if body.get("dlp_findings") else "")
    )
    for warning in body.get("warnings") or []:
        console.print(f"[yellow]warning:[/yellow] {escape(str(warning))}")
    if extract:
        _print_extraction_summary(body)


@meeting_app.command("list")
@handle_errors
def list_meetings(
    project: str | None = typer.Option(None, "--project", "-p"),
    group: str | None = typer.Option(None, "--group", "-g"),
    query: str | None = typer.Option(None, "--query", "-q", help="Match title and transcript text."),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=200),
    before: str | None = typer.Option(None, "--before", help="Cursor: the id printed after the previous page."),
) -> None:
    """List meetings, newest first."""
    list_impl(project, group, query, limit, before)


def list_impl(
    project: str | None,
    group: str | None,
    query: str | None,
    limit: int,
    before: str | None,
    kind: str | None = None,
) -> None:
    api, base = _scope_base(project, group)
    params: dict[str, str] = {"limit": str(limit), **_kind_params(kind)}
    if query:
        params["q"] = query
    if before:
        params["before"] = before
    body = _call("GET", f"{base}/meetings", params=params, api=api)
    _meeting_table(body.get("meetings", []) if isinstance(body, dict) else [], kind)
    if isinstance(body, dict) and body.get("next_cursor"):
        import shlex

        parts = ["sfs", "chat" if kind == "chat" else "meeting", "list"]
        if project:
            parts += ["--project", project]
        if group:
            parts += ["--group", group]
        if query:
            parts += ["--query", query]
        parts += ["--limit", str(limit), "--before", str(body["next_cursor"])]
        console.print(
            f"[dim]More: {escape(' '.join(shlex.quote(p) for p in parts))}[/dim]",
            soft_wrap=True,
        )


@meeting_app.command("search")
@handle_errors
def search_meetings(
    q: str = typer.Argument(..., help="Text to find in titles and transcripts."),
    project: str | None = typer.Option(None, "--project", "-p"),
    group: str | None = typer.Option(None, "--group", "-g"),
    limit: int = typer.Option(25, "--limit", "-n", min=1, max=200),
) -> None:
    """Search meetings by title and transcript text."""
    search_impl(q, project, group, limit)


def search_impl(
    q: str, project: str | None, group: str | None, limit: int, kind: str | None = None
) -> None:
    api, base = _scope_base(project, group)
    body = _call(
        "GET", f"{base}/meetings",
        params={"q": q, "limit": str(limit), **_kind_params(kind)}, api=api,
    )
    _meeting_table(body.get("meetings", []) if isinstance(body, dict) else [], kind)


def _get_meeting(meeting_id: str, kind: str | None = None) -> dict:
    body = _call("GET", f"/api/v1/meetings/{meeting_id}", params=_kind_params(kind) or None)
    if not isinstance(body, dict):
        err_console.print("[red]Unexpected API response.[/red]")
        raise typer.Exit(1)
    return body


@meeting_app.command("show")
@handle_errors
def show_meeting(
    meeting_id: str = typer.Argument(..., help="Meeting id (meet_…)"),
    no_transcript: bool = typer.Option(False, "--no-transcript", help="Hide the transcript text."),
) -> None:
    """Print a meeting: numbered transcript, participants, action items."""
    show_impl(meeting_id, no_transcript)


def show_impl(meeting_id: str, no_transcript: bool, kind: str | None = None) -> None:
    m = _get_meeting(meeting_id, kind)
    chat = m.get("kind") == "chat"
    when = (m.get("meeting_at") or m.get("created_at") or "")[:16]
    unit = "messages" if chat else "lines"
    channel = f"  {escape(str(m.get('channel_name') or ''))}" if chat else ""
    console.print(Panel(
        f"[bold]{escape(str(m.get('title', '')))}[/bold]\n"
        f"[dim]{m['id']}[/dim]{channel}  {when}  {m.get('line_count', '?')} {unit}  "
        f"extraction: {m.get('extraction_status')}",
        expand=False,
    ))
    if not no_transcript:
        lines = str(m.get("content", "")).split("\n")
        width = len(str(len(lines))) + (1 if chat else 0)
        for i, line in enumerate(lines, 1):
            num = f"M{i}" if chat else str(i)
            console.print(f"[dim]{num:>{width}}[/dim]  {escape(line)}", soft_wrap=True)
    console.print("\n[bold]Participants[/bold]")
    _print_participants(m.get("participants") or [])
    console.print("\n[bold]Items[/bold]")
    _print_items(
        m.get("action_items") or [], m.get("participants") or [], "chat" if chat else None
    )


@meeting_app.command("delete")
@handle_errors
def delete_meeting(
    meeting_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
) -> None:
    """Delete a meeting (soft-delete; the retention sweep removes it later)."""
    delete_impl(meeting_id, yes)


def delete_impl(meeting_id: str, yes: bool, kind: str | None = None) -> None:
    if not confirm_or_exit(f"Delete {_noun(kind)} {meeting_id}?", yes=yes):
        console.print("[dim]Cancelled.[/dim]")
        raise typer.Exit(0)
    _call("DELETE", f"/api/v1/meetings/{meeting_id}", params=_kind_params(kind) or None)
    console.print(f"[green]Deleted[/green] {meeting_id}")


@meeting_app.command("extract")
@handle_errors
def extract_meeting(meeting_id: str = typer.Argument(...)) -> None:
    """Extract participants, action items and decisions with your own LLM key."""
    extract_impl(meeting_id)


def extract_impl(meeting_id: str, kind: str | None = None) -> None:
    body = _call(
        "POST", f"/api/v1/meetings/{meeting_id}/extract",
        params=_kind_params(kind) or None, timeout=_LONG_TIMEOUT,
    )
    _print_extraction_summary(body)
    _print_items(body.get("action_items") or [], body.get("participants") or [], kind)


@meeting_app.command("participants")
@handle_errors
def list_participants(meeting_id: str = typer.Argument(...)) -> None:
    """List a meeting's participants and how each was resolved."""
    participants_impl(meeting_id)


def participants_impl(meeting_id: str, kind: str | None = None) -> None:
    _print_participants(_get_meeting(meeting_id, kind).get("participants") or [])


@meeting_app.command("resolve-participant")
@handle_errors
def resolve_participant(
    meeting_id: str = typer.Argument(...),
    participant_id: str = typer.Argument(...),
    user: str | None = typer.Option(None, "--user", help="User id to resolve to."),
    persona: str | None = typer.Option(None, "--persona", help="Persona name to resolve to."),
) -> None:
    """Resolve a participant to an org member or a persona (exactly one)."""
    resolve_participant_impl(meeting_id, participant_id, user, persona)


def resolve_participant_impl(
    meeting_id: str,
    participant_id: str,
    user: str | None,
    persona: str | None,
    kind: str | None = None,
) -> None:
    if bool(user) == bool(persona):
        err_console.print("[red]Give exactly one of --user or --persona.[/red]")
        raise typer.Exit(1)
    payload = {"user_id": user} if user else {"persona_name": persona}
    body = _call(
        "PUT", f"/api/v1/meetings/{meeting_id}/participants/{participant_id}",
        json_data=payload, params=_kind_params(kind) or None,
    )
    console.print(
        f"[green]Resolved[/green] {escape(str(body.get('name_raw', participant_id)))} → "
        f"{escape(_participant_label(body))}"
    )


@meeting_app.command("action-items")
@handle_errors
def list_action_items(meeting_id: str = typer.Argument(...)) -> None:
    """List a meeting's action items, decisions."""
    action_items_impl(meeting_id)


def action_items_impl(meeting_id: str, kind: str | None = None) -> None:
    m = _get_meeting(meeting_id, kind)
    _print_items(m.get("action_items") or [], m.get("participants") or [], kind)


def _item_path(meeting_id: str, item_id: str) -> str:
    return f"/api/v1/meetings/{meeting_id}/action-items/{item_id}"


@meeting_app.command("promote")
@handle_errors
def promote_item(
    meeting_id: str = typer.Argument(...),
    item_id: str = typer.Argument(...),
    title: str | None = typer.Option(None, "--title", help="Override the drafted title."),
    description: str | None = typer.Option(None, "--description", help="Override the drafted description."),
    assignee: str | None = typer.Option(None, "--assignee", "-a", help="Persona name to assign."),
    user: str | None = typer.Option(None, "--user", help="User id to assign."),
    due: str | None = typer.Option(None, "--due", help="Due date (YYYY-MM-DD)."),
    priority: str | None = typer.Option(None, "--priority", help="critical|high|medium|low"),
    target_project: str | None = typer.Option(None, "--target-project", help="Project id for the ticket (required for a group meeting)."),
    jira: bool = typer.Option(False, "--jira", help="Also publish the ticket to Jira."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Create the ticket without asking."),
) -> None:
    """Turn an action item into a ticket. Shows the draft first and asks to confirm."""
    promote_impl(
        meeting_id, item_id, title=title, description=description, assignee=assignee,
        user=user, due=due, priority=priority, target_project=target_project, jira=jira,
        yes=yes,
    )


def promote_impl(
    meeting_id: str,
    item_id: str,
    *,
    title: str | None,
    description: str | None,
    assignee: str | None,
    user: str | None,
    due: str | None,
    priority: str | None,
    target_project: str | None,
    jira: bool,
    yes: bool,
    kind: str | None = None,
) -> None:
    kp = _kind_params(kind) or None
    if assignee and user:
        err_console.print("[red]Use either --assignee or --user, not both.[/red]")
        raise typer.Exit(1)
    api = _get_api_config()
    root = _item_path(meeting_id, item_id)
    draft = _call("POST", f"{root}/promote/preview", params=kp, api=api)

    if target_project:
        draft["target_project_id"] = target_project
    if title:
        draft["title"] = title
    if description:
        draft["description"] = description
    if due:
        draft["due_date"] = due
    if priority:
        draft["priority"] = priority
    if assignee:
        draft["assigned_to"], draft["assigned_user_id"] = assignee, None
    elif user:
        draft["assigned_to"], draft["assigned_user_id"] = None, user

    options = draft.get("target_project_options")
    if not draft.get("target_project_id"):
        listing = ", ".join(f"{o['id']} ({o['name']})" for o in options or []) or "none editable"
        err_console.print(
            f"[red]This is a group {_noun(kind)}: pass --target-project.[/red] "
            f"Choices: {escape(listing)}"
        )
        raise typer.Exit(1)

    assignee_label = draft.get("assigned_to") or draft.get("assigned_user_id") or "—"
    console.print(Panel(
        f"[bold]{escape(str(draft['title']))}[/bold]\n"
        f"priority {draft.get('priority')}  kind {draft.get('kind')}  "
        f"assignee {escape(str(assignee_label))}  due {draft.get('due_date') or '—'}\n"
        f"project {draft['target_project_id']}"
        + ("  [yellow]+ publish to Jira[/yellow]" if jira else ""),
        title="Ticket draft",
        expand=False,
    ))
    console.print(escape(str(draft.get("description", ""))))

    if not confirm_or_exit("Create this ticket?", yes=yes, yes_hint="Pass --yes to create it without a prompt."):
        console.print("[dim]Nothing created.[/dim]")
        raise typer.Exit(0)

    payload = {
        k: draft.get(k)
        for k in (
            "title", "description", "priority", "kind", "assigned_to",
            "assigned_user_id", "due_date", "target_project_id",
        )
    }
    payload["publish_to_jira"] = jira
    body = _call(
        "POST", root + "/promote", json_data=payload, params=kp, api=api,
        timeout=_LONG_TIMEOUT if jira else 30,
    )
    console.print(f"[green]Created ticket[/green] [cyan]{body['ticket_id']}[/cyan]")
    for warning in body.get("warnings") or []:
        console.print(f"[yellow]warning:[/yellow] {escape(str(warning))}")


@meeting_app.command("dismiss")
@handle_errors
def dismiss_item(
    meeting_id: str = typer.Argument(...),
    item_id: str = typer.Argument(...),
) -> None:
    """Dismiss a pending action item."""
    item_action_impl(meeting_id, item_id, "dismiss")


def item_action_impl(meeting_id: str, item_id: str, action: str, kind: str | None = None) -> None:
    """POST .../action-items/<item>/dismiss|restore."""
    _call(
        "POST", f"{_item_path(meeting_id, item_id)}/{action}",
        params=_kind_params(kind) or None,
    )
    label = "Dismissed" if action == "dismiss" else "Restored"
    console.print(f"[green]{label}[/green] {item_id}")


@meeting_app.command("restore")
@handle_errors
def restore_item(
    meeting_id: str = typer.Argument(...),
    item_id: str = typer.Argument(...),
) -> None:
    """Bring a dismissed action item back to pending."""
    item_action_impl(meeting_id, item_id, "restore")


@meeting_app.command("link")
@handle_errors
def link_item(
    meeting_id: str = typer.Argument(...),
    item_id: str = typer.Argument(...),
    ticket_id: str | None = typer.Argument(
        None, help="Existing ticket id (tk_...). Omit to use the item's suggestion."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Link without asking."),
) -> None:
    """Tie a pending action item to an EXISTING ticket. Shows the ticket and
    the comment it will add, then asks to confirm."""
    link_impl(meeting_id, item_id, ticket_id, yes=yes)


def link_impl(
    meeting_id: str,
    item_id: str,
    ticket_id: str | None,
    *,
    yes: bool,
    kind: str | None = None,
) -> None:
    kp = _kind_params(kind) or None
    api = _get_api_config()
    root = _item_path(meeting_id, item_id)
    body = {"ticket_id": ticket_id} if ticket_id else None
    preview = _call("POST", f"{root}/link/preview", json_data=body, params=kp, api=api)
    ticket = preview["ticket"]
    console.print(Panel(
        f"[cyan]{ticket['id']}[/cyan] [bold]{escape(str(ticket.get('title', '')))}[/bold]\n"
        f"status {ticket.get('status')}  project {ticket.get('project_id')}"
        + ("  [dim](suggested)[/dim]" if preview.get("suggested") else ""),
        title="Link to ticket",
        expand=False,
    ))
    console.print("[dim]Adds this comment on the ticket:[/dim]")
    console.print(escape(str(preview.get("comment", ""))))
    if not confirm_or_exit("Link this item?", yes=yes, yes_hint="Pass --yes to link without a prompt."):
        console.print("[dim]Nothing linked.[/dim]")
        raise typer.Exit(0)
    out = _call(
        "POST", f"{root}/link", json_data={"ticket_id": ticket["id"]}, params=kp, api=api
    )
    console.print(f"[green]Linked[/green] {item_id} to [cyan]{out['ticket_id']}[/cyan]")


@meeting_app.command("unlink")
@handle_errors
def unlink_item(
    meeting_id: str = typer.Argument(...),
    item_id: str = typer.Argument(...),
) -> None:
    """Put a linked action item back to pending (the ticket comment stays)."""
    unlink_impl(meeting_id, item_id)


def unlink_impl(meeting_id: str, item_id: str, kind: str | None = None) -> None:
    _call(
        "POST", f"{_item_path(meeting_id, item_id)}/unlink",
        params=_kind_params(kind) or None,
    )
    console.print(f"[green]Unlinked[/green] {item_id}")

"""`sfs ticket …` — manage agent tickets for the current project.

Commands:
- sfs ticket list [--assigned-to ...] [--status ...] [--priority ...]
- sfs ticket show <id>
- sfs ticket create --title T [--assigned-to ...] [--criteria ...]
- sfs ticket start <id> [--force] [--tool ...]
- sfs ticket complete <id> --notes N [--files ...]
- sfs ticket comment <id> --content C [--as PERSONA]
- sfs ticket comments <id>         — list all comments on a ticket
- sfs ticket watch <id> [--interval N] [--from-author X] [--exit-on-new] [--notify]
                                    — poll the ticket comments endpoint and
                                      render new comments as they appear
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
from sessionfs.cli.common import console, err_console, format_api_error, handle_errors

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
    kind: str | None = typer.Option(
        None, "--kind", "-k",
        help="Filter by kind: 'issue' (PM rollup container) or 'task' (executor work). Omit for both.",
    ),
) -> None:
    """List tickets in the current project."""
    api_url, api_key, project_id = _resolve_project()
    params = []
    for k, v in (
        ("assigned_to", assigned_to),
        ("status", status),
        ("priority", priority),
        ("kind", kind),
    ):
        if v:
            params.append(f"{k}={v}")
    suffix = ("?" + "&".join(params)) if params else ""
    s, body, _ = asyncio.run(
        _api_request(
            "GET", f"/api/v1/projects/{project_id}/tickets{suffix}", api_url, api_key
        )
    )
    if s >= 400:
        err_console.print(f"[red]API error ({s}): {format_api_error(body, s)}[/red]")
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
        err_console.print(f"[red]API error ({s}): {format_api_error(body, s)}[/red]")
        raise typer.Exit(1)

    kind = body.get("kind", "task")
    kind_label = "[magenta]issue[/magenta]" if kind == "issue" else "task"
    console.print(Panel(
        f"[bold]{body['title']}[/bold]\n"
        f"[dim]{body['id']}[/dim]  "
        f"kind: {kind_label}  "
        f"[{_status_style(body['status'])}]{body['status']}[/]  "
        f"[{_priority_style(body['priority'])}]{body['priority']}[/]  "
        f"assignee: {body.get('assigned_to') or '—'}",
        expand=False,
    ))
    parent_id = body.get("parent_ticket_id")
    if parent_id:
        console.print(f"[dim]Parent Issue:[/dim] [magenta]{parent_id}[/magenta]")
    children = body.get("child_ticket_ids") or []
    if children:
        console.print(f"\n[bold]Child tasks ({len(children)}):[/bold]")
        for c in children:
            console.print(f"  - {c}")
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
    kind: str = typer.Option(
        "task", "--kind", "-k",
        help="'task' (default) = executor work. 'issue' = PM rollup container (Compass/owner only).",
    ),
    parent: str | None = typer.Option(
        None, "--parent",
        help="Optional parent Issue id (only valid when --kind task). Parent must be kind='issue' in this project.",
    ),
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
        "kind": kind,
    }
    if assigned_to:
        payload["assigned_to"] = assigned_to
    if parent:
        payload["parent_ticket_id"] = parent
    s, body, _ = asyncio.run(
        _api_request(
            "POST", f"/api/v1/projects/{project_id}/tickets", api_url, api_key,
            json_data=payload,
        )
    )
    if s >= 400 or not isinstance(body, dict):
        err_console.print(f"[red]API error ({s}): {format_api_error(body, s)}[/red]")
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
        err_console.print(f"[red]API error ({s}): {format_api_error(body, s)}[/red]")
        raise typer.Exit(1)

    ticket = body.get("ticket", {})
    bundle_ok = write_bundle(
        ticket_id=ticket_id,
        persona_name=ticket.get("assigned_to"),
        project_id=project_id,
        lease_epoch=ticket.get("lease_epoch") if isinstance(ticket.get("lease_epoch"), int) else None,
        retrieval_audit_id=(
            body.get("retrieval_audit_id")
            if isinstance(body.get("retrieval_audit_id"), str)
            else None
        ),
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
    lease_epoch: int | None = typer.Option(
        None, "--lease-epoch", help="Fence stale ticket workers"
    ),
) -> None:
    """Mark a ticket complete. Moves to review; removes the active bundle."""
    api_url, api_key, project_id = _resolve_project()
    payload = {
        "notes": notes,
        "changed_files": list(file_changed),
        "knowledge_entry_ids": list(knowledge_entry_id),
    }
    if lease_epoch is None:
        bundle = read_bundle()
        if (
            isinstance(bundle, dict)
            and bundle.get("ticket_id") == ticket_id
            and bundle.get("project_id") == project_id
            and isinstance(bundle.get("lease_epoch"), int)
        ):
            lease_epoch = bundle["lease_epoch"]
    if lease_epoch is not None:
        payload["lease_epoch"] = lease_epoch
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
        err_console.print(f"[red]API error ({s}): {format_api_error(body, s)}[/red]")
        raise typer.Exit(1)

    cleared = clear_bundle_if_owned(ticket_id=ticket_id, project_id=project_id)
    if cleared:
        console.print(f"[green]Completed {ticket_id}.[/green] Active bundle cleared.")
    else:
        console.print(
            f"[green]Completed {ticket_id}.[/green] "
            "[dim]Active bundle untouched (points at a different ticket).[/dim]"
        )


@ticket_app.command("update")
@handle_errors
def update_ticket_cmd(
    ticket_id: str = typer.Argument(...),
    title: str | None = typer.Option(None, "--title", help="New title"),
    description: str | None = typer.Option(
        None, "--description", help="New description (use --description-file for long markdown)"
    ),
    description_file: str | None = typer.Option(
        None, "--description-file",
        help="Path to a file whose contents become the new description (avoids shell-quoting for long markdown)",
    ),
    priority: str | None = typer.Option(
        None, "--priority", help="low | medium | high | critical (use `escalate` for one-step audit-tagged bumps)"
    ),
    assigned_to: str | None = typer.Option(
        None, "--assigned-to", "-a", help="Reassign to persona"
    ),
    acceptance_criteria_file: str | None = typer.Option(
        None, "--acceptance-criteria-file",
        help="Path to a file with one acceptance-criterion per line; replaces the full list",
    ),
    lease_epoch: int | None = typer.Option(
        None, "--lease-epoch",
        help="Fence stale ticket workers (read from `sfs ticket show`); 409 if stale",
    ),
) -> None:
    """Patch mutable ticket fields.

    tk_835a876529de4551. Every successful update auto-posts a system
    diff comment on the ticket and writes audit rows to `ticket_edits`.
    Status transitions go through the lifecycle commands (`start` /
    `complete` / `resolve` / etc.) — NOT this verb.
    """
    if description is not None and description_file is not None:
        err_console.print(
            "[red]Pass either --description or --description-file, not both.[/red]"
        )
        raise typer.Exit(2)

    api_url, api_key, project_id = _resolve_project()
    payload: dict = {}
    if title is not None:
        payload["title"] = title
    if description is not None:
        payload["description"] = description
    elif description_file is not None:
        try:
            with open(description_file, encoding="utf-8") as f:
                payload["description"] = f.read()
        except OSError as exc:
            err_console.print(f"[red]Could not read --description-file: {exc}[/red]")
            raise typer.Exit(2) from exc
    if priority is not None:
        payload["priority"] = priority
    if assigned_to is not None:
        payload["assigned_to"] = assigned_to
    if acceptance_criteria_file is not None:
        try:
            with open(acceptance_criteria_file, encoding="utf-8") as f:
                criteria = [
                    line.rstrip("\n").strip()
                    for line in f
                    if line.strip()
                ]
            payload["acceptance_criteria"] = criteria
        except OSError as exc:
            err_console.print(
                f"[red]Could not read --acceptance-criteria-file: {exc}[/red]"
            )
            raise typer.Exit(2) from exc

    # If caller didn't pass --lease-epoch, opportunistically pull it
    # from the active-ticket bundle so reasonable workflows get
    # fenced edits for free — matches `sfs ticket comment` semantics.
    if lease_epoch is None:
        bundle = read_bundle()
        if (
            isinstance(bundle, dict)
            and bundle.get("ticket_id") == ticket_id
            and bundle.get("project_id") == project_id
            and isinstance(bundle.get("lease_epoch"), int)
        ):
            lease_epoch = bundle["lease_epoch"]
    if lease_epoch is not None:
        payload["lease_epoch"] = lease_epoch

    if not payload or set(payload.keys()) == {"lease_epoch"}:
        err_console.print(
            "[red]At least one mutable field is required "
            "(--title / --description[-file] / --priority / --assigned-to / "
            "--acceptance-criteria-file).[/red]"
        )
        raise typer.Exit(2)

    s, body, _ = asyncio.run(
        _api_request(
            "PUT",
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}",
            api_url, api_key, json_data=payload,
        )
    )
    if s == 404:
        err_console.print(f"[red]Ticket '{ticket_id}' not found.[/red]")
        raise typer.Exit(1)
    if s == 403:
        err_console.print(
            "[red]Not authorized to update this ticket "
            "(only creator or project admin can edit).[/red]"
        )
        raise typer.Exit(1)
    if s == 409:
        err_console.print(
            f"[red]Stale lease_epoch — ticket has been updated since you "
            f"read it. Re-run `sfs ticket show {ticket_id}` and retry.[/red]"
        )
        raise typer.Exit(1)
    if s >= 400 or not isinstance(body, dict):
        err_console.print(f"[red]API error ({s}): {format_api_error(body, s)}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Updated {ticket_id}.[/green]")


@ticket_app.command("comment")
@handle_errors
def comment_ticket(
    ticket_id: str = typer.Argument(...),
    content: str = typer.Option(..., "--content", "-c"),
    as_persona: str | None = typer.Option(None, "--as", help="Attribute to persona"),
    lease_epoch: int | None = typer.Option(
        None, "--lease-epoch", help="Fence stale ticket workers"
    ),
) -> None:
    """Add a comment to a ticket."""
    api_url, api_key, project_id = _resolve_project()
    payload: dict = {"content": content}
    if as_persona:
        payload["author_persona"] = as_persona
    if lease_epoch is None:
        bundle = read_bundle()
        if (
            isinstance(bundle, dict)
            and bundle.get("ticket_id") == ticket_id
            and bundle.get("project_id") == project_id
            and isinstance(bundle.get("lease_epoch"), int)
        ):
            lease_epoch = bundle["lease_epoch"]
    if lease_epoch is not None:
        payload["lease_epoch"] = lease_epoch
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
        err_console.print(f"[red]API error ({s}): {format_api_error(body, s)}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Commented on {ticket_id}.[/green]")


def _render_comment(c: dict) -> None:
    """Render a single ticket comment as a titled Panel with markdown body."""
    author = c.get("author_persona") or c.get("author_user_id") or "?"
    created = c.get("created_at", "")
    console.print(
        Panel(
            Markdown(c.get("content", "")),
            title=f"{author} — {created}",
            title_align="left",
        )
    )


@ticket_app.command("comments")
@handle_errors
def list_ticket_comments(
    ticket_id: str = typer.Argument(...),
) -> None:
    """List all comments on a ticket, chronological."""
    api_url, api_key, project_id = _resolve_project()
    s, body, _ = asyncio.run(
        _api_request(
            "GET",
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}/comments",
            api_url,
            api_key,
        )
    )
    if s == 404:
        err_console.print(f"[red]Ticket '{ticket_id}' not found.[/red]")
        raise typer.Exit(1)
    if s >= 400 or not isinstance(body, list):
        err_console.print(f"[red]API error ({s}): {format_api_error(body, s)}[/red]")
        raise typer.Exit(1)
    if not body:
        console.print(f"[dim]No comments on {ticket_id}.[/dim]")
        return
    for c in body:
        _render_comment(c)


def _clamp_interval(seconds: int) -> int:
    """Clamp the watch poll interval to [5, 300] seconds."""
    return max(5, min(seconds, 300))


def _notify_macos(title: str, message: str) -> None:
    """Best-effort macOS notification via terminal-notifier. Silently
    no-ops if terminal-notifier isn't installed."""
    import shutil
    import subprocess

    notifier = shutil.which("terminal-notifier")
    if notifier is None:
        return
    try:
        subprocess.run(
            [notifier, "-title", title, "-message", message],
            check=False,
            timeout=2,
        )
    except Exception:
        pass


# v0.10.24 tk_dbccde26ed604b3c — Issues terminate at 'closed' (NOT
# 'done'). Without 'closed' in this set, `sfs ticket watch --until-
# closed` against an Issue would never exit after the Issue closes.
TERMINAL_TICKET_STATUSES = {"done", "cancelled", "closed"}


@ticket_app.command("watch")
@handle_errors
def watch_ticket(
    ticket_id: str = typer.Argument(...),
    interval: int = typer.Option(
        30, "--interval", "-i",
        help="Poll cadence in seconds (clamped to [5, 300]). Default 30.",
    ),
    from_author: str | None = typer.Option(
        None, "--from-author",
        help="Only render comments where author_persona matches (e.g. codex-reviewer).",
    ),
    exit_on_new: bool = typer.Option(
        False, "--exit-on-new",
        help="Exit 0 after the first new comment lands (for CI scripting).",
    ),
    until_closed: bool = typer.Option(
        False, "--until-closed",
        help=(
            "Keep watching until the ticket reaches a terminal status "
            "(done, cancelled, or closed for Issues), then exit. Natural "
            "for multi-round review threads where you want to keep seeing "
            "follow-up Codex comments until the work is resolved."
        ),
    ),
    notify: bool = typer.Option(
        False, "--notify",
        help="Send an OS notification on new comments (macOS terminal-notifier).",
    ),
) -> None:
    """Poll a ticket's comments endpoint and render new comments as they appear.

    Initial poll renders all existing comments. Subsequent polls diff
    against the seen-id set and only render NEW comments. Process-local
    seen-id state is lost on restart.

    Exit conditions (any of):
    - Ctrl-C
    - `--exit-on-new` (one-shot: exit after first new matching comment)
    - `--until-closed` (exit when ticket.status hits done or cancelled)

    `--exit-on-new` and `--until-closed` are mutually compatible; whichever
    condition trips first wins.
    """
    interval = _clamp_interval(interval)
    api_url, api_key, project_id = _resolve_project()

    banner_bits = [f"interval={interval}s"]
    if from_author:
        banner_bits.append(f"from-author={from_author}")
    if exit_on_new:
        banner_bits.append("exit-on-new")
    if until_closed:
        banner_bits.append("until-closed")
    banner_bits.append("ctrl-C to stop")
    console.print(
        f"[dim]Watching {ticket_id} ({', '.join(banner_bits)})[/dim]"
    )

    seen: set[str] = set()
    saw_any_new = False
    new_count = 0
    exit_reason: str | None = None

    async def _poll_comments() -> list[dict] | None:
        s, body, _ = await _api_request(
            "GET",
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}/comments",
            api_url,
            api_key,
        )
        if s == 404:
            err_console.print(f"[red]Ticket '{ticket_id}' not found.[/red]")
            raise typer.Exit(1)
        if s >= 400 or not isinstance(body, list):
            err_console.print(f"[red]API error ({s}): {format_api_error(body, s)}[/red]")
            return None
        return body

    async def _poll_ticket_status() -> str | None:
        """Fetch ticket status for --until-closed termination check."""
        s, body, _ = await _api_request(
            "GET",
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}",
            api_url,
            api_key,
        )
        if s >= 400 or not isinstance(body, dict):
            return None
        status = body.get("status")
        return status if isinstance(status, str) else None

    async def _run() -> None:
        nonlocal saw_any_new, new_count, exit_reason
        first = True
        while True:
            comments = await _poll_comments()
            if comments is None:
                await asyncio.sleep(interval)
                continue
            for c in comments:
                cid = c.get("id")
                if not isinstance(cid, str):
                    continue
                if cid in seen:
                    continue
                seen.add(cid)
                if from_author and c.get("author_persona") != from_author:
                    # Still mark as seen so we don't re-evaluate it.
                    continue
                if not first:
                    console.print("\a", end="")  # terminal bell
                    new_count += 1
                    saw_any_new = True
                    if notify:
                        author = c.get("author_persona") or "?"
                        _notify_macos(
                            f"New ticket comment — {ticket_id}",
                            f"From {author}",
                        )
                _render_comment(c)
            first = False
            if exit_on_new and saw_any_new:
                exit_reason = "first-new-comment"
                return
            if until_closed:
                status = await _poll_ticket_status()
                if status in TERMINAL_TICKET_STATUSES:
                    console.print(
                        f"\n[dim]Ticket {ticket_id} reached terminal status "
                        f"'{status}' — exiting.[/dim]"
                    )
                    exit_reason = f"ticket-{status}"
                    return
            await asyncio.sleep(interval)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    finally:
        console.print(
            f"\n[dim]Stopped watching {ticket_id} — saw {new_count} new "
            f"comment(s) during this session.[/dim]"
        )


@ticket_app.command("review-state")
@handle_errors
def ticket_review_state(
    ticket_id: str = typer.Argument(..., help="Ticket id (review tickets only)"),
) -> None:
    """Show a compact summary of a review ticket's open + closed findings.

    Derived from the comment thread server-side — no LLM, no scraping.
    Returns "no review state" for tickets without any codex-reviewer
    comments.
    """
    api_url, api_key, project_id = _resolve_project()

    async def _fetch() -> dict | None:
        status, body, _ = await _api_request(
            "GET",
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}/review-state",
            api_url,
            api_key,
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

    state = payload.get("review_state")
    if not state:
        console.print(
            f"[dim]No review state for {ticket_id} — ticket has no "
            f"codex-reviewer comments yet.[/dim]"
        )
        return

    # Header: verdict + severity counts of OPEN findings.
    verdict = state.get("last_verdict") or "—"
    sev = state.get("severity_counts") or {}
    open_total = sum(int(v) for v in sev.values())
    verdict_style = "green" if verdict == "VERIFIED-CLEAN" else "yellow"
    sev_bits = []
    for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        n = int(sev.get(s, 0))
        if n:
            color = {"CRITICAL": "red", "HIGH": "red", "MEDIUM": "yellow", "LOW": "dim"}[s]
            sev_bits.append(f"[{color}]{s}={n}[/{color}]")
    sev_line = " ".join(sev_bits) if sev_bits else "[green]no open findings[/green]"

    console.print(Panel(
        f"[bold {verdict_style}]{verdict}[/bold {verdict_style}]   "
        f"{sev_line}   "
        f"[dim]open={open_total}, closed={len(state.get('closed_findings') or [])}, "
        f"rounds={len(state.get('rounds') or [])}[/dim]",
        title=f"Review state — {ticket_id}",
        title_align="left",
    ))

    rounds = state.get("rounds") or []
    if rounds:
        table = Table(title="Rounds")
        table.add_column("R")
        table.add_column("Verdict")
        table.add_column("Timestamp")
        table.add_column("Raised")
        for r in rounds:
            v = r.get("verdict") or "—"
            style = "green" if v == "VERIFIED-CLEAN" else "yellow"
            table.add_row(
                f"R{r.get('round')}",
                f"[{style}]{v}[/{style}]",
                str(r.get("timestamp") or ""),
                str(r.get("findings_raised") or 0),
            )
        console.print(table)

    open_findings = state.get("open_findings") or []
    if open_findings:
        console.print("\n[bold yellow]Open findings:[/bold yellow]")
        for f in open_findings:
            sev_str = f.get("severity") or "?"
            color = {"CRITICAL": "red", "HIGH": "red", "MEDIUM": "yellow", "LOW": "dim"}.get(sev_str, "")
            text = f.get("text") or ""
            console.print(
                f"  • [{color}]{sev_str}[/{color}] (R{f.get('round')}) {text}"
            )

    closed_findings = state.get("closed_findings") or []
    if closed_findings:
        console.print("\n[bold green]Closed findings:[/bold green]")
        for f in closed_findings:
            sev_str = f.get("severity") or "?"
            text = f.get("text") or ""
            console.print(
                f"  • [dim]{sev_str}[/dim] (R{f.get('round')} → R{f.get('closed_round')}) {text}"
            )


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
        err_console.print(f"[red]API error ({s}): {format_api_error(body, s)}[/red]")
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


@ticket_app.command("close")
@handle_errors
def close_issue_cmd(ticket_id: str = typer.Argument(...)) -> None:
    """Move an Issue from in_progress → closed.

    Issue-only terminator. Tasks finish via complete + accept; Issues
    finish via close. Manual close by the PM — NOT auto-derived from
    child Task status.
    """
    _post_transition(ticket_id, "close", "Closed")


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
        err_console.print(f"[red]API error ({s}): {format_api_error(body, s)}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Assigned {ticket_id} to {persona}.[/green]")


@ticket_app.command("resolve")
@handle_errors
def resolve_ticket_cmd(
    ticket_id: str = typer.Argument(...),
    lease_epoch: int | None = typer.Option(
        None, "--lease-epoch", help="Fence stale ticket workers"
    ),
) -> None:
    """Mark a ticket resolved (review → done). Triggers dependency
    enrichment + auto-unblock for tickets waiting on this one.
    """
    api_url, api_key, project_id = _resolve_project()
    suffix = ""
    if lease_epoch is None:
        bundle = read_bundle()
        if (
            isinstance(bundle, dict)
            and bundle.get("ticket_id") == ticket_id
            and bundle.get("project_id") == project_id
            and isinstance(bundle.get("lease_epoch"), int)
        ):
            lease_epoch = bundle["lease_epoch"]
    if lease_epoch is not None:
        suffix = f"?lease_epoch={lease_epoch}"
    s, body, _ = asyncio.run(
        _api_request(
            "POST",
            f"/api/v1/projects/{project_id}/tickets/{ticket_id}/accept{suffix}",
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
        err_console.print(f"[red]API error ({s}): {format_api_error(body, s)}[/red]")
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
        err_console.print(f"[red]API error ({s}): {format_api_error(body, s)}[/red]")
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

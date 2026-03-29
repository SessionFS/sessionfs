"""CLI command: sfs summary."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from sessionfs.cli.common import (
    console,
    err_console,
    get_session_dir_or_exit,
    open_store,
    resolve_session_id,
)

summary_app = typer.Typer(name="summary", help="Session summaries.", no_args_is_help=False)


@summary_app.callback(invoke_without_command=True)
def summary_default(
    ctx: typer.Context,
    session_id: str = typer.Argument(None, help="Session ID or prefix"),
    fmt: str | None = typer.Option(None, "--format", help="Export format: md"),
    today: bool = typer.Option(False, "--today", help="Summarize all sessions from today"),
) -> None:
    """Show a session summary."""
    if ctx.invoked_subcommand is not None:
        return

    if today:
        _summary_today(fmt)
        return

    if not session_id:
        err_console.print("[dim]Usage: sfs summary <session_id> or sfs summary --today[/dim]")
        raise typer.Exit(0)

    store = open_store()
    try:
        full_id = resolve_session_id(store, session_id)
        session_dir = get_session_dir_or_exit(store, full_id)
    finally:
        store.close()

    # Generate deterministic summary from local data
    from sessionfs.server.services.summarizer import summarize_session

    messages = _read_messages(session_dir / "messages.jsonl")
    manifest = _read_json(session_dir / "manifest.json")
    workspace = _read_json(session_dir / "workspace.json")

    if not messages:
        err_console.print("[yellow]No messages found in session.[/yellow]")
        raise typer.Exit(1)

    summary = summarize_session(messages, manifest, workspace)

    if fmt == "md":
        _print_markdown(summary)
    else:
        _print_rich(summary)


def _read_messages(path: Path) -> list[dict]:
    if not path.exists():
        return []
    messages = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                messages.append(json.loads(line))
    return messages


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _print_rich(s) -> None:
    console.print(f"[bold]{s.title}[/bold]")

    duration = f"{s.duration_minutes}m" if s.duration_minutes < 60 else f"{s.duration_minutes / 60:.1f}h"
    console.print(f"{duration} | {s.message_count} msgs | {s.tool_call_count} tool calls | {s.tool or 'unknown'}")

    if s.branch:
        console.print(f"Branch: {s.branch}" + (f" @ {s.commit}" if s.commit else ""))

    console.print()

    if s.files_modified:
        console.print(f"[bold]Files modified ({len(s.files_modified)}):[/bold]")
        for f in s.files_modified[:10]:
            console.print(f"  {f}")
        if len(s.files_modified) > 10:
            console.print(f"  + {len(s.files_modified) - 10} more")

    if s.files_read:
        console.print(f"[bold]Files read ({len(s.files_read)}):[/bold]")
        for f in s.files_read[:5]:
            console.print(f"  {f}")
        if len(s.files_read) > 5:
            console.print(f"  + {len(s.files_read) - 5} more")

    console.print()
    console.print(f"Commands: {s.commands_executed}")
    if s.tests_run:
        color = "green" if s.tests_failed == 0 else "yellow" if s.tests_failed < s.tests_run else "red"
        console.print(f"Tests: {s.tests_run} runs ([{color}]{s.tests_passed} passed, {s.tests_failed} failed[/{color}])")
    if s.packages_installed:
        console.print(f"Packages: {', '.join(s.packages_installed)}")
    if s.errors_encountered:
        console.print(f"[red]Errors: {len(s.errors_encountered)} unique[/red]")


def _print_markdown(s) -> None:
    import sys

    duration = f"{s.duration_minutes}m" if s.duration_minutes < 60 else f"{s.duration_minutes / 60:.1f}h"
    lines = [
        f"# {s.title}",
        "",
        f"**Duration:** {duration} | **Messages:** {s.message_count} | **Tool calls:** {s.tool_call_count} | **Tool:** {s.tool or 'unknown'}",
    ]
    if s.branch:
        lines.append(f"**Branch:** {s.branch}" + (f" @ {s.commit}" if s.commit else ""))
    lines.append("")

    if s.files_modified:
        lines.append(f"## Files modified ({len(s.files_modified)})")
        for f in s.files_modified:
            lines.append(f"- `{f}`")
        lines.append("")

    if s.files_read:
        lines.append(f"## Files read ({len(s.files_read)})")
        for f in s.files_read[:10]:
            lines.append(f"- `{f}`")
        lines.append("")

    lines.append("## Activity")
    lines.append(f"- Commands executed: {s.commands_executed}")
    if s.tests_run:
        lines.append(f"- Tests: {s.tests_passed} passed, {s.tests_failed} failed")
    if s.packages_installed:
        lines.append(f"- Packages: {', '.join(s.packages_installed)}")
    if s.errors_encountered:
        lines.append(f"- Errors: {len(s.errors_encountered)} unique")

    sys.stdout.write("\n".join(lines) + "\n")


def _summary_today(fmt: str | None) -> None:
    """Summarize all sessions captured today."""
    from datetime import datetime, timezone

    from rich.table import Table

    from sessionfs.server.services.summarizer import summarize_session

    store = open_store()
    try:
        sessions = store.list_sessions()
    finally:
        store.close()

    today_local = datetime.now().strftime("%Y-%m-%d")
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    today_sessions = []
    for s in sessions:
        # Check both created_at and updated_at, match either local or UTC date
        for date_field in ("created_at", "updated_at"):
            date_val = s.get(date_field, "")
            if isinstance(date_val, str) and (date_val.startswith(today_local) or date_val.startswith(today_utc)):
                today_sessions.append(s)
                break

    if not today_sessions:
        console.print(f"[dim]No sessions captured today ({today_local}). Is the daemon running?[/dim]")
        return

    table = Table(title=f"Today's Sessions ({len(today_sessions)})")
    table.add_column("ID", style="cyan", width=14)
    table.add_column("Tool")
    table.add_column("Messages", justify="right")
    table.add_column("Tools", justify="right")
    table.add_column("Title", max_width=40)

    for s in today_sessions:
        sid = s.get("session_id", "")
        sfs_dir = store._store_dir / "sessions" / f"{sid}.sfs"
        msgs = _read_messages(sfs_dir / "messages.jsonl") if sfs_dir.exists() else []
        summary = summarize_session(msgs, _read_json(sfs_dir / "manifest.json")) if msgs else None

        table.add_row(
            sid[:14],
            s.get("source_tool", "?"),
            str(s.get("message_count", 0)),
            str(summary.tool_call_count if summary else 0),
            (s.get("title") or "Untitled")[:40],
        )

    console.print(table)

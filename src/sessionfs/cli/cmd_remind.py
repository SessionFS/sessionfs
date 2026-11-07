"""`sfs remind …` — personal reminders (tk_8a858d6354fc4ef3).

Commands:
- sfs remind add "title" --at "2026-11-06 09:00" [--tz Europe/London]
                 [--meeting MEETING_ID[:LINES]] [--ticket TK] [--for EMAIL]
- sfs remind list [--all | --done] [--due-before TIME]
- sfs remind show <id>
- sfs remind note <id> "text"
- sfs remind snooze <id> 1h|tomorrow|next-week | --until TIME
- sfs remind done <id>
- sfs remind dismiss <id>

A thin client of ``/api/v1/me/reminders``: privacy, source access, DLP and
the due-time bounds are enforced server-side.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer
from rich.markup import escape
from rich.table import Table

from sessionfs.cli.cmd_rules import _api_request, _get_api_config
from sessionfs.cli.common import console, err_console, format_api_error, handle_errors

remind_app = typer.Typer(
    name="remind",
    help="Personal reminders, optionally tied to a meeting or a ticket.",
    no_args_is_help=True,
)

_BASE = "/api/v1/me/reminders"


def _item(reminder_id: str) -> str:
    """The reminder's URL path, with the id as one encoded path segment."""
    return f"{_BASE}/{quote(reminder_id.strip(), safe='')}"

_MEETING_RE = re.compile(r"^(?P<id>[A-Za-z0-9_-]{1,64})(?::(?P<lines>\d{1,6}(?:-\d{1,6})?))?$")
_SNOOZE_PRESETS = {"1h": "1h", "tomorrow": "tomorrow", "next-week": "next_week", "next_week": "next_week"}
_TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")


def _fail(status: int, body: Any) -> None:
    if status == 404:
        err_console.print("[red]Reminder not found.[/red]")
    else:
        err_console.print(f"[red]API error ({status}): {format_api_error(body, status)}[/red]")
    raise typer.Exit(1)


def _call(method: str, path: str, *, json_data: dict | None = None, params: dict | None = None) -> Any:
    api_url, api_key = _get_api_config()
    status, body, _ = asyncio.run(
        _api_request(method, path, api_url, api_key, json_data=json_data, params=params)
    )
    if status >= 400:
        _fail(status, body)
    return body


def _local_zone_name() -> str | None:
    """The machine's IANA zone name when it can be found, else None."""
    env = os.environ.get("TZ", "").lstrip(":")
    candidates = [env] if env else []
    try:
        link = Path("/etc/localtime").resolve()
        if "zoneinfo" in link.parts:
            idx = link.parts.index("zoneinfo")
            candidates.append("/".join(link.parts[idx + 1 :]))
    except OSError:
        pass
    for name in candidates:
        try:
            ZoneInfo(name)
            return name
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return None


def parse_at(value: str, tz: str | None) -> tuple[str, str | None]:
    """Turn ``--at`` (+ ``--tz``) into (ISO 8601 due_at, IANA timezone or None).

    ``YYYY-MM-DD HH:MM`` (or ``T``, optional seconds, or a bare date meaning
    09:00) is local to ``--tz`` when given, else to this machine. A value that
    already carries an offset (``…Z`` / ``…+01:00``) is sent unchanged.
    """
    raw = value.strip()
    if tz is not None:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise typer.BadParameter(f"unknown timezone {tz!r} (use an IANA name like Europe/London)") from None
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for fmt in _TIME_FORMATS:
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise typer.BadParameter(f"could not read {value!r}; use e.g. \"2026-11-06 09:00\"")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        parsed = parsed.replace(hour=9, minute=0)
    if parsed.tzinfo is not None:
        return parsed.isoformat(), tz
    if tz is not None:
        return parsed.isoformat(), tz
    local_name = _local_zone_name()
    if local_name is not None:
        return parsed.isoformat(), local_name
    # No IANA name for this machine: send the local offset explicitly.
    return parsed.astimezone().isoformat(), None


def parse_at_aware(value: str, tz: str | None) -> str:
    """``parse_at`` resolved to an ISO 8601 value with an explicit offset, for
    fields the server reads without a timezone (``until``, ``due_before``)."""
    iso, zone = parse_at(value, tz)
    parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(zone)) if zone else parsed.astimezone()
    return parsed.isoformat()


def _when(r: dict) -> str:
    due = r.get("due_at") or ""
    tz = r.get("timezone")
    if tz and due:
        try:
            local = datetime.fromisoformat(due.replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
            return local.strftime("%a %d %b %Y, %H:%M ") + tz
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return due


def _source_label(r: dict) -> str:
    src = r.get("source")
    if src:
        label = f"{src.get('kind')}:{src.get('id')}"
        if src.get("lines"):
            label += f" L{src['lines']}"
        return label
    return "(hidden)" if r.get("source_hidden") else "—"


def _print_one(r: dict) -> None:
    status = r.get("status", "")
    if status == "open" and r.get("is_due"):
        status = "open (due)"
    console.print(f"[bold]{escape(str(r.get('title', '')))}[/bold]  [cyan]{r.get('id', '')}[/cyan]")
    console.print(f"  Due:     {_when(r)}")
    console.print(f"  Status:  {status}")
    console.print(f"  Source:  {escape(_source_label(r))}")
    if r.get("linked_ticket_id"):
        console.print(f"  Ticket:  {r['linked_ticket_id']}")
    if r.get("snooze_count"):
        console.print(f"  Snoozed: {r['snooze_count']} time(s)")


@remind_app.command("add")
@handle_errors
def add(
    title: str = typer.Argument(..., help="What to be reminded about."),
    at: str = typer.Option(..., "--at", help='When, e.g. "2026-11-06 09:00".'),
    tz: str | None = typer.Option(None, "--tz", help="IANA timezone, e.g. Europe/London."),
    meeting: str | None = typer.Option(None, "--meeting", help="MEETING_ID or MEETING_ID:LINES (e.g. 12-18)."),
    ticket: str | None = typer.Option(None, "--ticket", help="Ticket id to tie the reminder to."),
    for_: str | None = typer.Option(None, "--for", help="Remind a teammate (email) who can read the source."),
) -> None:
    """Create a reminder."""
    if meeting and ticket:
        raise typer.BadParameter("use either --meeting or --ticket, not both")
    due_at, zone = parse_at(at, tz)
    body: dict[str, Any] = {"title": title, "due_at": due_at}
    if zone:
        body["timezone"] = zone
    if meeting:
        m = _MEETING_RE.match(meeting.strip())
        if not m:
            raise typer.BadParameter("--meeting must look like MEETING_ID or MEETING_ID:12-18")
        source: dict[str, Any] = {"kind": "meeting", "id": m.group("id")}
        lines = m.group("lines")
        if lines:
            source["lines"] = lines if "-" in lines else f"{lines}-{lines}"
        body["source"] = source
    elif ticket:
        body["source"] = {"kind": "ticket", "id": ticket.strip()}
    if for_:
        body["owner"] = for_.strip()
    r = _call("POST", _BASE, json_data=body)
    console.print(f"[green]Reminder set[/green] {r.get('id', '')} for {_when(r)}")


@remind_app.command("list")
@handle_errors
def list_(
    all_: bool = typer.Option(False, "--all", help="Open and closed reminders."),
    done: bool = typer.Option(False, "--done", help="Only reminders marked done."),
    due_before: str | None = typer.Option(None, "--due-before", help="Only those due before this time."),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
) -> None:
    """List your reminders, earliest due first."""
    if all_ and done:
        raise typer.BadParameter("use either --all or --done")
    params: dict[str, Any] = {"status": "all" if all_ else "done" if done else "open", "limit": limit}
    if due_before:
        params["due_before"] = parse_at_aware(due_before, None)
    body = _call("GET", _BASE, params=params)
    items = body.get("items") or []
    if not items:
        console.print("[dim]No reminders.[/dim]")
        return
    table = Table()
    table.add_column("ID", style="cyan")
    table.add_column("Due")
    table.add_column("Status")
    table.add_column("Title")
    table.add_column("Source", style="dim")
    for r in items:
        status = r.get("status", "")
        if status == "open" and r.get("is_due"):
            status = "[yellow]due[/yellow]"
        table.add_row(r.get("id", ""), _when(r), status, escape(str(r.get("title", ""))), escape(_source_label(r)))
    console.print(table)


@remind_app.command("show")
@handle_errors
def show(reminder_id: str = typer.Argument(...)) -> None:
    """Show a reminder and its notes."""
    r = _call("GET", _item(reminder_id))
    _print_one(r)
    notes = (_call("GET", f"{_item(reminder_id)}/notes") or {}).get("items") or []
    if notes:
        console.print("  Notes:")
        for n in notes:
            console.print(f"   [dim]{(n.get('created_at') or '')[:16]}[/dim] {escape(str(n.get('body', '')))}")


@remind_app.command("note")
@handle_errors
def note(reminder_id: str = typer.Argument(...), text: str = typer.Argument(...)) -> None:
    """Add a note to a reminder."""
    _call("POST", f"{_item(reminder_id)}/notes", json_data={"body": text})
    console.print("[green]Note added.[/green]")


@remind_app.command("snooze")
@handle_errors
def snooze(
    reminder_id: str = typer.Argument(...),
    preset: str | None = typer.Argument(None, help="1h, tomorrow or next-week."),
    until: str | None = typer.Option(None, "--until", help="Snooze until this time instead."),
    tz: str | None = typer.Option(None, "--tz", help="Timezone for --until."),
) -> None:
    """Push a reminder later."""
    if (preset is None) == (until is None):
        raise typer.BadParameter("pass one of 1h, tomorrow, next-week or --until TIME")
    if until is not None:
        body: dict[str, Any] = {"preset": "custom", "until": parse_at_aware(until, tz)}
    else:
        key = _SNOOZE_PRESETS.get((preset or "").lower())
        if key is None:
            raise typer.BadParameter("snooze must be 1h, tomorrow or next-week")
        body = {"preset": key}
    r = _call("POST", f"{_item(reminder_id)}/snooze", json_data=body)
    console.print(f"[green]Snoozed[/green] until {_when(r)}")


def _close(reminder_id: str, status: str) -> None:
    _call("PATCH", _item(reminder_id), json_data={"status": status})
    console.print(f"[green]Reminder {status}.[/green]")


@remind_app.command("done")
@handle_errors
def done_(reminder_id: str = typer.Argument(...)) -> None:
    """Close a reminder as done."""
    _close(reminder_id, "done")


@remind_app.command("dismiss")
@handle_errors
def dismiss(reminder_id: str = typer.Argument(...)) -> None:
    """Close a reminder without doing it."""
    _close(reminder_id, "dismissed")

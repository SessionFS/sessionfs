"""`sfs attention` — what needs you right now (For You, tk_dc118ffd6e804a9a).

    sfs attention [--band now|your_turn|upcoming|lately] [--limit N] [--json]

A thin client of ``GET /api/v1/me/for-you``: ranking, access and the
per-source rules all live on the server.
"""

from __future__ import annotations

import asyncio
import json
from enum import Enum
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from sessionfs.cli.cmd_rules import _api_request, _get_api_config
from sessionfs.cli.common import console, err_console, format_api_error

_PATH = "/api/v1/me/for-you"
_BAND_LABELS = {"now": "Now", "your_turn": "Your turn", "upcoming": "Coming up", "lately": "Lately"}


class Band(str, Enum):
    now = "now"
    your_turn = "your_turn"
    upcoming = "upcoming"
    lately = "lately"


def _fetch(params: dict[str, Any]) -> dict[str, Any]:
    api_url, api_key = _get_api_config()
    status, body, _ = asyncio.run(_api_request("GET", _PATH, api_url, api_key, params=params))
    if status >= 400:
        err_console.print(f"[red]API error ({status}): {format_api_error(body, status)}[/red]")
        raise typer.Exit(1)
    return body if isinstance(body, dict) else {}


def _what(it: dict[str, Any]) -> str:
    """The title, tagged ``[chat M3-5]`` / ``[meeting L3-5]`` for a
    conversation action item."""
    title = str(it.get("title", ""))
    ctx = it.get("context") if isinstance(it.get("context"), dict) else {}
    conv = ctx.get("meeting") if isinstance(ctx, dict) else None
    if isinstance(conv, dict) and conv.get("kind") in ("chat", "meeting"):
        rng = conv.get("range") or (f"L{conv['lines']}" if conv.get("lines") else "")
        return f"[{conv['kind']}{' ' + str(rng) if rng else ''}] {title}"
    return title


def attention(
    band: Band | None = typer.Option(None, "--band", help="Only one band: now, your_turn, upcoming or lately."),
    limit: int = typer.Option(100, "--limit", min=1, max=100, help="At most this many items."),
    as_json: bool = typer.Option(False, "--json", help="Print the raw JSON response."),
) -> None:
    """Show what needs your attention: reminders, approvals, tickets, loops,
    meeting actions, knowledge changes and the weekly proof card."""
    params: dict[str, Any] = {"limit": limit}
    if band is not None:
        params["band"] = band.value
    body = _fetch(params)
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    items = body.get("items") or []
    counts = body.get("counts") or {}
    errors = [
        name for name, sec in (body.get("sections") or {}).items()
        if isinstance(sec, dict) and sec.get("error")
    ]
    if not items:
        console.print("[green]You're all caught up.[/green]")
    else:
        for key, label in _BAND_LABELS.items():
            rows = [i for i in items if i.get("band") == key]
            if not rows:
                continue
            table = Table(title=f"{label} ({counts.get(key, len(rows))})", show_lines=False)
            table.add_column("What", overflow="fold")
            table.add_column("Why", overflow="fold")
            table.add_column("Link", overflow="fold", style="dim")
            for it in rows:
                table.add_row(
                    escape(_what(it)),
                    escape(str(it.get("why", ""))),
                    escape(str(it.get("link", ""))),
                )
            console.print(table)
        if body.get("truncated"):
            console.print(f"[dim]Showing {len(items)} of {counts.get('total', len(items))}.[/dim]")
    if errors:
        err_console.print(f"[yellow]Couldn't load: {', '.join(sorted(errors))}[/yellow]")

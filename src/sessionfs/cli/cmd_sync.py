"""Autosync CLI commands: sfs sync auto, watch, unwatch, watchlist, status."""

from __future__ import annotations

import asyncio

import typer
from rich.table import Table

from sessionfs.cli.common import console, err_console

sync_app = typer.Typer(name="sync", help="Manage session sync.", no_args_is_help=False)


def _get_sync_cfg():
    """Load sync config."""
    from sessionfs.cli.cmd_cloud import _load_sync_config
    return _load_sync_config()


async def _api_get(path: str, api_url: str, api_key: str) -> dict:
    import httpx

    url = f"{api_url.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    if resp.status_code >= 400:
        return {"_error": resp.status_code}
    return resp.json()


async def _api_put(path: str, api_url: str, api_key: str, json_data: dict) -> dict:
    import httpx

    url = f"{api_url.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(url, headers={"Authorization": f"Bearer {api_key}"}, json=json_data)
    if resp.status_code >= 400:
        return {"_error": resp.status_code}
    return resp.json()


async def _api_post(path: str, api_url: str, api_key: str) -> dict:
    import httpx

    url = f"{api_url.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, headers={"Authorization": f"Bearer {api_key}"})
    if resp.status_code >= 400:
        return {"_error": resp.status_code}
    return resp.json()


async def _api_delete(path: str, api_url: str, api_key: str) -> dict:
    import httpx

    url = f"{api_url.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(url, headers={"Authorization": f"Bearer {api_key}"})
    if resp.status_code >= 400:
        return {"_error": resp.status_code}
    return resp.json()


@sync_app.callback(invoke_without_command=True)
def sync_default(ctx: typer.Context) -> None:
    """Show sync status or run bidirectional sync."""
    if ctx.invoked_subcommand is None:
        # Default: run bidirectional sync (existing behavior)
        from sessionfs.cli.cmd_cloud import sync_all
        sync_all()


@sync_app.command("status")
def sync_status() -> None:
    """Show current autosync status."""
    cfg = _get_sync_cfg()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise typer.Exit(1)

    result = asyncio.run(_api_get("/api/v1/sync/status", cfg["api_url"], cfg["api_key"]))
    if "_error" in result:
        err_console.print(f"[red]Failed to get sync status ({result['_error']})[/red]")
        raise typer.Exit(1)

    mode = result.get("mode", "off")
    total = result.get("total_sessions", 0)
    synced = result.get("synced_sessions", 0)
    watched = result.get("watched_sessions", 0)
    queued = result.get("queued", 0)
    failed = result.get("failed", 0)
    used = result.get("storage_used_bytes", 0)
    limit = result.get("storage_limit_bytes", 0)

    mode_label = {"off": "[dim]off[/dim]", "all": "[green]all[/green]", "selective": "[yellow]selective[/yellow]"}.get(mode, mode)
    console.print(f"[bold]Autosync:[/bold] {mode_label}")

    if mode == "all":
        console.print(f"Synced:   {synced} sessions")
    elif mode == "selective":
        console.print(f"Watched:  {watched} sessions")
        console.print(f"Total:    {total} sessions")

    if queued:
        console.print(f"[yellow]Queued:   {queued}[/yellow]")
    if failed:
        console.print(f"[red]Failed:   {failed}[/red]")

    if limit > 0:
        pct = int(used / limit * 100)
        used_mb = used / (1024 * 1024)
        limit_mb = limit / (1024 * 1024)
        console.print(f"Storage:  {used_mb:.0f} MB / {limit_mb:.0f} MB ({pct}%)")

    if mode == "off":
        console.print("\n[dim]Run 'sfs sync auto --mode all' to enable.[/dim]")


@sync_app.command("auto")
def sync_auto(
    mode: str = typer.Option(..., "--mode", help="Autosync mode: off, all, or selective"),
) -> None:
    """Set autosync mode."""
    if mode not in ("off", "all", "selective"):
        err_console.print("[red]Mode must be 'off', 'all', or 'selective'.[/red]")
        raise typer.Exit(1)

    cfg = _get_sync_cfg()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise typer.Exit(1)

    result = asyncio.run(_api_put(
        "/api/v1/sync/settings", cfg["api_url"], cfg["api_key"],
        {"mode": mode},
    ))
    if "_error" in result:
        err_console.print(f"[red]Failed to update settings ({result['_error']})[/red]")
        raise typer.Exit(1)

    if mode == "off":
        console.print("Autosync disabled. Sessions will not sync automatically.")
    elif mode == "all":
        console.print("[green]Autosync enabled: all sessions will sync automatically.[/green]")
    elif mode == "selective":
        console.print("[green]Autosync enabled: selective mode.[/green]")
        console.print("Use 'sfs sync watch <session_id>' to mark sessions for autosync.")


@sync_app.command("watch")
def sync_watch(
    session_ids: list[str] = typer.Argument(..., help="Session ID(s) to watch"),
) -> None:
    """Add session(s) to autosync watchlist."""
    cfg = _get_sync_cfg()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise typer.Exit(1)

    for sid in session_ids:
        result = asyncio.run(_api_post(f"/api/v1/sync/watch/{sid}", cfg["api_url"], cfg["api_key"]))
        if "_error" in result:
            err_console.print(f"[red]Failed to watch {sid} ({result['_error']})[/red]")
        else:
            console.print(f"Session {sid} added to autosync watchlist.")


@sync_app.command("unwatch")
def sync_unwatch(
    session_ids: list[str] = typer.Argument(..., help="Session ID(s) to unwatch"),
) -> None:
    """Remove session(s) from autosync watchlist."""
    cfg = _get_sync_cfg()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise typer.Exit(1)

    for sid in session_ids:
        result = asyncio.run(_api_delete(f"/api/v1/sync/watch/{sid}", cfg["api_url"], cfg["api_key"]))
        if "_error" in result:
            err_console.print(f"[red]Failed to unwatch {sid} ({result['_error']})[/red]")
        else:
            console.print(f"Session {sid} removed from autosync watchlist.")


@sync_app.command("watchlist")
def sync_watchlist() -> None:
    """List sessions in the autosync watchlist."""
    cfg = _get_sync_cfg()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise typer.Exit(1)

    result = asyncio.run(_api_get("/api/v1/sync/watchlist", cfg["api_url"], cfg["api_key"]))
    if "_error" in result:
        err_console.print(f"[red]Failed to get watchlist ({result['_error']})[/red]")
        raise typer.Exit(1)

    sessions = result.get("sessions", [])
    if not sessions:
        console.print("[dim]Watchlist is empty. Use 'sfs sync watch <session_id>' to add sessions.[/dim]")
        return

    table = Table(title=f"Autosync Watchlist ({len(sessions)} sessions)")
    table.add_column("Session", style="cyan")
    table.add_column("Tool")
    table.add_column("Messages", justify="right")
    table.add_column("Status")
    table.add_column("Last Synced")

    for s in sessions:
        status = s.get("status", "")
        status_fmt = {
            "synced": "[green]synced[/green]",
            "pending": "[yellow]pending[/yellow]",
            "queued": "[yellow]queued[/yellow]",
            "failed": "[red]failed[/red]",
        }.get(status, status)

        last_synced = s.get("last_synced_at", "")
        if last_synced:
            last_synced = last_synced[:16].replace("T", " ")

        table.add_row(
            s.get("session_id", "")[:20],
            s.get("source_tool", ""),
            str(s.get("message_count", 0)),
            status_fmt,
            last_synced or "[dim]never[/dim]",
        )

    console.print(table)

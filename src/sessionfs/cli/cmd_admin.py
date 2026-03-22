"""Admin commands: sfs admin reindex."""

from __future__ import annotations

import asyncio

import typer

from sessionfs.cli.common import console, err_console

admin_app = typer.Typer(name="admin", help="Server administration commands.")


def _get_sync_client():
    """Create a SyncClient from stored config."""
    from sessionfs.cli.cmd_cloud import _get_sync_client
    return _get_sync_client()


@admin_app.command("reindex")
def reindex() -> None:
    """Re-extract metadata from all session archives on the server.

    Backfills title, source_tool, model, stats, and tags for sessions
    that were pushed before metadata extraction was deployed.
    """
    from sessionfs.sync.client import SyncClient, SyncAuthError, SyncError

    client = _get_sync_client()

    async def _reindex():
        try:
            http = await client._get_client()
            resp = await http.post(
                f"{client.api_url}/api/v1/sessions/admin/reindex",
                timeout=120.0,
            )
            if resp.status_code in (401, 403):
                raise SyncAuthError(f"Authentication failed: {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        finally:
            await client.close()

    console.print("Reindexing all sessions on the server...")
    try:
        result = asyncio.run(_reindex())
    except SyncAuthError:
        err_console.print("[red]Authentication failed. Run 'sfs auth login' first.[/red]")
        raise SystemExit(1)
    except Exception as exc:
        err_console.print(f"[red]Reindex failed: {exc}[/red]")
        raise SystemExit(1)

    console.print(
        f"[green]Reindex complete:[/green] "
        f"{result['reindexed']} sessions, "
        f"{result['updated']} updated, "
        f"{result['errors']} errors"
    )

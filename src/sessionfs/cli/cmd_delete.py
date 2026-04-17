"""Delete lifecycle CLI commands: sfs delete, sfs trash, sfs restore."""

from __future__ import annotations

import shutil

import typer
from rich.table import Table

from sessionfs.cli.common import (
    console,
    err_console,
    open_store,
    resolve_session_id,
)


def _load_sync_config() -> dict:
    """Load sync config from config.toml."""
    from sessionfs.daemon.config import load_config

    config = load_config()
    return {
        "api_url": config.sync.api_url,
        "api_key": config.sync.api_key,
        "enabled": config.sync.enabled,
    }


def _get_sync_client():
    """Get an authenticated sync client."""
    from sessionfs.sync.client import SyncClient

    cfg = _load_sync_config()
    if not cfg.get("api_key"):
        err_console.print("[red]Not authenticated. Run: sfs auth login[/red]")
        raise SystemExit(1)
    return SyncClient(api_url=cfg["api_url"], api_key=cfg["api_key"])


def delete(
    session_id: str = typer.Argument(help="Session ID or prefix."),
    cloud: bool = typer.Option(False, "--cloud", help="Delete from cloud only."),
    local: bool = typer.Option(False, "--local", help="Delete from this device only."),
    everywhere: bool = typer.Option(False, "--everywhere", help="Delete from cloud and device."),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompt."),
) -> None:
    """Delete a session from cloud, device, or both."""
    from sessionfs.store.deleted import mark_deleted

    # Validate exactly one scope flag
    scope_count = sum([cloud, local, everywhere])
    if scope_count == 0:
        err_console.print("[red]Error: specify --cloud, --local, or --everywhere[/red]")
        raise SystemExit(1)
    if scope_count > 1:
        err_console.print("[red]Error: specify only one of --cloud, --local, or --everywhere[/red]")
        raise SystemExit(1)

    if cloud:
        scope = "cloud"
    elif local:
        scope = "local"
    else:
        scope = "everywhere"

    # Resolve prefix → full ID from local store when available.
    # All scopes benefit from resolution (the server API requires the full
    # session ID, not a prefix).
    store = None
    full_id = session_id
    try:
        store = open_store()
        full_id = resolve_session_id(store, session_id)
    except (SystemExit, Exception):
        if scope == "local":
            err_console.print(f"[red]Session {session_id} not found locally.[/red]")
            raise SystemExit(1)
        # For cloud/everywhere, session might only be on server —
        # pass the raw ID through and let the API resolve or 404.
        full_id = session_id

    # Confirmation prompt
    if not force:
        if scope == "cloud":
            prompt = f"Delete {full_id[:16]} from cloud? Local copy will be kept."
        elif scope == "local":
            prompt = f"Delete {full_id[:16]} from this device? Cloud copy will be kept."
        else:
            prompt = f"Delete {full_id[:16]} everywhere? Recoverable for 30 days on server."
        if not typer.confirm(prompt, default=False):
            console.print("[dim]Cancelled.[/dim]")
            if store:
                store.close()
            return

    try:
        # Cloud deletion
        if scope in ("cloud", "everywhere"):
            import httpx

            cfg = _load_sync_config()
            api_scope = "everywhere" if scope == "everywhere" else "cloud"
            resp = httpx.delete(
                f"{cfg['api_url']}/api/v1/sessions/{full_id}",
                params={"scope": api_scope},
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                timeout=10.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                console.print(f"[green]Deleted from cloud.[/green] Purge after: {data.get('purge_after', 'N/A')}")
            elif resp.status_code == 410:
                console.print("[yellow]Session already deleted on server.[/yellow]")
            elif resp.status_code == 404:
                if scope == "cloud":
                    err_console.print("[red]Session not found on server.[/red]")
                    raise SystemExit(1)
                # For everywhere, continue with local deletion
                console.print("[yellow]Session not found on server, removing locally.[/yellow]")
            else:
                err_console.print(f"[red]Server error: {resp.status_code} — {resp.text}[/red]")
                raise SystemExit(1)

        # Local deletion
        if scope in ("local", "everywhere") and store:
            session_dir = store.get_session_dir(full_id)
            if session_dir and session_dir.is_dir():
                shutil.rmtree(session_dir)
                console.print(f"[green]Removed local directory: {session_dir}[/green]")

            # Remove from SQLite index
            try:
                conn = store.index._conn
                if conn is not None:
                    conn.execute(
                        "DELETE FROM sessions WHERE session_id = ?", (full_id,)
                    )
                    conn.commit()
            except Exception:
                pass  # Index entry may not exist

        # Add to local exclusion list
        mark_deleted(full_id, scope)
        console.print(f"[green]Session {full_id[:16]} marked as deleted (scope={scope}).[/green]")

    finally:
        if store:
            store.close()


def trash() -> None:
    """List soft-deleted sessions (trash)."""
    from datetime import datetime

    cfg = _load_sync_config()

    # Fetch server-side trash
    server_trash = []
    try:
        import httpx

        resp = httpx.get(
            f"{cfg['api_url']}/api/v1/sessions",
            params={"deleted": "true", "page_size": 100},
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            server_trash = resp.json().get("sessions", [])
    except Exception as exc:
        err_console.print(f"[yellow]Could not reach server: {exc}[/yellow]")

    # Fetch local exclusion list
    from sessionfs.store.deleted import list_deleted

    local_deleted = list_deleted()

    if not server_trash and not local_deleted:
        console.print("[dim]Trash is empty.[/dim]")
        return

    table = Table(title=f"Trash ({len(server_trash) + len(local_deleted)} entries)")
    table.add_column("ID", style="cyan", max_width=20)
    table.add_column("Title", max_width=30)
    table.add_column("Deleted", style="yellow")
    table.add_column("Scope", style="magenta")
    table.add_column("Purge after", style="red")

    # Server-side entries
    for s in server_trash:
        sid = s.get("id", "?")
        title = s.get("title") or ""
        deleted_at = s.get("deleted_at") or ""
        scope = s.get("delete_scope") or ""
        purge = s.get("purge_after") or ""
        # Shorten date for display
        if deleted_at:
            try:
                dt = datetime.fromisoformat(deleted_at.replace("Z", "+00:00"))
                deleted_at = dt.strftime("%Y-%m-%d")
            except Exception:
                pass
        if purge:
            try:
                dt = datetime.fromisoformat(purge.replace("Z", "+00:00"))
                purge = dt.strftime("%Y-%m-%d")
            except Exception:
                pass
        table.add_row(sid[:20], title[:30], deleted_at, scope, purge)

    # Local-only entries (not on server)
    server_ids = {s.get("id") for s in server_trash}
    for sid, entry in local_deleted.items():
        if sid in server_ids:
            continue
        deleted_at = entry.get("deleted_at", "")
        if deleted_at:
            try:
                dt = datetime.fromisoformat(deleted_at.replace("Z", "+00:00"))
                deleted_at = dt.strftime("%Y-%m-%d")
            except Exception:
                pass
        scope = entry.get("scope", "")
        table.add_row(sid[:20], "[dim]local only[/dim]", deleted_at, scope, "—")

    console.print(table)


def restore(
    session_id: str = typer.Argument(help="Session ID or prefix to restore."),
) -> None:
    """Restore a soft-deleted session."""
    from sessionfs.store.deleted import get_entry, remove_exclusion

    import httpx

    # Resolve prefix → full ID from local store when available.
    try:
        store = open_store()
        session_id = resolve_session_id(store, session_id)
        store.close()
    except Exception:
        pass  # Use raw ID — server will resolve or 404

    cfg = _load_sync_config()

    # Try server restore
    resp = httpx.post(
        f"{cfg['api_url']}/api/v1/sessions/{session_id}/restore",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        timeout=10.0,
    )

    if resp.status_code == 200:
        console.print(f"[green]Restored session {session_id} on server.[/green]")
        # Check if local copy was also removed
        entry = get_entry(session_id)
        if entry and entry.get("scope") in ("local", "everywhere"):
            console.print(
                f"[yellow]Local copy was removed. Run: sfs pull {session_id}[/yellow]"
            )
    elif resp.status_code == 409:
        console.print("[yellow]Session is not deleted on server.[/yellow]")
        # Still check local exclusion — user may have done a local-only
        # delete and then the server copy was restored via dashboard.
        entry = get_entry(session_id)
        if entry:
            console.print(
                "[dim]Clearing local exclusion — session is active on server.[/dim]"
            )
            if entry.get("scope") in ("local", "everywhere"):
                console.print(
                    f"[yellow]Local copy was removed. Run: sfs pull {session_id}[/yellow]"
                )
    elif resp.status_code == 410:
        console.print("[red]Session has been permanently purged.[/red]")
        return
    elif resp.status_code == 404:
        # May be local-only delete
        entry = get_entry(session_id)
        if entry and entry.get("scope") == "local":
            console.print("[yellow]This was a local-only delete. No server restore needed.[/yellow]")
        else:
            err_console.print(f"[red]Session not found: {resp.text}[/red]")
            raise SystemExit(1)
    else:
        err_console.print(f"[red]Server error: {resp.status_code} — {resp.text}[/red]")
        raise SystemExit(1)

    # Remove from local exclusion list
    remove_exclusion(session_id)
    console.print(f"[green]Removed {session_id} from local exclusion list.[/green]")

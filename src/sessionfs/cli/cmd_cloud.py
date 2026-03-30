"""Cloud sync commands: sfs auth, sfs push, sfs pull, sfs sync, sfs handoff."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from sessionfs.cli.common import (
    console,
    err_console,
    get_session_dir_or_exit,
    open_store,
    resolve_session_id,
)

auth_app = typer.Typer(name="auth", help="Authentication for cloud sync.")


def _load_sync_config() -> dict:
    """Load sync config from config.toml."""
    from sessionfs.daemon.config import load_config

    config = load_config()
    return {
        "api_url": config.sync.api_url,
        "api_key": config.sync.api_key,
        "enabled": config.sync.enabled,
    }


def _save_sync_config(api_url: str, api_key: str) -> None:
    """Update the sync section in config.toml."""
    import sys

    if sys.version_info >= (3, 11):
        pass
    else:
        pass

    from sessionfs.daemon.config import DEFAULT_CONFIG_PATH, ensure_config

    ensure_config()
    config_path = DEFAULT_CONFIG_PATH

    if config_path.exists():
        text = config_path.read_text()
    else:
        text = ""

    # Simple TOML update: replace or append [sync] section
    lines = text.split("\n")
    new_lines = []
    in_sync_section = False
    sync_written = False

    for line in lines:
        stripped = line.strip()
        if stripped == "[sync]":
            in_sync_section = True
            sync_written = True
            new_lines.append("[sync]")
            new_lines.append('enabled = true')
            new_lines.append(f'api_url = "{api_url}"')
            new_lines.append(f'api_key = "{api_key}"')
            new_lines.append('push_interval = 30')
            new_lines.append('retry_max = 5')
            continue
        if in_sync_section:
            if stripped.startswith("[") and stripped != "[sync]":
                in_sync_section = False
                new_lines.append(line)
            # Skip old sync lines
            continue
        new_lines.append(line)

    if not sync_written:
        new_lines.append("")
        new_lines.append("[sync]")
        new_lines.append('enabled = true')
        new_lines.append(f'api_url = "{api_url}"')
        new_lines.append(f'api_key = "{api_key}"')
        new_lines.append('push_interval = 30')
        new_lines.append('retry_max = 5')

    config_path.write_text("\n".join(new_lines))
    import os
    import stat
    os.chmod(config_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600 — not world-readable


def _get_sync_client():
    """Create a SyncClient from stored config."""
    from sessionfs.sync.client import SyncClient

    cfg = _load_sync_config()
    if not cfg["api_key"]:
        err_console.print(
            "[red]Not authenticated. Run 'sfs auth login' first.[/red]"
        )
        raise SystemExit(1)
    return SyncClient(api_url=cfg["api_url"], api_key=cfg["api_key"])


@auth_app.command("login")
def auth_login(
    api_url: str = typer.Option(
        "https://api.sessionfs.dev", "--url", help="Server URL."
    ),
    api_key: Optional[str] = typer.Option(
        None, "--key", help="API key (will prompt if not provided)."
    ),
) -> None:
    """Authenticate with a SessionFS server."""
    if not api_key:
        api_key = typer.prompt("Enter your API key")

    if not api_key:
        err_console.print("[red]API key is required.[/red]")
        raise SystemExit(1)

    # Verify the key works
    from sessionfs.sync.client import SyncClient, SyncAuthError, SyncError

    client = SyncClient(api_url=api_url, api_key=api_key)

    async def _verify():
        try:
            return await client.health_check()
        finally:
            await client.close()

    console.print(f"Verifying key against {api_url}...")
    try:
        result = asyncio.run(_verify())
    except SyncAuthError:
        err_console.print("[red]Authentication failed. Check your API key.[/red]")
        raise SystemExit(1)
    except SyncError as exc:
        err_console.print(f"[red]Could not reach server: {exc}[/red]")
        raise SystemExit(1)
    except Exception as exc:
        err_console.print(f"[red]Connection error: {exc}[/red]")
        raise SystemExit(1)

    # Save to config
    _save_sync_config(api_url, api_key)
    console.print(f"[green]Authenticated with {api_url}[/green]")
    console.print(f"Server status: {result.get('status', 'unknown')}")
    console.print("Cloud sync is now enabled. The daemon will push sessions automatically.")


@auth_app.command("signup")
def auth_signup(
    email: str = typer.Option(..., prompt="Email", help="Your email address."),
    api_url: str = typer.Option(
        "https://api.sessionfs.dev", "--url", help="Server URL."
    ),
) -> None:
    """Create a new account and get your first API key."""
    import httpx

    try:
        resp = httpx.post(
            f"{api_url}/api/v1/auth/signup",
            json={"email": email},
            timeout=15.0,
        )
    except httpx.ConnectError:
        err_console.print(f"[red]Could not reach server at {api_url}[/red]")
        raise SystemExit(1)

    if resp.status_code == 409:
        err_console.print("[red]Email already registered. Use 'sfs auth login' instead.[/red]")
        raise SystemExit(1)

    if resp.status_code != 201:
        err_console.print(f"[red]Signup failed: {resp.text}[/red]")
        raise SystemExit(1)

    data = resp.json()
    raw_key = data["raw_key"]

    # Save to config
    _save_sync_config(api_url, raw_key)
    console.print(f"[green]Account created for {email}[/green]")
    console.print(f"  API key: {raw_key}")
    console.print("  [bold]Save this key — it won't be shown again.[/bold]")
    console.print("Cloud sync is now enabled.")


@auth_app.command("status")
def auth_status() -> None:
    """Show current authentication status."""
    cfg = _load_sync_config()
    if not cfg["api_key"]:
        console.print("[dim]Not authenticated. Run 'sfs auth login'.[/dim]")
        return

    console.print("[green]Authenticated[/green]")
    console.print(f"  Server: {cfg['api_url']}")
    console.print(f"  API key: {cfg['api_key'][:12]}...")

    # Fetch user profile from server
    try:
        import httpx

        resp = httpx.get(
            f"{cfg['api_url']}/api/v1/auth/me",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            timeout=10,
        )
        if resp.status_code == 200:
            me = resp.json()
            console.print(f"  Email: [cyan]{me.get('email', '?')}[/cyan]")
            console.print(f"  Tier: {me.get('tier', 'free')}")
            verified = me.get("email_verified", False)
            console.print(f"  Verified: {'[green]yes[/green]' if verified else '[yellow]no — check your inbox[/yellow]'}")
        else:
            console.print(f"  [dim]Could not fetch profile (HTTP {resp.status_code})[/dim]")
    except Exception:
        console.print("  [dim]Could not reach server[/dim]")

    console.print(f"  Sync enabled: {cfg['enabled']}")


def push(
    session_id: str = typer.Argument(help="Session ID or prefix."),
) -> None:
    """Push a local session to the server."""
    from sessionfs.sync.archive import pack_session
    from sessionfs.sync.client import SyncConflictError

    store = open_store()
    client = _get_sync_client()

    try:
        full_id = resolve_session_id(store, session_id)
        session_dir = get_session_dir_or_exit(store, full_id)

        console.print(f"Packing session {full_id[:12]}...")
        archive_data = pack_session(session_dir)

        # Read local etag
        manifest = store.get_session_manifest(full_id)
        local_etag = (manifest or {}).get("sync", {}).get("etag")

        async def _push():
            try:
                return await client.push_session(full_id, archive_data, etag=local_etag)
            finally:
                await client.close()

        console.print(f"Pushing ({len(archive_data):,} bytes)...")
        result = asyncio.run(_push())

        # Update local manifest with new etag
        _update_manifest_sync(session_dir, result.etag)

        action = "Created" if result.created else "Updated"
        console.print(
            f"[green]{action} remote session {result.session_id}[/green]\n"
            f"  ETag: {result.etag[:16]}...\n"
            f"  Size: {result.blob_size_bytes:,} bytes"
        )

    except SyncConflictError as exc:
        err_console.print(
            f"[red]Conflict: remote session was updated (etag={exc.current_etag[:16]}...).[/red]\n"
            f"Pull the latest version first with: sfs pull {session_id}"
        )
        raise SystemExit(1)
    finally:
        store.close()


def pull(
    session_id: str = typer.Argument(help="Session ID."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local without prompting."),
) -> None:
    """Pull a session from the server."""
    from sessionfs.sync.archive import unpack_session

    store = open_store()
    client = _get_sync_client()

    try:
        # Check if session exists locally
        local_dir = store.get_session_dir(session_id)
        local_etag = None

        if local_dir:
            manifest = store.get_session_manifest(session_id)
            local_etag = (manifest or {}).get("sync", {}).get("etag")

        async def _pull():
            try:
                return await client.pull_session(session_id, etag=local_etag)
            finally:
                await client.close()

        console.print(f"Pulling session {session_id[:12]}...")
        result = asyncio.run(_pull())

        if result.not_modified:
            console.print("[dim]Session is up to date (304 Not Modified).[/dim]")
            return

        if result.data is None:
            err_console.print("[red]No data received from server.[/red]")
            raise SystemExit(1)

        # If local exists and differs, prompt
        if local_dir and not force:
            if not typer.confirm("Local session differs from remote. Overwrite local?", default=True):
                console.print("[dim]Pull cancelled.[/dim]")
                return

        # Extract to local store
        target_dir = store.allocate_session_dir(session_id)
        unpack_session(result.data, target_dir)

        # Update index
        manifest_path = target_dir / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            store.upsert_session_metadata(session_id, manifest, str(target_dir))

        # Store etag
        _update_manifest_sync(target_dir, result.etag)

        console.print(
            f"[green]Pulled session {session_id}[/green]\n"
            f"  ETag: {result.etag[:16]}...\n"
            f"  Size: {len(result.data):,} bytes"
        )

    finally:
        store.close()


def list_remote(
    page: int = typer.Option(1, help="Page number."),
    page_size: int = typer.Option(20, help="Sessions per page."),
    source_tool: Optional[str] = typer.Option(None, "--tool", help="Filter by tool."),
) -> None:
    """List sessions on the remote server."""
    client = _get_sync_client()
    store = open_store()

    async def _list():
        try:
            return await client.list_remote_sessions(
                page=page, page_size=page_size, source_tool=source_tool
            )
        finally:
            await client.close()

    try:
        result = asyncio.run(_list())

        if not result.sessions:
            console.print("[dim]No remote sessions found.[/dim]")
            return

        # Build set of local session IDs for comparison
        local_sessions = {s["session_id"] for s in store.list_sessions()}

        table = Table(title=f"Remote Sessions ({result.total} total)")
        table.add_column("ID", style="cyan", max_width=16)
        table.add_column("Tool", style="dim")
        table.add_column("Model")
        table.add_column("Msgs", justify="right")
        table.add_column("Size", justify="right")
        table.add_column("Local", justify="center")
        table.add_column("Title", max_width=30)

        for s in result.sessions:
            is_local = s.id in local_sessions
            size_kb = s.blob_size_bytes / 1024
            table.add_row(
                s.id[:16],
                s.source_tool,
                s.model_id or "",
                str(s.message_count),
                f"{size_kb:.1f}K",
                "[green]yes[/green]" if is_local else "[dim]no[/dim]",
                (s.title or "")[:30],
            )

        console.print(table)

        if result.has_more:
            console.print(f"[dim]Page {result.page} — more available (--page {result.page + 1})[/dim]")

    finally:
        store.close()


def sync_all() -> None:
    """Bidirectional sync: push local changes, pull remote-only sessions."""
    from sessionfs.sync.archive import pack_session, unpack_session
    from sessionfs.sync.client import SyncConflictError

    client = _get_sync_client()
    store = open_store()

    async def _sync():
        pushed = 0
        pulled = 0
        conflicts = 0

        try:
            # Get remote session list
            remote_result = await client.list_remote_sessions(page=1, page_size=100)
            remote_by_id = {s.id: s for s in remote_result.sessions}

            # Get local sessions
            local_sessions = store.list_sessions()
            local_by_id = {s["session_id"]: s for s in local_sessions}

            # Push local sessions not on remote or with different etag
            for sid, local in local_by_id.items():
                session_dir = store.get_session_dir(sid)
                if not session_dir:
                    continue

                manifest = store.get_session_manifest(sid)
                local_etag = (manifest or {}).get("sync", {}).get("etag")

                remote = remote_by_id.get(sid)
                if remote and remote.etag == local_etag:
                    continue  # Already in sync

                try:
                    archive_data = pack_session(session_dir)
                    result = await client.push_session(sid, archive_data, etag=local_etag)
                    _update_manifest_sync(session_dir, result.etag)
                    pushed += 1
                except SyncConflictError:
                    conflicts += 1
                    err_console.print(
                        f"[yellow]Conflict: {sid[:12]} — pull first[/yellow]"
                    )
                except Exception as exc:
                    err_console.print(f"[red]Push failed for {sid[:12]}: {exc}[/red]")

            # Pull remote sessions not present locally
            for sid, remote in remote_by_id.items():
                if sid in local_by_id:
                    continue  # Already have it

                try:
                    result = await client.pull_session(sid)
                    if result.data:
                        target_dir = store.allocate_session_dir(sid)
                        unpack_session(result.data, target_dir)

                        manifest_path = target_dir / "manifest.json"
                        if manifest_path.exists():
                            manifest = json.loads(manifest_path.read_text())
                            store.upsert_session_metadata(sid, manifest, str(target_dir))

                        _update_manifest_sync(target_dir, result.etag)
                        pulled += 1
                except Exception as exc:
                    err_console.print(f"[red]Pull failed for {sid[:12]}: {exc}[/red]")

            return pushed, pulled, conflicts
        finally:
            await client.close()

    try:
        console.print("Fetching remote sessions...")
        pushed, pulled, conflicts = asyncio.run(_sync())
        console.print(
            f"[green]Sync complete: {pushed} pushed, {pulled} pulled, {conflicts} conflicts[/green]"
        )
    finally:
        store.close()


handoffs_app = typer.Typer(name="handoffs", help="View handoff inbox and sent items.")


def handoff(
    session_id: str = typer.Argument(help="Session ID or prefix."),
    to: str = typer.Option(..., "--to", help="Recipient email."),
    message: str = typer.Option("", "--message", "-m", help="Message to include."),
) -> None:
    """Hand off a session to another user (push + email notification)."""
    from sessionfs.sync.archive import pack_session
    from sessionfs.sync.client import SyncConflictError

    store = open_store()
    client = _get_sync_client()

    try:
        full_id = resolve_session_id(store, session_id)
        session_dir = get_session_dir_or_exit(store, full_id)

        # Push to server if not already synced
        manifest = store.get_session_manifest(full_id)
        local_etag = (manifest or {}).get("sync", {}).get("etag")

        console.print(f"Ensuring session {full_id[:12]} is synced...")
        archive_data = pack_session(session_dir)

        async def _push_and_handoff():
            try:
                push_result = await client.push_session(full_id, archive_data, etag=local_etag)
                return push_result
            finally:
                await client.close()

        result = asyncio.run(_push_and_handoff())
        _update_manifest_sync(session_dir, result.etag)

        # Create handoff via API
        import httpx

        cfg = _load_sync_config()
        body = {
            "session_id": full_id,
            "recipient_email": to,
        }
        if message:
            body["message"] = message

        resp = httpx.post(
            f"{cfg['api_url']}/api/v1/handoffs",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            json=body,
            timeout=15.0,
        )

        if resp.status_code == 201:
            data = resp.json()
            handoff_id = data["id"]
            console.print(f"\n[green]Handoff created: {handoff_id}[/green]")
            console.print(f"  Recipient: {to}")
            console.print(f"  Expires: {data['expires_at']}")
            console.print("\nRecipient can pull with:")
            console.print(f"  sfs pull-handoff {handoff_id}")
        else:
            err_console.print(f"[red]Handoff failed: {resp.text}[/red]")
            raise SystemExit(1)

    except SyncConflictError:
        err_console.print("[red]Conflict pushing session. Pull latest first.[/red]")
        raise SystemExit(1)
    finally:
        store.close()


def pull_handoff(
    handoff_id: str = typer.Argument(help="Handoff ID (hnd_xxx)."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local without prompting."),
    tool: str = typer.Option(
        "claude-code", "--in",
        help="Target tool for resume: claude-code, codex, copilot, gemini",
    ),
) -> None:
    """Pull a session from a handoff link."""
    import httpx
    from sessionfs.sync.archive import unpack_session

    store = open_store()
    cfg = _load_sync_config()
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}

    try:
        # Get handoff details
        console.print(f"Fetching handoff {handoff_id}...")
        resp = httpx.get(
            f"{cfg['api_url']}/api/v1/handoffs/{handoff_id}",
            headers=headers,
            timeout=15.0,
        )
        if resp.status_code == 410:
            err_console.print("[red]Handoff has expired.[/red]")
            raise SystemExit(1)
        if resp.status_code != 200:
            err_console.print(f"[red]Failed to get handoff: {resp.text}[/red]")
            raise SystemExit(1)

        handoff_data = resp.json()
        session_id = handoff_data["session_id"]

        console.print(f"  Session: {handoff_data.get('session_title') or session_id}")
        console.print(f"  From: {handoff_data['sender_email']}")
        if handoff_data.get("message"):
            console.print(f"  Message: {handoff_data['message']}")

        # Claim the handoff
        claim_resp = httpx.post(
            f"{cfg['api_url']}/api/v1/handoffs/{handoff_id}/claim",
            headers=headers,
            timeout=15.0,
        )
        if claim_resp.status_code == 409:
            console.print("[dim]Handoff already claimed — continuing with pull.[/dim]")
        elif claim_resp.status_code not in (200, 201):
            err_console.print(f"[yellow]Warning: could not claim handoff: {claim_resp.text}[/yellow]")

        # Pull the session
        client = _get_sync_client()

        async def _pull():
            try:
                return await client.pull_session(session_id)
            finally:
                await client.close()

        console.print(f"Pulling session {session_id[:12]}...")
        pull_result = asyncio.run(_pull())

        if pull_result.data is None:
            err_console.print("[red]No data received from server.[/red]")
            raise SystemExit(1)

        # Check if session exists locally
        local_dir = store.get_session_dir(session_id)
        if local_dir and not force:
            if not typer.confirm("Local session exists. Overwrite?", default=True):
                console.print("[dim]Pull cancelled.[/dim]")
                return

        # Extract to local store
        target_dir = store.allocate_session_dir(session_id)
        unpack_session(pull_result.data, target_dir)

        # Update index
        manifest_path = target_dir / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            store.upsert_session_metadata(session_id, manifest, str(target_dir))

        _update_manifest_sync(target_dir, pull_result.etag)

        # Try workspace resolution
        workspace_path = target_dir / "workspace.json"
        if workspace_path.exists():
            workspace = json.loads(workspace_path.read_text())
            git_info = workspace.get("git", {})
            git_remote = git_info.get("remote_url")
            if git_remote:
                from sessionfs.workspace.resolver import WorkspaceResolver
                resolver = WorkspaceResolver()
                resolved = resolver.resolve(git_remote, git_info.get("branch"))
                if resolved.path:
                    console.print(f"  Found local repo: {resolved.path}")
                    # Update workspace root_path to local clone
                    workspace["root_path"] = str(resolved.path)
                    workspace_path.write_text(json.dumps(workspace, indent=2))
                else:
                    console.print(
                        f"[dim]  Could not find local clone of {git_remote}[/dim]"
                    )

        console.print(
            f"\n[green]Pulled handoff session {session_id}[/green]\n"
            f"  Size: {len(pull_result.data):,} bytes"
        )
        console.print("\nResume with:")
        console.print(f"  sfs resume {session_id} --in {tool}")

    finally:
        store.close()


@handoffs_app.command("inbox")
def handoffs_inbox() -> None:
    """List handoffs sent to you."""
    import httpx

    cfg = _load_sync_config()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise SystemExit(1)

    resp = httpx.get(
        f"{cfg['api_url']}/api/v1/handoffs/inbox/",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        timeout=15.0,
    )

    if resp.status_code != 200:
        err_console.print(f"[red]Failed to fetch inbox: {resp.text}[/red]")
        raise SystemExit(1)

    data = resp.json()
    handoffs = data.get("handoffs", [])

    if not handoffs:
        console.print("[dim]No incoming handoffs.[/dim]")
        return

    table = Table(title=f"Inbox ({data['total']} handoffs)")
    table.add_column("ID", style="cyan")
    table.add_column("From")
    table.add_column("Session")
    table.add_column("Tool", style="dim")
    table.add_column("Status")
    table.add_column("Created")

    for h in handoffs:
        status_style = "[green]" if h["status"] == "pending" else "[dim]"
        table.add_row(
            h["id"],
            h["sender_email"],
            (h.get("session_title") or "")[:30],
            h.get("session_tool") or "",
            f"{status_style}{h['status']}[/{status_style.strip('[')}",
            h["created_at"][:10],
        )

    console.print(table)


@handoffs_app.command("sent")
def handoffs_sent() -> None:
    """List handoffs you've sent."""
    import httpx

    cfg = _load_sync_config()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise SystemExit(1)

    resp = httpx.get(
        f"{cfg['api_url']}/api/v1/handoffs/sent/",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        timeout=15.0,
    )

    if resp.status_code != 200:
        err_console.print(f"[red]Failed to fetch sent handoffs: {resp.text}[/red]")
        raise SystemExit(1)

    data = resp.json()
    handoffs = data.get("handoffs", [])

    if not handoffs:
        console.print("[dim]No sent handoffs.[/dim]")
        return

    table = Table(title=f"Sent ({data['total']} handoffs)")
    table.add_column("ID", style="cyan")
    table.add_column("To")
    table.add_column("Session")
    table.add_column("Status")
    table.add_column("Created")

    for h in handoffs:
        status_style = "[yellow]" if h["status"] == "pending" else "[green]"
        table.add_row(
            h["id"],
            h["recipient_email"],
            (h.get("session_title") or "")[:30],
            f"{status_style}{h['status']}[/{status_style.strip('[')}",
            h["created_at"][:10],
        )

    console.print(table)


def _update_manifest_sync(session_dir: Path, etag: str) -> None:
    """Update the sync block in a session's manifest.json."""
    from datetime import datetime, timezone

    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text())
    if "sync" not in manifest:
        manifest["sync"] = {}
    manifest["sync"]["etag"] = etag
    manifest["sync"]["last_sync_at"] = datetime.now(timezone.utc).isoformat()
    manifest["sync"]["dirty"] = False
    manifest_path.write_text(json.dumps(manifest, indent=2))

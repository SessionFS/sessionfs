"""Health check command: sfs doctor."""

from __future__ import annotations

import os
import sqlite3
import stat

from rich.table import Table

from sessionfs.cli.common import console, err_console, get_store_dir, handle_errors


def _check_mark(ok: bool) -> str:
    return "[green]\u2713[/green]" if ok else "[red]\u2717[/red]"


@handle_errors
def doctor() -> None:
    """Run health checks and auto-repair."""
    store_dir = get_store_dir()

    table = Table(title="SessionFS Health Check", show_lines=False)
    table.add_column("Check", min_width=30)
    table.add_column("Status", min_width=6)
    table.add_column("Detail")

    repaired = False

    # 1. Store directory exists and writable
    store_ok = store_dir.is_dir() and os.access(store_dir, os.W_OK)
    table.add_row(
        "Store directory",
        _check_mark(store_ok),
        str(store_dir) if store_ok else f"{store_dir} (missing or not writable)",
    )

    # 2. Sessions directory exists, count .sfs dirs
    sessions_dir = store_dir / "sessions"
    sessions_ok = sessions_dir.is_dir()
    disk_count = 0
    if sessions_ok:
        disk_count = sum(
            1
            for p in sessions_dir.iterdir()
            if p.is_dir() and p.name.endswith(".sfs")
        )
    table.add_row(
        "Sessions directory",
        _check_mark(sessions_ok),
        f"{disk_count} session(s) on disk" if sessions_ok else "missing",
    )

    # 3. Index opens, count matches disk
    index_path = store_dir / "index.db"
    index_ok = False
    index_count = 0
    index_detail = "missing"
    if index_path.exists():
        try:
            conn = sqlite3.connect(str(index_path))
            conn.execute("PRAGMA integrity_check")
            row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
            index_count = row[0] if row else 0
            conn.close()
            index_ok = True
            match_note = ""
            if index_count != disk_count:
                match_note = f" [yellow](disk has {disk_count})[/yellow]"
            index_detail = f"{index_count} indexed{match_note}"
        except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
            index_detail = f"corrupted: {exc}"
            # Auto-repair
            err_console.print(
                "[yellow]Index corrupted. Attempting auto-repair...[/yellow]"
            )
            try:
                from sessionfs.cli.common import open_store

                store = open_store(initialize=True)
                store.close()
                repaired = True
                index_ok = True
                index_detail = "repaired (reindexed from disk)"
            except Exception as repair_exc:
                index_detail = f"repair failed: {repair_exc}"
    table.add_row("Session index", _check_mark(index_ok), index_detail)

    # 4. Index freshness (disk vs index count)
    freshness_ok = index_ok and index_count == disk_count
    if index_ok:
        table.add_row(
            "Index freshness",
            _check_mark(freshness_ok),
            "in sync" if freshness_ok else f"stale ({disk_count} on disk, {index_count} indexed)",
        )

    # 5. Daemon running (check PID file)
    pid_path = store_dir / "sfsd.pid"
    daemon_ok = False
    daemon_detail = "not running"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            daemon_ok = True
            daemon_detail = f"running (PID {pid})"
        except (ValueError, ProcessLookupError, PermissionError):
            daemon_detail = "stale PID file (not running)"
    table.add_row("Daemon", _check_mark(daemon_ok), daemon_detail)

    # 6. API reachable (GET /health with timeout)
    api_ok = False
    api_detail = "not configured"
    try:
        from sessionfs.daemon.config import load_config

        config = load_config()
        if config.sync.enabled and config.sync.api_url:
            try:
                import httpx

                resp = httpx.get(
                    f"{config.sync.api_url}/health",
                    timeout=5.0,
                )
                api_ok = resp.status_code == 200
                api_detail = (
                    f"reachable ({config.sync.api_url})"
                    if api_ok
                    else f"HTTP {resp.status_code}"
                )
            except Exception as exc:
                api_detail = f"unreachable: {exc}"
        else:
            api_detail = "sync not enabled"
    except Exception:
        pass
    table.add_row("API server", _check_mark(api_ok), api_detail)

    # 7. Auth valid (GET /api/v1/auth/me)
    auth_ok = False
    auth_detail = "not authenticated"
    try:
        from sessionfs.daemon.config import load_config

        config = load_config()
        if config.sync.api_key:
            try:
                import httpx

                resp = httpx.get(
                    f"{config.sync.api_url}/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {config.sync.api_key}"},
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    me = resp.json()
                    auth_ok = True
                    auth_detail = f"{me.get('email', '?')} ({me.get('tier', 'free')})"
                else:
                    auth_detail = f"invalid (HTTP {resp.status_code})"
            except Exception as exc:
                auth_detail = f"check failed: {exc}"
    except Exception:
        pass
    table.add_row("Authentication", _check_mark(auth_ok), auth_detail)

    # 8. Config permissions
    config_path = store_dir / "config.toml"
    config_ok = False
    config_detail = "not found"
    if config_path.exists():
        mode = config_path.stat().st_mode
        world_readable = bool(
            mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH)
        )
        config_ok = not world_readable
        if config_ok:
            config_detail = f"permissions OK ({oct(mode & 0o777)})"
        else:
            config_detail = f"too permissive ({oct(mode & 0o777)}) — should be 0o600"
    table.add_row("Config permissions", _check_mark(config_ok), config_detail)

    console.print()
    console.print(table)
    console.print()

    if repaired:
        console.print("[green]Auto-repair completed successfully.[/green]")

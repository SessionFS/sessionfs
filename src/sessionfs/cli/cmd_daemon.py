"""Daemon management commands: sfs daemon start|stop|status|logs."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import typer
from rich.table import Table

from sessionfs.cli.common import console, get_store_dir
from sessionfs.daemon.status import read_status

daemon_app = typer.Typer(name="daemon", help="Manage the SessionFS daemon.", no_args_is_help=True)


def _pid_path() -> Path:
    return get_store_dir() / "sfsd.pid"


def _log_path() -> Path:
    return get_store_dir() / "daemon.log"


def _status_path() -> Path:
    return get_store_dir() / "daemon.json"


def _read_pid() -> int | None:
    """Read PID from file, return None if not found or invalid."""
    pid_file = _pid_path()
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None


def _is_running(pid: int) -> bool:
    """Check if a process is running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@daemon_app.command()
def start(
    config: str | None = typer.Option(None, help="Path to config.toml"),
    log_level: str = typer.Option("INFO", help="Log level (DEBUG, INFO, WARNING, ERROR)"),
) -> None:
    """Start the SessionFS daemon in the background."""
    pid = _read_pid()
    if pid and _is_running(pid):
        console.print(f"[yellow]Daemon already running (PID {pid}).[/yellow]")
        return

    # Build sfsd command
    cmd = [sys.executable, "-m", "sessionfs.daemon.main", "--log-level", log_level]
    if config:
        cmd.extend(["--config", config])

    # Ensure store dir exists
    store_dir = get_store_dir()
    store_dir.mkdir(parents=True, exist_ok=True)

    # Start detached process
    log_file = open(_log_path(), "a")
    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )

    # Write PID
    _pid_path().write_text(str(proc.pid))
    console.print(f"[green]Daemon started (PID {proc.pid}).[/green]")
    console.print(f"Logs: {_log_path()}")
    console.print()
    console.print("[dim]  Dashboard: sfs mcp serve (for AI tool integration)[/dim]")
    console.print("[dim]  Sessions:  sfs list[/dim]")


def _resolve_daemon_pid() -> int | None:
    """Find the daemon PID, preferring sfsd.pid and falling back to
    daemon.json. Both can become stale — we return whatever we find, leaving
    liveness checks to the caller.

    Falling back to daemon.json matters when a daemon exited uncleanly
    (cleared sfsd.pid in its finally block) and a replacement got launched
    that only updated daemon.json. Without the fallback, `sfs daemon stop`
    says "No PID file found" while a live process keeps running.
    """
    pid = _read_pid()
    if pid is not None:
        return pid
    status = read_status(_status_path())
    if status and status.pid:
        return status.pid
    return None


@daemon_app.command()
def stop() -> None:
    """Stop the SessionFS daemon."""
    pid = _resolve_daemon_pid()
    if pid is None:
        console.print("[yellow]No daemon PID file found.[/yellow]")
        return

    if not _is_running(pid):
        console.print("[yellow]Daemon not running (stale PID file). Cleaning up.[/yellow]")
        _pid_path().unlink(missing_ok=True)
        _status_path().unlink(missing_ok=True)
        return

    os.kill(pid, signal.SIGTERM)
    console.print(f"[green]Sent SIGTERM to daemon (PID {pid}).[/green]")

    # Clean up both PID file and status file so repeated `stop` calls
    # don't keep hitting the same stale metadata, and `status` doesn't
    # show dead daemon info after a successful stop.
    _pid_path().unlink(missing_ok=True)
    _status_path().unlink(missing_ok=True)


@daemon_app.command()
def status() -> None:
    """Show daemon status."""
    pid = _resolve_daemon_pid()
    running = pid is not None and _is_running(pid)

    daemon_status = read_status(_status_path())

    if daemon_status:
        table = Table(title="SessionFS Daemon Status")
        table.add_column("Field", style="bold")
        table.add_column("Value")

        table.add_row("PID", str(daemon_status.pid))
        table.add_row("Running", "[green]Yes[/green]" if running else "[red]No[/red]")
        table.add_row("Started", daemon_status.started_at)
        table.add_row("Sessions", str(daemon_status.sessions_total))

        for w in daemon_status.watchers:
            health = w.health
            color = {"healthy": "green", "degraded": "yellow", "broken": "red"}.get(health, "white")
            table.add_row(
                f"Watcher: {w.name}",
                f"[{color}]{health}[/{color}] ({w.sessions_tracked} sessions)",
            )

        console.print(table)
    elif running:
        console.print(f"[green]Daemon running (PID {pid}), no status file yet.[/green]")
    else:
        console.print("[dim]Daemon not running.[/dim]")


@daemon_app.command()
def restart() -> None:
    """Restart the SessionFS daemon."""
    import time

    pid = _read_pid()
    if pid is None or not _is_running(pid):
        console.print("[dim]Daemon not running. Starting...[/dim]")
        start()
        return

    # Stop
    os.kill(pid, signal.SIGTERM)
    _pid_path().unlink(missing_ok=True)

    # Wait for process to exit
    for _ in range(20):
        if not _is_running(pid):
            break
        time.sleep(0.25)

    # Start
    cmd = [sys.executable, "-m", "sessionfs.daemon.main"]
    log_file = open(_log_path(), "a")
    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )
    _pid_path().write_text(str(proc.pid))
    console.print(f"[green]Daemon restarted (PID {proc.pid}).[/green]")


@daemon_app.command()
def logs(
    lines: int = typer.Option(50, "--lines", "-n", help="Number of lines to show."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output."),
) -> None:
    """Show daemon logs."""
    log_file = _log_path()
    if not log_file.exists():
        console.print("[dim]No log file found.[/dim]")
        return

    if follow:
        try:
            subprocess.run(["tail", "-f", str(log_file)])
        except KeyboardInterrupt:
            pass
    else:
        # Read last N lines
        all_lines = log_file.read_text().splitlines()
        for line in all_lines[-lines:]:
            console.print(line)


@daemon_app.command("rebuild-index")
def rebuild_index() -> None:
    """Rebuild the local session index from .sfs files on disk."""
    import json

    from sessionfs.cli.common import open_store

    store = open_store()
    try:
        sessions_dir = store._store_dir / "sessions"
        if not sessions_dir.is_dir():
            console.print("[yellow]No sessions directory found.[/yellow]")
            return

        count = 0
        for sfs_dir in sorted(sessions_dir.iterdir()):
            if not sfs_dir.is_dir() or not sfs_dir.name.endswith(".sfs"):
                continue
            manifest_path = sfs_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text())
                session_id = manifest.get("session_id", sfs_dir.name.replace(".sfs", ""))
                # Backfill source_tool from tracked_sessions if missing
                source = manifest.get("source", {})
                if not source.get("tool"):
                    tracked = store.index.conn.execute(
                        "SELECT tool FROM tracked_sessions WHERE sfs_session_id = ?",
                        (session_id,),
                    ).fetchone()
                    if tracked:
                        if "source" not in manifest:
                            manifest["source"] = {}
                        manifest["source"]["tool"] = tracked[0]
                        manifest_path.write_text(json.dumps(manifest, indent=2))
                store.upsert_session_metadata(session_id, manifest, str(sfs_dir))
                count += 1
            except (json.JSONDecodeError, OSError) as e:
                console.print(f"[dim]Skipped {sfs_dir.name}: {e}[/dim]")

        console.print(f"[green]Rebuilt index: {count} sessions.[/green]")
    finally:
        store.close()

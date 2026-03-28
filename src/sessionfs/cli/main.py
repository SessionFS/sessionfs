"""SessionFS CLI entry point.

Usage:
    sfs daemon start|stop|status|logs
    sfs list [--tool] [--since] [--tag] [--sort] [--json] [--quiet]
    sfs show <session_id> [--messages] [--cost]
    sfs resume <session_id> [--project PATH]
    sfs checkpoint <session_id> --name <name>
    sfs fork <session_id> --name <name> [--from-checkpoint <name>]
    sfs import [FILE] [--from TOOL] [--format FORMAT]
    sfs export <session_id> [--format markdown|claude-code|sfs]
    sfs storage [prune [--dry-run] [--force]]
    sfs config show|set
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="sfs",
    help="SessionFS — Portable AI coding sessions.",
    no_args_is_help=True,
)

# Register sub-command groups
from sessionfs.cli.cmd_daemon import daemon_app
from sessionfs.cli.cmd_config import config_app
from sessionfs.cli.cmd_cloud import auth_app, handoffs_app
from sessionfs.cli.cmd_admin import admin_app
from sessionfs.cli.cmd_mcp import mcp_app
from sessionfs.cli.cmd_watcher import watcher_app
from sessionfs.cli.cmd_storage import storage_app
from sessionfs.cli.cmd_project import project_app
from sessionfs.cli.cmd_sync import sync_app

app.add_typer(daemon_app, name="daemon")
app.add_typer(config_app, name="config")
app.add_typer(auth_app, name="auth")
app.add_typer(admin_app, name="admin")
app.add_typer(mcp_app, name="mcp")
app.add_typer(handoffs_app, name="handoffs")
app.add_typer(watcher_app, name="watcher")
app.add_typer(storage_app, name="storage")
app.add_typer(project_app, name="project")
app.add_typer(sync_app, name="sync")

# Register top-level commands
from sessionfs.cli.cmd_sessions import list_sessions, show_session
from sessionfs.cli.cmd_ops import resume, checkpoint, fork, alias
from sessionfs.cli.cmd_io import import_sessions, export_session
from sessionfs.cli.cmd_cloud import push, pull, pull_handoff, list_remote, handoff
from sessionfs.cli.cmd_search import search
from sessionfs.cli.cmd_audit import audit

app.command("list")(list_sessions)
app.command("show")(show_session)
app.command("resume")(resume)
app.command("checkpoint")(checkpoint)
app.command("fork")(fork)
app.command("alias")(alias)
app.command("import")(import_sessions)
app.command("export")(export_session)
app.command("push")(push)
app.command("pull")(pull)
app.command("list-remote")(list_remote)
app.command("handoff")(handoff)
app.command("pull-handoff")(pull_handoff)
app.command("search")(search)
app.command("audit")(audit)


def cli_main() -> None:
    """Entry point for the sfs CLI."""
    app()

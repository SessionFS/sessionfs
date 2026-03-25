"""Watcher management commands: sfs watcher list|enable|disable."""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import typer
from rich.table import Table

from sessionfs.cli.common import console, err_console, get_store_dir
from sessionfs.cli.cmd_daemon import _read_pid, _is_running, _pid_path, _log_path

watcher_app = typer.Typer(name="watcher", help="Manage tool watchers.", no_args_is_help=True)

# All supported tools with their config key and default storage paths
TOOLS = {
    "claude-code": {"config_key": "claude_code", "default_enabled": True},
    "codex": {"config_key": "codex", "default_enabled": False},
    "gemini": {"config_key": "gemini", "default_enabled": False},
    "copilot": {"config_key": "copilot", "default_enabled": False},
    "cursor": {"config_key": "cursor", "default_enabled": False},
    "amp": {"config_key": "amp", "default_enabled": False},
    "cline": {"config_key": "cline", "default_enabled": False},
    "roo-code": {"config_key": "roo_code", "default_enabled": False},
}


def _config_path() -> Path:
    return get_store_dir() / "config.toml"


def _read_config() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text())


def _tool_is_installed(tool: str) -> bool:
    """Check if the tool's storage directory exists on this machine."""
    import platform

    home = Path.home()
    checks = {
        "claude-code": home / ".claude",
        "codex": home / ".codex",
        "gemini": home / ".gemini",
        "copilot": home / ".copilot",
        "amp": home / ".local" / "share" / "amp",
        "cline": (
            home / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"
            if platform.system() == "Darwin"
            else home / ".config" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"
        ),
        "roo-code": (
            home / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline"
            if platform.system() == "Darwin"
            else home / ".config" / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline"
        ),
        "cursor": (
            home / "Library" / "Application Support" / "Cursor"
            if platform.system() == "Darwin"
            else home / ".config" / "Cursor"
        ),
    }
    path = checks.get(tool)
    return path.exists() if path else False


def _is_enabled(config: dict, tool: str) -> bool:
    """Check if a tool watcher is enabled in config."""
    info = TOOLS[tool]
    section = config.get(info["config_key"], {})
    if isinstance(section, dict):
        return section.get("enabled", info["default_enabled"])
    return info["default_enabled"]


def _write_tool_enabled(tool: str, enabled: bool) -> None:
    """Update config.toml to enable/disable a tool watcher."""
    path = _config_path()
    config_key = TOOLS[tool]["config_key"]

    # Read existing content as text (preserve formatting)
    content = path.read_text() if path.exists() else ""

    # Check if section exists
    section_header = f"[{config_key}]"
    if section_header in content:
        # Replace enabled value in existing section
        import re
        pattern = rf"(\[{config_key}\][^\[]*?)enabled\s*=\s*(true|false)"
        replacement = rf"\g<1>enabled = {'true' if enabled else 'false'}"
        new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
        if new_content == content:
            # enabled key doesn't exist in section — add it
            new_content = content.replace(section_header, f"{section_header}\nenabled = {'true' if enabled else 'false'}")
        path.write_text(new_content)
    else:
        # Add new section
        if not content.endswith("\n"):
            content += "\n"
        content += f"\n[{config_key}]\nenabled = {'true' if enabled else 'false'}\n"
        path.write_text(content)


@watcher_app.command("list")
def list_watchers() -> None:
    """List all tool watchers and their status."""
    config = _read_config()

    table = Table(title="Tool Watchers")
    table.add_column("Tool", style="cyan")
    table.add_column("Enabled", width=8)
    table.add_column("Installed", width=10)
    table.add_column("Config Key")

    for tool, info in TOOLS.items():
        enabled = _is_enabled(config, tool)
        installed = _tool_is_installed(tool)

        enabled_str = "[green]yes[/green]" if enabled else "[dim]no[/dim]"
        installed_str = "[green]found[/green]" if installed else "[dim]not found[/dim]"

        table.add_row(tool, enabled_str, installed_str, info["config_key"])

    console.print(table)
    console.print(f"\n[dim]Config: {_config_path()}[/dim]")
    console.print("[dim]Enable: sfs watcher enable <tool>  |  Disable: sfs watcher disable <tool>[/dim]")


def _restart_daemon() -> None:
    """Restart the daemon if it's running."""
    import os
    import signal
    import subprocess
    import time

    pid = _read_pid()
    if pid is None or not _is_running(pid):
        console.print("[dim]Daemon not running — start it with: sfs daemon start[/dim]")
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


@watcher_app.command("enable")
def enable_watcher(
    tool: str = typer.Argument(..., help="Tool to enable (claude-code, codex, gemini, copilot, cursor, amp, cline, roo-code)"),
) -> None:
    """Enable a tool watcher and restart the daemon."""
    if tool not in TOOLS:
        err_console.print(f"[red]Unknown tool: {tool}[/red]")
        err_console.print(f"Available: {', '.join(TOOLS.keys())}")
        raise typer.Exit(1)

    _write_tool_enabled(tool, True)
    console.print(f"[green]Enabled {tool} watcher.[/green]")

    if not _tool_is_installed(tool):
        console.print(f"[yellow]Note: {tool} storage not found on this machine.[/yellow]")

    _restart_daemon()


@watcher_app.command("disable")
def disable_watcher(
    tool: str = typer.Argument(..., help="Tool to disable"),
) -> None:
    """Disable a tool watcher and restart the daemon."""
    if tool not in TOOLS:
        err_console.print(f"[red]Unknown tool: {tool}[/red]")
        err_console.print(f"Available: {', '.join(TOOLS.keys())}")
        raise typer.Exit(1)

    _write_tool_enabled(tool, False)
    console.print(f"Disabled {tool} watcher.")
    _restart_daemon()

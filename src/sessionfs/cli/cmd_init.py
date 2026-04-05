"""Interactive setup wizard: sfs init."""

from __future__ import annotations

import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import typer

from sessionfs.cli.common import console, get_store_dir
from sessionfs.daemon.config import ensure_config


@dataclass
class ToolInfo:
    """Describes an AI coding tool for detection."""

    name: str
    config_key: str
    detect_paths: list[Path]


def _get_tool_definitions() -> list[ToolInfo]:
    """Return tool definitions with platform-aware detection paths."""
    home = Path.home()
    is_mac = platform.system() == "Darwin"

    # VS Code extension base dirs
    if is_mac:
        vscode_global = home / "Library" / "Application Support" / "Code" / "User" / "globalStorage"
    else:
        vscode_global = home / ".config" / "Code" / "User" / "globalStorage"

    return [
        ToolInfo(
            name="Claude Code",
            config_key="claude_code",
            detect_paths=[
                home / ".claude",
                home / ".config" / "claude-code",
            ],
        ),
        ToolInfo(
            name="Cursor",
            config_key="cursor",
            detect_paths=[
                home / ".cursor",
            ],
        ),
        ToolInfo(
            name="Codex",
            config_key="codex",
            detect_paths=[
                home / ".codex",
            ],
        ),
        ToolInfo(
            name="Gemini CLI",
            config_key="gemini",
            detect_paths=[
                home / ".gemini",
            ],
        ),
        ToolInfo(
            name="Copilot",
            config_key="copilot",
            detect_paths=[
                home / ".config" / "github-copilot",
                home / ".copilot",
            ],
        ),
        ToolInfo(
            name="Amp",
            config_key="amp",
            detect_paths=[
                home / ".amp",
                home / ".local" / "share" / "amp",
            ],
        ),
        ToolInfo(
            name="Cline",
            config_key="cline",
            detect_paths=[
                home / ".cline",
                vscode_global / "saoudrizwan.claude-dev",
            ],
        ),
        ToolInfo(
            name="Roo Code",
            config_key="roo_code",
            detect_paths=[
                home / ".roo-code",
                vscode_global / "rooveterinaryinc.roo-cline",
            ],
        ),
    ]


@dataclass
class DetectedTool:
    """A tool that was found on the system."""

    info: ToolInfo
    found_path: Path


def detect_tools(tool_definitions: list[ToolInfo] | None = None) -> tuple[list[DetectedTool], list[ToolInfo]]:
    """Scan the system for installed AI coding tools.

    Returns (detected, missing) tuples.
    """
    if tool_definitions is None:
        tool_definitions = _get_tool_definitions()

    detected: list[DetectedTool] = []
    missing: list[ToolInfo] = []

    for tool in tool_definitions:
        found = False
        for path in tool.detect_paths:
            if path.exists():
                detected.append(DetectedTool(info=tool, found_path=path))
                found = True
                break
        if not found:
            missing.append(tool)

    return detected, missing


def _write_config_with_tools(enabled_keys: set[str]) -> None:
    """Write config.toml enabling the specified tool keys."""
    config_path = get_store_dir() / "config.toml"
    ensure_config(config_path)

    # Re-read existing config and update tool enabled flags
    import sys as _sys

    if _sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    if config_path.exists():
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    else:
        data = {}

    # Update each tool's enabled state
    all_tool_keys = {t.config_key for t in _get_tool_definitions()}
    for key in all_tool_keys:
        if key not in data:
            data[key] = {}
        data[key]["enabled"] = key in enabled_keys

    # Write using the same simple TOML writer from cmd_config
    from sessionfs.cli.cmd_config import _write_toml

    _write_toml(config_path, data)


def init_cmd() -> None:
    """Interactive setup wizard for SessionFS."""
    console.print()
    console.print("[bold]SessionFS Setup[/bold]")
    console.print()

    # --- Step 1: Detect installed tools ---
    console.print("Scanning for AI coding tools...")
    detected, missing = detect_tools()

    for tool in detected:
        console.print(f"  [green]\u2713[/green] {tool.info.name} detected ({tool.found_path})")
    for tool in missing:
        console.print(f"  [dim]\u2717[/dim] [dim]{tool.name} not found[/dim]")

    console.print()

    if not detected:
        console.print("[yellow]No AI coding tools detected.[/yellow]")
        console.print("Install a supported tool and run [bold]sfs init[/bold] again.")
        return

    track_all = typer.confirm(
        f"Found {len(detected)} tool{'s' if len(detected) != 1 else ''}. Track all?",
        default=True,
    )

    enabled_keys: set[str] = set()
    if track_all:
        enabled_keys = {t.info.config_key for t in detected}
    else:
        for tool in detected:
            if typer.confirm(f"  Track {tool.info.name}?", default=True):
                enabled_keys.add(tool.info.config_key)

    if not enabled_keys:
        console.print("[yellow]No tools selected. You can configure tools later with [bold]sfs config set[/bold].[/yellow]")
        return

    # Ensure config dir and write tool configuration
    store_dir = get_store_dir()
    store_dir.mkdir(parents=True, exist_ok=True)
    _write_config_with_tools(enabled_keys)
    console.print()

    # --- Step 2: Cloud sync (optional) ---
    console.print("Cloud sync lets you access sessions from any machine and share with teammates.")
    console.print()
    setup_sync = typer.confirm("Set up cloud sync now?", default=False)

    if setup_sync:
        server_url = typer.prompt("  Server URL", default="https://api.sessionfs.dev")
        console.print("  [dim]Sign up at https://sessionfs.dev to get an API key[/dim]")
        api_key = typer.prompt("  API Key")

        # Update sync config
        config_path = store_dir / "config.toml"
        import sys as _sys

        if _sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib

        if config_path.exists():
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
        else:
            data = {}

        data.setdefault("sync", {})
        data["sync"]["enabled"] = True
        data["sync"]["api_url"] = server_url
        data["sync"]["api_key"] = api_key

        from sessionfs.cli.cmd_config import _write_toml

        _write_toml(config_path, data)

        # Verify connection
        try:
            import httpx

            resp = httpx.get(
                f"{server_url}/api/v1/me",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                tier = resp.json().get("tier", "free")
                console.print(f"  [green]\u2713[/green] Connected! Tier: {tier}")
            else:
                console.print(f"  [yellow]Could not verify connection (HTTP {resp.status_code}). You can configure sync later.[/yellow]")
        except Exception:
            console.print("  [yellow]Could not reach server. Sync saved — it will connect when available.[/yellow]")

    console.print()

    # --- Step 3: Start daemon ---
    start_daemon = typer.confirm("Start the SessionFS daemon now?", default=True)

    if start_daemon:
        try:
            cmd = [sys.executable, "-m", "sessionfs.daemon.main", "--log-level", "INFO"]
            log_path = store_dir / "daemon.log"
            pid_path = store_dir / "sfsd.pid"

            log_file = open(log_path, "a")  # noqa: SIM115
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=log_file,
                    start_new_session=True,
                )
            finally:
                log_file.close()
            pid_path.write_text(str(proc.pid))

            tool_count = len(enabled_keys)
            console.print()
            console.print(f"[green]\u2713[/green] Daemon started! Watching {tool_count} tool{'s' if tool_count != 1 else ''}.")
            console.print(f"  Sessions stored in: {store_dir / 'sessions'}")
        except Exception as e:
            console.print(f"[red]Failed to start daemon: {e}[/red]")
            console.print("You can start it manually with [bold]sfs daemon start[/bold].")
    else:
        console.print("You can start the daemon later with [bold]sfs daemon start[/bold].")

    # --- Step 4: Knowledge contribution instructions ---
    if detected and typer.confirm(
        "Enable knowledge contribution? (Agents will proactively write discoveries to the knowledge base)",
        default=True,
    ):
        from sessionfs.cli.cmd_mcp import inject_agent_instructions

        # Map config_key to MCP tool name (underscores to hyphens)
        _config_key_to_tool = {
            "claude_code": "claude-code",
            "cursor": "cursor",
            "codex": "codex",
            "gemini": "gemini",
            "copilot": "copilot",
            "amp": "amp",
            "cline": "cline",
            "roo_code": "roo-code",
        }
        for tool in detected:
            if tool.info.config_key in enabled_keys:
                mcp_tool = _config_key_to_tool.get(tool.info.config_key)
                if mcp_tool:
                    # Install MCP server first, then inject instructions
                    try:
                        console.print(f"  Installing MCP for {mcp_tool}...")
                        from sessionfs.cli.cmd_mcp import _install_mcp_for_tool
                        _install_mcp_for_tool(mcp_tool)
                    except (SystemExit, Exception):
                        console.print(f"  [dim]MCP install skipped for {mcp_tool}[/dim]")
                    inject_agent_instructions(mcp_tool)

    # --- Next steps ---
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  [cyan]sfs list[/cyan]              \u2014 View captured sessions")
    console.print("  [cyan]sfs show[/cyan] <id>         \u2014 Inspect a session")
    console.print("  [cyan]sfs resume[/cyan] <id>       \u2014 Resume in any tool")
    console.print("  [cyan]sfs search[/cyan] \"query\"    \u2014 Search past sessions")
    console.print()

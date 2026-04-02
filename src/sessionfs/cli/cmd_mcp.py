"""MCP server commands: sfs mcp serve, sfs mcp install."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer

from sessionfs.cli.common import console, err_console

mcp_app = typer.Typer(name="mcp", help="MCP server for AI tool integration.")


@mcp_app.command("serve")
def serve() -> None:
    """Start the SessionFS MCP server on stdio.

    Used by AI tools (Claude Code, Cursor, etc.) as an MCP server.
    Exposes session search, retrieval, and browsing tools.
    """
    import asyncio

    from sessionfs.mcp.server import serve as mcp_serve

    asyncio.run(mcp_serve())


@mcp_app.command("install")
def install(
    tool: str = typer.Option(
        ..., "--for",
        help="Tool to configure: claude-code, codex, gemini, cursor, copilot, amp, cline, roo-code",
    ),
) -> None:
    """Auto-configure SessionFS as an MCP server for an AI tool."""
    sfs_path = shutil.which("sfs")
    if not sfs_path:
        err_console.print("[red]'sfs' command not found in PATH.[/red]")
        err_console.print("Install sessionfs first: pip install sessionfs")
        raise SystemExit(1)

    mcp_config = {
        "command": sfs_path,
        "args": ["mcp", "serve"],
    }

    # Map tool names to installer functions
    installers = {
        "claude-code": _install_claude_code,
        "cursor": _install_cursor,
        "copilot": _install_copilot,
        "codex": _install_codex,
        "gemini": _install_gemini,
        "amp": _install_amp,
        "cline": _install_vscode_extension,
        "roo-code": _install_vscode_extension,
    }

    if tool in ("cline", "roo-code"):
        _install_vscode_extension(mcp_config, tool)
    elif tool in installers:
        installers[tool](mcp_config)
    else:
        supported = ", ".join(sorted(installers.keys()))
        err_console.print(f"[red]Unknown tool: {tool}. Supported: {supported}[/red]")
        raise SystemExit(1)


def _install_claude_code(mcp_config: dict) -> None:
    """Add SessionFS to Claude Code's MCP config."""
    config_path = Path.home() / ".claude.json"

    data: dict = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    servers = data.setdefault("mcpServers", {})

    if "sessionfs" in servers:
        console.print("[dim]SessionFS MCP server already configured in Claude Code.[/dim]")
        return

    servers["sessionfs"] = mcp_config
    config_path.write_text(json.dumps(data, indent=2))

    console.print("[green]SessionFS MCP server added to Claude Code.[/green]")
    console.print(f"  Config: {config_path}")
    console.print("  Restart Claude Code to activate.")
    console.print()
    console.print("Your AI agent can now search past sessions:")
    console.print('  "Has anyone on the team seen this error before?"')
    console.print('  "What was the fix for the auth middleware bug?"')


def _install_cursor(mcp_config: dict) -> None:
    """Add SessionFS to Cursor's MCP config."""
    config_path = Path.home() / ".cursor" / "mcp.json"

    data: dict = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    servers = data.setdefault("mcpServers", {})

    if "sessionfs" in servers:
        console.print("[dim]SessionFS MCP server already configured in Cursor.[/dim]")
        return

    servers["sessionfs"] = mcp_config
    config_path.write_text(json.dumps(data, indent=2))

    console.print("[green]SessionFS MCP server added to Cursor.[/green]")
    console.print(f"  Config: {config_path}")
    console.print("  Restart Cursor to activate.")


def _install_copilot(mcp_config: dict) -> None:
    """Add SessionFS to Copilot CLI's MCP config."""
    config_path = Path.home() / ".copilot" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    servers = data.setdefault("mcpServers", {})

    if "sessionfs" in servers:
        console.print("[dim]SessionFS MCP server already configured in Copilot CLI.[/dim]")
        return

    servers["sessionfs"] = mcp_config
    config_path.write_text(json.dumps(data, indent=2))

    console.print("[green]SessionFS MCP server added to Copilot CLI.[/green]")
    console.print(f"  Config: {config_path}")
    console.print("  Restart Copilot CLI to activate.")


def _install_codex(mcp_config: dict) -> None:
    """Add SessionFS to Codex CLI via `codex mcp add`."""
    import subprocess

    # Check if already registered
    try:
        result = subprocess.run(
            ["codex", "mcp", "get", "sessionfs"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            console.print("[dim]SessionFS MCP server already configured in Codex.[/dim]")
            return
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Register via codex mcp add
    sfs_path = mcp_config["command"]
    try:
        result = subprocess.run(
            ["codex", "mcp", "add", "sessionfs", "--", sfs_path, "mcp", "serve"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            console.print("[green]SessionFS MCP server added to Codex.[/green]")
            console.print("  Registered via: codex mcp add sessionfs -- sfs mcp serve")
            console.print("  Restart Codex to activate.")
        else:
            err_console.print(f"[red]Failed to register: {result.stderr.strip()}[/red]")
            console.print("  Try manually: codex mcp add sessionfs -- sfs mcp serve")
            raise SystemExit(1)
    except FileNotFoundError:
        err_console.print("[red]'codex' command not found. Install Codex CLI first.[/red]")
        raise SystemExit(1)


def _install_gemini(mcp_config: dict) -> None:
    """Add SessionFS to Gemini CLI via `gemini mcp add`."""
    import subprocess

    # Check if already registered
    try:
        result = subprocess.run(
            ["gemini", "mcp", "list"],
            capture_output=True, text=True, timeout=5,
        )
        if "sessionfs" in result.stdout:
            console.print("[dim]SessionFS MCP server already configured in Gemini CLI.[/dim]")
            return
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Register via gemini mcp add
    sfs_path = mcp_config["command"]
    try:
        result = subprocess.run(
            ["gemini", "mcp", "add", "sessionfs", sfs_path, "mcp", "serve"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            console.print("[green]SessionFS MCP server added to Gemini CLI.[/green]")
            console.print("  Registered via: gemini mcp add sessionfs sfs mcp serve")
            console.print("  Restart Gemini CLI to activate.")
        else:
            err_console.print(f"[red]Failed to register: {result.stderr.strip()}[/red]")
            console.print("  Try manually: gemini mcp add sessionfs sfs mcp serve")
            raise SystemExit(1)
    except FileNotFoundError:
        err_console.print("[red]'gemini' command not found. Install Gemini CLI first.[/red]")
        raise SystemExit(1)


def _install_amp(mcp_config: dict) -> None:
    """Add SessionFS to Amp's MCP config."""
    config_path = Path.home() / ".amp" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    servers = data.setdefault("mcpServers", {})

    if "sessionfs" in servers:
        console.print("[dim]SessionFS MCP server already configured in Amp.[/dim]")
        return

    servers["sessionfs"] = mcp_config
    config_path.write_text(json.dumps(data, indent=2))

    console.print("[green]SessionFS MCP server added to Amp.[/green]")
    console.print(f"  Config: {config_path}")
    console.print("  Restart Amp to activate.")


def _install_vscode_extension(mcp_config: dict, tool: str = "cline") -> None:
    """Add SessionFS to a VS Code extension's MCP config (Cline or Roo Code)."""
    ext_ids = {
        "cline": "saoudrizwan.claude-dev",
        "roo-code": "rooveterinaryinc.roo-cline",
    }
    ext_id = ext_ids.get(tool, ext_ids["cline"])
    display = "Cline" if tool == "cline" else "Roo Code"

    # VS Code globalStorage path
    import platform
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "Code" / "User" / "globalStorage"
    elif system == "Linux":
        base = Path.home() / ".config" / "Code" / "User" / "globalStorage"
    else:
        base = Path.home() / "AppData" / "Roaming" / "Code" / "User" / "globalStorage"

    config_dir = base / ext_id
    config_path = config_dir / "mcp_config.json"

    if not config_dir.exists():
        err_console.print(f"[yellow]{display} extension directory not found: {config_dir}[/yellow]")
        err_console.print(f"Make sure {display} is installed in VS Code first.")
        raise SystemExit(1)

    data: dict = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    servers = data.setdefault("mcpServers", {})

    if "sessionfs" in servers:
        console.print(f"[dim]SessionFS MCP server already configured in {display}.[/dim]")
        return

    servers["sessionfs"] = mcp_config
    config_path.write_text(json.dumps(data, indent=2))

    console.print(f"[green]SessionFS MCP server added to {display}.[/green]")
    console.print(f"  Config: {config_path}")
    console.print("  Restart VS Code to activate.")


@mcp_app.command("index")
def index() -> None:
    """Rebuild the full-text search index from all local sessions."""
    from sessionfs.cli.common import get_store_dir
    from sessionfs.mcp.search import SessionSearchIndex

    store_dir = get_store_dir()
    search_db = store_dir / "search.db"
    search = SessionSearchIndex(search_db)
    search.initialize()

    count = search.reindex_all(store_dir)
    search.close()

    console.print(f"[green]Search index rebuilt: {count} sessions indexed.[/green]")

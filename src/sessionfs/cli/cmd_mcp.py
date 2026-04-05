"""MCP server commands: sfs mcp serve, sfs mcp install, sfs mcp uninstall."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import typer

from sessionfs.cli.common import console, err_console

mcp_app = typer.Typer(name="mcp", help="MCP server for AI tool integration.")

# ---------------------------------------------------------------------------
# Knowledge instruction injection
# ---------------------------------------------------------------------------

KNOWLEDGE_INSTRUCTIONS = """## SessionFS Knowledge Base — MANDATORY

**IMPORTANT: You MUST call `add_knowledge()` via MCP when you discover something significant about this codebase.** This is not optional — it is a core requirement of working in this repo, just like following commit conventions.

Every discovery you make helps the next AI agent that works here. If you learn it, log it.

### You MUST contribute when you:
- Make or discover an architecture/design decision → `add_knowledge("description", "decision")`
- Find a code pattern or convention → `add_knowledge("description", "pattern")`
- Discover a bug or workaround → `add_knowledge("description", "bug")`
- Learn about an external dependency → `add_knowledge("description", "dependency")`
- Establish a coding convention → `add_knowledge("description", "convention")`
- Learn anything useful for future sessions → `add_knowledge("description", "discovery")`

### How to contribute
- Quick facts: `add_knowledge("what you learned", "type")` — call this multiple times per session
- Full articles: `update_wiki_page("slug", "markdown content")` — for substantial findings
- Search first: `search_project_knowledge("topic")` — avoid duplicates
- Browse: `list_wiki_pages()` — see what exists

### Examples of when to call add_knowledge:
- "The auth middleware resolves effective tier from the org, not the user record" → decision
- "All converters follow the pattern: parse native format → canonical .sfs → write" → pattern
- "SQLite WAL mode can corrupt on power loss — index.py has self-healing" → bug
- "react-markdown is used for rendering project context, not a custom parser" → dependency

**Do NOT end a session without contributing at least one knowledge entry if you learned something new.**
"""

SECTION_MARKER_START = "<!-- sessionfs-knowledge-start -->"
SECTION_MARKER_END = "<!-- sessionfs-knowledge-end -->"

TOOL_INSTRUCTION_FILES: dict[str, str | None] = {
    "claude-code": "CLAUDE.md",
    "codex": "codex.md",
    "gemini": "GEMINI.md",
    "cursor": ".cursorrules",
    "copilot": None,
    "amp": None,
    "cline": None,
    "roo-code": None,
}


def inject_agent_instructions(tool: str) -> None:
    """Inject knowledge-contribution instructions into the tool's project config file."""
    instruction_file = TOOL_INSTRUCTION_FILES.get(tool)
    if not instruction_file:
        console.print(f"[dim]{tool} doesn't support project-level agent instructions.[/dim]")
        console.print("[dim]Knowledge instructions will be served via MCP at runtime.[/dim]")
        return

    path = Path.cwd() / instruction_file
    content = f"\n{SECTION_MARKER_START}\n{KNOWLEDGE_INSTRUCTIONS}\n{SECTION_MARKER_END}\n"

    if path.exists():
        existing = path.read_text()
        if SECTION_MARKER_START in existing:
            # Update in place
            pattern = f"{re.escape(SECTION_MARKER_START)}.*?{re.escape(SECTION_MARKER_END)}"
            updated = re.sub(pattern, content.strip(), existing, flags=re.DOTALL)
            path.write_text(updated)
            console.print(f"[green]Updated knowledge instructions in {instruction_file}[/green]")
        else:
            path.write_text(existing.rstrip() + "\n\n" + content)
            console.print(f"[green]Appended knowledge instructions to {instruction_file}[/green]")
    else:
        path.write_text(content.strip() + "\n")
        console.print(f"[green]Created {instruction_file} with knowledge instructions[/green]")


def remove_agent_instructions(tool: str) -> None:
    """Remove knowledge-contribution instructions from the tool's project config file."""
    instruction_file = TOOL_INSTRUCTION_FILES.get(tool)
    if not instruction_file:
        return
    path = Path.cwd() / instruction_file
    if not path.exists():
        return
    existing = path.read_text()
    if SECTION_MARKER_START in existing:
        pattern = f"\n*{re.escape(SECTION_MARKER_START)}.*?{re.escape(SECTION_MARKER_END)}\n*"
        cleaned = re.sub(pattern, "\n", existing, flags=re.DOTALL).strip()
        if cleaned:
            path.write_text(cleaned + "\n")
        else:
            path.unlink()
        console.print(f"[dim]Removed knowledge instructions from {instruction_file}[/dim]")


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
    skip_instructions: bool = typer.Option(
        False, "--skip-instructions",
        help="Skip injecting knowledge-contribution instructions into project config.",
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

    if not skip_instructions:
        inject_agent_instructions(tool)


@mcp_app.command("uninstall")
def uninstall(
    tool: str = typer.Option(
        ..., "--for",
        help="Tool to unconfigure: claude-code, codex, gemini, cursor, copilot, amp, cline, roo-code",
    ),
) -> None:
    """Remove SessionFS MCP server registration from an AI tool."""
    uninstallers = {
        "claude-code": _uninstall_claude_code,
        "cursor": _uninstall_cursor,
        "copilot": _uninstall_copilot,
        "codex": _uninstall_codex,
        "gemini": _uninstall_gemini,
        "amp": _uninstall_amp,
        "cline": _uninstall_vscode_extension,
        "roo-code": _uninstall_vscode_extension,
    }

    if tool in ("cline", "roo-code"):
        _uninstall_vscode_extension(tool)
    elif tool in uninstallers:
        uninstallers[tool]()
    else:
        supported = ", ".join(sorted(uninstallers.keys()))
        err_console.print(f"[red]Unknown tool: {tool}. Supported: {supported}[/red]")
        raise SystemExit(1)

    remove_agent_instructions(tool)


# ---------------------------------------------------------------------------
# Uninstall helpers
# ---------------------------------------------------------------------------


def _uninstall_json_config(config_path: Path, display_name: str) -> None:
    """Remove sessionfs from a JSON config file's mcpServers."""
    if not config_path.exists():
        console.print(f"[dim]No config found at {config_path}. Nothing to remove.[/dim]")
        return

    try:
        data = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        console.print(f"[yellow]Could not parse {config_path}. Skipping.[/yellow]")
        return

    servers = data.get("mcpServers", {})
    if "sessionfs" not in servers:
        console.print(f"[dim]SessionFS not found in {display_name} config. Nothing to remove.[/dim]")
        return

    del servers["sessionfs"]
    config_path.write_text(json.dumps(data, indent=2))
    console.print(f"[green]Removed SessionFS MCP server from {display_name}.[/green]")
    console.print(f"  Restart {display_name} to apply.")


def _uninstall_claude_code() -> None:
    """Remove SessionFS from Claude Code's MCP config."""
    _uninstall_json_config(Path.home() / ".claude.json", "Claude Code")


def _uninstall_cursor() -> None:
    """Remove SessionFS from Cursor's MCP config."""
    _uninstall_json_config(Path.home() / ".cursor" / "mcp.json", "Cursor")


def _uninstall_copilot() -> None:
    """Remove SessionFS from Copilot CLI's MCP config."""
    _uninstall_json_config(Path.home() / ".copilot" / "config.json", "Copilot CLI")


def _uninstall_amp() -> None:
    """Remove SessionFS from Amp's MCP config."""
    _uninstall_json_config(Path.home() / ".amp" / "config.json", "Amp")


def _uninstall_codex() -> None:
    """Remove SessionFS from Codex CLI via `codex mcp remove`."""
    import subprocess

    try:
        result = subprocess.run(
            ["codex", "mcp", "remove", "sessionfs"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            console.print("[green]Removed SessionFS MCP server from Codex.[/green]")
        else:
            err_console.print(f"[yellow]codex mcp remove failed: {result.stderr.strip()}[/yellow]")
    except FileNotFoundError:
        console.print("[dim]'codex' command not found. Skipping.[/dim]")


def _uninstall_gemini() -> None:
    """Remove SessionFS from Gemini CLI via `gemini mcp remove`."""
    import subprocess

    try:
        result = subprocess.run(
            ["gemini", "mcp", "remove", "sessionfs"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            console.print("[green]Removed SessionFS MCP server from Gemini CLI.[/green]")
        else:
            err_console.print(f"[yellow]gemini mcp remove failed: {result.stderr.strip()}[/yellow]")
    except FileNotFoundError:
        console.print("[dim]'gemini' command not found. Skipping.[/dim]")


def _uninstall_vscode_extension(tool: str = "cline") -> None:
    """Remove SessionFS from a VS Code extension's MCP config."""
    ext_ids = {
        "cline": "saoudrizwan.claude-dev",
        "roo-code": "rooveterinaryinc.roo-cline",
    }
    ext_id = ext_ids.get(tool, ext_ids["cline"])
    display = "Cline" if tool == "cline" else "Roo Code"

    import platform
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "Code" / "User" / "globalStorage"
    elif system == "Linux":
        base = Path.home() / ".config" / "Code" / "User" / "globalStorage"
    else:
        base = Path.home() / "AppData" / "Roaming" / "Code" / "User" / "globalStorage"

    config_path = base / ext_id / "mcp_config.json"
    _uninstall_json_config(config_path, display)


# ---------------------------------------------------------------------------
# Install helpers
# ---------------------------------------------------------------------------


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

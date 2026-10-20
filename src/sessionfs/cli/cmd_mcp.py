"""MCP server commands: sfs mcp serve, sfs mcp install, sfs mcp uninstall."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import typer

from sessionfs.cli import mcp_config_io as _cfg
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
    "kilo-code": None,
    # Antigravity reads GEMINI.md / AGENTS.md, which Gemini CLI and Codex
    # share; writing (and on uninstall, removing) a block there on
    # Antigravity's behalf would touch other tools' files. Served via MCP.
    "antigravity": None,
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
        help="Tool to configure: claude-code, codex, gemini, cursor, copilot, amp, cline, roo-code, kilo-code, antigravity",
    ),
    skip_instructions: bool = typer.Option(
        False, "--skip-instructions",
        help="Skip injecting knowledge-contribution instructions into project config.",
    ),
) -> None:
    """Auto-configure SessionFS as an MCP server for an AI tool."""
    _install_mcp_for_tool(tool)

    if not skip_instructions:
        try:
            inject_agent_instructions(tool)
        except Exception as e:
            err_console.print(f"[yellow]Warning: could not write agent instructions: {e}[/yellow]")
            err_console.print("[dim]MCP server was registered. Instructions will be served at runtime.[/dim]")


def _install_mcp_for_tool(tool: str) -> None:
    """Register SessionFS MCP server for a tool (no instructions)."""
    sfs_path = shutil.which("sfs")
    if not sfs_path:
        err_console.print("[red]'sfs' command not found in PATH.[/red]")
        raise SystemExit(1)

    mcp_config = {
        "command": sfs_path,
        "args": ["mcp", "serve"],
    }

    installers = {
        "claude-code": _install_claude_code,
        "cursor": _install_cursor,
        "copilot": _install_copilot,
        "codex": _install_codex,
        "gemini": _install_gemini,
        "amp": _install_amp,
        "cline": _install_vscode_extension,
        "roo-code": _install_vscode_extension,
        "kilo-code": _install_vscode_extension,
        "antigravity": _install_antigravity,
    }

    if tool in ("cline", "roo-code", "kilo-code"):
        _install_vscode_extension(mcp_config, tool)
    elif tool in installers:
        installers[tool](mcp_config)
    else:
        supported = ", ".join(sorted(installers.keys()))
        err_console.print(f"[red]Unknown tool: {tool}. Supported: {supported}[/red]")
        raise SystemExit(1)


@mcp_app.command("uninstall")
def uninstall(
    tool: str = typer.Option(
        ..., "--for",
        help="Tool to unconfigure: claude-code, codex, gemini, cursor, copilot, amp, cline, roo-code, kilo-code, antigravity",
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
        "kilo-code": _uninstall_vscode_extension,
        "antigravity": _uninstall_antigravity,
    }

    uninstall_ok = True
    try:
        if tool in ("cline", "roo-code", "kilo-code"):
            _uninstall_vscode_extension(tool)
        elif tool in uninstallers:
            uninstallers[tool]()
        else:
            supported = ", ".join(sorted(uninstallers.keys()))
            err_console.print(f"[red]Unknown tool: {tool}. Supported: {supported}[/red]")
            raise SystemExit(1)
    except SystemExit:
        raise
    except Exception:
        err_console.print(f"[yellow]MCP server removal failed for {tool}. Keeping instructions.[/yellow]")
        uninstall_ok = False

    if uninstall_ok:
        try:
            remove_agent_instructions(tool)
        except Exception as e:
            err_console.print(f"[yellow]Warning: could not remove agent instructions: {e}[/yellow]")


# ---------------------------------------------------------------------------
# Uninstall helpers
# ---------------------------------------------------------------------------


def _report_uninstall(plan: _cfg.ConfigPlan, display_name: str) -> None:
    if plan.action == "missing":
        console.print(f"[dim]No config found at {plan.path}. Nothing to remove.[/dim]")
    elif plan.action == "absent":
        console.print(f"[dim]SessionFS not found in {display_name} config. Nothing to remove.[/dim]")
    else:
        console.print(f"[green]Removed SessionFS MCP server from {display_name}.[/green]")
        console.print(f"  Restart {display_name} to apply.")


def _uninstall_json_configs(paths: list[Path], display_name: str, allow_empty: bool = False) -> None:
    """Remove sessionfs from the mcpServers of every file in *paths*.

    All files are preflighted first (nothing changes unless every one can be
    edited safely); each is then written on its own, atomically, with a
    timestamped backup, and each result is reported; see cli/mcp_config_io.
    ``allow_empty`` treats a zero-byte / whitespace-only file as an empty
    config (Antigravity ships ``mcp_config.json`` as an empty file).
    """

    def _plan(p: Path) -> _cfg.ConfigPlan:
        return _cfg.plan_uninstall(p, "sessionfs", display_name, allow_empty=allow_empty)

    try:
        plans = [_plan(p) for p in paths]
    except (_cfg.ConfigFileError, OSError) as e:
        console.print(f"[yellow]{e} Skipping.[/yellow]")
        raise RuntimeError(str(e)) from e
    results = _cfg.apply_plans(plans, lambda plan: _plan(plan.path))
    for result in results:
        if result.ok:
            _report_uninstall(result.plan, display_name)
        else:
            console.print(f"[yellow]Not changed: {result.error}[/yellow]")
    failed = [r for r in results if not r.ok]
    if failed:
        raise RuntimeError("; ".join(str(r.error) for r in failed))


def _uninstall_json_config(
    config_path: Path, display_name: str, allow_empty: bool = False,
) -> None:
    """Remove sessionfs from a JSON config file's mcpServers (atomic, preflighted)."""
    _uninstall_json_configs([config_path], display_name, allow_empty=allow_empty)


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
            raise RuntimeError(f"codex mcp remove failed: {result.stderr.strip()}")
    except FileNotFoundError:
        console.print("[dim]'codex' command not found. Skipping.[/dim]")
        raise RuntimeError("codex not found")


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
            raise RuntimeError(f"gemini mcp remove failed: {result.stderr.strip()}")
    except FileNotFoundError:
        console.print("[dim]'gemini' command not found. Skipping.[/dim]")
        raise RuntimeError("gemini not found")


def _uninstall_vscode_extension(tool: str = "cline") -> None:
    """Remove SessionFS from a VS Code extension's MCP config."""
    ext_ids = {
        "cline": "saoudrizwan.claude-dev",
        "roo-code": "rooveterinaryinc.roo-cline",
        "kilo-code": "kilocode.kilo-code",
    }
    ext_id = ext_ids.get(tool, ext_ids["cline"])
    display = {"cline": "Cline", "roo-code": "Roo Code", "kilo-code": "Kilo Code"}.get(tool, "Cline")

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


def _install_json_configs(
    paths: list[Path], mcp_config: dict, display_name: str, allow_empty: bool = False,
) -> None:
    """Add sessionfs to the mcpServers of every file in *paths*.

    Every file is preflighted (parsed, permission- and symlink-checked)
    before any is changed; each is then written on its own — atomically,
    permissions kept, the previous version kept as a timestamped backup —
    and every target's result is reported. See cli/mcp_config_io.
    Handles malformed JSON, non-dict roots, null mcpServers and stale
    registrations; ``allow_empty`` treats a zero-byte file as ``{}``.
    """

    def _plan(p: Path) -> _cfg.ConfigPlan:
        return _cfg.plan_install(p, "sessionfs", mcp_config, display_name, allow_empty=allow_empty)

    try:
        plans = [_plan(p) for p in paths]
    except _cfg.ConfigFileError as e:
        err_console.print(f"[red]{e}[/red]")
        raise SystemExit(1)
    except OSError as e:
        err_console.print(f"[red]Cannot read the {display_name} MCP config: {e}[/red]")
        raise SystemExit(1)

    results = _cfg.apply_plans(plans, lambda plan: _plan(plan.path))
    for result in results:
        plan = result.plan
        if not result.ok:
            err_console.print(f"[red]Not changed: {result.error}[/red]")
            continue
        if plan.action == "unchanged":
            console.print(f"[dim]SessionFS MCP server already configured in {display_name}.[/dim]")
            continue
        verb = "Updated" if plan.action == "updated" else "Added"
        console.print(f"[green]{verb} SessionFS MCP server in {display_name}.[/green]")
        console.print(f"  Config: {plan.path}")
        if result.backup is not None:
            console.print(f"  Previous version saved as {result.backup}")
        console.print(f"  Restart {display_name} to activate.")
    if any(not r.ok for r in results):
        if any(r.ok for r in results):
            err_console.print("[yellow]Some config files were updated and some were not (see above).[/yellow]")
        raise SystemExit(1)


def _install_json_config(
    config_path: Path, mcp_config: dict, display_name: str,
    allow_empty: bool = False,
) -> None:
    """Shared installer for JSON-based MCP configs (atomic, preflighted)."""
    _install_json_configs([config_path], mcp_config, display_name, allow_empty=allow_empty)


def _install_claude_code(mcp_config: dict) -> None:
    """Add SessionFS to Claude Code's MCP config."""
    config_path = Path.home() / ".claude.json"
    _install_json_config(config_path, mcp_config, "Claude Code")
    console.print()
    console.print("Your AI agent can now search past sessions:")
    console.print('  "Has anyone on the team seen this error before?"')
    console.print('  "What was the fix for the auth middleware bug?"')


def _install_cursor(mcp_config: dict) -> None:
    """Add SessionFS to Cursor's MCP config."""
    config_path = Path.home() / ".cursor" / "mcp.json"
    _install_json_config(config_path, mcp_config, "Cursor")


def _install_copilot(mcp_config: dict) -> None:
    """Add SessionFS to Copilot CLI's MCP config."""
    config_path = Path.home() / ".copilot" / "config.json"
    _install_json_config(config_path, mcp_config, "Copilot CLI")


def _install_codex(mcp_config: dict) -> None:
    """Add SessionFS to Codex CLI via `codex mcp add`."""
    import subprocess

    sfs_path = mcp_config["command"]
    expected_args = f"{sfs_path} mcp serve"

    # Check if already registered with correct command + args
    needs_remove = False
    try:
        result = subprocess.run(
            ["codex", "mcp", "get", "sessionfs"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            if expected_args in result.stdout:
                console.print("[dim]SessionFS MCP server already configured in Codex.[/dim]")
                return
            needs_remove = True
    except FileNotFoundError:
        err_console.print("[red]'codex' command not found. Install Codex CLI first.[/red]")
        raise SystemExit(1)
    except subprocess.TimeoutExpired:
        err_console.print("[yellow]Timed out checking Codex config. Proceeding with install.[/yellow]")

    # Remove stale registration in a separate step
    if needs_remove:
        try:
            rm = subprocess.run(
                ["codex", "mcp", "remove", "sessionfs"],
                capture_output=True, text=True, timeout=5,
            )
            if rm.returncode != 0:
                err_console.print(f"[red]Failed to remove stale Codex registration: {rm.stderr.strip()}[/red]")
                console.print("  Try manually: codex mcp remove sessionfs && codex mcp add sessionfs -- sfs mcp serve")
                raise SystemExit(1)
        except subprocess.TimeoutExpired:
            err_console.print("[red]Timed out removing stale Codex registration.[/red]")
            console.print("  Try manually: codex mcp remove sessionfs && codex mcp add sessionfs -- sfs mcp serve")
            raise SystemExit(1)

    # Register via codex mcp add
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
    except subprocess.TimeoutExpired:
        err_console.print("[red]Timed out registering with Codex.[/red]")
        console.print("  Try manually: codex mcp add sessionfs -- sfs mcp serve")
        raise SystemExit(1)


def _install_gemini(mcp_config: dict) -> None:
    """Add SessionFS to Gemini CLI via `gemini mcp add`."""
    import subprocess

    sfs_path = mcp_config["command"]
    expected_args = f"{sfs_path} mcp serve"

    # Check if already registered with correct command + args
    needs_remove = False
    try:
        result = subprocess.run(
            ["gemini", "mcp", "list"],
            capture_output=True, text=True, timeout=5,
        )
        if "sessionfs" in result.stdout:
            if expected_args in result.stdout:
                console.print("[dim]SessionFS MCP server already configured in Gemini CLI.[/dim]")
                return
            needs_remove = True
    except FileNotFoundError:
        err_console.print("[red]'gemini' command not found. Install Gemini CLI first.[/red]")
        raise SystemExit(1)
    except subprocess.TimeoutExpired:
        err_console.print("[yellow]Timed out checking Gemini config. Proceeding with install.[/yellow]")

    # Remove stale registration in a separate step
    if needs_remove:
        try:
            rm = subprocess.run(
                ["gemini", "mcp", "remove", "sessionfs"],
                capture_output=True, text=True, timeout=5,
            )
            if rm.returncode != 0:
                err_console.print(f"[red]Failed to remove stale Gemini registration: {rm.stderr.strip()}[/red]")
                console.print("  Try manually: gemini mcp remove sessionfs && gemini mcp add sessionfs sfs mcp serve")
                raise SystemExit(1)
        except subprocess.TimeoutExpired:
            err_console.print("[red]Timed out removing stale Gemini registration.[/red]")
            console.print("  Try manually: gemini mcp remove sessionfs && gemini mcp add sessionfs sfs mcp serve")
            raise SystemExit(1)

    # Register via gemini mcp add
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
    except subprocess.TimeoutExpired:
        err_console.print("[red]Timed out registering with Gemini CLI.[/red]")
        console.print("  Try manually: gemini mcp add sessionfs sfs mcp serve")
        raise SystemExit(1)


def _install_amp(mcp_config: dict) -> None:
    """Add SessionFS to Amp's MCP config."""
    config_path = Path.home() / ".amp" / "config.json"
    _install_json_config(config_path, mcp_config, "Amp")


def _install_vscode_extension(mcp_config: dict, tool: str = "cline") -> None:
    """Add SessionFS to a VS Code extension's MCP config (Cline, Roo Code, or Kilo Code)."""
    ext_ids = {
        "cline": "saoudrizwan.claude-dev",
        "roo-code": "rooveterinaryinc.roo-cline",
        "kilo-code": "kilocode.kilo-code",
    }
    ext_id = ext_ids.get(tool, ext_ids["cline"])
    display = {"cline": "Cline", "roo-code": "Roo Code", "kilo-code": "Kilo Code"}.get(tool, "Cline")

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

    _install_json_config(config_path, mcp_config, display)


def _antigravity_mcp_config_paths() -> list[Path]:
    """Antigravity MCP config files, newest layout first.

    ``~/.gemini/config/mcp_config.json`` is read by the agy CLI and current
    desktop builds; ``~/.gemini/antigravity/mcp_config.json`` by older
    desktop builds.
    """
    gemini = Path.home() / ".gemini"
    return [gemini / "config" / "mcp_config.json", gemini / "antigravity" / "mcp_config.json"]


def _antigravity_installed() -> bool:
    gemini = Path.home() / ".gemini"
    return any(
        (gemini / name).is_dir()
        for name in ("antigravity", "antigravity-cli", "antigravity-ide")
    )


def _install_antigravity(mcp_config: dict) -> None:
    """Add SessionFS to Antigravity's MCP config (desktop app, agy CLI, IDE).

    Always writes the current config (``~/.gemini/config/mcp_config.json``,
    read by the agy CLI and current desktop builds — created if missing),
    plus every legacy config that already exists. Other servers in the file
    are preserved; an unparseable file is refused, never overwritten.
    """
    if not _antigravity_installed():
        err_console.print("[red]Antigravity not found (no ~/.gemini/antigravity*).[/red]")
        err_console.print("Install and open Antigravity or the agy CLI first.")
        raise SystemExit(1)
    current, *legacy = _antigravity_mcp_config_paths()
    targets = [current] + [p for p in legacy if p.exists() or p.is_symlink()]
    # Every file is preflighted before any is written.
    _install_json_configs(targets, mcp_config, "Antigravity", allow_empty=True)


def _uninstall_antigravity() -> None:
    """Remove SessionFS from every Antigravity MCP config file."""
    targets = [p for p in _antigravity_mcp_config_paths() if p.exists() or p.is_symlink()]
    if targets:
        _uninstall_json_configs(targets, "Antigravity", allow_empty=True)


@mcp_app.command("index")
def index() -> None:
    """Rebuild the full-text search index from all local sessions."""
    from sessionfs.cli.common import get_store_dir
    from sessionfs.mcp.search import SessionSearchIndex

    store_dir = get_store_dir()
    search_db = store_dir / "search.db"
    search = SessionSearchIndex(search_db)
    search.initialize()

    count = search.reindex_all(store_dir, force=True)
    search.close()

    console.print(f"[green]Search index rebuilt: {count} sessions indexed.[/green]")

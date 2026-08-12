"""Interactive setup wizard: sfs init."""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import typer

from sessionfs.cli.common import console, get_store_dir, open_store
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

    # Cursor data paths (must match daemon config)
    if is_mac:
        cursor_data = home / "Library" / "Application Support" / "Cursor"
    else:
        cursor_data = home / ".config" / "Cursor"

    # Amp data paths (must match daemon config — XDG_DATA_HOME or ~/.local/share/amp)
    import os
    xdg_data = os.environ.get("XDG_DATA_HOME")
    amp_data = Path(xdg_data) / "amp" if xdg_data else home / ".local" / "share" / "amp"

    return [
        ToolInfo(
            name="Claude Code",
            config_key="claude_code",
            detect_paths=[
                home / ".claude",
            ],
        ),
        ToolInfo(
            name="Cursor",
            config_key="cursor",
            detect_paths=[
                cursor_data,  # App Support/Cursor (macOS) or .config/Cursor (Linux)
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
                home / ".copilot",
            ],
        ),
        ToolInfo(
            name="Amp",
            config_key="amp",
            detect_paths=[
                amp_data,  # ~/.local/share/amp or $XDG_DATA_HOME/amp
            ],
        ),
        ToolInfo(
            name="Cline",
            config_key="cline",
            detect_paths=[
                vscode_global / "saoudrizwan.claude-dev",
            ],
        ),
        ToolInfo(
            name="Roo Code",
            config_key="roo_code",
            detect_paths=[
                vscode_global / "rooveterinaryinc.roo-cline",
            ],
        ),
        ToolInfo(
            name="Kilo Code",
            config_key="kilo_code",
            detect_paths=[
                vscode_global / "kilocode.kilo-code",
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

    # Telemetry disclosure — MUST print before the daemon starts (the daemon can
    # emit funnel events immediately) and before this wizard's own emits, so the
    # user learns the opt-outs before anything is ever sent.
    console.print(
        "[dim]SessionFS collects anonymous usage telemetry (random install id, "
        "version, OS, event name — never paths, session content, or personal "
        "data). Disable anytime: export SFS_NO_TELEMETRY=1, or "
        "`sfs config set telemetry.enabled false`. Details: docs/telemetry.md[/dim]"
    )
    console.print()
    try:
        from sessionfs.telemetry import mark_disclosure_shown

        mark_disclosure_shown()
    except Exception:
        pass

    # --- Step 3: Start daemon ---
    start_daemon = typer.confirm("Start the SessionFS daemon now?", default=True)

    # ACTUAL spawn success — start_daemon is only the user's intent. If the
    # spawn fails, the magic moment must fall back to converting itself (there
    # is no daemon whose capture it could wait for).
    daemon_ok = False
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
            daemon_ok = True

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
            "kilo_code": "kilo-code",
        }
        for tool in detected:
            if tool.info.config_key in enabled_keys:
                mcp_tool = _config_key_to_tool.get(tool.info.config_key)
                if mcp_tool:
                    # Install MCP server first, then inject instructions only on success
                    try:
                        console.print(f"  Installing MCP for {mcp_tool}...")
                        from sessionfs.cli.cmd_mcp import _install_mcp_for_tool
                        _install_mcp_for_tool(mcp_tool)
                        inject_agent_instructions(mcp_tool)
                    except (SystemExit, Exception):
                        console.print(f"  [dim]MCP install skipped for {mcp_tool} — no instructions injected[/dim]")

    # --- Magic moment: capture the most-recent native session (non-fatal) ---
    _try_magic_moment(
        enabled_keys, daemon_started=daemon_ok or _daemon_is_running()
    )

    # --- Next steps ---
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  [cyan]sfs list[/cyan]              \u2014 View captured sessions")
    console.print("  [cyan]sfs show[/cyan] <id>         \u2014 Inspect a session")
    console.print("  [cyan]sfs resume[/cyan] <id>       \u2014 Resume in any tool")
    console.print("  [cyan]sfs search[/cyan] \"query\"    \u2014 Search past sessions")
    console.print()

    # v0.15 telemetry: wizard completed successfully
    try:
        from sessionfs.telemetry import emit
        emit("init_completed")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Magic moment: capture the most-recent native session after init
# ---------------------------------------------------------------------------


def _discover_all_native_sessions(enabled_keys: set[str]) -> list[dict]:
    """Discover native sessions across all enabled tools.

    Returns a list of dicts with keys session_id, path, tool, mtime,
    size_bytes, and optional title/name/first_prompt. Sorted by mtime
    descending (most recent first). Failures for individual tools are
    silently swallowed — the wizard must never crash on discovery.
    """
    home = Path.home()
    is_mac = platform.system() == "Darwin"
    all_sessions: list[dict] = []

    # -- Claude Code --
    if "claude_code" in enabled_keys:
        try:
            from sessionfs.watchers.claude_code import discover_sessions

            for s in discover_sessions(home / ".claude"):
                s["tool"] = "claude-code"
                all_sessions.append(s)
        except Exception:
            pass

    # -- Codex --
    if "codex" in enabled_keys:
        try:
            from sessionfs.watchers.codex import discover_codex_sessions

            for s in discover_codex_sessions(home / ".codex"):
                s["tool"] = "codex"
                all_sessions.append(s)
        except Exception:
            pass

    # -- Gemini CLI --
    if "gemini" in enabled_keys:
        try:
            from sessionfs.converters.gemini_to_sfs import discover_gemini_sessions

            for s in discover_gemini_sessions(home / ".gemini"):
                s["tool"] = "gemini-cli"
                all_sessions.append(s)
        except Exception:
            pass

    # -- Cursor --
    if "cursor" in enabled_keys:
        try:
            from sessionfs.converters.cursor_to_sfs import discover_cursor_composers

            composers = discover_cursor_composers()
            for c in composers:
                if c.is_archived:
                    continue
                all_sessions.append({
                    "session_id": c.composer_id,
                    "path": str(_cursor_global_db_path(is_mac)),
                    "tool": "cursor",
                    "mtime": c.last_updated_at / 1000.0 if c.last_updated_at else 0.0,
                    "size_bytes": 0,
                    "name": c.name or "",
                    "workspace_folder": c.workspace_folder or "",
                })
        except Exception:
            pass

    # -- Copilot --
    if "copilot" in enabled_keys:
        try:
            from sessionfs.converters.copilot_to_sfs import discover_copilot_sessions

            for s in discover_copilot_sessions(home / ".copilot"):
                s["tool"] = "copilot-cli"
                all_sessions.append(s)
        except Exception:
            pass

    # -- Amp --
    if "amp" in enabled_keys:
        try:
            from sessionfs.converters.amp_to_sfs import discover_amp_sessions

            import os as _os
            xdg_data = _os.environ.get("XDG_DATA_HOME")
            amp_data = Path(xdg_data) / "amp" if xdg_data else home / ".local" / "share" / "amp"
            for s in discover_amp_sessions(amp_data):
                s["tool"] = "amp"
                all_sessions.append(s)
        except Exception:
            pass

    # -- Cline --
    if "cline" in enabled_keys:
        _discover_cline_variants(all_sessions, is_mac, "cline")

    # -- Roo Code --
    if "roo_code" in enabled_keys:
        _discover_cline_variants(all_sessions, is_mac, "roo-code")

    # -- Kilo Code --
    if "kilo_code" in enabled_keys:
        _discover_cline_variants(all_sessions, is_mac, "kilo-code")

    all_sessions.sort(key=lambda s: s.get("mtime", 0), reverse=True)
    return all_sessions


def _cursor_global_db_path(is_mac: bool) -> Path:
    """Return the Cursor global DB path for the current platform."""
    if is_mac:
        return Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    return Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def _discover_cline_variants(
    sessions: list[dict], is_mac: bool, tool: str,
) -> None:
    """Discover Cline / Roo Code / Kilo Code sessions and append to *sessions*."""
    try:
        from sessionfs.converters.cline_to_sfs import discover_cline_sessions

        home = Path.home()
        vscode_global = (
            home / "Library" / "Application Support" / "Code" / "User" / "globalStorage"
            if is_mac
            else home / ".config" / "Code" / "User" / "globalStorage"
        )
        storage_dir_map = {
            "cline": vscode_global / "saoudrizwan.claude-dev",
            "roo-code": vscode_global / "rooveterinaryinc.roo-cline",
            "kilo-code": vscode_global / "kilocode.kilo-code",
        }
        storage_dir = storage_dir_map.get(tool)
        if storage_dir is None:
            return
        for s in discover_cline_sessions(storage_dir, tool=tool):
            s["tool"] = tool
            sessions.append(s)
    except Exception:
        pass


def _capture_one_session(
    session_info: dict, deadline: float | None = None
) -> tuple[str, str, int] | None:
    """Capture a single native session into .sfs format.

    Returns (sfs_id, title, message_count) on success, or None on failure.
    Follows the same parse→convert→index pattern as cmd_recapture.py.
    """
    tool: str = session_info["tool"]
    native_path = Path(session_info["path"])
    native_session_id: str = session_info.get("session_id", "")

    try:
        from sessionfs.session_id import session_id_from_native
    except Exception:
        return None
    sfs_id = session_id_from_native(native_session_id)

    # Honor the user's deletions: a session in deleted.json was intentionally
    # removed — the magic moment must never resurrect it (same contract as the
    # daemon's capture guard).
    try:
        from sessionfs.store.deleted import is_excluded

        if is_excluded(sfs_id, base_dir=get_store_dir()):
            return None
    except Exception:
        pass

    store = open_store()
    scratch_root: Path | None = None
    try:
        # NEVER overwrite an existing capture (the daemon's should_recapture
        # compaction guard protects richer captures; init must not bypass it).
        # An already-captured session — possibly grabbed by the daemon we just
        # started — IS the magic moment: display it. A dir WITHOUT a manifest
        # means the daemon is mid-write: hands off entirely.
        existing_dir = store.get_session_dir(sfs_id)
        if existing_dir is not None:
            if (existing_dir / "manifest.json").exists():
                return _read_display_info(existing_dir, sfs_id, session_info)
            return None  # daemon mid-write — do not touch

        # Convert into a SCRATCH dir (same filesystem as the store, so the
        # final install is an atomic rename). The live store is never written
        # concurrently with the daemon, and a timed-out/abandoned capture
        # thread can only ever litter scratch — swept on the next init.
        import os
        import shutil
        import tempfile

        sessions_root = store.sessions_dir
        scratch_base = sessions_root.parent / ".magic-tmp"
        _sweep_stale_scratch(scratch_base)
        scratch_base.mkdir(parents=True, exist_ok=True)
        scratch_root = Path(tempfile.mkdtemp(prefix=f"{os.getpid()}-", dir=scratch_base))
        session_dir = scratch_root / f"{sfs_id}.sfs"
        session_dir.mkdir(parents=True, exist_ok=True)

        if tool == "claude-code":
            from sessionfs.watchers.claude_code import parse_session
            from sessionfs.spec.convert_cc import convert_session

            cc_session = parse_session(native_path, copy_on_read=True)
            convert_session(cc_session, session_dir.parent, session_id=sfs_id, session_dir=session_dir)

        elif tool == "codex":
            from sessionfs.watchers.codex import parse_codex_session, convert_codex_to_sfs

            codex_session = parse_codex_session(native_path)
            # Mirror the daemon: rollouts injected by `sfs resume` are NOT
            # native Codex work — capturing one as "your most recent session"
            # would demo the magic moment on a synthetic import.
            if getattr(codex_session, "originator", None) == "sessionfs_import":
                return None
            convert_codex_to_sfs(codex_session, session_dir, session_id=sfs_id)

        elif tool == "gemini-cli":
            from sessionfs.converters.gemini_to_sfs import parse_gemini_session, convert_gemini_to_sfs

            gemini_session = parse_gemini_session(native_path)
            convert_gemini_to_sfs(gemini_session, session_dir, session_id=sfs_id)

        elif tool == "cursor":
            from sessionfs.converters.cursor_to_sfs import parse_cursor_composer, convert_cursor_to_sfs

            session = parse_cursor_composer(native_session_id, global_db=native_path)
            if session.message_count == 0:
                return None  # refuse empty captures
            convert_cursor_to_sfs(session, session_dir, session_id=sfs_id)

        elif tool == "copilot-cli":
            from sessionfs.converters.copilot_to_sfs import convert_copilot_to_sfs

            convert_copilot_to_sfs(native_path, session_dir, session_id=sfs_id)

        elif tool == "amp":
            from sessionfs.converters.amp_to_sfs import convert_amp_to_sfs

            convert_amp_to_sfs(native_path, session_dir, session_id=sfs_id)

        elif tool in ("cline", "roo-code", "kilo-code"):
            from sessionfs.converters.cline_to_sfs import parse_cline_session, convert_cline_to_sfs

            cline_session = parse_cline_session(native_path, tool=tool)
            convert_cline_to_sfs(cline_session, session_dir, session_id=sfs_id)

        else:
            return None

        # Atomic install: rename scratch → store. POSIX rename refuses an
        # existing non-empty target dir, so if the daemon captured this
        # session in the meantime IT wins and we display its result instead.
        manifest_path = session_dir / "manifest.json"
        if not manifest_path.exists():
            return None  # converter produced nothing usable
        # Cooperative timeout: past (deadline - margin) the caller has moved on
        # and may exit — do NOT install or index (scratch is swept later). The
        # margin keeps install+index inside the caller's join window, so a
        # live-store write can never be truncated by interpreter exit.
        if deadline is not None:
            import time as _time

            if _time.monotonic() > deadline - 1.5:
                return None

        target_dir = store.sessions_dir / f"{sfs_id}.sfs"
        # POSIX rename SUCCEEDS onto an existing EMPTY dir — and a dir the
        # daemon just allocated is exactly that. Any existing target (empty or
        # not) means the daemon owns this session now: back off. The remaining
        # exists→rename window is sub-millisecond and loses to ENOTEMPTY once
        # the daemon writes its first file.
        if target_dir.exists():
            if (target_dir / "manifest.json").exists():
                return _read_display_info(target_dir, sfs_id, session_info)
            return None  # daemon mid-write — its capture supersedes ours
        try:
            os.rename(session_dir, target_dir)
        except OSError:
            fresh = store.get_session_dir(sfs_id)
            if fresh is not None and (fresh / "manifest.json").exists():
                return _read_display_info(fresh, sfs_id, session_info)
            return None
        session_dir = target_dir
        manifest_path = session_dir / "manifest.json"

        # Update index and tracked-session ref
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            store.upsert_session_metadata(sfs_id, manifest, str(session_dir))

        stat = native_path.stat()
        from sessionfs.watchers.base import NativeSessionRef

        project_path = session_info.get("project_path") or session_info.get("workspace_folder") or ""
        ref = NativeSessionRef(
            tool=tool,
            native_session_id=native_session_id,
            native_path=str(native_path),
            sfs_session_id=sfs_id,
            last_mtime=stat.st_mtime,
            last_size=stat.st_size,
            last_captured_at=datetime.now(timezone.utc).isoformat(),
            project_path=project_path if project_path else None,
        )
        store.upsert_tracked_session(ref)

        # Read message count from manifest
        message_count = (
            manifest.get("stats", {}).get("message_count", 0)
            if manifest_path.exists()
            else 0
        )

        # Derive a display title
        title = (
            session_info.get("first_prompt")
            or session_info.get("title")
            or session_info.get("name")
            or session_info.get("task_label")
            or sfs_id
        )
        if isinstance(title, str) and len(title) > 60:
            title = title[:57] + "..."

        return sfs_id, str(title), message_count

    except Exception:
        return None
    finally:
        try:
            if scratch_root is not None and scratch_root.exists():
                import shutil

                shutil.rmtree(scratch_root, ignore_errors=True)
        except Exception:
            pass
        store.close()


def _sweep_stale_scratch(scratch_base: Path) -> None:
    """Best-effort removal of leftover magic-capture scratch dirs (>1h old) —
    e.g. from a timed-out capture thread abandoned by a previous init."""
    try:
        import shutil
        import time as _time

        if not scratch_base.is_dir():
            return
        cutoff = _time.time() - 3600
        for entry in scratch_base.iterdir():
            try:
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
            except Exception:
                continue
    except Exception:
        pass


def _read_display_info(session_dir: Path, sfs_id: str, info: dict) -> tuple | None:
    """Magic-moment display data for an ALREADY-captured session."""
    try:
        manifest = json.loads((session_dir / "manifest.json").read_text())
        message_count = manifest.get("stats", {}).get("message_count", 0)
        title = (
            manifest.get("title")
            or info.get("first_prompt")
            or info.get("title")
            or sfs_id
        )
        if isinstance(title, str) and len(title) > 60:
            title = title[:57] + "..."
        return sfs_id, str(title), message_count
    except Exception:
        return None


def _daemon_is_running() -> bool:
    """True if ANY live sfsd owns this store (pre-existing daemons included —
    a user re-running init may decline the start prompt precisely because one
    is already running; init must stay read-only then too). Never raises."""
    try:
        pid_file = get_store_dir() / "sfsd.pid"
        pid = int(pid_file.read_text().strip())
        import os

        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _wait_for_daemon_capture_any(
    candidates: list[dict], *, deadline: float
) -> tuple | None:
    """Poll for the FIRST of the candidate sessions the daemon has captured
    (manifest present). Returns (sfs_id, title, message_count, session_info)
    or None at the deadline. Read-only."""
    import time as _time

    from sessionfs.session_id import session_id_from_native

    ids: list[tuple[str, dict]] = []
    for info in candidates:
        try:
            sid = session_id_from_native(info.get("session_id", ""))
            # Honor deletions here too — a user-deleted session must never be
            # the magic moment, even via the daemon-wait display path.
            try:
                from sessionfs.store.deleted import is_excluded

                if is_excluded(sid, base_dir=get_store_dir()):
                    continue
            except Exception:
                pass
            ids.append((sid, info))
        except Exception:
            continue
    if not ids:
        return None

    while _time.monotonic() < deadline:
        try:
            store = open_store(initialize=False)
            try:
                for sfs_id, info in ids:
                    d = store.get_session_dir(sfs_id)
                    if d is not None and (d / "manifest.json").exists():
                        disp = _read_display_info(d, sfs_id, info)
                        if disp is not None:
                            return (*disp, info)
            finally:
                store.close()
        except Exception:
            return None
        _time.sleep(0.5)
    return None


def _wait_for_daemon_capture(
    session_info: dict, *, timeout_s: float = 10.0
) -> tuple[str, str, int] | None:
    """Wait (bounded, polling) for the freshly-started daemon to capture the
    most-recent session, then return its display info. Read-only — init never
    writes to the store while a daemon is running."""
    import time as _time

    try:
        from sessionfs.session_id import session_id_from_native

        sfs_id = session_id_from_native(session_info.get("session_id", ""))
    except Exception:
        return None

    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        try:
            store = open_store(initialize=False)
            try:
                d = store.get_session_dir(sfs_id)
                if d is not None and (d / "manifest.json").exists():
                    return _read_display_info(d, sfs_id, session_info)
            finally:
                store.close()
        except Exception:
            return None
        _time.sleep(0.5)
    return None


def _try_magic_moment(enabled_keys: set[str], *, daemon_started: bool = False) -> None:
    """Discover and surface the most-recent native session — non-fatal.

    RACE-FREE BY CONSTRUCTION: when the wizard just STARTED the daemon, init
    performs NO conversion of its own — the daemon's initial scan captures the
    session within seconds, and init merely WAITS (bounded) for the manifest to
    appear and displays it. There is never a second writer. Only when no
    daemon was started does init convert (scratch + atomic install), and then
    no concurrent writer exists either.

    On success: prints the captured session line + magic prompt + emits
    first_capture. On timeout or any failure: falls through silently
    (the wizard's next-steps block prints as usual). Zero-session machines
    get a graceful line.
    """
    # 1. Discover sessions — BOUNDED (a huge/slow native store must not stall
    # the wizard; discovery gets a 5s sub-budget of the overall 10s).
    sessions: list[dict] = []
    discovery_failed = False
    _disc_box: list = []

    def _discover_target() -> None:
        try:
            _disc_box.append(_discover_all_native_sessions(enabled_keys))
        except Exception:
            _disc_box.append(None)

    _disc_thread = threading.Thread(target=_discover_target, daemon=True)
    _disc_thread.start()
    _disc_thread.join(timeout=5.0)
    if _disc_thread.is_alive():
        return  # discovery too slow — skip the magic moment, wizard moves on
    discovered = _disc_box[0] if _disc_box else None
    if discovered is None:
        discovery_failed = True
    else:
        sessions = discovered

    # Zero-session machines
    if not sessions and not discovery_failed and enabled_keys:
        console.print()
        console.print(
            "[dim]No existing sessions found — your NEXT session will be "
            "captured automatically.[/dim]"
        )
        return

    if not sessions:
        return  # nothing to capture

    # Up to 3 candidates: the newest can be legitimately uncapturable (a
    # sessionfs_import rollout, a zero-message composer) — an older valid
    # session still deserves the magic moment. One shared 10s budget.
    import time as _time

    candidates = sessions[:3]
    deadline = _time.monotonic() + 10.0
    result: tuple[str, str, int] | None = None
    most_recent = candidates[0]

    if daemon_started:
        # The daemon owns all writes — wait for ANY candidate's capture to
        # appear (it may skip the newest by design).
        waited = _wait_for_daemon_capture_any(candidates, deadline=deadline)
        if waited is not None:
            sfs_id_w, title_w, count_w, most_recent = waited
            result = (sfs_id_w, title_w, count_w)
    else:
        # No daemon running → no concurrent writer; convert ourselves in a
        # scratch dir with atomic install, on a watchdog thread per candidate.
        for cand in candidates:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return
            result_container: list = []

            def _capture_target(info: dict = cand) -> None:
                try:
                    result_container.append(_capture_one_session(info, deadline))
                except Exception:
                    result_container.append(None)

            capture_thread = threading.Thread(target=_capture_target, daemon=True)
            capture_thread.start()
            capture_thread.join(timeout=remaining)

            if capture_thread.is_alive():
                # Timed out — the deadline check inside _capture_one_session
                # guarantees the abandoned thread can only ever write scratch.
                return

            candidate_result = result_container[0] if result_container else None
            if candidate_result is not None:
                result = candidate_result
                most_recent = cand
                break
    if result is None:
        return  # capture failed — fall through silently

    sfs_id, title, message_count = result

    # 3. Success — print the magic moment
    tool_display = most_recent.get("tool", "unknown")
    console.print()
    console.print(
        f"[green]✓[/green] Captured your most recent session: "
        f"[bold]{title}[/bold] ([dim]{tool_display}[/dim], {message_count} messages)"
    )
    console.print()
    console.print(
        '[bold]Now ask your agent:[/bold] [cyan]"what did we do last session?"[/cyan]'
    )
    console.print()

    # 4. Emit first_capture telemetry (guarded, non-fatal)
    try:
        from sessionfs.telemetry import emit_once
        emit_once("first_capture", "first_capture")
    except Exception:
        pass

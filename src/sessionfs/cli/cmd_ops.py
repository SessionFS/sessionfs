"""Session operation commands: sfs resume, checkpoint, fork."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import typer

import re

from sessionfs.cli.common import (
    console,
    err_console,
    get_session_dir_or_exit,
    open_store,
    resolve_session_id,
)
from sessionfs.cli.sfs_to_cc import reverse_convert_session


def resume(
    session_id: str = typer.Argument(help="Session ID or prefix."),
    project: str | None = typer.Option(None, help="Target project path (overrides workspace)."),
    tool: str = typer.Option(
        "claude-code", "--in",
        help="Target tool: claude-code, codex, copilot, gemini, cursor, cline, roo-code",
    ),
) -> None:
    """Resume a session in an AI tool."""
    store = open_store()
    try:
        full_id = resolve_session_id(store, session_id)
        session_dir = get_session_dir_or_exit(store, full_id)

        manifest = json.loads((session_dir / "manifest.json").read_text())

        # Record lineage — the new session is forked from this one
        manifest["_resume_parent_id"] = full_id
        manifest["_resume_source_tool"] = manifest.get("source", {}).get("tool", "unknown")

        # Determine project path
        target_path = project
        if not target_path:
            workspace_path = session_dir / "workspace.json"
            if workspace_path.exists():
                workspace = json.loads(workspace_path.read_text())
                target_path = workspace.get("root_path")

        # Fall back to CWD if original path doesn't exist
        if target_path and not Path(target_path).exists():
            console.print(f"[yellow]Original path {target_path} not found.[/yellow]")
            target_path = str(Path.cwd())
            console.print(f"Resuming in current directory: [bold]{target_path}[/bold]\n")

        if tool == "codex":
            _resume_in_codex(session_dir, manifest, target_path, full_id)
        elif tool == "copilot":
            _resume_in_copilot(session_dir, manifest, target_path, full_id)
        elif tool == "gemini":
            _resume_in_gemini(session_dir, manifest, target_path, full_id)
        elif tool == "cursor":
            err_console.print(
                "[yellow]Cursor is capture-only: content-addressed hashing prevents session injection.[/yellow]\n"
                "Resume in a bidirectional tool instead:\n"
                f"  sfs resume {session_id} --in claude-code\n"
                f"  sfs resume {session_id} --in codex\n"
                f"  sfs resume {session_id} --in copilot\n"
                f"  sfs resume {session_id} --in gemini\n"
                "\nLearn more: docs/compatibility.md"
            )
            raise SystemExit(1)
        elif tool == "cline":
            err_console.print(
                "[yellow]Cline is capture-only: VS Code extension state is too fragile for automated injection.[/yellow]\n"
                "Resume in a bidirectional tool instead:\n"
                f"  sfs resume {session_id} --in claude-code\n"
                f"  sfs resume {session_id} --in codex\n"
                f"  sfs resume {session_id} --in copilot\n"
                f"  sfs resume {session_id} --in gemini\n"
                "\nLearn more: docs/compatibility.md"
            )
            raise SystemExit(1)
        elif tool == "roo-code":
            err_console.print(
                "[yellow]Roo Code is capture-only: VS Code extension state is too fragile for automated injection.[/yellow]\n"
                "Resume in a bidirectional tool instead:\n"
                f"  sfs resume {session_id} --in claude-code\n"
                f"  sfs resume {session_id} --in codex\n"
                f"  sfs resume {session_id} --in copilot\n"
                f"  sfs resume {session_id} --in gemini\n"
                "\nLearn more: docs/compatibility.md"
            )
            raise SystemExit(1)
        elif tool == "amp":
            err_console.print(
                "[yellow]Amp is capture-only: cloud-first architecture means local files are a sync cache.[/yellow]\n"
                "Resume in a bidirectional tool instead:\n"
                f"  sfs resume {session_id} --in claude-code\n"
                f"  sfs resume {session_id} --in codex\n"
                f"  sfs resume {session_id} --in copilot\n"
                f"  sfs resume {session_id} --in gemini\n"
                "\nLearn more: docs/compatibility.md"
            )
            raise SystemExit(1)
        else:
            _resume_in_claude_code(session_dir, manifest, target_path)
    finally:
        store.close()


def _resume_in_claude_code(
    session_dir: Path, manifest: dict, target_path: str | None,
) -> None:
    if not target_path:
        err_console.print("[red]No project path found. Use --project to specify one.[/red]")
        raise SystemExit(1)

    result = reverse_convert_session(
        session_dir,
        manifest=manifest,
        target_project_path=target_path,
    )
    console.print("[green]Session resumed in Claude Code.[/green]")
    console.print(f"  CC Session ID: {result['cc_session_id']}")
    console.print(f"  JSONL: {result['jsonl_path']}")
    console.print(f"  Messages: {result['message_count']}")
    console.print()

    # Launch Claude Code resume directly
    claude_bin = shutil.which("claude")
    if claude_bin:
        console.print(f"Launching [bold]claude --resume {result['cc_session_id'][:12]}...[/bold]")
        subprocess.run([claude_bin, "--resume", result["cc_session_id"]], cwd=target_path)
    else:
        console.print(f"Open Claude Code in [bold]{target_path}[/bold] to continue.")


def _resume_in_codex(
    session_dir: Path, manifest: dict, target_path: str | None, sfs_id: str,
) -> None:
    from sessionfs.converters.sfs_to_codex import convert_sfs_to_codex
    from sessionfs.converters.codex_injector import inject_session

    cwd = target_path or "/tmp"

    # Convert .sfs → Codex JSONL
    result = convert_sfs_to_codex(session_dir, cwd=cwd)

    # Inject into Codex storage
    title = manifest.get("title") or sfs_id
    model = (manifest.get("model") or {}).get("model_id", "gpt-4.1")
    inject_result = inject_session(
        codex_jsonl=Path(result["jsonl_path"]),
        codex_session_id=result["codex_session_id"],
        cwd=cwd,
        title=title,
        model=model,
    )

    console.print("[green]Session resumed in Codex CLI.[/green]")
    console.print(f"  Codex Session ID: {result['codex_session_id'][:12]}...")
    console.print(f"  Rollout: {inject_result['rollout_path']}")
    console.print(f"  Messages: {result['message_count']}")
    console.print(f"  Index updated: {inject_result['index_updated']}")
    console.print()

    # Launch Codex resume directly
    codex_bin = shutil.which("codex")
    if codex_bin:
        console.print(f"Launching [bold]codex resume {result['codex_session_id'][:12]}...[/bold]")
        subprocess.run([codex_bin, "resume", result["codex_session_id"]])
    else:
        console.print(f"Run [bold]codex resume {result['codex_session_id']}[/bold] to continue.")


def _resume_in_copilot(
    session_dir: Path, manifest: dict, target_path: str | None, sfs_id: str,
) -> None:
    from sessionfs.converters.sfs_to_copilot import convert_sfs_to_copilot
    from sessionfs.converters.copilot_injector import inject_session as copilot_inject

    cwd = target_path or "/tmp"

    # Convert .sfs → Copilot events.jsonl
    result = convert_sfs_to_copilot(session_dir, cwd=cwd)

    # Inject into Copilot storage
    title = manifest.get("title") or sfs_id
    inject_result = copilot_inject(
        events_jsonl=Path(result["events_path"]),
        copilot_session_id=result["copilot_session_id"],
        cwd=cwd,
        title=title,
    )

    console.print("[green]Session resumed in Copilot CLI.[/green]")
    console.print(f"  Copilot Session ID: {result['copilot_session_id'][:12]}...")
    console.print(f"  Session: {inject_result['session_path']}")
    console.print(f"  Messages: {result['message_count']}")
    console.print()
    console.print(f"Open Copilot CLI in [bold]{cwd}[/bold] to continue.")


def _resume_in_gemini(
    session_dir: Path, manifest: dict, target_path: str | None, sfs_id: str,
) -> None:
    from sessionfs.converters.sfs_to_gemini import convert_sfs_to_gemini
    from sessionfs.converters.gemini_injector import inject_session

    project = target_path or "/tmp"
    result = convert_sfs_to_gemini(session_dir, project_path=project)

    inject_result = inject_session(
        gemini_json=Path(result["json_path"]),
        gemini_session_id=result["gemini_session_id"],
        project_path=project,
    )

    console.print("[green]Session resumed in Gemini CLI.[/green]")
    console.print(f"  Session: {result['gemini_session_id'][:8]}...")
    console.print(f"  File: {inject_result['session_path']}")
    console.print(f"  Messages: {result['message_count']}")
    console.print()

    # Launch Gemini resume directly
    gemini_bin = shutil.which("gemini")
    if gemini_bin:
        console.print("Launching [bold]gemini --resume latest[/bold]...")
        subprocess.run([gemini_bin, "--resume", "latest"], cwd=project)
    else:
        console.print(f"Open Gemini CLI in [bold]{project}[/bold] to continue.")


def checkpoint(
    session_id: str = typer.Argument(help="Session ID or prefix."),
    name: str = typer.Option(..., help="Checkpoint name."),
) -> None:
    """Create a named checkpoint of a session's current state."""
    store = open_store()
    try:
        full_id = resolve_session_id(store, session_id)
        session_dir = get_session_dir_or_exit(store, full_id)

        checkpoints_dir = session_dir / "checkpoints" / name
        if checkpoints_dir.exists():
            err_console.print(f"[red]Checkpoint '{name}' already exists.[/red]")
            raise SystemExit(1)

        checkpoints_dir.mkdir(parents=True)

        # Copy manifest and messages
        shutil.copy2(session_dir / "manifest.json", checkpoints_dir / "manifest.json")
        messages_path = session_dir / "messages.jsonl"
        if messages_path.exists():
            shutil.copy2(messages_path, checkpoints_dir / "messages.jsonl")

        console.print(f"[green]Checkpoint '{name}' created for session {full_id[:12]}.[/green]")
    finally:
        store.close()


def fork(
    session_id: str = typer.Argument(help="Session ID or prefix."),
    name: str = typer.Option(..., help="Name/title for the forked session."),
    from_checkpoint: str | None = typer.Option(
        None, "--from-checkpoint", help="Fork from a named checkpoint."
    ),
) -> None:
    """Fork a session into a new independent session."""
    store = open_store()
    try:
        full_id = resolve_session_id(store, session_id)
        session_dir = get_session_dir_or_exit(store, full_id)

        # Determine source directory
        if from_checkpoint:
            source_dir = session_dir / "checkpoints" / from_checkpoint
            if not source_dir.exists():
                err_console.print(
                    f"[red]Checkpoint '{from_checkpoint}' not found.[/red]"
                )
                raise SystemExit(1)
        else:
            source_dir = session_dir

        # Create new session
        from sessionfs.session_id import generate_session_id
        new_id = generate_session_id()
        new_dir = store.allocate_session_dir(new_id)

        # Copy messages
        src_messages = source_dir / "messages.jsonl"
        if src_messages.exists():
            shutil.copy2(src_messages, new_dir / "messages.jsonl")

        # Read and modify manifest
        src_manifest_path = source_dir / "manifest.json"
        if src_manifest_path.exists():
            manifest = json.loads(src_manifest_path.read_text())
        else:
            manifest = json.loads((session_dir / "manifest.json").read_text())

        manifest["session_id"] = new_id
        manifest["title"] = name
        manifest["parent_session_id"] = full_id
        if from_checkpoint:
            manifest["forked_from_checkpoint"] = from_checkpoint

        (new_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

        # Copy workspace and tools if they exist
        for fname in ("workspace.json", "tools.json"):
            src = session_dir / fname
            if src.exists():
                shutil.copy2(src, new_dir / fname)

        # Index the new session
        store.upsert_session_metadata(new_id, manifest, str(new_dir))

        console.print(f"[green]Forked session created: {new_id[:12]}[/green]")
        console.print(f"  Title: {name}")
        console.print(f"  Parent: {full_id[:12]}")
    finally:
        store.close()


_ALIAS_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{2,99}$")


def alias(
    session_id: str = typer.Argument(help="Session ID or prefix."),
    name: str = typer.Argument(None, help="Alias name (3-100 chars, alphanumeric/hyphens/underscores)."),
    clear: bool = typer.Option(False, "--clear", help="Remove the alias from this session."),
) -> None:
    """Set or clear a short alias for a session.

    Once set, use the alias anywhere you'd use a session ID:
      sfs show auth-debug
      sfs push auth-debug
      sfs resume auth-debug
    """
    store = open_store()
    try:
        full_id = resolve_session_id(store, session_id)
        session_dir = get_session_dir_or_exit(store, full_id)

        manifest_path = session_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())

        if clear:
            if "alias" in manifest:
                del manifest["alias"]
                manifest_path.write_text(json.dumps(manifest, indent=2))
                console.print(f"[green]Alias cleared for session {full_id[:12]}.[/green]")
            else:
                console.print("[dim]No alias set for this session.[/dim]")
            return

        if name is None:
            # Show current alias
            current = manifest.get("alias")
            if current:
                console.print(f"[bold]Alias:[/bold] {current}")
            else:
                console.print("[dim]No alias set. Usage: sfs alias <session_id> <name>[/dim]")
            return

        # Validate alias format
        if not _ALIAS_RE.match(name):
            err_console.print(
                "[red]Invalid alias. Must be 3-100 characters, "
                "alphanumeric/hyphens/underscores, starting with alphanumeric.[/red]"
            )
            raise SystemExit(1)

        # Check uniqueness across local sessions
        sessions = store.list_sessions()
        for s in sessions:
            if s["session_id"] == full_id:
                continue
            other_dir = store.get_session_dir(s["session_id"])
            if other_dir is None:
                continue
            other_manifest_path = other_dir / "manifest.json"
            if other_manifest_path.exists():
                try:
                    other = json.loads(other_manifest_path.read_text())
                    if other.get("alias") == name:
                        err_console.print(
                            f"[red]Alias '{name}' is already used by session "
                            f"{s['session_id'][:12]}.[/red]"
                        )
                        raise SystemExit(1)
                except (json.JSONDecodeError, OSError):
                    continue

        manifest["alias"] = name
        manifest_path.write_text(json.dumps(manifest, indent=2))
        console.print(f"[green]Alias '{name}' set for session {full_id[:12]}.[/green]")
        console.print(f"  You can now use: sfs show {name}, sfs push {name}, sfs resume {name}")
    finally:
        store.close()

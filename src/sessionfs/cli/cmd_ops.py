"""Session operation commands: sfs resume, checkpoint, fork."""

from __future__ import annotations

import json
import shutil
import uuid as uuid_mod
from pathlib import Path

import typer

from sessionfs.cli.common import (
    console,
    err_console,
    get_session_dir_or_exit,
    open_store,
    read_sfs_messages,
    resolve_session_id,
)
from sessionfs.cli.sfs_to_cc import reverse_convert_session


def resume(
    session_id: str = typer.Argument(help="Session ID or prefix."),
    project: str | None = typer.Option(None, help="Target project path (overrides workspace)."),
    tool: str = typer.Option(
        "claude-code", "--in",
        help="Target tool: claude-code, codex, gemini, cursor",
    ),
) -> None:
    """Resume a session in an AI tool."""
    store = open_store()
    try:
        full_id = resolve_session_id(store, session_id)
        session_dir = get_session_dir_or_exit(store, full_id)

        manifest = json.loads((session_dir / "manifest.json").read_text())

        # Determine project path
        target_path = project
        if not target_path:
            workspace_path = session_dir / "workspace.json"
            if workspace_path.exists():
                workspace = json.loads(workspace_path.read_text())
                target_path = workspace.get("root_path")

        if tool == "codex":
            _resume_in_codex(session_dir, manifest, target_path, full_id)
        elif tool == "gemini":
            _resume_in_gemini(session_dir, manifest, target_path, full_id)
        elif tool == "cursor":
            err_console.print(
                "[yellow]Cursor does not support session injection (capture-only).[/yellow]\n"
                "Resume in a CLI tool instead:\n"
                f"  sfs resume {session_id} --in claude-code\n"
                f"  sfs resume {session_id} --in codex\n"
                f"  sfs resume {session_id} --in gemini"
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
    console.print(f"Run [bold]codex --thread {result['codex_session_id'][:12]}[/bold] to continue.")


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

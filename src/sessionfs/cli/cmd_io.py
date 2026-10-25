"""Import/export commands: sfs import, sfs export."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

import typer

from sessionfs.cli.common import (
    console,
    err_console,
    get_session_dir_or_exit,
    open_store,
    read_sfs_messages,
    resolve_session_id,
)


def import_sessions(
    file: Optional[str] = typer.Argument(None, help="File to import (for file-based import)."),
    from_tool: Optional[str] = typer.Option(
        None, "--from", help="Import source: claude-code"
    ),
    format: Optional[str] = typer.Option(None, help="Input format (for file import)."),
) -> None:
    """Import sessions from external sources."""
    if from_tool == "claude-code":
        _import_from_claude_code()
    elif file:
        if format == "openai":
            _import_openai_file(Path(file))
        else:
            err_console.print(f"[red]Unknown format: {format}. Use --format openai.[/red]")
            raise SystemExit(1)
    else:
        err_console.print("[red]Specify --from claude-code or provide a file with --format.[/red]")
        raise SystemExit(1)


def _import_from_claude_code() -> None:
    """Bulk import all CC sessions."""
    from sessionfs.watchers.claude_code import discover_sessions, parse_session
    from sessionfs.spec.convert_cc import convert_session

    store = open_store()
    try:
        home = Path.home() / ".claude"
        sessions = discover_sessions(home)

        if not sessions:
            console.print("[dim]No Claude Code sessions found.[/dim]")
            return

        console.print(f"Found {len(sessions)} Claude Code session(s).")
        imported = 0

        from sessionfs.session_id import session_id_from_native

        for s in sessions:
            native_id = s["session_id"]
            sfs_id = session_id_from_native(native_id)

            # Skip if already captured
            existing = store.get_session_dir(sfs_id)
            if existing:
                continue

            try:
                cc_session = parse_session(Path(s["path"]))

                def _write(staging: Path):
                    convert_session(
                        cc_session, staging.parent,
                        session_id=sfs_id, session_dir=staging,
                    )
                    manifest_path = staging / "manifest.json"
                    if manifest_path.exists():
                        return json.loads(manifest_path.read_text())
                    return None

                outcome = store.commit_session(sfs_id, _write)
                if not outcome.skipped:
                    imported += 1
            except Exception as e:
                err_console.print(f"[red]Error importing {sfs_id}: {e}[/red]")

        console.print(f"[green]Imported {imported} new session(s).[/green]")
    finally:
        store.close()


def _import_openai_file(file_path: Path) -> None:
    """Import from an OpenAI-format JSON file (stub)."""
    err_console.print("[yellow]OpenAI format import is not yet implemented.[/yellow]")
    raise SystemExit(1)


def export_session(
    session_id: str = typer.Argument(help="Session ID or prefix."),
    output: str = typer.Option(".", "--output", "-o", help="Output directory."),
    format: str = typer.Option("sfs", help="Export format: sfs, markdown, claude-code."),
) -> None:
    """Export a session."""
    store = open_store()
    try:
        full_id = resolve_session_id(store, session_id)
        session_dir = get_session_dir_or_exit(store, full_id)
        output_dir = Path(output)
        output_dir.mkdir(parents=True, exist_ok=True)

        if format == "sfs":
            dest = output_dir / f"{full_id}.sfs"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(session_dir, dest)
            console.print(f"[green]Exported to {dest}[/green]")

        elif format == "markdown":
            from sessionfs.cli.sfs_to_md import render_session_markdown

            manifest = json.loads((session_dir / "manifest.json").read_text())
            messages = read_sfs_messages(session_dir)
            md = render_session_markdown(manifest, messages)

            md_path = output_dir / f"{full_id}.md"
            md_path.write_text(md)
            console.print(f"[green]Exported to {md_path}[/green]")

        elif format == "claude-code":
            from sessionfs.cli.sfs_to_cc import reverse_convert_session

            result = reverse_convert_session(
                session_dir,
                output_path=output_dir / f"{full_id}.jsonl",
            )
            console.print(f"[green]Exported to {result['jsonl_path']}[/green]")

        else:
            err_console.print(f"[red]Unknown format: {format}[/red]")
            raise SystemExit(1)
    finally:
        store.close()

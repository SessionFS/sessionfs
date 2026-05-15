"""Recapture command: sfs recapture <session_id>.

Force re-capture of a session from its native source, ignoring
the compression guard. Use case: the user intentionally cleaned up
a session and wants SessionFS to reflect the current state.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import typer

from sessionfs.cli.common import console, err_console, open_store, resolve_session_id
from sessionfs.watchers.base import NativeSessionRef

logger = logging.getLogger("sfs.recapture")

# Map tool names to their parser/converter pairs
_TOOL_HANDLERS = {
    "claude-code": "_recapture_claude_code",
    "codex": "_recapture_codex",
    "gemini-cli": "_recapture_gemini",
    "cursor": "_recapture_cursor",
    "copilot-cli": "_recapture_copilot",
    "amp": "_recapture_amp",
    "cline": "_recapture_cline",
    "roo-code": "_recapture_cline",
    "kilo-code": "_recapture_cline",
}


def recapture(
    session_id: str = typer.Argument(help="Session ID or prefix to re-capture"),
) -> None:
    """Force re-capture a session from its native source, ignoring the compression guard."""
    store = open_store()
    sfs_id = resolve_session_id(store, session_id)

    ref = store.get_tracked_session_by_sfs_id(sfs_id)
    if not ref:
        err_console.print(
            f"[red]No tracked native session found for {sfs_id}.[/red]\n"
            "This session may have been imported, not captured from a native tool."
        )
        raise SystemExit(1)

    native_path = Path(ref.native_path)
    if not native_path.exists():
        err_console.print(
            f"[red]Native source no longer exists: {native_path}[/red]"
        )
        raise SystemExit(1)

    handler_name = _TOOL_HANDLERS.get(ref.tool)
    if not handler_name:
        err_console.print(f"[red]Unsupported tool for recapture: {ref.tool}[/red]")
        raise SystemExit(1)

    handler = globals()[handler_name]
    try:
        handler(store, sfs_id, ref, native_path)
    except CursorComposerPurgedError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise SystemExit(1)

    console.print(f"[green]Re-captured {sfs_id} from {ref.tool} source.[/green]")


def _recapture_claude_code(
    store: object, sfs_id: str, ref: NativeSessionRef, native_path: Path,
) -> None:
    from sessionfs.watchers.claude_code import parse_session
    from sessionfs.spec.convert_cc import convert_session

    cc_session = parse_session(native_path, copy_on_read=True)
    session_dir = store.allocate_session_dir(sfs_id)
    convert_session(cc_session, session_dir.parent, session_id=sfs_id, session_dir=session_dir)

    _update_index_and_ref(store, sfs_id, session_dir, ref, native_path)


def _recapture_codex(
    store: object, sfs_id: str, ref: NativeSessionRef, native_path: Path,
) -> None:
    from sessionfs.watchers.codex import parse_codex_session, convert_codex_to_sfs

    codex_session = parse_codex_session(native_path)
    session_dir = store.allocate_session_dir(sfs_id)
    convert_codex_to_sfs(codex_session, session_dir, session_id=sfs_id)

    _update_index_and_ref(store, sfs_id, session_dir, ref, native_path)


def _recapture_gemini(
    store: object, sfs_id: str, ref: NativeSessionRef, native_path: Path,
) -> None:
    from sessionfs.converters.gemini_to_sfs import parse_gemini_session, convert_gemini_to_sfs

    gemini_session = parse_gemini_session(native_path)
    session_dir = store.allocate_session_dir(sfs_id)
    convert_gemini_to_sfs(gemini_session, session_dir, session_id=sfs_id)

    _update_index_and_ref(store, sfs_id, session_dir, ref, native_path)


def _recapture_cursor(
    store: object, sfs_id: str, ref: NativeSessionRef, native_path: Path,
) -> None:
    from sessionfs.converters.cursor_to_sfs import parse_cursor_composer, convert_cursor_to_sfs

    session = parse_cursor_composer(ref.native_session_id, global_db=native_path)
    # Cursor's native_path is the global DB itself, not a per-session file.
    # If the user purged the composer rows, the DB still exists but the
    # parse returns 0 messages. Refuse to overwrite a good capture with an
    # empty one — surface an error so the user knows the source is gone.
    if session.message_count == 0:
        raise CursorComposerPurgedError(
            f"Cursor composer {ref.native_session_id[:12]} has 0 messages "
            f"in the global DB — the composer rows appear to have been purged. "
            f"Refusing to overwrite the existing .sfs capture with empty content."
        )
    session_dir = store.allocate_session_dir(sfs_id)
    convert_cursor_to_sfs(session, session_dir, session_id=sfs_id)

    _update_index_and_ref(store, sfs_id, session_dir, ref, native_path)


class CursorComposerPurgedError(Exception):
    """Raised when a Cursor composer's bubble rows are gone from the global DB."""


def _recapture_copilot(
    store: object, sfs_id: str, ref: NativeSessionRef, native_path: Path,
) -> None:
    from sessionfs.converters.copilot_to_sfs import convert_copilot_to_sfs

    session_dir = store.allocate_session_dir(sfs_id)
    convert_copilot_to_sfs(native_path, session_dir, session_id=sfs_id)

    _update_index_and_ref(store, sfs_id, session_dir, ref, native_path)


def _recapture_amp(
    store: object, sfs_id: str, ref: NativeSessionRef, native_path: Path,
) -> None:
    from sessionfs.converters.amp_to_sfs import convert_amp_to_sfs

    session_dir = store.allocate_session_dir(sfs_id)
    convert_amp_to_sfs(native_path, session_dir, session_id=sfs_id)

    _update_index_and_ref(store, sfs_id, session_dir, ref, native_path)


def _recapture_cline(
    store: object, sfs_id: str, ref: NativeSessionRef, native_path: Path,
) -> None:
    from sessionfs.converters.cline_to_sfs import parse_cline_session, convert_cline_to_sfs

    cline_session = parse_cline_session(native_path, tool=ref.tool)
    session_dir = store.allocate_session_dir(sfs_id)
    convert_cline_to_sfs(cline_session, session_dir, session_id=sfs_id)

    _update_index_and_ref(store, sfs_id, session_dir, ref, native_path)


def _update_index_and_ref(
    store: object, sfs_id: str, session_dir: Path, ref: NativeSessionRef, native_path: Path,
) -> None:
    """Update session metadata index and tracking ref after re-capture."""
    manifest_path = session_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        store.upsert_session_metadata(sfs_id, manifest, str(session_dir))

    stat = native_path.stat()
    new_ref = NativeSessionRef(
        tool=ref.tool,
        native_session_id=ref.native_session_id,
        native_path=str(native_path),
        sfs_session_id=sfs_id,
        last_mtime=stat.st_mtime,
        last_size=stat.st_size,
        last_captured_at=datetime.now(timezone.utc).isoformat(),
        project_path=ref.project_path,
    )
    store.upsert_tracked_session(new_ref)

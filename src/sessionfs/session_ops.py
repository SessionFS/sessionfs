"""Pure helpers for checkpoint + fork over the local session store.

Both the `sfs` CLI (`cmd_ops.checkpoint` / `cmd_ops.fork`) and the MCP
server tools (`checkpoint_session` / `fork_session` / `list_checkpoints`)
call these helpers so behaviour stays consistent across surfaces.

The functions raise `SessionOpError` for known user errors (caller
renders the message); the CLI catches them and prints, the MCP wrapper
catches them and returns `{"error": ...}`.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sessionfs.session_id import generate_session_id
from sessionfs.store.local import LocalStore

_CHECKPOINT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$")


class SessionOpError(ValueError):
    """User-facing error from a session operation."""


def _checkpoints_root(session_dir: Path) -> Path:
    return session_dir / "checkpoints"


def _validate_checkpoint_name(name: str) -> None:
    if not _CHECKPOINT_NAME_RE.match(name):
        raise SessionOpError(
            "Checkpoint name must be 1-100 chars, start with alphanumeric, "
            "and use only letters, digits, '.', '_', '-'."
        )


def create_checkpoint(
    store: LocalStore, session_id: str, name: str
) -> dict[str, Any]:
    """Create a named checkpoint of the session's current state.

    Copies `manifest.json` and (if present) `messages.jsonl` into
    `<session_dir>/checkpoints/<name>/`. Returns metadata describing
    the new checkpoint.

    Raises `SessionOpError` if the session is unknown or the name is
    invalid / already taken.
    """
    _validate_checkpoint_name(name)

    session_dir = store.get_session_dir(session_id)
    if not session_dir:
        raise SessionOpError(f"Session '{session_id}' not found")

    checkpoint_dir = _checkpoints_root(session_dir) / name
    if checkpoint_dir.exists():
        raise SessionOpError(f"Checkpoint '{name}' already exists")

    checkpoint_dir.mkdir(parents=True)
    manifest_src = session_dir / "manifest.json"
    if manifest_src.exists():
        shutil.copy2(manifest_src, checkpoint_dir / "manifest.json")
    messages_src = session_dir / "messages.jsonl"
    if messages_src.exists():
        shutil.copy2(messages_src, checkpoint_dir / "messages.jsonl")

    created_at = datetime.now(timezone.utc).isoformat()
    return {
        "session_id": session_id,
        "name": name,
        "path": str(checkpoint_dir),
        "created_at": created_at,
        "has_messages": messages_src.exists(),
    }


def list_checkpoints(
    store: LocalStore, session_id: str
) -> list[dict[str, Any]]:
    """List checkpoints stored for a session, sorted by mtime ascending.

    Raises `SessionOpError` if the session is unknown. Returns an empty
    list if the session has no checkpoints.
    """
    session_dir = store.get_session_dir(session_id)
    if not session_dir:
        raise SessionOpError(f"Session '{session_id}' not found")

    root = _checkpoints_root(session_dir)
    if not root.is_dir():
        return []

    results: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime):
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.json"
        message_count: int | None = None
        messages_path = entry / "messages.jsonl"
        if messages_path.exists():
            with messages_path.open() as fh:
                message_count = sum(1 for _ in fh)
        created_at = datetime.fromtimestamp(
            entry.stat().st_mtime, tz=timezone.utc
        ).isoformat()
        results.append(
            {
                "name": entry.name,
                "path": str(entry),
                "created_at": created_at,
                "has_manifest": manifest_path.exists(),
                "message_count": message_count,
            }
        )
    return results


def fork_session(
    store: LocalStore,
    session_id: str,
    name: str,
    from_checkpoint: str | None = None,
) -> dict[str, Any]:
    """Fork a session (or a named checkpoint of it) into a new session.

    The new session inherits the source's messages + workspace + tools,
    but gets a fresh `session_id` and the given `name` as its title.
    The manifest records `parent_session_id` (and
    `forked_from_checkpoint` when applicable) so the lineage is
    introspectable.

    Raises `SessionOpError` if the source session or named checkpoint
    is missing.
    """
    if not name or not name.strip():
        raise SessionOpError("name must be a non-empty string")
    name = name.strip()

    session_dir = store.get_session_dir(session_id)
    if not session_dir:
        raise SessionOpError(f"Session '{session_id}' not found")

    if from_checkpoint is not None:
        _validate_checkpoint_name(from_checkpoint)
        source_dir = _checkpoints_root(session_dir) / from_checkpoint
        if not source_dir.is_dir():
            raise SessionOpError(
                f"Checkpoint '{from_checkpoint}' not found"
            )
    else:
        source_dir = session_dir

    new_id = generate_session_id()
    new_dir = store.allocate_session_dir(new_id)

    src_messages = source_dir / "messages.jsonl"
    if src_messages.exists():
        shutil.copy2(src_messages, new_dir / "messages.jsonl")

    src_manifest_path = source_dir / "manifest.json"
    if src_manifest_path.exists():
        manifest = json.loads(src_manifest_path.read_text())
    else:
        manifest = json.loads((session_dir / "manifest.json").read_text())

    manifest["session_id"] = new_id
    manifest["title"] = name
    manifest["parent_session_id"] = session_id
    if from_checkpoint is not None:
        manifest["forked_from_checkpoint"] = from_checkpoint

    (new_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    for fname in ("workspace.json", "tools.json"):
        src = session_dir / fname
        if src.exists():
            shutil.copy2(src, new_dir / fname)

    store.upsert_session_metadata(new_id, manifest, str(new_dir))

    return {
        "session_id": new_id,
        "title": name,
        "parent_session_id": session_id,
        "forked_from_checkpoint": from_checkpoint,
        "path": str(new_dir),
    }

"""Inject sessions into Codex CLI's native storage.

Places a converted JSONL rollout file into ~/.codex/sessions/YYYY/MM/DD/
with the correct naming convention, and updates the SQLite metadata index
if it exists.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sessionfs.converters.codex_injector")

# Codex state DB filename (versioned)
_STATE_DB = "state_5.sqlite"


def inject_session(
    codex_jsonl: Path,
    codex_session_id: str,
    cwd: str,
    title: str = "",
    model: str = "gpt-4.1",
    codex_home: Path | None = None,
) -> dict[str, Any]:
    """Inject a Codex JSONL session into Codex CLI's storage.

    Args:
        codex_jsonl: Path to the converted Codex JSONL file.
        codex_session_id: The Codex session UUID.
        cwd: Working directory for the session.
        title: Session title (first user message).
        model: Model name.
        codex_home: Override for ~/.codex.

    Returns:
        Dict with keys: rollout_path, index_updated
    """
    home = codex_home or Path.home() / ".codex"

    # Determine date-based directory
    now = datetime.now(timezone.utc)
    date_dir = home / "sessions" / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    date_dir.mkdir(parents=True, exist_ok=True)

    # Build filename: rollout-YYYY-MM-DDThh-mm-ss-{uuid}.jsonl
    ts_str = now.strftime("%Y-%m-%dT%H-%M-%S")
    filename = f"rollout-{ts_str}-{codex_session_id}.jsonl"
    rollout_path = date_dir / filename

    # Copy the JSONL file
    import shutil
    shutil.copy2(str(codex_jsonl), str(rollout_path))

    # Update SQLite index if it exists
    index_updated = _update_sqlite_index(
        home, codex_session_id, str(rollout_path), cwd, title, model, now,
    )

    # Append to session_index.jsonl (name-based lookup)
    _update_name_index(home, codex_session_id, title)

    logger.info(
        "Injected session %s into %s (index_updated=%s)",
        codex_session_id[:12], rollout_path, index_updated,
    )

    return {
        "rollout_path": str(rollout_path),
        "index_updated": index_updated,
    }


def discover_codex_home() -> Path | None:
    """Find Codex CLI installation directory."""
    # Check CODEX_HOME env var first
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        p = Path(env_home)
        if p.is_dir():
            return p

    default = Path.home() / ".codex"
    if default.is_dir():
        return default

    return None


def _update_sqlite_index(
    home: Path,
    session_id: str,
    rollout_path: str,
    cwd: str,
    title: str,
    model: str,
    now: datetime,
) -> bool:
    """Update the Codex threads table in SQLite. Returns True if updated."""
    db_path = home / _STATE_DB
    if not db_path.exists():
        return False

    try:
        conn = sqlite3.connect(str(db_path))
        epoch = int(now.timestamp())
        conn.execute(
            """
            INSERT OR REPLACE INTO threads (
                id, rollout_path, created_at, updated_at, source,
                model_provider, cwd, title, sandbox_policy, approval_mode,
                tokens_used, has_user_event, archived, cli_version,
                first_user_message, model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                rollout_path,
                epoch,
                epoch,
                "custom",
                "openai",
                cwd,
                title or "Imported from SessionFS",
                '{"type":"read-only"}',
                "never",
                0,
                1,
                0,
                "sessionfs",
                title or "",
                model,
            ),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as exc:
        logger.warning("Failed to update Codex SQLite index: %s", exc)
        return False


def _update_name_index(home: Path, session_id: str, title: str) -> None:
    """Append to session_index.jsonl for name-based lookups."""
    index_path = home / "session_index.jsonl"
    try:
        entry = {
            "id": session_id,
            "thread_name": title or f"sessionfs-{session_id[:8]}",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(index_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        logger.warning("Failed to update session_index.jsonl: %s", exc)

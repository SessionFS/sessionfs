"""Inject sessions into Copilot CLI's native storage.

Places a converted events.jsonl and workspace.yaml into
~/.copilot/session-state/{session-id}/ with the correct structure.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sessionfs.converters.copilot_injector")


def inject_session(
    events_jsonl: Path,
    copilot_session_id: str,
    cwd: str,
    title: str | None = None,
    copilot_home: Path | None = None,
) -> dict[str, Any]:
    """Inject a Copilot session into Copilot CLI's storage.

    Args:
        events_jsonl: Path to the converted events.jsonl file.
        copilot_session_id: The Copilot session UUID.
        cwd: Working directory for the session.
        title: Optional session title.
        copilot_home: Override for ~/.copilot.

    Returns:
        Dict with keys: session_path, events_path
    """
    home = copilot_home or Path.home() / ".copilot"

    # Create session directory
    session_dir = home / "session-state" / copilot_session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    # Copy events.jsonl
    target_events = session_dir / "events.jsonl"
    shutil.copy2(str(events_jsonl), str(target_events))

    # Write workspace.yaml
    workspace_yaml = session_dir / "workspace.yaml"
    _write_workspace_yaml(workspace_yaml, cwd, title)

    logger.info(
        "Injected Copilot session %s into %s",
        copilot_session_id[:12], session_dir,
    )

    return {
        "session_path": str(session_dir),
        "events_path": str(target_events),
    }


def discover_copilot_home() -> Path | None:
    """Find Copilot CLI installation directory."""
    import os

    # Check COPILOT_HOME env var first
    env_home = os.environ.get("COPILOT_HOME")
    if env_home:
        p = Path(env_home)
        if p.is_dir():
            return p

    default = Path.home() / ".copilot"
    if default.is_dir():
        return default

    return None


def _write_workspace_yaml(path: Path, cwd: str, title: str | None = None) -> None:
    """Write a workspace.yaml file for the Copilot session."""
    lines = [
        f"cwd: {cwd}",
    ]
    if title:
        lines.append(f"title: {title}")

    path.write_text("\n".join(lines) + "\n")

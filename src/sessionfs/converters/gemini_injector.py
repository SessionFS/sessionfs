"""Inject sessions into Gemini CLI's native storage.

Places a converted JSON session file into ~/.gemini/tmp/{hash}/chats/
with the correct naming convention.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sessionfs.converters.gemini_injector")


def inject_session(
    gemini_json: Path,
    gemini_session_id: str,
    project_path: str,
    gemini_home: Path | None = None,
) -> dict[str, Any]:
    """Inject a Gemini session JSON into Gemini CLI's storage.

    Args:
        gemini_json: Path to the converted Gemini JSON file.
        gemini_session_id: The session UUID.
        project_path: Project directory path.
        gemini_home: Override for ~/.gemini.

    Returns:
        Dict with: session_path, project_registered
    """
    home = gemini_home or Path.home() / ".gemini"

    # Compute project hash
    project_hash = hashlib.sha256(project_path.encode()).hexdigest()
    chats_dir = home / "tmp" / project_hash / "chats"
    chats_dir.mkdir(parents=True, exist_ok=True)

    # Build filename
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H-%M")
    filename = f"session-{ts}-{gemini_session_id[:8]}.json"
    target = chats_dir / filename

    # Copy file
    import shutil
    shutil.copy2(str(gemini_json), str(target))

    # Register project if not already known
    registered = _register_project(home, project_path)

    logger.info("Injected Gemini session %s into %s", gemini_session_id[:8], target)

    return {
        "session_path": str(target),
        "project_registered": registered,
    }


def _register_project(home: Path, project_path: str) -> bool:
    """Add project to projects.json if not already there."""
    projects_file = home / "projects.json"

    projects: dict[str, dict[str, str]] = {"projects": {}}
    if projects_file.exists():
        try:
            projects = json.loads(projects_file.read_text())
        except (json.JSONDecodeError, KeyError):
            pass

    existing = projects.get("projects", {})
    if project_path in existing:
        return False

    # Derive a name from the path
    name = Path(project_path).name
    existing[project_path] = name
    projects["projects"] = existing
    projects_file.write_text(json.dumps(projects, indent=2))

    # Also write the history link
    history_dir = home / "history" / name
    history_dir.mkdir(parents=True, exist_ok=True)
    root_file = history_dir / ".project_root"
    if not root_file.exists():
        root_file.write_text(project_path)

    return True

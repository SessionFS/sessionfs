"""Shared helper: annotate a captured .sfs session directory with instruction
provenance.

Called from each tool-specific watcher after `convert_session` writes the
.sfs directory. Non-fatal — any exception is swallowed and logged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("sfsd.provenance")


def annotate_manifest_with_provenance(
    session_dir: Path,
    tool: str,
    project_path: str | None,
) -> None:
    """Attempt to discover instruction artifacts for `project_path` and write
    the provenance object into the session's manifest.json.

    If project_path is missing or discovery fails we do not touch the manifest.
    """
    try:
        if not project_path:
            return
        project_root = Path(project_path)
        if not project_root.is_dir():
            return

        from sessionfs.spec.instruction_provenance import discover

        provenance = discover(project_root, tool=tool)

        # Only write when we actually found something worth persisting;
        # an all-none payload is still useful because it records the attempt.
        manifest_path = session_dir / "manifest.json"
        if not manifest_path.exists():
            return
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(manifest, dict):
            return
        manifest["instruction_provenance"] = provenance.as_dict()
        manifest_path.write_text(json.dumps(manifest, indent=2))
    except Exception as exc:  # pragma: no cover — best-effort
        # Warning (not debug) so breakage is visible in normal operation.
        # Provenance failure must never halt session capture, but silent
        # failure would let real regressions ship unnoticed.
        logger.warning(
            "Provenance annotation failed for %s (tool=%s): %s",
            session_dir, tool, exc,
        )

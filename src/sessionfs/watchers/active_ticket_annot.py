"""v0.10.1 Phase 6 — annotate a captured .sfs session manifest with
the active ticket + persona from ``~/.sessionfs/active_ticket.json``.

Mirrors the shape of ``watchers/provenance.py``. Best-effort: failures
log a warning and leave the manifest untouched.

The manifest gains two top-level fields when a bundle exists:
- ``persona_name`` — the persona the user is working under
- ``ticket_id`` — the ticket the user has started

These match the column names already in the ``sessions`` table
(migration 037), so the cloud sync path will populate the SQL columns
directly without further mapping.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("sfsd.active_ticket")


def annotate_manifest_with_active_ticket(session_dir: Path) -> None:
    """Read ~/.sessionfs/active_ticket.json and tag the session manifest.

    No-op when the bundle is missing or malformed — the user simply
    isn't working under a ticket.
    """
    try:
        from sessionfs.active_ticket import read_bundle
        bundle = read_bundle()
        if not isinstance(bundle, dict):
            return
        ticket_id = bundle.get("ticket_id")
        persona_name = bundle.get("persona_name")
        # v0.10.1 Phase 8 — persona-only bundles (assume_persona) carry
        # ticket_id=None. Annotate when EITHER field is present so ad-hoc
        # agent work still tags captured sessions with the persona.
        if not ticket_id and not persona_name:
            return

        manifest_path = session_dir / "manifest.json"
        if not manifest_path.exists():
            return
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(manifest, dict):
            return

        if ticket_id:
            manifest["ticket_id"] = ticket_id
        if persona_name:
            manifest["persona_name"] = persona_name

        manifest_path.write_text(json.dumps(manifest, indent=2))
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning(
            "Active-ticket annotation failed for %s: %s", session_dir, exc
        )

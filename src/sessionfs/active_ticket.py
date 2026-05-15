"""Active-ticket provenance bundle — shared by MCP + CLI.

The bundle lives at ``~/.sessionfs/active_ticket.json`` and records which
ticket the user is currently working on. ``start_ticket`` (MCP or CLI)
writes it; ``complete_ticket`` removes it, but only when both
``ticket_id`` and ``project_id`` match (KB 332 LOW fix — never delete
another tool's bundle).

Phase 6 (daemon) will read this bundle when a session is captured and
tag the manifest with ``persona_name`` + ``ticket_id``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sessionfs.active_ticket")


def bundle_path() -> Path:
    """Return the on-disk location of the active-ticket bundle."""
    return Path.home() / ".sessionfs" / "active_ticket.json"


def read_bundle() -> dict[str, Any] | None:
    """Return the parsed bundle or None if missing/unreadable."""
    path = bundle_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_bundle(
    *,
    ticket_id: str | None,
    persona_name: str | None,
    project_id: str,
    lease_epoch: int | None = None,
    retrieval_audit_id: str | None = None,
) -> bool:
    """Write the active-ticket bundle. Returns True on success, False on
    OSError so callers can surface a "provenance NOT recorded" warning
    (KB 339 LOW). Failures are still logged.

    Either `ticket_id` or `persona_name` must be set (the bundle is
    meaningless if both are None). Persona-only entries (ticket_id=None)
    are written by `assume_persona` for ad-hoc agent work that isn't
    tied to a specific ticket — the daemon tags captured sessions with
    just the persona in that case.
    """
    if not ticket_id and not persona_name:
        raise ValueError("write_bundle requires ticket_id or persona_name")
    path = bundle_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "ticket_id": ticket_id,
            "persona_name": persona_name,
            "project_id": project_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        if lease_epoch is not None:
            payload["lease_epoch"] = lease_epoch
        if retrieval_audit_id:
            payload["retrieval_audit_id"] = retrieval_audit_id
        path.write_text(json.dumps(payload))
    except OSError as exc:
        logger.warning("Failed to write active_ticket.json: %s", exc)
        return False
    return True


def clear_bundle() -> bool:
    """Unconditionally remove the bundle. Returns True if removed,
    False if it was already absent or removal failed.

    Used by `forget_persona` / `sfs persona forget` to retire an
    `assume_persona` bundle. Ticket bundles should use
    `clear_bundle_if_owned()` instead so concurrent ticket work isn't
    disturbed (KB 332 LOW).
    """
    path = bundle_path()
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError as exc:
        logger.warning("Failed to remove active_ticket.json: %s", exc)
        return False


def clear_bundle_if_owned(*, ticket_id: str, project_id: str) -> bool:
    """Remove the bundle only when it points at this exact ticket.

    Returns True if the bundle was removed, False if it was preserved
    (either missing, unreadable, or owned by a different ticket).
    """
    path = bundle_path()
    if not path.exists():
        return False
    bundle = read_bundle()
    if (
        isinstance(bundle, dict)
        and bundle.get("ticket_id") == ticket_id
        and bundle.get("project_id") == project_id
    ):
        try:
            path.unlink()
            return True
        except OSError as exc:
            logger.warning("Failed to remove active_ticket.json: %s", exc)
    return False

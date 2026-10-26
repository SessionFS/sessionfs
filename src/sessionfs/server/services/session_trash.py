"""Soft-delete a session into Trash.

Kept free of any route/request dependency so the session helper's
``trash_sessions`` confirm path and ``DELETE /api/v1/sessions/{id}`` run the
exact same mutation instead of duplicating the trash fields.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sessionfs.server.db.models import Session

# Trash is restorable until the purge deadline. Keep in sync with the
# retention sweep that hard-deletes rows past ``purge_after``.
TRASH_RETENTION_DAYS = 30


def soft_delete_session(
    session: Session, *, deleted_by: str, scope: str = "cloud"
) -> None:
    """Mark ``session`` deleted (Trash) with a 30-day purge deadline.

    Mutates the ORM object in place; the caller owns the commit. ``scope``
    is the resolved delete scope: ``cloud`` (server-side only) or ``both``
    (also instruct the client to remove its local copy).
    """
    now = datetime.now(timezone.utc)
    session.is_deleted = True
    session.deleted_at = now
    session.deleted_by = deleted_by
    session.delete_scope = scope
    session.purge_after = now + timedelta(days=TRASH_RETENTION_DAYS)

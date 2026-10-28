"""Append-only ticket event log (GitHub #113 ticket timeline).

``record_ticket_event`` is the single writer: it builds a ``TicketEvent``
row in the caller's transaction (it never commits) so the event lands or
rolls back atomically with the write it describes. Comments are never
events; description/acceptance-criteria bodies are never persisted here —
the encoding below stores a ``{"changed": true, "length": N}`` stub for
free-text fields and caps any stray long string, so the timeline can never
leak a comment or description body.

The ``actor_*`` columns are plain provenance strings (no FKs): a deleted
user still shows up as "a former user". ``actor_type`` uses the timeline
vocabulary (``user`` | ``service`` | ``system`` | ``jira``), not the
AuthContext value (``service_key``), so callers map service keys to
``"service"`` themselves.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Ticket, TicketEvent

# Fields whose free-text/list bodies must never appear in the timeline.
_LENGTH_STUB_FIELDS = {"description", "acceptance_criteria"}
# List-valued fields whose values are truncated to a count + first-N items.
_LIST_CAP_FIELDS = {"context_refs", "file_refs", "related_sessions", "depends_on"}
_LIST_CAP = 20
# Any scalar string longer than this is a free-text body, not a value.
_MAX_INLINE = 200


def encode_event_value(field: str | None, value: object) -> str | None:
    """Encode a before/after value as a JSON string, guarding free text.

    Returns ``None`` for a missing value. Free-text fields (description,
    acceptance criteria) and any over-long string become a length stub so a
    comment or description body can never land in ``ticket_events``.
    List-valued reference fields are capped to a ``count`` + first-``_LIST_CAP``
    items so an unbounded dependency/ref list can never bloat the ledger.
    """
    if value is None:
        return None
    if field in _LENGTH_STUB_FIELDS:
        length = len(value) if isinstance(value, (str, list, dict, tuple)) else 0
        return json.dumps({"changed": True, "length": length})
    if field in _LIST_CAP_FIELDS and isinstance(value, (list, tuple)):
        items = list(value)
        return json.dumps(
            {"count": len(items), "items": items[:_LIST_CAP]}, default=str
        )
    if isinstance(value, datetime):
        return json.dumps(value.isoformat())
    if isinstance(value, date):
        return json.dumps(value.isoformat())
    if isinstance(value, str):
        if len(value) > _MAX_INLINE:
            return json.dumps({"changed": True, "length": len(value)})
        return json.dumps(value)
    return json.dumps(value, default=str)


async def record_ticket_event(
    db: AsyncSession,
    ticket: Ticket,
    action: str,
    *,
    field: str | None = None,
    before: object | None = None,
    after: object | None = None,
    actor_type: str = "user",
    actor_user_id: str | None = None,
    actor_persona: str | None = None,
    actor_label: str | None = None,
) -> TicketEvent:
    """Write one ``ticket_events`` row in the current transaction.

    ``ticket`` must be a mapped ``Ticket`` (its ``project_id`` is
    denormalised onto the event). ``before`` / ``after`` are any
    JSON-encodable values; free text is stubbed, never stored. The row is
    ``db.add``-ed and returned, never committed — callers own the commit so
    the event shares the write's atomicity.
    """
    event = TicketEvent(
        id=f"evt_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket.id,
        project_id=ticket.project_id,
        action=action,
        field=field,
        before_value=encode_event_value(field, before),
        after_value=encode_event_value(field, after),
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        actor_persona=actor_persona,
        actor_label=actor_label,
        created_at=datetime.now(timezone.utc),
    )
    db.add(event)
    return event


def decode_event_value(raw: str | None) -> Any:
    """Parse a stored before/after JSON string back into a Python value."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None

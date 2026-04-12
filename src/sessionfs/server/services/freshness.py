"""Freshness computation for knowledge entries.

Each entry_type has a decay window. Age is measured from last_relevant_at
(if set) or created_at. Entries with superseded_by set are always 'superseded'.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from sessionfs.server.db.models import KnowledgeEntry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("sessionfs.freshness")

# Decay windows in days per entry_type
DECAY_WINDOWS: dict[str, int] = {
    "bug": 30,
    "dependency": 60,
    "pattern": 90,
    "discovery": 90,
    "convention": 180,
    "decision": 365,
}

DEFAULT_DECAY_WINDOW = 90


def compute_freshness(entry: KnowledgeEntry) -> str:
    """Compute the freshness class for a single entry.

    Returns one of: 'current', 'aging', 'stale', 'superseded'.

    Rules:
    - If superseded_by is set, always 'superseded'.
    - Age measured from last_relevant_at if set, else created_at.
    - current if age < 50% of window.
    - aging if age < window.
    - stale otherwise.
    """
    if entry.superseded_by is not None:
        return "superseded"

    window_days = DECAY_WINDOWS.get(entry.entry_type, DEFAULT_DECAY_WINDOW)
    now = datetime.now(timezone.utc)

    reference_time = entry.last_relevant_at or entry.created_at
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)

    age = now - reference_time
    half_window = timedelta(days=window_days / 2)
    full_window = timedelta(days=window_days)

    if age < half_window:
        return "current"
    elif age < full_window:
        return "aging"
    else:
        return "stale"


async def refresh_freshness_classes(project_id: str, db: AsyncSession) -> int:
    """Bulk-update freshness_class for all entries in a project.

    Returns the number of entries updated. Call during compile and rebuild.
    """
    result = await db.execute(
        select(KnowledgeEntry).where(
            KnowledgeEntry.project_id == project_id,
        )
    )
    entries = list(result.scalars().all())

    updated = 0
    for entry in entries:
        new_class = compute_freshness(entry)
        if entry.freshness_class != new_class:
            entry.freshness_class = new_class
            updated += 1

    if updated:
        await db.flush()
        logger.info(
            "Refreshed freshness for %d/%d entries in project %s",
            updated, len(entries), project_id,
        )

    return updated

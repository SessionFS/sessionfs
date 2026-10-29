"""Session-helper token usage, read from the append-only ``helper_usage`` ledger.

Every turn that reported tokens writes one ledger row (see the shielded
persistence block in ``routes/helper.py``). The org monthly budget and every
usage summary read ONLY the ledger, so deleting a conversation or the
helper-retention sweep purging messages never changes a budget. Ledger rows
are kept 13 months (the retention sweep deletes rows older than
``USAGE_RETENTION_DAYS``).

Org-key turns carry the key org in ``org_id`` with ``key_source == "org"``;
user-key turns have ``org_id`` NULL and never charge an org's budget.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import HelperUsage

# 13 months, with margin: rows older than this are deleted by the sweep.
USAGE_RETENTION_DAYS = 400


def month_start_utc(now: datetime | None = None) -> datetime:
    """Start of the current UTC calendar month."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )


def ledger_row(
    *,
    user_id: str,
    key_source: str,
    org_id: str | None,
    conversation_id: str | None,
    input_tokens: int,
    output_tokens: int,
    stop_reason: str | None,
    source: str = "helper",
) -> HelperUsage | None:
    """A ledger row for one turn, or ``None`` when the turn reported no tokens."""
    if input_tokens <= 0 and output_tokens <= 0:
        return None
    return HelperUsage(
        id=f"hu_{uuid.uuid4().hex[:24]}",
        user_id=user_id,
        key_source=key_source,
        source=source[:32] if source else "helper",
        org_id=org_id if key_source == "org" else None,
        conversation_id=conversation_id,
        input_tokens=max(int(input_tokens), 0),
        output_tokens=max(int(output_tokens), 0),
        stop_reason=stop_reason[:40] if stop_reason else None,
    )


def _usage_dict(input_tokens: int | None, output_tokens: int | None) -> dict[str, int]:
    in_tokens = int(input_tokens or 0)
    out_tokens = int(output_tokens or 0)
    return {
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "total_tokens": in_tokens + out_tokens,
    }


async def org_monthly_tokens(
    db: AsyncSession, org_id: str, *, now: datetime | None = None
) -> int:
    """This UTC calendar month's tokens spent through an org's key."""
    start = month_start_utc(now)
    total = (
        await db.execute(
            select(
                func.coalesce(
                    func.sum(HelperUsage.input_tokens + HelperUsage.output_tokens), 0
                )
            ).where(
                HelperUsage.org_id == org_id,
                HelperUsage.key_source == "org",
                HelperUsage.created_at >= start,
            )
        )
    ).scalar_one()
    return int(total or 0)


async def user_monthly_usage(
    db: AsyncSession, user_id: str, *, now: datetime | None = None
) -> dict[str, int]:
    """This month's tokens across the caller's own turns, whatever the key."""
    start = month_start_utc(now)
    input_tokens, output_tokens = (
        await db.execute(
            select(
                func.coalesce(func.sum(HelperUsage.input_tokens), 0),
                func.coalesce(func.sum(HelperUsage.output_tokens), 0),
            ).where(
                HelperUsage.user_id == user_id,
                HelperUsage.created_at >= start,
            )
        )
    ).one()
    return _usage_dict(input_tokens, output_tokens)


async def org_monthly_usage_by_member(
    db: AsyncSession, org_id: str, *, now: datetime | None = None
) -> list[dict]:
    """Per-member org-key usage for this calendar month."""
    start = month_start_utc(now)
    rows = (
        await db.execute(
            select(
                HelperUsage.user_id,
                func.coalesce(func.sum(HelperUsage.input_tokens), 0),
                func.coalesce(func.sum(HelperUsage.output_tokens), 0),
            )
            .where(
                HelperUsage.org_id == org_id,
                HelperUsage.key_source == "org",
                HelperUsage.created_at >= start,
            )
            .group_by(HelperUsage.user_id)
            .order_by(HelperUsage.user_id)
        )
    ).all()
    return [
        {"user_id": user_id, **_usage_dict(input_tokens, output_tokens)}
        for user_id, input_tokens, output_tokens in rows
    ]

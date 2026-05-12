"""Autosync settings, watchlist, and status routes."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import Session, SyncWatchlist, User
from sessionfs.server.tier_gate import UserContext, check_feature, get_user_context

router = APIRouter(prefix="/api/v1/sync", tags=["sync"])


class UpdateSyncSettings(BaseModel):
    mode: str  # "off", "all", "selective"
    debounce_seconds: int | None = None


class SyncSettingsResponse(BaseModel):
    mode: str
    debounce_seconds: int


class WatchlistEntry(BaseModel):
    session_id: str
    title: str | None
    source_tool: str | None
    message_count: int
    status: str
    last_synced_at: datetime | None


class WatchlistResponse(BaseModel):
    sessions: list[WatchlistEntry]


class SyncStatusResponse(BaseModel):
    mode: str
    total_sessions: int
    synced_sessions: int
    watched_sessions: int
    queued: int
    failed: int
    storage_used_bytes: int
    storage_limit_bytes: int


# ---- Settings ----


@router.get("/settings", response_model=SyncSettingsResponse)
async def get_sync_settings(
    user: User = Depends(get_current_user),
) -> SyncSettingsResponse:
    """Get user's autosync configuration."""
    return SyncSettingsResponse(
        mode=user.sync_mode or "off",
        debounce_seconds=user.sync_debounce or 30,
    )


@router.put("/settings", response_model=SyncSettingsResponse)
async def update_sync_settings(
    body: UpdateSyncSettings,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> SyncSettingsResponse:
    """Update autosync mode."""
    if body.mode != "off":
        check_feature(ctx, "autosync")
    if body.mode not in ("off", "all", "selective"):
        raise HTTPException(400, "Mode must be 'off', 'all', or 'selective'")

    user.sync_mode = body.mode
    if body.debounce_seconds is not None:
        user.sync_debounce = max(5, min(300, body.debounce_seconds))
    await db.commit()

    return SyncSettingsResponse(
        mode=user.sync_mode,
        debounce_seconds=user.sync_debounce,
    )


# ---- Watchlist ----


@router.get("/watchlist", response_model=WatchlistResponse)
async def get_watchlist(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WatchlistResponse:
    """Get sessions in the autosync watchlist."""
    stmt = (
        select(SyncWatchlist, Session)
        .outerjoin(Session, Session.id == SyncWatchlist.session_id)
        .where(SyncWatchlist.user_id == user.id)
        .order_by(SyncWatchlist.created_at.desc())
    )
    result = await db.execute(stmt)
    rows = result.all()

    entries = []
    for watch, session in rows:
        entries.append(WatchlistEntry(
            session_id=watch.session_id,
            title=session.title if session else None,
            source_tool=session.source_tool if session else None,
            message_count=session.message_count if session else 0,
            status=watch.status,
            last_synced_at=watch.last_synced_at,
        ))

    return WatchlistResponse(sessions=entries)


@router.post("/watch/{session_id}")
async def watch_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Add a session to the autosync watchlist."""
    # Check session exists and belongs to user
    stmt = select(Session).where(Session.id == session_id, Session.user_id == user.id)
    result = await db.execute(stmt)
    if result.scalar_one_or_none() is None:
        raise HTTPException(404, "Session not found")

    # Upsert
    existing = await db.execute(
        select(SyncWatchlist).where(
            SyncWatchlist.user_id == user.id,
            SyncWatchlist.session_id == session_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(SyncWatchlist(
            user_id=user.id,
            session_id=session_id,
            status="pending",
        ))
        await db.commit()

    return {"status": "watching", "session_id": session_id}


@router.delete("/watch/{session_id}")
async def unwatch_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove a session from the autosync watchlist."""
    stmt = delete(SyncWatchlist).where(
        SyncWatchlist.user_id == user.id,
        SyncWatchlist.session_id == session_id,
    )
    await db.execute(stmt)
    await db.commit()
    return {"status": "unwatched", "session_id": session_id}


@router.put("/watch/{session_id}/{status}")
async def update_watch_status(
    session_id: str,
    status: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update watchlist entry status (called by daemon during sync lifecycle)."""
    valid = {"pending", "queued", "synced", "failed"}
    if status not in valid:
        raise HTTPException(400, f"Status must be one of: {', '.join(sorted(valid))}")

    values: dict = {"status": status}
    if status == "synced":
        values["last_synced_at"] = datetime.now(timezone.utc)

    result = await db.execute(
        update(SyncWatchlist)
        .where(
            SyncWatchlist.user_id == user.id,
            SyncWatchlist.session_id == session_id,
        )
        .values(**values)
    )
    await db.commit()
    if result.rowcount == 0:  # type: ignore[union-attr]
        return {"status": "not_watched", "session_id": session_id}
    return {"status": status, "session_id": session_id}


# ---- Status ----


@router.get("/status", response_model=SyncStatusResponse)
async def get_sync_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SyncStatusResponse:
    """Get current sync status."""
    import os

    # Collapse the 5 prior aggregates to 2 round-trips.
    # Round-trip 1: session total + storage in one SELECT over the same
    # filtered Session set.
    session_agg = await db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(Session.blob_size_bytes), 0),
        ).where(
            Session.user_id == user.id,
            Session.is_deleted == False,  # noqa: E712
        )
    )
    total, storage_used = session_agg.one()
    total = total or 0
    storage_used = storage_used or 0

    # Round-trip 2: watchlist counts grouped by status. The total is
    # the sum of all rows for this user; "queued" and "failed" are the
    # status-specific bucket counts. Using COUNT FILTER (BY status =
    # 'X') would let us do this in one expression, but SQLite doesn't
    # support FILTER pre-3.30 — GROUP BY is cross-DB safe.
    watchlist_rows = (await db.execute(
        select(SyncWatchlist.status, func.count())
        .where(SyncWatchlist.user_id == user.id)
        .group_by(SyncWatchlist.status)
    )).all()
    watched = sum(count for _status, count in watchlist_rows)
    queued = next(
        (count for status, count in watchlist_rows if status == "queued"), 0
    )
    failed = next(
        (count for status, count in watchlist_rows if status == "failed"), 0
    )

    # Storage limit based on effective tier — org members inherit their org's
    # tier instead of being limited by personal user.tier.
    from sessionfs.server.tier_gate import get_effective_tier
    from sessionfs.server.tiers import Tier

    free_limit = int(os.environ.get("SFS_MAX_SYNC_BYTES_FREE", str(50 * 1024 * 1024)))
    paid_limit = int(os.environ.get("SFS_MAX_SYNC_BYTES_PAID", str(300 * 1024 * 1024)))
    effective_tier = await get_effective_tier(user, db)
    limit = paid_limit if effective_tier in (Tier.PRO, Tier.TEAM, Tier.ENTERPRISE) else free_limit

    return SyncStatusResponse(
        mode=user.sync_mode or "off",
        total_sessions=total,
        synced_sessions=total,  # all cloud sessions are synced by definition
        watched_sessions=watched,
        queued=queued,
        failed=failed,
        storage_used_bytes=storage_used,
        storage_limit_bytes=limit,
    )

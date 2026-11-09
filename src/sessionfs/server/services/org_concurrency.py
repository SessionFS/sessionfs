"""Per-org concurrency caps on the expensive routes.

The API runs as several instances, so an in-process counter would only cap
one instance. Slots live in the ``org_concurrency_slots`` table instead: a
request claims one of ``limit`` numbered slots for its (scope, route class)
with a guarded INSERT (the primary key makes two holders of one slot
impossible) and deletes it in ``finally``. A slot whose holder died is taken
over by the next claim once its lease has expired (guarded UPDATE on the old
holder token), so a crash never leaks capacity for longer than the lease.

Every claim and release is one short transaction on its OWN session, never
the request's: no database lock or transaction is held across a provider
call or a compile.

Scope: ``org:<org id>``; a personal (no-org) project or turn keys on its
owner, ``user:<user id>``. Route classes and their default caps:

    extraction  4   meeting / chat action-item extraction
    helper      8   session helper turns
    compile     2   KB compile, rebuild, group compile
    export      2   approval-log and contact CSV exports

Configured with ``SFS_ORG_CONCURRENCY_<CLASS>``; 0 turns a cap off. Over the
cap the request gets 429 ``org_concurrency_limit`` with ``details.route_class``,
``details.limit`` and a ``Retry-After`` header. A database error while
claiming fails open (logged): the cap protects shared capacity, it is not an
access control.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from fastapi import HTTPException
from sqlalchemy import and_, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionfs.server.db.models import OrgConcurrencySlot

logger = logging.getLogger("sessionfs.org_concurrency")

ROUTE_CLASSES: tuple[str, ...] = ("extraction", "helper", "compile", "export")

DEFAULT_LIMITS: dict[str, int] = {"extraction": 4, "helper": 8, "compile": 2, "export": 2}

# Lease per class: longer than the longest a request of that class runs, so a
# live holder is never taken over (extraction's provider deadline is 240 s, a
# streamed helper turn is cut at 600 s by the proxy).
LEASE_SECONDS: dict[str, int] = {"extraction": 330, "helper": 660, "compile": 900, "export": 300}

RETRY_AFTER_SECONDS: dict[str, int] = {"extraction": 15, "helper": 5, "compile": 30, "export": 10}

_limits: dict[str, int] = dict(DEFAULT_LIMITS)

# Strong references to scheduled releases (``release_soon``) so the event loop
# doesn't garbage-collect them mid-flight.
_pending_releases: set[asyncio.Task[None]] = set()


def configure_limits(limits: dict[str, int]) -> None:
    """Set the per-class caps for this process (from ``ServerConfig``)."""
    for route_class, value in limits.items():
        if route_class in DEFAULT_LIMITS:
            _limits[route_class] = max(int(value), 0)


def reset_limits() -> None:
    _limits.clear()
    _limits.update(DEFAULT_LIMITS)


def limit_for(route_class: str) -> int:
    return _limits.get(route_class, 0)


def scope_key(org_id: str | None, user_id: str | None) -> str | None:
    """``org:<id>``, else ``user:<id>`` (personal), else None (no cap)."""
    if org_id:
        return f"org:{org_id}"
    if user_id:
        return f"user:{user_id}"
    return None


def limit_error(route_class: str, limit: int) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "code": "org_concurrency_limit",
            "message": (
                "Too many of these requests are already running for this "
                "organization. Try again shortly."
            ),
            "route_class": route_class,
            "limit": limit,
        },
        headers={"Retry-After": str(RETRY_AFTER_SECONDS.get(route_class, 10))},
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    # SQLite hands back naive datetimes; they were written as UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _factory(db: AsyncSession | None) -> async_sessionmaker[AsyncSession]:
    bind = db.bind if db is not None else None
    if bind is None:
        from sessionfs.server.db.engine import get_session_factory

        return get_session_factory()
    return async_sessionmaker(bind, expire_on_commit=False)


class OrgSlot:
    """A held slot. ``release`` is idempotent; a no-op slot holds nothing."""

    __slots__ = ("_factory", "scope_key", "route_class", "slot_no", "holder", "_released")

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession] | None,
        scope_key: str | None = None,
        route_class: str | None = None,
        slot_no: int | None = None,
        holder: str | None = None,
    ) -> None:
        self._factory = factory
        self.scope_key = scope_key
        self.route_class = route_class
        self.slot_no = slot_no
        self.holder = holder
        self._released = factory is None

    @property
    def held(self) -> bool:
        return not self._released

    async def _delete(self) -> None:
        assert self._factory is not None
        try:
            async with self._factory() as session:
                await session.execute(
                    delete(OrgConcurrencySlot).where(
                        OrgConcurrencySlot.scope_key == self.scope_key,
                        OrgConcurrencySlot.route_class == self.route_class,
                        OrgConcurrencySlot.slot_no == self.slot_no,
                        OrgConcurrencySlot.holder == self.holder,
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001 — the lease expires on its own
            logger.warning("org concurrency slot release failed", exc_info=True)

    async def release(self) -> None:
        """Free the slot, shielded so a cancelled request still frees it."""
        if self._released:
            return
        self._released = True
        await asyncio.shield(self._delete())

    def release_soon(self) -> None:
        """Schedule the release from synchronous code (idempotent)."""
        if self._released:
            return
        self._released = True
        try:
            task = asyncio.get_running_loop().create_task(self._delete())
        except RuntimeError:
            return  # no loop: the lease expires on its own
        _pending_releases.add(task)
        task.add_done_callback(_pending_releases.discard)


async def drain_pending_releases() -> None:
    """Wait for scheduled releases (tests and shutdown)."""
    while _pending_releases:
        await asyncio.gather(*list(_pending_releases), return_exceptions=True)


async def _claim(
    session: AsyncSession, key: str, route_class: str, limit: int
) -> tuple[int, str] | None:
    lease = timedelta(seconds=LEASE_SECONDS.get(route_class, 600))
    for _ in range(3):
        now = _now()
        token = secrets.token_hex(16)
        rows = (
            await session.execute(
                select(
                    OrgConcurrencySlot.slot_no,
                    OrgConcurrencySlot.holder,
                    OrgConcurrencySlot.expires_at,
                ).where(
                    OrgConcurrencySlot.scope_key == key,
                    OrgConcurrencySlot.route_class == route_class,
                )
            )
        ).all()
        await session.rollback()
        taken = {r.slot_no for r in rows}
        free = [n for n in range(limit) if n not in taken]
        stale = [r for r in rows if r.slot_no < limit and _aware(r.expires_at) <= now]
        if not free and not stale:
            return None
        for slot_no in free:
            session.add(
                OrgConcurrencySlot(
                    scope_key=key,
                    route_class=route_class,
                    slot_no=slot_no,
                    holder=token,
                    acquired_at=now,
                    expires_at=now + lease,
                )
            )
            try:
                await session.commit()
                return slot_no, token
            except IntegrityError:
                await session.rollback()  # someone else took it; next number
        for row in stale:
            # Take over a dead holder's slot only if nobody else did first.
            result = await session.execute(
                update(OrgConcurrencySlot)
                .where(
                    and_(
                        OrgConcurrencySlot.scope_key == key,
                        OrgConcurrencySlot.route_class == route_class,
                        OrgConcurrencySlot.slot_no == row.slot_no,
                        OrgConcurrencySlot.holder == row.holder,
                    )
                )
                .values(holder=token, acquired_at=now, expires_at=now + lease)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            if (result.rowcount or 0) == 1:
                return row.slot_no, token
        # Lost every race this round: re-read once more.
    return None


async def acquire_org_slot(
    db: AsyncSession | None,
    route_class: str,
    *,
    org_id: str | None,
    user_id: str | None,
) -> OrgSlot:
    """Claim a slot or raise the 429 ``org_concurrency_limit`` envelope.

    Call it after the caller's access to the resource has been checked (an
    invisible resource keeps its 404) and before the expensive work. The
    caller must ``release`` the returned slot in ``finally``.
    """
    limit = limit_for(route_class)
    key = scope_key(org_id, user_id)
    if limit <= 0 or key is None:
        return OrgSlot(None)
    factory = _factory(db)
    try:
        async with factory() as session:
            claimed = await _claim(session, key, route_class, limit)
    except Exception:  # noqa: BLE001 — fail open; the cap is not access control
        logger.warning("org concurrency slot claim failed; not capping", exc_info=True)
        return OrgSlot(None)
    if claimed is None:
        from sessionfs.server.business_metrics import record_org_concurrency_rejection

        record_org_concurrency_rejection(route_class, org_id)
        raise limit_error(route_class, limit)
    slot_no, token = claimed
    return OrgSlot(factory, key, route_class, slot_no, token)


@asynccontextmanager
async def org_slot(
    db: AsyncSession | None,
    route_class: str,
    *,
    org_id: str | None,
    user_id: str | None,
) -> AsyncIterator[OrgSlot]:
    slot = await acquire_org_slot(db, route_class, org_id=org_id, user_id=user_id)
    try:
        yield slot
    finally:
        await slot.release()


def project_scope(project: Any) -> tuple[str | None, str | None]:
    """(org_id, owner user id) for a project: personal projects key on the owner."""
    return getattr(project, "org_id", None), getattr(project, "owner_id", None)

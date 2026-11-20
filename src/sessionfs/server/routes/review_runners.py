"""Auto-reviewer P1a — personal runner enrolment and work polling.

A runner is a customer-controlled process that pulls ``draft_for_me`` review
requests over HTTP. P1 runners authenticate with their owner's own user API
key (``get_current_user`` rejects service keys with 403), so a runner's work
boundary is exactly its owner's requests, narrowed further by the project ids
its owner listed at enrolment.

Endpoints:
- ``POST /api/v1/me/review-runners``                  enrol (idempotent per owner)
- ``GET  /api/v1/me/review-runners``                  my runners
- ``POST /api/v1/me/review-runners/{id}/retire``      retire an active runner
- ``GET  /api/v1/me/review-runners/{id}/next``        next claimable request (ETag / 304)

One active runner per owner is enforced by the partial unique index on
``owner_user_id`` where ``status='active'``; the Community edition further
caps the whole installation at one active runner (``runner_limit`` 409).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import ReviewRequest, ReviewRunner, User
from sessionfs.server.routes.knowledge import _get_project_or_404
from sessionfs.server.routes.review_requests import (
    _readable_project_ids,
    sweep_review_requests,
)
from sessionfs.server.services.scope_locks import advisory_key
from sessionfs.server.sanitize import sanitize_archive_string
from sessionfs.server.tier_gate import get_edition

router = APIRouter(prefix="/api/v1/me/review-runners", tags=["review-runners"])

_MAX_NAME = 100
_MAX_PROJECT_IDS = 1000
_LAST_SEEN_THROTTLE_SECONDS = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_loads(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


async def _lock_community_runner_slot(db: AsyncSession) -> None:
    """Serialize installation-wide runner enrolment in the Community edition.

    PostgreSQL: a transaction advisory lock on a stable key, so two
    concurrent enrols cannot both pass the one-active-runner check before
    inserting. SQLite has a single writer; the no-op write below takes the
    writer lock for the rest of the transaction, serializing the same path.
    """
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": advisory_key("community_runner", "slot")},
        )
    else:
        await db.execute(text("UPDATE review_runners SET id = id WHERE 1 = 0"))


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _runner_dict(r: ReviewRunner) -> dict:
    return {
        "id": r.id,
        "owner_user_id": r.owner_user_id,
        "name": r.name,
        "kind": r.kind,
        "project_ids": _json_loads(r.project_ids, []),
        "status": r.status,
        "last_seen_at": _iso(r.last_seen_at),
        "created_at": _iso(r.created_at),
        "retired_at": _iso(r.retired_at),
    }


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    bare = etag.removeprefix("W/")
    for candidate in header.split(","):
        c = candidate.strip()
        if c == "*" or c.removeprefix("W/") == bare:
            return True
    return False


async def _load_my_runner(
    db: AsyncSession, user_id: str, runner_id: str, *, active_only: bool = True
) -> ReviewRunner:
    stmt = select(ReviewRunner).where(
        ReviewRunner.id == runner_id, ReviewRunner.owner_user_id == user_id
    )
    if active_only:
        stmt = stmt.where(ReviewRunner.status == "active")
    runner = (await db.execute(stmt)).scalar_one_or_none()
    if runner is None:
        raise HTTPException(404, "Runner not found")
    return runner


class EnrolRunnerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=_MAX_NAME)
    kind: Literal["local", "ci"] = "local"
    project_ids: list[str] = Field(min_length=1, max_length=_MAX_PROJECT_IDS)


@router.post("")
async def enrol_runner(
    body: EnrolRunnerRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Enrol my runner. Idempotent: an existing active runner is returned."""
    name, _ = sanitize_archive_string(body.name)
    if not name:
        raise HTTPException(
            422,
            {
                "code": "invalid_name",
                "error": "invalid_name",
                "message": "name is required",
            },
        )

    active = (
        await db.execute(
            select(ReviewRunner).where(
                ReviewRunner.owner_user_id == user.id,
                ReviewRunner.status == "active",
            )
        )
    ).scalars().first()
    if active is not None:
        return _runner_dict(active)

    # Community edition: one active runner for the whole installation.
    if get_edition() == "community":
        await _lock_community_runner_slot(db)
        other_active = (
            await db.execute(
                select(ReviewRunner.id)
                .where(ReviewRunner.status == "active")
                .limit(1)
            )
        ).scalars().first()
        if other_active is not None:
            raise HTTPException(
                409,
                {
                    "code": "runner_limit",
                    "error": "runner_limit",
                    "message": "This edition allows one active runner at a time",
                },
            )

    # Every project the runner will pull work for must still be readable by
    # its owner (a miss is a 404, matching "every miss is one 404 body").
    for pid in body.project_ids:
        if not isinstance(pid, str) or not pid:
            raise HTTPException(
                422,
                {
                    "code": "invalid_project_ids",
                    "error": "invalid_project_ids",
                    "message": "project_ids must be non-empty project id strings",
                },
            )
        await _get_project_or_404(pid, db, user.id)

    project_ids = json.dumps(list(dict.fromkeys(body.project_ids)))

    runner = ReviewRunner(
        id=f"rnr_{uuid.uuid4().hex[:16]}",
        owner_user_id=user.id,
        name=name,
        kind=body.kind,
        project_ids=project_ids,
        status="active",
    )
    db.add(runner)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409,
            {
                "code": "runner_limit",
                "error": "runner_limit",
                "message": "You already have an active runner",
            },
        )
    await db.refresh(runner)
    return _runner_dict(runner)


@router.get("")
async def list_my_runners(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """All of my runners, newest first."""
    rows = (
        await db.execute(
            select(ReviewRunner)
            .where(ReviewRunner.owner_user_id == user.id)
            .order_by(ReviewRunner.created_at.desc(), ReviewRunner.id.desc())
        )
    ).scalars().all()
    return {"runners": [_runner_dict(r) for r in rows]}


@router.post("/{runner_id}/retire")
async def retire_runner(
    runner_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retire an active runner so it stops polling (204 when already retired)."""
    runner = await _load_my_runner(db, user.id, runner_id, active_only=False)
    if runner.status == "retired":
        return Response(status_code=204)
    now = _utcnow()
    await db.execute(
        update(ReviewRunner)
        .where(ReviewRunner.id == runner.id)
        .values(status="retired", retired_at=now)
    )
    # Revoke the retired runner's leases in the same transaction: its claimed
    # requests go back to the queue with a new epoch, so a heartbeat or
    # finish from the retired process is fenced out.
    await db.execute(
        update(ReviewRequest)
        .where(
            ReviewRequest.lease_runner_id == runner.id,
            ReviewRequest.status == "claimed",
        )
        .values(
            status="queued",
            lease_runner_id=None,
            lease_expires_at=None,
            lease_epoch=func.coalesce(ReviewRequest.lease_epoch, 0) + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return Response(status_code=204)


@router.get("/{runner_id}/next")
async def next_review_request(
    runner_id: str,
    response: Response,
    if_none_match: str | None = Header(None, alias="If-None-Match"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The owner's oldest claimable request within this runner's projects.

    Poll with ``If-None-Match`` — no new work answers 304. "Claimable" is
    ``queued`` or a ``claimed`` row whose lease has expired (a crashed
    runner's work becomes visible again). ``last_seen_at`` is refreshed at
    most once a minute."""
    runner = await _load_my_runner(db, user.id, runner_id, active_only=True)

    # Lazy expiry: a claimed request past its lease goes back to queued.
    await sweep_review_requests(db)

    now = _utcnow()
    project_ids = _json_loads(runner.project_ids, [])
    readable = await _readable_project_ids(db, user.id, set(project_ids))
    candidate = (
        await db.execute(
            select(ReviewRequest)
            .where(
                ReviewRequest.requester_user_id == user.id,
                ReviewRequest.project_id.in_(readable),
                or_(
                    ReviewRequest.status == "queued",
                    (ReviewRequest.status == "claimed")
                    & (ReviewRequest.lease_expires_at <= now),
                ),
            )
            .order_by(ReviewRequest.created_at, ReviewRequest.id)
            .limit(1)
        )
    ).scalars().first()

    # Throttle last_seen_at to one write a minute (idle polling is cheap).
    # SQLite returns naive datetimes from DateTime(timezone=True); normalize
    # before the subtraction (PostgreSQL hands back tz-aware automatically).
    last_seen = runner.last_seen_at
    if last_seen is not None and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    if last_seen is None or (
        now - last_seen >= timedelta(seconds=_LAST_SEEN_THROTTLE_SECONDS)
    ):
        await db.execute(
            update(ReviewRunner)
            .where(ReviewRunner.id == runner.id)
            .values(last_seen_at=now)
        )
        await db.commit()

    if candidate is None:
        etag = 'W/"next-empty"'
    else:
        h = hashlib.sha256()
        h.update(candidate.id.encode())
        h.update(str(candidate.lease_epoch).encode())
        h.update(
            (candidate.updated_at.isoformat() if candidate.updated_at else "").encode()
        )
        etag = f'W/"{h.hexdigest()[:32]}"'

    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    for k, v in headers.items():
        response.headers[k] = v

    if candidate is None:
        return {"next": None}

    return {
        "next": {
            "id": candidate.id,
            "project_id": candidate.project_id,
            "provider": candidate.provider,
            "repo": candidate.repo,
            "pr_number": candidate.pr_number,
            "head_sha": candidate.head_sha,
            "base_sha": candidate.base_sha,
            "profile": candidate.profile,
            "instructions_version": candidate.instructions_version,
            "status": candidate.status,
            "lease_epoch": candidate.lease_epoch,
            "lease_expires_at": _iso(candidate.lease_expires_at),
            "created_at": _iso(candidate.created_at),
        }
    }

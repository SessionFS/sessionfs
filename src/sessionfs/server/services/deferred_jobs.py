"""Durable jobs that used to run only after the response (tk_e3eecad8c3874d92).

A request that needs follow-up work it should not wait for (a merge check
recheck, an org-wide merge recheck, knowledge extraction after a push, the
knowledge backfill of a new project) writes a ``deferred_jobs`` row with
:func:`request_job` in its own transaction, then may try the job at once with
:func:`spawn_job` (a post-response fast path, see ``server/post_response.py``).
Whatever is still pending is drained by an existing scheduled sweep through
:func:`drain`:

* merge rechecks — ``POST /admin/github-webhook-deliveries/sweep`` (every 5 min)
* knowledge extraction/backfill — ``POST /admin/sweeps/kb-compile`` (every 15 min)

Protocol, per ``(kind, subject_id)`` row:

1. ``request_job`` upserts the row and bumps ``generation``; a row that is
   being run keeps its claim, any other becomes due at once.
2. ``run_job`` claims a due row (guarded UPDATE setting ``claim_token`` and a
   lease in ``available_at``), commits, and runs the handler with no
   transaction of its own open.
3. On success the row is deleted only if ``generation`` is unchanged; a
   request that landed mid-run leaves the row due again. On failure the
   attempt is counted with a backoff; after ``MAX_ATTEMPTS`` the row is
   dropped with a warning (handlers are idempotent best-effort work).

A run whose task stalls holds the claim at most ``CLAIM_LEASE_SECONDS``;
after that the sweep takes it over.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, delete, null, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server import post_response
from sessionfs.server.db.models import DeferredJob

logger = logging.getLogger("sessionfs.deferred_jobs")

KIND_MERGE_RECHECK = "merge_recheck"
KIND_MERGE_ORG_RECHECK = "merge_org_recheck"
KIND_KNOWLEDGE_EXTRACT = "knowledge_extract"
KIND_KNOWLEDGE_BACKFILL = "knowledge_backfill"
KIND_GITLAB_MR_COMMENT = "gitlab_mr_comment"

# Drained by the GitHub webhook delivery sweep.
MERGE_KINDS = (KIND_MERGE_RECHECK, KIND_MERGE_ORG_RECHECK, KIND_GITLAB_MR_COMMENT)
KNOWLEDGE_KINDS = (KIND_KNOWLEDGE_EXTRACT, KIND_KNOWLEDGE_BACKFILL)

MAX_ATTEMPTS = 5
CLAIM_LEASE_SECONDS = 600
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600

Handler = Callable[[str, Any, dict[str, Any]], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _backoff(attempts: int) -> int:
    """1, 4, 16, 64 minutes (capped at an hour)."""
    return min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 4 ** max(0, attempts - 1))


async def request_job(
    db: AsyncSession, kind: str, subject_id: str, *, payload: str | None = None
) -> None:
    """Record (or re-request) a job inside the caller's transaction.

    The caller commits. Re-requesting bumps ``generation`` so a run already
    in flight can't retire the newer request (and its checkpoints stop
    landing); a row that is not claimed becomes due immediately, its failure
    count and resume cursor start over, and a given ``payload`` replaces the
    stored one.
    """
    if kind not in _HANDLERS:
        raise ValueError(f"unknown deferred job kind: {kind}")
    now = _now()
    values = {
        "kind": kind,
        "subject_id": subject_id,
        "generation": 1,
        "requested_at": now,
        "available_at": None,
        "claim_token": None,
        "attempts": 0,
        "last_error": None,
        "payload": payload,
        "cursor": None,
    }
    set_: dict[str, Any] = {
        "generation": DeferredJob.generation + 1,
        "requested_at": now,
        "attempts": 0,
        "last_error": None,
        "cursor": None,
        # A claimed row keeps its lease (the holder re-releases it as due
        # when it sees the bumped generation); anything else is due now.
        "available_at": case(
            (DeferredJob.claim_token.is_(None), null()),
            else_=DeferredJob.available_at,
        ),
    }
    if payload is not None:
        set_["payload"] = payload
    connection = await db.connection()
    if connection.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(DeferredJob).values(**values).on_conflict_do_update(
            index_elements=["kind", "subject_id"], set_=set_
        )
    else:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(DeferredJob).values(**values).on_conflict_do_update(
            index_elements=["kind", "subject_id"], set_=set_
        )
    await db.execute(stmt)


async def record_job(
    db: AsyncSession, kind: str, subject_id: str, *, payload: str | None = None
) -> bool:
    """``request_job`` inside a savepoint of the caller's transaction.

    Returns False (and logs) when the row could not be written; the caller's
    transaction is untouched either way, and the caller then runs the work
    the way it did before durable rows existed.
    """
    try:
        async with db.begin_nested():
            await request_job(db, kind, subject_id, payload=payload)
        return True
    except Exception:
        logger.warning(
            "deferred job %s %s not recorded; running it directly", kind, subject_id,
            exc_info=True,
        )
        return False


def _due_clause(now: datetime):
    return and_(
        DeferredJob.attempts < MAX_ATTEMPTS,
        or_(DeferredJob.available_at.is_(None), DeferredJob.available_at <= now),
    )


@dataclass(frozen=True)
class Claim:
    """What a handler gets in ``context["claim"]``: the fence for
    :func:`checkpoint`, the stored payload and the resume cursor."""

    kind: str
    subject_id: str
    token: str
    generation: int
    payload: str | None
    cursor: str | None


async def _claim(db: AsyncSession, kind: str, subject_id: str) -> Claim | None:
    now = _now()
    token = uuid.uuid4().hex
    result = await db.execute(
        update(DeferredJob)
        .where(
            DeferredJob.kind == kind,
            DeferredJob.subject_id == subject_id,
            _due_clause(now),
        )
        .values(claim_token=token, available_at=now + timedelta(seconds=CLAIM_LEASE_SECONDS))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.commit()
        return None
    generation, payload, cursor = (
        await db.execute(
            select(DeferredJob.generation, DeferredJob.payload, DeferredJob.cursor).where(
                DeferredJob.kind == kind, DeferredJob.subject_id == subject_id
            )
        )
    ).one()
    await db.commit()
    return Claim(kind, subject_id, token, int(generation), payload, cursor)


async def checkpoint(session_factory: Any, claim: Claim, cursor: str) -> bool:
    """Store a resume point and extend the lease (a heartbeat) for a long job.

    Fenced on the claim token AND the generation: returns False when the
    claim was taken over or the job was re-requested, and the handler should
    then stop (the newer run starts from its own cursor).
    """
    now = _now()
    async with session_factory() as db:
        result = await db.execute(
            update(DeferredJob)
            .where(
                DeferredJob.kind == claim.kind,
                DeferredJob.subject_id == claim.subject_id,
                DeferredJob.claim_token == claim.token,
                DeferredJob.generation == claim.generation,
            )
            .values(
                cursor=cursor[:64],
                available_at=now + timedelta(seconds=CLAIM_LEASE_SECONDS),
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1


async def _complete(db: AsyncSession, claim: Claim) -> bool:
    """Retire the job; False when it was re-requested meanwhile (the claim is
    then released so the newer request is due at once)."""
    key = (DeferredJob.kind == claim.kind, DeferredJob.subject_id == claim.subject_id)
    result = await db.execute(
        delete(DeferredJob)
        .where(
            *key,
            DeferredJob.claim_token == claim.token,
            DeferredJob.generation == claim.generation,
        )
        .execution_options(synchronize_session=False)
    )
    retired = result.rowcount == 1
    if not retired:
        await db.execute(
            update(DeferredJob)
            .where(*key, DeferredJob.claim_token == claim.token)
            .values(claim_token=None, available_at=None)
            .execution_options(synchronize_session=False)
        )
    await db.commit()
    return retired


async def _release(session_factory: Any, claim: Claim) -> None:
    """Give a claim back as due now (the run was cancelled before it ran to
    the end; nothing is counted)."""
    async with session_factory() as db:
        await db.execute(
            update(DeferredJob)
            .where(
                DeferredJob.kind == claim.kind,
                DeferredJob.subject_id == claim.subject_id,
                DeferredJob.claim_token == claim.token,
            )
            .values(claim_token=None, available_at=None)
            .execution_options(synchronize_session=False)
        )
        await db.commit()


async def _fail(
    db: AsyncSession, kind: str, subject_id: str, token: str, exc: BaseException
) -> str:
    key = (DeferredJob.kind == kind, DeferredJob.subject_id == subject_id)
    row = (
        await db.execute(select(DeferredJob).where(*key, DeferredJob.claim_token == token))
    ).scalar_one_or_none()
    if row is None:
        await db.commit()
        return "failed"
    attempts = int(row.attempts or 0) + 1
    if attempts >= MAX_ATTEMPTS:
        await db.execute(
            delete(DeferredJob)
            .where(*key, DeferredJob.claim_token == token)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        logger.warning(
            "deferred job %s %s dropped after %d attempts (%s)",
            kind, subject_id, attempts, type(exc).__name__,
        )
        return "dropped"
    await db.execute(
        update(DeferredJob)
        .where(*key, DeferredJob.claim_token == token)
        .values(
            attempts=attempts,
            last_error=f"internal error ({type(exc).__name__})"[:200],
            claim_token=None,
            available_at=_now() + timedelta(seconds=_backoff(attempts)),
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return "failed"


async def run_job(
    kind: str,
    subject_id: str,
    *,
    session_factory: Any,
    context: dict[str, Any] | None = None,
) -> str:
    """Claim and run one job. Returns done | busy | failed | dropped.

    ``busy`` means the row was not due (absent, claimed by another run, or
    backing off). When the job was re-requested while it ran, it runs once
    more right away instead of waiting for the sweep. A cancelled run gives
    its claim back as due before re-raising. Otherwise never raises.
    """
    handler = _HANDLERS.get(kind)
    if handler is None or session_factory is None:
        return "busy"
    outcome = "busy"
    for _ in range(2):
        try:
            async with session_factory() as db:
                claim = await _claim(db, kind, subject_id)
        except Exception:
            logger.warning("deferred job %s %s claim failed", kind, subject_id, exc_info=True)
            return outcome
        if claim is None:
            return outcome
        ctx = dict(context or {})
        ctx["claim"] = claim
        try:
            await handler(subject_id, session_factory, ctx)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(_release(session_factory, claim))
            except Exception:  # the lease lapses on its own
                logger.warning("deferred job %s %s claim not released", kind, subject_id)
            raise
        except Exception as exc:  # noqa: BLE001 — counted and retried
            logger.warning("deferred job %s %s failed", kind, subject_id, exc_info=True)
            try:
                async with session_factory() as db:
                    return await _fail(db, kind, subject_id, claim.token, exc)
            except Exception:
                logger.warning("deferred job %s %s failure not recorded", kind, subject_id)
                return "failed"
        outcome = "done"
        try:
            async with session_factory() as db:
                if await _complete(db, claim):
                    return outcome
        except Exception:
            # The claim lapses and the sweep runs the (idempotent) job again.
            logger.warning("deferred job %s %s completion not recorded", kind, subject_id)
            return outcome
        # Re-requested while running: run the newer request now.
    return outcome


def spawn_job(
    kind: str,
    subject_id: str,
    *,
    context: dict[str, Any] | None = None,
    background_tasks: Any = None,
) -> None:
    """Try a recorded job right away on a post-response task (fast path).

    The row is already durable, so a task that never runs (or a disabled
    fast path) only leaves the job to its sweep. ``background_tasks`` runs it
    on the route's Starlette background tasks instead of a detached task.
    """
    from sessionfs.server.db import engine

    if engine._session_factory is None:
        return
    post_response.spawn(
        run_job(kind, subject_id, session_factory=engine._session_factory, context=context),
        name=f"deferred:{kind}",
        background_tasks=background_tasks,
    )


async def drain(
    kinds: tuple[str, ...],
    *,
    session_factory: Any,
    context: dict[str, Any] | None = None,
    limit: int = 50,
    budget_seconds: float = 50.0,
) -> dict[str, int]:
    """Run due jobs of ``kinds`` (oldest request first), bounded by count and
    wall clock. Each job commits on its own; safe to run repeatedly."""
    stats = {"selected": 0, "done": 0, "failed": 0, "dropped": 0, "busy": 0}
    if session_factory is None or limit <= 0:
        return stats
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(DeferredJob.kind, DeferredJob.subject_id)
                .where(DeferredJob.kind.in_(kinds), _due_clause(_now()))
                .order_by(DeferredJob.requested_at, DeferredJob.kind, DeferredJob.subject_id)
                .limit(limit)
            )
        ).all()
    stats["selected"] = len(rows)
    deadline = time.monotonic() + budget_seconds
    for kind, subject_id in rows:
        if time.monotonic() >= deadline:
            break
        outcome = await run_job(
            kind, subject_id, session_factory=session_factory, context=context
        )
        stats[outcome] = stats.get(outcome, 0) + 1
    return stats


# ── Handlers ──────────────────────────────────────────────────────────


async def _merge_recheck(ticket_id: str, session_factory: Any, context: dict) -> None:
    from sessionfs.server.services.merge_checks import on_verdict_recorded

    async with session_factory() as db:
        await on_verdict_recorded(db, ticket_id)
        # Keep any approval request raised for the ticket.
        await db.commit()


async def _merge_org_recheck(org_id: str, session_factory: Any, context: dict) -> None:
    """Pages through the org's open-PR tickets from the stored cursor; each
    committed page is checkpointed (resume point + lease heartbeat), so a run
    that is taken over or stalls resumes where it stopped instead of
    re-posting every check from the first page."""
    from sessionfs.server.services.merge_checks import recheck_org

    claim: Claim | None = context.get("claim")

    async def on_page(last_ticket_id: str) -> bool:
        if claim is None:
            return True
        return await checkpoint(session_factory, claim, last_ticket_id)

    await recheck_org(
        org_id,
        session_factory=session_factory,
        after=claim.cursor if claim is not None else None,
        on_page=on_page,
    )


async def _knowledge_extract(session_id: str, session_factory: Any, context: dict) -> None:
    from sessionfs.server.db.models import Session
    from sessionfs.server.routes.sessions import _auto_extract_knowledge

    async with session_factory() as db:
        session = await db.get(Session, session_id)
        if session is None or session.is_deleted:
            return
        etag = session.etag
        blob_key = session.blob_key
        remote = session.git_remote_normalized
        user_id = session.user_id
    if not remote or not blob_key:
        return
    archive = context.get("archive") if context.get("archive_etag") == etag else None
    if archive is None:
        blob_store = context.get("blob_store")
        if blob_store is None:
            raise RuntimeError("no blob store to read the session archive")
        archive = await blob_store.get(blob_key)
        if not archive:
            return
    await _auto_extract_knowledge(session_id, archive, remote, user_id)


async def _knowledge_backfill(project_id: str, session_factory: Any, context: dict) -> None:
    from sessionfs.server.db.models import Project
    from sessionfs.server.routes.projects import _backfill_knowledge_for_project

    async with session_factory() as db:
        project = await db.get(Project, project_id)
        if project is None or not project.git_remote_normalized:
            return
        remote = project.git_remote_normalized
        owner_id = project.owner_id
    blob_store = context.get("blob_store")
    if blob_store is None:
        raise RuntimeError("no blob store to read session archives")
    await _backfill_knowledge_for_project(project_id, remote, owner_id, blob_store)


async def _gitlab_mr_comment(subject_id: str, session_factory: Any, context: dict) -> None:
    """Post or refresh the legacy GitLab MR session comment from the stored
    webhook fields (the note's hidden marker makes a retry idempotent)."""
    import json

    from sessionfs.server.routes import webhooks

    claim: Claim | None = context.get("claim")
    if claim is None or not claim.payload:
        return
    payload = json.loads(claim.payload)
    await webhooks._handle_gitlab_mr(payload, None, payload.get("auth_user_id"))


_HANDLERS: dict[str, Handler] = {
    KIND_MERGE_RECHECK: _merge_recheck,
    KIND_MERGE_ORG_RECHECK: _merge_org_recheck,
    KIND_KNOWLEDGE_EXTRACT: _knowledge_extract,
    KIND_KNOWLEDGE_BACKFILL: _knowledge_backfill,
    KIND_GITLAB_MR_COMMENT: _gitlab_mr_comment,
}

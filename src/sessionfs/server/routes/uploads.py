"""Chunked, resumable session upload routes (GitHub #91).

The initiate endpoint lives on the sessions router (``POST
/api/v1/sessions/{id}/uploads``) so it shares the session-ID validation and
tier helpers there. Everything after initiate operates on an ``upload_id``
and lives here: part upload, status, complete and abort.

Complete assembles the staged parts and then hands the bytes to the SAME
``_sync_push`` function the single-shot push uses, so tar validation, tier
limits, DLP, quota, ETag/If-Match, project routing and summaries are one
code path.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import tempfile
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import require_verified_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import SessionUpload, SessionUploadPart, User
from sessionfs.server.routes.sessions import (
    COMPLETING_RECLAIM_MINUTES,
    _acquire_user_sync_semaphore,
    _delete_part_blobs,
    _get_blob_store,
    _if_match_from_request,
    _lazily_abort_expired_pending_uploads,
    _reclaim_stale_completing_uploads,
    _sync_push,
    _upload_part_blob_key,
    _validate_if_match,
)
from sessionfs.server.schemas.sessions import SessionUploadStatusResponse, SyncPushResponse
from sessionfs.server.storage.base import BlobStore
from sessionfs.server.tier_gate import UserContext, get_user_context
from sessionfs.server.timing import PhaseTimer

logger = logging.getLogger("sessionfs.api")

router = APIRouter(prefix="/api/v1/uploads", tags=["uploads"])

# Per-user in-flight part uploads, process-local. Each user may PUT at most
# _MAX_CONCURRENT_PART_PUTS parts at once; requests past the cap get 429
# upload_busy with a Retry-After hint (mirrors helper_busy / publish_busy).
_MAX_CONCURRENT_PART_PUTS = 2
_PART_PUT_RETRY_AFTER_SECONDS = 1
_part_puts_in_flight: dict[str, int] = {}


def _acquire_part_slot(user_id: str) -> None:
    if _part_puts_in_flight.get(user_id, 0) >= _MAX_CONCURRENT_PART_PUTS:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "upload_busy",
                "message": "You already have the maximum number of part uploads running.",
                "retry_after": _PART_PUT_RETRY_AFTER_SECONDS,
            },
            headers={"Retry-After": str(_PART_PUT_RETRY_AFTER_SECONDS)},
        )
    _part_puts_in_flight[user_id] = _part_puts_in_flight.get(user_id, 0) + 1


def _release_part_slot(user_id: str) -> None:
    remaining = _part_puts_in_flight.get(user_id, 0)
    if remaining <= 1:
        _part_puts_in_flight.pop(user_id, None)
    else:
        _part_puts_in_flight[user_id] = remaining - 1


async def _get_owned_upload(
    upload_id: str, user_id: str, db: AsyncSession, *, for_update: bool = False
) -> SessionUpload:
    """Load an upload owned by the caller, or 404 (owner hidden, not revealed)."""
    stmt = select(SessionUpload).where(SessionUpload.id == upload_id)
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    upload = result.scalar_one_or_none()
    if upload is None or upload.user_id != user_id:
        raise HTTPException(status_code=404, detail="Upload not found")
    return upload


def _expected_part_size(upload: SessionUpload, part_number: int) -> int:
    """Byte size a part must have (the last part may be short)."""
    if part_number < upload.part_count:
        return upload.part_size
    return upload.total_size - (upload.part_count - 1) * upload.part_size


def _as_utc(dt: datetime) -> datetime:
    """Return ``dt`` as timezone-aware UTC.

    SQLite returns naive datetimes for ``DateTime(timezone=True)`` columns,
    so a bare comparison against ``datetime.now(timezone.utc)`` raises.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ensure_upload_active(upload: SessionUpload, now: datetime) -> None:
    if upload.status != "pending":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "upload_not_pending",
                "message": f"Upload is {upload.status}",
            },
        )
    if _as_utc(upload.expires_at) <= _as_utc(now):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "upload_expired",
                "message": "Upload has expired; start a new upload",
            },
        )


async def _read_part_body(request: Request, expected_size: int) -> bytes:
    """Read a part body, rejecting oversized uploads before buffering them.

    The Content-Length pre-check rejects an oversized part without reading a
    byte (a 40 MiB body for a 100-byte part never hits memory). The stream
    read still hard-caps at ``expected_size`` so a client that omits or lies
    about Content-Length can't balloon memory either.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = -1
        if declared > expected_size:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "part_size_mismatch",
                    "message": f"Part must be {expected_size} bytes, got {declared}",
                },
            )

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > expected_size:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "part_size_mismatch",
                    "message": f"Part must be {expected_size} bytes, got more than {expected_size}",
                },
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _complete_revert_status(exc: HTTPException) -> str:
    """The status to revert a completing upload to when the push fails.

    Terminal errors — the single-shot error set the client treats as
    start-over (etag_mismatch 409, deleted 410, too-large 413, any 400, plus
    upload_expired/upload_not_pending) — mark the upload aborted so its staged
    state can't be replayed against a stale If-Match. Transient errors (rate
    limit and 5xx) revert to pending so a retry reuses the staged parts.
    """
    if exc.status_code == 429 or exc.status_code >= 500:
        return "pending"
    return "aborted"


def _stored_push_result(
    upload: SessionUpload, http_response: Response, timer: PhaseTimer
) -> Response | SyncPushResponse:
    """Replay the stored result of an already-completed upload."""
    if (
        upload.result_etag is None
        or upload.result_size is None
        or upload.result_synced_at is None
        or upload.result_created is None
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "upload_not_pending",
                "message": "Upload has no stored push result",
            },
        )
    payload = SyncPushResponse(
        session_id=upload.session_id,
        etag=upload.result_etag,
        blob_size_bytes=upload.result_size,
        synced_at=upload.result_synced_at,
    )
    http_response.headers["Server-Timing"] = timer.server_timing()
    if upload.result_created:
        return Response(
            status_code=201,
            content=payload.model_dump_json(),
            media_type="application/json",
        )
    return payload


def _extract_push_result_values(
    push_result: Response | SyncPushResponse,
) -> tuple[str, int, datetime]:
    """Pull (etag, size, synced_at) out of a ``_sync_push`` return value.

    ``_sync_push`` returns a ``SyncPushResponse`` on the update/undelete path
    and a 201 ``Response`` (whose body is the same schema) on the create path.
    Reading the values here — rather than re-fetching ``Session`` afterwards —
    captures exactly what the push produced, so a concurrent write between
    push and finalize can't poison the stored upload result.
    """
    if isinstance(push_result, Response):
        model = SyncPushResponse.model_validate_json(push_result.body)
        return model.etag, model.blob_size_bytes, model.synced_at
    return push_result.etag, push_result.blob_size_bytes, push_result.synced_at


async def _finalize_upload(
    upload_id: str,
    *,
    status: str,
    created: bool,
    result_etag: str | None,
    result_size: int | None,
    result_synced_at: datetime | None,
    blob_store: BlobStore,
    part_numbers: list[int],
    completion_token: str,
) -> None:
    """Mark an upload terminal (or back to pending) and clean up staged parts.

    ``_sync_push`` closes the request-scoped db session, so this runs in a
    fresh one (the same pattern Phase 3 uses). On success the push result —
    the etag, size and update timestamp the push actually produced — is stored
    on the upload row so a retried complete can replay it without re-pushing.
    The values are passed in from the ``_sync_push`` return value rather than
    re-read from ``Session`` here, so a concurrent write between push and
    finalize can't poison the stored result. Parts are kept when reverting to
    ``pending`` so a transient retry reuses them; every other terminal state
    drops them.

    The update is fenced by ``completion_token``: it only applies while the
    row is still ``completing`` with the token this complete wrote at claim
    time. If a reclaim reverted the row (or a new complete re-claimed it) in
    the meantime, the token is gone and the update matches zero rows — this
    stale complete must not overwrite the newer claimant's stored result, so
    it logs and returns without touching the row or the staged parts.
    """
    from sessionfs.server.db.engine import _session_factory as _sf

    async def _run(db2: AsyncSession) -> bool:
        values: dict = {
            "status": status,
            "completing_since": None,
            "completion_token": None,
        }
        if status == "completed":
            values.update(
                result_etag=result_etag,
                result_size=result_size,
                result_synced_at=result_synced_at,
                result_created=created,
            )
        result = await db2.execute(
            update(SessionUpload)
            .where(
                SessionUpload.id == upload_id,
                SessionUpload.status == "completing",
                SessionUpload.completion_token == completion_token,
            )
            .values(**values)
        )
        if result.rowcount == 0:
            await db2.rollback()
            return False
        if status != "pending":
            await db2.execute(
                delete(SessionUploadPart).where(SessionUploadPart.upload_id == upload_id)
            )
        await db2.commit()
        return True

    finalized = False
    if _sf is not None:
        async with _sf() as db2:
            finalized = await _run(db2)
    else:
        from sessionfs.server.db.engine import get_db as _get_db_gen

        async for db2 in _get_db_gen():
            finalized = await _run(db2)
            break
        else:
            raise RuntimeError("No database session available for upload finalization")

    if not finalized:
        logger.info(
            "upload_complete finalize fenced out upload=%s (stale completion_token)",
            upload_id,
        )
        return

    if status != "pending":
        await _delete_part_blobs(blob_store, upload_id, part_numbers)


@router.put("/{upload_id}/parts/{part_number}", status_code=200)
async def put_upload_part(
    upload_id: str,
    part_number: int,
    request: Request,
    user: User = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload one part of a chunked upload. Idempotent: re-upload overwrites.

    Concurrent part PUTs per user are capped at ``_MAX_CONCURRENT_PART_PUTS``
    (429 ``upload_busy`` with Retry-After past the cap) so one client can't
    pin every in-flight request body buffer at once.
    """
    _acquire_part_slot(user.id)
    try:
        return await _put_upload_part(upload_id, part_number, request, user, db)
    finally:
        _release_part_slot(user.id)


async def _put_upload_part(
    upload_id: str,
    part_number: int,
    request: Request,
    user: User,
    db: AsyncSession,
):
    now = datetime.now(timezone.utc)
    upload = await _get_owned_upload(upload_id, user.id, db)
    _ensure_upload_active(upload, now)

    if part_number < 1 or part_number > upload.part_count:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_part_number",
                "message": f"part_number must be between 1 and {upload.part_count}",
            },
        )

    expected_sha256 = request.headers.get("X-Part-SHA256", "").strip().lower()
    if not expected_sha256 or len(expected_sha256) != 64:
        raise HTTPException(
            status_code=400, detail="Missing or invalid X-Part-SHA256 header"
        )

    expected_size = _expected_part_size(upload, part_number)
    raw = await _read_part_body(request, expected_size)
    if len(raw) != expected_size:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "part_size_mismatch",
                "message": f"Part {part_number} must be {expected_size} bytes, got {len(raw)}",
            },
        )
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "part_hash_mismatch",
                "message": f"Part {part_number} sha256 does not match X-Part-SHA256",
            },
        )

    blob_store = _get_blob_store(request)
    await blob_store.put(_upload_part_blob_key(upload.id, part_number), raw)

    # Lock only around the part-row upsert so the body read + hash happen
    # without holding the row lock.
    locked = await _get_owned_upload(upload_id, user.id, db, for_update=True)
    _ensure_upload_active(locked, now)

    existing = await db.scalar(
        select(SessionUploadPart).where(
            SessionUploadPart.upload_id == upload.id,
            SessionUploadPart.part_number == part_number,
        )
    )
    if existing is not None:
        existing.size = len(raw)
        existing.sha256 = actual_sha256
    else:
        db.add(
            SessionUploadPart(
                upload_id=upload.id,
                part_number=part_number,
                size=len(raw),
                sha256=actual_sha256,
            )
        )
    await db.commit()

    return {
        "upload_id": upload.id,
        "part_number": part_number,
        "size": len(raw),
        "sha256": actual_sha256,
    }


@router.get("/{upload_id}")
async def get_upload(
    upload_id: str,
    user: User = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Report an upload's status and which parts have been received."""
    upload = await _get_owned_upload(upload_id, user.id, db)

    status = upload.status
    if status == "pending" and _as_utc(upload.expires_at) <= _as_utc(
        datetime.now(timezone.utc)
    ):
        status = "expired"

    result = await db.execute(
        select(SessionUploadPart.part_number)
        .where(SessionUploadPart.upload_id == upload.id)
        .order_by(SessionUploadPart.part_number)
    )
    received = [row[0] for row in result.all()]

    return SessionUploadStatusResponse(
        status=status,
        part_size=upload.part_size,
        part_count=upload.part_count,
        received=received,
        expires_at=upload.expires_at,
    )


@router.post("/{upload_id}/complete")
async def complete_upload(
    upload_id: str,
    background_tasks: BackgroundTasks,
    http_response: Response,
    request: Request,
    user: User = Depends(require_verified_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Assemble all parts and run them through the shared sync-push path.

    The response is identical to the single-shot push: 201 (created) or 200
    (updated) with the session etag. If-Match is taken from THIS request's
    header (the initiate-time value is gone with the dropped column). A
    completed upload replayed by its owner returns the stored result without
    re-pushing.
    """
    timer = PhaseTimer()
    status = 500
    try:
        now = datetime.now(timezone.utc)
        upload = await _get_owned_upload(upload_id, user.id, db)

        # Idempotent retry: replay the stored result of a completed upload.
        if upload.status == "completed":
            result = _stored_push_result(upload, http_response, timer)
            status = result.status_code if isinstance(result, Response) else 200
            return result

        if upload.status == "pending" and _as_utc(upload.expires_at) <= _as_utc(now):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "upload_expired",
                    "message": "Upload has expired; start a new upload",
                },
            )

        if_match = _validate_if_match(_if_match_from_request(request))

        # Cheap per-user cleanup: abort this user's expired pending uploads
        # and reclaim any completing rows stuck past the grace window.
        expired = await _lazily_abort_expired_pending_uploads(
            db, user_id=user.id, now=now, exclude_id=upload.id
        )
        reclaimed = await _reclaim_stale_completing_uploads(
            db, user_id=user.id, now=now, exclude_id=upload.id
        )

        # Claim the row for completion. rowcount == 1 is the concurrency
        # guard: a concurrent complete (or an abort that raced us) loses.
        # The fresh completion_token fences the stale-completing reclaim (and
        # any later re-claim) against this complete's finalize: if the token
        # is gone by the time we finalize, we no longer own the row.
        completion_token = secrets.token_hex(16)
        claimed = await db.execute(
            update(SessionUpload)
            .where(
                SessionUpload.id == upload.id,
                SessionUpload.user_id == user.id,
                SessionUpload.status == "pending",
            )
            .values(
                status="completing",
                completing_since=now,
                completion_token=completion_token,
            )
        )
        if claimed.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "upload_not_pending",
                    "message": "Upload is no longer pending; re-check its status",
                },
            )
        await db.commit()

        blob_store = _get_blob_store(request)
        for cleaned_id, cleaned_part_numbers in [*expired, *reclaimed]:
            await _delete_part_blobs(blob_store, cleaned_id, cleaned_part_numbers)

        # Assemble and push under the per-user sync semaphore (non-reentrant,
        # so acquire here and hand it to _sync_push to avoid deadlock). The
        # acquire lives INSIDE the try so a non-HTTPException failure (including
        # CancelledError) still reverts the row instead of leaving it stuck.
        part_numbers: list[int] = []
        push_result: Response | SyncPushResponse | None = None
        semaphore: asyncio.Semaphore | None = None
        try:
            semaphore = await _acquire_user_sync_semaphore(user.id, timer)
            parts_result = await db.execute(
                select(SessionUploadPart)
                .where(SessionUploadPart.upload_id == upload.id)
                .order_by(SessionUploadPart.part_number)
            )
            part_rows = parts_result.scalars().all()
            part_numbers = [p.part_number for p in part_rows]
            received = {p.part_number for p in part_rows}
            missing = [
                n for n in range(1, upload.part_count + 1) if n not in received
            ]
            if missing:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "missing_parts",
                        "message": "Upload is missing parts; upload them first",
                        "missing": missing,
                    },
                )

            data, actual_sha256 = await _assemble_parts(blob_store, upload, part_rows)
            if actual_sha256 != upload.sha256:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "sha256_mismatch",
                        "message": "Assembled archive does not match the declared sha256",
                    },
                )

            push_result = await _sync_push(
                upload.session_id,
                None,
                background_tasks,
                user,
                ctx,
                db,
                request,
                timer,
                data=data,
                if_match=if_match,
                held_semaphore=semaphore,
            )
        except BaseException as exc:
            revert_status = (
                _complete_revert_status(exc)
                if isinstance(exc, HTTPException)
                else "pending"
            )
            try:
                await _finalize_upload(
                    upload.id,
                    status=revert_status,
                    created=False,
                    result_etag=None,
                    result_size=None,
                    result_synced_at=None,
                    blob_store=blob_store,
                    part_numbers=part_numbers,
                    completion_token=completion_token,
                )
            except BaseException:
                # If the revert itself fails (or is cancelled), the stale-
                # completing reclaim recovers the row on a later request.
                logger.exception(
                    "upload_complete revert failed upload=%s", upload.id
                )
            raise
        finally:
            if semaphore is not None:
                semaphore.release()

        assert push_result is not None
        created = isinstance(push_result, Response)
        result_etag, result_size, result_synced_at = _extract_push_result_values(
            push_result
        )
        await _finalize_upload(
            upload.id,
            status="completed",
            created=created,
            result_etag=result_etag,
            result_size=result_size,
            result_synced_at=result_synced_at,
            blob_store=blob_store,
            part_numbers=part_numbers,
            completion_token=completion_token,
        )

        if isinstance(push_result, Response):
            status = push_result.status_code
            push_result.headers["Server-Timing"] = timer.server_timing()
        else:
            status = 200
            http_response.headers["Server-Timing"] = timer.server_timing()
        return push_result
    except HTTPException as exc:
        status = exc.status_code
        exc.headers = {**(exc.headers or {}), "Server-Timing": timer.server_timing()}
        raise
    finally:
        logger.info(
            "upload_complete upload=%s status=%d %s",
            upload_id,
            status,
            timer.log_fields(),
        )


async def _assemble_parts(
    blob_store: BlobStore, upload: SessionUpload, part_rows: list[SessionUploadPart]
) -> tuple[bytes, str]:
    """Concatenate parts in order into a bounded temp file.

    Each part's size was verified at upload time, so the running total can
    never exceed ``total_size``; the temp file keeps a corrupt/malicious
    upload from ballooning in memory during assembly. The sha256 is computed
    while parts stream into the temp file, so the assembled archive is hashed
    without a second full pass; the bytes are materialised once here because
    ``_sync_push`` needs the whole archive in memory.
    """
    hasher = hashlib.sha256()
    total = 0
    with tempfile.TemporaryFile() as fh:
        for n in range(1, upload.part_count + 1):
            raw = await blob_store.get(_upload_part_blob_key(upload.id, n))
            if raw is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "missing_parts",
                        "message": f"Part {n} is missing from storage",
                        "missing": [n],
                    },
                )
            total += len(raw)
            if total > upload.total_size:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "size_mismatch",
                        "message": "Parts exceed the declared total_size",
                    },
                )
            hasher.update(raw)
            fh.write(raw)
        if total != upload.total_size:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "size_mismatch",
                    "message": f"Assembled size {total} does not match total_size {upload.total_size}",
                },
            )
        fh.seek(0)
        data = fh.read()
    return data, hasher.hexdigest()


async def _delete_upload_blobs(
    blob_store: BlobStore, upload_id: str, part_count: int
) -> None:
    """Best-effort delete of every staged part blob for an upload.

    Iterates the deterministic key space (``part-00001`` .. ``part-<count>``)
    rather than the part rows, so a part whose blob landed but whose row never
    committed (a crash between the ``put`` and the commit) is still reclaimed.
    Part blobs are the only objects under an upload's prefix, so this clears
    the whole prefix.
    """
    for n in range(1, part_count + 1):
        try:
            await blob_store.delete(_upload_part_blob_key(upload_id, n))
        except Exception:
            pass


@router.delete("/{upload_id}", status_code=200)
async def abort_upload(
    upload_id: str,
    request: Request,
    user: User = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Abort a pending upload and delete its staged parts."""
    upload = await _get_owned_upload(upload_id, user.id, db, for_update=True)

    if upload.status == "completed":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "already_completed",
                "message": "Upload has already completed",
            },
        )
    if upload.status == "completing":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "upload_completing",
                "message": "Upload is completing; abort it after completion",
            },
        )

    await db.execute(
        delete(SessionUploadPart).where(SessionUploadPart.upload_id == upload.id)
    )
    upload.status = "aborted"
    await db.commit()

    await _delete_upload_blobs(
        _get_blob_store(request), upload.id, upload.part_count
    )

    return {"upload_id": upload.id, "status": upload.status}


async def abort_expired_pending_uploads(
    db: AsyncSession, blob_store: BlobStore, now: datetime
) -> int:
    """Abort expired pending uploads and reclaim stuck completing uploads.

    Called by the platform retention sweeper. Returns the number of uploads
    aborted (expired pending plus expired completing). A completing row whose
    upload has not expired is reverted to pending so a retry reuses its parts.
    """
    result = await db.execute(
        select(SessionUpload).where(
            SessionUpload.status == "pending",
            SessionUpload.expires_at < now,
        )
    )
    uploads = result.scalars().all()

    aborted = 0
    for upload in uploads:
        await db.execute(
            delete(SessionUploadPart).where(SessionUploadPart.upload_id == upload.id)
        )
        upload.status = "aborted"
        await _delete_upload_blobs(blob_store, upload.id, upload.part_count)
        aborted += 1

    # Reclaim completing rows stuck past the grace window (a complete request
    # that died before finalizing). Revert to pending if the upload is still
    # within its TTL; otherwise abort and drop the staged parts. Either way the
    # completion_token is cleared so the stale complete's finalize is fenced out.
    cutoff = now - timedelta(minutes=COMPLETING_RECLAIM_MINUTES)
    completing_result = await db.execute(
        select(SessionUpload).where(
            SessionUpload.status == "completing",
            SessionUpload.completing_since < cutoff,
        )
    )
    for upload in completing_result.scalars().all():
        if _as_utc(upload.expires_at) <= _as_utc(now):
            await db.execute(
                delete(SessionUploadPart).where(
                    SessionUploadPart.upload_id == upload.id
                )
            )
            upload.status = "aborted"
            upload.completion_token = None
            await _delete_upload_blobs(blob_store, upload.id, upload.part_count)
            aborted += 1
        else:
            upload.status = "pending"
            upload.completing_since = None
            upload.completion_token = None

    await db.commit()
    return aborted

"""Handoff routes: create, claim, inbox, sent + v0.10.9 revoke/decline/comments/events."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    Handoff,
    HandoffAttachment,
    HandoffComment,
    HandoffEvent,
    Session,
    Team,
    TeamMember,
    User,
)
from sessionfs.server.services.handoff_helpers import (
    claim_inbox_match,
    lazy_expire,
    parse_event_payload,
    persist_lazy_expire,
    team_ids_for_user,
    validate_attachments_for_recipient,
    validate_attachments_for_sender,
    validate_provenance_for_sender,
    write_event,
)
from sessionfs.server.tier_gate import UserContext, check_feature, get_user_context
from sessionfs.session_id import generate_session_id

logger = logging.getLogger("sessionfs.api")
from sessionfs.server.schemas.handoffs import (
    ActiveTicketPayload,
    CreateHandoffRequest,
    DeclineHandoffRequest,
    DroppedAttachment,
    HandoffAttachmentResponse,
    HandoffCommentCreate,
    HandoffCommentResponse,
    HandoffEventResponse,
    HandoffListResponse,
    HandoffResponse,
    HandoffSummaryResponse,
    RevokeHandoffRequest,
)

router = APIRouter(prefix="/api/v1/handoffs", tags=["handoffs"])

HANDOFF_EXPIRY_DAYS = 7
HANDOFF_DEFAULT_EXPIRES_HOURS = 168  # 7d, matches HANDOFF_EXPIRY_DAYS
HANDOFF_MAX_EXPIRES_HOURS_STANDARD = 720  # 30d (Free/Pro/Team)
HANDOFF_MAX_EXPIRES_HOURS_ENTERPRISE = 2160  # 90d (Enterprise only)
HANDOFF_COMMENT_PAGE_LIMIT = 200
HANDOFF_EVENT_PAGE_LIMIT = 200


def _clamp_expires_hours(requested: int | None, tier_value: str) -> int:
    """Clamp the requested expires_in_hours to tier-permitted ceiling.
    Default 168 (7d) preserves the v0.10.8 HANDOFF_EXPIRY_DAYS contract.
    Enterprise tier may go up to 90d; everyone else caps at 30d."""
    if requested is None:
        return HANDOFF_DEFAULT_EXPIRES_HOURS
    ceiling = (
        HANDOFF_MAX_EXPIRES_HOURS_ENTERPRISE
        if tier_value == "enterprise"
        else HANDOFF_MAX_EXPIRES_HOURS_STANDARD
    )
    return max(1, min(requested, ceiling))


def normalize_email(value: str | None) -> str:
    """Strip + lowercase. Single source of truth for handoff email keys.

    Used at write time (recipient_email_normalized column), at read time
    in the inbox legacy fallback, in admin cleanup, and in migration 032
    backfill. Anywhere we compare emails for equality, route them
    through here so the on-disk column matches what queries look for.
    Returns "" (not None) so callers can compare directly without a
    None-guard.
    """
    return (value or "").strip().lower()


def _generate_handoff_id() -> str:
    """Generate a handoff ID like hnd_xxxxxxxxxx."""
    return f"hnd_{secrets.token_hex(8)}"


def _effective_status(handoff: Handoff) -> str:
    """Derive status from stored status + expiry."""
    if handoff.status == "claimed":
        return "claimed"
    if handoff.expires_at:
        exp = handoff.expires_at.replace(tzinfo=timezone.utc) if handoff.expires_at.tzinfo is None else handoff.expires_at
        if exp < datetime.now(timezone.utc):
            return "expired"
    return handoff.status


def _handoff_to_response(
    handoff: Handoff,
    sender_email: str,
    session: Session | None = None,
    session_title: str | None = None,
    session_tool: str | None = None,
    *,
    attachments: list[HandoffAttachment] | None = None,
    comments: list[HandoffComment] | None = None,
    events: list[HandoffEvent] | None = None,
    active_ticket_payload: ActiveTicketPayload | None = None,
    dropped_attachments: list[DroppedAttachment] | None = None,
) -> HandoffResponse:
    # Prefer snapshot fields (immune to session-ID reuse) over live session
    snap_title = getattr(handoff, "snapshot_title", None)
    snap_tool = getattr(handoff, "snapshot_tool", None)
    snap_model = getattr(handoff, "snapshot_model_id", None)
    snap_msgs = getattr(handoff, "snapshot_message_count", None)
    snap_tokens = getattr(handoff, "snapshot_total_tokens", None)

    return HandoffResponse(
        id=handoff.id,
        session_id=handoff.session_id,
        recipient_session_id=getattr(handoff, "recipient_session_id", None),
        sender_email=sender_email,
        recipient_email=handoff.recipient_email,
        recipient_user_id=getattr(handoff, "recipient_user_id", None),
        recipient_team_id=getattr(handoff, "recipient_team_id", None),
        message=handoff.message,
        status=_effective_status(handoff),
        session_title=session_title or snap_title or (session.title if session else None),
        session_tool=session_tool or snap_tool or (session.source_tool if session else None),
        session_model_id=snap_model or (session.model_id if session else None),
        session_message_count=snap_msgs if snap_msgs is not None else (session.message_count if session else None),
        session_total_tokens=snap_tokens if snap_tokens is not None else (
            (session.total_input_tokens or 0) + (session.total_output_tokens or 0)
            if session else None
        ),
        created_at=handoff.created_at,
        claimed_at=handoff.claimed_at,
        expires_at=handoff.expires_at,
        # v0.10.9 fields — None/[] when row predates v0.10.9 schema
        ticket_id=getattr(handoff, "ticket_id", None),
        persona_name=getattr(handoff, "persona_name", None),
        revoked_at=getattr(handoff, "revoked_at", None),
        revoked_by_user_id=getattr(handoff, "revoked_by_user_id", None),
        revoke_reason=getattr(handoff, "revoke_reason", None),
        handoff_kind=getattr(handoff, "handoff_kind", None) or "individual",
        viewed_at=getattr(handoff, "viewed_at", None),
        snapshot_persona_name=getattr(handoff, "snapshot_persona_name", None),
        snapshot_ticket_title=getattr(handoff, "snapshot_ticket_title", None),
        sender_tier_snapshot=getattr(handoff, "sender_tier_snapshot", None),
        attachments=[
            HandoffAttachmentResponse(
                kind=att.kind,
                ref_id=att.ref_id,
                project_id=att.project_id or None,
                created_at=att.created_at,
            )
            for att in (attachments or [])
        ],
        comments=[
            HandoffCommentResponse(
                id=c.id,
                handoff_id=c.handoff_id,
                author_user_id=c.author_user_id,
                content=c.content,
                created_at=c.created_at,
            )
            for c in (comments or [])
        ],
        events=[
            HandoffEventResponse(
                id=e.id,
                handoff_id=e.handoff_id,
                event_type=e.event_type,
                actor_user_id=e.actor_user_id,
                payload=parse_event_payload(e.payload),
                created_at=e.created_at,
            )
            for e in (events or [])
        ],
        active_ticket_payload=active_ticket_payload,
        dropped_attachments=list(dropped_attachments or []),
    )


@router.post("", status_code=201, response_model=HandoffResponse)
async def create_handoff(
    body: CreateHandoffRequest,
    request: Request,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Create a handoff — push session to recipient.

    v0.10.9 — recipient may be specified as exactly one of:
    recipient_email (any user, by email — same as v0.10.8),
    recipient_user_id (direct account match), or
    recipient_team_id (team handoff, Team+ tier required).
    Optional provenance: ticket_id + persona_name (validated against
    session's project). Optional attachments: kb_entry / wiki_page /
    ticket refs (validated against session's project).
    """
    check_feature(ctx, "handoff")

    # Team handoffs require Team+ tier (gates broadcast/first-come team flow)
    if body.recipient_team_id is not None:
        check_feature(ctx, "team_handoff")

    # Verify session exists and belongs to sender
    result = await db.execute(
        select(Session).where(Session.id == body.session_id, Session.user_id == user.id)
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # If team handoff, validate the team exists and the sender belongs to it
    # (you can only hand off to teams you're a member of).
    resolved_team: Team | None = None
    if body.recipient_team_id is not None:
        resolved_team = (
            await db.execute(
                select(Team).where(Team.id == body.recipient_team_id)
            )
        ).scalar_one_or_none()
        if resolved_team is None:
            raise HTTPException(status_code=404, detail="Team not found")
        is_member = (
            await db.execute(
                select(TeamMember.id).where(
                    TeamMember.team_id == body.recipient_team_id,
                    TeamMember.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if is_member is None:
            raise HTTPException(
                status_code=403,
                detail="Sender must be a member of the recipient team",
            )

    # If recipient_user_id, validate user exists (case-insensitive email lookup not needed here)
    if body.recipient_user_id is not None:
        target_user = (
            await db.execute(
                select(User.id, User.email).where(User.id == body.recipient_user_id)
            )
        ).one_or_none()
        if target_user is None:
            raise HTTPException(status_code=404, detail="Recipient user not found")

    # Validate provenance + attachments against the session's project scope.
    try:
        snap_ticket_title, snap_persona_name = await validate_provenance_for_sender(
            db,
            sender_id=user.id,
            session=session,
            ticket_id=body.ticket_id,
            persona_name=body.persona_name,
        )
        await validate_attachments_for_sender(
            db,
            sender_id=user.id,
            session=session,
            attachments=[a.model_dump() for a in body.attachments],
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Tier-aware expiry clamp. Enterprise gets up to 90d; everyone else 30d.
    expires_hours = _clamp_expires_hours(body.expires_in_hours, ctx.effective_tier.value)

    now = datetime.now(timezone.utc)
    total_tokens = (session.total_input_tokens or 0) + (session.total_output_tokens or 0)
    handoff_kind = "team" if body.recipient_team_id is not None else "individual"
    handoff = Handoff(
        id=_generate_handoff_id(),
        session_id=body.session_id,
        sender_id=user.id,
        recipient_email=body.recipient_email,
        # Lowercased + stripped copy used by inbox lookups via the
        # dedicated index (migration 032). Keep raw `recipient_email`
        # for display. Routed through the shared helper so the runtime
        # write path, the migration backfill, and admin cleanup all
        # produce byte-identical keys.
        recipient_email_normalized=normalize_email(body.recipient_email) or None,
        recipient_user_id=body.recipient_user_id,
        recipient_team_id=body.recipient_team_id,
        message=body.message,
        status="pending",
        created_at=now,
        expires_at=now + timedelta(hours=expires_hours),
        # Snapshot metadata at creation — immune to session-ID reuse
        snapshot_title=session.title,
        snapshot_tool=session.source_tool,
        snapshot_model_id=session.model_id,
        snapshot_message_count=session.message_count,
        snapshot_total_tokens=total_tokens or None,
        # v0.10.9 provenance + display snapshots + tier snapshot
        ticket_id=body.ticket_id,
        persona_name=body.persona_name,
        handoff_kind=handoff_kind,
        snapshot_ticket_title=snap_ticket_title,
        snapshot_persona_name=snap_persona_name,
        sender_tier_snapshot=ctx.effective_tier.value,
    )
    db.add(handoff)
    await db.flush()  # surface handoff.id for attachment + event FK

    # Attachments — stored with project_id (R2 MEDIUM #3) so the
    # recipient-side validator can do unambiguous (project_id, kind,
    # ref_id) lookup at claim time without slug ambiguity.
    project_id_for_atts: str | None = None
    if body.attachments and session.git_remote_normalized:
        from sessionfs.server.db.models import Project

        proj = (
            await db.execute(
                select(Project.id).where(
                    Project.git_remote_normalized == session.git_remote_normalized
                )
            )
        ).scalar_one_or_none()
        project_id_for_atts = proj
    for att in body.attachments:
        db.add(
            HandoffAttachment(
                handoff_id=handoff.id,
                kind=att.kind,
                ref_id=att.ref_id,
                project_id=project_id_for_atts or "",
                created_at=now,
            )
        )

    # Durable audit row.
    await write_event(
        db,
        handoff_id=handoff.id,
        event_type="created",
        actor_user_id=user.id,
        payload={
            "handoff_kind": handoff_kind,
            "expires_in_hours": expires_hours,
            "attachment_count": len(body.attachments),
            "has_ticket": bool(body.ticket_id),
            "has_persona": bool(body.persona_name),
        },
    )

    await db.commit()
    await db.refresh(handoff)

    # Send handoff email if email service is available.
    # Team handoffs without a recipient_email skip the email step here —
    # Phase 4 will fan out to each team member's email separately.
    email_service = getattr(request.app.state, "email_service", None)
    if email_service is not None and body.recipient_email:
        try:
            total_tokens = (session.total_input_tokens or 0) + (session.total_output_tokens or 0)

            # Read workspace for git info
            git_remote = None
            git_branch = None
            blob_store = getattr(request.app.state, "blob_store", None)
            if blob_store and session.blob_key:
                try:
                    import io
                    import json
                    import tarfile

                    data = await blob_store.get(session.blob_key)
                    if data:
                        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                            for member in tar.getmembers():
                                if member.name == "workspace.json":
                                    f = tar.extractfile(member)
                                    if f:
                                        workspace = json.loads(f.read())
                                        git_info = workspace.get("git", {})
                                        git_remote = git_info.get("remote_url")
                                        git_branch = git_info.get("branch")
                except Exception:
                    pass  # Non-critical — email still sends without git info

            await email_service.send_handoff(
                to_email=body.recipient_email,
                sender_email=user.email,
                session_title=session.title,
                source_tool=session.source_tool,
                model_id=session.model_id,
                message_count=session.message_count or 0,
                total_tokens=total_tokens,
                git_remote=git_remote,
                git_branch=git_branch,
                sender_message=body.message,
                handoff_id=handoff.id,
            )
        except Exception:
            pass  # Email failure should not fail the handoff

    return _handoff_to_response(
        handoff,
        sender_email=user.email,
        session=session,
    )


@router.get("/inbox", response_model=HandoffListResponse)
async def inbox(
    include_team: bool = True,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List handoffs sent TO this user.

    Three match paths (all OR-combined):
      1. recipient_email_normalized == user_email (indexed since migration 032)
      2. legacy raw recipient_email fallback for pre-migration rows
         (whitespace + mixed case)
      3. recipient_user_id == user.id (v0.10.9 direct account match)
      4. recipient_team_id IN (teams user belongs to) (v0.10.9 team handoff)

    Pass `include_team=false` to drop the team-handoff dimension.
    """
    from sqlalchemy import func as sa_func

    user_email_lower = normalize_email(user.email)
    user_team_ids = await team_ids_for_user(db, user.id) if include_team else []

    email_match = or_(
        Handoff.recipient_email_normalized == user_email_lower,
        # Safety net for pre-migration rows where the normalized
        # column is NULL. Migration 032 backfills these but a
        # self-hosted deploy may not have run it yet. Match the
        # raw column with the SAME normalization the runtime
        # write path uses (strip + lower) so a row with leading
        # whitespace doesn't slip past both predicates.
        (Handoff.recipient_email_normalized.is_(None))
        & (
            sa_func.lower(sa_func.trim(Handoff.recipient_email))
            == user_email_lower
        ),
    )
    user_id_match = Handoff.recipient_user_id == user.id
    predicates = [email_match, user_id_match]
    if user_team_ids:
        predicates.append(Handoff.recipient_team_id.in_(user_team_ids))

    result = await db.execute(
        select(Handoff)
        .where(or_(*predicates))
        .order_by(Handoff.created_at.desc())
    )
    handoffs = list(result.scalars().all())

    return HandoffListResponse(
        handoffs=await _hydrate_handoffs(db, handoffs, missing_sender_email="unknown"),
        total=len(handoffs),
    )


@router.get("/sent", response_model=HandoffListResponse)
async def sent(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List handoffs sent BY this user.

    Pre-perf-2 the inner loop set sender_email to user.email directly
    without doing a sender lookup. We preserve that — /sent is always
    the viewer's own handoffs, so falling back to viewer_email when the
    User row is unreachable matches the legacy behavior.
    """
    result = await db.execute(
        select(Handoff)
        .where(Handoff.sender_id == user.id)
        .order_by(Handoff.created_at.desc())
    )
    handoffs = list(result.scalars().all())

    return HandoffListResponse(
        handoffs=await _hydrate_handoffs(
            db, handoffs, missing_sender_email=user.email or "unknown"
        ),
        total=len(handoffs),
    )


async def _hydrate_handoffs(
    db: AsyncSession,
    handoffs: list[Handoff],
    *,
    missing_sender_email: str,
) -> list[HandoffResponse]:
    """Batch-load Senders + Sessions referenced by `handoffs` in one
    query each, then assemble responses without the N+1 round-trips.

    `missing_sender_email` is the fallback used when the User row for a
    sender_id is not found. /inbox passes "unknown" (matching the
    pre-perf-2 behavior so an inbox row never claims its OWN viewer is
    the sender). /sent passes the viewer's email since /sent is always
    the viewer's own handoffs.
    """
    if not handoffs:
        return []

    # Batch sender lookup. /sent always shares one sender (viewer); /inbox
    # can have many. Keep it generic — empty fetch when set is empty.
    sender_ids = {h.sender_id for h in handoffs}
    senders_by_id: dict[str, User] = {}
    if sender_ids:
        sender_result = await db.execute(
            select(User).where(User.id.in_(sender_ids))
        )
        senders_by_id = {s.id: s for s in sender_result.scalars().all()}

    # Batch session lookup.
    session_ids = {h.session_id for h in handoffs}
    sessions_by_id: dict[str, Session] = {}
    if session_ids:
        sessions_result = await db.execute(
            select(Session).where(Session.id.in_(session_ids))
        )
        sessions_by_id = {s.id: s for s in sessions_result.scalars().all()}

    out: list[HandoffResponse] = []
    for h in handoffs:
        sender = senders_by_id.get(h.sender_id)
        sender_email = sender.email if sender else missing_sender_email
        session = sessions_by_id.get(h.session_id)
        out.append(_handoff_to_response(
            h, sender_email=sender_email, session=session,
        ))
    return out


@router.get("/{handoff_id}", response_model=HandoffResponse)
async def get_handoff(
    handoff_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get handoff details (auth required).

    Returns 404 — not 403 — when the caller is neither sender nor a
    valid recipient (individual/team). v0.10.9 design: handoff IDs leak
    secrets if existence is exposed to non-parties.

    Lazy expiry: pending handoffs past expires_at flip to 'expired' on
    this call (atomic, with audit event). Per Codex G, no background
    sweeper in v0.10.9.

    Recipient's first GET records viewed_at + a viewed event so the
    sender knows the handoff was opened (independent of claim).
    """
    result = await db.execute(select(Handoff).where(Handoff.id == handoff_id))
    handoff = result.scalar_one_or_none()
    if handoff is None:
        raise HTTPException(status_code=404, detail="Handoff not found")

    # Lazy expiry: flip pending→expired atomically + record audit event.
    # We do the in-memory check first so we know whether to issue the
    # UPDATE; the UPDATE itself uses WHERE status='pending' as a guard
    # against concurrent claims winning the race.
    if lazy_expire(handoff):
        flipped = await persist_lazy_expire(
            db, handoff_id=handoff.id, current_status="pending"
        )
        if flipped:
            await write_event(
                db,
                handoff_id=handoff.id,
                event_type="expired",
                actor_user_id=None,
            )
            await db.commit()
        else:
            # Concurrent claim/revoke won — reload to see canonical state.
            await db.refresh(handoff)

    # Sender always sees. Recipient sees by email/user_id/team membership.
    # Anything else → 404 (existence is sensitive).
    is_sender = user.id == handoff.sender_id
    if not (is_sender or await claim_inbox_match(db, user, handoff)):
        raise HTTPException(status_code=404, detail="Handoff not found")

    # Preserve the v0.10.8 410-on-expiry contract for callers that
    # branch on status; the lazy-expire UPDATE has already persisted
    # the new state but the route still surfaces 410 for symmetry with
    # the existing /summary + /claim endpoints.
    if handoff.status == "expired":
        raise HTTPException(status_code=410, detail="Handoff has expired")

    # Record viewed_at on first recipient view (does not apply to sender
    # opening their own handoff). Atomic, idempotent — only writes if
    # viewed_at is still NULL.
    if not is_sender and getattr(handoff, "viewed_at", None) is None:
        now = datetime.now(timezone.utc)
        viewed_update = await db.execute(
            update(Handoff)
            .where(Handoff.id == handoff.id, Handoff.viewed_at.is_(None))
            .values(viewed_at=now)
        )
        if viewed_update.rowcount == 1:
            await write_event(
                db,
                handoff_id=handoff.id,
                event_type="viewed",
                actor_user_id=user.id,
            )
            await db.commit()
            handoff.viewed_at = now

    # Look up sender email and session info
    sender = await db.execute(select(User).where(User.id == handoff.sender_id))
    sender_user = sender.scalar_one_or_none()
    sender_email = sender_user.email if sender_user else "unknown"

    session_result = await db.execute(select(Session).where(Session.id == handoff.session_id))
    session = session_result.scalar_one_or_none()

    # Hydrate attachments, comments, events for detail view.
    attachments = list(
        (
            await db.execute(
                select(HandoffAttachment)
                .where(HandoffAttachment.handoff_id == handoff.id)
                .order_by(HandoffAttachment.created_at.asc())
            )
        ).scalars().all()
    )
    comments = list(
        (
            await db.execute(
                select(HandoffComment)
                .where(HandoffComment.handoff_id == handoff.id)
                .order_by(HandoffComment.created_at.asc())
                .limit(HANDOFF_COMMENT_PAGE_LIMIT)
            )
        ).scalars().all()
    )
    events = list(
        (
            await db.execute(
                select(HandoffEvent)
                .where(HandoffEvent.handoff_id == handoff.id)
                .order_by(HandoffEvent.created_at.asc())
                .limit(HANDOFF_EVENT_PAGE_LIMIT)
            )
        ).scalars().all()
    )

    return _handoff_to_response(
        handoff,
        sender_email=sender_email,
        session=session,
        attachments=attachments,
        comments=comments,
        events=events,
    )


@router.post("/{handoff_id}/claim", response_model=HandoffResponse)
async def claim_handoff(
    handoff_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Claim a handoff — copy session data to recipient and mark as claimed.

    v0.10.9 — supports individual + team handoffs. Uses an atomic
    rowcount-1 UPDATE WHERE status='pending' so concurrent team-member
    claims serialize cleanly: the loser sees 409 with a hint pointing
    to the winning claimant. Returns active_ticket_payload (ticket +
    persona + project_id + lease_epoch) the recipient's CLI/MCP can
    drop into ~/.sessionfs/active_ticket.json. Cross-project attachment
    refs the recipient can't access are silently dropped but surfaced
    in dropped_attachments + an audit event.
    """
    result = await db.execute(select(Handoff).where(Handoff.id == handoff_id))
    handoff = result.scalar_one_or_none()
    if handoff is None:
        raise HTTPException(status_code=404, detail="Handoff not found")

    # R3 HIGH 1 — eligibility check FIRST. If the caller isn't a valid
    # recipient, return 404 without revealing whether the handoff is
    # pending vs claimed vs revoked vs expired. Lazy-expire writes are
    # held back until after authorization to prevent an unauthorized
    # caller from triggering a state-changing UPDATE.
    if not await claim_inbox_match(db, user, handoff):
        raise HTTPException(status_code=404, detail="Handoff not found")

    # Lazy expiry — only safe AFTER eligibility confirmed. Same pattern
    # as get_handoff. If we expire here the rest of the function will
    # see status='expired' and 410.
    if lazy_expire(handoff):
        flipped = await persist_lazy_expire(
            db, handoff_id=handoff.id, current_status="pending"
        )
        if flipped:
            await write_event(
                db,
                handoff_id=handoff.id,
                event_type="expired",
                actor_user_id=None,
            )
            await db.commit()
        else:
            await db.refresh(handoff)

    if handoff.status == "expired":
        raise HTTPException(status_code=410, detail="Handoff has expired")
    if handoff.status == "claimed":
        raise HTTPException(status_code=409, detail="Handoff already claimed")
    if handoff.status in {"revoked", "declined"}:
        raise HTTPException(
            status_code=409,
            detail=f"Handoff is {handoff.status} and cannot be claimed",
        )

    # Look up source session
    session_result = await db.execute(select(Session).where(Session.id == handoff.session_id))
    source_session = session_result.scalar_one_or_none()
    if source_session is None:
        raise HTTPException(status_code=404, detail="Source session no longer exists")

    # R3 MEDIUM 2 — win the atomic claim BEFORE doing any blob copy
    # side effect. Pre-allocate new_session_id so we can include it in
    # the atomic UPDATE, but defer the blob copy + Session insert until
    # the UPDATE confirms we won the race. Race losers therefore never
    # write to object storage and never leave orphan blobs.
    blob_store = getattr(request.app.state, "blob_store", None)
    new_session_id = generate_session_id()
    now = datetime.now(timezone.utc)
    new_blob_key = f"sessions/{user.id}/{new_session_id}.tar.gz"

    claim_result = await db.execute(
        update(Handoff)
        .where(Handoff.id == handoff.id, Handoff.status == "pending")
        .values(
            status="claimed",
            recipient_id=user.id,
            recipient_session_id=new_session_id,
            claimed_at=now,
        )
    )
    if claim_result.rowcount != 1:
        # Lost the race — another claimer (or revoke) won. No blob copy
        # was attempted; nothing to clean up. Refresh + report 409 with
        # the canonical winner identity.
        await db.refresh(handoff)
        winner_email = None
        if handoff.recipient_id:
            w = (
                await db.execute(select(User.email).where(User.id == handoff.recipient_id))
            ).scalar_one_or_none()
            winner_email = w
        raise HTTPException(
            status_code=409,
            detail={
                "error": "claim_race_lost",
                "winner_user_id": handoff.recipient_id,
                "winner_email": winner_email,
                "current_status": handoff.status,
            },
        )

    # We won — now safe to copy the blob.
    if blob_store and source_session.blob_key:
        try:
            data = await blob_store.get(source_session.blob_key)
            if data:
                await blob_store.put(new_blob_key, data)
            else:
                logger.warning("Handoff claim: source blob empty for %s", handoff.session_id)
                new_blob_key = source_session.blob_key  # fallback: share blob
        except Exception:
            logger.exception("Handoff claim: failed to copy blob for %s", handoff.session_id)
            new_blob_key = source_session.blob_key  # fallback: share blob

    # Create new session record owned by recipient
    new_etag = hashlib.sha256(f"{new_session_id}{now.isoformat()}".encode()).hexdigest()[:16]
    copied_session = Session(
        id=new_session_id,
        user_id=user.id,
        title=source_session.title,
        tags=source_session.tags,
        source_tool=source_session.source_tool,
        source_tool_version=source_session.source_tool_version,
        original_session_id=source_session.id,
        model_provider=source_session.model_provider,
        model_id=source_session.model_id,
        message_count=source_session.message_count,
        turn_count=source_session.turn_count,
        tool_use_count=source_session.tool_use_count,
        total_input_tokens=source_session.total_input_tokens,
        total_output_tokens=source_session.total_output_tokens,
        duration_ms=source_session.duration_ms,
        blob_key=new_blob_key,
        blob_size_bytes=source_session.blob_size_bytes,
        etag=new_etag,
        parent_session_id=source_session.id,
        created_at=source_session.created_at,
        updated_at=now,
        uploaded_at=now,
        messages_text=source_session.messages_text,
        git_remote_normalized=source_session.git_remote_normalized,
        git_branch=source_session.git_branch,
        git_commit=source_session.git_commit,
    )
    db.add(copied_session)

    # Validate attachments recipient can actually see; structurally drop
    # the rest. Dropped refs surface in the response + audit event for
    # the sender to investigate.
    attachments = list(
        (
            await db.execute(
                select(HandoffAttachment)
                .where(HandoffAttachment.handoff_id == handoff.id)
                .order_by(HandoffAttachment.created_at.asc())
            )
        ).scalars().all()
    )
    dropped_raw = await validate_attachments_for_recipient(
        db, recipient_id=user.id, attachments=attachments,
    )
    dropped = [DroppedAttachment(**d) for d in dropped_raw]
    # Filter the response attachment list to what survived validation,
    # so the recipient's UI doesn't render dangling refs.
    dropped_pairs = {(d.kind, d.ref_id) for d in dropped}
    visible_attachments = [a for a in attachments if (a.kind, a.ref_id) not in dropped_pairs]

    # Build active_ticket_payload — the recipient's CLI/MCP writes this
    # into ~/.sessionfs/active_ticket.json so their next command picks
    # up the right ticket + persona + project context automatically.
    #
    # R3 MEDIUM 1 — persona-only handoffs (persona_name set, ticket_id
    # absent) ALSO need project_id, otherwise write_bundle() rejects
    # the payload and the recipient agent can't load the persona. We
    # resolve project_id from the source session's git_remote when
    # ticket_id is not the source of truth.
    active_payload: ActiveTicketPayload | None = None
    if handoff.ticket_id or handoff.persona_name:
        from sessionfs.server.db.models import Project, Ticket
        from sessionfs.server.services.handoff_helpers import _accessible_project_ids

        proj_id: str | None = None
        lease_epoch: int | None = None
        accessible: set[str] | None = None
        if handoff.ticket_id:
            # Re-validate ticket is still in a project the recipient can
            # see. If not, drop it from the payload and add a sentinel
            # to dropped_attachments so the recipient knows.
            t_row = (
                await db.execute(
                    select(Ticket.project_id, Ticket.lease_epoch).where(
                        Ticket.id == handoff.ticket_id
                    )
                )
            ).one_or_none()
            if t_row is not None:
                t_project_id, t_lease = t_row
                accessible = await _accessible_project_ids(db, user.id)
                if t_project_id in accessible:
                    proj_id = t_project_id
                    lease_epoch = t_lease
        # R3 MEDIUM 1 — persona-only path: resolve project_id from the
        # source session's git_remote so write_bundle has what it needs.
        # Only used when we don't already have a project_id from ticket
        # resolution above.
        if proj_id is None and handoff.persona_name and source_session.git_remote_normalized:
            src_proj_id = (
                await db.execute(
                    select(Project.id).where(
                        Project.git_remote_normalized == source_session.git_remote_normalized
                    )
                )
            ).scalar_one_or_none()
            if src_proj_id is not None:
                if accessible is None:
                    accessible = await _accessible_project_ids(db, user.id)
                if src_proj_id in accessible:
                    proj_id = src_proj_id

        # Drop persona from payload if we have a persona but no
        # accessible project to anchor it to — that's the only way
        # write_bundle would reject the payload downstream.
        keep_persona = handoff.persona_name if proj_id else None
        if handoff.persona_name and not proj_id:
            dropped.append(
                DroppedAttachment(
                    kind="persona",
                    ref_id=handoff.persona_name,
                    reason="project_not_accessible",
                )
            )
        active_payload = ActiveTicketPayload(
            ticket_id=handoff.ticket_id if proj_id else None,
            persona_name=keep_persona,
            project_id=proj_id,
            lease_epoch=lease_epoch,
        )

    # Refresh in-memory handoff so the response reflects the updated
    # status/claimed_at/recipient_id we wrote via UPDATE above.
    await db.refresh(handoff)

    await write_event(
        db,
        handoff_id=handoff.id,
        event_type="claimed",
        actor_user_id=user.id,
        payload={
            "new_session_id": new_session_id,
            "dropped_attachment_count": len(dropped),
            "active_ticket_id": active_payload.ticket_id if active_payload else None,
        },
    )

    await db.commit()
    await db.refresh(handoff)

    logger.info(
        "Handoff %s claimed: session %s copied to %s for user %s",
        handoff_id, handoff.session_id, new_session_id, user.email,
    )

    # Look up sender email
    sender = await db.execute(select(User).where(User.id == handoff.sender_id))
    sender_user = sender.scalar_one_or_none()
    sender_email = sender_user.email if sender_user else "unknown"

    # Notify sender that recipient claimed (best-effort; never fails op).
    email_service = getattr(request.app.state, "email_service", None)
    if email_service is not None and sender_user is not None and sender_user.email:
        try:
            await email_service.send_handoff_claimed(
                to_email=sender_user.email,
                recipient_email=user.email or "unknown",
                session_title=handoff.snapshot_title,
                handoff_id=handoff.id,
            )
        except Exception:
            logger.exception("Handoff %s claim-notify email failed", handoff.id)

    return _handoff_to_response(
        handoff,
        sender_email=sender_email,
        session=copied_session,
        attachments=visible_attachments,
        active_ticket_payload=active_payload,
        dropped_attachments=dropped,
    )


def _generate_comment_id() -> str:
    return f"hcm_{secrets.token_hex(8)}"


@router.post("/{handoff_id}/revoke", response_model=HandoffResponse)
async def revoke_handoff(
    handoff_id: str,
    body: RevokeHandoffRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Sender (or org admin override — Phase 3) revokes a pending handoff.
    Atomic UPDATE WHERE status='pending' guards against a claim winning
    the race; loser sees 409 with the canonical status.
    """
    result = await db.execute(select(Handoff).where(Handoff.id == handoff_id))
    handoff = result.scalar_one_or_none()
    if handoff is None:
        raise HTTPException(status_code=404, detail="Handoff not found")

    # Only the sender can revoke in v0.10.9 scope (admin-override deferred).
    if user.id != handoff.sender_id:
        # 404 — recipient shouldn't even learn revoke is an option for them.
        raise HTTPException(status_code=404, detail="Handoff not found")

    if handoff.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot revoke a {handoff.status} handoff",
        )

    now = datetime.now(timezone.utc)
    update_result = await db.execute(
        update(Handoff)
        .where(Handoff.id == handoff.id, Handoff.status == "pending")
        .values(
            status="revoked",
            revoked_at=now,
            revoked_by_user_id=user.id,
            revoke_reason=body.reason,
        )
    )
    if update_result.rowcount != 1:
        await db.refresh(handoff)
        raise HTTPException(
            status_code=409,
            detail=f"Cannot revoke a {handoff.status} handoff",
        )

    await write_event(
        db,
        handoff_id=handoff.id,
        event_type="revoked",
        actor_user_id=user.id,
        payload={"reason": body.reason},
    )
    await db.commit()
    await db.refresh(handoff)

    sender_email = user.email

    # Notify recipient (individual handoffs with an email only). Team
    # handoffs without a stable per-recipient email are skipped here —
    # they'd require fanning out to each team member, deferred to a
    # later phase.
    email_service = getattr(request.app.state, "email_service", None)
    if (
        email_service is not None
        and handoff.recipient_email
        and handoff.handoff_kind == "individual"
    ):
        try:
            await email_service.send_handoff_revoked(
                to_email=handoff.recipient_email,
                sender_email=sender_email or "unknown",
                session_title=handoff.snapshot_title,
                reason=body.reason,
                handoff_id=handoff.id,
            )
        except Exception:
            logger.exception("Handoff %s revoke-notify email failed", handoff.id)

    return _handoff_to_response(handoff, sender_email=sender_email)


@router.post("/{handoff_id}/decline", response_model=HandoffResponse)
async def decline_handoff(
    handoff_id: str,
    body: DeclineHandoffRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Recipient explicitly declines a pending handoff (optional reason).
    Status flips pending → declined atomically; sender can then redirect.
    """
    result = await db.execute(select(Handoff).where(Handoff.id == handoff_id))
    handoff = result.scalar_one_or_none()
    if handoff is None:
        raise HTTPException(status_code=404, detail="Handoff not found")

    # R3 HIGH 1 — eligibility check before status check. A non-recipient
    # otherwise distinguishes pending (would 200) from non-pending (409)
    # via the response code.
    if not await claim_inbox_match(db, user, handoff):
        raise HTTPException(status_code=404, detail="Handoff not found")

    if handoff.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot decline a {handoff.status} handoff",
        )

    update_result = await db.execute(
        update(Handoff)
        .where(Handoff.id == handoff.id, Handoff.status == "pending")
        .values(status="declined")
    )
    if update_result.rowcount != 1:
        await db.refresh(handoff)
        raise HTTPException(
            status_code=409,
            detail=f"Cannot decline a {handoff.status} handoff",
        )

    await write_event(
        db,
        handoff_id=handoff.id,
        event_type="declined",
        actor_user_id=user.id,
        payload={"reason": body.reason} if body.reason else None,
    )
    await db.commit()
    await db.refresh(handoff)

    sender = await db.execute(select(User).where(User.id == handoff.sender_id))
    sender_user = sender.scalar_one_or_none()
    sender_email = sender_user.email if sender_user else "unknown"

    # Notify sender that recipient declined.
    email_service = getattr(request.app.state, "email_service", None)
    if email_service is not None and sender_user is not None and sender_user.email:
        try:
            await email_service.send_handoff_declined(
                to_email=sender_user.email,
                recipient_email=user.email or "unknown",
                session_title=handoff.snapshot_title,
                reason=body.reason,
                handoff_id=handoff.id,
            )
        except Exception:
            logger.exception("Handoff %s decline-notify email failed", handoff.id)
    return _handoff_to_response(handoff, sender_email=sender_email)


@router.post(
    "/{handoff_id}/comments",
    response_model=HandoffCommentResponse,
    status_code=201,
)
async def create_handoff_comment(
    handoff_id: str,
    body: HandoffCommentCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Post a comment to the handoff thread. Author must be the sender
    or a valid recipient (individual or team)."""
    result = await db.execute(select(Handoff).where(Handoff.id == handoff_id))
    handoff = result.scalar_one_or_none()
    if handoff is None:
        raise HTTPException(status_code=404, detail="Handoff not found")

    is_sender = user.id == handoff.sender_id
    if not (is_sender or await claim_inbox_match(db, user, handoff)):
        raise HTTPException(status_code=404, detail="Handoff not found")

    now = datetime.now(timezone.utc)
    comment = HandoffComment(
        id=_generate_comment_id(),
        handoff_id=handoff.id,
        author_user_id=user.id,
        content=body.content,
        created_at=now,
    )
    db.add(comment)
    await write_event(
        db,
        handoff_id=handoff.id,
        event_type="commented",
        actor_user_id=user.id,
        payload={"comment_id": comment.id},
    )
    await db.commit()
    await db.refresh(comment)

    # Notify the "other party" — if author is sender, notify recipient;
    # if author is recipient, notify sender. Only individual handoffs
    # with a stable recipient email get notified here; team comment
    # fan-out is deferred.
    email_service = getattr(request.app.state, "email_service", None)
    if email_service is not None:
        try:
            target_email: str | None = None
            if user.id == handoff.sender_id:
                if handoff.handoff_kind == "individual" and handoff.recipient_email:
                    target_email = handoff.recipient_email
            else:
                sender_row = (
                    await db.execute(
                        select(User.email).where(User.id == handoff.sender_id)
                    )
                ).scalar_one_or_none()
                if sender_row:
                    target_email = sender_row
            if target_email:
                await email_service.send_handoff_comment(
                    to_email=target_email,
                    author_email=user.email or "unknown",
                    session_title=handoff.snapshot_title,
                    content=body.content,
                    handoff_id=handoff.id,
                )
        except Exception:
            logger.exception("Handoff %s comment-notify email failed", handoff.id)

    return HandoffCommentResponse(
        id=comment.id,
        handoff_id=comment.handoff_id,
        author_user_id=comment.author_user_id,
        content=comment.content,
        created_at=comment.created_at,
    )


@router.get(
    "/{handoff_id}/comments",
    response_model=list[HandoffCommentResponse],
)
async def list_handoff_comments(
    handoff_id: str,
    limit: int = 200,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all comments on a handoff, oldest first. Caller must be the
    sender or a valid recipient."""
    result = await db.execute(select(Handoff).where(Handoff.id == handoff_id))
    handoff = result.scalar_one_or_none()
    if handoff is None:
        raise HTTPException(status_code=404, detail="Handoff not found")

    is_sender = user.id == handoff.sender_id
    if not (is_sender or await claim_inbox_match(db, user, handoff)):
        raise HTTPException(status_code=404, detail="Handoff not found")

    limit = max(1, min(limit, HANDOFF_COMMENT_PAGE_LIMIT))
    rows = (
        await db.execute(
            select(HandoffComment)
            .where(HandoffComment.handoff_id == handoff.id)
            .order_by(HandoffComment.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        HandoffCommentResponse(
            id=c.id,
            handoff_id=c.handoff_id,
            author_user_id=c.author_user_id,
            content=c.content,
            created_at=c.created_at,
        )
        for c in rows
    ]


@router.get(
    "/{handoff_id}/events",
    response_model=list[HandoffEventResponse],
)
async def list_handoff_events(
    handoff_id: str,
    limit: int = 200,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Audit-log view of the handoff's lifecycle events. Caller must be
    the sender or a valid recipient."""
    result = await db.execute(select(Handoff).where(Handoff.id == handoff_id))
    handoff = result.scalar_one_or_none()
    if handoff is None:
        raise HTTPException(status_code=404, detail="Handoff not found")

    is_sender = user.id == handoff.sender_id
    if not (is_sender or await claim_inbox_match(db, user, handoff)):
        raise HTTPException(status_code=404, detail="Handoff not found")

    limit = max(1, min(limit, HANDOFF_EVENT_PAGE_LIMIT))
    rows = (
        await db.execute(
            select(HandoffEvent)
            .where(HandoffEvent.handoff_id == handoff.id)
            .order_by(HandoffEvent.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        HandoffEventResponse(
            id=e.id,
            handoff_id=e.handoff_id,
            event_type=e.event_type,
            actor_user_id=e.actor_user_id,
            payload=parse_event_payload(e.payload),
            created_at=e.created_at,
        )
        for e in rows
    ]


@router.get("/{handoff_id}/summary", response_model=HandoffSummaryResponse)
async def get_handoff_summary(
    handoff_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a deterministic summary of the handoff's session context."""
    import io
    import json
    import tarfile

    from sessionfs.server.services.summarizer import summarize_session
    from sessionfs.server.storage.base import BlobStore

    result = await db.execute(select(Handoff).where(Handoff.id == handoff_id))
    handoff = result.scalar_one_or_none()
    if handoff is None:
        raise HTTPException(status_code=404, detail="Handoff not found")

    # R3 MEDIUM 3 — eligibility check first, then status. Matches
    # claim/decline ordering and uses claim_inbox_match so user_id and
    # team recipients are honored (the old email-only check rejected
    # both). 404-not-403 keeps existence sensitive for non-parties.
    is_sender = user.id == handoff.sender_id
    if not (is_sender or await claim_inbox_match(db, user, handoff)):
        raise HTTPException(status_code=404, detail="Handoff not found")

    # Enforce expiry, claimed status
    exp = handoff.expires_at.replace(tzinfo=timezone.utc) if handoff.expires_at.tzinfo is None else handoff.expires_at
    if exp < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Handoff has expired")
    if handoff.status == "claimed":
        raise HTTPException(status_code=410, detail="Handoff already claimed")

    session_result = await db.execute(
        select(Session).where(Session.id == handoff.session_id)
    )
    session = session_result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Extract messages from blob storage
    blob_store: BlobStore = request.app.state.blob_store
    data = await blob_store.get(session.blob_key) if session.blob_key else None

    messages: list[dict] = []
    manifest: dict = {}
    workspace: dict = {}

    if data:
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                for member in tar.getmembers():
                    f = tar.extractfile(member)
                    if not f:
                        continue
                    content = f.read().decode("utf-8", errors="replace")
                    if member.name.endswith("messages.jsonl"):
                        for line in content.splitlines():
                            line = line.strip()
                            if line:
                                messages.append(json.loads(line))
                    elif member.name.endswith("manifest.json"):
                        manifest = json.loads(content)
                    elif member.name.endswith("workspace.json"):
                        workspace = json.loads(content)
        except Exception:
            logger.warning("Failed to extract session archive for handoff summary")

    # Run deterministic summarizer
    if messages:
        summary = summarize_session(messages, manifest, workspace)
        files_modified = summary.files_modified[:10]
        errors = summary.errors_encountered[:3]
        commands_executed = summary.commands_executed
        tests_run = summary.tests_run
        tests_passed = summary.tests_passed
        tests_failed = summary.tests_failed
    else:
        files_modified = []
        errors = []
        commands_executed = 0
        tests_run = 0
        tests_passed = 0
        tests_failed = 0

    # Extract last 3 assistant messages (truncated)
    last_assistant: list[str] = []
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            text = ""
            content = msg.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        break
            if text:
                last_assistant.append(text[:200])
            if len(last_assistant) >= 3:
                break

    return HandoffSummaryResponse(
        session_id=session.id,
        title=session.title or "Untitled",
        tool=session.source_tool or "",
        model=session.model_id,
        message_count=session.message_count or 0,
        files_modified=files_modified,
        commands_executed=commands_executed,
        tests_run=tests_run,
        tests_passed=tests_passed,
        tests_failed=tests_failed,
        errors_encountered=errors,
        last_assistant_messages=last_assistant,
    )

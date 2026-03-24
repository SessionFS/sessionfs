"""Handoff routes: create, claim, inbox, sent."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import Handoff, Session, User
from sessionfs.server.schemas.handoffs import (
    CreateHandoffRequest,
    HandoffListResponse,
    HandoffResponse,
)

router = APIRouter(prefix="/api/v1/handoffs", tags=["handoffs"])

HANDOFF_EXPIRY_DAYS = 7


def _generate_handoff_id() -> str:
    """Generate a handoff ID like hnd_xxxxxxxxxx."""
    return f"hnd_{secrets.token_hex(8)}"


def _handoff_to_response(
    handoff: Handoff,
    sender_email: str,
    session_title: str | None = None,
    session_tool: str | None = None,
) -> HandoffResponse:
    return HandoffResponse(
        id=handoff.id,
        session_id=handoff.session_id,
        sender_email=sender_email,
        recipient_email=handoff.recipient_email,
        message=handoff.message,
        status=handoff.status,
        session_title=session_title,
        session_tool=session_tool,
        created_at=handoff.created_at,
        expires_at=handoff.expires_at,
    )


@router.post("", status_code=201, response_model=HandoffResponse)
async def create_handoff(
    body: CreateHandoffRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a handoff — push session to recipient via email."""
    # Verify session exists and belongs to sender
    result = await db.execute(
        select(Session).where(Session.id == body.session_id, Session.user_id == user.id)
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    now = datetime.now(timezone.utc)
    handoff = Handoff(
        id=_generate_handoff_id(),
        session_id=body.session_id,
        sender_id=user.id,
        recipient_email=body.recipient_email,
        message=body.message,
        status="pending",
        created_at=now,
        expires_at=now + timedelta(days=HANDOFF_EXPIRY_DAYS),
    )
    db.add(handoff)
    await db.commit()
    await db.refresh(handoff)

    # Send handoff email if email service is available
    email_service = getattr(request.app.state, "email_service", None)
    if email_service is not None:
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
        session_title=session.title,
        session_tool=session.source_tool,
    )


@router.get("/inbox", response_model=HandoffListResponse)
async def inbox(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List handoffs sent TO this user (matched by email)."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Handoff)
        .where(Handoff.recipient_email == user.email)
        .order_by(Handoff.created_at.desc())
    )
    handoffs = list(result.scalars().all())

    responses = []
    for h in handoffs:
        sender = await db.execute(select(User).where(User.id == h.sender_id))
        sender_user = sender.scalar_one_or_none()
        sender_email = sender_user.email if sender_user else "unknown"
        session_result = await db.execute(select(Session).where(Session.id == h.session_id))
        session = session_result.scalar_one_or_none()
        responses.append(_handoff_to_response(
            h, sender_email=sender_email,
            session_title=session.title if session else None,
            session_tool=session.source_tool if session else None,
        ))

    return HandoffListResponse(handoffs=responses, total=len(responses))


@router.get("/sent", response_model=HandoffListResponse)
async def sent(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List handoffs sent BY this user."""
    result = await db.execute(
        select(Handoff)
        .where(Handoff.sender_id == user.id)
        .order_by(Handoff.created_at.desc())
    )
    handoffs = list(result.scalars().all())

    responses = []
    for h in handoffs:
        session_result = await db.execute(select(Session).where(Session.id == h.session_id))
        session = session_result.scalar_one_or_none()
        responses.append(_handoff_to_response(
            h, sender_email=user.email,
            session_title=session.title if session else None,
            session_tool=session.source_tool if session else None,
        ))

    return HandoffListResponse(handoffs=responses, total=len(responses))


@router.get("/{handoff_id}", response_model=HandoffResponse)
async def get_handoff(
    handoff_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get handoff details (public for recipient to view before claiming)."""
    result = await db.execute(select(Handoff).where(Handoff.id == handoff_id))
    handoff = result.scalar_one_or_none()
    if handoff is None:
        raise HTTPException(status_code=404, detail="Handoff not found")

    # Check expiry
    exp = handoff.expires_at.replace(tzinfo=timezone.utc) if handoff.expires_at.tzinfo is None else handoff.expires_at
    if exp < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Handoff has expired")

    # Look up sender email and session info
    sender = await db.execute(select(User).where(User.id == handoff.sender_id))
    sender_user = sender.scalar_one_or_none()
    sender_email = sender_user.email if sender_user else "unknown"

    session_result = await db.execute(select(Session).where(Session.id == handoff.session_id))
    session = session_result.scalar_one_or_none()

    return _handoff_to_response(
        handoff,
        sender_email=sender_email,
        session_title=session.title if session else None,
        session_tool=session.source_tool if session else None,
    )


@router.post("/{handoff_id}/claim", response_model=HandoffResponse)
async def claim_handoff(
    handoff_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Claim a handoff — link the recipient and mark as claimed."""
    result = await db.execute(select(Handoff).where(Handoff.id == handoff_id))
    handoff = result.scalar_one_or_none()
    if handoff is None:
        raise HTTPException(status_code=404, detail="Handoff not found")

    exp = handoff.expires_at.replace(tzinfo=timezone.utc) if handoff.expires_at.tzinfo is None else handoff.expires_at
    if exp < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Handoff has expired")

    if handoff.status == "claimed":
        raise HTTPException(status_code=409, detail="Handoff already claimed")

    # Update handoff
    handoff.recipient_id = user.id
    handoff.status = "claimed"
    handoff.claimed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(handoff)

    # Look up sender email and session info
    sender = await db.execute(select(User).where(User.id == handoff.sender_id))
    sender_user = sender.scalar_one_or_none()
    sender_email = sender_user.email if sender_user else "unknown"

    session_result = await db.execute(select(Session).where(Session.id == handoff.session_id))
    session = session_result.scalar_one_or_none()

    return _handoff_to_response(
        handoff,
        sender_email=sender_email,
        session_title=session.title if session else None,
        session_tool=session.source_tool if session else None,
    )



"""Handoff routes: create, claim, inbox, sent."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import Handoff, Session, User
from sessionfs.server.tier_gate import UserContext, check_feature, get_user_context
from sessionfs.session_id import generate_session_id

logger = logging.getLogger("sessionfs.api")
from sessionfs.server.schemas.handoffs import (
    CreateHandoffRequest,
    HandoffListResponse,
    HandoffResponse,
    HandoffSummaryResponse,
)

router = APIRouter(prefix="/api/v1/handoffs", tags=["handoffs"])

HANDOFF_EXPIRY_DAYS = 7


def _generate_handoff_id() -> str:
    """Generate a handoff ID like hnd_xxxxxxxxxx."""
    return f"hnd_{secrets.token_hex(8)}"


def _handoff_to_response(
    handoff: Handoff,
    sender_email: str,
    session: Session | None = None,
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
        session_title=session_title or (session.title if session else None),
        session_tool=session_tool or (session.source_tool if session else None),
        session_model_id=session.model_id if session else None,
        session_message_count=session.message_count if session else None,
        session_total_tokens=(
            (session.total_input_tokens or 0) + (session.total_output_tokens or 0)
            if session
            else None
        ),
        created_at=handoff.created_at,
        claimed_at=handoff.claimed_at,
        expires_at=handoff.expires_at,
    )


@router.post("", status_code=201, response_model=HandoffResponse)
async def create_handoff(
    body: CreateHandoffRequest,
    request: Request,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Create a handoff — push session to recipient via email."""
    check_feature(ctx, "handoff")
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
        session=session,
    )


@router.get("/inbox", response_model=HandoffListResponse)
async def inbox(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List handoffs sent TO this user (matched by email)."""
    datetime.now(timezone.utc)
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
            h, sender_email=sender_email, session=session,
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
            h, sender_email=user.email, session=session,
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
        session=session,
    )


@router.post("/{handoff_id}/claim", response_model=HandoffResponse)
async def claim_handoff(
    handoff_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Claim a handoff — copy session data to recipient and mark as claimed."""
    result = await db.execute(select(Handoff).where(Handoff.id == handoff_id))
    handoff = result.scalar_one_or_none()
    if handoff is None:
        raise HTTPException(status_code=404, detail="Handoff not found")

    exp = handoff.expires_at.replace(tzinfo=timezone.utc) if handoff.expires_at.tzinfo is None else handoff.expires_at
    if exp < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Handoff has expired")

    if handoff.status == "claimed":
        raise HTTPException(status_code=409, detail="Handoff already claimed")

    # Look up source session
    session_result = await db.execute(select(Session).where(Session.id == handoff.session_id))
    source_session = session_result.scalar_one_or_none()
    if source_session is None:
        raise HTTPException(status_code=404, detail="Source session no longer exists")

    # Copy blob in storage
    blob_store = getattr(request.app.state, "blob_store", None)
    new_session_id = generate_session_id()
    now = datetime.now(timezone.utc)
    new_blob_key = f"sessions/{user.id}/{new_session_id}.tar.gz"

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

    # Update handoff
    handoff.recipient_id = user.id
    handoff.status = "claimed"
    handoff.claimed_at = now
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

    return _handoff_to_response(
        handoff,
        sender_email=sender_email,
        session=copied_session,
    )


@router.get("/{handoff_id}/summary", response_model=HandoffSummaryResponse)
async def get_handoff_summary(
    handoff_id: str,
    request: Request,
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

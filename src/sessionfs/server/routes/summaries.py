"""Session summary routes."""

from __future__ import annotations

import io
import json
import logging
import tarfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import Session, SessionSummaryRecord, User
from sessionfs.server.storage.base import BlobStore
from sessionfs.server.tier_gate import UserContext, check_feature, get_user_context

logger = logging.getLogger("sessionfs.api")

router = APIRouter(prefix="/api/v1/sessions", tags=["summaries"])


class NarrativeRequest(BaseModel):
    model: str | None = None
    provider: str | None = None
    llm_api_key: str | None = None
    base_url: str | None = None


class SummaryResponse(BaseModel):
    session_id: str
    title: str
    tool: str
    model: str | None = None
    duration_minutes: int = 0
    message_count: int = 0
    tool_call_count: int = 0
    branch: str | None = None
    commit: str | None = None
    files_modified: list[str] = []
    files_read: list[str] = []
    commands_executed: int = 0
    tests_run: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    packages_installed: list[str] = []
    errors_encountered: list[str] = []
    what_happened: str | None = None
    key_decisions: list[str] | None = None
    outcome: str | None = None
    open_issues: list[str] | None = None
    narrative_model: str | None = None
    personas_active: list[str] = []
    generated_at: str = ""


@router.get("/{session_id}/summary", response_model=SummaryResponse)
async def get_summary(
    session_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SummaryResponse:
    """Get or generate a deterministic session summary."""
    # Verify session ownership
    stmt = select(Session).where(Session.id == session_id, Session.user_id == user.id)
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")

    # Check cache
    cached = await db.execute(
        select(SessionSummaryRecord).where(SessionSummaryRecord.session_id == session_id)
    )
    existing = cached.scalar_one_or_none()
    if existing:
        return _record_to_response(existing, session)

    # Generate fresh
    summary = await _generate_summary(session, request)
    if summary is None:
        raise HTTPException(422, "Could not generate summary — no messages found")

    # Cache
    record = _summary_to_record(session_id, summary)
    db.add(record)
    await db.commit()

    return _summary_to_response(summary, session)


@router.post("/{session_id}/summary", response_model=SummaryResponse)
async def generate_summary(
    session_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> SummaryResponse:
    """Generate or regenerate a session summary."""
    check_feature(ctx, "summary_deterministic")
    stmt = select(Session).where(Session.id == session_id, Session.user_id == user.id)
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")

    summary = await _generate_summary(session, request)
    if summary is None:
        raise HTTPException(422, "Could not generate summary — no messages found")

    # Upsert cache
    cached = await db.execute(
        select(SessionSummaryRecord).where(SessionSummaryRecord.session_id == session_id)
    )
    existing = cached.scalar_one_or_none()
    record = _summary_to_record(session_id, summary)

    if existing:
        for col in ("duration_minutes", "tool_call_count", "files_modified", "files_read",
                     "commands_executed", "tests_run", "tests_passed", "tests_failed",
                     "packages_installed", "errors_encountered", "personas_active"):
            setattr(existing, col, getattr(record, col))
        existing.created_at = datetime.now(timezone.utc)
    else:
        db.add(record)
    await db.commit()

    return _summary_to_response(summary, session)


@router.post("/{session_id}/summary/narrative", response_model=SummaryResponse)
async def generate_narrative_summary(
    session_id: str,
    body: NarrativeRequest,
    request: Request,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> SummaryResponse:
    """Generate an LLM-powered narrative for a session summary."""
    check_feature(ctx, "summary_narrative")

    stmt = select(Session).where(Session.id == session_id, Session.user_id == user.id)
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")

    # Get or generate the deterministic summary first
    cached = await db.execute(
        select(SessionSummaryRecord).where(SessionSummaryRecord.session_id == session_id)
    )
    existing = cached.scalar_one_or_none()

    summary = await _generate_summary(session, request)
    if summary is None:
        raise HTTPException(422, "Could not generate summary — no messages found")

    # Extract messages for narrative prompt
    messages = await _extract_messages(session, request)

    # Resolve LLM API key — prefer request body, fall back to saved judge settings
    api_key = body.llm_api_key
    model = body.model or "claude-sonnet-4"
    provider = body.provider
    base_url = body.base_url

    if not api_key:
        # Try saved judge settings (same pattern as audit route)
        import base64
        import hashlib
        import os

        from cryptography.fernet import Fernet

        from sessionfs.server.db.models import UserJudgeSettings

        judge_stmt = select(UserJudgeSettings).where(UserJudgeSettings.user_id == user.id)
        judge_result = await db.execute(judge_stmt)
        judge_settings = judge_result.scalar_one_or_none()
        if judge_settings and judge_settings.encrypted_api_key:
            secret = os.environ.get("SFS_VERIFICATION_SECRET", "dev-secret")
            key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
            fernet = Fernet(key)
            api_key = fernet.decrypt(judge_settings.encrypted_api_key.encode()).decode()
            if not body.model and judge_settings.model:
                model = judge_settings.model
            if not body.provider and judge_settings.provider:
                provider = judge_settings.provider
            if not body.base_url and judge_settings.base_url:
                base_url = judge_settings.base_url

    if not api_key:
        raise HTTPException(400, "No API key provided — set judge settings or pass llm_api_key")

    from sessionfs.server.services.summarizer import generate_narrative

    summary = await generate_narrative(
        summary=summary,
        messages=messages,
        model=model,
        api_key=api_key,
        provider=provider,
        base_url=base_url,
    )

    # Update the DB record with narrative fields. v0.10.7 R2 — also
    # refresh personas_active so regenerations don't leave the cached
    # value stale relative to the latest manifest/messages scan.
    if existing:
        existing.what_happened = summary.what_happened
        existing.key_decisions = json.dumps(summary.key_decisions) if summary.key_decisions else None
        existing.outcome = summary.outcome
        existing.open_issues = json.dumps(summary.open_issues) if summary.open_issues else None
        existing.narrative_model = summary.narrative_model
        existing.personas_active = json.dumps(summary.personas_active or [])
    else:
        record = _summary_to_record(session_id, summary)
        db.add(record)
    await db.commit()

    return _summary_to_response(summary, session)


async def _extract_messages(session: Session, request: Request) -> list[dict]:
    """Extract raw messages from session blob."""
    blob_store: BlobStore = request.app.state.blob_store
    data = await blob_store.get(session.blob_key) if session.blob_key else None
    if not data:
        return []

    messages: list[dict] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                f = tar.extractfile(member)
                if not f:
                    continue
                if member.name.endswith("messages.jsonl"):
                    content = f.read().decode("utf-8", errors="replace")
                    for line in content.splitlines():
                        line = line.strip()
                        if line:
                            messages.append(json.loads(line))
    except Exception:
        logger.warning("Failed to extract messages for narrative generation")

    return messages


# ---- Batch endpoint for reporting ----

batch_router = APIRouter(prefix="/api/v1/summaries", tags=["summaries"])


@batch_router.get("")
async def get_batch_summaries(
    since: str = Query(None, description="ISO date (YYYY-MM-DD)"),
    until: str = Query(None, description="ISO date (YYYY-MM-DD)"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Get summaries for sessions in a date range."""
    from sqlalchemy import and_

    conditions = [Session.user_id == user.id, Session.is_deleted == False]  # noqa: E712
    if since:
        conditions.append(Session.created_at >= since)
    if until:
        conditions.append(Session.created_at <= until + "T23:59:59")

    stmt = (
        select(Session, SessionSummaryRecord)
        .outerjoin(SessionSummaryRecord, SessionSummaryRecord.session_id == Session.id)
        .where(and_(*conditions))
        .order_by(Session.created_at.desc())
        .limit(100)
    )
    result = await db.execute(stmt)
    rows = result.all()

    summaries = []
    for session, record in rows:
        entry = {
            "session_id": session.id,
            "title": session.title,
            "tool": session.source_tool,
            "message_count": session.message_count,
            "created_at": session.created_at.isoformat() if session.created_at else "",
        }
        if record:
            entry.update({
                "tool_call_count": record.tool_call_count,
                "files_modified": json.loads(record.files_modified),
                "commands_executed": record.commands_executed,
                "tests_run": record.tests_run,
                "tests_passed": record.tests_passed,
                "tests_failed": record.tests_failed,
            })
        summaries.append(entry)

    return summaries


async def _generate_summary(session: Session, request: Request):
    """Extract messages from blob and run deterministic summarizer."""
    from sessionfs.server.services.summarizer import summarize_session

    blob_store: BlobStore = request.app.state.blob_store
    data = await blob_store.get(session.blob_key) if session.blob_key else None
    if not data:
        return None

    messages: list[dict] = []
    manifest: dict = {}
    workspace: dict = {}

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
        logger.warning("Failed to extract session archive for summary")
        return None

    if not messages:
        return None

    return summarize_session(messages, manifest, workspace)


def _summary_to_record(session_id: str, summary) -> SessionSummaryRecord:
    return SessionSummaryRecord(
        session_id=session_id,
        duration_minutes=summary.duration_minutes,
        tool_call_count=summary.tool_call_count,
        files_modified=json.dumps(summary.files_modified),
        files_read=json.dumps(summary.files_read),
        commands_executed=summary.commands_executed,
        tests_run=summary.tests_run,
        tests_passed=summary.tests_passed,
        tests_failed=summary.tests_failed,
        packages_installed=json.dumps(summary.packages_installed),
        errors_encountered=json.dumps(summary.errors_encountered),
        what_happened=summary.what_happened,
        key_decisions=json.dumps(summary.key_decisions) if summary.key_decisions else None,
        outcome=summary.outcome,
        open_issues=json.dumps(summary.open_issues) if summary.open_issues else None,
        narrative_model=summary.narrative_model,
        personas_active=json.dumps(summary.personas_active or []),
    )


def _record_to_response(record: SessionSummaryRecord, session: Session) -> SummaryResponse:
    try:
        personas_active = json.loads(record.personas_active or "[]")
        if not isinstance(personas_active, list):
            personas_active = []
    except (json.JSONDecodeError, TypeError):
        personas_active = []
    return SummaryResponse(
        session_id=session.id,
        title=session.title or "Untitled",
        tool=session.source_tool or "",
        model=session.model_id,
        duration_minutes=record.duration_minutes or 0,
        message_count=session.message_count or 0,
        tool_call_count=record.tool_call_count,
        files_modified=json.loads(record.files_modified),
        files_read=json.loads(record.files_read),
        commands_executed=record.commands_executed,
        tests_run=record.tests_run,
        tests_passed=record.tests_passed,
        tests_failed=record.tests_failed,
        packages_installed=json.loads(record.packages_installed),
        errors_encountered=json.loads(record.errors_encountered),
        what_happened=record.what_happened,
        key_decisions=json.loads(record.key_decisions) if record.key_decisions else None,
        outcome=record.outcome,
        open_issues=json.loads(record.open_issues) if record.open_issues else None,
        narrative_model=record.narrative_model,
        personas_active=personas_active,
        generated_at=record.created_at.isoformat() if record.created_at else "",
    )


def _summary_to_response(summary, session: Session) -> SummaryResponse:
    return SummaryResponse(
        session_id=session.id,
        title=session.title or "Untitled",
        tool=session.source_tool or "",
        model=session.model_id,
        duration_minutes=summary.duration_minutes,
        message_count=session.message_count or 0,
        tool_call_count=summary.tool_call_count,
        files_modified=summary.files_modified,
        files_read=summary.files_read,
        commands_executed=summary.commands_executed,
        tests_run=summary.tests_run,
        tests_passed=summary.tests_passed,
        tests_failed=summary.tests_failed,
        packages_installed=summary.packages_installed,
        errors_encountered=summary.errors_encountered,
        what_happened=summary.what_happened,
        key_decisions=summary.key_decisions,
        outcome=summary.outcome,
        open_issues=summary.open_issues,
        narrative_model=summary.narrative_model,
        personas_active=list(summary.personas_active or []),
        generated_at=summary.generated_at,
    )

"""Session summary routes."""

from __future__ import annotations

import io
import json
import logging
import tarfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import Session, SessionSummaryRecord, User
from sessionfs.server.storage.base import BlobStore

logger = logging.getLogger("sessionfs.api")

router = APIRouter(prefix="/api/v1/sessions", tags=["summaries"])


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
    db: AsyncSession = Depends(get_db),
) -> SummaryResponse:
    """Generate or regenerate a session summary."""
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
                     "packages_installed", "errors_encountered"):
            setattr(existing, col, getattr(record, col))
        existing.created_at = datetime.now(timezone.utc)
    else:
        db.add(record)
    await db.commit()

    return _summary_to_response(summary, session)


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
    )


def _record_to_response(record: SessionSummaryRecord, session: Session) -> SummaryResponse:
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
        generated_at=summary.generated_at,
    )

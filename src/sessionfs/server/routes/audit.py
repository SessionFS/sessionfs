"""Audit routes — LLM-as-a-Judge for session verification."""

from __future__ import annotations

import io
import json
import logging
import tarfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import Session, User
from sessionfs.server.storage.base import BlobStore

logger = logging.getLogger("sessionfs.server.routes.audit")

router = APIRouter(prefix="/api/v1/sessions", tags=["audit"])


class AuditRequest(BaseModel):
    model: str | None = None
    provider: str | None = None
    llm_api_key: str | None = None
    base_url: str | None = None


class AuditFinding(BaseModel):
    message_index: int
    claim: str
    verdict: str
    severity: str
    evidence: str
    explanation: str


class AuditSummaryResponse(BaseModel):
    total_claims: int
    verified: int
    unverified: int
    hallucinations: int
    trust_score: float
    major_findings: int
    moderate_findings: int
    minor_findings: int


class AuditResponse(BaseModel):
    session_id: str
    model: str
    timestamp: str
    findings: list[AuditFinding]
    summary: AuditSummaryResponse


def _get_blob_store(request: Request) -> BlobStore:
    return request.app.state.blob_store


async def _get_session_or_404(
    session_id: str,
    user: User,
    db: AsyncSession,
) -> Session:
    """Fetch a session owned by the user, or raise 404."""
    stmt = select(Session).where(
        Session.id == session_id,
        Session.user_id == user.id,
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


async def _extract_messages_from_archive(
    blob_store: BlobStore,
    blob_key: str,
) -> list[dict]:
    """Download session archive and extract messages.jsonl."""
    data = await blob_store.get(blob_key)
    if data is None:
        return []

    messages: list[dict] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("messages.jsonl"):
                    f = tar.extractfile(member)
                    if f is not None:
                        for line in f.read().decode("utf-8", errors="replace").splitlines():
                            line = line.strip()
                            if line:
                                messages.append(json.loads(line))
                    break
    except (tarfile.TarError, json.JSONDecodeError) as exc:
        logger.warning("Failed to extract messages from archive: %s", exc)

    return messages


def _report_to_response(report) -> AuditResponse:
    """Convert a JudgeReport to an API response."""
    from dataclasses import asdict

    data = asdict(report)
    return AuditResponse(**data)


import asyncio as _asyncio

# Track running audit jobs: session_id -> task
_audit_jobs: dict[str, _asyncio.Task] = {}
# Store completed results: session_id -> AuditResponse (temp cache until stored in blob)
_audit_results: dict[str, dict] = {}


@router.get("/{session_id}/audit/status")
async def audit_status(
    session_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if an audit is running, completed, or not started."""
    await _get_session_or_404(session_id, user, db)

    if session_id in _audit_jobs and not _audit_jobs[session_id].done():
        return {"status": "running", "session_id": session_id}

    if session_id in _audit_results:
        return {"status": "completed", "session_id": session_id}

    # Check blob store for stored report
    blob_store = _get_blob_store(request)
    report_key = f"sessions/{user.id}/{session_id}_audit.json"
    try:
        data = await blob_store.get(report_key)
        if data:
            return {"status": "completed", "session_id": session_id}
    except Exception:
        pass

    return {"status": "not_started", "session_id": session_id}


@router.post("/{session_id}/audit")
async def run_audit(
    session_id: str,
    body: AuditRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run LLM-as-a-Judge audit on a session.

    For large sessions, returns 202 Accepted and runs in background.
    For small sessions (<500 messages), runs synchronously.
    The llm_api_key is used for the single LLM call and is NEVER logged
    or persisted.
    """
    session = await _get_session_or_404(session_id, user, db)
    blob_store = _get_blob_store(request)

    # Resolve API key: explicit param > stored settings > error
    llm_api_key = body.llm_api_key
    model = body.model or "claude-sonnet-4"
    provider = body.provider
    base_url = body.base_url

    if not llm_api_key:
        from sessionfs.server.db.models import UserJudgeSettings

        stmt = select(UserJudgeSettings).where(UserJudgeSettings.user_id == user.id)
        result = await db.execute(stmt)
        settings = result.scalar_one_or_none()
        if settings:
            import base64
            import hashlib
            import os

            from cryptography.fernet import Fernet

            secret = os.environ.get("SFS_VERIFICATION_SECRET", "dev-secret")
            key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
            fernet = Fernet(key)
            llm_api_key = fernet.decrypt(settings.encrypted_api_key.encode()).decode()
            if not body.model:
                model = settings.model
            if not provider:
                provider = settings.provider
            if not base_url:
                base_url = settings.base_url
        else:
            raise HTTPException(
                status_code=400,
                detail="No API key configured. Provide llm_api_key in request or configure judge settings.",
            )

    # Fall back to deployment-wide env var
    if not base_url:
        import os
        base_url = os.environ.get("SFS_JUDGE_BASE_URL") or None

    # Extract messages from the stored archive
    messages = await _extract_messages_from_archive(blob_store, session.blob_key)
    if not messages:
        raise HTTPException(
            status_code=400,
            detail="No messages found in session archive",
        )

    # For large sessions (500+ messages), run in background
    BACKGROUND_THRESHOLD = 500
    if len(messages) >= BACKGROUND_THRESHOLD:
        # Launch background task and return 202
        async def _run_audit_background():
            try:
                report = await _run_judge_pipeline(
                    session_id, messages, model, llm_api_key, provider, base_url,
                )
                report_key = f"sessions/{user.id}/{session_id}_audit.json"
                from dataclasses import asdict
                report_data = json.dumps(asdict(report), indent=2).encode("utf-8")
                await blob_store.put(report_key, report_data)
                _audit_results[session_id] = asdict(report)
                logger.info("Background audit completed for %s", session_id)
            except Exception as exc:
                logger.error("Background audit failed for %s: %s", session_id, exc)
                _audit_results[session_id] = {"error": str(exc)}
            finally:
                _audit_jobs.pop(session_id, None)

        task = _asyncio.create_task(_run_audit_background())
        _audit_jobs[session_id] = task

        return JSONResponse(
            status_code=202,
            content={
                "status": "accepted",
                "session_id": session_id,
                "message": f"Audit started in background ({len(messages)} messages). Poll GET /audit/status to check progress.",
            },
        )

    # Small sessions — run synchronously
    report = await _run_judge_pipeline(
        session_id, messages, model, llm_api_key, provider, base_url,
    )

    # Store the report
    report_key = f"sessions/{user.id}/{session_id}_audit.json"
    from dataclasses import asdict
    report_data = json.dumps(asdict(report), indent=2).encode("utf-8")
    await blob_store.put(report_key, report_data)

    return _report_to_response(report)


async def _run_judge_pipeline(
    session_id: str,
    messages: list[dict],
    model: str,
    llm_api_key: str,
    provider: str | None,
    base_url: str | None = None,
):
    """Run the judge pipeline — extracted for sync and background use."""
    from sessionfs.judge.evidence import gather_evidence
    from sessionfs.judge.extractor import extract_claims
    from sessionfs.judge.judge import (
        JUDGE_SYSTEM_PROMPT,
        _compute_summary,
        _deduplicate_findings,
        _parse_judge_response,
        build_judge_prompt,
        chunk_messages,
    )
    from sessionfs.judge.providers import call_llm
    from sessionfs.judge.report import Finding, JudgeReport

    all_claims = extract_claims(messages)
    all_evidence = gather_evidence(messages)

    if not all_claims:
        return JudgeReport(
            session_id=session_id,
            model=model,
            timestamp=datetime.now(timezone.utc).isoformat(),
            findings=[],
            summary=_compute_summary([]),
        )

    msg_count = len(messages)
    window = 100 if msg_count > 500 else 50
    chunks = chunk_messages(messages, window_size=window, overlap=10)

    MAX_CHUNKS = 10
    claim_indices = {c.message_index for c in all_claims}
    scored_chunks = []
    for chunk in chunks:
        start = messages.index(chunk[0]) if chunk else 0
        end = start + len(chunk)
        claims_in_chunk = sum(1 for ci in claim_indices if start <= ci < end)
        if claims_in_chunk > 0:
            scored_chunks.append((claims_in_chunk, chunk))
    scored_chunks.sort(key=lambda x: x[0], reverse=True)
    chunks = [c for _, c in scored_chunks[:MAX_CHUNKS]]

    all_findings: list[Finding] = []

    for chunk in chunks:
        chunk_start = messages.index(chunk[0]) if chunk else 0
        chunk_end = chunk_start + len(chunk)
        chunk_claims = [c for c in all_claims if chunk_start <= c.message_index < chunk_end]
        chunk_evidence = [e for e in all_evidence if chunk_start <= e.message_index < chunk_end]

        if not chunk_claims:
            continue

        prompt = build_judge_prompt(chunk_claims, chunk_evidence, messages)
        try:
            response = await call_llm(
                model=model, system=JUDGE_SYSTEM_PROMPT, prompt=prompt,
                api_key=llm_api_key, provider=provider, base_url=base_url,
            )
        except Exception:
            continue  # Skip failed chunks, don't crash the whole audit

        findings = _parse_judge_response(response, chunk_claims)
        all_findings.extend(findings)

    all_findings = _deduplicate_findings(all_findings)
    return JudgeReport(
        session_id=session_id,
        model=model,
        timestamp=datetime.now(timezone.utc).isoformat(),
        findings=all_findings,
        summary=_compute_summary(all_findings),
    )


@router.get("/{session_id}/audit", response_model=AuditResponse)
async def get_audit(
    session_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AuditResponse:
    """Return stored audit report if it exists."""
    # Verify session ownership
    await _get_session_or_404(session_id, user, db)
    blob_store = _get_blob_store(request)

    report_key = f"sessions/{user.id}/{session_id}_audit.json"
    data = await blob_store.get(report_key)
    if data is None:
        raise HTTPException(status_code=404, detail="No audit report found for this session")

    try:
        report_data = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=500, detail="Stored audit report is corrupted")

    return AuditResponse(**report_data)

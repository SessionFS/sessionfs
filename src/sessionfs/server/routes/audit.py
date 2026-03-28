"""Audit routes — LLM-as-a-Judge for session verification."""

from __future__ import annotations

import asyncio as _asyncio
import io
import json
import logging
import tarfile
from datetime import datetime, timezone

import httpx
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
    category: str = "other"


class AuditSummaryResponse(BaseModel):
    total_claims: int
    verified: int
    unverified: int
    hallucinations: int
    trust_score: float
    major_findings: int
    moderate_findings: int
    minor_findings: int
    critical_count: int = 0
    high_count: int = 0
    low_count: int = 0


class AuditResponse(BaseModel):
    session_id: str
    model: str
    timestamp: str
    provider: str = ""
    base_url: str = ""
    execution_time_ms: int = 0
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

    # Fall back to deployment-wide env var
    if not base_url:
        import os
        base_url = os.environ.get("SFS_JUDGE_BASE_URL") or None

    # Require either an API key or a base URL (some endpoints like Ollama need no key)
    if not llm_api_key and not base_url:
        raise HTTPException(
            status_code=400,
            detail="No API key or base URL configured. Provide llm_api_key, base_url, or configure judge settings.",
        )

    # Extract messages from the stored archive
    messages = await _extract_messages_from_archive(blob_store, session.blob_key)
    if not messages:
        raise HTTPException(
            status_code=400,
            detail="No messages found in session archive",
        )

    # Count tool calls for diagnostics
    tool_call_count = sum(
        1 for m in messages
        if isinstance(m.get("content", []), list)
        and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in m.get("content", []))
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
    try:
        report = await _run_judge_pipeline(
            session_id, messages, model, llm_api_key, provider, base_url,
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "no_claims_extracted",
                "message": str(exc),
                "session_stats": {
                    "tool_calls": tool_call_count,
                    "messages": len(messages),
                },
            },
        )
    except httpx.HTTPStatusError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "error": "llm_request_failed",
                "message": f"Judge LLM returned {exc.response.status_code}",
                "detail": exc.response.text[:500],
            },
        )
    except httpx.TimeoutException:
        return JSONResponse(
            status_code=504,
            content={
                "error": "llm_timeout",
                "message": "Judge LLM timed out after 120s. Try a smaller session or faster model.",
            },
        )

    # Store in blob storage
    report_key = f"sessions/{user.id}/{session_id}_audit.json"
    from dataclasses import asdict
    report_data = json.dumps(asdict(report), indent=2).encode("utf-8")
    await blob_store.put(report_key, report_data)

    # Store in database for history
    await _store_audit_report(db, report, user.id, provider or "", base_url or "")

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
        # Check if session has tool calls — if so, this is a parser failure
        tool_uses = sum(
            1 for m in messages
            if isinstance(m.get("content", []), list)
            and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in m.get("content", []))
        )
        if tool_uses > 0:
            raise ValueError(
                f"Could not extract claims from session with {tool_uses} tool calls and "
                f"{len(messages)} messages. The claim parser may not support this session format."
            )
        # No tool calls — genuinely nothing to audit
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


async def _store_audit_report(
    db: AsyncSession,
    report,  # JudgeReport
    user_id: str,
    provider: str,
    base_url: str,
) -> None:
    """Persist audit report to database for history tracking."""
    import uuid

    from sessionfs.server.db.models import AuditReport as AuditReportModel

    from dataclasses import asdict

    audit_id = f"aud_{uuid.uuid4().hex[:16]}"
    findings_json = json.dumps([asdict(f) for f in report.findings])

    record = AuditReportModel(
        id=audit_id,
        session_id=report.session_id,
        user_id=user_id,
        judge_model=report.model,
        judge_provider=provider,
        judge_base_url=base_url or None,
        trust_score=int(report.summary.trust_score * 100),
        total_claims=report.summary.total_claims,
        verified_count=report.summary.verified,
        unverified_count=report.summary.unverified,
        contradiction_count=report.summary.hallucinations,
        findings=findings_json,
        execution_time_ms=getattr(report, "execution_time_ms", None),
    )
    db.add(record)
    await db.commit()


# ---- Audit History ----


class AuditHistoryEntry(BaseModel):
    id: str
    session_id: str
    judge_model: str
    judge_provider: str
    trust_score: int
    total_claims: int
    contradiction_count: int
    execution_time_ms: int | None
    created_at: str


@router.get("/{session_id}/audits")
async def get_audit_history(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AuditHistoryEntry]:
    """Get all audit reports for a session (history)."""
    from sessionfs.server.db.models import AuditReport as AuditReportModel

    await _get_session_or_404(session_id, user, db)

    stmt = (
        select(AuditReportModel)
        .where(AuditReportModel.session_id == session_id, AuditReportModel.user_id == user.id)
        .order_by(AuditReportModel.created_at.desc())
    )
    result = await db.execute(stmt)
    reports = result.scalars().all()

    return [
        AuditHistoryEntry(
            id=r.id,
            session_id=r.session_id,
            judge_model=r.judge_model,
            judge_provider=r.judge_provider,
            trust_score=r.trust_score,
            total_claims=r.total_claims,
            contradiction_count=r.contradiction_count,
            execution_time_ms=r.execution_time_ms,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in reports
    ]


@router.get("/audits/{audit_id}")
async def get_audit_by_id(
    audit_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get a specific historical audit report."""
    from sessionfs.server.db.models import AuditReport as AuditReportModel

    stmt = select(AuditReportModel).where(
        AuditReportModel.id == audit_id, AuditReportModel.user_id == user.id
    )
    result = await db.execute(stmt)
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, "Audit report not found")

    findings = json.loads(report.findings) if report.findings else []

    return {
        "id": report.id,
        "session_id": report.session_id,
        "model": report.judge_model,
        "provider": report.judge_provider,
        "base_url": report.judge_base_url,
        "trust_score": report.trust_score / 100.0,
        "total_claims": report.total_claims,
        "verified": report.verified_count,
        "unverified": report.unverified_count,
        "hallucinations": report.contradiction_count,
        "findings": findings,
        "execution_time_ms": report.execution_time_ms,
        "created_at": report.created_at.isoformat() if report.created_at else "",
    }

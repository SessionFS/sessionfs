"""Integration tests for audit finding dismiss/confirm."""

from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Session, User


@pytest.fixture
async def audited_session(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes,
    db_session: AsyncSession, test_user: User, blob_store,
) -> str:
    """Push a session and store a fake audit report."""
    session_id = f"ses_auditdismiss{uuid.uuid4().hex[:6]}"

    # Push session
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201

    # Store a fake audit report in blob storage
    report = {
        "session_id": session_id,
        "model": "test-model",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": "test",
        "base_url": "",
        "execution_time_ms": 500,
        "findings": [
            {
                "message_index": 0,
                "claim": "Tests pass with exit code 0",
                "verdict": "hallucination",
                "severity": "critical",
                "evidence": "Exit code was 1",
                "explanation": "Test output shows failures",
                "category": "test_result",
                "confidence": 92,
                "cwe_id": "CWE-393",
                "evidence_snippets": [{"source": "tool_result", "message_index": 2, "text": "exit code 1"}],
                "dismissed": False,
                "dismissed_by": "",
                "dismissed_reason": "",
            },
            {
                "message_index": 1,
                "claim": "File created successfully",
                "verdict": "verified",
                "severity": "high",
                "evidence": "File exists in output",
                "explanation": "Tool result confirms file creation",
                "category": "file_existence",
                "confidence": 88,
                "cwe_id": "CWE-552",
                "evidence_snippets": [],
                "dismissed": False,
                "dismissed_by": "",
                "dismissed_reason": "",
            },
        ],
        "summary": {
            "total_claims": 2,
            "verified": 1,
            "unverified": 0,
            "hallucinations": 1,
            "trust_score": 0.5,
            "major_findings": 1,
            "moderate_findings": 0,
            "minor_findings": 0,
            "critical_count": 1,
            "high_count": 0,
            "low_count": 0,
        },
    }
    report_key = f"sessions/{test_user.id}/{session_id}_audit.json"
    await blob_store.put(report_key, json.dumps(report).encode())

    return session_id


@pytest.mark.asyncio
async def test_dismiss_finding(client: AsyncClient, auth_headers: dict, audited_session: str):
    """Dismiss a finding marks it as dismissed."""
    resp = await client.post(
        f"/api/v1/sessions/{audited_session}/audit/dismiss",
        headers=auth_headers,
        json={"finding_index": 0, "dismissed": True, "reason": "False positive - test was in a different file"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["dismissed"] is True
    assert data["finding_index"] == 0

    # Verify the report was updated
    resp = await client.get(
        f"/api/v1/sessions/{audited_session}/audit",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    report = resp.json()
    assert report["findings"][0]["dismissed"] is True
    assert "False positive" in report["findings"][0]["dismissed_reason"]
    assert report["findings"][1]["dismissed"] is False


@pytest.mark.asyncio
async def test_undismiss_finding(client: AsyncClient, auth_headers: dict, audited_session: str):
    """Un-dismissing a finding clears the dismiss state."""
    # First dismiss
    await client.post(
        f"/api/v1/sessions/{audited_session}/audit/dismiss",
        headers=auth_headers,
        json={"finding_index": 0, "dismissed": True, "reason": "Mistake"},
    )

    # Then un-dismiss
    resp = await client.post(
        f"/api/v1/sessions/{audited_session}/audit/dismiss",
        headers=auth_headers,
        json={"finding_index": 0, "dismissed": False},
    )
    assert resp.status_code == 200

    resp = await client.get(
        f"/api/v1/sessions/{audited_session}/audit",
        headers=auth_headers,
    )
    report = resp.json()
    assert report["findings"][0]["dismissed"] is False


@pytest.mark.asyncio
async def test_dismiss_invalid_index(client: AsyncClient, auth_headers: dict, audited_session: str):
    """Invalid finding index returns 400."""
    resp = await client.post(
        f"/api/v1/sessions/{audited_session}/audit/dismiss",
        headers=auth_headers,
        json={"finding_index": 99, "dismissed": True},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_audit_report_includes_new_fields(client: AsyncClient, auth_headers: dict, audited_session: str):
    """Audit report includes confidence, CWE, and evidence snippets."""
    resp = await client.get(
        f"/api/v1/sessions/{audited_session}/audit",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    report = resp.json()

    finding = report["findings"][0]
    assert finding["confidence"] == 92
    assert finding["cwe_id"] == "CWE-393"
    assert len(finding["evidence_snippets"]) == 1
    assert finding["evidence_snippets"][0]["text"] == "exit code 1"

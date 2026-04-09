"""DLP (Data Loss Prevention) API routes.

Provides org-level DLP policy management, dry-run scanning, and statistics.
All routes gated by the ``dlp_secrets`` feature flag.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import Session
from sessionfs.server.dlp import (
    DEFAULT_DLP_POLICY,
    scan_dlp,
    validate_dlp_policy,
)
from sessionfs.server.tier_gate import (
    UserContext,
    check_feature,
    check_role,
    get_user_context,
)

logger = logging.getLogger("sessionfs.api")

router = APIRouter(prefix="/api/v1/dlp", tags=["dlp"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class DLPPolicyResponse(BaseModel):
    enabled: bool
    mode: str
    categories: list[str]
    custom_patterns: list[dict] = []
    allowlist: list[str] = []


class DLPPolicyUpdate(BaseModel):
    enabled: bool | None = None
    mode: str | None = None
    categories: list[str] | None = None
    custom_patterns: list[dict] | None = None
    allowlist: list[str] | None = None


class DLPScanRequest(BaseModel):
    text: str
    categories: list[str] = ["secrets"]


class DLPScanFinding(BaseModel):
    pattern_name: str
    category: str
    severity: str
    line_number: int


class DLPScanResponse(BaseModel):
    finding_count: int
    findings: list[DLPScanFinding]


class DLPStatsResponse(BaseModel):
    total_scanned: int
    by_action: dict[str, int]


# ---------------------------------------------------------------------------
# GET /api/v1/dlp/policy — get org DLP policy
# ---------------------------------------------------------------------------

@router.get("/policy", response_model=DLPPolicyResponse)
async def get_dlp_policy(
    ctx: UserContext = Depends(get_user_context),
) -> DLPPolicyResponse:
    """Get the organization's DLP policy. Requires org membership."""
    check_feature(ctx, "dlp_secrets")

    if not ctx.org:
        # Solo users get defaults
        return DLPPolicyResponse(**DEFAULT_DLP_POLICY)

    try:
        settings = json.loads(ctx.org.settings) if isinstance(ctx.org.settings, str) else ctx.org.settings
    except (json.JSONDecodeError, TypeError):
        settings = {}

    policy = settings.get("dlp", DEFAULT_DLP_POLICY)
    return DLPPolicyResponse(
        enabled=policy.get("enabled", False),
        mode=policy.get("mode", "warn"),
        categories=policy.get("categories", ["secrets"]),
    )


# ---------------------------------------------------------------------------
# PUT /api/v1/dlp/policy — update org DLP policy (admin only)
# ---------------------------------------------------------------------------

@router.put("/policy", response_model=DLPPolicyResponse)
async def update_dlp_policy(
    body: DLPPolicyUpdate,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> DLPPolicyResponse:
    """Update the organization's DLP policy. Admin only."""
    check_feature(ctx, "dlp_secrets")
    check_role(ctx, "admin")

    if not ctx.org:
        raise HTTPException(400, "DLP policy requires an organization")

    # Merge updates into existing policy (don't overwrite unset fields)
    try:
        settings = json.loads(ctx.org.settings) if isinstance(ctx.org.settings, str) else ctx.org.settings
    except (json.JSONDecodeError, TypeError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}

    existing_dlp = settings.get("dlp", {})
    if not isinstance(existing_dlp, dict):
        existing_dlp = {}

    # Only update fields that were explicitly provided
    updates = body.model_dump(exclude_none=True)
    merged = {**existing_dlp, **updates}

    # Validate the merged result
    try:
        validated = validate_dlp_policy(merged)
    except ValueError as e:
        raise HTTPException(422, str(e))

    settings["dlp"] = validated
    ctx.org.settings = json.dumps(settings)
    await db.commit()

    return DLPPolicyResponse(
        enabled=validated.get("enabled", False),
        mode=validated.get("mode", "warn"),
        categories=validated.get("categories", ["secrets"]),
        custom_patterns=validated.get("custom_patterns", []),
        allowlist=validated.get("allowlist", []),
    )


# ---------------------------------------------------------------------------
# POST /api/v1/dlp/scan — dry-run scan
# ---------------------------------------------------------------------------

@router.post("/scan", response_model=DLPScanResponse)
async def scan_text_endpoint(
    body: DLPScanRequest,
    ctx: UserContext = Depends(get_user_context),
) -> DLPScanResponse:
    """Dry-run DLP scan. Returns findings without matched text."""
    check_feature(ctx, "dlp_secrets")

    findings = scan_dlp(body.text, categories=body.categories)

    # Return findings WITHOUT match_text (security: don't echo secrets back)
    safe_findings = [
        DLPScanFinding(
            pattern_name=f.pattern_name,
            category=f.category,
            severity=f.severity,
            line_number=f.line_number,
        )
        for f in findings
    ]

    return DLPScanResponse(
        finding_count=len(safe_findings),
        findings=safe_findings,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/dlp/stats — scan statistics
# ---------------------------------------------------------------------------

@router.get("/stats", response_model=DLPStatsResponse)
async def get_dlp_stats(
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> DLPStatsResponse:
    """DLP scan statistics — sessions scanned, grouped by action taken."""
    check_feature(ctx, "dlp_secrets")

    if not ctx.org:
        return DLPStatsResponse(total_scanned=0, by_action={})

    # Count sessions with actual DLP scan results
    from sessionfs.server.db.models import OrgMember

    member_ids_stmt = select(OrgMember.user_id).where(OrgMember.org_id == ctx.org.id)
    result = await db.execute(member_ids_stmt)
    member_ids = [row[0] for row in result.all()]

    if not member_ids:
        return DLPStatsResponse(total_scanned=0, by_action={})

    # Get all sessions with DLP results
    scanned_result = await db.execute(
        select(Session.dlp_scan_results).where(
            Session.user_id.in_(member_ids),
            Session.is_deleted == False,  # noqa: E712
            Session.dlp_scan_results.isnot(None),
        )
    )
    rows = scanned_result.all()

    by_action: dict[str, int] = {}
    for (raw,) in rows:
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            action = data.get("action_taken", "unknown") if isinstance(data, dict) else "unknown"
        except (json.JSONDecodeError, TypeError):
            action = "unknown"
        by_action[action] = by_action.get(action, 0) + 1

    return DLPStatsResponse(
        total_scanned=len(rows),
        by_action=by_action,
    )

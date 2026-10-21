"""Marketplace instruction scanning: findings detail, rescans, rulesets (§5.8).

- ``GET /api/v1/marketplace/versions/{id}/scan``: the latest scan of a
  version with its value-free findings. Visible to anyone who can see the
  deliverable version, and to the people who moderate it (owner, org
  admins, platform admins) even while it is quarantined or blocked, so a
  publisher can see why.
- ``POST /api/v1/admin/skills/rescan``: platform-admin sweeper (Cloud
  Scheduler calls it on a cadence; it also catches up after a ruleset
  change). Bounded batch, time budget, resumable cursor. A new critical
  finding QUARANTINES the version and opens a system moderation report; it
  never blocks or uninstalls (D3).
- ``POST /api/v1/orgs/{org_id}/skills/rescan``: an org owner / admin rescans
  the versions of packages the ORG owns (user key only). Rate limited per org
  (a per-process burst limit and a durable daily cap counted from the org
  audit log). Each rescan is reserved atomically before it runs (org row
  lock; a ``skill_rescan`` event) and its outcome audited as
  ``skill_rescan_result``.
- ``GET /api/v1/admin/skills/scan-rulesets``: rulesets used so far plus the
  running ruleset's rule catalog.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    require_admin,
    require_platform_sweeper,
    require_scope,
    sweep_actor_details,
)
from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    AdminAction,
    OrgAuditEvent,
    OrgMember,
    Organization,
    SkillMarketplaceListing,
    SkillPackageVersion,
    SkillScanRuleset,
    User,
)
from sessionfs.server.routes.marketplace import _blob_store, _json, _visible_row
from sessionfs.server.roles import has_minimum_role
from sessionfs.server.routes.marketplace_moderation import (
    _is_platform_admin,
    _member_roles,
    _owns_listing,
    _require_user_key,
    _utc,
)
from sessionfs.server.services.skill_scanning import latest_scan, rescan_batch, scan_findings
from sessionfs.skills.instruction_scan import RULES, RULESET_VERSION

router = APIRouter(prefix="/api/v1", tags=["skill-marketplace-scanning"])


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


async def _scan_viewable_version(
    db: AsyncSession, auth: AuthContext, version_id: str
) -> tuple[SkillPackageVersion, bool]:
    """(version, caller_moderates). Constant 404 for anything else."""
    try:
        _, _, version = await _visible_row(db, auth, version_id=version_id)
    except HTTPException as exc:
        if exc.status_code != 404 or auth.key_kind != "user":
            raise
        version = None
    listings = list(
        (
            await db.execute(
                select(SkillMarketplaceListing).where(SkillMarketplaceListing.version_id == version_id)
            )
        )
        .scalars()
        .all()
    )
    moderates = False
    if auth.key_kind == "user" and listings:
        roles = await _member_roles(db, auth)
        # Platform admins, and the owners of the version's non-public
        # listings (an org's owners/admins, a personal package's owner).
        moderates = _is_platform_admin(auth) or any(
            _owns_listing(auth, listing, roles) for listing in listings
        )
    if version is None:
        if not moderates:
            raise HTTPException(404, "Marketplace package not found")
        version = await db.get(SkillPackageVersion, version_id)
        if version is None:
            raise HTTPException(404, "Marketplace package not found")
    return version, moderates


@router.get("/marketplace/versions/{version_id}/scan")
async def get_version_scan(
    version_id: str,
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    version, moderates = await _scan_viewable_version(db, auth, version_id)
    scan = await latest_scan(db, version.id)
    findings = await scan_findings(db, scan.id) if scan is not None else []
    result: dict = {
        "version_id": version.id,
        "package_id": version.package_id,
        "content_hash": version.content_hash,
        "current_ruleset": RULESET_VERSION,
        "quarantined": version.quarantined_at is not None,
        "quarantined_at": _iso(version.quarantined_at),
        "summary": _json(version.scan_summary, {}),
        "scan": (
            {
                "id": scan.id,
                "ruleset": scan.ruleset_version,
                "scanned_at": _iso(scan.scanned_at),
                "trigger": scan.trigger,
                "max_severity": scan.max_severity,
                "finding_count": scan.finding_count,
                "new_finding_count": scan.new_finding_count,
                "truncated": scan.truncated,
            }
            if scan is not None
            else None
        ),
        "findings": [
            {
                "rule_id": row.rule_id,
                "family": row.family,
                "severity": row.severity,
                "path": row.file_path,
                "line": row.line,
                "column": row.col,
                "message": row.message,
                "line_sha": row.line_sha,
                "suppressed": row.suppressed,
                "suppression_justification": row.suppression_justification,
                "first_seen_scan_id": row.first_seen_scan_id,
            }
            for row in findings
        ],
    }
    if moderates:
        result["quarantine_reason"] = version.quarantine_reason
    return result


class RescanRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)
    cursor: str | None = Field(default=None, min_length=1, max_length=64)
    force: bool = False
    # Report-only rollout mode (§6 step 2): record findings and open reports,
    # but do not quarantine.
    report_only: bool = False
    time_budget_seconds: float = Field(default=20.0, ge=1.0, le=120.0)


@router.post("/admin/skills/rescan")
async def rescan_skill_versions(
    request: Request,
    body: RescanRequest | None = None,
    auth: AuthContext = Depends(require_platform_sweeper()),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Rescan live versions that are due under the current ruleset.
    Platform-admin user key or a ``platform_sweeps:run`` service key.

    Safe to call repeatedly (Cloud Scheduler): a scanned version is not due
    again until the ruleset changes or a week passes. Pass back
    ``next_cursor`` with ``force=true`` to page a full forced pass. Versions
    that could not be scanned are listed in ``skipped``; ``done`` is only
    true for a batch that scanned everything it covered."""
    body = body or RescanRequest()
    if auth.key_kind == "service" and (body.report_only or body.force):
        # A forced report-only pass would stamp every version as scanned
        # without quarantining, so the enforcing sweep would skip criticals
        # for a week. Rollout and forced passes stay with an admin user key.
        raise HTTPException(
            status_code=403,
            detail={
                "error": "sweep_override_not_allowed",
                "message": "report_only and force need a platform-admin user key.",
            },
        )
    result = await rescan_batch(
        db,
        _blob_store(request),
        limit=body.limit,
        cursor=body.cursor,
        force=body.force,
        enforce=not body.report_only,
        time_budget_seconds=body.time_budget_seconds,
    )
    db.add(
        AdminAction(
            id=str(uuid.uuid4()),
            admin_id=auth.user.id,
            action="skill_rescan",
            target_type="skill_version",  # admin_actions.target_type is VARCHAR(20)
            target_id="bulk",
            details=json.dumps(
                {
                    "ruleset": result["ruleset"],
                    "scanned": result["scanned"],
                    "quarantined": result["quarantined"],
                    "reported": result["reported"],
                    "skipped": result["skipped"],
                    "force": body.force,
                    "report_only": body.report_only,
                    **sweep_actor_details(auth),
                },
                sort_keys=True,
            ),
        )
    )
    await db.commit()
    return result


# Org rescans (§5.9: "org rescans limited to own packages and rate-limited").
ORG_RESCAN_BURST = 3  # per org per minute, per process
ORG_RESCANS_PER_DAY = 24  # durable, counted from the org audit log
_org_rescan_limiter = SlidingWindowRateLimiter(max_requests=ORG_RESCAN_BURST, window_seconds=60.0)


class OrgRescanRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=64)
    force: bool = False
    time_budget_seconds: float = Field(default=20.0, ge=1.0, le=60.0)


def _org_rescan_limited() -> HTTPException:
    return HTTPException(
        429,
        detail={"code": "rescan_rate_limited", "message": "Too many rescans for this organization. Try again later."},
    )


@router.post("/orgs/{org_id}/skills/rescan")
async def rescan_org_skill_versions(
    org_id: str,
    request: Request,
    body: OrgRescanRequest | None = None,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Rescan the versions of packages this org owns (owner / admin only).

    The same engine and rules as the platform sweeper: a critical finding
    quarantines the version and opens a system report (never a block). Not
    the org's packages, not an admin, or not a member: the same 404."""
    _require_user_key(auth)
    role = (
        await db.execute(
            select(OrgMember.role).where(OrgMember.org_id == org_id, OrgMember.user_id == auth.user.id)
        )
    ).scalar_one_or_none()
    org = await db.get(Organization, org_id) if role is not None else None
    if org is None or role is None or not has_minimum_role(role, "admin"):
        raise HTTPException(404, "Organization not found")
    if not _org_rescan_limiter.is_allowed(f"org-rescan:{org_id}"):
        raise _org_rescan_limited()
    body = body or OrgRescanRequest()
    org_name = org.name
    actor_email = auth.user.email
    actor_user_id = auth.user.id
    # Reserve one of today's rescans BEFORE scanning and commit it, so
    # concurrent requests cannot all pass the daily check and then scan.
    event = await reserve_org_rescan(
        db, org_id, org_name=org_name, actor_user_id=actor_user_id,
        actor_email=actor_email, actor_role=role, force=body.force,
    )
    if event is None:
        raise _org_rescan_limited()
    event_id = event.id
    await db.commit()
    result = await rescan_batch(
        db,
        _blob_store(request),
        limit=body.limit,
        cursor=body.cursor,
        force=body.force,
        enforce=True,
        time_budget_seconds=body.time_budget_seconds,
        org_id=org_id,
    )
    # The audit log is append-only: the outcome is its own event, pointing
    # at the reservation (only ``skill_rescan`` events count toward the cap).
    db.add(
        OrgAuditEvent(
            id=f"oae_{secrets.token_hex(12)}",
            org_id=org_id,
            org_name_snapshot=org_name,
            event_type="skill_rescan_result",
            actor_user_id=actor_user_id,
            actor_email_snapshot=actor_email,
            actor_role_at_time=role,
            target_type="skill_package_version",
            target_id="bulk",
            after=json.dumps(
                {
                    "reservation_event_id": event_id,
                    "ruleset": result["ruleset"],
                    "scanned": result["scanned"],
                    "quarantined": result["quarantined"],
                    "skipped": result["skipped"],
                    "force": body.force,
                },
                sort_keys=True,
            ),
        )
    )
    await db.commit()
    return result


async def reserve_org_rescan(
    db: AsyncSession,
    org_id: str,
    *,
    org_name: str,
    actor_user_id: str | None,
    actor_email: str | None,
    actor_role: str,
    force: bool,
) -> OrgAuditEvent | None:
    """Reserve one of the org's ORG_RESCANS_PER_DAY rescans; None when the
    day's are used up.

    The org row is locked (SELECT ... FOR UPDATE) while the last day's
    ``skill_rescan`` events are counted and the reservation event is
    inserted, so concurrent requests serialize: each one counts every
    reservation committed before it. The caller commits (releasing the lock)
    before scanning. A reservation stays used even if the scan then fails
    (fail closed). The outcome is recorded as a separate ``skill_rescan_result`` event
    (the audit log is append-only)."""
    await db.execute(select(Organization.id).where(Organization.id == org_id).with_for_update())
    since = datetime.now(timezone.utc) - timedelta(days=1)
    recent = (
        await db.execute(
            select(func.count(OrgAuditEvent.id)).where(
                OrgAuditEvent.org_id == org_id,
                OrgAuditEvent.event_type == "skill_rescan",
                OrgAuditEvent.created_at > since,
            )
        )
    ).scalar_one()
    if recent >= ORG_RESCANS_PER_DAY:
        await db.rollback()
        return None
    event = OrgAuditEvent(
        id=f"oae_{secrets.token_hex(12)}",
        org_id=org_id,
        org_name_snapshot=org_name,
        event_type="skill_rescan",
        actor_user_id=actor_user_id,
        actor_email_snapshot=actor_email,
        actor_role_at_time=actor_role,
        target_type="skill_package_version",
        target_id="bulk",
        after=json.dumps({"status": "started", "force": force}, sort_keys=True),
    )
    db.add(event)
    await db.flush()
    return event


@router.get("/admin/skills/scan-rulesets")
async def list_scan_rulesets(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = (
        await db.execute(select(SkillScanRuleset).order_by(SkillScanRuleset.released_at.desc()))
    ).scalars().all()
    return {
        "current": RULESET_VERSION,
        "rulesets": [
            {
                "version": row.version,
                "released_at": _iso(row.released_at),
                "rule_count": row.rule_count,
                "notes": row.notes,
            }
            for row in rows
        ],
        "rules": [
            {"rule_id": rule.rule_id, "family": rule.family, "explanation": rule.explanation}
            for rule in RULES.values()
        ],
    }

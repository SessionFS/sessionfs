"""Org skills policy: revisions, compiled per-tool settings, device
compliance (marketplace v2 §8.6). Enterprise only (``skills_policy``, D6).

- ``GET  /api/v1/orgs/{org_id}/skills/policy``: the current revision (ETag).
- ``PUT  /api/v1/orgs/{org_id}/skills/policy``: a new immutable revision
  (``If-Match`` required; side effects listed in the compiled warnings must
  be acknowledged).
- ``POST /api/v1/orgs/{org_id}/skills/policy/preview``: validate and compile
  a draft without saving, with the per-tool changes against the current
  revision.
- ``POST /api/v1/orgs/{org_id}/skills/policy/disable``: publish a revision
  that restricts nothing (``If-Match`` required). Allowed at any tier.
- ``GET  /api/v1/orgs/{org_id}/skills/policy/revisions``: revision history.
- ``GET  /api/v1/orgs/{org_id}/skills/policy/compiled?tool=``: the current
  revision compiled into each tool's native settings, with per-field
  enforceable / report_only / unsupported lists.
- ``GET  /api/v1/orgs/{org_id}/skills/compliance``: the latest report of
  every device (owner / admin).
- ``GET  /api/v1/devices/me/policy``: the policies that apply to the caller
  (advisory reconcile input).
- ``POST /api/v1/devices/me/policy-report``: a device's compliance report.
  Advisory telemetry, never an access-control input. Storage is bounded:
  the latest report per (device, org) is upserted, plus at most one history
  row per (device, org, UTC day) kept 90 days; a user has at most
  ``MAX_DEVICES_PER_USER`` devices (a new one beyond that is refused, never
  evicting another device's record; devices unseen for 90 days are pruned).

Below Enterprise an org that already has a policy keeps it enforced; its
owners and admins can still read it and may only relax or disable it
(403 ``upgrade_required`` with the tightened fields otherwise). Members and
orgs that never had a policy get the usual ``upgrade_required``.

Signed MDM bundles / ``exports`` and the MCP Registry endpoint are
follow-ups (see docs/design/marketplace-v2.md §8 "as built").
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import case, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import AuthContext, require_scope
from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    DevicePolicyReport,
    DevicePolicyReportHistory,
    Organization,
    OrgMember,
    OrgPolicyCompilation,
    OrgSkillPolicy,
    SkillDevice,
    User,
)
from sessionfs.server.routes.marketplace import _audit, _check_org_feature, _org_access
from sessionfs.server.services import skill_policy
from sessionfs.server.services.policy_compiler import (
    COMPILER_VERSION,
    TOOLS,
    CompiledPolicy,
    compile_all,
    compile_policy,
)
from sessionfs.skills.models import validate_device_id
from sessionfs.skills.policy import (
    MAX_POLICY_BYTES,
    PolicyDocument,
    PolicyError,
    canonical_json,
    parse_policy,
    policy_hash,
    tightened_fields,
    unrestricted_from,
)

router = APIRouter(prefix="/api/v1", tags=["skill-policy"])

REPORT_RETENTION = timedelta(days=90)
MAX_REPORT_BYTES = 32 * 1024
MAX_REPORT_ORGS = 20
# Refuse (never evict) a user's device beyond this: evicting the oldest
# would let anyone holding the user's devices:report key erase another
# device's non-compliant record by reporting from fresh device ids.
MAX_DEVICES_PER_USER = 20
# Devices per page of the compliance view (summary counts cover all).
COMPLIANCE_PAGE_SIZE = 1000
_report_limiter = SlidingWindowRateLimiter(max_requests=30, window_seconds=3600)


def _etag(revision: int) -> str:
    return f'"osp-{revision}"'


def _policy_response(org: Organization, row: OrgSkillPolicy | None) -> dict[str, Any]:
    if row is None:
        return {
            "org_id": org.id,
            "revision": 0,
            "policy": None,
            "policy_hash": None,
            "created_at": None,
            "created_by_user_id": None,
            "valid": True,
        }
    document = skill_policy.document_of(row)
    return {
        "org_id": org.id,
        "revision": row.revision,
        "policy": document.model_dump(mode="json") if document else json.loads(row.policy_json),
        "policy_hash": row.policy_hash,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "created_by_user_id": row.created_by_user_id,
        "valid": document is not None,
    }


_ADMIN_ROLES = {"owner", "admin", "platform_admin"}


async def _policy_org(
    db: AsyncSession, auth: AuthContext, org_id: str, *, admin: bool = False
) -> tuple[Organization, str, bool]:
    """(org, role, entitled). Below the tier that includes the policy, only
    an owner or admin of an org that already has a policy gets through
    (to read it, relax it or disable it); everyone else gets the usual
    upgrade_required."""
    org, role = await _org_access(db, auth, org_id, admin=admin)
    try:
        await _check_org_feature(db, org, skill_policy.FEATURE)
        return org, role, True
    except HTTPException as exc:
        if exc.status_code != 403 or role not in _ADMIN_ROLES:
            raise
        if await skill_policy.latest_policy_row(db, org.id) is None:
            raise
        return org, role, False


def _require_relaxation(current: OrgSkillPolicy | None, document: PolicyDocument) -> None:
    """Below Enterprise a new revision may only relax the current one."""
    current_doc = skill_policy.document_of(current) if current else None
    if current is None:
        raise HTTPException(
            403,
            detail={
                "code": "upgrade_required",
                "message": "This organization tier does not include the skills policy.",
                "feature": skill_policy.FEATURE,
            },
        )
    if current_doc is None:
        return  # an unreadable stored policy refuses everything; any valid revision relaxes it
    tighter = tightened_fields(current_doc.skills, document.skills)
    if tighter:
        raise HTTPException(
            403,
            detail={
                "code": "upgrade_required",
                "message": (
                    "This organization is no longer on a tier that includes the skills policy. Its last "
                    "policy stays enforced and can be relaxed or disabled, but not tightened: "
                    + ", ".join(tighter) + "."
                ),
                "feature": skill_policy.FEATURE,
                "tightened_fields": tighter,
            },
        )


def _compiled_summary(result: CompiledPolicy) -> dict[str, Any]:
    data = result.as_dict()
    # Devices only need the hash and system paths of each file to tell
    # whether IT deployed it (never the content here).
    data["artifacts"] = [
        {"kind": a["kind"], "content_hash": a["content_hash"], "paths": a["paths"]}
        for a in data["artifacts"]
        if a["kind"] == "file"
    ]
    return data


def _parse_body_policy(raw: Any) -> PolicyDocument:
    if len(json.dumps(raw, default=str)) > MAX_POLICY_BYTES:
        raise HTTPException(413, detail={"code": "policy_too_large", "message": "The policy is too large."})
    try:
        return parse_policy(raw)
    except PolicyError as exc:
        raise HTTPException(422, detail={"code": "invalid_policy", "message": str(exc)}) from None


class PolicyWrite(BaseModel):
    policy: dict[str, Any]
    # Warning codes from the compiled preview the admin has read (side
    # effects such as allowManagedHooksOnly stopping users' own hooks).
    acknowledge_warnings: list[str] = Field(default_factory=list, max_length=100)


class PolicyPreview(BaseModel):
    policy: dict[str, Any]


@router.get("/orgs/{org_id}/skills/policy")
async def get_policy(
    org_id: str,
    response: Response,
    auth: AuthContext = Depends(require_scope("skills:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org, _, entitled = await _policy_org(db, auth, org_id)
    row = await skill_policy.latest_policy_row(db, org.id)
    response.headers["ETag"] = _etag(row.revision if row else 0)
    # entitled=false: kept enforced after a downgrade; relax or disable only.
    return {**_policy_response(org, row), "entitled": entitled}


def _changes(old: dict[str, CompiledPolicy] | None, new: dict[str, CompiledPolicy]) -> list[str]:
    if old is None:
        return sorted(new)
    return sorted(tool for tool in new if old[tool].artifact_hash != new[tool].artifact_hash)


@router.post("/orgs/{org_id}/skills/policy/preview")
async def preview_policy(
    org_id: str,
    body: PolicyPreview,
    auth: AuthContext = Depends(require_scope("skills:admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org, _, entitled = await _policy_org(db, auth, org_id, admin=True)
    document = await skill_policy.resolve_source_ids(db, auth.user.id, _parse_body_policy(body.policy))
    catalogs = await skill_policy.catalog_sources(db, org)
    compiled = compile_all(document.skills, catalogs)
    current = await skill_policy.latest_policy_row(db, org.id)
    if not entitled:
        _require_relaxation(current, document)
    current_doc = skill_policy.document_of(current) if current else None
    old = compile_all(current_doc.skills, catalogs) if current_doc else None
    return {
        "policy": document.model_dump(mode="json"),
        "policy_hash": policy_hash(document),
        "current_revision": current.revision if current else 0,
        "changed_tools": _changes(old, compiled),
        "warnings": [w.as_dict() for result in compiled.values() for w in result.warnings],
        "compiled": {tool: result.as_dict() for tool, result in compiled.items()},
    }


@router.put("/orgs/{org_id}/skills/policy")
async def put_policy(
    org_id: str,
    body: PolicyWrite,
    request: Request,
    response: Response,
    auth: AuthContext = Depends(require_scope("skills:admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org, role, entitled = await _policy_org(db, auth, org_id, admin=True)
    if_match = _if_match(request)
    document = await skill_policy.resolve_source_ids(db, auth.user.id, _parse_body_policy(body.policy))
    return await _publish_revision(
        db, auth, org, role, response,
        if_match=if_match, document=document, acknowledged=body.acknowledge_warnings,
        entitled=entitled, event_type="skill_policy_updated",
    )


@router.post("/orgs/{org_id}/skills/policy/disable")
async def disable_policy(
    org_id: str,
    request: Request,
    response: Response,
    auth: AuthContext = Depends(require_scope("skills:admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Publish a revision that restricts nothing. Allowed at any tier for an
    org that has a policy (disabling is always a relaxation)."""
    org, role, entitled = await _policy_org(db, auth, org_id, admin=True)
    if_match = _if_match(request)
    current = await skill_policy.latest_policy_row(db, org.id)
    if current is None:
        raise HTTPException(404, detail={"code": "no_policy", "message": "This organization has no skills policy."})
    current_doc = skill_policy.document_of(current)
    document = unrestricted_from(current_doc.skills if current_doc else None)
    return await _publish_revision(
        db, auth, org, role, response,
        if_match=if_match, document=document, acknowledged=[],
        entitled=entitled, event_type="skill_policy_disabled",
    )


def _if_match(request: Request) -> str:
    if_match = request.headers.get("If-Match")
    if not if_match:
        raise HTTPException(428, "If-Match header required (use the ETag from GET; \"osp-0\" for the first policy)")
    return if_match


async def _publish_revision(
    db: AsyncSession,
    auth: AuthContext,
    org: Organization,
    role: str,
    response: Response,
    *,
    if_match: str,
    document: PolicyDocument,
    acknowledged: list[str],
    entitled: bool,
    event_type: str,
) -> dict:
    if not entitled:
        # Refused before asking for acknowledgements; re-checked under the lock.
        _require_relaxation(await skill_policy.latest_policy_row(db, org.id), document)
    catalogs = await skill_policy.catalog_sources(db, org)
    compiled = compile_all(document.skills, catalogs)
    warnings = [w for result in compiled.values() for w in result.warnings]
    missing = sorted({w.code for w in warnings} - set(acknowledged))
    if missing:
        raise HTTPException(
            409,
            detail={
                "code": "acknowledgement_required",
                "message": "This policy has side effects in some tools; review and acknowledge them.",
                "warnings": [w.as_dict() for w in warnings if w.code in missing],
            },
        )
    # Serialize revisions per org (PostgreSQL row lock; SQLite is
    # single-writer), then compare the ETag under the lock.
    await db.execute(select(Organization.id).where(Organization.id == org.id).with_for_update())
    current = await skill_policy.latest_policy_row(db, org.id)
    current_revision = current.revision if current else 0
    if if_match.strip() != _etag(current_revision):
        raise HTTPException(
            409,
            detail={
                "code": "policy_changed",
                "message": "The policy changed since it was read; reload and retry.",
                "current_revision": current_revision,
            },
        )
    if not entitled:
        # Checked against the revision read under the lock.
        _require_relaxation(current, document)
    canonical = canonical_json(document)
    digest = policy_hash(document)
    row = OrgSkillPolicy(
        id=f"osp_{secrets.token_hex(12)}",
        org_id=org.id,
        revision=current_revision + 1,
        policy_json=canonical,
        policy_hash=digest,
        created_by_user_id=auth.user.id,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409,
            detail={"code": "policy_changed", "message": "The policy changed concurrently; reload and retry."},
        ) from None
    for tool, result in compiled.items():
        db.add(
            OrgPolicyCompilation(
                id=f"opc_{secrets.token_hex(12)}",
                policy_id=row.id,
                tool=tool,
                compiler_version=COMPILER_VERSION,
                artifact_hash=result.artifact_hash,
                enforceable=json.dumps(result.enforceable),
                report_only=json.dumps(result.report_only),
                unsupported=json.dumps(result.unsupported),
            )
        )
    _audit(
        db,
        org=org,
        auth=auth,
        role=role,
        event_type=event_type,
        target_type="skill_policy",
        target_id=row.id,
        after={
            "revision": row.revision,
            "previous_revision": current_revision,
            "policy_hash": digest,
            "previous_policy_hash": current.policy_hash if current else None,
            "mode": document.skills.mode,
            "acknowledged_warnings": sorted(set(acknowledged)),
            "entitled": entitled,
        },
    )
    await db.commit()
    await db.refresh(row)
    response.headers["ETag"] = _etag(row.revision)
    return _policy_response(org, row)


@router.get("/orgs/{org_id}/skills/policy/revisions")
async def list_policy_revisions(
    org_id: str,
    limit: int = Query(50, ge=1, le=200),
    auth: AuthContext = Depends(require_scope("skills:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org, _, _ = await _policy_org(db, auth, org_id, admin=True)
    rows = (
        await db.execute(
            select(OrgSkillPolicy)
            .where(OrgSkillPolicy.org_id == org.id)
            .order_by(OrgSkillPolicy.revision.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {"items": [_policy_response(org, row) for row in rows]}


@router.get("/orgs/{org_id}/skills/policy/compiled")
async def get_compiled_policy(
    org_id: str,
    tool: str | None = Query(None),
    auth: AuthContext = Depends(require_scope("skills:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org, _, _ = await _policy_org(db, auth, org_id)
    if tool is not None and tool not in TOOLS:
        raise HTTPException(422, "Unsupported tool")
    row = await skill_policy.latest_policy_row(db, org.id)
    if row is None:
        raise HTTPException(404, detail={"code": "no_policy", "message": "This organization has no skills policy."})
    document = skill_policy.document_of(row)
    if document is None:
        raise HTTPException(409, detail={"code": "policy_invalid", "message": "The stored policy does not validate."})
    catalogs = await skill_policy.catalog_sources(db, org)
    tools = [tool] if tool else list(TOOLS)
    return {
        "org_id": org.id,
        "revision": row.revision,
        "policy_hash": row.policy_hash,
        "compiled": {name: compile_policy(document.skills, catalogs, name).as_dict() for name in tools},
    }


@router.get("/orgs/{org_id}/skills/compliance")
async def org_compliance(
    org_id: str,
    limit: int | None = Query(None, ge=1, le=1000),
    cursor: str | None = Query(None, max_length=64),
    auth: AuthContext = Depends(require_scope("skills:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Latest report of every device, one page at a time (keyset on the
    device id; ``next_cursor`` is set while more remain). ``summary`` counts
    ALL devices of the org with SQL aggregates, never just this page."""
    org, _, _ = await _policy_org(db, auth, org_id, admin=True)
    current = await skill_policy.latest_policy_row(db, org.id)
    current_revision = current.revision if current else None
    page_size = min(limit or COMPLIANCE_PAGE_SIZE, COMPLIANCE_PAGE_SIZE)
    on_current = func.coalesce(DevicePolicyReport.policy_revision, -1) == (
        current_revision if current_revision is not None else -1
    )
    totals = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(
                    func.sum(case((on_current & (DevicePolicyReport.compliant.is_(True)), 1), else_=0)), 0
                ),
                func.coalesce(func.sum(case((on_current, 0), else_=1)), 0),
            )
            .select_from(DevicePolicyReport)
            .join(SkillDevice, SkillDevice.id == DevicePolicyReport.device_id)
            .where(DevicePolicyReport.org_id == org.id)
        )
    ).one()
    # One latest-report row per (device, org): no aggregation needed.
    query = (
        select(DevicePolicyReport, SkillDevice, User.email)
        .join(SkillDevice, SkillDevice.id == DevicePolicyReport.device_id)
        .join(User, User.id == SkillDevice.user_id)
        .where(DevicePolicyReport.org_id == org.id)
    )
    if cursor:
        query = query.where(DevicePolicyReport.device_id > cursor)
    rows = (
        await db.execute(query.order_by(DevicePolicyReport.device_id).limit(page_size + 1))
    ).all()
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    items = []
    for report, device, email in rows:
        items.append(
            {
                "device_id": device.id,
                "device_key": device.device_key,
                "display_name": device.display_name,
                "platform": device.platform,
                "user_id": device.user_id,
                "user_email": email,
                "reported_at": report.reported_at.isoformat(),
                "policy_revision": report.policy_revision,
                "current": report.policy_revision == current_revision,
                "compliant": bool(report.compliant) and report.policy_revision == current_revision,
                "mode": "advisory",
                "report": json.loads(report.report_json or "{}"),
            }
        )
    return {
        "org_id": org.id,
        "revision": current.revision if current else 0,
        "devices": items,
        "has_more": has_more,
        "next_cursor": items[-1]["device_id"] if has_more and items else None,
        "summary": {
            "devices": int(totals[0]),
            "compliant": int(totals[1]),
            "stale": int(totals[2]),
        },
    }


async def _caller_org_ids(db: AsyncSession, auth: AuthContext) -> list[str]:
    if auth.key_kind == "service":
        return [auth.org_id] if auth.org_id else []
    return list(
        (await db.execute(select(OrgMember.org_id).where(OrgMember.user_id == auth.user.id))).scalars().all()
    )


@router.get("/devices/me/policy")
async def my_device_policy(
    auth: AuthContext = Depends(require_scope("skills:read", "devices:report")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    policies = await skill_policy.effective_policies(db, await _caller_org_ids(db, auth))
    items = []
    for org_id in sorted(policies):
        effective = policies[org_id]
        entry: dict[str, Any] = {
            "org_id": org_id,
            "org_name": effective.org.name,
            "revision": effective.row.revision,
            "policy_hash": effective.row.policy_hash,
            "valid": effective.policy is not None,
            "delivery": "advisory",
        }
        if effective.policy is not None:
            catalogs = await skill_policy.catalog_sources(db, effective.org)
            entry["policy"] = {"skills": effective.policy.model_dump(mode="json")}
            entry["tools"] = {
                tool: _compiled_summary(result)
                for tool, result in compile_all(effective.policy, catalogs).items()
            }
        items.append(entry)
    return {
        "items": items,
        "advisory": True,
        "note": (
            "Advisory delivery: SessionFS refuses non-compliant installs it manages and reports "
            "compliance. A user can bypass it on their own machine; deploy the compiled managed "
            "settings through MDM for tool-level enforcement."
        ),
    }


class ToolReport(BaseModel):
    enforced_mode: str = Field("absent", pattern="^(advisory|system|absent)$")
    artifact_hash_found: str | None = Field(None, max_length=80)
    violations: list[str] = Field(default_factory=list, max_length=100)


class OrgReport(BaseModel):
    org_id: str = Field(..., max_length=64)
    policy_revision: int = Field(..., ge=0)
    per_tool: dict[str, ToolReport] = Field(default_factory=dict)
    # SessionFS-managed installs the policy refuses (name / version / reason).
    noncompliant_installs: list[dict[str, str]] = Field(default_factory=list, max_length=200)
    # Items this reconcile refused to install.
    refused: list[dict[str, str]] = Field(default_factory=list, max_length=200)
    # Plugins installed outside SessionFS (names and sources only).
    unmanaged_plugins: list[dict[str, str]] = Field(default_factory=list, max_length=200)


class DeviceReport(BaseModel):
    device_id: str = Field(..., max_length=100)
    display_name: str = Field("", max_length=200)
    platform: str = Field("", max_length=30)
    reports: list[OrgReport] = Field(..., max_length=MAX_REPORT_ORGS)


@router.post("/devices/me/policy-report", status_code=202)
async def post_device_report(
    body: DeviceReport,
    auth: AuthContext = Depends(require_scope("devices:report")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not _report_limiter.is_allowed(auth.user.id):
        raise HTTPException(429, detail={"code": "rate_limited", "message": "Too many policy reports."})
    try:
        device_key = validate_device_id(body.device_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    for report in body.reports:
        unknown = set(report.per_tool) - set(TOOLS)
        if unknown:
            raise HTTPException(422, f"Unknown tools: {', '.join(sorted(unknown))}")
        encoded = report.model_dump_json()
        if len(encoded) > MAX_REPORT_BYTES:
            raise HTTPException(413, detail={"code": "report_too_large", "message": "The report is too large."})
    policies = await skill_policy.effective_policies(db, await _caller_org_ids(db, auth))
    now = datetime.now(timezone.utc)
    device = await _device_for_report(db, auth.user.id, device_key, body, now)
    device.last_seen_at = now
    if body.display_name:
        device.display_name = body.display_name
    if body.platform:
        device.platform = body.platform
    accepted, ignored = [], []
    day = now.date().isoformat()
    for report in body.reports:
        effective = policies.get(report.org_id)
        if effective is None:
            ignored.append(report.org_id)  # not a member, or no policy applies
            continue
        compliant = (
            report.policy_revision == effective.row.revision
            and not report.noncompliant_installs
            and not report.refused
            and not any(item.violations for item in report.per_tool.values())
        )
        values = {
            "policy_revision": report.policy_revision,
            "reported_at": now,
            "report_json": report.model_dump_json(),
            "compliant": compliant,
        }
        await _upsert(
            db,
            DevicePolicyReport,
            (DevicePolicyReport.device_id == device.id, DevicePolicyReport.org_id == report.org_id),
            {
                **values,
                "policy_id": effective.row.id if report.policy_revision == effective.row.revision else None,
            },
            {"id": f"dpr_{secrets.token_hex(12)}", "device_id": device.id, "org_id": report.org_id},
        )
        await _upsert(
            db,
            DevicePolicyReportHistory,
            (
                DevicePolicyReportHistory.device_id == device.id,
                DevicePolicyReportHistory.org_id == report.org_id,
                DevicePolicyReportHistory.report_day == day,
            ),
            values,
            {
                "id": f"dph_{secrets.token_hex(12)}",
                "device_id": device.id,
                "org_id": report.org_id,
                "report_day": day,
            },
        )
        # Retention: 90 days of daily history per device and org (the latest
        # report is a single row and is always kept).
        await db.execute(
            delete(DevicePolicyReportHistory).where(
                DevicePolicyReportHistory.device_id == device.id,
                DevicePolicyReportHistory.org_id == report.org_id,
                DevicePolicyReportHistory.reported_at < now - REPORT_RETENTION,
            )
        )
        accepted.append({"org_id": report.org_id, "compliant": compliant})
    await db.commit()
    return {"device_id": device.id, "accepted": accepted, "ignored": ignored}


async def _upsert(db: AsyncSession, model: Any, where: tuple, values: dict[str, Any], key: dict[str, Any]) -> None:
    """Update the one row matching ``where`` (locked), or insert it; a
    concurrent insert of the same key loses the unique constraint and is
    retried as an update."""
    for _ in range(2):
        row = (await db.execute(select(model).where(*where).with_for_update())).scalar_one_or_none()
        if row is not None:
            for name, value in values.items():
                setattr(row, name, value)
            return
        try:
            async with db.begin_nested():
                db.add(model(**key, **values))
                await db.flush()
            return
        except IntegrityError:
            continue
    raise HTTPException(409, detail={"code": "report_conflict", "message": "Concurrent report; retry."})


async def _device_for_report(
    db: AsyncSession, user_id: str, device_key: str, body: "DeviceReport", now: datetime
) -> SkillDevice:
    """The caller's device row, created on first report within the
    per-user cap. Devices unseen for the retention window are pruned first;
    beyond the cap a NEW device is refused (409 device_limit) rather than
    evicting another device's compliance record."""
    lookup = select(SkillDevice).where(SkillDevice.user_id == user_id, SkillDevice.device_key == device_key)
    device = (await db.execute(lookup)).scalar_one_or_none()
    if device is not None:
        return device
    # Serialize new-device creation per user (PostgreSQL row lock; SQLite is
    # single-writer) so concurrent first reports cannot overshoot the cap.
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    device = (await db.execute(lookup)).scalar_one_or_none()
    if device is not None:
        return device
    stale = select(SkillDevice.id).where(
        SkillDevice.user_id == user_id, SkillDevice.last_seen_at < now - REPORT_RETENTION
    )
    # Explicit child deletes: the FK cascade does not fire on SQLite
    # connections without foreign_keys enabled.
    await db.execute(delete(DevicePolicyReportHistory).where(DevicePolicyReportHistory.device_id.in_(stale)))
    await db.execute(delete(DevicePolicyReport).where(DevicePolicyReport.device_id.in_(stale)))
    await db.execute(
        delete(SkillDevice).where(
            SkillDevice.user_id == user_id, SkillDevice.last_seen_at < now - REPORT_RETENTION
        )
    )
    count = (
        await db.execute(select(func.count()).select_from(SkillDevice).where(SkillDevice.user_id == user_id))
    ).scalar_one()
    if count >= MAX_DEVICES_PER_USER:
        raise HTTPException(
            409,
            detail={
                "code": "device_limit",
                "message": (
                    f"This account already reports from {MAX_DEVICES_PER_USER} devices. A device stops "
                    f"counting {REPORT_RETENTION.days} days after its last report; reuse an existing "
                    "device id (SESSIONFS_DEVICE_ID) or ask an org admin."
                ),
                "max_devices": MAX_DEVICES_PER_USER,
            },
        )
    device = SkillDevice(
        id=f"dev_{secrets.token_hex(12)}",
        user_id=user_id,
        device_key=device_key,
        display_name=body.display_name,
        platform=body.platform,
        first_seen_at=now,
        last_seen_at=now,
    )
    try:
        async with db.begin_nested():
            db.add(device)
            await db.flush()
    except IntegrityError:
        device = (await db.execute(lookup)).scalar_one()
    return device

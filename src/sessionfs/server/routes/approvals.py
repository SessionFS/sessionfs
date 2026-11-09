"""Approval policies REST API (GitHub #123, phase A1).

- ``GET/POST /api/v1/orgs/{org}/approval-policies`` — list (members see names
  and requirements; owners/admins see everything) and create (owner/admin).
- ``PUT/DELETE /api/v1/orgs/{org}/approval-policies/{id}`` — an edit inserts
  a new immutable version and deactivates the old one (pending requests stay
  on the version that raised them); delete deactivates.
- ``GET /api/v1/me/approvals`` — pending requests the caller could decide.
- ``GET /api/v1/approval-requests/{id}`` — one request with its decisions
  (readers of the target's project).
- ``POST /api/v1/approval-requests/{id}/decide`` — approve or reject; people
  only (service keys get 403).

Every miss is one identical 404 per resource (unknown org or not a member,
unknown request or unreadable target). Visibility runs before the role and
plan gates. Creating and editing a policy needs an Enterprise org; reading,
deactivating and deciding keep working after a downgrade so an enforced gate
can always be cleared. Service keys may only read (``approvals:read``), and
only inside their own org and project allowlist.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    assert_service_key_can_access_project,
    get_current_user,
    require_scope,
)
from sessionfs.server.auth.project_access import (
    user_can_access_project,
)
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.timeouts import bound_heavy_reads
from sessionfs.server.services.org_concurrency import org_slot
from sessionfs.server.db.models import (
    ApprovalPolicy,
    ApprovalRequest,
    OrgAuditEvent,
    Organization,
    Project,
    User,
)
from sessionfs.server.roles import has_minimum_role
from sessionfs.server.sanitize import sanitize_archive_string
from sessionfs.server.services import approvals as svc
from sessionfs.server.tier_gate import resolve_org_plan_tier
from sessionfs.server.tiers import get_features_for_tier, get_minimum_tier_for_feature

ORG_NOT_FOUND = "Organization not found"
_ME_LIMIT = 100

org_router = APIRouter(prefix="/api/v1/orgs", tags=["approvals"])
router = APIRouter(prefix="/api/v1", tags=["approvals"])


# ── helpers ────────────────────────────────────────────────────────


def _clean(value: str) -> str:
    cleaned, _ = sanitize_archive_string(value)
    return cleaned


async def _scan(db: AsyncSession, auth: AuthContext, org_id: str, text: str) -> str:
    """DLP: strictest union of the caller's orgs plus the scope org. Block
    mode refuses with 422 ``dlp_blocked``; other modes redact."""
    from sessionfs.server.routes.meetings import _dlp_scan_content, _upload_dlp_policy

    if not text:
        return text
    policy = await _upload_dlp_policy(db, auth, org_id)
    scanned, _ = _dlp_scan_content(_clean(text), policy)
    return scanned


def _auth(request: Request) -> AuthContext:
    return request.state.auth_context


async def _org_role(db: AsyncSession, auth: AuthContext, org_id: str) -> str:
    """The caller's standing in ``org_id``: their org role, or ``"service"``
    for a service key bound to that org. Everything else is the same 404."""
    if _clean(org_id) != org_id:
        raise HTTPException(404, ORG_NOT_FOUND)
    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, ORG_NOT_FOUND)
    if auth.key_kind == "service":
        if auth.org_id != org_id:
            raise HTTPException(404, ORG_NOT_FOUND)
        return "service"
    role = await svc.active_member_role(db, org_id, auth.user.id)
    if role is None:
        raise HTTPException(404, ORG_NOT_FOUND)
    return role


def _require_admin(role: str) -> None:
    if not has_minimum_role(role, "admin"):
        raise HTTPException(
            403,
            {
                "error": "insufficient_role",
                "code": "insufficient_role",
                "current_role": role,
                "required_role": "admin",
                "message": "Managing approval policies requires the admin or owner role.",
            },
        )


async def _require_plan(db: AsyncSession, org_id: str) -> None:
    plan = await resolve_org_plan_tier(org_id, db)
    if svc.FEATURE in get_features_for_tier(plan):
        return
    required = get_minimum_tier_for_feature(svc.FEATURE)
    raise HTTPException(
        403,
        {
            "error": "upgrade_required",
            "code": "upgrade_required",
            "feature": svc.FEATURE,
            "plan_scope": "org",
            "current_tier": plan.value,
            "required_tier": required,
            "upgrade_url": "https://sessionfs.dev/pricing",
            "message": f"Approval policies require the org's plan to be {required} or above.",
        },
    )


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _policy_view(p: ApprovalPolicy, *, full: bool) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": p.id,
        "lineage_id": p.lineage_id,
        "name": p.name,
        "target_kind": p.target_kind,
        "requirements": svc.policy_requirements(p),
        "blocking": bool(p.blocking),
        "active": bool(p.active),
        "version": p.version,
    }
    if full:
        view.update(
            description=p.description,
            filter=svc.policy_filter(p),
            supersedes_id=p.supersedes_id,
            created_by_user_id=p.created_by_user_id,
            created_at=_iso(p.created_at),
            deactivated_at=_iso(p.deactivated_at),
        )
    return view


def _audit_policy(
    db: AsyncSession,
    *,
    org: Organization,
    event_type: str,
    actor: User,
    role: str,
    before: dict[str, Any] | None,
    after: ApprovalPolicy | None,
) -> None:
    """Append-only OrgAuditEvent for every policy create/edit/deactivate, in
    the same transaction as the write, so turning a blocking gate off (and on
    again) always records who did it."""
    target_id = after.lineage_id if after is not None else (before or {}).get("lineage_id")
    db.add(
        OrgAuditEvent(
            id=f"oae_{secrets.token_hex(12)}",
            org_id=org.id,
            org_name_snapshot=org.name,
            event_type=event_type,
            actor_user_id=actor.id,
            actor_email_snapshot=actor.email,
            actor_role_at_time=role,
            target_type="approval_policy",
            target_id=target_id,
            before=json.dumps(before) if before is not None else None,
            after=json.dumps(_policy_view(after, full=True)) if after is not None else None,
        )
    )


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    target_kind: Literal["merge", "kb_promote"]
    filter: dict[str, Any] = Field(default_factory=dict)
    requirements: list[Any]
    blocking: bool = True


class DecideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    reason: str = Field(default="", max_length=svc.MAX_REASON_CHARS)


async def _prepared(
    db: AsyncSession, auth: AuthContext, org_id: str, body: PolicyBody
) -> dict[str, Any]:
    name = " ".join(_clean(body.name).split())
    if not name:
        raise HTTPException(422, {"error": "name_required", "code": "name_required",
                                  "message": "name is required."})
    flt, reqs = svc.validate_policy_body(body.target_kind, body.filter, body.requirements)
    await svc.check_policy_references(db, org_id, flt, reqs)
    return {
        "name": (await _scan(db, auth, org_id, name))[:120],
        "description": (await _scan(db, auth, org_id, _clean(body.description).strip()))[:2000],
        "target_kind": body.target_kind,
        "filter": json.dumps(flt, separators=(",", ":"), sort_keys=True),
        "requirements": json.dumps(reqs, separators=(",", ":")),
        "blocking": body.blocking,
    }


async def _recheck_org_merges(db: AsyncSession, org_id: str, target_kind: str) -> None:
    """A merge policy write can turn a green check non-green (or back): one
    detached task re-evaluates every ticket linked to an open PR/MR in the
    org, page by page (GitHub checks and GitLab statuses). Called after the
    commit."""
    if target_kind != "merge":
        return
    from sessionfs.server.services import merge_checks

    merge_checks.schedule_org_recheck(org_id)


def _has_scope(auth: AuthContext, scope: str) -> bool:
    return "*" in auth.scopes or scope in auth.scopes


async def _lock_org(db: AsyncSession, org_id: str) -> None:
    """Serialise policy writes per org (the 50-active cap and version swaps)."""
    await db.execute(
        select(Organization.id).where(Organization.id == org_id).with_for_update()
    )


async def _load_policy(db: AsyncSession, org_id: str, policy_id: str) -> ApprovalPolicy:
    if _clean(policy_id) != policy_id:
        raise HTTPException(404, svc.POLICY_NOT_FOUND)
    policy = (
        await db.execute(
            select(ApprovalPolicy).where(
                ApprovalPolicy.id == policy_id, ApprovalPolicy.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if policy is None:
        raise HTTPException(404, svc.POLICY_NOT_FOUND)
    return policy


# ── policies ───────────────────────────────────────────────────────


@org_router.get("/{org_id}/approval-policies")
async def list_policies(
    org_id: str,
    include_inactive: bool = Query(False),
    auth: AuthContext = Depends(require_scope("approvals:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Active policies (owners/admins may add ``include_inactive=true`` to see
    every version). Members and service keys see names and requirements."""
    role = await _org_role(db, auth, org_id)
    full = has_minimum_role(role, "admin")
    stmt = select(ApprovalPolicy).where(ApprovalPolicy.org_id == org_id)
    if not (full and include_inactive):
        stmt = stmt.where(ApprovalPolicy.active.is_(True))
    policies = (
        await db.execute(
            stmt.order_by(
                ApprovalPolicy.created_at, ApprovalPolicy.lineage_id, ApprovalPolicy.version
            )
        )
    ).scalars().all()
    plan = await resolve_org_plan_tier(org_id, db)
    views = [_policy_view(p, full=full) for p in policies]
    if role == "service":
        # A service key is not a person in the org: no named user ids.
        for v in views:
            v["requirements"] = svc.redacted_requirements(v["requirements"])
    return {
        "policies": views,
        "can_manage": full,
        "plan_includes_approvals": svc.FEATURE in get_features_for_tier(plan),
    }


@org_router.post("/{org_id}/approval-policies", status_code=201)
async def create_policy(
    org_id: str,
    body: PolicyBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a policy (owner/admin, Enterprise). Named approvers must be
    active members; at most 50 active policies per org."""
    auth = _auth(request)
    role = await _org_role(db, auth, org_id)
    _require_admin(role)
    await _require_plan(db, org_id)
    values = await _prepared(db, auth, org_id, body)
    await _lock_org(db, org_id)
    active = (
        await db.execute(
            select(func.count())
            .select_from(ApprovalPolicy)
            .where(ApprovalPolicy.org_id == org_id, ApprovalPolicy.active.is_(True))
        )
    ).scalar_one()
    if active >= svc.MAX_ACTIVE_POLICIES:
        raise HTTPException(
            409,
            {"error": "policy_limit", "code": "policy_limit",
             "message": f"An organization can have at most {svc.MAX_ACTIVE_POLICIES} active policies."},
        )
    pid = svc.new_id("ap")
    policy = ApprovalPolicy(
        id=pid, org_id=org_id, lineage_id=pid, version=1, active=True,
        created_by_user_id=user.id, **values,
    )
    db.add(policy)
    await db.flush()
    await db.refresh(policy)
    _audit_policy(
        db, org=await db.get(Organization, org_id), event_type="approval_policy_created",
        actor=user, role=role, before=None, after=policy,
    )
    await db.commit()
    await db.refresh(policy)
    await _recheck_org_merges(db, org_id, policy.target_kind)
    return _policy_view(policy, full=True)


@org_router.put("/{org_id}/approval-policies/{policy_id}")
async def update_policy(
    org_id: str,
    policy_id: str,
    body: PolicyBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Edit = a new version row (``version`` + 1, ``supersedes_id`` = this
    one) and this row deactivated, in one transaction. Requests already raised
    stay on this version. ``target_kind`` can't change."""
    auth = _auth(request)
    role = await _org_role(db, auth, org_id)
    old = await _load_policy(db, org_id, policy_id)
    _require_admin(role)
    await _require_plan(db, org_id)
    if body.target_kind != old.target_kind:
        raise HTTPException(
            422, {"error": "target_kind_immutable", "code": "target_kind_immutable",
                  "message": "target_kind can't change; create a new policy instead."},
        )
    values = await _prepared(db, auth, org_id, body)
    await _lock_org(db, org_id)
    before_view = _policy_view(old, full=True)
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(ApprovalPolicy)
        .where(ApprovalPolicy.id == old.id, ApprovalPolicy.active.is_(True))
        .values(active=False, deactivated_at=now)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        raise HTTPException(
            409, {"error": "policy_inactive", "code": "policy_inactive",
                  "message": "Only the active version of a policy can be edited."},
        )
    new = ApprovalPolicy(
        id=svc.new_id("ap"), org_id=org_id, lineage_id=old.lineage_id,
        version=old.version + 1, supersedes_id=old.id, active=True,
        created_by_user_id=user.id, **values,
    )
    db.add(new)
    await db.flush()
    await db.refresh(new)
    _audit_policy(
        db, org=await db.get(Organization, org_id), event_type="approval_policy_updated",
        actor=user, role=role, before=before_view, after=new,
    )
    await db.commit()
    await db.refresh(new)
    await _recheck_org_merges(db, org_id, new.target_kind)
    return _policy_view(new, full=True)


@org_router.delete("/{org_id}/approval-policies/{policy_id}")
async def deactivate_policy(
    org_id: str,
    policy_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Deactivate a policy (owner/admin, any plan). Idempotent."""
    auth = _auth(request)
    role = await _org_role(db, auth, org_id)
    policy = await _load_policy(db, org_id, policy_id)
    _require_admin(role)
    before_view = _policy_view(policy, full=True)
    result = await db.execute(
        update(ApprovalPolicy)
        .where(ApprovalPolicy.id == policy.id, ApprovalPolicy.active.is_(True))
        .values(active=False, deactivated_at=datetime.now(timezone.utc))
    )
    changed = result.rowcount == 1  # type: ignore[attr-defined]
    if changed:
        await db.refresh(policy)
        _audit_policy(
            db, org=await db.get(Organization, org_id),
            event_type="approval_policy_deactivated",
            actor=user, role=role, before=before_view, after=policy,
        )
    await db.commit()
    await db.refresh(policy)
    if changed:
        await _recheck_org_merges(db, org_id, policy.target_kind)
    return _policy_view(policy, full=True)


# ── requests ───────────────────────────────────────────────────────


async def _visible_request(
    db: AsyncSession, auth: AuthContext, request_id: str
) -> ApprovalRequest:
    """The request when the caller may read its target's project (live
    scope), else the one 404."""
    if _clean(request_id) != request_id:
        raise HTTPException(404, svc.REQUEST_NOT_FOUND)
    req = await db.get(ApprovalRequest, request_id)
    if req is None:
        raise HTTPException(404, svc.REQUEST_NOT_FOUND)
    project = await db.get(Project, req.project_id)
    # A project that moved out of the request's org no longer exposes it.
    if project is None or project.org_id != req.org_id:
        raise HTTPException(404, svc.REQUEST_NOT_FOUND)
    if auth.key_kind == "service":
        await assert_service_key_can_access_project(
            db, auth, project, opaque_404_detail=svc.REQUEST_NOT_FOUND
        )
    elif not await user_can_access_project(db, auth.user.id, project):
        raise HTTPException(404, svc.REQUEST_NOT_FOUND)
    return req


@router.get("/approval-requests/{request_id}")
async def get_request(
    request_id: str,
    auth: AuthContext = Depends(require_scope("approvals:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    req = await _visible_request(db, auth, request_id)
    # Project readers who are not active members of the request's org
    # (captured-session readers, service keys) get the redacted view: no
    # approver names or emails, no named user ids.
    redact = auth.key_kind == "service" or not await svc.is_active_org_member(
        db, req.org_id, auth.user.id
    )
    view = await svc.request_view(db, req, viewer=auth.user, redact=redact)
    await db.commit()  # a lazily expired request is persisted
    return view


@router.post("/approval-requests/{request_id}/decide")
async def decide_request(
    request_id: str,
    body: DecideBody,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Approve or reject. People only; the approver must fit a requirement,
    still be an active org member and not be an implementer of the target."""
    auth = _auth(request)
    req = await _visible_request(db, auth, request_id)
    reason = await _scan(db, auth, req.org_id, _clean(body.reason).strip())
    try:
        await svc.decide(
            db, request_id=req.id, approver=user, decision=body.decision,
            reason=reason[: svc.MAX_REASON_CHARS],
        )
    except HTTPException as exc:
        # Keep a lazy expiry, roll back anything else.
        if isinstance(exc.detail, dict) and exc.detail.get("state") == "expired":
            await db.commit()
        else:
            await db.rollback()
        raise
    await db.commit()
    await db.refresh(req)
    if req.state in ("approved", "rejected"):
        # The request was pending before this call (decide refuses
        # otherwise), so this decision settled it.
        from sessionfs.server import business_metrics

        business_metrics.observe_approval_decision(
            state=req.state,
            requested_at=req.requested_at,
            decided_at=req.decided_at,
            org_id=req.org_id,
            project_id=req.project_id,
        )
    if req.target_kind == "merge" and req.state in ("approved", "rejected"):
        # The GitHub/GitLab check for this ticket's PR reflects the outcome.
        from sessionfs.server.services import merge_checks

        merge_checks.schedule_recheck(req.target_id)
    return await svc.request_view(db, req, viewer=user)


@router.get("/me/approvals")
async def list_my_approvals(
    auth: AuthContext = Depends(require_scope("approvals:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Pending requests the caller (a service key: its user) could decide
    now, newest first, on projects the caller can read."""
    user = auth.user
    show_tickets = show_kb = True
    if auth.key_kind == "service":
        # The key's own org, narrowed to its project allowlist when it has
        # one. Target titles need the matching read scope.
        if not auth.org_id:
            return {"requests": [], "count": 0}
        mine = await svc.pending_for(
            db, user, org_id=auth.org_id, project_ids=auth.project_ids, limit=_ME_LIMIT
        )
        show_tickets = _has_scope(auth, "tickets:read")
        show_kb = _has_scope(auth, "knowledge:read")
    else:
        mine = await svc.pending_for(db, user, limit=_ME_LIMIT)
    policies = {
        p.id: p
        for p in (
            await db.execute(
                select(ApprovalPolicy).where(ApprovalPolicy.id.in_({r.policy_id for r in mine}))
            )
        ).scalars().all()
    } if mine else {}
    # Merge: the ticket title; kb_promote: the start of the entry.
    titled = [
        r for r in mine
        if (r.target_kind == "merge" and show_tickets)
        or (r.target_kind == "kb_promote" and show_kb)
    ]
    titles = await svc.target_titles(db, titled) if titled else {}
    titled_ids = {r.id for r in titled}
    counts = await svc.approval_counts(db, mine)
    items = []
    for r in mine:
        p = policies.get(r.policy_id)
        reqs = svc.policy_requirements(p) if p is not None else []
        item = {
            "id": r.id,
            "policy_id": r.policy_id,
            "policy_name": p.name if p is not None else None,
            "org_id": r.org_id,
            "project_id": r.project_id,
            "target_kind": r.target_kind,
            "target_id": r.target_id,
            "target_fingerprint": r.target_fingerprint,
            "state": r.state,
            "requested_at": _iso(r.requested_at),
            "expires_at": _iso(r.expires_at),
            "approved_count": svc.approved_count(reqs, counts.get(r.id, {})),
            "required_count": svc.required_count(reqs),
        }
        if r.id in titled_ids:
            item["target_title"] = titles.get(r.id)
        items.append(item)
    return {"requests": items, "count": len(items)}


# ── approval log (A2) ──────────────────────────────────────────────

_LOG_LIMIT_DEFAULT = 100
_LOG_LIMIT_MAX = 500
_CSV_MAX_ROWS = 10_000
_CSV_COLUMNS = (
    "decided_at", "decision", "approver_user_id", "approver_name", "reason",
    "request_id", "request_state", "policy_id", "policy_name", "policy_version",
    "project_id", "target_kind", "target_id", "target_fingerprint", "requested_at",
)
# A cell starting with one of these is read as a formula by spreadsheet apps.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "|", "%")


def csv_safe(value: Any) -> str:
    """One CSV cell, defused against formula injection (OWASP): a value that
    a spreadsheet would evaluate is prefixed with a single quote."""
    text = "" if value is None else str(value)
    if text.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + text
    return text


def _parse_log_time(value: str | None, what: str) -> datetime | None:
    if value is None or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, {"error": "invalid_filter", "code": "invalid_filter",
                                  "message": f"{what} must be an ISO 8601 date-time."}) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_cursor(before: str | None) -> tuple[datetime, str] | None:
    if before is None or not before.strip():
        return None
    ts, sep, cid = before.strip().rpartition("|")
    if not sep or _clean(cid) != cid or not svc._ID_RE.match(cid):
        raise HTTPException(422, {"error": "invalid_cursor", "code": "invalid_cursor",
                                  "message": "before must be a next_before value from a previous page."})
    when = _parse_log_time(ts, "before")
    assert when is not None
    return when, cid


def _log_row(d: Any, req: ApprovalRequest, p: ApprovalPolicy | None,
             name: str | None, email: str | None) -> dict[str, Any]:
    return {
        "decision_id": d.id,
        "decided_at": _iso(d.decided_at),
        "decision": d.decision,
        "approver_user_id": d.approver_user_id,
        "approver_name": name or email or (
            svc.FORMER_MEMBER if d.approver_user_id is None else None
        ),
        "reason": d.reason,
        "request_id": req.id,
        "request_state": req.state,
        "policy_id": req.policy_id,
        "policy_name": p.name if p is not None else None,
        "policy_version": p.version if p is not None else None,
        "project_id": req.project_id,
        "target_kind": req.target_kind,
        "target_id": req.target_id,
        "target_fingerprint": req.target_fingerprint,
        "requested_at": _iso(req.requested_at),
    }


@org_router.get("/{org_id}/approval-log")
async def approval_log(
    org_id: str,
    project_id: str | None = Query(None),
    policy_id: str | None = Query(None, description="Any version id; matches the whole lineage."),
    target_kind: Literal["merge", "kb_promote"] | None = Query(None),
    decision: Literal["approve", "reject"] | None = Query(None),
    approver: str | None = Query(None, description="Approver user id."),
    since: str | None = Query(None),
    until: str | None = Query(None),
    before: str | None = Query(None, description="next_before from the previous page."),
    limit: int = Query(_LOG_LIMIT_DEFAULT, ge=1, le=_LOG_LIMIT_MAX),
    format: Literal["json", "csv"] = Query("json"),
    auth: AuthContext = Depends(require_scope("approvals:read")),
    db: AsyncSession = Depends(get_db),
):
    """Every approval decision in the org, newest first (owners and admins,
    any plan). Keyset pages via ``next_before``; ``format=csv`` exports up to
    10,000 matching rows with formula-like cells defused."""
    await bound_heavy_reads(db)
    from fastapi.responses import Response as RawResponse

    from sessionfs.server.db.models import ApprovalDecision

    role = await _org_role(db, auth, org_id)
    _require_admin(role)
    for value, what in ((project_id, "project_id"), (policy_id, "policy_id"), (approver, "approver")):
        if value is not None and (_clean(value) != value or not svc._ID_RE.match(value)):
            raise HTTPException(422, {"error": "invalid_filter", "code": "invalid_filter",
                                      "message": f"{what} is not a valid id."})
    since_dt = _parse_log_time(since, "since")
    until_dt = _parse_log_time(until, "until")
    cursor = _parse_cursor(before)

    stmt = (
        select(ApprovalDecision, ApprovalRequest, ApprovalPolicy, User.display_name, User.email)
        .join(ApprovalRequest, ApprovalRequest.id == ApprovalDecision.request_id)
        .join(ApprovalPolicy, ApprovalPolicy.id == ApprovalRequest.policy_id, isouter=True)
        .join(User, User.id == ApprovalDecision.approver_user_id, isouter=True)
        .where(ApprovalRequest.org_id == org_id)
    )
    if project_id:
        stmt = stmt.where(ApprovalRequest.project_id == project_id)
    if policy_id:
        lineage = (
            await db.execute(
                select(ApprovalPolicy.lineage_id).where(
                    ApprovalPolicy.id == policy_id, ApprovalPolicy.org_id == org_id
                )
            )
        ).scalar_one_or_none()
        stmt = stmt.where(ApprovalPolicy.lineage_id == (lineage or "\x00none"))
    if target_kind:
        stmt = stmt.where(ApprovalRequest.target_kind == target_kind)
    if decision:
        stmt = stmt.where(ApprovalDecision.decision == decision)
    if approver:
        stmt = stmt.where(ApprovalDecision.approver_user_id == approver)
    if since_dt is not None:
        stmt = stmt.where(ApprovalDecision.decided_at >= since_dt)
    if until_dt is not None:
        stmt = stmt.where(ApprovalDecision.decided_at < until_dt)
    stmt = stmt.order_by(ApprovalDecision.decided_at.desc(), ApprovalDecision.id.desc())

    def _page(s: Any, cur: tuple[datetime, str] | None) -> Any:
        if cur is None:
            return s
        ts, cid = cur
        return s.where(
            (ApprovalDecision.decided_at < ts)
            | ((ApprovalDecision.decided_at == ts) & (ApprovalDecision.id < cid))
        )

    if format == "csv":
        import csv
        import io

        async with org_slot(db, "export", org_id=org_id, user_id=None):
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(_CSV_COLUMNS)
            written = 0
            cur = cursor
            while written < _CSV_MAX_ROWS:
                rows = (await db.execute(_page(stmt, cur).limit(_LOG_LIMIT_MAX))).all()
                if not rows:
                    break
                for d, req, p, name, email in rows:
                    row = _log_row(d, req, p, name, email)
                    writer.writerow([csv_safe(row[c]) for c in _CSV_COLUMNS])
                    written += 1
                    if written >= _CSV_MAX_ROWS:
                        break
                last = rows[-1][0]
                last_at = last.decided_at if last.decided_at.tzinfo else last.decided_at.replace(tzinfo=timezone.utc)
                cur = (last_at, last.id)
                if len(rows) < _LOG_LIMIT_MAX:
                    break
        headers = {
            "Content-Disposition": f'attachment; filename="approval-log-{org_id}.csv"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if written >= _CSV_MAX_ROWS:
            headers["X-Truncated"] = "true"
        return RawResponse(buf.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)

    rows = (await db.execute(_page(stmt, cursor).limit(limit + 1))).all()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [_log_row(d, req, p, name, email) for d, req, p, name, email in rows]
    next_before = None
    if more and rows:
        last = rows[-1][0]
        next_before = f"{_iso(last.decided_at)}|{last.id}"
    return {"items": items, "next_before": next_before}

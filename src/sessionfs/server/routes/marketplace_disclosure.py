"""Install disclosure, update diffs and held org updates (marketplace v2 §3.7).

- ``GET /api/v1/marketplace/versions/{id}/disclosure``: the per-tool
  disclosure card (files written per adapter, hooks, scripts, MCP servers,
  tool pre-approvals, network endpoints, shell commands the instructions
  tell the agent to run, scanner findings, hidden Unicode). Same JSON the
  CLI and the dashboard render.
- ``GET /api/v1/marketplace/versions/{from}/diff/{to}``: files added /
  removed / changed with bounded text diffs, capability delta, scan and
  Unicode delta, and the D2 update class.
- ``GET /api/v1/orgs/{org}/skills/assignments/updates``: the org's "held
  updates" queue: active assignments whose package has a newer deliverable
  version, with its update class.
- ``POST /api/v1/orgs/{org}/skills/assignments/{id}/advance``: move an
  assignment to a newer version of the same package. Text updates need only
  the call; code changes need ``approve_code_change`` (unless the
  trusted-publisher hook allows them); widening updates always need
  ``approve_widening``.

All of it is available on every tier (§3.9) except the org queue and
advance, which use the org's ``team_skill_library`` like assignments do.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sessionfs.server.auth.dependencies import AuthContext, require_scope
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import OrgSkillAssignment, SkillPackage, SkillPackageVersion
from sessionfs.server.routes.marketplace import (
    _assignment_response,
    _audit,
    _blob_store,
    _check_org_feature,
    lock_content_hashes,
    _org_access,
    _org_tier,
    _take_delivery_locks,
    _revalidate_delivery,
    _visible_listings,
    _visible_row,
    bump_grant_generations,
)
from sessionfs.server.routes.marketplace_scanning import _scan_viewable_version
from sessionfs.server.services import skill_disclosure
from sessionfs.skills import disclosure
from sessionfs.skills.models import SUPPORTED_TOOLS

router = APIRouter(prefix="/api/v1", tags=["skill-marketplace-disclosure"])

MAX_UPDATES_PAGE = 100


def _tools(raw: str | None) -> list[str] | None:
    if raw is None or not raw.strip():
        return None
    tools = list(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    unknown = sorted(set(tools) - set(SUPPORTED_TOOLS))
    if unknown:
        raise HTTPException(422, f"Unsupported tool(s): {', '.join(unknown)}")
    return tools


async def disclosure_for_version(
    db: AsyncSession,
    request: Request,
    version: SkillPackageVersion,
    *,
    tools: list[str] | None = None,
    scope: str = "project",
) -> dict:
    manifest = await skill_disclosure.version_manifest(db, _blob_store(request), version)
    if manifest is None:
        raise HTTPException(503, "Marketplace package blob is unavailable")
    card = disclosure.build_disclosure(
        manifest,
        tools=tools,
        scope=scope,
        version_id=version.id,
        content_hash=version.content_hash,
        provenance=skill_disclosure.version_provenance(version),
    )
    card["package_id"] = version.package_id
    card["consent_rule_version"] = disclosure.CONSENT_RULE_VERSION
    return card


@router.get("/marketplace/versions/{version_id}/disclosure")
async def get_version_disclosure(
    version_id: str,
    request: Request,
    tools: str | None = Query(None, max_length=200),
    scope: str = Query("project", pattern="^(project|user)$"),
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    version, recheck = await _viewable(db, auth, version_id)
    card = await disclosure_for_version(db, request, version, tools=_tools(tools), scope=scope)
    await _recheck(db, auth, [(version, recheck)])
    return card


async def lock_versions_in_order(db: AsyncSession, version_ids: list[str]) -> dict:
    """FOR UPDATE on the version rows in id order, then their content-hash
    locks (sorted): the order moderation takes them in."""
    ids = sorted(set(version_ids))
    rows = (
        await db.execute(
            select(SkillPackageVersion)
            .where(SkillPackageVersion.id.in_(ids))
            .order_by(SkillPackageVersion.id)
            .with_for_update()
        )
    ).scalars().all()
    await lock_content_hashes(db, [row.content_hash for row in rows])
    return {row.id: row for row in rows}


async def _viewable(db: AsyncSession, auth: AuthContext, version_id: str):
    """(version, deliverable up front). Moderators also see versions that are
    quarantined / yanked / blocked (constant 404 for everyone else)."""
    version, _moderates = await _scan_viewable_version(db, auth, version_id)
    try:
        await _visible_row(db, auth, version_id=version_id)
    except HTTPException:
        return version, False
    return version, True


async def _recheck(db: AsyncSession, auth: AuthContext, versions: list) -> None:
    """Re-check deliverability of every involved version AFTER the blob work,
    immediately before returning, in version-id order (the delivery helper
    takes the takedown protocol's locks). Moderators may view versions that
    are not deliverable: a version that was NOT deliverable up front (only a
    moderator could see it) is not re-checked. Commits once (which also
    persists a lazily computed manifest - derived data only)."""
    deliverable = sorted({version.id: version for version, recheck in versions if recheck}.values(),
                         key=lambda item: item.id)
    for version in deliverable:
        await _revalidate_delivery(db, auth, version, release=False)
    await db.commit()


@router.get("/marketplace/versions/{from_version_id}/diff/{to_version_id}")
async def get_version_diff(
    from_version_id: str,
    to_version_id: str,
    request: Request,
    text: bool = Query(True, description="Include bounded unified text diffs"),
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    old, old_recheck = await _viewable(db, auth, from_version_id)
    new, new_recheck = await _viewable(db, auth, to_version_id)
    if old.package_id != new.package_id:
        raise HTTPException(
            422,
            detail={
                "code": "different_packages",
                "message": "A diff compares two versions of the same package.",
            },
        )
    result = await skill_disclosure.version_diff(
        db, _blob_store(request), old, new, include_text=text
    )
    await _recheck(db, auth, [(old, old_recheck), (new, new_recheck)])
    return {
        "package_id": new.package_id,
        "from_version_id": old.id,
        "to_version_id": new.id,
        "from_content_hash": old.content_hash,
        "to_content_hash": new.content_hash,
        **result,
    }


async def _deliverable_for_org(db: AsyncSession, auth: AuthContext, org_id: str, version_id: str) -> bool:
    try:
        rows = await _visible_listings(db, auth, version_id=version_id)
    except HTTPException:
        return False
    return any(
        row[0].visibility == "public" or (row[0].visibility == "org" and row[0].org_id == org_id)
        for row in rows
    )


async def _trusted_code(db: AsyncSession, org, old: SkillPackageVersion, new: SkillPackageVersion) -> bool:
    tier = await _org_tier(db, org)
    return disclosure.trusted_code_update_allowed(
        org_id=org.id,
        org_tier=tier.value,
        publisher=None,
        old_provenance=skill_disclosure.version_provenance(old),
        new_provenance=skill_disclosure.version_provenance(new),
    )


def _newer_than(version: SkillPackageVersion, column: Any = None):
    """Publication order: the monotonic, package-scoped publication sequence
    (allocated under the publisher lock, the same order /advance uses to
    reject a non-newer target). A NULL sequence never counts as newer."""
    newer = column if column is not None else SkillPackageVersion
    return newer.publication_seq > version.publication_seq


async def _newest_deliverable_after(
    db: AsyncSession, auth: AuthContext, org_id: str, assigned: SkillPackageVersion
) -> SkillPackageVersion | None:
    """The newest version of the same package strictly after the assigned one
    that is deliverable to this org (a public or matching-org listing, not
    yanked, not quarantined). This is what the held-updates queue offers:
    the package's current pointer can be yanked or quarantined while an older
    deliverable version is still a valid update, and a republished older
    pointer must never propose a downgrade."""
    candidates = (
        await db.execute(
            select(SkillPackageVersion)
            .where(
                SkillPackageVersion.package_id == assigned.package_id,
                SkillPackageVersion.quarantined_at.is_(None),
                _newer_than(assigned),
            )
            .order_by(SkillPackageVersion.publication_seq.desc().nullslast())
        )
    ).scalars().all()
    for row in candidates:
        if await _deliverable_for_org(db, auth, org_id, row.id):
            return row
    return None


def _has_newer_version():
    """EXISTS a strictly newer version of the assigned package (the queue's
    SQL prefilter; deliverability itself is checked per row, before the page
    is cut)."""
    newer = aliased(SkillPackageVersion)
    return (
        select(newer.id)
        .where(newer.package_id == SkillPackage.id, _newer_than(SkillPackageVersion, column=newer))
        .exists()
    )


def _encode_updates_cursor(assignment_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([assignment_id]).encode()).decode().rstrip("=")


def _decode_updates_cursor(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        (assignment_id,) = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, TypeError, binascii.Error):
        raise HTTPException(422, "Invalid cursor") from None
    if not isinstance(assignment_id, str) or not assignment_id or len(assignment_id) > 64:
        raise HTTPException(422, "Invalid cursor")
    return assignment_id


@router.get("/orgs/{org_id}/skills/assignments/updates")
async def list_assignment_updates(
    org_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=MAX_UPDATES_PAGE),
    cursor: str | None = Query(None, max_length=512),
    auth: AuthContext = Depends(require_scope("skills:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Active assignments whose package has a newer deliverable version,
    keyset-paginated by assignment id (unique; a total order that does not
    depend on timestamp precision). Rows are filtered (deliverability)
    BEFORE the page is cut, and the cursor is the last row EXAMINED, so
    filtered rows never hide later ones.

    Text updates may be advanced with one action; code and widening updates
    are held until an admin approves them through /advance."""
    org, _role = await _org_access(db, auth, org_id, admin=True)
    await _check_org_feature(db, org, "team_skill_library")
    after = _decode_updates_cursor(cursor)
    store = _blob_store(request)
    items: list[dict] = []
    last_examined: str | None = after
    exhausted = False
    batch = limit * 2
    while len(items) < limit and not exhausted:
        statement = (
            select(OrgSkillAssignment, SkillPackageVersion, SkillPackage)
            .join(SkillPackageVersion, SkillPackageVersion.id == OrgSkillAssignment.version_id)
            .join(SkillPackage, SkillPackage.id == SkillPackageVersion.package_id)
            .where(
                OrgSkillAssignment.org_id == org.id,
                OrgSkillAssignment.revoked_at.is_(None),
                # Any strictly newer version of the package — NOT the current
                # pointer, which can be yanked / quarantined (an older
                # deliverable version is still an update) or republished to an
                # older version (the assigned version may be newer than it).
                _has_newer_version(),
            )
        )
        if last_examined is not None:
            statement = statement.where(OrgSkillAssignment.id > last_examined)
        rows = (
            await db.execute(
                statement.order_by(OrgSkillAssignment.id).limit(batch)
            )
        ).all()
        exhausted = len(rows) < batch
        for assignment, version, package in rows:
            if len(items) >= limit:
                exhausted = False
                break
            last_examined = assignment.id
            item = await _update_item(db, auth, org, store, assignment, version, package)
            if item is not None:
                items.append(item)
    has_more = not exhausted
    next_cursor = (
        _encode_updates_cursor(last_examined) if has_more and last_examined is not None else None
    )
    await db.rollback()  # read-only: nothing to keep
    return {"items": items, "count": len(items), "has_more": has_more, "next_cursor": next_cursor}


def _is_diff_unavailable(exc: HTTPException) -> bool:
    detail = exc.detail
    return (
        exc.status_code == 503
        and isinstance(detail, dict)
        and detail.get("code") == "diff_unavailable"
    )


async def _update_item(db, auth, org, store, assignment, version, package) -> dict | None:
    target = await _newest_deliverable_after(db, auth, org.id, version)
    if target is None:
        return None
    # Read-only: a GET never persists a manifest (no row locks, no deadlock).
    try:
        update: dict[str, Any] | None = await skill_disclosure.classify_grant_update(
            db, store, version, target,
            digest=assignment.capability_digest, rule_version=assignment.consent_rule_version,
            persist=False,
        )
    except HTTPException as exc:
        # One target whose manifest cannot be computed (blob missing or
        # unreadable, an incomplete lazy rescan) must not fail the whole
        # org's page: the item is listed as held with NO diff and NO
        # update_digest, so it can never be acknowledged here, and /advance
        # keeps refusing it until the diff can be built.
        if not _is_diff_unavailable(exc):
            raise
        update = None
    if update is None:
        await _take_delivery_locks(db, target)
        try:
            deliverable = await _deliverable_for_org(db, auth, org.id, target.id)
        finally:
            await db.commit()
        if not deliverable:
            return None
        return {
            "assignment": _assignment_response(assignment),
            "package_id": package.id,
            "name": package.name,
            "from_version_id": version.id,
            "from_version": version.version_label,
            "to_version_id": target.id,
            "to_version": target.version_label,
            "to_content_hash": target.content_hash,
            # Unknown diff fails closed to widening.
            "update_class": "widening",
            "widens": [],
            "code_changes": [],
            "narrows": [],
            "baseline_known": None,
            "diff": None,
            "update_digest": None,
            "diff_unavailable": True,
            "held": True,
            "requires": "approve_widening",
        }
    trusted = update["update_class"] == "code" and await _trusted_code(db, org, version, target)
    kind = update["update_class"]
    # Like the disclosure / diff endpoints, re-check deliverability AFTER the
    # blob work (the diff was built) under the takedown protocol's SHARED
    # locks: a yank / block that commits in between drops the item instead of
    # offering a version that is no longer deliverable to this org.
    await _take_delivery_locks(db, target)
    try:
        deliverable = await _deliverable_for_org(db, auth, org.id, target.id)
    finally:
        await db.commit()
    if not deliverable:
        return None
    return {
        "assignment": _assignment_response(assignment),
        "package_id": package.id,
        "name": package.name,
        "from_version_id": version.id,
        "from_version": version.version_label,
        "to_version_id": target.id,
        "to_version": target.version_label,
        "to_content_hash": target.content_hash,
        "update_class": kind,
        "widens": update.get("widens") or [],
        "code_changes": update.get("code_changes") or [],
        "narrows": update.get("narrows") or [],
        "baseline_known": update.get("baseline_known"),
        # The exact build_diff() object the update_digest binds (the same
        # object the reapproval_required 409 returns as details.diff).
        "diff": update,
        "update_digest": skill_disclosure.update_digest(
            prior_id=assignment.id, old=version, new=target,
            bound_digest=assignment.capability_digest, update=update,
        ),
        "diff_unavailable": False,
        "held": kind == "widening" or (kind == "code" and not trusted),
        "requires": (
            "approve_widening"
            if kind == "widening"
            else "approve_code_change"
            if kind == "code" and not trusted
            else None
        ),
    }


class AssignmentAdvance(BaseModel):
    to_version_id: str = Field(..., min_length=1, max_length=64)
    content_hash: str = Field(..., min_length=1, max_length=80)
    # D2: explicit, never implied. Widening = new access; code change =
    # script / hook / MCP-config / executable-extension bytes changed.
    approve_widening: bool = False
    approve_code_change: bool = False
    # The update_digest of the held update that was reviewed.
    update_digest: str | None = Field(None, max_length=80)
    # The disclosure the admin saw (optional): must match the current one.
    capability_digest: str | None = Field(None, max_length=80)
    consent_rule_version: int | None = None
    disclosure_digest: str | None = Field(None, max_length=80)


@router.post("/orgs/{org_id}/skills/assignments/{assignment_id}/advance")
async def advance_assignment(
    org_id: str,
    assignment_id: str,
    body: AssignmentAdvance,
    request: Request,
    auth: AuthContext = Depends(require_scope("skills:admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org, role = await _org_access(db, auth, org_id, admin=True)
    await _check_org_feature(db, org, "team_skill_library")
    # Lock order (shared with moderation): version rows in id order, then
    # their content hashes, THEN the assignment row, which is re-read and
    # revalidated under the lock.
    peek = (
        await db.execute(
            select(OrgSkillAssignment.version_id).where(
                OrgSkillAssignment.id == assignment_id, OrgSkillAssignment.org_id == org.id
            )
        )
    ).scalar_one_or_none()
    if peek is None:
        raise HTTPException(404, "Skill assignment not found")
    locked = await lock_versions_in_order(db, [peek, body.to_version_id])
    current = (
        await db.execute(
            select(OrgSkillAssignment)
            .where(OrgSkillAssignment.id == assignment_id, OrgSkillAssignment.org_id == org.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if current is None:
        raise HTTPException(404, "Skill assignment not found")
    if current.version_id != peek:
        raise HTTPException(
            409,
            detail={"code": "assignment_changed", "message": "The assignment changed concurrently; retry."},
        )
    if current.revoked_at is not None:
        raise HTTPException(
            409,
            detail={
                "code": "assignment_revoked",
                "message": "This assignment was revoked by moderation and is kept as history.",
            },
        )
    old = locked.get(current.version_id)
    target = locked.get(body.to_version_id)
    if old is None or target is None or target.package_id != old.package_id:
        raise HTTPException(404, "Marketplace package not found")
    if target.id == old.id:
        raise HTTPException(409, detail={"code": "same_version", "message": "The assignment is already on this version."})
    if not await _deliverable_for_org(db, auth, org.id, target.id):
        raise HTTPException(404, "Marketplace package not found")
    if not (
        target.publication_seq is not None
        and old.publication_seq is not None
        and target.publication_seq > old.publication_seq
    ):
        # An advance never moves backwards or sideways in publication order:
        # a republished older current pointer must not propose a downgrade.
        raise HTTPException(
            409,
            detail={
                "code": "not_newer",
                "message": "The target version is not newer than the assigned version.",
            },
        )
    if target.content_hash != body.content_hash:
        raise HTTPException(
            409,
            detail={"code": "content_hash_mismatch", "message": "The hash is not the immutable version hash."},
        )
    store = _blob_store(request)
    target_manifest = await skill_disclosure.require_manifest(db, store, target)
    skill_disclosure.require_displayed_disclosure(
        target,
        body.capability_digest,
        body.consent_rule_version,
        disclosure_digest=body.disclosure_digest,
        manifest=target_manifest,
        tools=json.loads(current.target_tools or "[]"),
        scope="user",
    )
    # A legacy or stale assignment binding is an unknown baseline (widening).
    update = await skill_disclosure.classify_grant_update(
        db, store, old, target,
        digest=current.capability_digest, rule_version=current.consent_rule_version,
    )
    skill_disclosure.enforce_reapproval(
        update,
        approve_widening=body.approve_widening,
        approve_code_change=body.approve_code_change,
        trusted_code=update["update_class"] == "code" and await _trusted_code(db, org, old, target),
        widening_flag="approve_widening",
        code_flag="approve_code_change",
        expected_digest=skill_disclosure.update_digest(
            prior_id=current.id, old=old, new=target, bound_digest=current.capability_digest, update=update
        ),
        given_digest=body.update_digest,
        require_digest=True,
    )
    duplicates = (
        await db.execute(
            select(OrgSkillAssignment).where(
                OrgSkillAssignment.org_id == org.id,
                OrgSkillAssignment.version_id == target.id,
                OrgSkillAssignment.target_type == current.target_type,
                OrgSkillAssignment.target_id == current.target_id,
                OrgSkillAssignment.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    if any(
        current.target_type != "device" or row.target_user_id == current.target_user_id
        for row in duplicates
    ):
        raise HTTPException(
            409,
            detail={
                "code": "assignment_exists",
                "message": "This target already has an assignment for that version.",
            },
        )
    advanced = OrgSkillAssignment(
        id=f"osa_{secrets.token_hex(12)}",
        org_id=org.id,
        version_id=target.id,
        content_hash=target.content_hash,
        target_type=current.target_type,
        target_id=current.target_id,
        target_user_id=current.target_user_id,
        mode=current.mode,
        target_tools=current.target_tools,
        policy_snapshot=current.policy_snapshot,
        created_by_user_id=auth.user.id,
        package_id=target.package_id,
        capability_digest=target.capability_digest,
        consent_rule_version=disclosure.CONSENT_RULE_VERSION,
    )
    previous_id = current.id
    # The old assignment is kept as history (digest, targets, policy,
    # creator, timestamps): superseded, and revoked so it never grants again
    # and never occupies the active unique index.
    now = datetime.now(timezone.utc)
    current.superseded_at = now
    current.superseded_by_assignment_id = advanced.id
    current.revoked_at = now
    await db.flush()
    db.add(advanced)
    _audit(
        db,
        org=org,
        auth=auth,
        role=role,
        event_type="skill_assignment_advanced",
        target_type="skill_assignment",
        target_id=advanced.id,
        after={
            "previous_assignment_id": previous_id,
            "from_version_id": old.id,
            "to_version_id": target.id,
            "content_hash": target.content_hash,
            "update_class": update["update_class"],
            "widens": update.get("widens") or [],
            "code_changes": update.get("code_changes") or [],
            "approved_widening": body.approve_widening,
            "approved_code_change": body.approve_code_change,
            "consent_rule_version": disclosure.CONSENT_RULE_VERSION,
        },
    )
    await bump_grant_generations(db, "org", [org.id])
    await db.commit()
    await db.refresh(advanced)
    return {
        **_assignment_response(advanced),
        "previous_assignment_id": previous_id,
        "update_class": update["update_class"],
        "widens": update.get("widens") or [],
    }


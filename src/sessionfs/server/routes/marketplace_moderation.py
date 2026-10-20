"""Marketplace takedown: abuse reports, yank / block / restore, revocations feed.

Two moderation layers (server-side, re-derived on every request):

- PLATFORM layer (platform admins: user keys whose user tier is ``admin``):
  version-wide (the listing flags ``is_yanked`` / ``verification_state`` on
  every listing of the version), package-wide (``skill_packages.is_yanked``)
  and global content-hash blocks. Only this layer can take a PUBLIC listing
  down.
- OWNER layer (an org's owners/admins via ``has_minimum_role``, so owners are
  never excluded; or a personal package's owner): LISTING-SCOPED. It writes
  only ``owner_state`` on the owner's own non-public listings (version
  target) or ``skill_packages.owner_state`` (package target, which hides the
  package's non-public listings including later ones), plus org-scoped
  content-hash blocks that apply to that org's non-public listings only. It
  never changes anything visible through a public listing. Personal owners
  may yank, never block.

A platform admin always acts on the platform layer; a restore by a platform
admin lifts the platform layer, or the owner layer when only that one is
revoked. An owner restores only the owner layer; a platform takedown can
only be lifted by a platform admin.

Moderation is always a human action: service keys are refused on every
mutating and queue route here (403 ``service_key_not_allowed``). Service keys
may READ the revocations feed with ``skills:read``.

Delivery denial lives in ``routes/marketplace.py`` (``_deliverable_predicates``):
yanked and blocked versions disappear from search, get, preview, download,
feeds, install approval, org assignment and MCP instruction loading. Blocking
additionally revokes stored grants (install requests, consent receipts, org
assignments) by MARKING them; history is never deleted, and a restore never
re-grants anything.

Blocks are never lifted by deletion. A blocked version cannot be deleted
(409 ``active_block``; a moderator must restore it first). Content-hash
blocklist rows reference no package, version or org row (only their actor
columns, which are SET NULL), so deactivating or removing the owning
account, or removing the org or package rows, leaves every block active.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import AuthContext, require_scope
from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    AdminAction,
    Organization,
    OrgAuditEvent,
    OrgMember,
    OrgSkillAssignment,
    Project,
    SkillBlockedContentHash,
    SkillConsentReceipt,
    SkillInstallRequest,
    SkillMarketplaceListing,
    SkillPackage,
    SkillPackageReport,
    SkillPackageVersion,
    SkillRevocationEvent,
)
from sessionfs.server.roles import has_minimum_role
from sessionfs.server.services.skill_scanning import (
    RULESET_VERSION,
    latest_scan,
    on_quarantine_restored,
    open_critical_findings,
)
from sessionfs.server.routes.marketplace import (
    _audit,
    _decode_cursor,
    _deliverable_predicates,
    _encode_cursor,
    _org_ids,
    _visibility_predicate,
    bump_grant_generations_for_versions,
    lock_content_hashes,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["skill-marketplace-moderation"])

# Reports: a per-process burst limit plus a durable per-user daily cap that is
# counted in the database, so it holds across replicas and restarts.
REPORT_BURST_PER_MINUTE = 10
MAX_REPORTS_PER_DAY = 20
_report_burst_limiter = SlidingWindowRateLimiter(
    max_requests=REPORT_BURST_PER_MINUTE, window_seconds=60
)

MAX_REPORT_DETAILS_CHARS = 2000
MAX_REASON_CHARS = 500
MAX_FEED_LIMIT = 500

# Every revocation-event writer takes this transaction-scoped advisory lock on
# PostgreSQL before reading max(changed_at), and holds it until commit. That
# makes commit order == id order == changed_at order (see _next_changed_at).
_REVOCATION_WRITER_LOCK_KEY = 0x53_46_53_52_45_56_4B  # "SFSREVK"

_FEED_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Reason served on a global content block to callers who cannot see any of the
# moderated listings: the moderator's text may describe a private package.
GENERIC_CONTENT_BLOCK_REASON = "Blocked by SessionFS moderation"

# C0/C1 controls (except \n and \t), zero-width characters and bidi overrides /
# isolates: invisible text that can disguise what a moderator is reading.
_UNSAFE_CHARS_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]"
)

Action = Literal["yank", "block", "restore"]


class ReportCreate(BaseModel):
    reason: Literal["malicious", "secrets", "impersonation", "broken", "license", "spam", "other"]
    details: str = Field("", max_length=MAX_REPORT_DETAILS_CHARS)
    version_id: str | None = Field(None, min_length=1, max_length=64)


class FindingKey(BaseModel):
    rule_id: str = Field(..., min_length=1, max_length=80)
    path: str = Field(..., min_length=1, max_length=500)
    line_sha: str = Field(..., min_length=64, max_length=64)


class ModerationRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=MAX_REASON_CHARS)
    # Restoring a QUARANTINED version only: the critical scan findings the
    # moderator is clearing as false positives. Required when the version has
    # more than one distinct open critical finding, and it must name every one
    # of them (409 ``findings_required`` lists them otherwise).
    findings: list[FindingKey] | None = Field(default=None, max_length=500)


class ReportDismiss(BaseModel):
    note: str = Field(..., min_length=1, max_length=MAX_REASON_CHARS)


def _sanitize(value: str, *, cap: int) -> str:
    """Normalize newlines, drop control / invisible characters, trim, cap."""
    cleaned = value.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _UNSAFE_CHARS_RE.sub("", cleaned)
    return cleaned.strip()[:cap]


def _required_text(value: str, *, field: str) -> str:
    cleaned = _sanitize(value, cap=MAX_REASON_CHARS)
    if not cleaned:
        raise HTTPException(422, f"{field} must contain visible text")
    return cleaned


def _utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; every stored value is UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_platform_admin(auth: AuthContext) -> bool:
    return auth.key_kind == "user" and auth.user.tier == "admin"


def _require_user_key(auth: AuthContext) -> None:
    if auth.key_kind != "user":
        raise HTTPException(
            403,
            detail={
                "code": "service_key_not_allowed",
                "message": "Marketplace moderation and reports require a user key.",
            },
        )


async def _member_roles(db: AsyncSession, auth: AuthContext) -> dict[str, str]:
    """Raw org memberships (org_id -> role). Moderation and the revocations
    feed deliberately ignore the team_skill_library entitlement: a downgraded
    org must still be able to take down, and learn about, unsafe content."""
    if auth.key_kind == "service":
        return {auth.org_id: "service"} if auth.org_id else {}
    rows = (
        await db.execute(
            select(OrgMember.org_id, OrgMember.role).where(OrgMember.user_id == auth.user.id)
        )
    ).all()
    return {org_id: role for org_id, role in rows}


def _admin_org_ids(roles: dict[str, str]) -> list[str]:
    return sorted(org_id for org_id, role in roles.items() if has_minimum_role(role, "admin"))


def _can_see(auth: AuthContext, listing: SkillMarketplaceListing, roles: dict[str, str]) -> bool:
    if _is_platform_admin(auth) or listing.visibility == "public":
        return True
    if listing.visibility == "org":
        return listing.org_id in roles
    return listing.visibility == "user" and listing.owner_user_id == auth.user.id


def _owns_listing(auth: AuthContext, listing: SkillMarketplaceListing, roles: dict[str, str]) -> bool:
    """Owner-layer authority over one NON-PUBLIC listing: an owner/admin of
    the listing's org, or the owner of a personal listing."""
    if listing.visibility == "org":
        return listing.org_id is not None and has_minimum_role(roles.get(listing.org_id, ""), "admin")
    if listing.visibility == "user":
        return listing.owner_user_id == auth.user.id
    return False  # public listings belong to the platform layer


def _actor_role(auth: AuthContext, listings: list[SkillMarketplaceListing], roles: dict[str, str]) -> str:
    if _is_platform_admin(auth):
        return "platform_admin"
    for listing in listings:
        if listing.visibility == "org" and listing.org_id in roles:
            return roles[listing.org_id]
    return "owner"


def _scopes(listings: list[SkillMarketplaceListing]) -> list[tuple[str, str | None, str | None]]:
    """Visibility snapshots for the revocation events of one action.

    A public listing is visible to every signed-in principal, so it subsumes
    any narrower scope: one public event. Otherwise one event per distinct
    org / owner scope."""
    if any(listing.visibility == "public" for listing in listings):
        return [("public", None, None)]
    scopes: list[tuple[str, str | None, str | None]] = []
    for listing in listings:
        scope = (
            ("org", listing.org_id, None)
            if listing.visibility == "org"
            else ("user", None, listing.owner_user_id)
        )
        if scope not in scopes:
            scopes.append(scope)
    return scopes


def _platform_version_state(
    listings: list[SkillMarketplaceListing],
    global_blocks: list[SkillBlockedContentHash],
    version: SkillPackageVersion | None = None,
) -> str:
    if global_blocks or any(listing.verification_state == "blocked" for listing in listings):
        return "blocked"
    # Instruction-scan quarantine (migration 069) is PLATFORM-layer state, set
    # by an automated rescan and lifted only by a human restore (§5.6 / D3).
    if version is not None and version.quarantined_at is not None:
        return "quarantined"
    if any(listing.is_yanked for listing in listings):
        return "yanked"
    return "active"


def _owner_version_state(
    owner_listings: list[SkillMarketplaceListing], org_blocks: list[SkillBlockedContentHash]
) -> str:
    """Owner-layer state of a version's NON-PUBLIC listings (org blocks only
    ever apply to those)."""
    if not owner_listings:
        return "active"
    if org_blocks or any(listing.owner_state == "blocked" for listing in owner_listings):
        return "blocked"
    if any(listing.owner_state == "yanked" for listing in owner_listings):
        return "yanked"
    return "active"


def _platform_package_state(package: SkillPackage) -> str:
    if package.platform_state in {"yanked", "blocked"}:
        return package.platform_state
    return "yanked" if package.is_yanked else "active"  # pre-066 flag only


async def _acquire_revocation_writer_lock(db: AsyncSession) -> None:
    """Serialize event writers from here until commit, BEFORE max(changed_at)
    is read.

    PostgreSQL: a transaction-scoped advisory lock. SQLite: a database lock
    is only taken at a transaction's first write, so a writer that has not
    written yet could read max(changed_at) concurrently with another and the
    two could commit in reverse timestamp order. A zero-row UPDATE opens the
    write transaction (RESERVED lock) now; a concurrent writer waits on it
    (busy timeout) until we commit."""
    connection = await db.connection()
    if connection.dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _REVOCATION_WRITER_LOCK_KEY}
        )
    else:
        await db.execute(text("UPDATE skill_revocation_events SET id = id WHERE 1 = 0"))


async def _next_changed_at(db: AsyncSession) -> datetime:
    """A changed_at strictly greater than every committed event.

    Called under the writer lock, so no other writer can commit in between:
    each event's changed_at is unique and increases with its id, and a reader
    holding cursor T can never later see a NEW event with changed_at <= T.
    """
    latest = (await db.execute(select(func.max(SkillRevocationEvent.changed_at)))).scalar()
    now = datetime.now(timezone.utc)
    if latest is not None and _utc(latest) >= now:
        return _utc(latest) + timedelta(microseconds=1)
    return now


async def _revoke_grants(db: AsyncSession, version_ids: list[str], now: datetime) -> dict:
    """Mark (never delete) every grant on blocked versions."""
    if not version_ids:
        return {"install_requests": 0, "consent_receipts": 0, "assignments": 0}
    # Which tenants' rows are about to change, for their own audit records.
    request_rows = (
        await db.execute(
            select(SkillInstallRequest.id, Project.org_id, SkillInstallRequest.version_id)
            .join(Project, Project.id == SkillInstallRequest.project_id)
            .where(
                SkillInstallRequest.version_id.in_(version_ids),
                SkillInstallRequest.status.in_(["approved", "applied", "pending"]),
            )
        )
    ).all()
    assignment_rows = (
        await db.execute(
            select(OrgSkillAssignment.id, OrgSkillAssignment.org_id, OrgSkillAssignment.version_id).where(
                OrgSkillAssignment.version_id.in_(version_ids),
                OrgSkillAssignment.revoked_at.is_(None),
            )
        )
    ).all()
    revoked = await db.execute(
        update(SkillInstallRequest)
        .where(
            SkillInstallRequest.version_id.in_(version_ids),
            SkillInstallRequest.status.in_(["approved", "applied"]),
        )
        .values(status="revoked", decided_at=now)
    )
    rejected = await db.execute(
        update(SkillInstallRequest)
        .where(
            SkillInstallRequest.version_id.in_(version_ids),
            SkillInstallRequest.status == "pending",
        )
        .values(
            status="rejected",
            decided_at=now,
            approval_challenge_hash=None,
            approval_challenge_expires_at=None,
        )
    )
    receipts = await db.execute(
        update(SkillConsentReceipt)
        .where(
            SkillConsentReceipt.version_id.in_(version_ids),
            SkillConsentReceipt.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    assignment_orgs = list(
        (
            await db.execute(
                select(OrgSkillAssignment.org_id)
                .where(
                    OrgSkillAssignment.version_id.in_(version_ids),
                    OrgSkillAssignment.revoked_at.is_(None),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    assignments = await db.execute(
        update(OrgSkillAssignment)
        .where(
            OrgSkillAssignment.version_id.in_(version_ids),
            OrgSkillAssignment.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    return {
        "install_requests": (revoked.rowcount or 0) + (rejected.rowcount or 0),  # type: ignore[attr-defined]
        "consent_receipts": receipts.rowcount or 0,  # type: ignore[attr-defined]
        "assignments": assignments.rowcount or 0,  # type: ignore[attr-defined]
        "assignment_org_ids": sorted(assignment_orgs),
        "_per_org": _grants_by_org(assignment_rows, request_rows),
    }


def _grants_by_org(assignment_rows, request_rows) -> dict[str, dict[str, list[str]]]:
    """org_id -> that org's OWN revoked assignment / install-request ids and
    version ids (personal projects have no org and get no org audit)."""
    per_org: dict[str, dict[str, set[str]]] = {}
    for row_id, org_id, version_id in assignment_rows:
        entry = per_org.setdefault(org_id, {"assignments": set(), "requests": set(), "versions": set()})
        entry["assignments"].add(row_id)
        entry["versions"].add(version_id)
    for row_id, org_id, version_id in request_rows:
        if org_id is None:
            continue
        entry = per_org.setdefault(org_id, {"assignments": set(), "requests": set(), "versions": set()})
        entry["requests"].add(row_id)
        entry["versions"].add(version_id)
    return {
        org_id: {key: sorted(values) for key, values in entry.items()}
        for org_id, entry in per_org.items()
    }


async def _applying_hash_blocks(
    db: AsyncSession, *, package: SkillPackage, content_hashes: list[str]
) -> list[SkillBlockedContentHash]:
    """Every active row on these bytes that applies to this package: global
    rows, and rows scoped to the package's org."""
    if not content_hashes:
        return []
    return list(
        (
            await db.execute(
                select(SkillBlockedContentHash).where(
                    SkillBlockedContentHash.content_hash.in_(sorted(set(content_hashes))),
                    SkillBlockedContentHash.lifted_at.is_(None),
                    or_(
                        SkillBlockedContentHash.scope_org_id.is_(None),
                        SkillBlockedContentHash.scope_org_id == package.org_id,
                    ),
                )
            )
        ).scalars().all()
    )


async def _active_hash_blocks(
    db: AsyncSession, *, package: SkillPackage, version: SkillPackageVersion | None
) -> list[SkillBlockedContentHash]:
    """Active durable blocks on this target: global rows (platform layer) and
    rows scoped to the package's org (owner layer; they apply to that org's
    non-public listings only). Version target: every row on the version's
    bytes. Package target: the rows the whole-package block itself wrote."""
    statement = select(SkillBlockedContentHash).where(SkillBlockedContentHash.lifted_at.is_(None))
    if version is not None:
        statement = statement.where(
            SkillBlockedContentHash.content_hash == version.content_hash,
            or_(
                SkillBlockedContentHash.scope_org_id.is_(None),
                SkillBlockedContentHash.scope_org_id == package.org_id,
            ),
        )
    else:
        statement = statement.where(
            SkillBlockedContentHash.source_package_id == package.id,
            SkillBlockedContentHash.source_version_id.is_(None),
        )
    return list((await db.execute(statement.with_for_update())).scalars().all())


def _add_hash_block(
    db: AsyncSession,
    auth: AuthContext,
    *,
    package: SkillPackage,
    version_id: str | None,
    content_hashes: list[str],
    reason: str,
    now: datetime,
    scope_org_id: str | None,
) -> list[tuple[str, str | None]]:
    """A platform block is global (scope_org_id None); an org block applies
    only to that org's non-public listings. Returns the (content_hash,
    scope_org_id) pairs written."""
    platform = scope_org_id is None
    for content_hash in content_hashes:
        db.add(
            SkillBlockedContentHash(
                content_hash=content_hash,
                scope_org_id=scope_org_id,
                source_package_id=package.id,
                source_version_id=version_id,
                reason=reason,
                actor_user_id=auth.user.id,
                actor_role="platform_admin" if platform else "org_admin",
                blocked_at=now,
            )
        )
    return [(content_hash, scope_org_id) for content_hash in content_hashes]


async def _versions_with_hashes(
    db: AsyncSession, content_hashes: list[str], scope_org_id: str | None
) -> set[str]:
    """Every version carrying these bytes within a block's effective scope:
    all packages for a global block. An org block covers only that org's
    versions with NO public listing - a public copy reaches people outside
    the org and only a platform admin may take it down, so its grants are
    never touched by an org block."""
    if not content_hashes:
        return set()
    statement = (
        select(SkillPackageVersion.id)
        .join(SkillPackage, SkillPackage.id == SkillPackageVersion.package_id)
        .where(SkillPackageVersion.content_hash.in_(sorted(set(content_hashes))))
    )
    if scope_org_id is not None:
        publicly_listed = select(SkillMarketplaceListing.version_id).where(
            SkillMarketplaceListing.visibility == "public"
        )
        statement = statement.where(
            SkillPackage.org_id == scope_org_id,
            SkillPackageVersion.id.not_in(publicly_listed),
        )
    return set((await db.execute(statement)).scalars().all())


async def _target_hashes(
    db: AsyncSession, *, package: SkillPackage, version: SkillPackageVersion | None
) -> list[str]:
    """Content hashes a moderation action on this target may block or lift."""
    if version is not None:
        return [version.content_hash]
    current = (
        await db.execute(
            select(SkillPackageVersion.content_hash).where(
                SkillPackageVersion.package_id == package.id
            )
        )
    ).scalars().all()
    blocked = (
        await db.execute(
            select(SkillBlockedContentHash.content_hash).where(
                SkillBlockedContentHash.source_package_id == package.id,
                SkillBlockedContentHash.lifted_at.is_(None),
            )
        )
    ).scalars().all()
    return sorted(set(current) | set(blocked))


async def _still_blocked(db: AsyncSession, content_hash: str, scope_org_id: str | None) -> bool:
    statement = select(SkillBlockedContentHash.id).where(
        SkillBlockedContentHash.content_hash == content_hash,
        SkillBlockedContentHash.lifted_at.is_(None),
    )
    if scope_org_id is None:
        statement = statement.where(SkillBlockedContentHash.scope_org_id.is_(None))
    else:
        statement = statement.where(SkillBlockedContentHash.scope_org_id == scope_org_id)
    return (await db.execute(statement.limit(1))).scalar_one_or_none() is not None


async def _moderate(
    db: AsyncSession,
    auth: AuthContext,
    *,
    action: Action,
    reason: str,
    package_id: str | None = None,
    version_id: str | None = None,
    findings: list[FindingKey] | None = None,
) -> dict:
    _require_user_key(auth)
    reason = _required_text(reason, field="reason")

    # Lock order matches routes/marketplace.py: version rows first, then the
    # package row, then (at event insert) the revocation writer lock.
    version: SkillPackageVersion | None = None
    if version_id is not None:
        version = (
            await db.execute(
                select(SkillPackageVersion)
                .where(SkillPackageVersion.id == version_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if version is None:
            raise HTTPException(404, "Marketplace package not found")
        package_id = version.package_id
        version_ids = [version.id]
    else:
        version_ids = list(
            (
                await db.execute(
                    select(SkillPackageVersion.id)
                    .where(SkillPackageVersion.package_id == package_id)
                    .order_by(SkillPackageVersion.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
    package = (
        await db.execute(
            select(SkillPackage).where(SkillPackage.id == package_id).with_for_update()
        )
    ).scalar_one_or_none()
    if package is None:
        raise HTTPException(404, "Marketplace package not found")
    # Lock order: version row(s) -> package row -> content-hash locks (shared
    # with every grant-creating path in routes/marketplace.py) -> revocation
    # writer lock. Holding the hash locks from here to commit means no grant
    # on these bytes can be created between our revocation and our commit.
    target_hashes = await _target_hashes(db, package=package, version=version)
    await lock_content_hashes(db, target_hashes)

    listing_query = select(SkillMarketplaceListing).where(
        SkillMarketplaceListing.package_id == package.id
    )
    if version is not None:
        listing_query = listing_query.where(SkillMarketplaceListing.version_id == version.id)
    listings = list((await db.execute(listing_query)).scalars().all())
    roles = await _member_roles(db, auth)
    # Nothing to see (no listing, foreign org, someone else's personal
    # package) is a constant 404: existence is never disclosed.
    if not listings or not any(_can_see(auth, listing, roles) for listing in listings):
        raise HTTPException(404, "Marketplace package not found")
    platform = _is_platform_admin(auth)
    non_public = [listing for listing in listings if listing.visibility != "public"]
    if platform:
        layer = "platform"
    else:
        # Owner layer: only the actor's own non-public listings, never a
        # public one. Seeing the target without owning any of its non-public
        # listings (e.g. a public listing, or an org member) is a 403.
        owned = [listing for listing in non_public if _owns_listing(auth, listing, roles)]
        if not owned:
            raise _forbidden()
        if action == "block" and any(listing.visibility == "user" for listing in owned):
            raise _forbidden("Only a platform admin can block a personal listing.")
        layer = "owner"
    # The owner layer covers the target's non-public listings: the actor's
    # own when an owner acts; all of them when a platform admin lifts it.
    owner_listings = non_public if platform else owned

    hash_blocks = await _active_hash_blocks(db, package=package, version=version)
    global_blocks = [row for row in hash_blocks if row.scope_org_id is None]
    org_blocks = [row for row in hash_blocks if row.scope_org_id is not None]
    if version is not None:
        # Rows this version's OWN block wrote, versus rows another target
        # (another version / package with the same bytes) wrote that merely
        # apply to it. Only owned rows decide what this target can restore,
        # and a restore lifts only owned rows.
        own_global = [row for row in global_blocks if row.source_version_id == version.id]
        own_org = [row for row in org_blocks if row.source_version_id == version.id]
        platform_state = _platform_version_state(listings, own_global, version)
        owner_state = _owner_version_state(owner_listings, own_org)
        platform_effective = _platform_version_state(listings, global_blocks, version)
        owner_effective = _owner_version_state(owner_listings, org_blocks)
    else:
        # Package target: _active_hash_blocks returns the rows the
        # whole-package block itself wrote (the only rows a package restore
        # may lift). The EFFECTIVE state also counts every active row on the
        # package's bytes in its scope that another target wrote, mirroring
        # the version logic, so a restore can tell it is blocked elsewhere.
        own_global, own_org = global_blocks, org_blocks
        platform_state = _platform_package_state(package)
        owner_state = package.owner_state or "active"
        own_ids = {row.id for row in hash_blocks}
        others = [
            row for row in await _applying_hash_blocks(db, package=package, content_hashes=target_hashes)
            if row.id not in own_ids
        ]
        platform_effective = (
            "blocked" if any(row.scope_org_id is None for row in others) else platform_state
        )
        owner_effective = (
            "blocked" if any(row.scope_org_id is not None for row in others) else owner_state
        )

    # Lifting an automated quarantine (§5.6): a platform admin, or the org's
    # owners/admins when every listing of the version is their org's own
    # (never a public or personal listing). It lifts the quarantine only:
    # yanks and blocks are separate rows with their own restores.
    lift_quarantine = False
    if action == "restore":
        if layer == "platform" and platform_state == "active" and owner_state != "active":
            layer = "owner"  # a platform admin may lift an owner takedown too
        current = platform_state if layer == "platform" else owner_state
        if version is not None and version.quarantined_at is not None:
            if layer == "platform" and current == "quarantined":
                lift_quarantine = True
            elif (
                layer == "owner"
                and current == "active"
                and platform_state == "quarantined"
                and all(listing.visibility == "org" for listing in listings)
                and len(owner_listings) == len(listings)
            ):
                current = "quarantined"
                lift_quarantine = True
        if current == "active":
            effective = platform_effective if layer == "platform" else owner_effective
            if effective != "active":
                # Blocked only through another target's content-hash block:
                # that target must be restored. Names nothing private.
                raise _conflict(
                    "blocked_by_other_target",
                    "This target is blocked only by a block on identical content elsewhere; "
                    "restore that block instead.",
                    effective,
                )
            if layer == "owner" and platform_effective != "active":
                raise _forbidden("Only a platform admin can restore a platform takedown.")
            raise _conflict("not_revoked", "Nothing to restore: the target is not yanked or blocked.", current)
    else:
        current = platform_state if layer == "platform" else owner_state
        if action == "yank" and current != "active":
            raise _conflict("already_revoked", f"The target is already {current}.", current)
        if action == "block" and current == "blocked":
            raise _conflict("already_revoked", "The target is already blocked.", current)

    cleared_findings: set[tuple[str, str, str]] = set()
    if lift_quarantine:
        assert version is not None
        cleared_findings = await _validate_cleared_findings(db, version, findings)

    now = datetime.now(timezone.utc)
    grants: dict = {}
    affected_versions: set[str] = set()
    written_blocks: list[tuple[str, str | None]] = []
    lifted: list[SkillBlockedContentHash] = []
    scope_org_id = None if layer == "platform" else package.org_id
    if action == "block":
        if version is not None:
            hashes = [version.content_hash]
        else:
            hashes = sorted(
                set(
                    (
                        await db.execute(
                            select(SkillPackageVersion.content_hash).where(
                                SkillPackageVersion.package_id == package.id
                            )
                        )
                    ).scalars().all()
                )
            )
        if layer == "platform":
            # Version- / package-wide, and every copy of the bytes everywhere.
            own_versions = {version.id} if version is not None else set(version_ids)
            affected_versions = own_versions | await _versions_with_hashes(db, hashes, None)
        elif scope_org_id is not None:
            # The org's non-public copies only (never a publicly listed version).
            affected_versions = await _versions_with_hashes(db, hashes, scope_org_id)
        grants = await _revoke_grants(db, sorted(affected_versions), now)
        if layer == "platform" or scope_org_id is not None:
            written_blocks = _add_hash_block(
                db, auth, package=package,
                version_id=version.id if version is not None else None,
                content_hashes=hashes, reason=reason, now=now, scope_org_id=scope_org_id,
            )
    if lift_quarantine:
        assert version is not None
        await on_quarantine_restored(
            db, version, reason=reason, actor_user_id=auth.user.id, now=now,
            finding_keys=cleared_findings,
        )
    elif action == "restore":
        to_lift = own_global if layer == "platform" else own_org
        for row in to_lift:
            row.lifted_at = now
            row.lifted_by_user_id = auth.user.id
        lifted = list(to_lift)

    new_owner_state = {"yank": "yanked", "block": "blocked", "restore": None}[action]
    if lift_quarantine:
        pass  # the version's quarantine flag only; listings are untouched
    elif version is not None:
        if layer == "platform":
            if action == "yank":
                values: dict = {"is_yanked": True}
            elif action == "block":
                values = {"verification_state": "blocked"}
            else:
                values = {
                    "is_yanked": False,
                    # Same rule publish uses for a fresh listing.
                    "verification_state": case(
                        (SkillMarketplaceListing.verification_state != "blocked",
                         SkillMarketplaceListing.verification_state),
                        (SkillMarketplaceListing.visibility == "public", "verified"),
                        else_="unverified",
                    ),
                }
            await db.execute(
                update(SkillMarketplaceListing)
                .where(SkillMarketplaceListing.version_id == version.id)
                .values(**values)
            )
        else:
            await db.execute(
                update(SkillMarketplaceListing)
                .where(
                    SkillMarketplaceListing.id.in_([listing.id for listing in owner_listings]),
                    SkillMarketplaceListing.visibility != "public",
                )
                .values(owner_state=new_owner_state)
            )
    elif layer == "platform":
        # The platform state hides every listing of every version, including
        # versions published later, until restored; is_yanked mirrors it.
        package.platform_state = {"yank": "yanked", "block": "blocked", "restore": None}[action]
        package.is_yanked = action != "restore"
    else:
        # The owner flag hides the package's NON-PUBLIC listings only,
        # including ones published later; public listings are unaffected.
        package.owner_state = new_owner_state

    state = {"yank": "yanked", "block": "blocked", "restore": "restored"}[action]
    actor_role = _actor_role(auth, listings, roles)
    block_scope: str | None = None
    if lift_quarantine:
        block_scope = "global"  # pairs with the rescan's global quarantine event
    elif action == "block" or (action == "restore" and current == "blocked"):
        block_scope = "global" if layer == "platform" else "org"
    elif layer == "owner":
        # Owner-layer yank (or restore of one): applies only to copies from
        # the owner's non-public listings, never to public ones.
        block_scope = "owner"
    event_listings = listings if layer == "platform" or lift_quarantine else owner_listings
    # Content events: one per (hash, scope) newly blocked, and one per
    # (hash, scope) whose LAST active block this restore lifted.
    content_events: list[tuple[str, str | None, str]] = [
        (content_hash, block_org, "blocked") for content_hash, block_org in written_blocks
    ]
    if lifted:
        await db.flush()
        for content_hash, block_org in sorted(
            {(row.content_hash, row.scope_org_id) for row in lifted}, key=lambda item: (item[0], item[1] or "")
        ):
            if not await _still_blocked(db, content_hash, block_org):
                content_events.append((content_hash, block_org, "restored"))

    # Every grant list this action can change - revoked grants, and grants on
    # versions whose deliverability changed - gets a new generation, so a
    # client paging it mid-change restarts instead of missing rows.
    touched_versions = affected_versions | (
        {version.id} if version is not None else set(version_ids)
    )
    # Lifting a content-hash block makes every copy of those bytes
    # deliverable again (and a new block hides them): include all of them.
    hash_changes = sorted(
        {content_hash for content_hash, _scope in written_blocks}
        | {row.content_hash for row in lifted}
    )
    touched_versions |= await _versions_with_hashes(db, hash_changes, None)
    await bump_grant_generations_for_versions(db, touched_versions)

    await _acquire_revocation_writer_lock(db)
    changed_at = await _next_changed_at(db)
    events: list[SkillRevocationEvent] = []

    def add_event(**values) -> None:
        nonlocal changed_at
        event = SkillRevocationEvent(
            reason=reason,
            actor_user_id=auth.user.id,
            actor_role=actor_role[:20],
            changed_at=changed_at,
            **values,
        )
        changed_at = changed_at + timedelta(microseconds=1)
        db.add(event)
        events.append(event)

    for visibility, org_id, owner_user_id in _scopes(event_listings or listings):
        add_event(
            kind="version" if version is not None else "package",
            package_id=package.id,
            version_id=version.id if version is not None else None,
            content_hash=version.content_hash if version is not None else None,
            state=state,
            block_scope=block_scope,
            scope_org_id=package.org_id if block_scope in ("org", "owner") else None,
            org_id=org_id,
            owner_user_id=owner_user_id,
            visibility=visibility,
        )
    # A platform-layer restore emits `restored` with a platform scope, which a
    # client applies to the whole platform layer of the target. Re-emit the
    # platform state that is still in force afterwards (a quarantine that a
    # block restore did not touch, or a yank that a quarantine lift did not),
    # so the client keeps the right flag. Additive: same event shape.
    if version is not None and action == "restore" and (layer == "platform" or lift_quarantine):
        remaining: list[tuple[str, str | None]] = []
        if lift_quarantine and any(listing.is_yanked for listing in listings):
            remaining.append(("yanked", None))
        elif not lift_quarantine and version.quarantined_at is not None:
            remaining.append(("quarantined", "global"))
        for remaining_state, remaining_scope in remaining:
            for visibility, org_id, owner_user_id in _scopes(listings):
                add_event(
                    kind="version",
                    package_id=package.id,
                    version_id=version.id,
                    content_hash=version.content_hash,
                    state=remaining_state,
                    block_scope=remaining_scope,
                    scope_org_id=None,
                    org_id=org_id,
                    owner_user_id=owner_user_id,
                    visibility=visibility,
                )
    for content_hash, scope_org_id, content_state in content_events:
        add_event(
            kind="content",
            package_id=None,
            version_id=None,
            content_hash=content_hash,
            state=content_state,
            block_scope="global" if scope_org_id is None else "org",
            scope_org_id=scope_org_id,
            source_package_id=package.id,
            source_version_id=version.id if version is not None else None,
            org_id=scope_org_id,
            owner_user_id=None,
            visibility="public" if scope_org_id is None else "org",
        )

    reports_resolved = 0
    if action != "restore":
        report_filter = [
            SkillPackageReport.package_id == package.id,
            SkillPackageReport.status == "open",
        ]
        if version is not None:
            report_filter.append(SkillPackageReport.version_id == version.id)
        # Only reports whose bound scope this actor has authority over.
        report_filter.append(_report_scope_predicate(auth, roles, moderation=True))
        resolved = await db.execute(
            update(SkillPackageReport)
            .where(*report_filter)
            .values(
                status="actioned",
                resolved_at=now,
                resolved_by_user_id=auth.user.id,
                resolution_note=f"{state}: {reason}",
            )
        )
        reports_resolved = resolved.rowcount or 0  # type: ignore[attr-defined]

    audit_payload = {
        "package_id": package.id,
        "version_id": version.id if version is not None else None,
        "content_hash": version.content_hash if version is not None else None,
        "state": state,
        "previous_state": current,
        "layer": layer,
        "reason": reason,
        "reports_resolved": reports_resolved,
        "grants_revoked": {k: v for k, v in grants.items() if not k.startswith("_") and k != "assignment_org_ids"},
        "affected_version_ids": sorted(affected_versions),
        "content_blocks": [
            {"content_hash": h, "scope_org_id": o, "state": st} for h, o, st in content_events
        ],
    }
    if package.org_id is not None:
        org = await db.get(Organization, package.org_id)
        if org is not None:
            # The source org sees its own package in full, but never other
            # tenants' version ids; the full list stays in the AdminAction.
            own_versions = set(
                (
                    await db.execute(
                        select(SkillPackageVersion.id)
                        .join(SkillPackage, SkillPackage.id == SkillPackageVersion.package_id)
                        .where(
                            SkillPackageVersion.id.in_(sorted(affected_versions)),
                            SkillPackage.org_id == package.org_id,
                        )
                    )
                ).scalars().all()
            ) if affected_versions else set()
            own_counts = grants.get("_per_org", {}).get(package.org_id, {})
            _audit(
                db,
                org=org,
                auth=auth,
                role=actor_role,
                event_type=f"skill_package_{state}",
                target_type="skill_package",
                target_id=package.id,
                after={
                    **audit_payload,
                    "affected_version_ids": sorted(own_versions),
                    "grants_revoked": {
                        "assignments": len(own_counts.get("assignments", [])),
                        "install_requests": len(own_counts.get("requests", [])),
                    },
                },
            )
    # A block on shared bytes revokes OTHER tenants' grants too. Each such
    # org gets a REDACTED record of only its own changes: a generic reason,
    # its own assignment / request / version ids, no source package or
    # version, no global affected list, and the platform as the actor (no
    # moderator identity). The full detail stays in the source org's audit
    # and the platform AdminAction record.
    for org_id, own in sorted(grants.get("_per_org", {}).items()):
        if org_id == package.org_id:
            continue
        org = await db.get(Organization, org_id)
        if org is None:
            continue
        db.add(
            OrgAuditEvent(
                id=f"oae_{secrets.token_hex(12)}",
                org_id=org.id,
                org_name_snapshot=org.name,
                event_type="skill_grants_revoked_by_moderation",
                actor_user_id=None,
                actor_email_snapshot=None,
                actor_role_at_time="system",
                target_type="skill_assignment" if own["assignments"] else "skill_install_request",
                target_id=(own["assignments"] or own["requests"] or [None])[0],
                after=json.dumps(
                    {
                        "reason": GENERIC_CONTENT_BLOCK_REASON,
                        "state": state,
                        "assignment_ids": own["assignments"],
                        "install_request_ids": own["requests"],
                        "version_ids": own["versions"],
                        "actor": {"actor_type": "system"},
                    },
                    sort_keys=True,
                ),
            )
        )
    if _is_platform_admin(auth):
        db.add(
            AdminAction(
                id=str(uuid.uuid4()),
                admin_id=auth.user.id,
                action=f"skill_{action}",
                target_type="skill_package",
                target_id=package.id,
                details=json.dumps(audit_payload, sort_keys=True),
            )
        )
    await db.commit()
    logger.info(
        "skill_moderation action=%s package=%s version=%s actor=%s role=%s",
        action, package.id, version.id if version is not None else None, auth.user.id, actor_role,
    )
    return {
        "package_id": package.id,
        "version_id": version.id if version is not None else None,
        "content_hash": version.content_hash if version is not None else None,
        "state": state,
        "previous_state": current,
        "reason": reason,
        "revocation_event_ids": [event.id for event in events],
        "reports_resolved": reports_resolved,
        "grants_revoked": {k: v for k, v in grants.items() if not k.startswith("_") and k != "assignment_org_ids"},
        "affected_version_ids": sorted(affected_versions),
    }


async def _validate_cleared_findings(
    db: AsyncSession, version: SkillPackageVersion, findings: list[FindingKey] | None
) -> set[tuple[str, str, str]]:
    """Which critical findings a quarantine restore clears.

    The moderator clears findings deliberately, never in bulk by accident:
    with exactly one open critical finding the list may be omitted; with more
    than one it is required and must name every open critical finding. A key
    that is not an open critical finding is a 422.

    A truncated latest scan is refused (409 scan_truncated): it stores only
    the capped subset of its findings, so clearing what it shows could
    restore a version whose omitted critical findings are never seen (and
    later scans reproduce the same capped subset).

    A latest scan made with an older ruleset is refused (409 scan_stale):
    the findings a moderator clears must come from the current detection
    rules, or a rule change could make the restore itself the bypass. Run a
    forced rescan first (it reaches quarantined versions whose scans are
    stale)."""
    scan = await latest_scan(db, version.id)
    if scan is None or scan.ruleset_version != RULESET_VERSION:
        raise HTTPException(
            409,
            detail={
                "code": "scan_stale",
                "message": (
                    "This version has no complete scan with the current detection ruleset. "
                    "Run a forced rescan, then restore from its findings."
                ),
                "current_state": "quarantined",
            },
        )
    if scan.truncated:
        raise HTTPException(
            409,
            detail={
                "code": "scan_truncated",
                "message": (
                    "The latest scan of this version was truncated, so not every critical finding "
                    "is listed. It cannot be restored as a false positive; block it or publish a "
                    "corrected version."
                ),
                "current_state": "quarantined",
            },
        )
    open_findings = await open_critical_findings(db, version)
    open_keys = {(item["rule_id"], item["path"], item["line_sha"]) for item in open_findings}
    if findings is None:
        if len(open_keys) > 1:
            raise _findings_required(open_findings)
        return open_keys
    given = {(item.rule_id, item.path, item.line_sha) for item in findings}
    unknown = given - open_keys
    if unknown:
        raise HTTPException(
            422,
            detail={
                "code": "unknown_finding",
                "message": "A listed finding is not an open critical finding of this version.",
            },
        )
    missing = open_keys - given
    if missing:
        raise _findings_required(
            [item for item in open_findings if (item["rule_id"], item["path"], item["line_sha"]) in missing]
        )
    return given


def _findings_required(findings: list[dict]) -> HTTPException:
    return HTTPException(
        409,
        detail={
            "code": "findings_required",
            "message": (
                "Restoring this quarantine clears its critical scan findings as false positives. "
                "Confirm each one by listing it in `findings`."
            ),
            "current_state": "quarantined",
            "findings": findings,
        },
    )


def _forbidden(message: str = "You cannot moderate this marketplace listing.") -> HTTPException:
    return HTTPException(403, detail={"code": "moderation_forbidden", "message": message})


def _conflict(code: str, message: str, current: str) -> HTTPException:
    return HTTPException(409, detail={"code": code, "message": message, "current_state": current})


def _moderation_route(action: Action, *, whole_package: bool):
    async def handler(
        target_id: str,
        body: ModerationRequest,
        auth: AuthContext = Depends(require_scope("skills:admin")),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        if whole_package:
            return await _moderate(db, auth, action=action, reason=body.reason, package_id=target_id)
        return await _moderate(
            db, auth, action=action, reason=body.reason, version_id=target_id, findings=body.findings
        )

    handler.__name__ = f"{action}_{'package' if whole_package else 'version'}"
    return handler


for _action in ("yank", "block", "restore"):
    router.add_api_route(
        f"/marketplace/packages/{{target_id}}/{_action}",
        _moderation_route(_action, whole_package=True),  # type: ignore[arg-type]
        methods=["POST"],
    )
    router.add_api_route(
        f"/marketplace/versions/{{target_id}}/{_action}",
        _moderation_route(_action, whole_package=False),  # type: ignore[arg-type]
        methods=["POST"],
    )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _report_response(row: SkillPackageReport, *, moderator: bool) -> dict:
    result = {
        "id": row.id,
        "package_id": row.package_id,
        "version_id": row.version_id,
        "reason": row.reason,
        "details": row.details,
        "status": row.status,
        "created_at": _utc(row.created_at).isoformat(),
        "resolved_at": _utc(row.resolved_at).isoformat() if row.resolved_at else None,
    }
    if moderator:
        result.update(
            {
                "reporter_user_id": row.reporter_user_id,
                "resolved_by_user_id": row.resolved_by_user_id,
                "resolution_note": row.resolution_note,
            }
        )
    return result


@router.post("/marketplace/packages/{package_id}/reports", status_code=201)
async def create_report(
    package_id: str,
    body: ReportCreate,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Report a package (or one of its versions) the caller can see.

    One OPEN report per (reporter, package): a second is a 409 until the first
    is actioned or dismissed. Rate limited per user: a per-process burst limit
    and a durable daily cap counted in the database."""
    _require_user_key(auth)
    if not _report_burst_limiter.is_allowed(f"report:{auth.user.id}"):
        raise _rate_limited()
    # Visibility is the catalog's own rule (published, not yanked/blocked,
    # caller-visible); anything else is the same constant 404.
    scope_kind, scope_org_id = await _report_scope(
        db, auth, package_id=package_id, version_id=body.version_id
    )
    # Serialize this reporter's quota check and insert, then count UNDER the
    # lock: concurrent reports can no longer all read "19" and all insert.
    await _lock_reporter(db, auth.user.id)
    since = datetime.now(timezone.utc) - timedelta(days=1)
    recent = (
        await db.execute(
            select(func.count(SkillPackageReport.id)).where(
                SkillPackageReport.reporter_user_id == auth.user.id,
                SkillPackageReport.created_at > since,
            )
        )
    ).scalar_one()
    if recent >= MAX_REPORTS_PER_DAY:
        raise _rate_limited()
    existing = (
        await db.execute(
            select(SkillPackageReport.id).where(
                SkillPackageReport.reporter_user_id == auth.user.id,
                SkillPackageReport.package_id == package_id,
                SkillPackageReport.status == "open",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise _duplicate_report(existing)
    report = SkillPackageReport(
        id=f"spr_{secrets.token_hex(12)}",
        package_id=package_id,
        version_id=body.version_id,
        reporter_user_id=auth.user.id,
        reason=body.reason,
        details=_sanitize(body.details, cap=MAX_REPORT_DETAILS_CHARS),
        status="open",
        scope_kind=scope_kind,
        scope_org_id=scope_org_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(report)
    try:
        await db.commit()
    except IntegrityError:
        # Lost a race with a concurrent identical report (partial unique index).
        await db.rollback()
        raise _duplicate_report(None) from None
    return _report_response(report, moderator=False)


def _reporter_lock_key(user_id: str) -> int:
    digest = hashlib.sha256(f"sessionfs-skill-report:{user_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def _lock_reporter(db: AsyncSession, user_id: str) -> None:
    """Per-reporter writer lock held until commit. PostgreSQL: a
    transaction-scoped advisory lock keyed on the user id. SQLite: a
    zero-row UPDATE opens the write transaction (the database write lock)."""
    connection = await db.connection()
    if connection.dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _reporter_lock_key(user_id)}
        )
    else:
        await db.execute(text("UPDATE skill_package_reports SET id = id WHERE 1 = 0"))


def _rate_limited() -> HTTPException:
    return HTTPException(
        429,
        detail={"code": "report_rate_limited", "message": "Too many reports. Try again later."},
    )


def _duplicate_report(report_id: str | None) -> HTTPException:
    return HTTPException(
        409,
        detail={
            "code": "report_already_open",
            "message": "You already have an open report for this package.",
            "report_id": report_id,
        },
    )


async def _report_scope(
    db: AsyncSession, auth: AuthContext, *, package_id: str, version_id: str | None
) -> tuple[str, str | None]:
    """Bind a report to the listings its reporter can see right now.

    Visibility is the catalog's own rule (published, not yanked/blocked,
    caller-visible); nothing visible is the same constant 404. The scope is
    what a moderator needs authority over (the same "authority over every
    affected listing" rule as _moderate): any public or personal listing in
    view -> 'platform' (platform admins); otherwise exactly one org's
    listings -> 'org' (that org's owners/admins). Listings of several orgs
    collapse to 'platform', the stricter authority."""
    org_ids = await _org_ids(db, auth)
    statement = (
        select(SkillMarketplaceListing)
        .join(SkillPackage, SkillPackage.id == SkillMarketplaceListing.package_id)
        .join(SkillPackageVersion, SkillPackageVersion.id == SkillMarketplaceListing.version_id)
        .where(
            *_deliverable_predicates(),
            _visibility_predicate(auth, org_ids),
            SkillPackage.id == package_id,
        )
    )
    if version_id is not None:
        statement = statement.where(SkillPackageVersion.id == version_id)
    listings = list((await db.execute(statement)).scalars().all())
    if not listings:
        raise HTTPException(404, "Marketplace package not found")
    if any(listing.visibility != "org" for listing in listings):
        return "platform", None
    orgs = {listing.org_id for listing in listings}
    if len(orgs) == 1:
        return "org", orgs.pop()
    return "platform", None


def _report_scope_predicate(auth: AuthContext, roles: dict[str, str], *, moderation: bool = False):
    """Reports whose bound scope this principal has authority over.

    Queue / dismiss: platform admins see 'platform' reports, org owners and
    admins see 'org' reports of their orgs. When resolving reports after a
    moderation action (``moderation=True``) a platform admin, who holds
    authority over every listing, resolves both."""
    scopes = []
    if _is_platform_admin(auth):
        if moderation:
            return SkillPackageReport.id.is_not(None)
        scopes.append(SkillPackageReport.scope_kind == "platform")
    admin_orgs = _admin_org_ids(roles)
    if admin_orgs:
        scopes.append(
            and_(
                SkillPackageReport.scope_kind == "org",
                SkillPackageReport.scope_org_id.in_(admin_orgs),
            )
        )
    if not scopes:
        return SkillPackageReport.id.is_(None)  # matches nothing
    return or_(*scopes)


def _moderated_package_predicate(auth: AuthContext, roles: dict[str, str]):
    """Queue / dismiss filter, or None when the caller moderates nothing."""
    if not _is_platform_admin(auth) and not _admin_org_ids(roles):
        return None
    return _report_scope_predicate(auth, roles)


@router.get("/marketplace/reports")
async def list_reports(
    status: Literal["open", "actioned", "dismissed", "all"] = Query("open"),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, max_length=512),
    auth: AuthContext = Depends(require_scope("skills:admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Moderation queue, newest first. A caller with no moderation authority
    gets an empty queue (never an error that would reveal anything)."""
    _require_user_key(auth)
    after = _decode_cursor(cursor)
    predicate = _moderated_package_predicate(auth, await _member_roles(db, auth))
    if predicate is None:
        return {"items": [], "count": 0, "has_more": False, "next_cursor": None}
    statement = select(SkillPackageReport).where(predicate)
    if status != "all":
        statement = statement.where(SkillPackageReport.status == status)
    if after is not None:
        created_at, report_id = after
        statement = statement.where(
            or_(
                SkillPackageReport.created_at < created_at,
                and_(SkillPackageReport.created_at == created_at, SkillPackageReport.id < report_id),
            )
        )
    statement = statement.order_by(
        SkillPackageReport.created_at.desc(), SkillPackageReport.id.desc()
    ).limit(limit + 1)
    rows = list((await db.execute(statement)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        _encode_cursor(_utc(rows[-1].created_at), rows[-1].id) if has_more and rows else None
    )
    return {
        "items": [_report_response(row, moderator=True) for row in rows],
        "count": len(rows),
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


@router.post("/marketplace/reports/{report_id}/dismiss")
async def dismiss_report(
    report_id: str,
    body: ReportDismiss,
    auth: AuthContext = Depends(require_scope("skills:admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_user_key(auth)
    note = _required_text(body.note, field="note")
    predicate = _moderated_package_predicate(auth, await _member_roles(db, auth))
    if predicate is None:
        raise HTTPException(404, "Report not found")
    row = (
        await db.execute(
            select(SkillPackageReport).where(SkillPackageReport.id == report_id, predicate)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Report not found")
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(SkillPackageReport)
        .where(SkillPackageReport.id == row.id, SkillPackageReport.status == "open")
        .values(
            status="dismissed",
            resolved_at=now,
            resolved_by_user_id=auth.user.id,
            resolution_note=note,
        )
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise HTTPException(
            409,
            detail={"code": "report_not_open", "message": "The report has already been resolved."},
        )
    package = await db.get(SkillPackage, row.package_id)
    if package is not None and package.org_id is not None:
        org = await db.get(Organization, package.org_id)
        if org is not None:
            roles = await _member_roles(db, auth)
            _audit(
                db,
                org=org,
                auth=auth,
                role="platform_admin" if _is_platform_admin(auth) else roles.get(org.id, "member"),
                event_type="skill_report_dismissed",
                target_type="skill_package_report",
                target_id=row.id,
                after={"package_id": row.package_id, "note": note},
            )
    await db.commit()
    refreshed = await db.get(SkillPackageReport, row.id)
    assert refreshed is not None
    await db.refresh(refreshed)
    return _report_response(refreshed, moderator=True)


# ---------------------------------------------------------------------------
# Revocations feed
# ---------------------------------------------------------------------------


def _parse_since(raw: str | None) -> datetime:
    if raw is None or not raw.strip():
        return _FEED_EPOCH
    value = raw.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, "since must be an ISO-8601 timestamp") from exc
    return _utc(parsed)


@router.get("/marketplace/revocations")
async def list_revocations(
    since: str | None = Query(None, max_length=64),
    limit: int = Query(100, ge=1, le=MAX_FEED_LIMIT),
    auth: AuthContext = Depends(require_scope("skills:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Yank / block / restore / quarantine events after `since`, oldest first.

    `state` is one of yanked, blocked, restored, quarantined (migration 069:
    an automated rescan stopped new installs; existing installs should warn,
    never uninstall). The set is additive: clients MUST ignore states they do
    not know (the v0.17 client does).

    Contract: ``{"revocations": [{"package_id", "version_id", "content_hash",
    "state", "reason", "changed_at", "kind", "block_scope", "scope_org_id"}],
    "next_since":
    "<iso>", "has_more": bool}``. Echo `next_since` back as `since` to
    continue; an empty page returns the same cursor.

    kind: 'version' (one version), 'package' (whole package; version_id and
    content_hash null) or 'content' (a blocked / unblocked content hash;
    package_id and version_id null, so a global block reaches tenants who
    never saw the moderated listing without disclosing it). block_scope:
    'global' (platform block, an automated instruction-scan quarantine, or
    restore of either), 'org' (an org owner/admin's
    block, or restore of one), 'owner' (an owner-layer yank, or restore of
    one, by an org owner/admin or a personal package's owner), or null (a
    platform yank, or restore of one). 'org' and 'owner' events apply ONLY to
    copies installed from a NON-PUBLIC listing (``visibility`` != 'public')
    of that package / version; an owner never takes down a public listing,
    which is exactly what the server hides and revokes. scope_org_id: the
    owning org for 'org' (always set) and 'owner' events (null for a
    personal package); clients additionally require ``owner_org_id`` to
    match when it is set. Null for 'global' and null block_scope.

    Cursor choice: `next_since` stays a plain ISO-8601 timestamp, and it is
    still exact under concurrent writers because every event writer holds one
    transaction-scoped lock from reading max(changed_at) until commit and
    writes changed_at strictly greater than every committed event (unique
    index as backstop). So changed_at is a total order equal to the monotonic
    id, no two events share a timestamp, and a reader holding cursor T can
    never later see a new event at or before T. Paging on `changed_at > since`
    therefore never skips or repeats an event.

    Scope: version / package events for listings the caller can see (public,
    the caller's orgs, the caller's own personal listings; service keys:
    public + their org). Global content events reach every principal, with
    the moderator's reason replaced by a generic one unless the caller can
    see a moderated listing; org content events reach that org's members.
    """
    cursor = _parse_since(since)
    roles = await _member_roles(db, auth)
    scopes = [SkillRevocationEvent.visibility == "public"]
    if roles:
        scopes.append(
            and_(
                SkillRevocationEvent.visibility == "org",
                SkillRevocationEvent.org_id.in_(sorted(roles)),
            )
        )
    if auth.key_kind == "user":
        scopes.append(
            and_(
                SkillRevocationEvent.visibility == "user",
                SkillRevocationEvent.owner_user_id == auth.user.id,
            )
        )
    rows = list(
        (
            await db.execute(
                select(SkillRevocationEvent)
                .where(SkillRevocationEvent.changed_at > cursor, or_(*scopes))
                .order_by(SkillRevocationEvent.changed_at, SkillRevocationEvent.id)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    visible_sources = await _visible_content_sources(db, auth, roles, rows)
    revocations = []
    for row in rows:
        reason = row.reason
        if (
            row.kind == "content"
            and row.visibility == "public"
            and (row.source_version_id or row.source_package_id) not in visible_sources
        ):
            reason = GENERIC_CONTENT_BLOCK_REASON
        revocations.append(
            {
                "package_id": row.package_id,
                "version_id": row.version_id,
                "content_hash": row.content_hash,
                "state": row.state,
                "reason": reason,
                "changed_at": _utc(row.changed_at).isoformat(),
                "kind": row.kind,
                "block_scope": row.block_scope,
                "scope_org_id": row.scope_org_id,
            }
        )
    next_since = revocations[-1]["changed_at"] if revocations else cursor.isoformat()
    return {"revocations": revocations, "next_since": next_since, "has_more": has_more}


async def _visible_content_sources(
    db: AsyncSession,
    auth: AuthContext,
    roles: dict[str, str],
    rows: list[SkillRevocationEvent],
) -> set[str]:
    """Source version / package ids of global content events whose moderated
    listings this caller can see (any state). Platform admins see all."""
    version_ids = {
        row.source_version_id for row in rows
        if row.kind == "content" and row.visibility == "public" and row.source_version_id
    }
    package_ids = {
        row.source_package_id for row in rows
        if row.kind == "content" and row.visibility == "public"
        and not row.source_version_id and row.source_package_id
    }
    if not version_ids and not package_ids:
        return set()
    if _is_platform_admin(auth):
        return version_ids | package_ids
    can_see = [SkillMarketplaceListing.visibility == "public"]
    if roles:
        can_see.append(
            and_(
                SkillMarketplaceListing.visibility == "org",
                SkillMarketplaceListing.org_id.in_(sorted(roles)),
            )
        )
    if auth.key_kind == "user":
        can_see.append(
            and_(
                SkillMarketplaceListing.visibility == "user",
                SkillMarketplaceListing.owner_user_id == auth.user.id,
            )
        )
    listings = (
        await db.execute(
            select(SkillMarketplaceListing.version_id, SkillMarketplaceListing.package_id).where(
                or_(
                    SkillMarketplaceListing.version_id.in_(sorted(version_ids)),
                    SkillMarketplaceListing.package_id.in_(sorted(package_ids)),
                ),
                or_(*can_see),
            )
        )
    ).all()
    visible: set[str] = set()
    for listing_version_id, listing_package_id in listings:
        if listing_version_id in version_ids:
            visible.add(listing_version_id)
        if listing_package_id in package_ids:
            visible.add(listing_package_id)
    return visible

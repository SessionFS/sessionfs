"""Marketplace discovery and freshness (marketplace v2 §9, migration 076).

- ``GET /api/v1/marketplace/categories``: the curated taxonomy.
- ``GET /api/v1/marketplace/packages/{id}/readme``: the package README (or
  the first SKILL.md body) as untrusted markdown with hidden / bidirectional
  characters made visible and reported. Clients render it without raw HTML.
- ``GET /api/v1/marketplace/versions/{id}/assets?path=``: a raster image
  from inside the package (PNG / JPEG / GIF / WebP, magic-byte checked, no
  SVG), for README images; same visibility as a download.
- ``POST /api/v1/marketplace/versions/{id}/events``: install, uninstall,
  active (reconcile ping) and translation-failure counts by target tool,
  deduplicated per principal per day; aggregates only.
- ``GET /api/v1/publishers/{id}/analytics``: publisher analytics (managers
  only; Starter+ for personal publishers, Team+ for org publishers); every
  count k-floored, consuming orgs never identified.
- ``PATCH /api/v1/publishers/{id}/packages/{package_id}``: categories,
  deprecation with a same-publisher successor, README path.
- ``GET /api/v1/orgs/{org_id}/skills/usage``: the org's own members' usage
  (org owners / admins, Team+).
- ``POST /api/v1/admin/marketplace/freshness/sweep``: the daily freshness
  sweeper (platform admin; Cloud Scheduler).
- ``POST /api/v1/admin/marketplace/works-in-results`` and
  ``POST /api/v1/admin/marketplace/source-status``: intake for the sandboxed
  works-in harness and the source-repository archive check.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    require_admin,
    require_platform_sweeper,
    require_scope,
    sweep_actor_details,
)
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    AdminAction,
    OrgMember,
    Project,
    SkillPackage,
    SkillPackageCategory,
    User,
)
from sessionfs.server.routes import marketplace as mp
from sessionfs.server.routes import marketplace_publishers as pubs
from sessionfs.server.routes.marketplace_moderation import _UNSAFE_CHARS_RE
from sessionfs.server.services import skill_discovery as svc
from sessionfs.skills import discovery
from sessionfs.skills.models import SUPPORTED_TOOLS

router = APIRouter(prefix="/api/v1", tags=["skill-marketplace-discovery"])


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def _busy() -> HTTPException:
    """Every archive-read slot of this process is taken: retry shortly."""
    return HTTPException(
        503,
        detail={"code": "discovery_busy", "message": "Package files are busy; retry shortly."},
        headers={"Retry-After": str(svc.DISCOVERY_RETRY_AFTER_SECONDS)},
    )


@router.get("/marketplace/categories")
async def list_categories(
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    items = await svc.list_categories(db)
    await db.commit()
    return {"items": items, "count": len(items), "max_per_package": discovery.MAX_CATEGORIES_PER_PACKAGE}


@router.get("/marketplace/packages/{package_id}/readme")
async def get_package_readme(
    package_id: str,
    request: Request,
    version_id: str | None = Query(None, max_length=64),
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The README of the package's current visible version (or of
    ``version_id``). Untrusted publisher text: render without raw HTML,
    with nofollow links and package-only images."""
    if version_id:
        listing, package, version = await mp._visible_row(db, auth, version_id=version_id)
        if package.id != package_id:
            raise HTTPException(404, "Marketplace package not found")
    else:
        listing, package, version = await mp._visible_row(db, auth, package_id=package_id)
    try:
        result = await svc.load_readme(mp._blob_store(request), package, version)
    except svc.DiscoveryBusy:
        raise _busy() from None
    result["package_id"] = package.id
    result["image_path_prefix"] = f"/api/v1/marketplace/versions/{version.id}/assets?path="
    await mp._revalidate_delivery(db, auth, version)
    return result


@router.get("/marketplace/versions/{version_id}/assets")
async def get_version_asset(
    version_id: str,
    request: Request,
    path: str = Query(..., min_length=1, max_length=300),
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> Response:
    _, _, version = await mp._visible_row(db, auth, version_id=version_id)
    if not discovery.valid_package_path(path):
        raise HTTPException(404, "Asset not found")
    try:
        loaded = await svc.load_image(mp._blob_store(request), version, path)
    except svc.DiscoveryBusy:
        raise _busy() from None
    if loaded is None:
        raise HTTPException(404, "Asset not found")
    await mp._revalidate_delivery(db, auth, version)
    data, media_type = loaded
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": "inline",
        },
    )


# ---------------------------------------------------------------------------
# Install events
# ---------------------------------------------------------------------------


class InstallEventBody(BaseModel):
    event: Literal["install", "uninstall", "active", "translation_failure"]
    tools: list[str] = Field(..., min_length=1, max_length=len(SUPPORTED_TOOLS))
    source: Literal["download", "reconcile", "native"] = "download"
    project_id: str | None = Field(default=None, max_length=64)

    @field_validator("tools")
    @classmethod
    def _known_tools(cls, value: list[str]) -> list[str]:
        unknown = sorted({tool for tool in value if tool not in SUPPORTED_TOOLS})
        if unknown:
            raise ValueError(f"Unsupported tool(s): {', '.join(unknown)}")
        return svc.unique_tools(value)


async def _consuming_org(db: AsyncSession, auth: AuthContext, project_id: str | None) -> str | None:
    """The org whose usage this event counts toward (never shown to the
    publisher): the service key's org; else the project's org when the
    caller is a member; else the user's default org or only membership."""
    if auth.key_kind == "service":
        return auth.org_id
    memberships = set(
        (await db.execute(select(OrgMember.org_id).where(OrgMember.user_id == auth.user.id))).scalars().all()
    )
    if project_id:
        project = await db.get(Project, project_id)
        if project is not None and project.org_id in memberships:
            return project.org_id
    default = getattr(auth.user, "default_org_id", None)
    if default in memberships:
        return default
    return next(iter(memberships)) if len(memberships) == 1 else None


@router.post("/marketplace/versions/{version_id}/events", status_code=202)
async def record_install_events(
    version_id: str,
    body: InstallEventBody,
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Count an install-lifecycle event for each target tool. Each principal
    counts at most once per (version, tool, event, day); repeats are accepted
    and ignored. Only aggregates are kept. An uninstall of a version the
    caller can no longer see is accepted and not counted (no visibility
    oracle)."""
    try:
        listing, package, version = await mp._visible_row(db, auth, version_id=version_id)
    except HTTPException as exc:
        if exc.status_code == 404 and body.event == "uninstall":
            return {"version_id": version_id, "event": body.event, "counted": 0}
        raise
    # A private package's usage never reaches another org's usage view: an
    # org-private listing counts only toward its owning org, a personal one
    # toward no org; only a public listing is attributed to the caller's org.
    if listing.visibility == "org":
        org_id: str | None = listing.org_id
    elif listing.visibility == "user":
        org_id = None
    else:
        org_id = await _consuming_org(db, auth, body.project_id)
    principal = auth.service_key_id if auth.key_kind == "service" and auth.service_key_id else auth.user.id
    counted = 0
    for tool in body.tools:
        if await svc.record_event(
            db,
            package_id=package.id,
            version_id=version.id,
            tool=tool,
            event=body.event,
            source=body.source,
            actor_type="service" if auth.key_kind == "service" else "user",
            principal_id=str(principal),
            org_id=org_id,
        ):
            counted += 1
    await db.commit()
    return {"version_id": version.id, "event": body.event, "counted": counted}


# ---------------------------------------------------------------------------
# Publisher analytics and package settings
# ---------------------------------------------------------------------------


async def _require_analytics_tier(db: AsyncSession, auth: AuthContext, publisher, role: str, org) -> None:
    """D6 / §9.6: personal publisher analytics need Starter+
    (marketplace_access); org publisher analytics need Team+
    (team_skill_library)."""
    if role == "platform_admin":
        return
    if publisher.owner_type == "user":
        await pubs._require_personal_tier(db, auth.user)
    elif org is not None:
        await mp._check_org_feature(db, org, "team_skill_library")


@router.get("/publishers/{publisher_id}/analytics")
async def get_publisher_analytics(
    publisher_id: str,
    days: int = Query(30, description="Window: 7, 30 or 90 days"),
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    # Fixed windows only: arbitrary windows could be differenced (day N vs
    # N+1) to recover a single day's hidden count.
    if days not in discovery.ANALYTICS_WINDOWS:
        raise HTTPException(
            422,
            detail={"code": "invalid_window", "message": "days must be 7, 30 or 90"},
        )
    publisher, role, org = await pubs._manage(db, auth, publisher_id, write=False)
    await _require_analytics_tier(db, auth, publisher, role, org)
    return await svc.publisher_analytics(db, publisher, days=days)


class PackageSettingsBody(BaseModel):
    categories: list[str] | None = Field(default=None, max_length=discovery.MAX_CATEGORIES_PER_PACKAGE)
    deprecated: bool | None = None
    deprecation_message: str | None = Field(default=None, max_length=500)
    successor_package_id: str | None = Field(default=None, max_length=64)
    readme_path: str | None = Field(default=None, max_length=300)

    @field_validator("categories")
    @classmethod
    def _known_categories(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = sorted({slug for slug in value if slug not in discovery.CATEGORY_SLUGS})
        if unknown:
            raise ValueError(f"Unknown category: {', '.join(unknown)}")
        if len(set(value)) > discovery.MAX_CATEGORIES_PER_PACKAGE:
            raise ValueError(f"At most {discovery.MAX_CATEGORIES_PER_PACKAGE} categories")
        return value

    @field_validator("deprecation_message")
    @classmethod
    def _plain_message(cls, value: str | None) -> str | None:
        """Shown in terminals and the dashboard: refuse control characters
        (C0 / C1, including newlines and tabs), zero-width and bidi
        characters instead of silently dropping them."""
        if value is None:
            return None
        if (
            _UNSAFE_CHARS_RE.search(value)
            or discovery.has_hidden_characters(value)
            or any(char in value for char in "\n\r\t")
        ):
            raise ValueError(
                "deprecation_message must be a single line without control, zero-width or bidirectional characters"
            )
        return value

    @field_validator("readme_path")
    @classmethod
    def _safe_path(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return value
        if not discovery.valid_package_path(value):
            raise ValueError("readme_path must be a relative path inside the package")
        return value


def _settings_response(package: SkillPackage, categories: list[str]) -> dict:
    return {
        "package_id": package.id,
        "namespace": package.namespace,
        "name": package.name,
        "categories": categories,
        "deprecated_at": package.deprecated_at.isoformat() if package.deprecated_at else None,
        "deprecation_message": package.deprecation_message,
        "successor_package_id": package.successor_package_id,
        "readme_path": package.readme_path,
    }


@router.patch("/publishers/{publisher_id}/packages/{package_id}")
async def update_package_settings(
    publisher_id: str,
    package_id: str,
    body: PackageSettingsBody,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Publisher-managed discovery settings of one package. Platform admins
    (moderators) may change any package's categories. A successor must be a
    different package under the SAME publisher (deprecation-hijack defence)."""
    publisher, role, org = await pubs._manage(db, auth, publisher_id)
    package = await db.get(SkillPackage, package_id)
    if package is None or package.publisher_id != publisher.id:
        raise HTTPException(404, "Package not found")
    changes: dict[str, Any] = {}
    fields = body.model_fields_set
    if "categories" in fields and body.categories is not None:
        changes["categories"] = await svc.set_package_categories(db, package.id, body.categories)
    if "successor_package_id" in fields:
        successor_id = body.successor_package_id or None
        if successor_id is not None:
            if successor_id == package.id:
                raise HTTPException(422, "A package cannot succeed itself")
            successor = await db.get(SkillPackage, successor_id)
            if successor is None or successor.publisher_id != publisher.id:
                raise HTTPException(
                    422,
                    detail={
                        "code": "successor_not_owned",
                        "message": "The successor must be a package of the same publisher.",
                    },
                )
        package.successor_package_id = successor_id
        changes["successor_package_id"] = successor_id
    if "deprecated" in fields and body.deprecated is not None:
        if body.deprecated:
            if package.deprecated_at is None:
                package.deprecated_at = datetime.now(timezone.utc)
        else:
            package.deprecated_at = None
            package.deprecation_message = None
            package.successor_package_id = None
        changes["deprecated"] = body.deprecated
    if "deprecation_message" in fields and (package.deprecated_at is not None or body.deprecated):
        package.deprecation_message = (body.deprecation_message or "").strip() or None
        changes["deprecation_message"] = package.deprecation_message
    if "readme_path" in fields:
        package.readme_path = body.readme_path or None
        changes["readme_path"] = package.readme_path
    if role == "platform_admin":
        db.add(
            AdminAction(
                id=str(uuid.uuid4()),
                admin_id=auth.user.id,
                action="skill_package_settings",
                target_type="skill_package",
                target_id=package.id,
                details=json.dumps(changes, sort_keys=True, default=str),
            )
        )
    else:
        pubs._org_audit(
            db,
            org=org,
            auth=auth,
            role=role,
            event_type="skill_package_settings_changed",
            target_type="skill_package",
            target_id=package.id,
            after=changes,
        )
    await db.commit()
    categories = list(
        (
            await db.execute(
                select(SkillPackageCategory.category_slug)
                .where(SkillPackageCategory.package_id == package.id)
                .order_by(SkillPackageCategory.category_slug)
            )
        ).scalars().all()
    )
    return _settings_response(package, categories)


# ---------------------------------------------------------------------------
# Org usage
# ---------------------------------------------------------------------------


@router.get("/orgs/{org_id}/skills/usage")
async def get_org_skill_usage(
    org_id: str,
    days: int = Query(30, ge=1, le=90),
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org, role = await mp._org_access(db, auth, org_id, admin=True)
    if role != "platform_admin":
        await mp._check_org_feature(db, org, "team_skill_library")
    return await svc.org_usage(db, org.id, days=days)


# ---------------------------------------------------------------------------
# Scheduler / harness intake (platform admin)
# ---------------------------------------------------------------------------


class SweepBody(BaseModel):
    limit: int = Field(default=500, ge=1, le=2000)
    cursor: str | None = Field(default=None, min_length=1, max_length=64)
    time_budget_seconds: float = Field(default=20.0, ge=1.0, le=120.0)


def _admin_action(db: AsyncSession, admin: User, action: str, target_id: str, details: dict) -> None:
    db.add(
        AdminAction(
            id=str(uuid.uuid4()),
            admin_id=admin.id,
            action=action[:50],
            target_type="skill_package",
            target_id=target_id[:64],
            details=json.dumps(details, sort_keys=True, default=str),
        )
    )


@router.post("/admin/marketplace/freshness/sweep")
async def sweep_freshness(
    body: SweepBody | None = None,
    auth: AuthContext = Depends(require_platform_sweeper()),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Recompute stale / unmaintained flags, last updated per tool, ranking
    inputs, and prune install dedupe rows older than 30 days. Idempotent and
    resumable: call daily (Cloud Scheduler); pass ``next_cursor`` back until
    ``done``. Platform-admin user key or a ``platform_sweeps:run`` service
    key."""
    body = body or SweepBody()
    result = await svc.sweep_freshness(
        db, limit=body.limit, cursor=body.cursor, time_budget_seconds=body.time_budget_seconds
    )
    _admin_action(
        db, auth.user, "skill_freshness_sweep", "bulk",
        {**{k: result[k] for k in ("processed", "stale", "unmaintained", "pruned_dedupe_rows", "done")},
         **sweep_actor_details(auth)},
    )
    await db.commit()
    return result


class WorksInResult(BaseModel):
    version_id: str = Field(..., min_length=1, max_length=64)
    tool: str
    passed: bool
    checked_at: datetime | None = None
    detail: str | None = Field(default=None, max_length=300)

    @field_validator("tool")
    @classmethod
    def _tool(cls, value: str) -> str:
        if value not in SUPPORTED_TOOLS:
            raise ValueError("Unsupported tool")
        return value


class WorksInBody(BaseModel):
    results: list[WorksInResult] = Field(..., min_length=1, max_length=200)


@router.post("/admin/marketplace/works-in-results")
async def record_works_in_results(
    body: WorksInBody,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Intake for the sandboxed works-in harness (it runs as a separate job,
    never in the API). Results for a package's current version set its
    per-tool verified / regressed standing; the next sweep turns a
    regression older than 180 days without a new version into a stale flag."""
    outcomes = []
    for item in sorted(body.results, key=lambda r: (r.version_id, r.tool)):
        outcomes.append(
            await svc.record_tool_verification(
                db, version_id=item.version_id, tool=item.tool, passed=item.passed,
                checked_at=item.checked_at, detail=item.detail,
            )
        )
    _admin_action(db, admin, "skill_works_in_results", "bulk", {"results": len(outcomes), "recorded": sum(1 for o in outcomes if o["recorded"])})
    await db.commit()
    return {"results": outcomes}


class SourceStatus(BaseModel):
    package_id: str = Field(..., min_length=1, max_length=64)
    archived: bool


class SourceStatusBody(BaseModel):
    items: list[SourceStatus] = Field(..., min_length=1, max_length=500)


@router.post("/admin/marketplace/source-status")
async def record_source_status(
    body: SourceStatusBody,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record whether each package's source repository is archived (from the
    scheduled GitHub check). Counts toward "stale" only for verified GitHub
    publishers."""
    updated = 0
    for item in sorted(body.items, key=lambda i: i.package_id):
        updated += int(await svc.set_source_archived(db, item.package_id, item.archived))
    _admin_action(db, admin, "skill_source_status", "bulk", {"items": len(body.items), "updated": updated})
    await db.commit()
    return {"updated": updated}

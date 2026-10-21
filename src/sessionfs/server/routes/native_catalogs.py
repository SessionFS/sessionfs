"""Native per-tool marketplace catalogs and "works in" verification
(marketplace v2 §7.6, tk_86084118c8ee453c).

- ``GET /api/v1/marketplace/catalogs``: the caller's catalogs with clone
  URLs and each tool's own add/install commands.
- ``GET /api/v1/git/<scope>/<repo>.git/<path>``: read-only "dumb HTTP" git
  (HEAD, info/refs, objects/info/packs, the current pack). Authenticated on
  every request: HTTP Basic (any username, a ``marketplace:read`` API key as
  the password, which is what ``sfs catalog credential`` hands git) or a
  Bearer key. Never a key in the URL.
- ``GET /api/v1/marketplace/versions/{id}/works-in``: per-tool evidence.
  Private (non-public) packages: Team+ (D6).
- ``POST /api/v1/orgs/{org_id}/skills/catalog/export``: record the org's
  customer-owned mirror remote (D4b). The push itself is done by
  ``sfs catalog export`` with the customer's own git credentials.
- ``POST /api/v1/orgs/{org_id}/skills/verify``: an org admin queues harness
  runs for the org's packages (Team+, per-org daily quota).
- Platform admin (the scheduler calls these):
  ``POST /api/v1/admin/skills/verify`` (L0 now, queue L1),
  ``POST /api/v1/admin/skills/catalogs/regenerate``.
- Harness protocol (a ``skills_verify:run`` service key minted by a
  platform admin, or a platform-admin user key for manual runs):
  ``POST /api/v1/admin/skills/verify/jobs/claim``,
  ``GET  /api/v1/admin/skills/verify/jobs/{id}/projection`` (``410`` and the
  job ends ``skipped`` when the package is no longer deliverable),
  ``POST /api/v1/admin/skills/verify/jobs/{id}/result`` (``status=error``
  re-queues the job; after ``MAX_JOB_ATTEMPTS`` it ends ``errored``).

The harness that RUNS a tool never runs inside the API: it is the opt-in
``sfs catalog harness`` job, sandboxed per §7.4.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request as StarletteRequest

from sessionfs.server.auth.dependencies import (
    AuthContext,
    assert_platform_key_minter_active,
    get_rate_limiter,
    require_platform_sweeper,
    require_scope,
    sweep_actor_details,
)
from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    AdminAction,
    SkillFeedCatalog,
    SkillMarketplaceListing,
    SkillPackage,
    SkillPackageVersion,
    SkillToolVerification,
    SkillVerificationJob,
)
from sessionfs.server.routes.marketplace import (
    _blob_store,
    _check_org_feature,
    _deliverable_predicates,
    _org_access,
    _org_ids,
    _visible_row,
)
from sessionfs.server.services import native_catalogs as service
from sessionfs.server.tier_gate import get_effective_tier
from sessionfs.server.tiers import get_features_for_tier
from sessionfs.skills import catalog_mirror, native_feeds

router = APIRouter(prefix="/api/v1", tags=["skill-native-catalogs"])

HARNESS_TOOLS = ("claude-code", "copilot", "codex", "cursor", "gemini", "antigravity")
HARNESS_SCOPE = "skills_verify:run"
MAX_JOB_ATTEMPTS = 3
_CLAIM_TTL = timedelta(minutes=30)
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PLUGIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_GIT_FILE_RE = re.compile(
    r"^(HEAD|info/refs|objects/info/packs|objects/pack/pack-[0-9a-f]{40}\.(pack|idx))$"
)


def _base_url(request: Request) -> str:
    config = getattr(request.app.state, "config", None)
    configured = str(getattr(config, "public_api_url", "") or "").strip().rstrip("/")
    return configured or str(request.base_url).rstrip("/")


def _homepage_base(request: Request) -> str:
    config = getattr(request.app.state, "config", None)
    app_url = str(getattr(config, "app_url", "") or "").strip().rstrip("/")
    return app_url or "https://app.sessionfs.dev"


def _public_mirror(request: Request) -> str | None:
    """The configured public GitHub mirror (``owner/repo``) or None. The
    config validator already refused anything else at startup."""
    config = getattr(request.app.state, "config", None)
    return catalog_mirror.parse_mirror(str(getattr(config, "public_catalog_mirror", "") or ""))


def _clone_path(scope_type: str, scope_id: str | None) -> str:
    if scope_type == "public":
        return "/api/v1/git/public/sessionfs.git"
    return f"/api/v1/git/{scope_type}/{scope_id}/catalog.git"


def _add_commands(clone_url: str, marketplace: str, *, public: bool, mirror: str | None = None) -> dict[str, Any]:
    """Each tool's OWN command (vendor docs checked 2026-10-20).

    With a public GitHub ``mirror`` (public scope only), each tool whose docs
    accept a GitHub ``owner/repo`` or repository URL leads with that keyless
    form (``catalog_mirror.MIRROR_VENDOR_SOURCES``, checked 2026-10-21) and
    the SessionFS-hosted URL moves to ``add_hosted``. Gemini stays on the
    SessionFS host: its per-plugin repositories are not mirrored."""
    commands: dict[str, Any] = {
        "claude-code": {
            "add": f"/plugin marketplace add {clone_url}",
            "cli": f"claude plugin marketplace add {clone_url}",
            "install": f"/plugin install <plugin>@{marketplace}",
        },
        "copilot": {
            "add": f"copilot plugin marketplace add {clone_url}",
            "install": f"copilot plugin install <plugin>@{marketplace}",
        },
        "codex": {
            "add": f"codex plugin marketplace add {clone_url}",
            "install": "Install from the Codex app's Plugins Directory",
        },
        "cursor": {
            "add": (
                "Cursor team marketplaces import from GitHub, GitLab, Bitbucket or Azure DevOps: "
                "mirror this catalog with `sfs catalog export --to <remote>`, then import it in "
                "Dashboard > Plugins & MCPs > Team Marketplaces > Add Marketplace > Import from Repo"
            ),
        },
        "antigravity": {
            "add": (
                "Antigravity supports only its official marketplace: clone this catalog and run "
                "`agy plugin install <clone>/plugins/<plugin>`"
            ),
        },
    }
    if public and mirror:
        github = catalog_mirror.https_url(mirror)
        for tool, add in (
            ("claude-code", f"/plugin marketplace add {mirror}"),
            ("copilot", f"copilot plugin marketplace add {mirror}"),
            ("codex", f"codex plugin marketplace add {mirror}"),
        ):
            hosted = commands[tool]
            commands[tool] = {"add": add, **{k: v for k, v in hosted.items() if k != "add"},
                              "add_hosted": hosted["add"]}
        commands["claude-code"]["cli"] = f"claude plugin marketplace add {mirror}"
        commands["cursor"] = {
            "add": f"Dashboard > Plugins & MCPs > Team Marketplaces > Add Marketplace > Import from Repo: {github}",
        }
        commands["antigravity"] = {
            "add": (
                f"Antigravity supports only its official marketplace: clone {github} and run "
                "`agy plugin install <clone>/plugins/<plugin>`"
            ),
        }
    if public:
        commands["gemini"] = {
            "install": f"gemini extensions install {clone_url.rsplit('/', 1)[0]}/plugins/<plugin>.git",
        }
    return commands


# ---------------------------------------------------------------------------
# Scope authorization
# ---------------------------------------------------------------------------


async def _user_has_feature(db: AsyncSession, auth: AuthContext, feature: str) -> bool:
    if auth.key_kind != "user":
        return False
    return feature in get_features_for_tier(await get_effective_tier(auth.user, db))


async def _authorize_scope(
    db: AsyncSession, auth: AuthContext, scope_type: str, scope_id: str | None
) -> None:
    """Constant 404 for a scope the caller cannot see; 403 upgrade_required
    for one they own but whose tier lacks the feature (§7.8)."""
    if scope_type == "public":
        return
    if scope_type == "user":
        if auth.key_kind != "user" or scope_id != auth.user.id:
            raise HTTPException(404, "Catalog not found")
        if not await _user_has_feature(db, auth, "marketplace_access"):
            raise HTTPException(
                403,
                detail={
                    "code": "upgrade_required",
                    "message": "Personal native catalogs need a plan with marketplace access (Starter+).",
                    "feature": "marketplace_access",
                },
            )
        return
    if scope_type == "org":
        if not scope_id:
            raise HTTPException(404, "Catalog not found")
        if auth.key_kind == "service":
            if auth.org_id != scope_id:
                raise HTTPException(404, "Catalog not found")
            org = await _org_for(db, scope_id)
        else:
            org, _role = await _org_access(db, auth, scope_id)
        await _check_org_feature(db, org, "team_skill_library")
        return
    raise HTTPException(404, "Catalog not found")


async def _org_for(db: AsyncSession, org_id: str):
    from sessionfs.server.db.models import Organization

    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, "Catalog not found")
    return org


# ---------------------------------------------------------------------------
# Catalog listing
# ---------------------------------------------------------------------------


@router.get("/marketplace/catalogs")
async def list_catalogs(
    request: Request,
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    base = _base_url(request)
    mirror = _public_mirror(request)
    scopes: list[tuple[str, str | None]] = [("public", None)]
    if auth.key_kind == "user" and await _user_has_feature(db, auth, "marketplace_access"):
        scopes.append(("user", auth.user.id))
    for org_id in sorted(await _org_ids(db, auth)):
        scopes.append(("org", org_id))
    rows = {
        row.scope_key: row
        for row in (
            await db.execute(
                select(SkillFeedCatalog).where(
                    SkillFeedCatalog.scope_key.in_([service.scope_key(t, i) for t, i in scopes])
                )
            )
        ).scalars()
    }
    items = []
    for scope_type, scope_id in scopes:
        row = rows.get(service.scope_key(scope_type, scope_id))
        name = native_feeds.marketplace_name(scope_type, scope_id)
        clone_url = base + _clone_path(scope_type, scope_id)
        items.append(
            {
                "scope": scope_type,
                "scope_id": scope_id,
                "marketplace": name,
                "clone_url": clone_url,
                "current_commit": row.current_commit_sha if row else None,
                "generated_at": row.generated_at.isoformat() if row and row.generated_at else None,
                "plugin_count": row.plugin_count if row else None,
                "export_remote": row.export_remote if row else None,
                # Keyless public GitHub mirror (D4b); public scope only.
                "mirror": (
                    {"repo": mirror, "url": catalog_mirror.https_url(mirror), "branch": "main"}
                    if mirror and scope_type == "public" else None
                ),
                "tools": _add_commands(clone_url, name, public=scope_type == "public", mirror=mirror),
            }
        )
    return {
        "items": items,
        "credential_helper": "sfs catalog credential",
        "vendor_docs_checked": native_feeds.VENDOR_DOCS_CHECKED,
        "unconfirmed": native_feeds.UNCONFIRMED,
    }


# ---------------------------------------------------------------------------
# Dumb-HTTP git
# ---------------------------------------------------------------------------


def _git_unauthorized() -> HTTPException:
    return HTTPException(
        401,
        detail="Authentication required",
        headers={"WWW-Authenticate": 'Basic realm="SessionFS catalog", charset="UTF-8"'},
    )


async def _git_auth(
    request: Request,
    db: AsyncSession = Depends(get_db),
    limiter: SlidingWindowRateLimiter = Depends(get_rate_limiter),
) -> AuthContext:
    """HTTP Basic (password = API key) or Bearer; a 401 always carries the
    Basic challenge so git asks its credential helper."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise _git_unauthorized() from None
        _, sep, key = decoded.partition(":")
        if not sep or not key:
            raise _git_unauthorized()
        scope = dict(request.scope)
        scope["headers"] = [
            (name, value) for name, value in request.scope["headers"] if name.lower() != b"authorization"
        ] + [(b"authorization", f"Bearer {key}".encode("latin-1", "replace"))]
        request = StarletteRequest(scope, request.receive)  # type: ignore[assignment]
    elif not header.startswith("Bearer "):
        raise _git_unauthorized()
    try:
        return await require_scope("marketplace:read")(request, db, limiter)
    except HTTPException as exc:
        if exc.status_code == 401:
            raise _git_unauthorized() from None
        raise


async def _serve_git(
    request: Request,
    db: AsyncSession,
    auth: AuthContext,
    scope_type: str,
    scope_id: str | None,
    path: str,
    *,
    plugin_id: str | None = None,
) -> Response:
    if not _GIT_FILE_RE.match(path):
        raise HTTPException(404, "Not found")
    await _authorize_scope(db, auth, scope_type, scope_id)
    store = _blob_store(request)
    unavailable = HTTPException(503, "Catalog is being generated; retry shortly", headers={"Retry-After": "30"})
    try:
        if path in {"info/refs", "HEAD"}:
            catalog = await service.ensure_catalog(
                db, store, scope_type, scope_id, homepage_base=_homepage_base(request),
                mirror=_public_mirror(request),
            )
        else:
            catalog = await service.fresh_catalog(
                db, store, scope_type, scope_id, homepage_base=_homepage_base(request),
                mirror=_public_mirror(request),
            )
    except service.CatalogUnavailable:
        raise unavailable from None
    state = await service.catalog_state(store, catalog)
    if state is None:
        raise unavailable
    data = await service.read_repo_file(store, catalog, state, path, plugin_id=plugin_id)
    if data is None:
        raise HTTPException(404, "Not found")
    immutable = path.startswith("objects/pack/")
    return Response(
        content=data,
        media_type="application/octet-stream" if immutable else "text/plain; charset=utf-8",
        headers={
            # Private catalogs must never sit in a shared cache.
            "Cache-Control": "private, max-age=31536000, immutable" if immutable else "no-store",
            "X-SessionFS-Catalog-Commit": catalog.current_commit_sha or "",
        },
    )


@router.get("/git/public/sessionfs.git/{path:path}")
async def git_public(path: str, request: Request, auth: AuthContext = Depends(_git_auth),
                     db: AsyncSession = Depends(get_db)) -> Response:
    return await _serve_git(request, db, auth, "public", None, path)


@router.get("/git/public/plugins/{plugin_id}.git/{path:path}")
async def git_public_plugin(plugin_id: str, path: str, request: Request,
                            auth: AuthContext = Depends(_git_auth),
                            db: AsyncSession = Depends(get_db)) -> Response:
    if not _PLUGIN_RE.match(plugin_id):
        raise HTTPException(404, "Not found")
    return await _serve_git(request, db, auth, "public", None, path, plugin_id=plugin_id)


@router.get("/git/{scope_type}/{scope_id}/catalog.git/{path:path}")
async def git_scoped(scope_type: Literal["org", "user"], scope_id: str, path: str, request: Request,
                     auth: AuthContext = Depends(_git_auth),
                     db: AsyncSession = Depends(get_db)) -> Response:
    if not _ID_RE.match(scope_id):
        raise HTTPException(404, "Catalog not found")
    return await _serve_git(request, db, auth, scope_type, scope_id, path)


# ---------------------------------------------------------------------------
# Works-in
# ---------------------------------------------------------------------------


async def _works_in_allowed(db: AsyncSession, auth: AuthContext, version_id: str) -> None:
    """Works-in on a private package is Team+ (D6): public listings are open;
    an org listing is visible only under team_skill_library already; a
    personal-only package needs the caller's own Team+ tier."""
    org_ids = await _org_ids(db, auth)
    visibilities = set(
        (
            await db.execute(
                select(SkillMarketplaceListing.visibility)
                .join(SkillPackage, SkillPackage.id == SkillMarketplaceListing.package_id)
                .join(SkillPackageVersion, SkillPackageVersion.id == SkillMarketplaceListing.version_id)
                .where(
                    *_deliverable_predicates(),
                    SkillPackageVersion.id == version_id,
                    or_(
                        SkillMarketplaceListing.visibility == "public",
                        and_(
                            SkillMarketplaceListing.visibility == "org",
                            SkillMarketplaceListing.org_id.in_(org_ids or [""]),
                        ),
                    ),
                )
            )
        ).scalars()
    )
    if visibilities:
        return
    if not await _user_has_feature(db, auth, "team_skill_library"):
        raise HTTPException(
            403,
            detail={
                "code": "upgrade_required",
                "message": "Works-in verification of private packages is available on Team and above.",
                "feature": "team_skill_library",
            },
        )


async def works_in_for_version(
    request: Request, db: AsyncSession, auth: AuthContext, version_id: str
) -> dict:
    row = await _visible_row(db, auth, version_id=version_id)
    await _works_in_allowed(db, auth, version_id)
    version = row[2]
    latest = await service.latest_verifications(db, version.id)
    if not any("L0" in levels for levels in latest.values()):
        # Static checks are pure and in-process: compute them on first view.
        projection = await service.single_version_projection(
            _blob_store(request), row, homepage_base=_homepage_base(request)
        )
        if projection is not None and projection.plugins:
            plugin = projection.plugins[0]
            await service.record_l0(
                db, {version.id: projection.verdicts[version.id]}, {version.id: plugin.projection_hash}
            )
            await db.commit()
            latest = await service.latest_verifications(db, version.id)
    return service.works_in_payload(version, latest)


@router.get("/marketplace/versions/{version_id}/works-in")
async def get_works_in(
    version_id: str,
    request: Request,
    auth: AuthContext = Depends(require_scope("marketplace:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await works_in_for_version(request, db, auth, version_id)


# ---------------------------------------------------------------------------
# Org export target + org verification requests (Team+)
# ---------------------------------------------------------------------------


class ExportTarget(BaseModel):
    remote: str | None = Field(None, max_length=500)


_REMOTE_RE = re.compile(r"^(https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/[A-Za-z0-9._~/-]+|git@[A-Za-z0-9.-]+:[A-Za-z0-9._~/-]+)$")


def _require_user(auth: AuthContext) -> None:
    if auth.key_kind != "user":
        raise HTTPException(403, detail={"code": "service_key_not_allowed",
                                         "message": "This action requires a user key."})


@router.post("/orgs/{org_id}/skills/catalog/export")
async def set_org_export(
    org_id: str,
    body: ExportTarget,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_user(auth)
    org, _role = await _org_access(db, auth, org_id, admin=True)
    await _check_org_feature(db, org, "team_skill_library")
    remote = (body.remote or "").strip() or None
    if remote is not None and (not _REMOTE_RE.match(remote) or ".." in remote):
        raise HTTPException(
            422, "remote must be an https:// or git@host: URL without credentials, query or fragment"
        )
    catalog = await service._catalog_row(db, "org", org.id, lock=True)
    catalog.export_remote = remote
    await db.commit()
    return {
        "org_id": org.id,
        "export_remote": remote,
        "command": f"sfs catalog export --org {org.id} --to {remote}" if remote else None,
    }


class VerifyRequest(BaseModel):
    version_ids: list[str] | None = Field(None, max_length=200)
    tools: list[str] | None = Field(None, max_length=10)
    limit: int = Field(50, ge=1, le=500)


def _tools(requested: list[str] | None) -> list[str]:
    tools = list(requested or HARNESS_TOOLS)
    unknown = sorted(set(tools) - set(HARNESS_TOOLS))
    if unknown:
        raise HTTPException(422, f"Unsupported tool(s): {', '.join(unknown)}")
    return tools


async def _enqueue(
    db: AsyncSession,
    *,
    version_id: str,
    tool: str,
    owner_org_id: str | None,
    requested_by: str | None,
    trigger: str,
) -> bool:
    existing = (
        await db.execute(
            select(SkillVerificationJob.id).where(
                SkillVerificationJob.version_id == version_id,
                SkillVerificationJob.tool == tool,
                SkillVerificationJob.level == "L1",
                SkillVerificationJob.status.in_(("queued", "claimed")),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False
    db.add(
        SkillVerificationJob(
            id=f"svj_{secrets.token_hex(12)}",
            version_id=version_id,
            tool=tool,
            level="L1",
            status="queued",
            trigger=trigger,
            owner_org_id=owner_org_id,
            requested_by_user_id=requested_by,
        )
    )
    await db.flush()
    return True


@router.post("/orgs/{org_id}/skills/verify")
async def org_verify(
    org_id: str,
    body: VerifyRequest,
    auth: AuthContext = Depends(require_scope("marketplace:write")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Queue harness runs for the org's own packages (Team+, quota/day)."""
    _require_user(auth)
    org, _role = await _org_access(db, auth, org_id, admin=True)
    await _check_org_feature(db, org, "team_skill_library")
    if not body.version_ids:
        raise HTTPException(422, "version_ids is required")
    tools = _tools(body.tools)
    await service._catalog_row(db, "org", org.id, lock=True)  # serializes quota checks per org
    since = datetime.now(timezone.utc) - timedelta(days=1)
    used = (
        await db.execute(
            # Only runs the org asked for: platform sweeps of its listings
            # never consume the org's quota.
            select(func.count(SkillVerificationJob.id)).where(
                SkillVerificationJob.owner_org_id == org.id,
                SkillVerificationJob.trigger == "org",
                SkillVerificationJob.created_at >= since,
            )
        )
    ).scalar_one()
    quota = service.harness_quota()
    owned = set(
        (
            await db.execute(
                select(SkillPackageVersion.id)
                .join(SkillPackage, SkillPackage.id == SkillPackageVersion.package_id)
                .where(SkillPackageVersion.id.in_(body.version_ids), SkillPackage.org_id == org.id)
            )
        ).scalars()
    )
    queued: list[dict] = []
    for version_id in body.version_ids:
        if version_id not in owned:
            raise HTTPException(404, "Marketplace package not found")
        for tool in tools:
            if tool in native_feeds.PUBLIC_ONLY_TOOLS:
                continue  # org-private packages are never in the public mirror
            if used >= quota:
                await db.commit()
                raise HTTPException(
                    429,
                    detail={
                        "code": "verification_quota_exceeded",
                        "message": f"This organization may queue {quota} verification runs per day.",
                        "queued": queued,
                    },
                )
            if await _enqueue(db, version_id=version_id, tool=tool, owner_org_id=org.id,
                              requested_by=auth.user.id, trigger="org"):
                used += 1
                queued.append({"version_id": version_id, "tool": tool})
    await db.commit()
    return {"queued": queued, "quota": quota, "used": used}


# ---------------------------------------------------------------------------
# Platform admin: sweeper + harness protocol
# ---------------------------------------------------------------------------


def _require_platform_admin(auth: AuthContext) -> None:
    if auth.key_kind != "user" or auth.user.tier != "admin":
        raise HTTPException(403, "Admin access required")


async def _require_harness(auth: AuthContext, db: AsyncSession) -> None:
    """The harness protocol: a ``skills_verify:run`` service key (it reaches
    no other route) whose minter is a platform admin, or a platform-admin
    user key for a manual run. An org admin's ``skills_verify:run`` key is
    refused: jobs span every org's packages. The key stops working when its
    owner or minter is no longer an active platform admin."""
    if auth.user.tier != "admin":
        raise HTTPException(403, "Admin access required")
    if auth.key_kind == "service" and HARNESS_SCOPE not in auth.scopes:
        raise HTTPException(403, "Admin access required")
    # The run-as user and the minter (or last rotator) must both still be
    # active platform admins.
    await assert_platform_key_minter_active(
        auth, db, error="platform_key_minter_not_admin", scope=HARNESS_SCOPE
    )


@router.post("/admin/skills/verify")
async def admin_verify(
    body: VerifyRequest,
    request: Request,
    auth: AuthContext = Depends(require_platform_sweeper()),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Recompute L0 in-process and queue L1 harness jobs. Without
    ``version_ids`` it sweeps deliverable public versions and org versions
    of entitled orgs that have no current L1 for a tool (bounded).
    Platform-admin user key or a ``platform_sweeps:run`` service key."""
    tools = _tools(body.tools)
    store = _blob_store(request)
    statement = (
        select(SkillMarketplaceListing, SkillPackage, SkillPackageVersion)
        .join(SkillPackage, SkillPackage.id == SkillMarketplaceListing.package_id)
        .join(SkillPackageVersion, SkillPackageVersion.id == SkillMarketplaceListing.version_id)
        .where(*_deliverable_predicates(), SkillMarketplaceListing.visibility.in_(("public", "org")))
        .order_by(SkillMarketplaceListing.published_at.desc(), SkillMarketplaceListing.id.desc())
    )
    if body.version_ids:
        statement = statement.where(SkillPackageVersion.id.in_(body.version_ids))
    rows = list((await db.execute(statement)).all())
    seen: set[str] = set()
    picked: list[tuple] = []
    for row in rows:
        if row[2].id in seen:
            continue
        if row[0].visibility == "org":
            from sessionfs.server.routes.marketplace import _orgs_with_feature

            listing_org = row[0].org_id
            if not listing_org or not await _orgs_with_feature(db, [listing_org], "team_skill_library"):
                continue
        seen.add(row[2].id)
        picked.append(row)
        if len(picked) >= body.limit:
            break
    l0 = 0
    queued = 0
    for row in picked:
        projection = await service.single_version_projection(store, row, homepage_base=_homepage_base(request))
        if projection is not None and projection.plugins:
            version_id = row[2].id
            l0 += await service.record_l0(
                db, {version_id: projection.verdicts[version_id]},
                {version_id: projection.plugins[0].projection_hash},
            )
        for tool in tools:
            if tool in native_feeds.PUBLIC_ONLY_TOOLS and row[0].visibility != "public":
                continue
            if await _enqueue(db, version_id=row[2].id, tool=tool,
                              owner_org_id=row[1].org_id if row[0].visibility == "org" else None,
                              requested_by=auth.user.id, trigger="admin" if body.version_ids else "sweep"):
                queued += 1
    db.add(
        AdminAction(
            id=str(uuid.uuid4()),
            admin_id=auth.user.id,
            action="skill_verify",
            target_type="skill_version",  # admin_actions.target_type is VARCHAR(20)
            target_id="bulk" if not body.version_ids else (sorted(seen)[0] if len(seen) == 1 else "bulk"),
            details=json.dumps(
                {"versions": sorted(seen)[:50], "tools": tools, "l0_written": l0, "queued": queued,
                 **sweep_actor_details(auth)}
            ),
        )
    )
    await db.commit()
    return {"versions": len(picked), "l0_written": l0, "queued": queued}


class ClaimRequest(BaseModel):
    tool: str
    limit: int = Field(5, ge=1, le=50)
    harness_image_digest: str | None = Field(None, max_length=80)


def _claimable(tool: str, stale: datetime):
    return and_(
        SkillVerificationJob.tool == tool,
        or_(
            SkillVerificationJob.status == "queued",
            and_(SkillVerificationJob.status == "claimed", SkillVerificationJob.claimed_at < stale),
        ),
    )


@router.post("/admin/skills/verify/jobs/claim")
async def claim_jobs(
    body: ClaimRequest,
    auth: AuthContext = Depends(require_scope(HARNESS_SCOPE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require_harness(auth, db)
    if body.tool not in HARNESS_TOOLS:
        raise HTTPException(422, "Unsupported tool")
    now = datetime.now(timezone.utc)
    stale = now - _CLAIM_TTL
    # A job that used up its attempts (a harness that kept dying on it, or
    # kept failing to report) ends 'errored' instead of blocking the queue.
    await db.execute(
        update(SkillVerificationJob)
        .where(_claimable(body.tool, stale), SkillVerificationJob.attempts >= MAX_JOB_ATTEMPTS)
        .values(status="done", outcome="errored", completed_at=now, claim_token_hash=None,
                outcome_reason=f"gave up after {MAX_JOB_ATTEMPTS} attempts")
        .execution_options(synchronize_session=False)
    )
    candidates = (
        await db.execute(
            select(SkillVerificationJob)
            .where(_claimable(body.tool, stale), SkillVerificationJob.attempts < MAX_JOB_ATTEMPTS)
            .order_by(SkillVerificationJob.created_at, SkillVerificationJob.id)
            .limit(body.limit)
        )
    ).scalars().all()
    jobs = []
    for job in candidates:
        token = secrets.token_urlsafe(24)
        result = await db.execute(
            update(SkillVerificationJob)
            .where(SkillVerificationJob.id == job.id, SkillVerificationJob.status == job.status,
                   SkillVerificationJob.attempts == job.attempts)
            .values(status="claimed", claimed_at=now, attempts=job.attempts + 1,
                    claim_token_hash=hashlib.sha256(token.encode()).hexdigest())
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            continue
        jobs.append({
            "job_id": job.id,
            "version_id": job.version_id,
            "tool": job.tool,
            "level": job.level,
            "claim_token": token,
            "projection_path": f"/api/v1/admin/skills/verify/jobs/{job.id}/projection",
        })
    await db.commit()
    return {"jobs": jobs}


async def _claimed_job(db: AsyncSession, job_id: str, token: str | None) -> SkillVerificationJob:
    job = await db.get(SkillVerificationJob, job_id)
    if job is None or job.status != "claimed" or not token or not job.claim_token_hash:
        raise HTTPException(404, "Job not found")
    if not secrets.compare_digest(job.claim_token_hash, hashlib.sha256(token.encode()).hexdigest()):
        raise HTTPException(404, "Job not found")
    return job


def _finish(job: SkillVerificationJob, outcome: str, reason: str) -> None:
    job.status = "done"
    job.outcome = outcome
    job.outcome_reason = reason[:500]
    job.completed_at = datetime.now(timezone.utc)
    job.claim_token_hash = None


@router.get("/admin/skills/verify/jobs/{job_id}/projection")
async def job_projection(
    job_id: str,
    request: Request,
    auth: AuthContext = Depends(require_scope(HARNESS_SCOPE)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """A one-plugin catalog tarball for the harness. Same bytes the catalog
    serves; only deliverable versions. A version blocked, yanked or removed
    since it was queued ends the job ``skipped`` (no evidence row) and
    answers ``410`` so the harness moves on."""
    await _require_harness(auth, db)
    job = await _claimed_job(db, job_id, request.headers.get("X-SessionFS-Claim-Token"))
    rows = list(
        (
            await db.execute(
                select(SkillMarketplaceListing, SkillPackage, SkillPackageVersion)
                .join(SkillPackage, SkillPackage.id == SkillMarketplaceListing.package_id)
                .join(SkillPackageVersion, SkillPackageVersion.id == SkillMarketplaceListing.version_id)
                .where(*_deliverable_predicates(), SkillPackageVersion.id == job.version_id)
                .order_by(SkillMarketplaceListing.visibility.desc())
            )
        ).all()
    )
    rows.sort(key=lambda row: 0 if row[0].visibility == "public" else 1)
    if not rows:
        _finish(job, "skipped", "package is no longer deliverable (blocked, yanked or removed)")
        await db.commit()
        raise HTTPException(
            410,
            detail={"code": "job_skipped", "message": "The package is no longer deliverable; job skipped."},
        )
    bundle = await service.harness_bundle(_blob_store(request), rows[0], homepage_base=_homepage_base(request))
    if bundle is None:
        raise HTTPException(503, "Package blob unavailable")
    payload, meta = bundle
    await db.commit()
    return Response(
        content=payload,
        media_type="application/gzip",
        headers={
            "X-SessionFS-Marketplace": meta["marketplace"],
            "X-SessionFS-Plugin": meta["plugin"],
            "X-SessionFS-Projection-Hash": meta["projection_hash"],
        },
    )


class JobResult(BaseModel):
    claim_token: str = Field(..., min_length=10, max_length=100)
    level: Literal["L1", "L2"] = "L1"
    # 'error': the harness could not run the job (not a verdict). The job is
    # re-queued until it has used MAX_JOB_ATTEMPTS, then ends 'errored'.
    status: Literal["pass", "fail", "skipped", "error"]
    # With status 'error': end the job 'errored' now instead of re-queuing
    # (the harness could not prove its sandbox clean after this package).
    final: bool = False
    tool_version: str = Field(..., min_length=1, max_length=50)
    reason: str = Field("", max_length=2000)
    evidence: dict[str, Any] = Field(default_factory=dict)
    projection_hash: str | None = Field(None, max_length=71)
    harness_image_digest: str | None = Field(None, max_length=80)


@router.post("/admin/skills/verify/jobs/{job_id}/result")
async def job_result(
    job_id: str,
    body: JobResult,
    auth: AuthContext = Depends(require_scope(HARNESS_SCOPE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require_harness(auth, db)
    job = await _claimed_job(db, job_id, body.claim_token)
    if body.status == "error":
        if body.final or job.attempts >= MAX_JOB_ATTEMPTS:
            _finish(job, "errored", body.reason or "harness error")
            requeued = False
        else:
            job.status = "queued"
            job.claim_token_hash = None
            job.claimed_at = None
            job.outcome_reason = (body.reason or "harness error")[:500]
            requeued = True
        await db.commit()
        return {"verification_id": None, "status": "error", "requeued": requeued}
    evidence = json.dumps(body.evidence, sort_keys=True, default=str)
    if len(evidence) > 8000:
        raise HTTPException(422, "evidence is too large (8000 bytes max)")
    status = body.status
    if status == "fail":
        previous = (
            await db.execute(
                select(SkillToolVerification)
                .where(
                    SkillToolVerification.version_id == job.version_id,
                    SkillToolVerification.tool == job.tool,
                    SkillToolVerification.level.in_(("L1", "L2")),
                )
                .order_by(SkillToolVerification.run_at.desc(), SkillToolVerification.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if previous is not None and previous.status in {"pass", "regressed"} and previous.tool_version != body.tool_version:
            status = "regressed"  # worked on an older tool version, fails on the new one
    row = SkillToolVerification(
        id=f"stv_{secrets.token_hex(12)}",
        version_id=job.version_id,
        tool=job.tool,
        tool_version=body.tool_version,
        level=body.level,
        status=status,
        reason=body.reason,
        evidence=evidence,
        projection_hash=body.projection_hash,
        harness_image_digest=body.harness_image_digest,
        run_at=datetime.now(timezone.utc),
    )
    db.add(row)
    _finish(job, status, body.reason)
    await db.commit()
    return {"verification_id": row.id, "status": status}


class RegenerateRequest(BaseModel):
    limit: int = Field(50, ge=1, le=500)


@router.post("/admin/skills/catalogs/regenerate")
async def regenerate_catalogs(
    body: RegenerateRequest,
    request: Request,
    auth: AuthContext = Depends(require_platform_sweeper()),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Scheduler safety net: rebuild the public catalog and every known
    catalog whose approved input set changed (each fetch also does this).
    Platform-admin user key or a ``platform_sweeps:run`` service key."""
    store = _blob_store(request)
    scopes: list[tuple[str, str | None]] = [("public", None)]
    for row in (
        await db.execute(
            select(SkillFeedCatalog.scope_type, SkillFeedCatalog.scope_id)
            .where(SkillFeedCatalog.scope_type != "public")
            .order_by(SkillFeedCatalog.generated_at.asc().nullsfirst(), SkillFeedCatalog.id)
            .limit(body.limit)
        )
    ).all():
        scopes.append((row[0], row[1]))
    results: list[dict[str, Any]] = []
    for scope_type, scope_id in scopes:
        if scope_type == "org":
            from sessionfs.server.routes.marketplace import _orgs_with_feature

            if not await _orgs_with_feature(db, [scope_id or ""], "team_skill_library"):
                results.append({"scope": scope_type, "scope_id": scope_id, "skipped": "not entitled"})
                continue
        try:
            catalog = await service.ensure_catalog(
                db, store, scope_type, scope_id, homepage_base=_homepage_base(request),
                mirror=_public_mirror(request),
            )
        except service.CatalogUnavailable:
            results.append({"scope": scope_type, "scope_id": scope_id, "error": "build failed"})
            continue
        results.append({"scope": scope_type, "scope_id": scope_id, "commit": catalog.current_commit_sha,
                        "plugins": catalog.plugin_count})
    return {"catalogs": results}

"""Native per-tool catalogs: approved input sets, generation, storage, and
works-in evidence (marketplace v2 §7, tk_86084118c8ee453c).

Freshness model: a catalog row stores the digest of the APPROVED input set
it was built from. Every ``info/refs`` fetch recomputes that digest (one
query, no blob reads) and rebuilds the repository when it changed, so a
block, yank, quarantine, revoked assignment or new approval is reflected on
the next fetch once the rebuild it triggers lands: there is no post-commit
hook to forget and nothing to go stale between sweeps. Only the CURRENT
commit's pack is served, and pack fetches re-check freshness at most every
``_FRESHNESS_TTL`` seconds per catalog.

Rebuilds never hold a database lock or transaction while they read blobs
and build: the build runs first, then a short compare-and-swap moves the
row from the commit/digest the builder observed to the new one (a builder
that lost the race discards its result). While a rebuild of a scope is
running in this process, or after one failed (retried after
``_BUILD_RETRY_SECONDS``), fetches serve the last good commit. A failure
never takes a scope down once it has a commit.

Native installs bypass SessionFS consent, so a catalog only ever contains
versions its scope already approved (§7.2):

- public: deliverable public listings from a verified (or the platform)
  publisher whose open scan severity is at most ``medium``;
- org: versions with an active org-wide assignment, or an approved / applied
  install request on one of the org's projects;
- user: versions the user holds a live consent receipt for.

All of them must also pass the shared deliverability predicates.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import secrets
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    Organization,
    OrgMember,
    OrgSkillAssignment,
    Project,
    SkillConsentReceipt,
    SkillFeedCatalog,
    SkillFeedEntry,
    SkillInstallRequest,
    SkillMarketplaceListing,
    SkillPackage,
    SkillPackageVersion,
    SkillPublisher,
    SkillToolVerification,
    User,
)
from sessionfs.server.storage.base import BlobStore
from sessionfs.skills import native_feeds
from sessionfs.skills.git_catalog import GitRepository, build_repository, info_refs, packs_listing
from sessionfs.skills.instruction_scan import SEVERITY_RANK

logger = logging.getLogger(__name__)

PUBLIC_MAX_SCAN_SEVERITY = "medium"
L0_TOOL_VERSION = f"docs-{native_feeds.VENDOR_DOCS_CHECKED}"
MAX_CATALOG_PLUGINS = 2_000
_FRESHNESS_TTL = 15.0
_fresh_until: dict[str, float] = {}
_state_cache: dict[tuple[str, str], dict[str, Any]] = {}
_STATE_CACHE_MAX = 64
_BUILD_RETRY_SECONDS = 60.0
_build_locks: dict[str, asyncio.Lock] = {}
# scope key -> (input digest that failed, monotonic time to retry after)
_build_failures: dict[str, tuple[str, float]] = {}
_BUILD_STATE_MAX = 4096


class CatalogUnavailable(RuntimeError):
    """The scope has never been built successfully (nothing to serve yet)."""


def _env_int(name: str, default: int, *, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return max(low, min(value, high))


def catalog_budget() -> tuple[int, int, int]:
    """(max files, max bytes, max bytes of one plugin) of one catalog build
    (env-tunable, capped)."""
    files = _env_int(
        "SFS_NATIVE_CATALOG_MAX_FILES", native_feeds.DEFAULT_MAX_CATALOG_FILES,
        low=1, high=native_feeds.DEFAULT_MAX_CATALOG_FILES,
    )
    size = _env_int(
        "SFS_NATIVE_CATALOG_MAX_BYTES", native_feeds.DEFAULT_MAX_CATALOG_BYTES,
        low=1, high=4 * native_feeds.DEFAULT_MAX_CATALOG_BYTES,
    )
    plugin = _env_int(
        "SFS_NATIVE_CATALOG_MAX_PLUGIN_BYTES", native_feeds.DEFAULT_MAX_ADMITTED_PLUGIN_BYTES,
        low=1, high=native_feeds.MAX_PLUGIN_BYTES,
    )
    return files, size, min(plugin, size)


def scope_key(scope_type: str, scope_id: str | None) -> str:
    return "public" if scope_type == "public" else f"{scope_type}:{scope_id}"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _json(raw: str | None, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def scan_ok_for_public(scan_summary: str | None) -> bool:
    """Fail closed: a version with no scan summary never enters the public
    native catalog."""
    summary = _json(scan_summary, {})
    if not summary.get("ruleset"):
        return False
    severity = summary.get("open_max_severity", summary.get("max_severity"))
    rank = SEVERITY_RANK.get(str(severity), None)
    return rank is not None and rank <= SEVERITY_RANK[PUBLIC_MAX_SCAN_SEVERITY]


# ---------------------------------------------------------------------------
# Approved input sets
# ---------------------------------------------------------------------------


async def _entitled_orgs_for_user(db: AsyncSession, user_id: str) -> list[str]:
    from sessionfs.server.routes.marketplace import _orgs_with_feature

    memberships = list(
        (await db.execute(select(OrgMember.org_id).where(OrgMember.user_id == user_id))).scalars().all()
    )
    return await _orgs_with_feature(db, memberships, "team_skill_library")


def _base_select():
    from sessionfs.server.routes.marketplace import _deliverable_predicates

    return (
        select(SkillMarketplaceListing, SkillPackage, SkillPackageVersion)
        .join(SkillPackage, SkillPackage.id == SkillMarketplaceListing.package_id)
        .join(SkillPackageVersion, SkillPackageVersion.id == SkillMarketplaceListing.version_id)
        .where(*_deliverable_predicates())
    )


async def _verified_publishers(db: AsyncSession, packages: list[SkillPackage]) -> set[str]:
    from sessionfs.server.services import publishers as publishers_service

    ids = {package.publisher_id for package in packages if package.publisher_id}
    if not ids:
        return set()
    active = set(
        (
            await db.execute(
                select(SkillPublisher.id).where(
                    SkillPublisher.id.in_(sorted(ids)), SkillPublisher.status == "active"
                )
            )
        ).scalars().all()
    )
    return active & await publishers_service.verified_publisher_ids(db, active)


async def approved_rows(db: AsyncSession, scope_type: str, scope_id: str | None) -> list[tuple]:
    """(listing, package, version) per package, newest approved version."""
    statement = _base_select()
    if scope_type == "public":
        statement = statement.where(SkillMarketplaceListing.visibility == "public")
    elif scope_type == "org":
        assigned = select(OrgSkillAssignment.version_id).where(
            OrgSkillAssignment.org_id == scope_id,
            OrgSkillAssignment.revoked_at.is_(None),
            OrgSkillAssignment.target_type == "org",
            OrgSkillAssignment.content_hash == SkillPackageVersion.content_hash,
        )
        requested = (
            select(SkillInstallRequest.version_id)
            .join(Project, Project.id == SkillInstallRequest.project_id)
            .where(
                Project.org_id == scope_id,
                SkillInstallRequest.status.in_(("approved", "applied")),
                SkillInstallRequest.deleted_at.is_(None),
            )
        )
        statement = statement.where(
            or_(
                SkillMarketplaceListing.visibility == "public",
                and_(
                    SkillMarketplaceListing.visibility == "org",
                    SkillMarketplaceListing.org_id == scope_id,
                ),
            ),
            or_(SkillPackageVersion.id.in_(assigned), SkillPackageVersion.id.in_(requested)),
        )
    elif scope_type == "user":
        org_ids = await _entitled_orgs_for_user(db, scope_id or "")
        visible = [
            SkillMarketplaceListing.visibility == "public",
            and_(
                SkillMarketplaceListing.visibility == "user",
                SkillMarketplaceListing.owner_user_id == scope_id,
            ),
        ]
        if org_ids:
            visible.append(
                and_(
                    SkillMarketplaceListing.visibility == "org",
                    SkillMarketplaceListing.org_id.in_(org_ids),
                )
            )
        consented = select(SkillConsentReceipt.version_id).where(
            SkillConsentReceipt.actor_user_id == scope_id,
            SkillConsentReceipt.revoked_at.is_(None),
            SkillConsentReceipt.deleted_at.is_(None),
            SkillConsentReceipt.content_hash == SkillPackageVersion.content_hash,
        )
        statement = statement.where(or_(*visible), SkillPackageVersion.id.in_(consented))
    else:
        raise ValueError(f"unknown scope {scope_type}")
    rows = list(
        (
            await db.execute(
                statement.order_by(
                    SkillMarketplaceListing.published_at.desc(), SkillMarketplaceListing.id.desc()
                )
            )
        ).all()
    )
    if scope_type == "public":
        verified = await _verified_publishers(db, [row[1] for row in rows])
        rows = [
            row
            for row in rows
            if (row[1].owner_type == "system" or row[1].publisher_id in verified)
            and scan_ok_for_public(row[2].scan_summary)
        ]
    chosen: dict[str, tuple] = {}
    for row in rows:
        chosen.setdefault(row[1].id, row)
    ordered = sorted(chosen.values(), key=lambda row: (row[1].namespace, row[1].name, row[2].id))
    return ordered[:MAX_CATALOG_PLUGINS]


def input_digest(key: str, rows: list[tuple], *, mirror: str | None = None) -> str:
    material: dict[str, Any] = {
        "generator": native_feeds.GENERATOR_VERSION,
        "budget": list(catalog_budget()),
        "scope": key,
        "items": [
            [
                package.id,
                version.id,
                version.content_hash,
                package.namespace,
                package.name,
                listing.description or package.description,
                version.version_label,
                _utc(listing.published_at).isoformat(),
                version.compatibility,
                version.license_metadata,
            ]
            for listing, package, version in rows
        ],
    }
    if mirror:
        # Only when set: digests of deployments without a mirror are unchanged.
        material["mirror"] = mirror
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def _extract_files(payload: bytes) -> dict[str, bytes]:
    from sessionfs.skills.publish_validation import _extract_archive

    with tempfile.TemporaryDirectory(prefix="sfs-native-feed-") as temp:
        root = Path(temp)
        _extract_archive(payload, root)
        files: dict[str, bytes] = {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            files[path.relative_to(root).as_posix()] = path.read_bytes()
        return files


def homepage_url(base_url: str, package: SkillPackage) -> str:
    return f"{base_url.rstrip('/')}/marketplace/{package.namespace}/{package.name}"


async def catalog_inputs(
    store: BlobStore,
    rows: list[tuple],
    *,
    homepage_base: str,
    public: bool = False,
    max_bytes: int | None = None,
    max_plugin_bytes: int | None = None,
) -> list[native_feeds.CatalogInput]:
    """Load each approved version's files, one archive at a time.

    Memory stays bounded: a version that cannot be admitted (it would be
    excluded, or is larger than ``max_plugin_bytes``) is dropped right away
    and NOT charged to the budget; once admitted plugins reach ``max_bytes``
    (the catalog byte budget), later versions are not even fetched. Dropped
    versions become ``skip_reason`` inputs, which the projection excludes
    and discloses. Rows arrive in catalog order, so the result is
    deterministic."""
    _, budget_bytes, budget_plugin = catalog_budget()
    limit = budget_bytes if max_bytes is None else max_bytes
    plugin_limit = budget_plugin if max_plugin_bytes is None else max_plugin_bytes
    loaded = 0
    inputs: list[native_feeds.CatalogInput] = []
    for listing, package, version in rows:
        skip_reason: str | None = None
        files: dict[str, bytes] = {}
        if loaded >= limit:
            skip_reason = f"catalog_budget: the catalog is limited to {limit} bytes"
        else:
            payload = await store.get(version.blob_key)
            if payload is None:
                logger.warning("native catalog: blob for %s is unavailable; entry excluded", version.id)
                skip_reason = "package archive is unavailable"
            else:
                try:
                    files = await asyncio.to_thread(_extract_files, payload)
                except Exception:  # noqa: BLE001 - an unreadable archive never enters a catalog
                    logger.warning("native catalog: archive of %s unreadable; entry excluded", version.id)
                    skip_reason = "package archive is unreadable"
                del payload
            if skip_reason is None:
                probe = native_feeds.CatalogInput(
                    version_id=version.id, package_id=package.id, namespace=package.namespace,
                    name=package.name, description=listing.description or package.description,
                    version_label=version.version_label, content_hash=version.content_hash,
                    published_at=_utc(listing.published_at), files=files,
                )
                try:
                    size = native_feeds.admission_size(probe, public=public)
                except native_feeds.NativeFeedError as exc:
                    skip_reason = str(exc)
                else:
                    if size > plugin_limit:
                        skip_reason = (
                            f"plugin_too_large: a catalog plugin is limited to {plugin_limit} bytes"
                        )
                    elif loaded + size > limit:
                        skip_reason = f"catalog_budget: the catalog is limited to {limit} bytes"
                    else:
                        loaded += size  # only admitted plugins are charged
            if skip_reason is not None:
                files = {}
        license_meta = _json(version.license_metadata, {})
        compatibility = _json(version.compatibility, {})
        omitted: set[str] = set()
        for entry in compatibility.values():
            if isinstance(entry, dict) and isinstance(entry.get("omitted_components"), list):
                omitted.update(str(item) for item in entry["omitted_components"])
        inputs.append(
            native_feeds.CatalogInput(
                version_id=version.id,
                package_id=package.id,
                namespace=package.namespace,
                name=package.name,
                description=listing.description or package.description,
                version_label=version.version_label,
                content_hash=version.content_hash,
                published_at=_utc(listing.published_at),
                files=files,
                license=license_meta.get("value") if license_meta.get("declared") else None,
                homepage=homepage_url(homepage_base, package),
                publisher_name=package.display_name,
                omitted_components=tuple(sorted(omitted)),
                skip_reason=skip_reason,
            )
        )
    return inputs


@dataclass
class BuiltCatalog:
    projection: native_feeds.CatalogProjection
    repo: GitRepository
    plugin_repos: dict[str, GitRepository]


def build(
    scope_type: str, scope_id: str | None, inputs: list[native_feeds.CatalogInput], *, mirror: str | None = None
) -> BuiltCatalog:
    max_files, max_bytes, max_plugin_bytes = catalog_budget()
    projection = native_feeds.build_catalog(
        scope_type, scope_id, inputs, max_files=max_files, max_bytes=max_bytes,
        max_plugin_bytes=max_plugin_bytes, mirror=mirror if scope_type == "public" else None,
    )
    message = (
        f"SessionFS {scope_type} catalog: {len(projection.plugins)} plugin(s)\n\n"
        f"generator {native_feeds.GENERATOR_VERSION}"
    )
    repo = build_repository(projection.files, timestamp=projection.timestamp, message=message)
    plugin_repos: dict[str, GitRepository] = {}
    by_id = {plugin.plugin_id: plugin for plugin in projection.plugins}
    for plugin_id, files in projection.plugin_repos.items():
        label = next(
            (item.version_label for item in inputs if item.version_id == by_id[plugin_id].version_id),
            None,
        )
        tags: dict[str, None] = {}
        if label:
            tag = label if label.startswith("v") else f"v{label}"
            if all(ch.isalnum() or ch in "._+-" for ch in tag) and ".." not in tag:
                tags[tag] = None
        plugin_repos[plugin_id] = build_repository(
            files,
            timestamp=projection.timestamp,
            message=f"{plugin_id} {label or ''}".strip(),
            tags=tags,
        )
    return BuiltCatalog(projection=projection, repo=repo, plugin_repos=plugin_repos)


def _pack_key(catalog: SkillFeedCatalog, pack_name: str, suffix: str) -> str:
    return f"{catalog.blob_prefix}/packs/{pack_name}.{suffix}"


def _state_key(catalog: SkillFeedCatalog, commit: str) -> str:
    return f"{catalog.blob_prefix}/commits/{commit}/state.json"


async def _put_pack(store: BlobStore, catalog: SkillFeedCatalog, repo: GitRepository) -> dict[str, Any]:
    name, pack, index = repo.pack()
    # Pack names are content digests: an existing blob already holds these bytes.
    if not await store.exists(_pack_key(catalog, name, "pack")):
        await store.put(_pack_key(catalog, name, "pack"), pack)
        await store.put(_pack_key(catalog, name, "idx"), index)
    return {"commit": repo.commit_sha, "refs": repo.refs, "pack": name}


def _packs(state: dict[str, Any] | None) -> set[str]:
    if not state:
        return set()
    names = {state["pack"]} if state.get("pack") else set()
    names.update(plugin["pack"] for plugin in state.get("plugins", {}).values() if plugin.get("pack"))
    return names


async def _put_packs(store: BlobStore, catalog: SkillFeedCatalog, built: BuiltCatalog) -> dict[str, Any]:
    """Write the new commit's packs (content-addressed, idempotent: safe for
    a builder that later loses the compare-and-swap) and return its state."""
    state = await _put_pack(store, catalog, built.repo)
    state["plugins"] = {
        plugin_id: await _put_pack(store, catalog, repo)
        for plugin_id, repo in sorted(built.plugin_repos.items())
    }
    return state


async def _finalize_state(
    store: BlobStore, catalog: SkillFeedCatalog, state: dict[str, Any], previous_commit: str | None
) -> None:
    """Winner only (after its compare-and-swap, before commit): retire the
    previous commit's packs and write the new state. Packs the previous
    commit used are kept for one more generation (a clone that read the old
    info/refs can finish) and deleted on the rebuild after. ``previous_commit``
    is the commit the swap replaced, so this never deletes a live pack."""
    previous = None
    if previous_commit:
        raw = await store.get(_state_key(catalog, previous_commit))
        previous = json.loads(raw) if raw is not None else None
    current = _packs(state)
    if previous is not None and previous.get("commit") != state["commit"]:
        state["retired"] = sorted(_packs(previous) - current)
        for name in previous.get("retired", []):
            if name not in current and name not in _packs(previous):
                await store.delete(_pack_key(catalog, name, "pack"))
                await store.delete(_pack_key(catalog, name, "idx"))
    elif previous is not None:
        state["retired"] = previous.get("retired", [])
    await store.put(_state_key(catalog, state["commit"]), json.dumps(state, sort_keys=True).encode())


async def _record_entries(db: AsyncSession, catalog: SkillFeedCatalog, built: BuiltCatalog) -> None:
    await db.execute(delete(SkillFeedEntry).where(SkillFeedEntry.catalog_id == catalog.id))
    for plugin in built.projection.plugins:
        for tool in plugin.tools:
            db.add(
                SkillFeedEntry(
                    catalog_id=catalog.id,
                    version_id=plugin.version_id,
                    tool=tool,
                    plugin_name=plugin.plugin_id,
                    projection_hash=plugin.projection_hash,
                    plugin_path=plugin.plugin_path,
                )
            )


async def record_l0(
    db: AsyncSession, verdicts: dict[str, list[native_feeds.Verdict]], hashes: dict[str, str]
) -> int:
    """Store L0 verdicts not already recorded for the same projection."""
    if not verdicts:
        return 0
    existing = {
        (row.version_id, row.tool, row.projection_hash)
        for row in (
            await db.execute(
                select(SkillToolVerification).where(
                    SkillToolVerification.version_id.in_(sorted(verdicts)),
                    SkillToolVerification.level == "L0",
                    SkillToolVerification.tool_version == L0_TOOL_VERSION,
                )
            )
        ).scalars()
    }
    written = 0
    now = datetime.now(timezone.utc)
    for version_id, items in sorted(verdicts.items()):
        for verdict in items:
            key = (version_id, verdict.tool, hashes.get(version_id))
            if key in existing:
                continue
            db.add(
                SkillToolVerification(
                    id=f"stv_{secrets.token_hex(12)}",
                    version_id=version_id,
                    tool=verdict.tool,
                    tool_version=L0_TOOL_VERSION,
                    level="L0",
                    status=verdict.status,
                    reason=verdict.reason[:2000],
                    evidence=json.dumps(verdict.evidence, sort_keys=True)[:4000],
                    projection_hash=hashes.get(version_id),
                    harness_image_digest=f"in-process:{native_feeds.GENERATOR_VERSION}",
                    run_at=now,
                )
            )
            existing.add(key)
            written += 1
    return written


async def _l0_allowed_versions(db: AsyncSession, rows: list[tuple]) -> set[str]:
    """Works-in on PRIVATE packages is Team+ (D6). Public listings, and org
    listings (org catalogs already require team_skill_library), always get
    L0 evidence; a personal-only listing only when its owner is Team+."""
    from sessionfs.server.tier_gate import get_effective_tier
    from sessionfs.server.tiers import get_features_for_tier

    allowed: set[str] = set()
    tier_cache: dict[str, bool] = {}
    for listing, _package, version in rows:
        if listing.visibility in {"public", "org"}:
            allowed.add(version.id)
            continue
        owner = listing.owner_user_id or ""
        if owner not in tier_cache:
            user = await db.get(User, owner)
            tier_cache[owner] = bool(
                user is not None
                and "team_skill_library" in get_features_for_tier(await get_effective_tier(user, db))
            )
        if tier_cache[owner]:
            allowed.add(version.id)
    return allowed


async def _catalog_row(db: AsyncSession, scope_type: str, scope_id: str | None, *, lock: bool) -> SkillFeedCatalog:
    key = scope_key(scope_type, scope_id)
    statement = select(SkillFeedCatalog).where(SkillFeedCatalog.scope_key == key)
    if lock:
        statement = statement.with_for_update()
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is not None:
        return row
    catalog_id = f"sfc_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}"
    row = SkillFeedCatalog(
        id=catalog_id,
        scope_key=key,
        scope_type=scope_type,
        scope_id=scope_id,
        blob_prefix=f"native-catalogs/{catalog_id}",
    )
    connection = await db.connection()
    if connection.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert  # type: ignore[assignment]
    await db.execute(
        insert(SkillFeedCatalog)
        .values(
            id=row.id,
            scope_key=row.scope_key,
            scope_type=row.scope_type,
            scope_id=row.scope_id,
            blob_prefix=row.blob_prefix,
            plugin_count=0,
        )
        .on_conflict_do_nothing()
    )
    statement = select(SkillFeedCatalog).where(SkillFeedCatalog.scope_key == key)
    if lock:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one()


def _build_lock(key: str) -> asyncio.Lock:
    if len(_build_locks) >= _BUILD_STATE_MAX:
        for stale in [name for name, lock in _build_locks.items() if not lock.locked()]:
            _build_locks.pop(stale, None)
    return _build_locks.setdefault(key, asyncio.Lock())


def _note_failure(key: str, digest: str) -> None:
    if len(_build_failures) >= _BUILD_STATE_MAX:
        _build_failures.clear()
    _build_failures[key] = (digest, time.monotonic() + _BUILD_RETRY_SECONDS)


async def _reload(db: AsyncSession, key: str) -> SkillFeedCatalog:
    return (
        await db.execute(
            select(SkillFeedCatalog)
            .where(SkillFeedCatalog.scope_key == key)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


def _serveable(catalog: SkillFeedCatalog) -> SkillFeedCatalog:
    if not catalog.current_commit_sha:
        raise CatalogUnavailable("catalog is being generated")
    return catalog


async def _serve_stale(db: AsyncSession, catalog: SkillFeedCatalog, rows: list[tuple]) -> SkillFeedCatalog:
    """The last good commit, unless it still carries a version that has since
    been withdrawn from this scope (block, yank, quarantine, revoked grant).
    A takedown must never keep shipping while rebuilds fail: in that case the
    scope is unavailable until a rebuild succeeds. A newer approved version of
    a package still in the set (a plain update) does not count as withdrawn."""
    catalog = _serveable(catalog)
    committed = set(
        (
            await db.execute(
                select(SkillFeedEntry.version_id).where(SkillFeedEntry.catalog_id == catalog.id).distinct()
            )
        ).scalars().all()
    )
    missing = committed - {row[2].id for row in rows}
    if not missing:
        return catalog
    packages_in_set = {row[1].id for row in rows}
    deliverable = {
        version.id: package.id
        for _listing, package, version in (
            await db.execute(_base_select().where(SkillPackageVersion.id.in_(sorted(missing))))
        ).all()
    }
    await db.commit()
    for version_id in missing:
        package_id = deliverable.get(version_id)
        if package_id is None or package_id not in packages_in_set:
            logger.warning(
                "native catalog %s: last good commit carries withdrawn version %s; refusing to serve it",
                catalog.scope_key, version_id,
            )
            raise CatalogUnavailable("catalog is being regenerated")
    return catalog


async def ensure_catalog(
    db: AsyncSession,
    store: BlobStore,
    scope_type: str,
    scope_id: str | None,
    *,
    homepage_base: str,
    force: bool = False,
    mirror: str | None = None,
) -> SkillFeedCatalog:
    """The catalog row, rebuilt first when its approved input set changed.
    Commits. Raises ``CatalogUnavailable`` only when the scope has no
    commit at all and building one failed or is in progress elsewhere.

    ``mirror`` is the configured public GitHub mirror (public scope only;
    it is named in the generated README)."""
    key = scope_key(scope_type, scope_id)
    mirror = mirror if scope_type == "public" else None
    rows = await approved_rows(db, scope_type, scope_id)
    digest = input_digest(key, rows, mirror=mirror)
    catalog = await _catalog_row(db, scope_type, scope_id, lock=False)
    if not force and catalog.input_digest == digest and catalog.current_commit_sha:
        await db.commit()
        _fresh_until[key] = time.monotonic() + _FRESHNESS_TTL
        return catalog
    failure = _build_failures.get(key)
    if (
        not force
        and catalog.current_commit_sha
        and failure is not None
        and failure[0] == digest
        and failure[1] > time.monotonic()
    ):
        await db.commit()
        # the last build of these inputs failed: serve the last good commit
        return await _serve_stale(db, catalog, rows)
    lock = _build_lock(key)
    if lock.locked() and catalog.current_commit_sha and not force:
        await db.commit()
        # a rebuild is running in this process: serve the last good commit
        return await _serve_stale(db, catalog, rows)
    async with lock:
        # Take the compare-and-swap baseline FIRST, then read the approved
        # set again: the rows are at least as new as the commit they would
        # replace, so a builder that waited here can never swap an older
        # input set over a newer commit.
        await db.commit()
        catalog = await _reload(db, key)
        observed_commit = catalog.current_commit_sha
        observed_digest = catalog.input_digest
        rows = await approved_rows(db, scope_type, scope_id)
        digest = input_digest(key, rows, mirror=mirror)
        if not force and observed_digest == digest and observed_commit:
            await db.commit()
            _fresh_until[key] = time.monotonic() + _FRESHNESS_TTL
            return catalog
        allowed = await _l0_allowed_versions(db, rows)
        # End the read transaction: nothing is locked or held open while
        # blobs are read and the repository is built.
        await db.commit()
        try:
            inputs = await catalog_inputs(
                store, rows, homepage_base=homepage_base, public=scope_type == "public"
            )
            built = await asyncio.to_thread(build, scope_type, scope_id, inputs, mirror=mirror)
            del inputs
            state = await _put_packs(store, catalog, built)
        except Exception:  # noqa: BLE001 - a failed build must never take the scope down
            logger.exception("native catalog %s: rebuild failed; serving the last good commit", key)
            _note_failure(key, digest)
            return await _serve_stale(db, catalog, rows)
        swapped = await db.execute(
            update(SkillFeedCatalog)
            .where(
                SkillFeedCatalog.id == catalog.id,
                SkillFeedCatalog.current_commit_sha.is_not_distinct_from(observed_commit),
                SkillFeedCatalog.input_digest.is_not_distinct_from(observed_digest),
            )
            .values(
                current_commit_sha=built.repo.commit_sha,
                input_digest=digest,
                plugin_count=len(built.projection.plugins),
                generated_at=datetime.now(timezone.utc),
            )
            .execution_options(synchronize_session=False)
        )
        if swapped.rowcount != 1:  # type: ignore[attr-defined]
            # Another builder moved the row first: discard this build.
            await db.rollback()
            winner = await _reload(db, key)
            await db.commit()
            return _serveable(winner)
        try:
            await _finalize_state(store, catalog, state, observed_commit)
            await _record_entries(db, catalog, built)
            hashes = {plugin.version_id: plugin.projection_hash for plugin in built.projection.plugins}
            await record_l0(
                db,
                {vid: items for vid, items in built.projection.verdicts.items() if vid in allowed},
                hashes,
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception("native catalog %s: could not record the rebuild; serving the last good commit", key)
            _note_failure(key, digest)
            current = await _reload(db, key)
            await db.commit()
            return await _serve_stale(db, current, rows)
        _build_failures.pop(key, None)
        _fresh_until[key] = time.monotonic() + _FRESHNESS_TTL
        if built.projection.excluded:
            logger.warning("native catalog %s excluded %d entries", key, len(built.projection.excluded))
        fresh = await _reload(db, key)
        await db.commit()
        return fresh


async def fresh_catalog(
    db: AsyncSession, store: BlobStore, scope_type: str, scope_id: str | None, *, homepage_base: str,
    mirror: str | None = None,
) -> SkillFeedCatalog:
    """Like ensure_catalog, but skips the digest query inside the TTL window
    (object fetches of one clone)."""
    key = scope_key(scope_type, scope_id)
    if _fresh_until.get(key, 0.0) > time.monotonic():
        catalog = (
            await db.execute(select(SkillFeedCatalog).where(SkillFeedCatalog.scope_key == key))
        ).scalar_one_or_none()
        if catalog is not None and catalog.current_commit_sha:
            return catalog
    return await ensure_catalog(db, store, scope_type, scope_id, homepage_base=homepage_base, mirror=mirror)


async def catalog_state(store: BlobStore, catalog: SkillFeedCatalog) -> dict[str, Any] | None:
    commit = catalog.current_commit_sha
    if not commit:
        return None
    cache_key = (catalog.id, commit)
    if cache_key in _state_cache:
        return _state_cache[cache_key]
    raw = await store.get(_state_key(catalog, commit))
    if raw is None:
        return None
    state = json.loads(raw)
    if len(_state_cache) >= _STATE_CACHE_MAX:
        _state_cache.pop(next(iter(_state_cache)))
    _state_cache[cache_key] = state
    return state


async def read_repo_file(
    store: BlobStore, catalog: SkillFeedCatalog, state: dict[str, Any], path: str, *, plugin_id: str | None
) -> bytes | None:
    """A dumb-HTTP file of the current commit (or of one plugin repo), or
    None. Only the CURRENT commit's pack is ever served; loose objects never
    exist, so a client always falls back to ``objects/info/packs``."""
    repo = state if plugin_id is None else state.get("plugins", {}).get(plugin_id)
    if repo is None:
        return None
    pack = repo["pack"]
    if path == "HEAD":
        return b"ref: refs/heads/main\n"
    if path == "info/refs":
        return info_refs(repo["refs"])
    if path == "objects/info/packs":
        return packs_listing(pack)
    for suffix in ("pack", "idx"):
        if path == f"objects/pack/{pack}.{suffix}":
            return await store.get(_pack_key(catalog, pack, suffix))
    return None


# ---------------------------------------------------------------------------
# Works-in
# ---------------------------------------------------------------------------


def _pack_dir(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    import gzip

    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for path in sorted(files):
                info = tarfile.TarInfo(path)
                info.size = len(files[path])
                info.mode = 0o644
                info.mtime = 0
                archive.addfile(info, io.BytesIO(files[path]))
    return output.getvalue()


async def single_version_projection(
    store: BlobStore, row: tuple, *, homepage_base: str
) -> native_feeds.CatalogProjection | None:
    """A one-plugin catalog for a version (L0 on demand, harness input). Public
    listings project with the public layout (Gemini included)."""
    listing = row[0]
    scope_type = "public" if listing.visibility == "public" else "org"
    scope_id = None if scope_type == "public" else (listing.org_id or listing.owner_user_id or "private")
    inputs = await catalog_inputs(store, [row], homepage_base=homepage_base, public=scope_type == "public")
    if not inputs:
        return None
    max_files, max_bytes, max_plugin_bytes = catalog_budget()
    return await asyncio.to_thread(
        lambda: native_feeds.build_catalog(
            scope_type, scope_id, inputs, max_files=max_files, max_bytes=max_bytes,
            max_plugin_bytes=max_plugin_bytes,
        )
    )


async def harness_bundle(store: BlobStore, row: tuple, *, homepage_base: str) -> tuple[bytes, dict] | None:
    projection = await single_version_projection(store, row, homepage_base=homepage_base)
    if projection is None or not projection.plugins:
        return None
    plugin = projection.plugins[0]
    meta = {
        "marketplace": projection.marketplace_name,
        "plugin": plugin.plugin_id,
        "plugin_path": plugin.plugin_path,
        "projection_hash": plugin.projection_hash,
    }
    return _pack_dir(projection.files), meta


async def latest_verifications(db: AsyncSession, version_id: str) -> dict[str, dict[str, SkillToolVerification]]:
    rows = (
        await db.execute(
            select(SkillToolVerification)
            .where(SkillToolVerification.version_id == version_id)
            .order_by(SkillToolVerification.run_at.desc(), SkillToolVerification.id.desc())
        )
    ).scalars().all()
    latest: dict[str, dict[str, SkillToolVerification]] = {}
    for row in rows:
        latest.setdefault(row.tool, {}).setdefault(row.level, row)
    return latest


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def works_in_payload(
    version: SkillPackageVersion, latest: dict[str, dict[str, SkillToolVerification]]
) -> dict[str, Any]:
    """Badge rules (§7.4): "verified" only from L1/L2 harness evidence;
    L0 is "format-valid"; adapter compatibility is "expected"."""
    compatibility = _json(version.compatibility, {})
    tools: dict[str, Any] = {}
    for tool in native_feeds.NATIVE_TOOLS:
        by_level = latest.get(tool, {})
        harness = by_level.get("L2") or by_level.get("L1")
        static = by_level.get("L0")
        badge = "unverified"
        if harness is not None and harness.status == "pass":
            badge = "verified"
        elif harness is not None and harness.status == "regressed":
            badge = "regressed"
        elif harness is not None and harness.status == "fail":
            badge = "failed"
        elif static is not None and static.status == "pass":
            badge = "format-valid"
        elif static is not None and static.status == "fail":
            badge = "format-invalid"
        elif static is not None and static.status == "skipped":
            badge = "not-applicable"
        entry: dict[str, Any] = {"badge": badge, "levels": {}}
        for level in ("L0", "L1", "L2"):
            row = by_level.get(level)
            if row is None:
                continue
            entry["levels"][level] = {
                "status": row.status,
                "tool_version": row.tool_version,
                "run_at": _iso(row.run_at),
                "reason": row.reason,
                "projection_hash": row.projection_hash,
            }
        if isinstance(compatibility.get(tool), dict):
            entry["expected"] = {
                "omitted_components": compatibility[tool].get("omitted_components", []),
            }
        tools[tool] = entry
    expected_only = {
        tool: {"badge": "expected", "omitted_components": value.get("omitted_components", [])}
        for tool, value in sorted(compatibility.items())
        if tool not in tools and isinstance(value, dict)
    }
    return {
        "version_id": version.id,
        "content_hash": version.content_hash,
        "vendor_docs_checked": native_feeds.VENDOR_DOCS_CHECKED,
        "tools": tools,
        "expected_only": expected_only,
    }


async def org_name(db: AsyncSession, org_id: str) -> str | None:
    org = await db.get(Organization, org_id)
    return org.name if org else None


def harness_quota() -> int:
    try:
        return max(0, int(os.environ.get("SFS_WORKS_IN_ORG_DAILY_QUOTA", "25")))
    except ValueError:
        return 25

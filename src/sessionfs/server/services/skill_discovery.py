"""Marketplace discovery and freshness (marketplace v2 §9, migration 076).

- Search facets and the deterministic ranking used by
  ``GET /api/v1/marketplace/packages`` (filters run in SQL before the page is
  cut; see routes/marketplace.py ``search_marketplace``).
- Install-event counting: one count per pseudonymous principal per
  (version, tool, event, day), stored as daily aggregates. No per-user rows
  are kept beyond the 30-day dedupe window.
- The freshness sweeper (stale / unmaintained flags, last updated and last
  verified per tool, ranking inputs) and the works-in result intake.
- Publisher analytics and org usage aggregates, with the k >= 10 display
  floor for anything a publisher sees.
- README and package-image loading from the immutable archive.

The server never executes package code here: the archive is only extracted
(with the publish validator's bounded extractor) to read text and images.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
from collections import OrderedDict
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import and_, case, delete, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    SkillCategory,
    SkillInstallDaily,
    SkillInstallDedupe,
    SkillMarketplaceListing,
    SkillPackage,
    SkillPackageCategory,
    SkillPackageFreshness,
    SkillPackageReport,
    SkillPackageToolFreshness,
    SkillPackageVersion,
    SkillPublisher,
    SkillPublisherVerification,
    SkillVersionTool,
)
from sessionfs.server.storage.base import BlobStore
from sessionfs.skills import discovery

logger = logging.getLogger(__name__)

SORTS = ("recent", "relevance")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _utc(value)
    return value.isoformat() if value is not None else None


async def _dialect_insert(db: AsyncSession, model):
    connection = await db.connection()
    if connection.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert  # type: ignore[assignment]
    return dialect_insert(model)


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


async def ensure_categories(db: AsyncSession) -> None:
    """Insert any taxonomy row that is missing (migration 076 seeds them; a
    schema built with create_all does not)."""
    present = set((await db.execute(select(SkillCategory.slug))).scalars().all())
    missing = [
        (index, slug, name)
        for index, (slug, name) in enumerate(discovery.CATEGORIES)
        if slug not in present
    ]
    for index, slug, name in missing:
        statement = (await _dialect_insert(db, SkillCategory)).values(
            slug=slug, display_name=name, sort_order=index * 10
        )
        await db.execute(statement.on_conflict_do_nothing())


async def list_categories(db: AsyncSession) -> list[dict[str, Any]]:
    await ensure_categories(db)
    rows = (
        await db.execute(select(SkillCategory).order_by(SkillCategory.sort_order, SkillCategory.slug))
    ).scalars().all()
    return [{"slug": row.slug, "display_name": row.display_name} for row in rows]


async def set_package_categories(db: AsyncSession, package_id: str, slugs: list[str]) -> list[str]:
    await ensure_categories(db)
    unique = sorted(dict.fromkeys(slugs))
    await db.execute(delete(SkillPackageCategory).where(SkillPackageCategory.package_id == package_id))
    for slug in unique:
        db.add(SkillPackageCategory(package_id=package_id, category_slug=slug))
    return unique


# ---------------------------------------------------------------------------
# Search facets and ranking
# ---------------------------------------------------------------------------


@dataclass
class SearchFilters:
    category: str | None = None
    publisher: str | None = None
    verified: bool | None = None
    attested: bool | None = None
    license: str | None = None
    max_severity: str | None = None
    capability: str | None = None
    works_in: str | None = None
    updated_within: int | None = None
    include_deprecated: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


def verified_publisher_expr():
    """EXISTS an active publisher for the package with a live verification
    (or the platform publisher): the same rule as the publisher badge."""
    live = exists(
        select(SkillPublisherVerification.id).where(
            SkillPublisherVerification.publisher_id == SkillPublisher.id,
            SkillPublisherVerification.status == "verified",
        )
    )
    return exists(
        select(SkillPublisher.id).where(
            SkillPublisher.id == SkillPackage.publisher_id,
            SkillPublisher.status == "active",
            or_(SkillPublisher.owner_type == "system", live),
        )
    )


def passing_now_predicates() -> list:
    """A tool counts as "works in" only while the verified version IS the
    package's current version: a new release reads as not verified until the
    harness checks it."""
    return [
        SkillPackageToolFreshness.package_id == SkillPackage.id,
        SkillPackageToolFreshness.verify_status == "passing",
        SkillPackageToolFreshness.last_verified_version_id == SkillPackage.current_version_id,
    ]


def effective_status(row: SkillPackageToolFreshness, current_version_id: str | None) -> str:
    """The displayed works-in status: only a result about the current
    version counts; anything else is "unknown" (not yet verified)."""
    if current_version_id is None:
        return "unknown"
    if row.verify_status == "passing":
        return "passing" if row.last_verified_version_id == current_version_id else "unknown"
    if row.verify_status in ("failing", "regressed"):
        return row.verify_status if row.last_checked_version_id == current_version_id else "unknown"
    return "unknown"


def works_in_coverage_expr():
    return (
        select(func.count())
        .select_from(SkillPackageToolFreshness)
        .where(*passing_now_predicates())
        .scalar_subquery()
    )


def popularity_expr():
    return func.coalesce(
        select(SkillPackageFreshness.popularity_rank)
        .where(SkillPackageFreshness.package_id == SkillPackage.id)
        .scalar_subquery(),
        0,
    )


def relevance_expr(query: str | None):
    if not query:
        return literal(0)
    name = func.lower(SkillMarketplaceListing.name)
    return case(
        (name == query, 3),
        (name.startswith(query, autoescape=True), 2),
        (name.contains(query, autoescape=True), 1),
        else_=0,
    )


def current_rank_expr():
    """1 for the package's current version, else 0: older versions of the
    same package rank after it."""
    return case((SkillPackage.current_version_id == SkillPackageVersion.id, 1), else_=0)


def not_deprecated_expr():
    return case((SkillPackage.deprecated_at.is_(None), 1), else_=0)


def rank_columns(query: str | None) -> list:
    """The documented ranking (§9.2), most significant first; ties break on
    (published_at, listing id) so the order is total and deterministic:
    not deprecated, text relevance, verified publisher, works-in coverage,
    popularity bucket (log2 of 30-day installs), current version."""
    return [
        not_deprecated_expr(),
        relevance_expr(query),
        case((verified_publisher_expr(), 1), else_=0),
        works_in_coverage_expr(),
        popularity_expr(),
        current_rank_expr(),
    ]


def apply_filters(statement, filters: SearchFilters, *, now: datetime | None = None):
    if filters.category:
        statement = statement.where(
            exists(
                select(SkillPackageCategory.package_id).where(
                    SkillPackageCategory.package_id == SkillPackage.id,
                    SkillPackageCategory.category_slug == filters.category,
                )
            )
        )
    if filters.publisher:
        value = filters.publisher.strip()
        statement = statement.where(
            exists(
                select(SkillPublisher.id).where(
                    SkillPublisher.id == SkillPackage.publisher_id,
                    SkillPublisher.status != "tombstoned",
                    or_(SkillPublisher.id == value, func.lower(SkillPublisher.namespace) == value.casefold()),
                )
            )
        )
    if filters.verified is not None:
        expr = verified_publisher_expr()
        statement = statement.where(expr if filters.verified else ~expr)
    if filters.attested is not None:
        statement = statement.where(SkillMarketplaceListing.provenance_attested.is_(filters.attested))
    if filters.license:
        if filters.license.casefold() in ("none", "undeclared"):
            statement = statement.where(SkillPackageVersion.license_key.is_(None))
        else:
            statement = statement.where(
                SkillPackageVersion.license_key == discovery.normalize_license(filters.license)
            )
    if filters.max_severity:
        statement = statement.where(
            SkillPackageVersion.scan_severity_rank <= discovery.SEVERITY_RANK[filters.max_severity]
        )
    if filters.capability:
        if filters.capability == "instructions_only":
            statement = statement.where(SkillPackageVersion.capability_flags == "")
        else:
            statement = statement.where(
                SkillPackageVersion.capability_flags.contains(f",{filters.capability},", autoescape=True)
            )
    if filters.works_in:
        statement = statement.where(
            exists(
                select(SkillPackageToolFreshness.package_id).where(
                    *passing_now_predicates(),
                    SkillPackageToolFreshness.tool == filters.works_in,
                )
            )
        )
    if filters.updated_within:
        cutoff = (now or now_utc()) - timedelta(days=filters.updated_within)
        statement = statement.where(SkillMarketplaceListing.published_at >= cutoff)
    if not filters.include_deprecated:
        statement = statement.where(SkillPackage.deprecated_at.is_(None))
    return statement


# ---------------------------------------------------------------------------
# Response enrichment
# ---------------------------------------------------------------------------


def _since(days: int, today: date | None = None) -> date:
    return (today or now_utc().date()) - timedelta(days=days - 1)


async def attach_discovery(db: AsyncSession, items: list[dict], *, visible_package_ids) -> None:
    """Attach categories, deprecation (with the successor only when the
    caller can see it), freshness and public install counts (k-floored) to
    listing responses. ``visible_package_ids(ids)`` returns the subset of
    package ids the caller may see."""
    package_ids = sorted({str(item["package_id"]) for item in items if item.get("package_id")})
    if not package_ids:
        return
    categories: dict[str, list[str]] = {}
    for package_id, slug in (
        await db.execute(
            select(SkillPackageCategory.package_id, SkillPackageCategory.category_slug)
            .where(SkillPackageCategory.package_id.in_(package_ids))
            .order_by(SkillPackageCategory.package_id, SkillPackageCategory.category_slug)
        )
    ).all():
        categories.setdefault(package_id, []).append(slug)
    packages = {
        row.id: row
        for row in (
            await db.execute(select(SkillPackage).where(SkillPackage.id.in_(package_ids)))
        ).scalars().all()
    }
    successor_ids = sorted({p.successor_package_id for p in packages.values() if p.successor_package_id})
    successors: dict[str, dict] = {}
    if successor_ids:
        visible = set(await visible_package_ids(successor_ids))
        for row in (
            await db.execute(select(SkillPackage).where(SkillPackage.id.in_(sorted(visible))))
        ).scalars().all():
            successors[row.id] = {
                "package_id": row.id,
                "namespace": row.namespace,
                "name": row.name,
                "display_name": row.display_name,
            }
    freshness = {
        row.package_id: row
        for row in (
            await db.execute(
                select(SkillPackageFreshness).where(SkillPackageFreshness.package_id.in_(package_ids))
            )
        ).scalars().all()
    }
    tools: dict[str, dict[str, dict]] = {}
    for row in (
        await db.execute(
            select(SkillPackageToolFreshness)
            .where(SkillPackageToolFreshness.package_id.in_(package_ids))
            .order_by(SkillPackageToolFreshness.package_id, SkillPackageToolFreshness.tool)
        )
    ).scalars().all():
        owner = packages.get(row.package_id)
        tools.setdefault(row.package_id, {})[row.tool] = {
            "last_updated_at": _iso(row.last_updated_at),
            "last_verified_at": _iso(row.last_verified_at),
            "last_verified_version_id": row.last_verified_version_id,
            "verify_status": effective_status(row, owner.current_version_id if owner else None),
            "stale": row.stale,
        }
    since = _since(30)
    installs: dict[str, dict[str, int]] = {}
    cells: dict[str, list[int]] = {}
    # Finest cells (version × tool × day), the same ones publisher analytics
    # uses: one hidden cell coarsens every count shown for the package.
    for package_id, _version_id, tool, _day, total in (
        await db.execute(
            select(
                SkillInstallDaily.package_id,
                SkillInstallDaily.version_id,
                SkillInstallDaily.tool,
                SkillInstallDaily.day,
                func.sum(SkillInstallDaily.count),
            )
            .where(
                SkillInstallDaily.package_id.in_(package_ids),
                SkillInstallDaily.event == "install",
                SkillInstallDaily.actor_type == "user",
                SkillInstallDaily.day >= since,
            )
            .group_by(
                SkillInstallDaily.package_id,
                SkillInstallDaily.version_id,
                SkillInstallDaily.tool,
                SkillInstallDaily.day,
            )
        )
    ).all():
        count = int(total or 0)
        cells.setdefault(package_id, []).append(count)
        by_tool_counts = installs.setdefault(package_id, {})
        by_tool_counts[tool] = by_tool_counts.get(tool, 0) + count
    for item in items:
        package_id = str(item.get("package_id") or "")
        package = packages.get(package_id)
        item["categories"] = categories.get(package_id, [])
        if package is not None and package.deprecated_at is not None:
            item["deprecation"] = {
                "deprecated_at": _iso(package.deprecated_at),
                "message": package.deprecation_message,
                "successor": successors.get(package.successor_package_id or ""),
            }
        else:
            item["deprecation"] = None
        fresh = freshness.get(package_id)
        item["freshness"] = {
            "stale": bool(fresh.stale) if fresh else False,
            "unmaintained": bool(fresh.unmaintained) if fresh else False,
            "deprecated": item["deprecation"] is not None,
            "last_updated_at": _iso(fresh.last_version_at) if fresh else None,
            "computed_at": _iso(fresh.computed_at) if fresh else None,
            "tools": tools.get(package_id, {}),
        }
        by_tool = installs.get(package_id, {})
        shown = discovery.coarse_counter(cells.get(package_id, []))
        item["installs"] = {
            "window_days": 30,
            "total": shown(sum(by_tool.values())),
            "by_tool": {tool: shown(count) for tool, count in sorted(by_tool.items())},
        }


# ---------------------------------------------------------------------------
# Install events
# ---------------------------------------------------------------------------


def principal_hash(actor_type: str, principal_id: str) -> str:
    """Pseudonymous, stable principal key for the 30-day dedupe window."""
    return hashlib.sha256(f"sfs-skill-install:v1:{actor_type}:{principal_id}".encode()).hexdigest()


async def record_event(
    db: AsyncSession,
    *,
    package_id: str,
    version_id: str,
    tool: str,
    event: str,
    source: str,
    actor_type: str,
    principal_id: str,
    org_id: str | None,
    day: date | None = None,
) -> bool:
    """Count one event at most once per principal per (version, tool, event,
    day). True when it was counted."""
    if event not in discovery.INSTALL_EVENTS or source not in discovery.INSTALL_SOURCES:
        raise ValueError("unknown install event or source")
    actor = "service" if actor_type == "service" else "user"
    day = day or now_utc().date()
    dedupe = (await _dialect_insert(db, SkillInstallDedupe)).values(
        principal_hash=principal_hash(actor, principal_id),
        version_id=version_id,
        tool=tool,
        event=event,
        day=day,
        package_id=package_id,
        actor_type=actor,
    )
    inserted = await db.execute(dedupe.on_conflict_do_nothing())
    if not inserted.rowcount:
        return False
    aggregate = (await _dialect_insert(db, SkillInstallDaily)).values(
        version_id=version_id,
        tool=tool,
        source=source,
        event=event,
        actor_type=actor,
        org_id=org_id or "",
        day=day,
        package_id=package_id,
        count=1,
    )
    await db.execute(
        aggregate.on_conflict_do_update(
            index_elements=["version_id", "tool", "source", "event", "actor_type", "org_id", "day"],
            set_={"count": SkillInstallDaily.count + 1},
        )
    )
    return True


# ---------------------------------------------------------------------------
# Works-in results and freshness sweep
# ---------------------------------------------------------------------------


async def _tool_row(db: AsyncSession, package_id: str, tool: str) -> SkillPackageToolFreshness:
    row = await db.get(SkillPackageToolFreshness, (package_id, tool))
    if row is None:
        row = SkillPackageToolFreshness(package_id=package_id, tool=tool, verify_status="unknown", stale=False)
        db.add(row)
    return row


async def record_tool_verification(
    db: AsyncSession,
    *,
    version_id: str,
    tool: str,
    passed: bool,
    checked_at: datetime | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Record a works-in result (from the sandboxed harness). Only results for
    the package's CURRENT version change its standing: a pass marks the tool
    passing; a failure after a pass is a regression."""
    version = await db.get(SkillPackageVersion, version_id)
    if version is None:
        return {"version_id": version_id, "tool": tool, "recorded": False, "reason": "unknown_version"}
    package = await db.get(SkillPackage, version.package_id)
    if package is None or package.current_version_id != version.id:
        return {"version_id": version_id, "tool": tool, "recorded": False, "reason": "not_current_version"}
    checked = _utc(checked_at) or now_utc()
    row = await _tool_row(db, package.id, tool)
    if passed:
        row.verify_status = "passing"
        row.last_verified_at = checked
        row.last_verified_version_id = version.id
    else:
        # A regression: this tool worked with an earlier check (of this or a
        # previous version) and the current version now fails.
        row.verify_status = "regressed" if row.last_verified_at is not None else "failing"
    row.last_checked_at = checked
    row.last_checked_version_id = version.id
    row.verify_detail = (detail or "")[:300] or None
    return {"version_id": version_id, "tool": tool, "recorded": True, "verify_status": row.verify_status}


async def set_source_archived(db: AsyncSession, package_id: str, archived: bool) -> bool:
    if await db.get(SkillPackage, package_id) is None:
        return False
    row = await db.get(SkillPackageFreshness, package_id)
    if row is None:
        row = SkillPackageFreshness(package_id=package_id)
        db.add(row)
    row.source_archived = archived
    return True


async def _verified_github_publishers(db: AsyncSession, publisher_ids: list[str]) -> set[str]:
    if not publisher_ids:
        return set()
    return set(
        (
            await db.execute(
                select(SkillPublisherVerification.publisher_id).where(
                    SkillPublisherVerification.publisher_id.in_(publisher_ids),
                    SkillPublisherVerification.status == "verified",
                    SkillPublisherVerification.method.in_(("github_org", "github_user")),
                )
            )
        ).scalars().all()
    )


async def _lapsed_publishers(db: AsyncSession, publisher_ids: list[str]) -> set[str]:
    """Publishers whose verification lapsed: some lapsed/revoked-by-lapse row
    and no live one (system publishers never lapse)."""
    if not publisher_ids:
        return set()
    rows = (
        await db.execute(
            select(SkillPublisher.id, SkillPublisher.owner_type, SkillPublisherVerification.status)
            .join(SkillPublisherVerification, SkillPublisherVerification.publisher_id == SkillPublisher.id)
            .where(SkillPublisher.id.in_(publisher_ids))
        )
    ).all()
    live: set[str] = set()
    lapsed: set[str] = set()
    for publisher_id, owner_type, status in rows:
        if owner_type == "system":
            continue
        if status == "verified":
            live.add(publisher_id)
        elif status == "lapsed":
            lapsed.add(publisher_id)
    return lapsed - live


async def sweep_freshness(
    db: AsyncSession,
    *,
    limit: int = 500,
    cursor: str | None = None,
    time_budget_seconds: float = 20.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Recompute freshness for up to ``limit`` packages after ``cursor``
    (package id order) and prune expired dedupe rows. Idempotent; pass
    ``next_cursor`` back until ``done``."""
    started = time.monotonic()
    now = _utc(now) or now_utc()
    today = now.date()
    stale_cutoff = now - timedelta(days=discovery.STALE_AFTER_DAYS)
    pruned = 0
    if cursor is None:
        result = await db.execute(
            delete(SkillInstallDedupe).where(
                SkillInstallDedupe.day < today - timedelta(days=discovery.DEDUPE_RETENTION_DAYS)
            )
        )
        pruned = int(result.rowcount or 0)
    statement = select(SkillPackage).order_by(SkillPackage.id).limit(limit)
    if cursor:
        statement = statement.where(SkillPackage.id > cursor)
    packages = list((await db.execute(statement)).scalars().all())
    ids = [package.id for package in packages]
    processed = stale = unmaintained = 0
    if ids:
        last_version = dict(
            (
                await db.execute(
                    select(SkillPackageVersion.package_id, func.max(SkillPackageVersion.created_at))
                    .where(SkillPackageVersion.package_id.in_(ids))
                    .group_by(SkillPackageVersion.package_id)
                )
            ).all()
        )
        per_tool: dict[str, dict[str, datetime]] = {}
        for package_id, tool, latest in (
            await db.execute(
                select(SkillPackageVersion.package_id, SkillVersionTool.tool, func.max(SkillPackageVersion.created_at))
                .join(SkillVersionTool, SkillVersionTool.version_id == SkillPackageVersion.id)
                .where(SkillPackageVersion.package_id.in_(ids))
                .group_by(SkillPackageVersion.package_id, SkillVersionTool.tool)
            )
        ).all():
            per_tool.setdefault(package_id, {})[tool] = latest
        existing_tools: dict[str, dict[str, SkillPackageToolFreshness]] = {}
        for row in (
            await db.execute(select(SkillPackageToolFreshness).where(SkillPackageToolFreshness.package_id.in_(ids)))
        ).scalars().all():
            existing_tools.setdefault(row.package_id, {})[row.tool] = row
        freshness = {
            row.package_id: row
            for row in (
                await db.execute(select(SkillPackageFreshness).where(SkillPackageFreshness.package_id.in_(ids)))
            ).scalars().all()
        }
        installs: dict[tuple[str, str], int] = {}
        for package_id, actor, total in (
            await db.execute(
                select(SkillInstallDaily.package_id, SkillInstallDaily.actor_type, func.sum(SkillInstallDaily.count))
                .where(
                    SkillInstallDaily.package_id.in_(ids),
                    SkillInstallDaily.event == "install",
                    SkillInstallDaily.day >= _since(30, today),
                )
                .group_by(SkillInstallDaily.package_id, SkillInstallDaily.actor_type)
            )
        ).all():
            installs[(package_id, actor)] = int(total or 0)
        publisher_ids = sorted({p.publisher_id for p in packages if p.publisher_id})
        github_verified = await _verified_github_publishers(db, publisher_ids)
        lapsed = await _lapsed_publishers(db, publisher_ids)
        for package in packages:
            if processed and time.monotonic() - started > time_budget_seconds:
                break
            tool_rows = existing_tools.get(package.id, {})
            for tool, latest in per_tool.get(package.id, {}).items():
                if tool not in tool_rows:
                    tool_rows[tool] = SkillPackageToolFreshness(
                        package_id=package.id, tool=tool, verify_status="unknown"
                    )
                    db.add(tool_rows[tool])
                tool_rows[tool].last_updated_at = latest
            regressed_tools = set()
            for tool, row in tool_rows.items():
                updated = _utc(row.last_updated_at)
                # Only a regression of the CURRENT version counts (§9.2).
                regressed = effective_status(row, package.current_version_id) == "regressed"
                if regressed:
                    regressed_tools.add(tool)
                row.stale = bool(updated is not None and updated < stale_cutoff and regressed)
                row.computed_at = now
            row_f = freshness.get(package.id)
            if row_f is None:
                row_f = SkillPackageFreshness(package_id=package.id, source_archived=False)
                db.add(row_f)
            last = _utc(last_version.get(package.id))
            row_f.last_version_at = last
            row_f.works_in_regressed = bool(regressed_tools)
            archived_counts = bool(row_f.source_archived) and package.publisher_id in github_verified
            row_f.stale = bool(
                last is not None and last < stale_cutoff and (row_f.works_in_regressed or archived_counts)
            )
            row_f.unmaintained = package.publisher_id in lapsed
            user_installs = installs.get((package.id, "user"), 0)
            service_installs = installs.get((package.id, "service"), 0)
            row_f.installs_30d = user_installs
            row_f.service_installs_30d = service_installs
            row_f.popularity_rank = discovery.popularity_rank(user_installs, service_installs)
            row_f.computed_at = now
            processed += 1
            stale += int(row_f.stale)
            unmaintained += int(row_f.unmaintained)
    done = processed == len(packages) and len(packages) < limit
    next_cursor = ids[processed - 1] if processed and not done else None
    return {
        "processed": processed,
        "stale": stale,
        "unmaintained": unmaintained,
        "pruned_dedupe_rows": pruned,
        "next_cursor": next_cursor,
        "done": done,
        "computed_at": now.isoformat(),
    }


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


async def _active_installs(db: AsyncSession, package_ids: list[str], since: date) -> dict[str, int]:
    """Distinct principals with an install or reconcile ping in the window
    (an estimate: the dedupe table keeps 30 days)."""
    if not package_ids:
        return {}
    rows = (
        await db.execute(
            select(SkillInstallDedupe.package_id, func.count(func.distinct(SkillInstallDedupe.principal_hash)))
            .where(
                SkillInstallDedupe.package_id.in_(package_ids),
                SkillInstallDedupe.event.in_(("install", "active")),
                # Service-key installs are counted separately (§9.5).
                SkillInstallDedupe.actor_type == "user",
                SkillInstallDedupe.day >= since,
            )
            .group_by(SkillInstallDedupe.package_id)
        )
    ).all()
    return {package_id: int(count or 0) for package_id, count in rows}


async def publisher_analytics(db: AsyncSession, publisher: SkillPublisher, *, days: int = 30) -> dict[str, Any]:
    """Aggregates for every package under the publisher. Every count a
    publisher sees is k-floored; consuming orgs are never identified."""
    today = now_utc().date()
    since = _since(days, today)
    packages = list(
        (
            await db.execute(
                select(SkillPackage).where(SkillPackage.publisher_id == publisher.id).order_by(SkillPackage.name, SkillPackage.id)
            )
        ).scalars().all()
    )
    ids = [package.id for package in packages]
    package_by_id = {package.id: package for package in packages}
    rows = []
    if ids:
        rows = (
            await db.execute(
                select(
                    SkillInstallDaily.package_id,
                    SkillInstallDaily.version_id,
                    SkillInstallDaily.tool,
                    SkillInstallDaily.event,
                    SkillInstallDaily.actor_type,
                    SkillInstallDaily.day,
                    func.sum(SkillInstallDaily.count),
                )
                .where(SkillInstallDaily.package_id.in_(ids), SkillInstallDaily.day >= since)
                .group_by(
                    SkillInstallDaily.package_id,
                    SkillInstallDaily.version_id,
                    SkillInstallDaily.tool,
                    SkillInstallDaily.event,
                    SkillInstallDaily.actor_type,
                    SkillInstallDaily.day,
                )
            )
        ).all()
    totals: dict[str, int] = {event: 0 for event in discovery.INSTALL_EVENTS}
    service_installs = 0
    by_tool: dict[str, dict[str, int]] = {}
    by_day: dict[date, int] = {}
    per_package: dict[str, dict[str, Any]] = {
        package_id: {"events": {event: 0 for event in discovery.INSTALL_EVENTS}, "by_version": {}, "failures_by_tool": {}, "by_tool": {}}
        for package_id in ids
    }
    for package_id, version_id, tool, event, actor, day, count in rows:
        count = int(count or 0)
        if actor == "service":
            if event == "install":
                service_installs += count
            continue
        totals[event] += count
        tool_counts = by_tool.setdefault(tool, {e: 0 for e in discovery.INSTALL_EVENTS})
        tool_counts[event] += count
        entry = per_package[package_id]
        entry["events"][event] += count
        if event == "install":
            day_value = day if isinstance(day, date) else date.fromisoformat(str(day))
            by_day[day_value] = by_day.get(day_value, 0) + count
            entry["by_version"][version_id] = entry["by_version"].get(version_id, 0) + count
            entry["by_tool"][tool] = entry["by_tool"].get(tool, 0) + count
        elif event == "translation_failure":
            entry["failures_by_tool"][tool] = entry["failures_by_tool"].get(tool, 0) + count
    active = await _active_installs(db, ids, max(since, _since(discovery.DEDUPE_RETENTION_DAYS, today)))
    versions: dict[str, SkillPackageVersion] = {}
    if ids:
        versions = {
            row.id: row
            for row in (
                await db.execute(select(SkillPackageVersion).where(SkillPackageVersion.package_id.in_(ids)))
            ).scalars().all()
        }
    tool_fresh: dict[str, list[dict]] = {}
    freshness: dict[str, SkillPackageFreshness] = {}
    reports: dict[str, int] = {}
    if ids:
        for row in (
            await db.execute(
                select(SkillPackageToolFreshness)
                .where(SkillPackageToolFreshness.package_id.in_(ids))
                .order_by(SkillPackageToolFreshness.tool)
            )
        ).scalars().all():
            owner = package_by_id.get(row.package_id)
            tool_fresh.setdefault(row.package_id, []).append(
                {
                    "tool": row.tool,
                    "verify_status": effective_status(row, owner.current_version_id if owner else None),
                    "last_verified_at": _iso(row.last_verified_at),
                    "last_updated_at": _iso(row.last_updated_at),
                    "stale": row.stale,
                }
            )
        freshness = {
            row.package_id: row
            for row in (
                await db.execute(select(SkillPackageFreshness).where(SkillPackageFreshness.package_id.in_(ids)))
            ).scalars().all()
        }
        reports = dict(
            (
                await db.execute(
                    select(SkillPackageReport.package_id, func.count())
                    .where(SkillPackageReport.package_id.in_(ids), SkillPackageReport.status == "open")
                    .group_by(SkillPackageReport.package_id)
                )
            ).all()
        )
    package_categories: dict[str, list[str]] = {}
    if ids:
        for package_id, slug in (
            await db.execute(
                select(SkillPackageCategory.package_id, SkillPackageCategory.category_slug)
                .where(SkillPackageCategory.package_id.in_(ids))
                .order_by(SkillPackageCategory.package_id, SkillPackageCategory.category_slug)
            )
        ).all():
            package_categories.setdefault(package_id, []).append(slug)
    # One coarsening rule per event (marketplace v2 §9.5): if any finest
    # cell (package × version × tool × day) of the window is hidden, every
    # count of that event in this response is rounded down to a multiple of
    # 10, so nothing hidden can be recovered by differencing.
    fine: dict[str, list[int]] = {event: [] for event in discovery.INSTALL_EVENTS}
    for _package_id, _version_id, _tool, event, actor, _day, count in rows:
        if actor != "service":
            fine[event].append(int(count or 0))
    shown = {event: discovery.coarse_counter(cells) for event, cells in fine.items()}
    floored = discovery.floored
    package_items: list[dict[str, Any]] = []
    for package in packages:
        entry = per_package[package.id]
        events = entry["events"]
        current = versions.get(package.current_version_id or "")
        scan = {}
        if current is not None:
            try:
                scan = json.loads(current.scan_summary or "{}")
            except ValueError:
                scan = {}
        by_version = sorted(entry["by_version"].items(), key=lambda kv: (-kv[1], kv[0]))
        fresh = freshness.get(package.id)
        installs_by_tool = {tool: shown["install"](c) for tool, c in sorted(entry["by_tool"].items())}
        failures_by_tool = {
            tool: shown["translation_failure"](c) for tool, c in sorted(entry["failures_by_tool"].items())
        }
        installs_by_version = [
            {
                "version_id": version_id,
                "version": versions[version_id].version_label if version_id in versions else None,
                "installs": shown["install"](count),
            }
            for version_id, count in by_version
        ]
        # A total shown next to its breakdown is rounded when any part is
        # hidden, so the hidden part cannot be subtracted back (§9.5).
        installs = shown["install"](events["install"])
        uninstalls = shown["uninstall"](events["uninstall"])
        package_items.append(
            {
                "package_id": package.id,
                "namespace": package.namespace,
                "name": package.name,
                "display_name": package.display_name,
                "current_version_id": package.current_version_id,
                "current_version": current.version_label if current else None,
                "installs": installs,
                "active_installs": floored(active.get(package.id, 0)),
                "uninstalls": uninstalls,
                "uninstall_rate": discovery.displayed_rate(uninstalls, installs),
                "translation_failures": shown["translation_failure"](events["translation_failure"]),
                "installs_by_tool": installs_by_tool,
                "translation_failures_by_tool": failures_by_tool,
                # Update adoption: installs per version in the window.
                "installs_by_version": installs_by_version,
                "works_in": tool_fresh.get(package.id, []),
                "works_in_regressions": [row["tool"] for row in tool_fresh.get(package.id, []) if row["verify_status"] == "regressed"],
                "stale": bool(fresh.stale) if fresh else False,
                "unmaintained": bool(fresh.unmaintained) if fresh else False,
                "deprecated": package.deprecated_at is not None,
                # Current settings (PATCH /publishers/{id}/packages/{pid}).
                "categories": package_categories.get(package.id, []),
                "deprecation_message": package.deprecation_message,
                "successor_package_id": package.successor_package_id,
                "readme_path": package.readme_path,
                "scan": {
                    "max_severity": scan.get("open_max_severity", scan.get("max_severity")),
                    "counts": scan.get("counts") if isinstance(scan.get("counts"), dict) else {},
                },
                "open_reports": int(reports.get(package.id, 0)),
            }
        )
    tool_rows: list[dict[str, Any]] = []
    for tool, counts in sorted(by_tool.items()):
        # Per-tool totals are also sums of the packages' per-tool parts.
        tool_installs = shown["install"](counts["install"])
        tool_uninstalls = shown["uninstall"](counts["uninstall"])
        tool_rows.append(
            {
                "tool": tool,
                "installs": tool_installs,
                "active": shown["active"](counts["active"]),
                "uninstalls": tool_uninstalls,
                "uninstall_rate": discovery.displayed_rate(tool_uninstalls, tool_installs),
                "translation_failures": shown["translation_failure"](counts["translation_failure"]),
            }
        )
    day_rows: list[dict[str, Any]] = [
        {"day": (since + timedelta(days=offset)).isoformat(),
         "installs": shown["install"](by_day.get(since + timedelta(days=offset), 0))}
        for offset in range((today - since).days + 1)
    ]
    total_installs = shown["install"](totals["install"])
    total_uninstalls = shown["uninstall"](totals["uninstall"])
    # Active installs are distinct accounts (not a sum): the total is
    # rounded whenever a package's value is hidden.
    total_active = discovery.floored_group(
        sum(active.values()), [item["active_installs"] for item in package_items]
    )
    return {
        "publisher_id": publisher.id,
        "namespace": publisher.namespace,
        "window_days": days,
        "since": since.isoformat(),
        "k_floor": discovery.K_FLOOR,
        "totals": {
            "installs": total_installs,
            "active_installs": total_active,
            "uninstalls": total_uninstalls,
            "uninstall_rate": discovery.displayed_rate(total_uninstalls, total_installs),
            "translation_failures": shown["translation_failure"](totals["translation_failure"]),
            "service_installs": floored(service_installs),
        },
        "by_tool": tool_rows,
        "by_day": day_rows,
        "packages": package_items,
    }


async def org_usage(db: AsyncSession, org_id: str, *, days: int = 30) -> dict[str, Any]:
    """The org's own members' usage by package and tool (exact counts: org
    admins administer these members; no other org's data is included)."""
    today = now_utc().date()
    since = _since(days, today)
    rows = (
        await db.execute(
            select(
                SkillInstallDaily.package_id,
                SkillInstallDaily.tool,
                SkillInstallDaily.event,
                func.sum(SkillInstallDaily.count),
            )
            .where(
                SkillInstallDaily.org_id == org_id,
                SkillInstallDaily.day >= since,
                # Only packages this org can see: a public listing or its own
                # org listing (never a member's personal or another org's
                # private package, whatever the stored attribution says).
                exists(
                    select(SkillMarketplaceListing.id).where(
                        SkillMarketplaceListing.package_id == SkillInstallDaily.package_id,
                        or_(
                            SkillMarketplaceListing.visibility == "public",
                            and_(
                                SkillMarketplaceListing.visibility == "org",
                                SkillMarketplaceListing.org_id == org_id,
                            ),
                        ),
                    )
                ),
            )
            .group_by(SkillInstallDaily.package_id, SkillInstallDaily.tool, SkillInstallDaily.event)
        )
    ).all()
    per: dict[str, dict[str, dict[str, int]]] = {}
    for package_id, tool, event, count in rows:
        per.setdefault(package_id, {}).setdefault(tool, {e: 0 for e in discovery.INSTALL_EVENTS})[event] += int(count or 0)
    ids = sorted(per)
    packages = {
        row.id: row
        for row in (await db.execute(select(SkillPackage).where(SkillPackage.id.in_(ids)))).scalars().all()
    } if ids else {}
    freshness = {
        row.package_id: row
        for row in (
            await db.execute(select(SkillPackageFreshness).where(SkillPackageFreshness.package_id.in_(ids)))
        ).scalars().all()
    } if ids else {}
    items: list[dict[str, Any]] = []
    for package_id in ids:
        package = packages.get(package_id)
        if package is None:
            continue
        tools = per[package_id]
        fresh = freshness.get(package_id)
        items.append(
            {
                "package_id": package_id,
                "namespace": package.namespace,
                "name": package.name,
                "display_name": package.display_name,
                "installs": sum(t["install"] for t in tools.values()),
                "active": sum(t["active"] for t in tools.values()),
                "uninstalls": sum(t["uninstall"] for t in tools.values()),
                "translation_failures": sum(t["translation_failure"] for t in tools.values()),
                "by_tool": [
                    {"tool": tool, "installs": c["install"], "active": c["active"],
                     "uninstalls": c["uninstall"], "translation_failures": c["translation_failure"]}
                    for tool, c in sorted(tools.items())
                ],
                "stale": bool(fresh.stale) if fresh else False,
                "unmaintained": bool(fresh.unmaintained) if fresh else False,
                "deprecated": package.deprecated_at is not None,
            }
        )
    items.sort(key=lambda item: (-int(item["installs"]), str(item["name"]), str(item["package_id"])))
    return {
        "org_id": org_id,
        "window_days": days,
        "since": since.isoformat(),
        "items": items,
        "stale_or_deprecated": [
            item["package_id"] for item in items if item["stale"] or item["deprecated"] or item["unmaintained"]
        ],
    }


# ---------------------------------------------------------------------------
# README and package images
# ---------------------------------------------------------------------------


class DiscoveryBusy(Exception):
    """Every archive-read slot of this process is taken (503 + Retry-After)."""


# Archive reads (README, package images) run off the event loop, at most
# this many at a time per process. A request that finds them all taken is
# refused at once (503 discovery_busy) instead of queueing work. Results are
# cached (archives are immutable), so only cache misses take a slot.
ARCHIVE_READ_SLOTS = 4
DISCOVERY_RETRY_AFTER_SECONDS = 5
_archive_slots_in_use = 0


@contextlib.contextmanager
def _archive_read_slot():
    global _archive_slots_in_use
    if _archive_slots_in_use >= ARCHIVE_READ_SLOTS:
        raise DiscoveryBusy()
    _archive_slots_in_use += 1
    try:
        yield
    finally:
        _archive_slots_in_use -= 1


def _normalize_member(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name


def _read_member(payload: bytes, wanted, limit: int) -> tuple[str, bytes] | None:
    """Stream the gzip tar and return the FIRST regular member ``wanted``
    accepts, reading at most ``limit + 1`` bytes of it, then stop. Nothing
    is written to disk; decompression is byte-budgeted (the publish
    validator's limits) and the member count capped. Links, devices and
    directories are never read. Any archive error reads as "not found"."""
    import gzip
    import tarfile
    import zlib

    from sessionfs.skills.publish_validation import (
        MAX_ARCHIVE_MEMBERS,
        MAX_DECOMPRESSED_BYTES,
        _BudgetedReader,
    )

    reader = _BudgetedReader(gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb"), MAX_DECOMPRESSED_BYTES)
    try:
        with tarfile.open(fileobj=reader, mode="r|") as archive:
            for count, member in enumerate(archive, start=1):
                if count > MAX_ARCHIVE_MEMBERS:
                    return None
                if not member.isreg():
                    continue
                name = _normalize_member(member.name)
                if not wanted(name):
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    return None
                return name, handle.read(limit + 1)
    except (tarfile.TarError, OSError, EOFError, zlib.error, ValueError):
        return None
    except Exception:  # noqa: BLE001 - budget overruns and the like read as missing
        return None
    return None


def _readme_member(readme_path: str | None):
    def wanted(name: str) -> bool:
        if readme_path:
            return name == readme_path
        return "/" not in name and name.casefold() in discovery.README_NAMES

    return wanted


# Bounded per-process LRU caches. Archives are immutable (one blob per
# version), so a cached read never goes stale; visibility is still checked
# by the route on every request. Misses ARE cached for images (a missing
# path stays missing), but never when the blob itself was unavailable.
_README_CACHE: "OrderedDict[tuple[str, str], tuple[str, bytes] | None]" = OrderedDict()
_README_CACHE_ENTRIES = 128
_IMAGE_CACHE: "OrderedDict[tuple[str, str], tuple[bytes, str] | None]" = OrderedDict()
_IMAGE_CACHE_ENTRIES = 64


def _cache_get(cache: OrderedDict, key: tuple[str, str]) -> tuple[bool, Any]:
    if key in cache:
        cache.move_to_end(key)
        return True, cache[key]
    return False, None


def _cache_put(cache: OrderedDict, key: tuple[str, str], value: Any, entries: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > entries:
        cache.popitem(last=False)


async def _read_archive_member(store: BlobStore, version: SkillPackageVersion, wanted, limit: int):
    """(fetched, member): fetched is False when the blob was unavailable."""
    with _archive_read_slot():
        payload = await store.get(version.blob_key)
        if payload is None:
            return False, None
        return True, await asyncio.to_thread(_read_member, payload, wanted, limit)


async def load_readme(store: BlobStore, package: SkillPackage, version: SkillPackageVersion) -> dict[str, Any]:
    key = (version.id, package.readme_path or "")
    hit, found = _cache_get(_README_CACHE, key)
    if not hit:
        fetched, found = await _read_archive_member(
            store, version, _readme_member(package.readme_path), discovery.README_MAX_BYTES
        )
        if fetched:
            _cache_put(_README_CACHE, key, found, _README_CACHE_ENTRIES)
    source = "readme"
    if found is not None:
        path, raw = found
        safe = discovery.sanitize_readme(raw)
    else:
        try:
            bundle = json.loads(version.instruction_bundle or "[]")
        except ValueError:
            bundle = []
        fallback = discovery.first_skill_body(bundle if isinstance(bundle, list) else [])
        if fallback is None:
            return {"version_id": version.id, "source": "none", "path": None, "content": "",
                    "hidden_unicode": [], "truncated": False, "untrusted": True}
        path, text_value = fallback
        source = "skill_md"
        safe = discovery.sanitize_readme(text_value)
    return {
        "version_id": version.id,
        "source": source,
        "path": path,
        "content": safe.content,
        "hidden_unicode": safe.hidden_unicode,
        "truncated": safe.truncated,
        # Publisher-controlled text: clients render it without raw HTML,
        # with nofollow links and package-only images.
        "untrusted": True,
    }


async def load_image(store: BlobStore, version: SkillPackageVersion, path: str) -> tuple[bytes, str] | None:
    if not discovery.valid_package_path(path):
        return None
    key = (version.id, path)
    hit, cached = _cache_get(_IMAGE_CACHE, key)
    if hit:
        return cached
    fetched, member = await _read_archive_member(
        store, version, lambda name: name == path, discovery.IMAGE_MAX_BYTES
    )
    result: tuple[bytes, str] | None = None
    if member is not None:
        data = member[1]
        media_type = discovery.image_media_type(path, data) if len(data) <= discovery.IMAGE_MAX_BYTES else None
        result = (data, media_type) if media_type else None
    if fetched:
        _cache_put(_IMAGE_CACHE, key, result, _IMAGE_CACHE_ENTRIES)
    return result


def unique_tools(tools: Iterable[str]) -> list[str]:
    return sorted(dict.fromkeys(tools))

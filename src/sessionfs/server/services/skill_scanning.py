"""Instruction-scan persistence, scheduled rescans and automated quarantine.

The scanner itself is :mod:`sessionfs.skills.instruction_scan` (shared with
the CLI). This module stores its results per version and runs rescans:

- Publish: the shared validator already refused critical findings; the
  accepted result (high / medium / low) is stored as a ``publish`` scan and
  summarized on the version.
- Rescan: a new ruleset, the weekly cadence, or an admin request re-scans
  every live version from its stored canonical archive. Findings are diffed
  against the version's previous scan, and only NEW unsuppressed critical
  findings act.
- Acting (marketplace v2 §5.6, CEO decision D3): the version is QUARANTINED
  (no new installs, approvals, assignments or MCP loads; existing installs
  are warned through a ``quarantined`` revocation-feed event) and a system
  moderation report is opened. Nothing is ever blocked, uninstalled or
  revoked automatically: only a human moderator's block removes a version
  from machines, and a moderator's restore records the false positive.

No Redis, no queue: the rescan is a bounded, resumable batch driven by an
admin endpoint (invoked by Cloud Scheduler), with a cursor and a time budget.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    SkillMarketplaceListing,
    SkillPackage,
    SkillPackageReport,
    SkillPackageVersion,
    SkillScanFinding,
    SkillScanRuleset,
    SkillVersionScan,
)
from sessionfs.server.storage.base import BlobStore
from sessionfs.skills.instruction_scan import (
    RULES,
    RULESET_VERSION,
    SEVERITY_RANK,
    ScanFinding,
    ScanResult,
)
from sessionfs.skills.publish_validation import HostedSkillPackageError, scan_canonical_archive

logger = logging.getLogger(__name__)

RESCAN_INTERVAL = timedelta(days=7)
MAX_STORED_FINDINGS = 500
SYSTEM_ACTOR_ROLE = "system"
_MAX_REPORT_DETAILS = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_ruleset(db: AsyncSession) -> None:
    """Register the running ruleset version (idempotent, race-safe)."""
    connection = await db.connection()
    values = {
        "version": RULESET_VERSION,
        "rule_count": len(RULES),
        "notes": "",
        "released_at": _now(),
    }
    if connection.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        await db.execute(pg_insert(SkillScanRuleset).values(**values).on_conflict_do_nothing())
    else:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        await db.execute(sqlite_insert(SkillScanRuleset).values(**values).on_conflict_do_nothing())


def _finding_key(rule_id: str, path: str, line_sha: str) -> tuple[str, str, str]:
    return rule_id, path, line_sha


def _summary(
    result: ScanResult, suppressed: int = 0, open_max_severity: str | None = None
) -> str:
    """The bounded card summary: the scan's own summary, plus how many of its
    findings are suppressed and the worst severity still OPEN (unsuppressed)."""
    summary = result.summary()
    summary["suppressed"] = suppressed
    summary["open_max_severity"] = open_max_severity if open_max_severity is not None else result.max_severity
    return json.dumps(summary, sort_keys=True)


def _summary_from_rows(
    scan: SkillVersionScan, rows: list[SkillScanFinding], previous_summary: str
) -> str:
    """Recompute the version's summary from its latest scan's stored rows
    (after suppressions changed, e.g. a moderator restore)."""
    try:
        previous = json.loads(previous_summary or "{}")
    except ValueError:
        previous = {}
    result = ScanResult(
        ruleset_version=scan.ruleset_version,
        findings=tuple(
            ScanFinding(
                rule_id=row.rule_id,
                family=row.family,
                severity=row.severity,
                path=row.file_path,
                line=row.line,
                column=row.col,
                message=row.message,
                line_sha=row.line_sha,
            )
            for row in rows
        ),
        scanned_files=int(previous.get("scanned_files") or 0) if isinstance(previous, dict) else 0,
        truncated=bool(scan.truncated),
    )
    open_rows = [row for row in rows if not row.suppressed]
    open_max = max((row.severity for row in open_rows), key=lambda value: SEVERITY_RANK[value], default="none")
    return _summary(result, suppressed=len(rows) - len(open_rows), open_max_severity=open_max)


def _new_scan_id() -> str:
    return f"svs_{secrets.token_hex(12)}"


@dataclass(frozen=True)
class _Prior:
    """Plain snapshot of a previous finding (ORM rows may be deleted)."""

    suppressed: bool
    suppression_justification: str | None
    first_seen_scan_id: str
    severity: str


def _escalated(before: _Prior | None, severity: str) -> bool:
    """The finding is worse than when it was last seen (or is new)."""
    return before is None or SEVERITY_RANK[severity] > SEVERITY_RANK.get(before.severity, 0)


def _finding_rows(
    scan_id: str,
    findings: tuple[ScanFinding, ...],
    previous: dict[tuple[str, str, str], _Prior],
) -> list[SkillScanFinding]:
    rows = []
    for finding in findings[:MAX_STORED_FINDINGS]:
        before = previous.get(_finding_key(finding.rule_id, finding.path, finding.line_sha))
        rows.append(
            SkillScanFinding(
                scan_id=scan_id,
                rule_id=finding.rule_id[:80],
                family=finding.family[:40],
                severity=finding.severity,
                file_path=finding.path[:500],
                line=finding.line,
                col=finding.column,
                message=finding.message[:300],
                line_sha=finding.line_sha,
                # A moderator-accepted false positive stays accepted for the
                # same rule on the same (unchanged) line - but only at the
                # severity it was accepted at. A ruleset that ESCALATES the
                # finding clears the suppression: the acceptance was a
                # judgement about a lower risk, so the escalated finding is
                # treated as new and reviewed again.
                suppressed=bool(
                    before is not None and before.suppressed and not _escalated(before, finding.severity)
                ),
                suppression_justification=(
                    before.suppression_justification
                    if before is not None and not _escalated(before, finding.severity)
                    else None
                ),
                first_seen_scan_id=before.first_seen_scan_id if before else scan_id,
            )
        )
    return rows


def stage_publish_scan(
    db: AsyncSession, version: SkillPackageVersion, result: ScanResult | None
) -> None:
    """Stage the publish-time scan of a NEW version (same transaction, no
    I/O; the caller ran ensure_ruleset() first)."""
    if result is None:
        return
    scan_id = _new_scan_id()
    version.scan_summary = _summary(result)
    db.add(
        SkillVersionScan(
            id=scan_id,
            version_id=version.id,
            ruleset_version=result.ruleset_version,
            scanned_at=_now(),
            max_severity=result.max_severity,
            finding_count=len(result.findings),
            new_finding_count=len(result.findings),
            truncated=result.truncated,
            trigger="publish",
        )
    )
    for row in _finding_rows(scan_id, result.findings, {}):
        db.add(row)


async def latest_scan(db: AsyncSession, version_id: str) -> SkillVersionScan | None:
    return (
        await db.execute(
            select(SkillVersionScan)
            .where(SkillVersionScan.version_id == version_id)
            .order_by(SkillVersionScan.scanned_at.desc(), SkillVersionScan.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def scan_findings(db: AsyncSession, scan_id: str) -> list[SkillScanFinding]:
    return list(
        (
            await db.execute(
                select(SkillScanFinding)
                .where(SkillScanFinding.scan_id == scan_id)
                .order_by(SkillScanFinding.id)
                .limit(MAX_STORED_FINDINGS)
            )
        )
        .scalars()
        .all()
    )


async def _version_blocked(db: AsyncSession, version: SkillPackageVersion) -> bool:
    """Already taken down by a human block (any layer, or the durable content
    blocklist): quarantine would add nothing."""
    if version.dlp_status == "blocked":
        return True
    from sessionfs.server.db.models import SkillPackage
    from sessionfs.server.routes.marketplace import _has_active_block

    package = await db.get(SkillPackage, version.package_id)
    if package is None:
        return True
    return await _has_active_block(db, package=package, version=version)


@dataclass
class RescanOutcome:
    version_id: str
    action: str  # "clean" | "flagged" | "quarantined" | "reported" | "skipped"
    max_severity: str = "none"
    new_findings: int = 0
    new_critical_rules: list[str] = field(default_factory=list)


async def apply_scan_result(
    db: AsyncSession,
    version_id: str,
    result: ScanResult,
    *,
    trigger: str = "rescan",
    enforce: bool = True,
) -> RescanOutcome:
    """Store one rescan result and act on its critical findings. Commits.

    With ``enforce``, ANY current unsuppressed critical quarantines a version
    that is not quarantined yet - including criticals first stored by a
    report-only pass (enforcement switched on later must still act on them).
    "New" (unseen, or escalated by a new ruleset) only decides whether a
    report-only pass reports, and which rules the outcome lists as new.

    Locks the version row first (the moderation lock order: version, then
    package, then the revocation writer lock), so a concurrent block,
    restore or second sweeper sees a consistent state."""
    # Lock order shared with moderation and every grant path (routes/
    # marketplace.py): version row, then the content-hash lock, then (at event
    # insert) the revocation writer lock. A grant racing the quarantine either
    # commits first or re-checks deliverability after we commit.
    from sessionfs.server.routes.marketplace import lock_content_hashes

    version = (
        await db.execute(
            select(SkillPackageVersion)
            .where(SkillPackageVersion.id == version_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if version is None:
        await db.rollback()
        return RescanOutcome(version_id, "skipped")
    await lock_content_hashes(db, [version.content_hash])

    previous_scan = await latest_scan(db, version.id)
    previous_rows = await scan_findings(db, previous_scan.id) if previous_scan else []
    previous = {
        _finding_key(row.rule_id, row.file_path, row.line_sha): _Prior(
            suppressed=bool(row.suppressed),
            suppression_justification=row.suppression_justification,
            first_seen_scan_id=row.first_seen_scan_id or row.scan_id,
            severity=row.severity,
        )
        for row in previous_rows
    }
    # Snapshotted above; drop the ORM rows so a re-run that replaces this
    # scan's findings never collides with them in the identity map.
    for row in previous_rows:
        db.expunge(row)

    existing = (
        await db.execute(
            select(SkillVersionScan).where(
                SkillVersionScan.version_id == version.id,
                SkillVersionScan.ruleset_version == result.ruleset_version,
            )
        )
    ).scalar_one_or_none()
    now = _now()
    if existing is None:
        scan = SkillVersionScan(id=_new_scan_id(), version_id=version.id, ruleset_version=result.ruleset_version)
        db.add(scan)
    else:
        scan = existing
        await db.execute(
            delete(SkillScanFinding)
            .where(SkillScanFinding.scan_id == scan.id)
            .execution_options(synchronize_session=False)
        )
    rows = _finding_rows(scan.id, result.findings, previous)
    # "New" = not seen before, OR seen at a lower severity (a new ruleset
    # escalated it): an escalation to critical must act like a new critical.
    new_rows = [
        row
        for row in rows
        if _escalated(previous.get(_finding_key(row.rule_id, row.file_path, row.line_sha)), row.severity)
    ]
    scan.scanned_at = now
    scan.max_severity = result.max_severity
    scan.finding_count = len(result.findings)
    scan.new_finding_count = len(new_rows)
    scan.truncated = result.truncated
    scan.trigger = trigger
    await db.flush()
    for row in rows:
        db.add(row)
    open_rows = [row for row in rows if not row.suppressed]
    version.scan_summary = _summary(
        result,
        suppressed=len(rows) - len(open_rows),
        open_max_severity=max(
            (row.severity for row in open_rows), key=lambda value: SEVERITY_RANK[value], default="none"
        ),
    )

    outcome = RescanOutcome(
        version.id,
        "flagged" if result.findings else "clean",
        max_severity=result.max_severity,
        new_findings=len(new_rows),
    )
    new_critical = sorted(
        {row.rule_id for row in new_rows if row.severity == "critical" and not row.suppressed}
    )
    current_critical = sorted(
        {row.rule_id for row in rows if row.severity == "critical" and not row.suppressed}
    )
    # Enforced: every current critical acts. Report-only: only new ones are
    # reported (an already reported critical is not reported again).
    acting = current_critical if enforce else new_critical
    if acting and version.quarantined_at is None and not await _version_blocked(db, version):
        outcome.new_critical_rules = new_critical
        ordered = new_critical + [rule for rule in acting if rule not in new_critical]
        reason = (
            f"Automated instruction scan (ruleset {result.ruleset_version}) found "
            f"{'new ' if new_critical else ''}critical finding(s): {', '.join(ordered[:8])}"
        )
        if enforce:
            await _quarantine(db, version, reason=reason, now=now)
            outcome.action = "quarantined"
        else:
            outcome.action = "reported"
        # One open system report per version (deduplicated inside).
        await _open_system_report(db, version, reason=reason, quarantined=enforce, now=now)
    await db.commit()
    return outcome


async def _quarantine(
    db: AsyncSession, version: SkillPackageVersion, *, reason: str, now: datetime
) -> None:
    """Stop new delivery and tell existing installs. Never blocks, never
    revokes a grant, never uninstalls (D3)."""
    # Imported here: routes.marketplace imports this module for publish, and
    # the moderation routes import routes.marketplace.
    from sessionfs.server.db.models import SkillRevocationEvent
    from sessionfs.server.routes.marketplace import bump_grant_generations_for_versions
    from sessionfs.server.routes.marketplace_moderation import (
        _acquire_revocation_writer_lock,
        _next_changed_at,
        _scopes,
    )

    version.quarantined_at = now
    version.quarantine_reason = reason
    # Deliverability changed: every grant list referencing the version gets a
    # new generation (cursors issued before it restart).
    await bump_grant_generations_for_versions(db, {version.id})
    listings = list(
        (
            await db.execute(
                select(SkillMarketplaceListing).where(SkillMarketplaceListing.version_id == version.id)
            )
        )
        .scalars()
        .all()
    )
    if not listings:
        return
    await _acquire_revocation_writer_lock(db)
    changed_at = await _next_changed_at(db)
    for visibility, org_id, owner_user_id in _scopes(listings):
        db.add(
            SkillRevocationEvent(
                kind="version",
                package_id=version.package_id,
                version_id=version.id,
                content_hash=version.content_hash,
                state="quarantined",
                # Platform layer: applies to every copy of this version.
                block_scope="global",
                scope_org_id=None,
                reason=reason,
                actor_user_id=None,
                actor_role=SYSTEM_ACTOR_ROLE,
                org_id=org_id,
                owner_user_id=owner_user_id,
                visibility=visibility,
                changed_at=changed_at,
            )
        )
        changed_at = changed_at + timedelta(microseconds=1)
    logger.warning(
        "skill_quarantined version=%s package=%s reason=%s", version.id, version.package_id, reason
    )


async def _open_system_report(
    db: AsyncSession,
    version: SkillPackageVersion,
    *,
    reason: str,
    quarantined: bool,
    now: datetime,
) -> None:
    """One open system-authored report per version: the moderator's queue item."""
    existing = (
        await db.execute(
            select(SkillPackageReport.id).where(
                SkillPackageReport.package_id == version.package_id,
                SkillPackageReport.version_id == version.id,
                SkillPackageReport.reporter_user_id.is_(None),
                SkillPackageReport.status == "open",
            )
        )
    ).first()
    if existing is not None:
        return
    action = (
        "The version is QUARANTINED (no new installs; existing installs warned). "
        "Block it to remove it from devices, or restore it to record a false positive."
        if quarantined
        else "Report-only mode: the version was not quarantined."
    )
    # Same scope rule as a user report (routes/marketplace_moderation.py
    # _report_scope): one org's listings only -> that org's owners/admins
    # moderate it; any public or personal listing -> platform admins.
    listings = list(
        (
            await db.execute(
                select(SkillMarketplaceListing).where(SkillMarketplaceListing.version_id == version.id)
            )
        )
        .scalars()
        .all()
    )
    orgs = {listing.org_id for listing in listings}
    if listings and all(listing.visibility == "org" for listing in listings) and len(orgs) == 1:
        scope_kind, scope_org_id = "org", orgs.pop()
    else:
        scope_kind, scope_org_id = "platform", None
    db.add(
        SkillPackageReport(
            id=f"spr_{secrets.token_hex(12)}",
            package_id=version.package_id,
            version_id=version.id,
            scope_kind=scope_kind,
            scope_org_id=scope_org_id,
            reporter_user_id=None,
            reason="malicious",
            details=f"[system] {reason}. {action}"[:_MAX_REPORT_DETAILS],
            status="open",
            created_at=now,
        )
    )


FindingKey = tuple[str, str, str]  # (rule_id, path, line_sha)


async def open_critical_findings(db: AsyncSession, version: SkillPackageVersion) -> list[dict]:
    """Distinct unsuppressed critical findings of the version's latest scan:
    what a moderator lifting its quarantine must explicitly clear."""
    scan = await latest_scan(db, version.id)
    if scan is None:
        return []
    seen: dict[FindingKey, dict] = {}
    for row in await scan_findings(db, scan.id):
        if row.severity != "critical" or row.suppressed:
            continue
        key = _finding_key(row.rule_id, row.file_path, row.line_sha)
        seen.setdefault(
            key,
            {"rule_id": row.rule_id, "path": row.file_path, "line": row.line, "line_sha": row.line_sha},
        )
    return [seen[key] for key in sorted(seen)]


async def on_quarantine_restored(
    db: AsyncSession,
    version: SkillPackageVersion,
    *,
    reason: str,
    actor_user_id: str,
    now: datetime,
    finding_keys: set[FindingKey],
) -> None:
    """A moderator restored a quarantined version: clear the flag, record the
    false positive for EXACTLY the findings the moderator cleared (suppressed
    on those lines so a later rescan does not re-quarantine them), and close
    the system report. The caller validates ``finding_keys`` against
    open_critical_findings(), holds the version row lock and commits."""
    version.quarantined_at = None
    version.quarantine_reason = None
    scan = await latest_scan(db, version.id)
    if scan is not None:
        for rule_id, path, line_sha in sorted(finding_keys):
            await db.execute(
                update(SkillScanFinding)
                .where(
                    SkillScanFinding.scan_id == scan.id,
                    SkillScanFinding.rule_id == rule_id,
                    SkillScanFinding.file_path == path,
                    SkillScanFinding.line_sha == line_sha,
                    SkillScanFinding.severity == "critical",
                    SkillScanFinding.suppressed.is_(False),
                )
                .values(
                    suppressed=True,
                    suppression_justification=f"False positive (moderator restore): {reason}"[:1000],
                )
            )
        # The card summary reflects the cleared findings in the same
        # transaction (suppressed count, worst OPEN severity).
        rows = list(
            (
                await db.execute(
                    select(SkillScanFinding)
                    .where(SkillScanFinding.scan_id == scan.id)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        version.scan_summary = _summary_from_rows(scan, rows, version.scan_summary)
    await db.execute(
        update(SkillPackageReport)
        .where(
            SkillPackageReport.version_id == version.id,
            SkillPackageReport.reporter_user_id.is_(None),
            SkillPackageReport.status == "open",
        )
        .values(
            status="dismissed",
            resolved_at=now,
            resolved_by_user_id=actor_user_id,
            resolution_note=f"restored (false positive): {reason}"[:1000],
        )
    )


def _due_versions_query(
    *, cursor: str | None, force: bool, limit: int, now: datetime, org_id: str | None = None
):
    blocked_listing = select(SkillMarketplaceListing.version_id).where(
        SkillMarketplaceListing.verification_state == "blocked"
    )
    statement = select(SkillPackageVersion.id).where(
        SkillPackageVersion.dlp_status != "blocked",
        SkillPackageVersion.id.not_in(blocked_listing),
    )
    if not force:
        statement = statement.where(SkillPackageVersion.quarantined_at.is_(None))
        fresh = select(SkillVersionScan.version_id).where(
            SkillVersionScan.ruleset_version == RULESET_VERSION,
            SkillVersionScan.scanned_at > now - RESCAN_INTERVAL,
        )
        statement = statement.where(SkillPackageVersion.id.not_in(fresh))
    else:
        # A forced rescan also reaches quarantined versions, but only one
        # whose latest scan predates the current ruleset: it cannot be
        # restored until a scan with the current ruleset has run, and a
        # quarantine already re-scanned with the current ruleset is awaiting
        # a human decision, not another scan.
        current = select(SkillVersionScan.version_id).where(
            SkillVersionScan.ruleset_version == RULESET_VERSION
        )
        statement = statement.where(
            or_(
                SkillPackageVersion.quarantined_at.is_(None),
                SkillPackageVersion.id.not_in(current),
            )
        )
    if org_id is not None:
        # An org's on-demand rescan: only versions of packages the org OWNS.
        owned = select(SkillPackage.id).where(
            SkillPackage.owner_type == "org", SkillPackage.org_id == org_id
        )
        statement = statement.where(SkillPackageVersion.package_id.in_(owned))
    if cursor:
        statement = statement.where(SkillPackageVersion.id > cursor)
    return statement.order_by(SkillPackageVersion.id).limit(limit)


async def rescan_batch(
    db: AsyncSession,
    store: BlobStore,
    *,
    limit: int = 50,
    cursor: str | None = None,
    force: bool = False,
    enforce: bool = True,
    time_budget_seconds: float = 20.0,
    org_id: str | None = None,
) -> dict:
    """Rescan up to ``limit`` live versions that are due (``org_id``: only
    versions of packages that org owns) (no scan under the
    current ruleset, or one older than RESCAN_INTERVAL; ``force`` ignores
    freshness). Stops at the time budget. Re-running is idempotent: a scanned
    version is no longer due, and the cursor only resumes a forced pass.

    A version that could not be scanned (blob missing or unreadable, row
    gone) is listed in ``skipped`` with the reason. The cursor moves past it
    (a permanently missing blob must not pin every later page), but ``done``
    stays False for a batch that skipped anything: a pass is only complete
    when every version it covered was actually scanned."""
    started = time.monotonic()
    await ensure_ruleset(db)
    await db.commit()
    ids = list(
        (
            await db.execute(
                _due_versions_query(cursor=cursor, force=force, limit=limit, now=_now(), org_id=org_id)
            )
        )
        .scalars()
        .all()
    )
    outcomes: list[RescanOutcome] = []
    missing = 0
    errors = 0
    skipped: list[dict[str, str]] = []
    last_id: str | None = cursor
    budget_exhausted = False
    for version_id in ids:
        if time.monotonic() - started > time_budget_seconds:
            budget_exhausted = True
            break
        last_id = version_id
        version = await db.get(SkillPackageVersion, version_id)
        if version is None:
            skipped.append({"version_id": version_id, "reason": "version_gone"})
            continue
        blob_key = version.blob_key
        payload = await store.get(blob_key)
        if payload is None:
            missing += 1
            logger.warning("skill_rescan_missing_blob version=%s", version_id)
            skipped.append({"version_id": version_id, "reason": "missing_blob"})
            continue
        try:
            result = await asyncio.to_thread(scan_canonical_archive, payload)
        except HostedSkillPackageError as exc:
            errors += 1
            logger.warning("skill_rescan_unreadable version=%s reason=%s", version_id, exc.reason)
            skipped.append({"version_id": version_id, "reason": "unreadable"})
            continue
        outcomes.append(await apply_scan_result(db, version_id, result, enforce=enforce))
    reached_end = not budget_exhausted and len(ids) < limit
    done = reached_end and not skipped
    return {
        "ruleset": RULESET_VERSION,
        "scanned": len(outcomes),
        "flagged": [o.version_id for o in outcomes if o.action == "flagged"],
        "quarantined": [o.version_id for o in outcomes if o.action == "quarantined"],
        "reported": [o.version_id for o in outcomes if o.action == "reported"],
        "missing_blobs": missing,
        "errors": errors,
        "skipped": skipped,
        "next_cursor": None if done else last_id,
        "done": done,
        "enforce": enforce,
    }

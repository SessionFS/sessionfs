"""Org skills policy: storage and server-side enforcement (marketplace v2 §8).

Writing a policy needs the Enterprise ``skills_policy`` feature (D6). Once
written, the org's latest revision stays enforced if the org later drops
below Enterprise: a downgrade must never silently lift restrictions an org
relied on. While downgraded, owners and admins can still read the policy
and may only relax it or disable it (``skills.policy.tightened_fields``).
Enforcement points (all server-side):

- install request creation and approval (the project's org);
- org assignment creation and /advance (the assigning org);
- instruction loads through MCP (the project's org);
- the device reconcile listings: ``GET /users/me/skills/assignments`` drops
  assignments the assigning org's CURRENT policy refuses (listing them under
  ``policy_refused``), and approved install requests carry a ``policy``
  verdict the client honours.

A stored policy that no longer validates (it never should: every revision
is validated on write) fails closed: every version is refused.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    Organization,
    OrgSkillPolicy,
    SkillPackage,
    SkillPackageVersion,
    SkillPublisher,
)
from sessionfs.server.services import publishers as publishers_service
from sessionfs.server.services.policy_compiler.base import CatalogSource
from sessionfs.server.services import skill_disclosure
from sessionfs.server.services.entitlements import resolve_entitlement
from sessionfs.server.tiers import Tier, get_features_for_tier
from sessionfs.skills import disclosure
from sessionfs.skills.policy import (
    PolicyDocument,
    PolicyError,
    PolicySubject,
    SkillsPolicy,
    Verdict,
    Violation,
    evaluate,
    parse_policy,
    refusal_message,
)

logger = logging.getLogger(__name__)

FEATURE = "skills_policy"


@dataclass
class EffectivePolicy:
    org: Organization
    row: OrgSkillPolicy
    policy: SkillsPolicy | None  # None = stored document invalid (fail closed)


async def catalog_sources(db: AsyncSession, org: Organization) -> list[CatalogSource]:
    """SessionFS-served native catalogs (phase 3a) to register and allow in
    each tool. None exist until native feeds ship; the compiler then omits
    marketplace registration and an allowlist holds only ``skills-dir``."""
    return []


async def org_has_policy_feature(db: AsyncSession, org: Organization) -> bool:
    entitlement = await resolve_entitlement("org", org.id, db)
    raw = entitlement.tier if entitlement else org.tier
    try:
        tier = Tier(raw)
    except ValueError:
        tier = Tier.ENTERPRISE if raw == "admin" else Tier.FREE
    return FEATURE in get_features_for_tier(tier)


async def latest_policy_row(db: AsyncSession, org_id: str) -> OrgSkillPolicy | None:
    return (
        await db.execute(
            select(OrgSkillPolicy)
            .where(OrgSkillPolicy.org_id == org_id)
            .order_by(OrgSkillPolicy.revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def document_of(row: OrgSkillPolicy) -> PolicyDocument | None:
    try:
        return parse_policy(json.loads(row.policy_json))
    except (PolicyError, ValueError, TypeError):
        logger.error("Stored skills policy %s (org %s) does not validate", row.id, row.org_id)
        return None


async def effective_policy(db: AsyncSession, org: Organization | None) -> EffectivePolicy | None:
    """The org's latest policy revision, else None. Enforced whether or not
    the org is still entitled (a downgrade keeps the last revision)."""
    if org is None:
        return None
    row = await latest_policy_row(db, org.id)
    if row is None:
        return None
    document = document_of(row)
    return EffectivePolicy(org=org, row=row, policy=document.skills if document else None)


async def effective_policies(db: AsyncSession, org_ids: Iterable[str]) -> dict[str, EffectivePolicy]:
    result: dict[str, EffectivePolicy] = {}
    ids = sorted(set(org_ids))
    if not ids:
        return result
    orgs = (await db.execute(select(Organization).where(Organization.id.in_(ids)))).scalars().all()
    for org in orgs:
        effective = await effective_policy(db, org)
        if effective is not None:
            result[org.id] = effective
    return result


async def verified_active_publishers(db: AsyncSession, publisher_ids: Iterable[str | None]) -> set[str]:
    """Publisher ids that are active AND currently verified (suspended or
    tombstoned publishers never satisfy a verified-only allowlist)."""
    ids = sorted({pid for pid in publisher_ids if pid})
    if not ids:
        return set()
    verified = await publishers_service.verified_publisher_ids(db, ids)
    active = set(
        (
            await db.execute(
                select(SkillPublisher.id).where(
                    SkillPublisher.id.in_(ids), SkillPublisher.status == "active"
                )
            )
        ).scalars().all()
    )
    return verified & active


def _raw_manifest(version: SkillPackageVersion) -> dict[str, Any] | None:
    """The stored manifest even when derived under an older scanner ruleset
    (capability classes do not depend on the ruleset); None when unknown."""
    try:
        manifest = json.loads(version.capability_manifest or "{}")
    except (TypeError, ValueError):
        return None
    return manifest if disclosure.manifest_known(manifest) else None


def _scan_severity(version: SkillPackageVersion, manifest: dict[str, Any] | None) -> str:
    try:
        summary = json.loads(version.scan_summary or "{}")
    except (TypeError, ValueError):
        summary = {}
    severity = summary.get("max_severity") if isinstance(summary, dict) else None
    if not severity and manifest:
        severity = (manifest.get("scan") or {}).get("max_severity")
    return str(severity or "none")


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def verified_source(version: SkillPackageVersion) -> tuple[int | None, str | None]:
    """(repository id, commit) the server VERIFIED for this version, else
    (None, None).

    Only trusted publishing proves where a version came from: its
    signature_metadata is written by the server from the GitHub OIDC token
    it verified (``published_from_repo_id`` - the numeric repository id,
    stable across renames and never reused - and the run's ``sha``), and a
    publisher cannot set it. The package's ``source_locator`` and the
    version's ``source_commit`` are declared by whoever uploads and are
    never used here. Metadata without a repository id matches nothing."""
    try:
        metadata = json.loads(version.signature_metadata or "{}")
    except (TypeError, ValueError):
        return None, None
    if not isinstance(metadata, dict):
        return None, None
    if metadata.get("kind") != "github_oidc" or metadata.get("verification") != "oidc_token":
        return None, None
    repo_id = metadata.get("published_from_repo_id")
    if not isinstance(repo_id, int) or isinstance(repo_id, bool) or repo_id < 1:
        return None, None
    sha = metadata.get("publishing_run_sha")
    commit = sha.lower() if isinstance(sha, str) and _FULL_SHA_RE.fullmatch(sha.lower()) else None
    return repo_id, commit


MAX_SOURCE_LOOKUPS = 50


async def resolve_source_ids(db: AsyncSession, user_id: str, document: PolicyDocument) -> PolicyDocument:
    """Pin every allowlist ``source`` entry to its numeric GitHub repository
    id, resolved from GitHub NOW (the caller's own GitHub App installations
    first, then the public API). Entries that already carry an id keep it
    (an admin may pin one explicitly, as for trusted publishers). Stored
    trusted-publisher rows are not used: their display name is whatever
    the creator typed when they passed explicit ids, so it proves nothing
    about which repository a name means. An entry that cannot be resolved
    refuses the whole write (422 source_unresolved): a name-only entry is
    never stored."""
    from sessionfs.server import github_app
    from sessionfs.skills.policy import github_repository

    entries = document.skills.allowlist
    pending = [e for e in entries if e.source is not None and e.repository_id is None]
    if not pending:
        return document
    names: dict[tuple[str, str], int | None] = {}
    for entry in pending:
        parsed = github_repository(entry.source or "")
        if parsed is None:
            raise _unresolved(entry.source or "", "only github.com/<owner>/<repo> sources can be verified")
        names.setdefault(parsed, None)
    if len(names) > MAX_SOURCE_LOOKUPS:
        raise HTTPException(
            422,
            detail={
                "code": "source_unresolved",
                "message": f"At most {MAX_SOURCE_LOOKUPS} new repositories per write; pass repository_id "
                "for the others.",
            },
        )
    for owner, repo in list(names):
        try:
            resolved = await github_app.resolve_repository(db, user_id, owner, repo)
        except Exception:
            resolved = None
        if resolved is None or not isinstance(resolved.get("id"), int):
            raise _unresolved(f"github.com/{owner}/{repo}", "the repository could not be found on GitHub")
        names[(owner, repo)] = int(resolved["id"])
    for entry in pending:
        parsed = github_repository(entry.source or "")
        if parsed is not None:
            entry.repository_id = names[parsed]
    return document


def _unresolved(source: str, why: str) -> HTTPException:
    return HTTPException(
        422,
        detail={
            "code": "source_unresolved",
            "message": (
                f"Allowlist source {source!r} cannot be pinned to a GitHub repository id: {why}. "
                "Install the SessionFS GitHub App on it, or pass repository_id."
            ),
            "source": source,
        },
    )


async def subject_for(
    db: AsyncSession,
    org_id: str,
    version: SkillPackageVersion,
    package: SkillPackage | None = None,
    *,
    manifest: dict[str, Any] | None = None,
    verified_publishers: set[str] | None = None,
) -> PolicySubject:
    if package is None:
        package = await db.get(SkillPackage, version.package_id)
    publisher_id = package.publisher_id if package is not None else None
    if verified_publishers is None:
        verified_publishers = await verified_active_publishers(db, [publisher_id])
    if manifest is None:
        manifest = _raw_manifest(version)
    provenance = skill_disclosure.version_provenance(version)
    repository_id, commit = verified_source(version)
    return PolicySubject(
        package_org_id=package.org_id if package is not None else None,
        org_id=org_id,
        publisher_id=publisher_id,
        publisher_verified=bool(publisher_id and publisher_id in verified_publishers),
        verified_repository_id=repository_id,
        verified_commit=commit,
        manifest=manifest,
        attested=bool(provenance.get("attested")),
        dlp_status=version.dlp_status,
        scan_max_severity=_scan_severity(version, manifest),
    )


def verdict_of(effective: EffectivePolicy, subject: PolicySubject) -> Verdict:
    if effective.policy is None:
        return Verdict([Violation("policy_invalid", "policy", "The organization's policy could not be read.")])
    return evaluate(effective.policy, subject)


def violation_error(effective: EffectivePolicy, verdict: Verdict) -> HTTPException:
    return HTTPException(
        403,
        detail={
            "code": "org_policy_violation",
            "message": refusal_message(effective.org.name, verdict),
            "org_id": effective.org.id,
            "policy_revision": effective.row.revision,
            "violations": [v.as_dict() for v in verdict.violations],
            "ask": f"an owner or admin of {effective.org.name}",
        },
    )


async def enforce(
    db: AsyncSession,
    org: Organization | None,
    version: SkillPackageVersion,
    *,
    manifest: dict[str, Any] | None = None,
) -> None:
    """Raise 403 org_policy_violation when ``org``'s policy refuses ``version``."""
    effective = await effective_policy(db, org)
    if effective is None:
        return
    subject = await subject_for(db, effective.org.id, version, manifest=manifest)
    verdict = verdict_of(effective, subject)
    if not verdict.allowed:
        raise violation_error(effective, verdict)


async def enforce_for_org(
    db: AsyncSession,
    org_id: str | None,
    version: SkillPackageVersion,
    *,
    store: Any = None,
    manifest: dict[str, Any] | None = None,
) -> None:
    """`enforce` for an org id. Loads the CURRENT manifest from the blob
    store (without persisting it) only when a policy applies."""
    if org_id is None:
        return
    org = await db.get(Organization, org_id)
    effective = await effective_policy(db, org)
    if effective is None:
        return
    if manifest is None and store is not None:
        manifest = await skill_disclosure.version_manifest(db, store, version, persist=False)
    subject = await subject_for(db, effective.org.id, version, manifest=manifest)
    verdict = verdict_of(effective, subject)
    if not verdict.allowed:
        raise violation_error(effective, verdict)


async def verdicts(
    db: AsyncSession, pairs: list[tuple[str, str]]
) -> dict[tuple[str, str], tuple[EffectivePolicy, Verdict]]:
    """Batch verdicts for (org_id, version_id) pairs; pairs whose org has no
    effective policy are absent (allowed)."""
    policies = await effective_policies(db, [org_id for org_id, _ in pairs])
    wanted = sorted({version_id for org_id, version_id in pairs if org_id in policies})
    if not wanted:
        return {}
    rows = (
        await db.execute(
            select(SkillPackageVersion, SkillPackage)
            .join(SkillPackage, SkillPackage.id == SkillPackageVersion.package_id)
            .where(SkillPackageVersion.id.in_(wanted))
        )
    ).all()
    by_id = {version.id: (version, package) for version, package in rows}
    verified = await verified_active_publishers(db, [package.publisher_id for _, package in rows])
    result: dict[tuple[str, str], tuple[EffectivePolicy, Verdict]] = {}
    for org_id, version_id in pairs:
        effective = policies.get(org_id)
        if effective is None or version_id not in by_id:
            continue
        version, package = by_id[version_id]
        subject = await subject_for(
            db, org_id, version, package, verified_publishers=verified
        )
        result[(org_id, version_id)] = (effective, verdict_of(effective, subject))
    return result

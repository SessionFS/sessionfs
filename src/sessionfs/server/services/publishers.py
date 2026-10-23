"""Marketplace publishers: namespaces, lookalike gate, verification, trusted
publishing, and public-listing moderation (Marketplace v2 §4).

Invariants this module enforces (Sentinel):

- A claimed namespace is globally unique by its confusable skeleton
  (``uq_skill_publishers_namespace_normalized``) and never released.
- New namespaces and new public package names that imitate a reserved name
  or a verified publisher are refused (``409 namespace_confusable``).
- Verification is about WHO publishes and lives on the publisher, never on
  a listing. DNS proofs are re-checked by a sweeper and lapse after 7 days
  of failures; GitHub proofs are revoked as soon as the installation is gone.
- Trusted publishing binds to GitHub's NUMERIC repository and owner ids and
  the exact workflow file, or to a GitLab instance on the operator allowlist
  plus the NUMERIC project id, a protected ref and a non-merge-request
  pipeline; publish tokens are hashed at rest, expire after 15 minutes, are
  single use, and are minted at most once per OIDC (``iss``, ``jti``).
- Public listings from anyone but the platform need moderation for a
  publisher's first public version and for any update that widens access.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
import hashlib
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from fastapi import HTTPException
from sqlalchemy import exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server import github_app
from sessionfs.server.db.models import (
    GitHubInstallation,
    SkillGithubInstallationState,
    OrgDomainVerification,
    SkillMarketplaceListing,
    SkillPackage,
    SkillPackageVersion,
    SkillPublisher,
    SkillPublisherVerification,
    SkillPublishToken,
    SkillReservedName,
    SkillTrustedPublisher,
)
from sessionfs.server.services import domain_verification
from sessionfs.server.services.oidc_token import GITHUB_ACTIONS_ISSUER
from sessionfs.skills.confusables import (
    forbidden_code_points,
    NAMESPACE_CLAIM_RE,
    RESERVED_SKELETONS,
    ConfusableVerdict,
    ProtectedName,
    check_confusable,
    has_mixed_scripts,
    skeleton,
)

logger = logging.getLogger("sessionfs.publishers")

SYSTEM_PUBLISHER_ID = "pub_sessionfs"
SYSTEM_NAMESPACE = "sessionfs"
DNS_LAPSE_AFTER = timedelta(days=7)
PUBLISH_TOKEN_TTL = timedelta(minutes=15)
PUBLISH_TOKEN_PREFIX = "sfs_tp_"
VERIFICATION_METHODS = ("dns_txt", "github_org", "github_user")
_WORKFLOW_PATH_RE = re.compile(r"^\.github/workflows/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.ya?ml$")
_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_REF_PATTERN_RE = re.compile(r"^refs/(heads|tags)/[A-Za-z0-9._/*+-]{1,200}$")
_ENVIRONMENT_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,255}$")
_REUSABLE_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}/"
    r"\.github/workflows/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.ya?ml$"
)
# Events that can legitimately publish a release. Pull-request-derived events
# (pull_request, pull_request_target, ...) never match; workflow_run only when
# the trusted publisher opts in (it runs with the default branch's privileges
# on behalf of another workflow, possibly one a fork triggered).
PUBLISH_EVENTS = frozenset({"push", "release", "workflow_dispatch"})
# GitLab pipeline sources that can publish (an allowlist, the parallel of
# PUBLISH_EVENTS): a push (branch / tag), a pipeline run from the UI, or the
# pipelines API. Everything else is refused - merge_request_event and
# external_pull_request_event (merge / pull request code, possibly from a
# fork), parent_pipeline and pipeline (a child or downstream pipeline, whose
# parent may itself be a merge request pipeline), trigger (trigger tokens),
# schedule, chat, webide, and any source GitLab adds later.
GITLAB_PUBLISH_SOURCES = frozenset({"push", "web", "api"})
# GitLab environment names: letters, digits, spaces and - _ / . (no $ { }).
_GITLAB_ENVIRONMENT_RE = re.compile(r"^[A-Za-z0-9 ._/-]{1,255}$")
_GITLAB_PROJECT_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,254}(?:/[A-Za-z0-9_][A-Za-z0-9._-]{0,254}){1,20}$")
# A reserved name unlocks only on FRESH domain evidence: a publisher DNS
# verification that succeeded within this window, or a live DNS re-check of
# the org's SSO domain proof at claim time.
DOMAIN_EVIDENCE_FRESHNESS = timedelta(hours=48)
_CLAIM_KEYS = (
    "sub",
    "repository",
    "repository_id",
    "repository_owner",
    "repository_owner_id",
    "workflow_ref",
    "job_workflow_ref",
    "ref",
    "ref_type",
    "sha",
    "environment",
    "run_id",
    "run_attempt",
    "event_name",
    "runner_environment",
    "actor_id",
)
# The GitLab claims kept on a publish token (no e-mail address, no login).
_GITLAB_CLAIM_KEYS = (
    "sub",
    "namespace_id",
    "namespace_path",
    "project_id",
    "project_path",
    "pipeline_id",
    "pipeline_source",
    "job_id",
    "ref",
    "ref_type",
    "ref_path",
    "ref_protected",
    "sha",
    "environment",
    "environment_protected",
    "deployment_tier",
    "user_id",
    "runner_environment",
    "ci_config_ref_uri",
    "ci_config_sha",
)
TRUSTED_PROVENANCE_KINDS = frozenset({"github_oidc", "gitlab_oidc"})


def utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; every stored value is UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _conflict(code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(409, detail={"code": code, "message": message, **extra})


def _forbidden(code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(403, detail={"code": code, "message": message, **extra})


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def owner_key(owner_type: str, owner_id: str | None) -> str:
    if owner_type == "system":
        return "system"
    return f"{owner_type}:{owner_id}"


def publisher_owner_key(publisher: SkillPublisher) -> str:
    if publisher.owner_type == "org":
        return owner_key("org", publisher.org_id)
    if publisher.owner_type == "user":
        return owner_key("user", publisher.user_id)
    return "system"


async def ensure_system_publisher(db: AsyncSession) -> SkillPublisher:
    """The platform publisher (seeded by migration 071; created lazily on a
    database built from the models)."""
    publisher = await db.get(SkillPublisher, SYSTEM_PUBLISHER_ID)
    if publisher is not None:
        return publisher
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    now = now_utc()
    await db.execute(
        insert(SkillPublisher)
        .values(
            id=SYSTEM_PUBLISHER_ID,
            owner_type="system",
            namespace=SYSTEM_NAMESPACE,
            namespace_normalized=skeleton(SYSTEM_NAMESPACE),
            display_name="SessionFS",
            status="active",
            public_publishing=True,
            suspended_listing_ids="[]",
            review_flags="[]",
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing()
    )
    publisher = await db.get(SkillPublisher, SYSTEM_PUBLISHER_ID)
    if publisher is None:  # pragma: no cover - a namespace collision, never seeded
        raise RuntimeError("the platform publisher namespace is held by another row")
    return publisher


# ---------------------------------------------------------------------------
# Verification state
# ---------------------------------------------------------------------------


async def verified_publisher_ids(db: AsyncSession, publisher_ids: Iterable[str]) -> set[str]:
    ids = sorted({pid for pid in publisher_ids if pid})
    if not ids:
        return set()
    rows = (
        await db.execute(
            select(SkillPublisherVerification.publisher_id).where(
                SkillPublisherVerification.publisher_id.in_(ids),
                SkillPublisherVerification.status == "verified",
            )
        )
    ).scalars().all()
    verified = set(rows)
    verified.update(pid for pid in ids if pid == SYSTEM_PUBLISHER_ID)
    return verified


async def is_verified(db: AsyncSession, publisher: SkillPublisher) -> bool:
    if publisher.status != "active":
        return False
    if publisher.owner_type == "system":
        return True
    return publisher.id in await verified_publisher_ids(db, [publisher.id])


def badge_text(verification: SkillPublisherVerification) -> str:
    if verification.method == "dns_txt":
        return f"verified: {verification.subject_display}"
    kind = "org" if verification.method == "github_org" else "user"
    return f"verified: {verification.subject_display} ({kind} #{verification.subject})"


async def publisher_blocks(db: AsyncSession, publisher_ids: Iterable[str]) -> dict[str, dict]:
    """Public publisher summaries for listing responses (badges never come
    from a display name: only from a live verification row)."""
    ids = sorted({pid for pid in publisher_ids if pid})
    if not ids:
        return {}
    publishers = (
        await db.execute(select(SkillPublisher).where(SkillPublisher.id.in_(ids)))
    ).scalars().all()
    verifications = (
        await db.execute(
            select(SkillPublisherVerification).where(
                SkillPublisherVerification.publisher_id.in_(ids),
                SkillPublisherVerification.status == "verified",
            )
        )
    ).scalars().all()
    by_publisher: dict[str, list[SkillPublisherVerification]] = {}
    for row in verifications:
        by_publisher.setdefault(row.publisher_id, []).append(row)
    blocks: dict[str, dict] = {}
    for publisher in publishers:
        rows = by_publisher.get(publisher.id, [])
        badges = sorted(badge_text(row) for row in rows)
        if publisher.owner_type == "system":
            badges = ["verified: SessionFS platform"]
        active = publisher.status == "active"
        blocks[publisher.id] = {
            "id": publisher.id,
            "namespace": publisher.namespace,
            "display_name": publisher.display_name,
            "owner_type": publisher.owner_type,
            "status": publisher.status,
            "verified": active and bool(badges),
            "badges": badges if active else [],
        }
    return blocks


# ---------------------------------------------------------------------------
# Lookalike gate
# ---------------------------------------------------------------------------


async def owner_proves_domain(db: AsyncSession, key: str, domain: str) -> bool:
    """Fresh evidence that this owner controls ``domain``.

    Accepted: (a) a DNS publisher verification of one of the owner's
    publishers that SUCCEEDED within DOMAIN_EVIDENCE_FRESHNESS, or (b) for an
    org, its SSO domain proof re-resolved live NOW (an SSO row is never
    re-checked by the SSO surface, so its stored status alone is stale)."""
    kind, _, ident = key.partition(":")
    if kind not in ("org", "user") or not ident:
        return False
    owner_filter = SkillPublisher.org_id == ident if kind == "org" else SkillPublisher.user_id == ident
    cutoff = now_utc() - DOMAIN_EVIDENCE_FRESHNESS
    rows = (
        await db.execute(
            select(SkillPublisherVerification.last_success_at)
            .join(SkillPublisher, SkillPublisher.id == SkillPublisherVerification.publisher_id)
            .where(
                owner_filter,
                SkillPublisher.owner_type == kind,
                SkillPublisherVerification.method == "dns_txt",
                SkillPublisherVerification.subject == domain,
                SkillPublisherVerification.status == "verified",
            )
        )
    ).scalars().all()
    if any((utc(value) or cutoff) > cutoff for value in rows if value is not None):
        return True
    if kind != "org":
        return False
    sso = (
        await db.execute(
            select(OrgDomainVerification).where(
                OrgDomainVerification.org_id == ident,
                OrgDomainVerification.domain == domain,
                OrgDomainVerification.status == "verified",
            )
        )
    ).scalar_one_or_none()
    if sso is None or not sso.verification_token:
        return False
    records = await domain_verification.resolve_txt(domain)
    return domain_verification.records_contain_token(records, sso.verification_token)


async def _reserved_entries(db: AsyncSession) -> list[tuple[str, str, str | None, str]]:
    """(name, skeleton, required_domain, kind) for vendored + table entries."""
    entries = [
        (name, skel, domain, "reserved") for skel, (name, domain) in RESERVED_SKELETONS.items()
    ]
    for row in (await db.execute(select(SkillReservedName))).scalars().all():
        kind = "tombstone" if row.reason == "tombstone" else "reserved"
        entries.append((row.name, row.normalized, row.required_subject, kind))
    return entries


async def _strong_publishers(db: AsyncSession) -> list[SkillPublisher]:
    """Publishers whose names get the distance/affix protection: the platform,
    every publisher that currently holds a verification, and every retired
    (tombstoned) namespace that WAS verified before it was retired (retiring
    must not drop that protection). An unverified namespace never had strong
    protection, so retiring it does not create it: it keeps only the exact
    skeleton reservation (the namespace row itself is never released)."""
    verified = exists(
        select(SkillPublisherVerification.id).where(
            SkillPublisherVerification.publisher_id == SkillPublisher.id,
            SkillPublisherVerification.status == "verified",
        )
    )
    was_verified = exists(
        select(SkillPublisherVerification.id).where(
            SkillPublisherVerification.publisher_id == SkillPublisher.id,
            SkillPublisherVerification.verified_at.is_not(None),
        )
    )
    return list(
        (
            await db.execute(
                select(SkillPublisher).where(
                    or_(
                        (SkillPublisher.status == "tombstoned") & was_verified,
                        (SkillPublisher.status != "tombstoned")
                        & or_(SkillPublisher.owner_type == "system", verified),
                    )
                )
            )
        ).scalars().all()
    )


async def find_confusable(
    db: AsyncSession,
    candidate: str,
    *,
    claimant_key: str,
    unlocked_domains: set[str] | None = None,
) -> ConfusableVerdict | None:
    """Blocking verdict (or a review flag) for ``candidate`` against reserved
    names, every claimed namespace (exact skeleton) and verified publishers
    (exact, distance 1, affix). Reserved names unlock for an owner who has
    verified the reserved name's domain."""
    skel = skeleton(candidate)
    taken = (
        await db.execute(select(SkillPublisher).where(SkillPublisher.namespace_normalized == skel))
    ).scalar_one_or_none()
    if taken is not None and publisher_owner_key(taken) != claimant_key:
        kind = "tombstone" if taken.status == "tombstoned" else "publisher"
        return ConfusableVerdict(True, "exact", taken.namespace, kind)
    reserved = await _reserved_entries(db)
    domain_for = {name: domain for name, _skel, domain, kind in reserved if kind == "reserved" and domain}
    exempt: set[str] = set()
    strong = [
        ProtectedName(
            publisher.namespace,
            publisher.namespace_normalized,
            # A retired name protects itself from everyone, its former owner
            # included (no exemption); live names exempt their owner.
            "" if publisher.status == "tombstoned" else publisher_owner_key(publisher),
            "tombstone" if publisher.status == "tombstoned" else "publisher",
            True,
        )
        for publisher in await _strong_publishers(db)
    ]
    # Re-evaluate after each reserved name the claimant proves (fresh domain
    # evidence only). Bounded: each pass exempts one more reserved entry.
    for _ in range(len(domain_for) + 1):
        protected = [
            ProtectedName(name, entry_skel, claimant_key if name in exempt else "", kind, True)
            for name, entry_skel, _domain, kind in reserved
        ] + strong
        verdict = check_confusable(candidate, protected, owner_key=claimant_key)
        if verdict is None or not verdict.blocked or verdict.conflict_kind != "reserved":
            return verdict
        domain = domain_for.get(verdict.conflict)
        if domain is None or verdict.conflict in exempt:
            return verdict
        if not await owner_proves_domain(db, claimant_key, domain):
            return verdict
        exempt.add(verdict.conflict)
        if unlocked_domains is not None:
            unlocked_domains.add(domain)
    return verdict


def confusable_error(verdict: ConfusableVerdict, *, what: str = "namespace") -> HTTPException:
    code = "namespace_confusable" if what == "namespace" else "package_name_confusable"
    if verdict.conflict_kind == "tombstone":
        message = f"This {what} is retired and cannot be registered again."
    elif verdict.conflict_kind == "reserved":
        message = (
            f"This {what} is reserved or too similar to a reserved name. Verify the matching "
            "domain first, or ask SessionFS to review the claim."
        )
    else:
        message = f"This {what} is too similar to an existing verified publisher."
    return _conflict(code, message, conflict=verdict.conflict, rule=verdict.rule)


def validate_namespace_shape(namespace: str) -> str:
    reject_forbidden_code_points(namespace or "", field="namespace")
    value = (namespace or "").strip().lower()
    if not NAMESPACE_CLAIM_RE.fullmatch(value) or "--" in value:
        raise HTTPException(
            422,
            detail={
                "code": "invalid_namespace",
                "message": "namespace must be 3-39 lowercase letters, digits or single hyphens, "
                "starting and ending with a letter or digit",
            },
        )
    return value


def reject_forbidden_code_points(value: str, *, field: str) -> None:
    """422 for format / bidi / control / surrogate / private-use / unassigned
    code points, BEFORE any skeleton or lookalike check (the skeleton strips
    them, which is exactly what would let a bidi-reordered spoof through)."""
    bad = forbidden_code_points(value)
    if bad:
        raise HTTPException(
            422,
            detail={
                "code": f"invalid_{field}",
                "message": f"{field} contains invisible or control characters ({', '.join(bad[:5])})",
            },
        )


def validate_display_name(display_name: str | None, fallback: str) -> str:
    # On the RAW input: str.strip() would silently drop control characters
    # such as U+001C-U+001F before they could be seen.
    reject_forbidden_code_points(display_name or "", field="display_name")
    value = (display_name or "").strip() or fallback
    reject_forbidden_code_points(value, field="display_name")
    if len(value) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise HTTPException(422, detail={"code": "invalid_display_name", "message": "display_name must be printable and at most 200 characters"})
    if has_mixed_scripts(value):
        raise HTTPException(
            422,
            detail={
                "code": "display_name_mixed_script",
                "message": "display_name mixes letters from different scripts",
            },
        )
    return value


async def check_display_name(db: AsyncSession, display_name: str, *, claimant_key: str) -> None:
    """Display names never carry a badge, but they must not spell a reserved
    name or a verified publisher either (UTS #39 skeleton), in any casing."""
    for variant in {display_name, display_name.casefold()}:
        verdict = await find_confusable(db, variant, claimant_key=claimant_key)
        if verdict is not None and verdict.blocked and verdict.rule == "exact":
            raise _conflict(
                "display_name_confusable",
                "display_name imitates a reserved name or a verified publisher",
                conflict=verdict.conflict,
            )


async def namespace_in_use_by_others(db: AsyncSession, namespace: str, key: str) -> str | None:
    """A package namespace (or a lookalike) another owner already publishes
    in. Legacy per-owner namespaces are reserved to the owners using them:
    only they can claim the name (several owners -> nobody, without an admin
    override). Returns the conflicting namespace or None."""
    skel = skeleton(namespace)
    rows = (
        await db.execute(
            select(SkillPackage.namespace, SkillPackage.owner_type, SkillPackage.owner_id).distinct()
        )
    ).all()
    for existing, owner_type, owner_id in rows:
        if owner_key(owner_type, owner_id) == key:
            continue
        if _cached_skeleton(existing) == skel:
            return existing
    return None


@functools.lru_cache(maxsize=4096)
def _cached_skeleton(value: str) -> str:
    return skeleton(value)


MAX_PUBLISHERS_PER_OWNER = 5
# Retired (tombstoned) namespaces still count, so tombstoning cannot reserve
# an unbounded number of names. A platform admin can assign more.
MAX_PUBLISHER_CLAIMS_LIFETIME = 10
# Durable (cross-process) claim rate: publishers a user created in the window.
MAX_CLAIMS_PER_USER_PER_HOUR = 10


async def enforce_claim_quota(
    db: AsyncSession, *, owner_type: str, owner_id: str, claimant_user_id: str
) -> None:
    """Lock the owning user / org row (and the claimant's user row) with
    FOR NO KEY UPDATE, then count, so concurrent claims serialize and cannot
    all slip under the limits (it does not block unrelated inserts that only
    reference the row). The locks are held until the claim commits.

    The locked user rows are also the claim's final active-account fence: a
    user deactivated since authentication must not acquire a namespace."""
    from sessionfs.server.db.models import Organization, User

    user_ids = sorted({claimant_user_id, owner_id} if owner_type == "user" else {claimant_user_id})
    for user_id in user_ids:
        is_active = (
            await db.execute(
                select(User.is_active).where(User.id == user_id).with_for_update(key_share=True)
            )
        ).scalar_one_or_none()
        if is_active is not True:
            raise HTTPException(403, "User account is inactive")
    if owner_type == "org":
        await db.execute(select(Organization.id).where(Organization.id == owner_id).with_for_update(key_share=True))
    column = SkillPublisher.org_id if owner_type == "org" else SkillPublisher.user_id
    statuses = (
        await db.execute(
            select(SkillPublisher.status).where(
                SkillPublisher.owner_type == owner_type, column == owner_id
            )
        )
    ).scalars().all()
    active = sum(1 for status in statuses if status != "tombstoned")
    if active >= MAX_PUBLISHERS_PER_OWNER or len(statuses) >= MAX_PUBLISHER_CLAIMS_LIFETIME:
        raise _conflict(
            "publisher_limit",
            f"An owner can hold at most {MAX_PUBLISHERS_PER_OWNER} publishers and claim "
            f"{MAX_PUBLISHER_CLAIMS_LIFETIME} namespaces in total (retired ones count). "
            "Ask SessionFS to raise the limit.",
        )
    recent = (
        await db.execute(
            select(SkillPublisher.id).where(
                SkillPublisher.created_by_user_id == claimant_user_id,
                SkillPublisher.created_at >= now_utc() - timedelta(hours=1),
            )
        )
    ).scalars().all()
    if len(recent) >= MAX_CLAIMS_PER_USER_PER_HOUR:
        raise HTTPException(
            429, detail={"code": "rate_limited", "message": "Too many namespace claims; retry later."}
        )


async def claim_namespace(
    db: AsyncSession,
    *,
    namespace: str,
    owner_type: str,
    org_id: str | None,
    user_id: str | None,
    display_name: str | None,
    created_by_user_id: str | None,
    admin_override: bool = False,
) -> tuple[SkillPublisher, list[dict]]:
    """Create a publisher for ``namespace``. Returns (publisher, review_flags)."""
    namespace = validate_namespace_shape(namespace)
    key = owner_key(owner_type, org_id if owner_type == "org" else user_id)
    name = validate_display_name(display_name, namespace)
    flags: list[dict] = []
    unlocked: set[str] = set()
    if not admin_override:
        verdict = await find_confusable(db, namespace, claimant_key=key, unlocked_domains=unlocked)
        if verdict is not None and verdict.blocked:
            raise confusable_error(verdict)
        if verdict is not None:
            flags.append({"rule": verdict.rule, "conflict": verdict.conflict})
        in_use = await namespace_in_use_by_others(db, namespace, key)
        if in_use is not None:
            raise _conflict(
                "namespace_in_use",
                "Another owner already publishes packages in this namespace (or one "
                "indistinguishable from it). Ask SessionFS to review the claim.",
                conflict=in_use,
            )
        await check_display_name(db, name, claimant_key=key)
    publisher = SkillPublisher(
        id=f"pub_{secrets.token_hex(12)}",
        owner_type=owner_type,
        org_id=org_id if owner_type == "org" else None,
        user_id=user_id if owner_type == "user" else None,
        namespace=namespace,
        namespace_normalized=skeleton(namespace),
        display_name=name,
        status="active",
        review_flags=json.dumps(flags, sort_keys=True),
        # Bound to the exact proof(s) that unlocked a reserved brand name.
        required_domains=json.dumps(sorted(unlocked)),
        created_by_user_id=created_by_user_id,
    )
    db.add(publisher)
    try:
        await db.flush()
    except IntegrityError:
        # A concurrent claim of the same skeleton won the unique index. The
        # claim is the only write in this transaction, so roll it back whole.
        await db.rollback()
        raise _conflict(
            "namespace_confusable",
            "This namespace (or one indistinguishable from it) is already claimed.",
            conflict=namespace,
            rule="exact",
        ) from None
    return publisher, flags


# ---------------------------------------------------------------------------
# Publish-path namespace resolution
# ---------------------------------------------------------------------------


async def lock_publisher(db: AsyncSession, publisher_id: str) -> SkillPublisher | None:
    """The common serialization point: every publish under a publisher and
    every publisher / trust lifecycle mutation takes this row FOR UPDATE
    and re-reads it, so a concurrent suspension, tombstone or revocation
    that committed first is seen.

    Global lock order (marketplace publishers):
    confusable promotion (advisory, global) -> namespace (advisory) ->
    GitHub installation (advisory) -> domain
    (advisory) -> publisher row -> version row -> owner rows (user / org)
    -> package row -> content hash (advisory)."""
    return (
        await db.execute(
            select(SkillPublisher)
            .where(SkillPublisher.id == publisher_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def required_domains(publisher: SkillPublisher) -> list[str]:
    try:
        value = json.loads(publisher.required_domains or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


async def has_current_domain_proofs(db: AsyncSession, publisher: SkillPublisher) -> bool:
    """Every domain that unlocked this publisher's reserved namespace is
    CURRENTLY verified (dns_txt, status verified) on this very publisher."""
    needed = set(required_domains(publisher))
    if not needed:
        return True
    held = set(
        (
            await db.execute(
                select(SkillPublisherVerification.subject).where(
                    SkillPublisherVerification.publisher_id == publisher.id,
                    SkillPublisherVerification.method == "dns_txt",
                    SkillPublisherVerification.status == "verified",
                    SkillPublisherVerification.subject.in_(sorted(needed)),
                )
            )
        ).scalars().all()
    )
    return needed <= held


async def _publisher_by_skeleton(db: AsyncSession, namespace: str) -> SkillPublisher | None:
    return (
        await db.execute(
            select(SkillPublisher).where(SkillPublisher.namespace_normalized == skeleton(namespace))
        )
    ).scalar_one_or_none()


async def resolve_publish_publisher(
    db: AsyncSession,
    *,
    namespace: str,
    owner_type: str,
    owner_id: str,
    visibility: str,
    platform_admin: bool,
    is_new_package: bool,
    package_name: str,
    forced: SkillPublisher | None = None,
    first_public_listing: bool | None = None,
    package_id: str | None = None,
    allow_confusable_name: bool = False,
) -> SkillPublisher | None:
    """Decide which publisher (if any) a publish lands under, or refuse it.

    - A namespace whose skeleton is claimed by ANOTHER owner takes no new
      package identities (existing ones are grandfathered).
    - Publishing under your own suspended or tombstoned publisher is refused.
    - Public listings need a publisher: the platform (platform admin), or the
      caller's own verified publisher opted into public publishing (D1).
    - A package's name must not imitate reserved / verified names when it
      gets its FIRST public listing, even if its private package row or
      version already exists (``first_public_listing``; defaults to
      ``is_new_package``).
    """
    key = owner_key(owner_type, owner_id)
    if allow_confusable_name and not platform_admin:
        raise _forbidden(
            "confusable_override_not_allowed",
            "Only a platform administrator can override the package-name lookalike check.",
        )
    name_gate = is_new_package if first_public_listing is None else first_public_listing

    async def _public_name_gate(claimant_key: str) -> None:
        """Lookalike + sibling checks for a package's FIRST public listing.
        Runs before EVERY successful public return; a platform administrator
        may skip it only with the explicit, audited override."""
        if not name_gate or allow_confusable_name:
            return
        reject_forbidden_code_points(package_name, field="package_name")
        verdict = await find_confusable(db, package_name, claimant_key=claimant_key)
        if verdict is not None and verdict.blocked:
            raise confusable_error(verdict, what="package name")
        # Callers hold the namespace lock: the public names in this namespace
        # cannot change underneath this check.
        sibling = await public_sibling_confusable(db, namespace, package_name, package_id)
        if sibling is not None:
            raise confusable_error(sibling, what="package name")

    claimed = forced if forced is not None else await _publisher_by_skeleton(db, namespace)
    own: SkillPublisher | None = None
    if claimed is not None:
        mine = publisher_owner_key(claimed) == key
        if not mine and not (platform_admin and claimed.owner_type == "system"):
            if is_new_package:
                raise _conflict(
                    "namespace_claimed",
                    "This namespace belongs to another publisher.",
                    namespace=claimed.namespace,
                )
        elif claimed.namespace == namespace or forced is not None:
            own = claimed
    if own is not None and own.owner_type != "system":
        # Serialize with suspension / tombstoning / trust revocation and see
        # their committed result (not the possibly stale row we loaded).
        own = await lock_publisher(db, own.id)
        if own is None:  # pragma: no cover - publishers are never deleted
            raise _forbidden("publisher_tombstoned", "This namespace is retired.")
    if own is not None and own.status == "tombstoned":
        raise _forbidden("publisher_tombstoned", "This namespace is retired.")
    if own is not None and own.status == "suspended":
        raise _forbidden("publisher_suspended", "This publisher is suspended and cannot publish.")
    if visibility != "public":
        # The platform publisher only ever fronts PUBLIC platform listings.
        return own if own is None or own.owner_type != "system" else None
    if platform_admin and (own is None or own.owner_type == "system"):
        if claimed is not None and claimed.owner_type != "system" and publisher_owner_key(claimed) != key:
            raise _conflict(
                "namespace_claimed",
                "This namespace belongs to another publisher.",
                namespace=claimed.namespace,
            )
        # The platform publisher fronts only content SessionFS controls: the
        # `sessionfs` namespace. Other platform-admin public listings carry no
        # publisher (and so no badge).
        if namespace == SYSTEM_NAMESPACE:
            system = await ensure_system_publisher(db)
            await _public_name_gate("system")
            return system
        if skeleton(namespace) == skeleton(SYSTEM_NAMESPACE):
            raise _conflict(
                "namespace_confusable",
                "This namespace imitates the platform namespace; publish under 'sessionfs' itself.",
                conflict=SYSTEM_NAMESPACE,
                rule="exact",
            )
        await _public_name_gate(key)
        return None
    if own is None:
        raise _forbidden(
            "public_publishing_not_allowed",
            "Public packages must be published under your own verified publisher namespace.",
        )
    if not await is_verified(db, own):
        raise _forbidden(
            "publisher_not_verified",
            "Verify this publisher (DNS or GitHub) before publishing public packages.",
        )
    if not await has_current_domain_proofs(db, own):
        raise _forbidden(
            "reserved_name_proof_lapsed",
            "This namespace was unlocked by a domain proof that is no longer verified on this "
            "publisher; re-verify " + ", ".join(required_domains(own)) + " to publish public packages.",
            required_domains=required_domains(own),
        )
    if not own.public_publishing and not platform_admin:
        raise _forbidden(
            "public_publishing_not_enabled",
            "Public publishing is open to an invited cohort of verified publishers; "
            "ask SessionFS to enable it for this publisher.",
        )
    await _public_name_gate(key)
    return own


async def public_sibling_confusable(
    db: AsyncSession, namespace: str, package_name: str, package_id: str | None
) -> ConfusableVerdict | None:
    """A NEW public package name must not imitate another public package
    name in the same namespace (skeleton, distance, affix). The package
    itself (same id) is excluded."""
    statement = (
        select(SkillPackage.id, SkillPackage.name)
        .join(SkillMarketplaceListing, SkillMarketplaceListing.package_id == SkillPackage.id)
        .where(
            SkillPackage.namespace == namespace,
            SkillMarketplaceListing.visibility == "public",
        )
        .distinct()
    )
    names = [
        (pid, name)
        for pid, name in (await db.execute(statement)).all()
        if pid != package_id
    ]
    protected = [ProtectedName(name, skeleton(name), "", "package", True) for _pid, name in names]
    verdict = check_confusable(package_name, protected, owner_key="package")
    if verdict is not None and verdict.blocked:
        return verdict
    return None


async def has_public_listing(db: AsyncSession, package_id: str) -> bool:
    """Any public listing (any version, any moderation state) of a package."""
    return (
        await db.execute(
            select(SkillMarketplaceListing.id)
            .where(
                SkillMarketplaceListing.package_id == package_id,
                SkillMarketplaceListing.visibility == "public",
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Moderation of public listings (D1)
# ---------------------------------------------------------------------------


def capability_set(risk: dict, signature: dict) -> set[str]:
    caps: set[str] = set()
    for flag in ("scripts", "mcp", "executable_extensions", "hooks"):
        if risk.get(flag):
            caps.add(flag)
    for tool in risk.get("allowed_tools") or []:
        caps.add(f"allowed_tool:{tool}")
    for event in risk.get("hook_events") or []:
        caps.add(f"hook_event:{event}")
    return caps


def widening_reasons(
    new_risk: dict, new_signature: dict, old_risk: dict | None, old_signature: dict | None
) -> list[str]:
    old_caps = capability_set(old_risk or {}, old_signature or {})
    reasons = sorted(capability_set(new_risk, new_signature) - old_caps)
    old_kind = (old_signature or {}).get("kind")
    new_kind = new_signature.get("kind")
    if old_kind in TRUSTED_PROVENANCE_KINDS:
        if new_kind not in TRUSTED_PROVENANCE_KINDS:
            reasons.append("trust_downgrade:unattested_after_trusted_publish")
        elif new_kind != old_kind:
            # Trusted either way, but published from a different CI provider.
            reasons.append("trust_change:provider_changed")
    return reasons


def _json_obj(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def public_moderation_state(
    db: AsyncSession,
    *,
    publisher: SkillPublisher,
    package: SkillPackage,
    version: SkillPackageVersion,
    platform_admin: bool,
    provenance: dict | None = None,
) -> tuple[str, str | None]:
    """('approved' | 'pending_review', reason) for a NEW public listing.

    ``provenance`` is THIS publish's provenance, not the deduplicated version's
    ``signature_metadata`` (which records the run that FIRST minted the
    version): a reused version re-published without trusted attestation must be
    compared against the previous LISTING's provenance, so a trust downgrade is
    still caught even when the version row still carries the old github_oidc
    signature."""
    if platform_admin or publisher.owner_type == "system":
        return "approved", None
    approved_for_publisher = (
        await db.execute(
            select(SkillMarketplaceListing.id)
            .join(SkillPackage, SkillPackage.id == SkillMarketplaceListing.package_id)
            .where(
                SkillPackage.publisher_id == publisher.id,
                SkillMarketplaceListing.visibility == "public",
                SkillMarketplaceListing.moderation_state == "approved",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if approved_for_publisher is None:
        return "pending_review", "first_public_version"
    previous = (
        await db.execute(
            select(SkillMarketplaceListing, SkillPackageVersion)
            .join(SkillPackageVersion, SkillPackageVersion.id == SkillMarketplaceListing.version_id)
            .where(
                SkillMarketplaceListing.package_id == package.id,
                SkillMarketplaceListing.visibility == "public",
                SkillMarketplaceListing.moderation_state == "approved",
                SkillMarketplaceListing.version_id != version.id,
            )
            .order_by(SkillMarketplaceListing.published_at.desc(), SkillMarketplaceListing.id.desc())
            .limit(1)
        )
    ).first()
    previous_risk = _json_obj(previous[1].risk_summary) if previous is not None else None
    previous_signature = _json_obj(previous[0].signature_metadata) if previous is not None else None
    reasons = widening_reasons(
        _json_obj(version.risk_summary),
        provenance or {},
        previous_risk,
        previous_signature,
    )
    if reasons:
        return "pending_review", "widens_access:" + ",".join(reasons)[:900]
    scan = _json_obj(version.scan_summary)
    if scan.get("max_severity") in ("high", "critical"):
        return "pending_review", f"scan_{scan.get('max_severity')}"
    return "approved", None


# ---------------------------------------------------------------------------
# Verification sweeper and GitHub revocation
# ---------------------------------------------------------------------------


async def installation_owned_by(db: AsyncSession, user_id: str | None, installation_id: int) -> bool:
    """True only when ``user_id`` holds ``installation_id`` through a VERIFIED,
    PRIVILEGED and RECENT claim binding: the claimant proved through the
    state-bound OAuth flow (tk_b6c5e184ba8d4aa1) that they are the account
    itself or an active org admin, within CLAIM_PROOF_MAX_AGE - the same bar
    repository verification uses. A member's or installer's claim, a legacy
    claim (claim_verified_at NULL) or a stale proof never earns a GitHub
    publisher badge."""
    if not user_id:
        return False
    from datetime import datetime, timezone

    from sessionfs.server.services.github_oauth import CLAIM_PROOF_MAX_AGE, PRIVILEGED_ROLES

    row = (
        await db.execute(
            select(GitHubInstallation.id).where(
                GitHubInstallation.id == int(installation_id),
                GitHubInstallation.user_id == user_id,
                GitHubInstallation.claim_verified_at.is_not(None),
                GitHubInstallation.claim_role.in_(sorted(PRIVILEGED_ROLES)),
                GitHubInstallation.claim_verified_at
                >= datetime.now(timezone.utc) - CLAIM_PROOF_MAX_AGE,
            )
        )
    ).scalar_one_or_none()
    return row is not None


@dataclass(frozen=True)
class VerificationProbe:
    """Result of contacting a verification's source of truth (DNS or the
    GitHub App API). Computed WITHOUT holding any row lock; applied later
    under the publisher lock by :func:`apply_probe`."""

    method: str
    token: str | None  # dns: the token probed for
    dns_ok: bool = False
    transient: bool = False  # github: API failure, never mistaken for an uninstall
    owned: bool = True  # github: verified installation claim binding holds
    account: dict | None = None


async def probe_verification(
    db: AsyncSession, verification: SkillPublisherVerification
) -> VerificationProbe:
    if verification.method == "dns_txt":
        token = verification.verification_token or ""
        records = await domain_verification.resolve_txt(verification.subject)
        return VerificationProbe(
            "dns_txt", token, dns_ok=bool(token) and domain_verification.records_contain_token(records, token)
        )
    installation_id = int(verification.github_installation_id or 0)
    if not await installation_owned_by(db, verification.created_by_user_id, installation_id):
        return VerificationProbe(verification.method, None, owned=False)
    try:
        account = await github_app.get_installation_account(installation_id)
    except Exception:
        logger.info("github installation check failed for verification %s", verification.id)
        return VerificationProbe(verification.method, None, transient=True)
    return VerificationProbe(verification.method, None, account=account)


async def apply_probe(
    db: AsyncSession,
    verification: SkillPublisherVerification,
    probe: VerificationProbe,
    *,
    now: datetime | None = None,
) -> str:
    """Apply a probe to a verification row the caller has re-read under the
    publisher lock. Returns the new status."""
    now = now or now_utc()
    previous = verification.status
    if previous == "revoked":
        # Terminal: a probe taken before a revocation never resurrects or
        # rewrites it.
        return previous
    verification.last_checked_at = now
    if verification.method == "dns_txt":
        if probe.token != (verification.verification_token or ""):
            return verification.status  # the row changed since the probe
        if probe.dns_ok:
            verification.consecutive_failures = 0
            verification.first_failure_at = None
            verification.last_success_at = now
            verification.status_reason = None
            if previous in ("pending", "lapsed"):
                await _promote(db, verification, now)
        else:
            verification.consecutive_failures = (verification.consecutive_failures or 0) + 1
            if verification.first_failure_at is None:
                verification.first_failure_at = now
            if previous == "verified" and now - (utc(verification.first_failure_at) or now) >= DNS_LAPSE_AFTER:
                verification.status = "lapsed"
                verification.status_reason = "dns_record_missing_7_days"
            elif previous == "pending":
                verification.status_reason = "dns_record_not_found"
        return verification.status
    if not probe.owned:
        verification.status = "revoked"
        verification.status_reason = "installation_claim_unverified"
        return verification.status
    if probe.transient:
        return verification.status
    account = probe.account
    expected_type = "Organization" if verification.method == "github_org" else "User"
    if (
        account is None
        or account.get("suspended")
        or str(account.get("id")) != verification.subject
        or account.get("type") != expected_type
    ):
        verification.status = "revoked"
        verification.status_reason = "github_installation_gone"
        return verification.status
    verification.last_success_at = now
    verification.subject_display = f"github.com/{account.get('login')}"[:255]
    return verification.status


async def reload_verification(db: AsyncSession, verification_id: str) -> SkillPublisherVerification | None:
    return (
        await db.execute(
            select(SkillPublisherVerification)
            .where(SkillPublisherVerification.id == verification_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def check_verification(
    db: AsyncSession, verification: SkillPublisherVerification, *, now: datetime | None = None
) -> str:
    """Probe (network, no lock), then take the publisher lock, re-read the
    verification and apply. Returns the new status."""
    verification_id = verification.id
    publisher_id = verification.publisher_id
    subject = verification.subject if verification.method == "dns_txt" else None
    probe = await probe_verification(db, verification)
    await lock_confusable_promotion(db)
    if subject is not None:
        await lock_domain(db, subject)
    await lock_publisher(db, publisher_id)
    current = await reload_verification(db, verification_id)
    if current is None:
        return "revoked"
    return await apply_probe(db, current, probe, now=now)


async def subject_held_elsewhere(db: AsyncSession, verification: SkillPublisherVerification) -> bool:
    """Another verification currently holds this (method, subject)."""
    return (
        await db.execute(
            select(SkillPublisherVerification.id)
            .where(
                SkillPublisherVerification.method == verification.method,
                SkillPublisherVerification.subject == verification.subject,
                SkillPublisherVerification.status == "verified",
                SkillPublisherVerification.id != verification.id,
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None


async def domain_verified_by_other_org(db: AsyncSession, domain: str, org_id: str | None) -> bool:
    """An SSO domain proof for ``domain`` held by an org other than ``org_id``."""
    holder = (
        await db.execute(
            select(OrgDomainVerification.org_id).where(
                OrgDomainVerification.domain == _domain_key(domain),
                OrgDomainVerification.status == "verified",
            )
        )
    ).scalars().all()
    return any(owner != org_id for owner in holder)


async def domain_verified_by_other_publisher(db: AsyncSession, domain: str, org_id: str) -> bool:
    """A publisher DNS proof for ``domain`` held by a publisher that is not
    one of ``org_id``'s own publishers (used by the SSO promotion path)."""
    rows = (
        await db.execute(
            select(SkillPublisher.owner_type, SkillPublisher.org_id)
            .join(SkillPublisherVerification, SkillPublisherVerification.publisher_id == SkillPublisher.id)
            .where(
                SkillPublisherVerification.method == "dns_txt",
                SkillPublisherVerification.subject == _domain_key(domain),
                SkillPublisherVerification.status == "verified",
            )
        )
    ).all()
    return any(not (owner_type == "org" and owner == org_id) for owner_type, owner in rows)


async def _promote(db: AsyncSession, verification: SkillPublisherVerification, now: datetime) -> None:
    """Flip to verified unless another publisher holds the subject, or (DNS)
    another org's SSO proof holds the domain. Callers hold the domain lock;
    the partial unique index stays the race backstop (an IntegrityError at
    commit becomes a 409)."""
    if await subject_held_elsewhere(db, verification):
        verification.status_reason = "subject_verified_by_another_publisher"
        return
    owner = await db.get(SkillPublisher, verification.publisher_id)
    if verification.method == "dns_txt":
        if await domain_verified_by_other_org(db, verification.subject, owner.org_id if owner else None):
            verification.status_reason = "domain_verified_by_another_org"
            return
    if owner is not None and await promotion_conflict(db, owner) is not None:
        verification.status_reason = "namespace_confusable_with_verified_publisher"
        return
    verification.status = "verified"
    verification.verified_at = verification.verified_at or now


async def recheck_verifications(db: AsyncSession, *, limit: int = 200) -> dict:
    """Sweeper: re-check verified and lapsed proofs, oldest check first.
    Bounded batch; safe to call repeatedly (Cloud Scheduler, daily)."""
    rows = list(
        (
            await db.execute(
                select(SkillPublisherVerification)
                .where(SkillPublisherVerification.status.in_(("verified", "lapsed")))
                .order_by(
                    SkillPublisherVerification.last_checked_at.is_not(None),
                    SkillPublisherVerification.last_checked_at,
                    SkillPublisherVerification.id,
                )
                .limit(max(1, min(limit, 1000)))
            )
        ).scalars().all()
    )
    summary = {"checked": 0, "verified": 0, "lapsed": 0, "revoked": 0, "unchanged": 0, "conflicts": 0}
    ids = [row.id for row in rows]
    await db.commit()  # end the read transaction: hold nothing while probing
    for verification_id in ids:
        row = await reload_verification(db, verification_id)
        if row is None or row.status not in ("verified", "lapsed"):
            continue
        before = row.status
        # check_verification: network probe first, then the publisher lock,
        # a re-read of the row, and the transition (one publisher per commit).
        after = await check_verification(db, row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            summary["conflicts"] += 1
            continue
        summary["checked"] += 1
        if after == before:
            summary["unchanged"] += 1
        elif after in summary:
            summary[after] += 1
    return summary


async def record_installation_state(db: AsyncSession, installation_id: int, action: str) -> None:
    """Persist a signed installation lifecycle event (caller holds the
    installation lock): suspend / unsuspend / deleted."""
    state = await db.get(SkillGithubInstallationState, int(installation_id))
    if state is None:
        state = SkillGithubInstallationState(installation_id=int(installation_id))
        db.add(state)
    now = now_utc()
    if action == "suspend":
        state.suspended_at = now
    elif action == "unsuspend":
        state.suspended_at = None
    elif action == "deleted":
        state.deleted_at = now
    state.updated_at = now


async def installation_usable(db: AsyncSession, installation_id: int) -> bool:
    """No recorded suspension or deletion (caller holds the installation lock)."""
    state = (
        await db.execute(
            select(SkillGithubInstallationState)
            .where(SkillGithubInstallationState.installation_id == int(installation_id))
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return state is None or (state.suspended_at is None and state.deleted_at is None)


async def handle_installation_event(db: AsyncSession, installation_id: int, action: str) -> int:
    """Signed webhook: under the installation lock, record the state, then
    revoke the verifications it backs. A verification being created
    concurrently either committed first (and is revoked here) or waits for
    this lock and then sees the recorded state (and is refused)."""
    await lock_installation(db, int(installation_id))
    await record_installation_state(db, int(installation_id), action)
    if action in ("suspend", "deleted"):
        reason = "github_installation_suspended" if action == "suspend" else "github_installation_deleted"
        return await revoke_installation_verifications(db, int(installation_id), reason)
    return 0


async def revoke_installation_verifications(db: AsyncSession, installation_id: int, reason: str) -> int:
    """Revoke every live verification backed by an installation. Takes the
    publisher locks first (sorted, so concurrent bulk changes cannot
    deadlock), then re-reads each verification under its lock. Callers that
    can race verification creation hold the installation lock (see
    handle_installation_event)."""
    live = ("verified", "lapsed", "pending")
    publisher_ids = sorted(
        set(
            (
                await db.execute(
                    select(SkillPublisherVerification.publisher_id).where(
                        SkillPublisherVerification.github_installation_id == int(installation_id),
                        SkillPublisherVerification.status.in_(live),
                    )
                )
            ).scalars().all()
        )
    )
    for publisher_id in publisher_ids:
        await lock_publisher(db, publisher_id)
    rows = (
        await db.execute(
            select(SkillPublisherVerification)
            .where(
                SkillPublisherVerification.github_installation_id == int(installation_id),
                SkillPublisherVerification.publisher_id.in_(publisher_ids or [""]),
                SkillPublisherVerification.status.in_(live),
            )
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    now = now_utc()
    for row in rows:
        row.status = "revoked"
        row.status_reason = reason[:200]
        row.last_checked_at = now
    return len(rows)


# ---------------------------------------------------------------------------
# Trusted publishing
# ---------------------------------------------------------------------------


def validate_reusable_workflow(value: str | None) -> str | None:
    """``owner/repo/.github/workflows/file.yml`` of an optional reusable
    (callee) workflow, matched against the token's job_workflow_ref."""
    text = (value or "").strip() or None
    if text is not None and not _REUSABLE_RE.fullmatch(text):
        raise HTTPException(
            422,
            detail={
                "code": "invalid_reusable_workflow",
                "message": "reusable_workflow must look like owner/repo/.github/workflows/publish.yml",
            },
        )
    return text


def validate_trusted_publisher_input(
    *, repository: str, workflow: str, environment: str | None, ref_pattern: str | None
) -> tuple[str, str, str | None, str | None]:
    repository = (repository or "").strip()
    if not _REPO_RE.fullmatch(repository):
        raise HTTPException(422, detail={"code": "invalid_repository", "message": "repository must be owner/repo"})
    workflow = (workflow or "").strip()
    if "/" not in workflow:
        workflow = f".github/workflows/{workflow}"
    if not _WORKFLOW_PATH_RE.fullmatch(workflow):
        raise HTTPException(
            422,
            detail={
                "code": "invalid_workflow",
                "message": "workflow must be a file in .github/workflows ending in .yml or .yaml",
            },
        )
    env = (environment or "").strip() or None
    if env is not None and not _ENVIRONMENT_RE.fullmatch(env):
        raise HTTPException(422, detail={"code": "invalid_environment", "message": "environment name is invalid"})
    pattern = (ref_pattern or "").strip() or None
    if pattern is not None and (not _REF_PATTERN_RE.fullmatch(pattern) or "**" in pattern):
        raise HTTPException(
            422,
            detail={
                "code": "invalid_ref_pattern",
                "message": "ref_pattern must look like refs/tags/v* or refs/heads/main",
            },
        )
    return repository, workflow, env, pattern


def ref_matches(pattern: str | None, ref: str) -> bool:
    """Anchored glob where ``*`` matches any run of characters; nothing else
    is special (``fnmatch`` would also honour ``?`` and ``[...]``)."""
    if pattern is None:
        return True
    regex = "".join(".*" if char == "*" else re.escape(char) for char in pattern)
    return re.fullmatch(regex, ref) is not None


def workflow_matches(claims: dict, workflow_path: str | None, reusable_workflow: str | None = None) -> bool:
    """Bind the CALLER and, optionally, the CALLEE independently.

    ``workflow_ref`` (``<owner>/<repo>/<path>@<ref>``) names the workflow file
    the run was started from; it must be the configured file in the SAME
    repository the token was issued for. ``job_workflow_ref`` names the file
    that ran this job: equal to the caller for a plain workflow, the callee
    for a job in a reusable workflow. It is checked only when a reusable
    workflow is configured, and then must be exactly that file (any ref)."""
    repository = str(claims.get("repository") or "")
    caller = split_workflow_ref(claims.get("workflow_ref"))
    # owner/repo are case-insensitive on GitHub (and already bound by numeric
    # id); the workflow FILE path is compared exactly: release.yml and
    # Release.yml are different files.
    if not repository or caller is None or not workflow_path:
        return False
    if caller[0].casefold() != repository.casefold() or caller[1] != workflow_path:
        return False
    if reusable_workflow is None:
        return True
    callee = split_workflow_ref(claims.get("job_workflow_ref"))
    configured = split_workflow_ref(f"{reusable_workflow}@")
    return (
        callee is not None
        and configured is not None
        and callee[0].casefold() == configured[0].casefold()
        and callee[1] == configured[1]
    )


def split_workflow_ref(value: Any) -> tuple[str, str] | None:
    """``owner/repo/.github/workflows/file.yml@ref`` -> (``owner/repo``,
    ``.github/workflows/file.yml``). The path ends at the first ``@``
    (workflow file names cannot contain one; refs can)."""
    if not isinstance(value, str) or "@" not in value:
        return None
    head = value.split("@", 1)[0]
    parts = head.split("/", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return f"{parts[0]}/{parts[1]}", parts[2]


def event_allowed(claims: dict, allow_workflow_run: bool) -> bool:
    event = claims.get("event_name")
    if event in PUBLISH_EVENTS:
        return True
    return event == "workflow_run" and allow_workflow_run


def _environment_matches(configured: str | None, claimed: Any) -> bool:
    if configured is None:
        return True
    return isinstance(claimed, str) and claimed.casefold() == configured.casefold()


def bounded_claims(claims: dict) -> dict:
    return {key: claims[key] for key in _CLAIM_KEYS if isinstance(claims.get(key), str)}


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def exchange_oidc_claims(
    db: AsyncSession,
    claims: dict,
    *,
    requested_package: str | None,
    namespace: str | None = None,
) -> tuple[str, SkillPublishToken, SkillTrustedPublisher, SkillPublisher]:
    """Match verified GitHub claims to ONE active trusted publisher and mint a
    publish token. Every mismatch is the same 403 (no oracle about which
    part of the binding failed)."""
    ref = str(claims.get("ref") or "")
    denied = _forbidden(
        "trusted_publisher_mismatch",
        "No trusted publisher matches this workflow run.",
    )
    if not (ref.startswith("refs/heads/") or ref.startswith("refs/tags/")):
        raise denied
    candidates = (
        await db.execute(
            select(SkillTrustedPublisher, SkillPublisher)
            .join(SkillPublisher, SkillPublisher.id == SkillTrustedPublisher.publisher_id)
            .where(
                SkillTrustedPublisher.provider == "github",
                SkillTrustedPublisher.repository_id == int(claims["repository_id"]),
                SkillTrustedPublisher.repository_owner_id == int(claims["repository_owner_id"]),
                SkillTrustedPublisher.revoked_at.is_(None),
                SkillPublisher.status == "active",
            )
        )
    ).all()
    matches = [
        (tp, publisher)
        for tp, publisher in candidates
        if workflow_matches(claims, tp.workflow_path, tp.reusable_workflow)
        and event_allowed(claims, tp.allow_workflow_run)
        and _environment_matches(tp.environment, claims.get("environment"))
        and ref_matches(tp.ref_pattern, ref)
        and (requested_package is None or tp.package_name in (None, requested_package))
        # Naming the namespace makes the match immune to another publisher
        # registering the same repository and workflow (no ambiguity DoS).
        and (namespace is None or publisher.namespace == namespace)
    ]
    trusted, publisher = _single_match(matches, requested_package, denied)
    return await _mint_publish_token(
        db,
        trusted,
        publisher,
        requested_package=requested_package,
        issuer=GITHUB_ACTIONS_ISSUER,
        jti=str(claims["jti"]),
        claims=bounded_claims(claims),
    )


def _single_match(
    matches: list[tuple[SkillTrustedPublisher, SkillPublisher]],
    requested_package: str | None,
    denied: HTTPException,
) -> tuple[SkillTrustedPublisher, SkillPublisher]:
    if requested_package is not None:
        # Prefer the package-scoped binding over a namespace-wide one.
        scoped = [item for item in matches if item[0].package_name == requested_package]
        matches = scoped or matches
    if not matches:
        raise denied
    if len({tp.publisher_id for tp, _ in matches}) > 1 or len(matches) > 1 and requested_package is None:
        raise _conflict(
            "trusted_publisher_ambiguous",
            "Several trusted publishers match this run; pass the package name.",
        )
    return matches[0]


async def _mint_publish_token(
    db: AsyncSession,
    trusted: SkillTrustedPublisher,
    publisher: SkillPublisher,
    *,
    requested_package: str | None,
    issuer: str,
    jti: str,
    claims: dict,
) -> tuple[str, SkillPublishToken, SkillTrustedPublisher, SkillPublisher]:
    """Insert the hashed, single-use publish token. UNIQUE(oidc_issuer,
    oidc_jti) refuses a second exchange of the same OIDC token (401)."""
    raw = PUBLISH_TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = SkillPublishToken(
        id=f"spt_{secrets.token_hex(12)}",
        token_hash=token_hash(raw),
        trusted_publisher_id=trusted.id,
        publisher_id=publisher.id,
        package_name=trusted.package_name or requested_package,
        oidc_issuer=issuer[:255],
        oidc_jti=jti[:255],
        claims=json.dumps(claims, sort_keys=True),
        auth_generation=publisher.auth_generation or 0,
        expires_at=now_utc() + PUBLISH_TOKEN_TTL,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            401,
            detail={"code": "oidc_token_replayed", "message": "This OIDC token was already exchanged."},
        ) from None
    return raw, row, trusted, publisher


# ---------------------------------------------------------------------------
# GitLab CI trusted publishing
# ---------------------------------------------------------------------------


def validate_gitlab_trust_input(
    *,
    host: str | None,
    issuers: tuple[str, ...],
    project_path: str | None,
    environment: str | None,
    ref_pattern: str | None,
) -> tuple[str, str | None, str | None, str | None]:
    """(issuer, project_path, environment, ref_pattern) for a GitLab trust.

    The host must be on the operator allowlist (SFS_GITLAB_OIDC_ISSUERS) - a
    trust for any other instance is refused here and could never match at
    exchange (no JWKS is ever fetched for it)."""
    from sessionfs.server.services import gitlab_oidc

    issuer = gitlab_oidc.allowed_issuer(host or gitlab_oidc.GITLAB_COM_ISSUER, issuers)
    if issuer is None:
        raise HTTPException(
            422,
            detail={
                "code": "gitlab_host_not_allowed",
                "message": "This GitLab instance is not enabled on this server "
                "(the operator lists allowed instances in SFS_GITLAB_OIDC_ISSUERS).",
            },
        )
    path = (project_path or "").strip().strip("/") or None
    if path is not None and (len(path) > 200 or not _GITLAB_PROJECT_PATH_RE.fullmatch(path)):
        raise HTTPException(
            422, detail={"code": "invalid_project_path", "message": "project path must look like group/project"}
        )
    env = (environment or "").strip() or None
    if env is not None and not _GITLAB_ENVIRONMENT_RE.fullmatch(env):
        raise HTTPException(422, detail={"code": "invalid_environment", "message": "environment name is invalid"})
    pattern = (ref_pattern or "").strip() or None
    if pattern is not None and (not _REF_PATTERN_RE.fullmatch(pattern) or "**" in pattern):
        raise HTTPException(
            422,
            detail={
                "code": "invalid_ref_pattern",
                "message": "ref_pattern must look like refs/tags/v* or refs/heads/main",
            },
        )
    return issuer, path, env, pattern


def gitlab_source_allowed(claims: dict) -> bool:
    return claims.get("pipeline_source") in GITLAB_PUBLISH_SOURCES


def bounded_gitlab_claims(claims: dict) -> dict:
    kept = {key: claims[key] for key in _GITLAB_CLAIM_KEYS if isinstance(claims.get(key), str)}
    kept["iss"] = str(claims.get("iss") or "")
    return kept


async def exchange_gitlab_claims(
    db: AsyncSession,
    claims: dict,
    *,
    issuers: tuple[str, ...],
    requested_package: str | None,
    namespace: str | None = None,
) -> tuple[str, SkillPublishToken, SkillTrustedPublisher, SkillPublisher]:
    """Match VERIFIED GitLab CI claims to ONE active GitLab trusted publisher
    and mint a publish token. Every mismatch is the same 403.

    Binding: the row's issuer equals the token's verified iss AND is still
    allowlisted; numeric project id (and namespace id when pinned); the full
    ref (``ref_path``) under refs/heads or refs/tags and inside the row's
    ref pattern; a PROTECTED ref (``ref_protected`` == "true", always
    required); the environment when the row names one (exact); and a
    publishing pipeline source (never a merge request pipeline)."""
    denied = _forbidden(
        "trusted_publisher_mismatch",
        "No trusted publisher matches this pipeline job.",
    )
    issuer = str(claims.get("iss") or "")
    ref_path = str(claims.get("ref_path") or "")
    if issuer not in issuers:
        raise denied
    if not (ref_path.startswith("refs/heads/") or ref_path.startswith("refs/tags/")):
        raise denied
    if claims.get("ref_protected") != "true" or not gitlab_source_allowed(claims):
        raise denied
    candidates = (
        await db.execute(
            select(SkillTrustedPublisher, SkillPublisher)
            .join(SkillPublisher, SkillPublisher.id == SkillTrustedPublisher.publisher_id)
            .where(
                SkillTrustedPublisher.provider == "gitlab",
                SkillTrustedPublisher.gitlab_project_id == int(claims["project_id"]),
                SkillTrustedPublisher.issuer == issuer,
                SkillTrustedPublisher.revoked_at.is_(None),
                SkillPublisher.status == "active",
            )
        )
    ).all()
    matches = [
        (tp, publisher)
        for tp, publisher in candidates
        if tp.issuer in issuers
        and (tp.gitlab_namespace_id is None or tp.gitlab_namespace_id == int(claims["namespace_id"]))
        # GitLab environment names are case-sensitive: exact match.
        and (tp.environment is None or claims.get("environment") == tp.environment)
        # Opt-in: GitLab must report that environment as protected (a job
        # names its own environment; only protection restricts who runs it).
        and (not tp.require_protected_environment or claims.get("environment_protected") == "true")
        and ref_matches(tp.ref_pattern, ref_path)
        and (requested_package is None or tp.package_name in (None, requested_package))
        and (namespace is None or publisher.namespace == namespace)
    ]
    trusted, publisher = _single_match(matches, requested_package, denied)
    return await _mint_publish_token(
        db,
        trusted,
        publisher,
        requested_package=requested_package,
        issuer=issuer,
        jti=str(claims["jti"]),
        claims=bounded_gitlab_claims(claims),
    )


def _invalid_publish_token() -> HTTPException:
    return HTTPException(
        401,
        detail={
            "code": "invalid_publish_token",
            "message": "Publish token is invalid, expired or already used.",
        },
    )


async def load_publish_token(
    db: AsyncSession, raw: str | None
) -> tuple[SkillPublishToken, SkillTrustedPublisher, SkillPublisher]:
    """Look up an unexpired, unused publish token and its still-active
    trusted publisher and publisher. Constant 401 otherwise."""
    if not raw or not raw.startswith(PUBLISH_TOKEN_PREFIX) or len(raw) > 200:
        raise _invalid_publish_token()
    row = (
        await db.execute(
            select(SkillPublishToken, SkillTrustedPublisher, SkillPublisher)
            .join(SkillTrustedPublisher, SkillTrustedPublisher.id == SkillPublishToken.trusted_publisher_id)
            .join(SkillPublisher, SkillPublisher.id == SkillPublishToken.publisher_id)
            .where(SkillPublishToken.token_hash == token_hash(raw))
        )
    ).first()
    if row is None:
        raise _invalid_publish_token()
    token, trusted, publisher = row
    if (
        token.used_at is not None
        or (utc(token.expires_at) or now_utc()) <= now_utc()
        or trusted.revoked_at is not None
        or publisher.status != "active"
        or (token.auth_generation or 0) != (publisher.auth_generation or 0)
    ):
        raise _invalid_publish_token()
    return token, trusted, publisher


async def burn_publish_token(db: AsyncSession, token_id: str) -> None:
    """Atomically mark the token used (rowcount-1 guard) and COMMIT, before
    any publish work: a token can authorize at most one publish attempt."""
    now = now_utc()
    result = await db.execute(
        update(SkillPublishToken)
        .where(
            SkillPublishToken.id == token_id,
            SkillPublishToken.used_at.is_(None),
            SkillPublishToken.expires_at > now,
        )
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    if (result.rowcount or 0) != 1:
        await db.rollback()
        raise _invalid_publish_token()
    await db.commit()


def provenance_metadata(token: SkillPublishToken, trusted: SkillTrustedPublisher, verifier: str) -> dict:
    """signature_metadata for a trusted publish: PUBLICATION provenance, i.e.
    which GitHub workflow run published these bytes, from the VERIFIED OIDC
    token. It is not build provenance: nothing binds the archive digest to the
    commit (that needs a verified attestation, design §4.12), so the commit is
    recorded as the publishing run's SHA and source_commit is left alone."""
    claims = _json_obj(token.claims)
    if trusted.provider == "gitlab":
        return _gitlab_provenance(token, trusted, claims, verifier)
    repository = claims.get("repository", "")

    def _int(name: str) -> int | None:
        value = str(claims.get(name, ""))
        return int(value) if value.isdigit() else None

    return {
        "kind": "github_oidc",
        "claim": "publication",
        "verification": "oidc_token",
        "build_provenance": None,
        "attestation": None,
        "issuer": GITHUB_ACTIONS_ISSUER,
        "published_from_repo": f"github.com/{repository}" if repository else None,
        "published_from_repo_id": _int("repository_id"),
        "published_from_owner": claims.get("repository_owner"),
        "published_from_owner_id": _int("repository_owner_id"),
        "publishing_workflow": trusted.workflow_path,
        "publishing_workflow_ref": claims.get("workflow_ref"),
        "publishing_job_workflow_ref": claims.get("job_workflow_ref"),
        "publishing_ref": claims.get("ref"),
        "publishing_run_sha": claims.get("sha"),
        "publishing_environment": claims.get("environment"),
        "publishing_event": claims.get("event_name"),
        "publishing_run_id": claims.get("run_id"),
        "publishing_run_attempt": claims.get("run_attempt"),
        "runner_environment": claims.get("runner_environment"),
        "publishing_run_url": (
            f"https://github.com/{repository}/actions/runs/{claims.get('run_id')}"
            if repository and str(claims.get("run_id", "")).isdigit()
            else None
        ),
        "trusted_publisher_id": trusted.id,
        "verified_at": now_utc().isoformat(),
        "verifier_version": verifier,
    }


def _gitlab_provenance(token: SkillPublishToken, trusted: SkillTrustedPublisher, claims: dict, verifier: str) -> dict:
    """Publication provenance for a GitLab CI publish. Its own ``kind``
    (``gitlab_oidc``) and its own id fields: a GitLab project id is never
    written where a GitHub repository id is expected (the org skills policy
    matches ``published_from_repo_id`` against GitHub repositories only)."""

    def _int(name: str) -> int | None:
        value = str(claims.get(name, ""))
        return int(value) if value.isascii() and value.isdigit() else None

    issuer = token.oidc_issuer
    host = issuer.split("://", 1)[-1]
    project_path = claims.get("project_path") or ""
    job_id = _int("job_id")
    return {
        "kind": "gitlab_oidc",
        "claim": "publication",
        "verification": "oidc_token",
        "build_provenance": None,
        "attestation": None,
        "issuer": issuer,
        "published_from_repo": f"{host}/{project_path}" if project_path else None,
        "published_from_project_id": _int("project_id"),
        "published_from_namespace": claims.get("namespace_path"),
        "published_from_namespace_id": _int("namespace_id"),
        "publishing_ref": claims.get("ref_path"),
        "publishing_ref_protected": claims.get("ref_protected") == "true",
        "publishing_run_sha": claims.get("sha"),
        "publishing_environment": claims.get("environment"),
        "publishing_environment_protected": (
            claims.get("environment_protected") == "true" if claims.get("environment") else None
        ),
        "publishing_event": claims.get("pipeline_source"),
        "publishing_pipeline_id": claims.get("pipeline_id"),
        "publishing_job_id": claims.get("job_id"),
        "publishing_ci_config_ref_uri": claims.get("ci_config_ref_uri"),
        "runner_environment": claims.get("runner_environment"),
        "publishing_run_url": (
            f"{issuer}/{project_path}/-/jobs/{job_id}" if project_path and job_id is not None else None
        ),
        "trusted_publisher_id": trusted.id,
        "verified_at": now_utc().isoformat(),
        "verifier_version": verifier,
    }


async def sweep_expired_tokens(db: AsyncSession, *, older_than: timedelta = timedelta(days=1)) -> int:
    """Delete publish tokens past their expiry by ``older_than``. The jti
    stays unique only while its row exists, so keep rows well beyond the
    OIDC token's own acceptance window (minutes)."""
    from sqlalchemy import delete

    cutoff = now_utc() - older_than
    result = await db.execute(delete(SkillPublishToken).where(SkillPublishToken.expires_at < cutoff))
    return int(result.rowcount or 0)



def bump_auth_generation(publisher: SkillPublisher) -> None:
    """Invalidate every publish token minted for this publisher so far."""
    publisher.auth_generation = (publisher.auth_generation or 0) + 1


async def suspend_publisher(db: AsyncSession, publisher_id: str, reason: str) -> tuple[SkillPublisher, list[str]]:
    """Suspend under the publisher lock (so an in-flight publish either
    finishes first and its listing is collected here, or sees the suspension
    and is refused). Moves approved public listings to review and bumps the
    authorization generation. Caller commits."""
    publisher = await lock_publisher(db, publisher_id)
    if publisher is None:
        raise HTTPException(404, "Publisher not found")
    if publisher.owner_type == "system":
        raise _conflict("system_publisher", "The platform publisher cannot be suspended.")
    if publisher.status != "active":
        raise _conflict("publisher_not_active", f"Publisher is {publisher.status}.")
    listing_ids = sorted(
        (
            await db.execute(
                select(SkillMarketplaceListing.id)
                .join(SkillPackage, SkillPackage.id == SkillMarketplaceListing.package_id)
                .where(
                    SkillPackage.publisher_id == publisher.id,
                    SkillMarketplaceListing.visibility == "public",
                    SkillMarketplaceListing.moderation_state == "approved",
                )
            )
        ).scalars().all()
    )
    if listing_ids:
        await db.execute(
            update(SkillMarketplaceListing)
            .where(SkillMarketplaceListing.id.in_(listing_ids))
            .values(moderation_state="pending_review", moderation_reason="publisher_suspended")
            .execution_options(synchronize_session=False)
        )
    publisher.status = "suspended"
    publisher.suspended_reason = reason
    publisher.suspended_listing_ids = json.dumps(listing_ids)
    bump_auth_generation(publisher)
    return publisher, listing_ids



# ---------------------------------------------------------------------------
# Transaction-scoped advisory locks
# ---------------------------------------------------------------------------


def advisory_key(scope: str, value: str) -> int:
    """Signed 64-bit PostgreSQL advisory-lock key for (scope, value)."""
    digest = hashlib.sha256(f"sessionfs-publishers:{scope}:{value}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def advisory_xact_lock(db: AsyncSession, scope: str, value: str) -> None:
    """``pg_advisory_xact_lock`` (released at commit / rollback). SQLite has
    no advisory locks: a zero-row UPDATE opens the database write
    transaction, which serializes every writer until commit."""
    from sqlalchemy import text

    connection = await db.connection()
    if connection.dialect.name != "postgresql":
        await db.execute(text("UPDATE skill_publishers SET id = id WHERE 1 = 0"))
        return
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": advisory_key(scope, value)})


async def lock_namespace(db: AsyncSession, namespace: str) -> None:
    """Serializes namespace claims with first package publishes in the same
    namespace (keyed by the confusable skeleton, so lookalikes share it)."""
    await advisory_xact_lock(db, "namespace", skeleton(namespace))


async def lock_installation(db: AsyncSession, installation_id: int) -> None:
    await advisory_xact_lock(db, "installation", str(int(installation_id)))


def _domain_key(domain: str) -> str:
    try:
        return domain_verification.canonical_domain(domain)
    except ValueError:
        return (domain or "").strip().lower().rstrip(".")


async def lock_domain(db: AsyncSession, domain: str) -> None:
    """Serializes SSO and publisher DNS promotion of the same domain (keyed
    by the canonical spelling)."""
    await advisory_xact_lock(db, "domain", _domain_key(domain))



async def lock_confusable_promotion(db: AsyncSession) -> None:
    """One global lock (fixed key) for namespace claims and every transition
    into `verified`: two unverified lookalike namespaces (northstar /
    northstar-official) cannot both become verified, because each promotion
    re-checks the current strong names under this lock. First in the lock
    order (before namespace / installation / domain / publisher)."""
    await advisory_xact_lock(db, "confusable-promotion", "global")


async def promotion_conflict(db: AsyncSession, publisher: SkillPublisher) -> ConfusableVerdict | None:
    """Would verifying ``publisher`` make it a lookalike of a CURRENT strong
    name (verified, system or retired namespace) held by someone else? An
    already-verified publisher is already strong, so it has no conflict.
    Callers hold lock_confusable_promotion()."""
    if publisher.owner_type == "system" or publisher.id in await verified_publisher_ids(db, [publisher.id]):
        return None
    key = publisher_owner_key(publisher)
    protected = [
        ProtectedName(
            other.namespace,
            other.namespace_normalized,
            "" if other.status == "tombstoned" else publisher_owner_key(other),
            "tombstone" if other.status == "tombstoned" else "publisher",
            True,
        )
        for other in await _strong_publishers(db)
        if other.id != publisher.id
    ]
    verdict = check_confusable(publisher.namespace, protected, owner_key=key)
    if verdict is not None and verdict.blocked:
        return verdict
    # Symmetric: existing PUBLIC package names of other owners must not
    # already imitate this namespace (e.g. a `northstar-official` package
    # published while `northstar` was unverified, hence not yet protected).
    as_protected = ProtectedName(publisher.namespace, publisher.namespace_normalized, key, "publisher", True)
    rows = (
        await db.execute(
            select(SkillPackage.name, SkillPackage.owner_type, SkillPackage.owner_id)
            .join(SkillMarketplaceListing, SkillMarketplaceListing.package_id == SkillPackage.id)
            .where(SkillMarketplaceListing.visibility == "public")
            .distinct()
        )
    ).all()
    for name, package_owner_type, package_owner_id in rows:
        package_owner = owner_key(package_owner_type, package_owner_id)
        if package_owner == key:
            continue
        hit = check_confusable(name, [as_protected], owner_key=package_owner)
        if hit is not None and hit.blocked:
            return ConfusableVerdict(True, hit.rule, name, "package")
    return None

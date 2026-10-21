"""Poll registered Git marketplace registries and publish new versions
(GitHub #69, migration 079).

Runs ONLY from the scheduler endpoint (``POST /api/v1/admin/skills/
registries/poll``: Cloud Scheduler on hosted, ``sfs admin registries poll``
or the Helm CronJob on self-hosted), never from a user request. Per poll:

1. Claim due registries with a lease (conditional UPDATE, rowcount 1): two
   API instances never poll the same registry at once.
2. Re-check the registry (org registry, approved, org still entitled to the
   team skill library, poll actor still an active owner/admin), resolve the
   credential reference (fail closed), resolve + pin the host (SSRF guard).
3. ``git ls-remote`` the tags (or the branch head) in a worker thread;
   select refs matching the registry's pattern whose (ref, commit) was never
   processed (``skill_registry_poll_refs`` is the idempotency record).
4. For each new ref (oldest first, at most MAX_NEW_REFS_PER_POLL): fetch the
   tree in a worker thread and publish each package through the SAME
   ``_publish_package`` path as a manual org publish (validation, DLP,
   instruction scanning, disclosure, moderation, quotas, fences).
5. Record the poll status; repeated failures back off exponentially.

A refused package (DLP, scanner, validation) is recorded against its ref
and is not retried until the tag moves; a registry-level failure (network,
credential, entitlement) is NOT recorded per ref, so the ref is retried
after the backoff.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import AuthContext
from sessionfs.server.db.models import (
    Organization,
    OrgMember,
    SkillMarketplaceRegistry,
    SkillRegistryPollRef,
    User,
)
from sessionfs.server.services import registry_polling as rp
from sessionfs.server.storage.base import BlobStore
from sessionfs.skills.models import SkillError
from sessionfs.skills.sources import GitTransportError

logger = logging.getLogger("sessionfs.api")

LEASE_SECONDS = 20 * 60


def _lease_seconds() -> int:
    """The lease outlives the longest poll the deadline allows (plus publish
    time), so a slow poll is never taken over while it is still running."""
    return max(LEASE_SECONDS, rp.poll_timeout_seconds() + rp.PUBLISH_SLACK_SECONDS + 10 * 60)


DEFAULT_BATCH = 5
MAX_BATCH = 20

# One poll run per API process at a time (the lease covers other processes).
_RUN_LOCK = asyncio.Lock()

_REJECTED_403_CODES = {"package_blocked", "content_blocked", "publisher_tombstoned"}


class RegistryPollFailure(Exception):
    """A registry-level failure: recorded as last_poll_status=error with a
    sanitized summary and exponential backoff. ``code`` is stable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class RefResult:
    ref: str
    commit: str
    outcome: str
    version_ids: list[str] = field(default_factory=list)
    packages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PollResult:
    registry_id: str
    status: str  # ok | partial | error | skipped
    summary: str | None = None
    refs: list[RefResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "registry_id": self.registry_id,
            "status": self.status,
            "summary": self.summary,
            "refs": [
                {
                    "ref": item.ref,
                    "commit": item.commit,
                    "outcome": item.outcome,
                    "version_ids": item.version_ids,
                    "packages": item.packages,
                }
                for item in self.refs
            ],
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _due_predicate(now: datetime):
    return [
        SkillMarketplaceRegistry.owner_type == "org",
        SkillMarketplaceRegistry.poll_interval_seconds > 0,
        or_(SkillMarketplaceRegistry.next_poll_at.is_(None), SkillMarketplaceRegistry.next_poll_at <= now),
        or_(
            SkillMarketplaceRegistry.poll_lease_expires_at.is_(None),
            SkillMarketplaceRegistry.poll_lease_expires_at < now,
        ),
    ]


async def claim_registry(
    db: AsyncSession, registry_id: str, *, owner: str, now: datetime, force: bool = False
) -> bool:
    """Take the poll lease of one registry (atomic conditional UPDATE). With
    ``force`` the due check is skipped (an explicit admin run); an unexpired
    lease held by another poller is still respected."""
    predicates = [SkillMarketplaceRegistry.id == registry_id]
    if force:
        predicates += [
            SkillMarketplaceRegistry.owner_type == "org",
            SkillMarketplaceRegistry.poll_interval_seconds > 0,
            or_(
                SkillMarketplaceRegistry.poll_lease_expires_at.is_(None),
                SkillMarketplaceRegistry.poll_lease_expires_at < now,
            ),
        ]
    else:
        predicates += _due_predicate(now)
    result = await db.execute(
        update(SkillMarketplaceRegistry)
        .where(*predicates)
        .values(poll_lease_owner=owner, poll_lease_expires_at=now + timedelta(seconds=_lease_seconds()))
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return (result.rowcount or 0) == 1  # type: ignore[attr-defined]


async def due_registry_ids(
    db: AsyncSession, *, now: datetime, limit: int, per_org: int | None = None
) -> list[str]:
    """Due registries, fair across orgs: candidates are taken most-overdue
    first, then interleaved round-robin by org with at most ``per_org`` from
    any one org, so one org's many registries cannot crowd out the others."""
    per_org = per_org or rp.max_per_org_per_run()
    rows = (
        await db.execute(
            select(SkillMarketplaceRegistry.id, SkillMarketplaceRegistry.org_id)
            .where(*_due_predicate(now))
            .order_by(
                case((SkillMarketplaceRegistry.next_poll_at.is_(None), 0), else_=1),
                SkillMarketplaceRegistry.next_poll_at,
                SkillMarketplaceRegistry.id,
            )
            .limit(max(limit * 20, 100))
        )
    ).all()
    queues: dict[str, list[str]] = {}
    for registry_id, org_id in rows:
        queue = queues.setdefault(str(org_id), [])
        if len(queue) < per_org:
            queue.append(registry_id)
    picked: list[str] = []
    while len(picked) < limit and any(queues.values()):
        for queue in queues.values():  # insertion order = most overdue org first
            if queue and len(picked) < limit:
                picked.append(queue.pop(0))
    return picked


class _LeaseLost(Exception):
    """Another poller took this registry over; stop without writing state."""


async def _renew_lease(db: AsyncSession, registry_id: str, owner: str) -> None:
    result = await db.execute(
        update(SkillMarketplaceRegistry)
        .where(
            SkillMarketplaceRegistry.id == registry_id,
            SkillMarketplaceRegistry.poll_lease_owner == owner,
        )
        .values(poll_lease_expires_at=_now() + timedelta(seconds=_lease_seconds()))
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if (result.rowcount or 0) != 1:  # type: ignore[attr-defined]
        raise _LeaseLost()


async def _finish(
    db: AsyncSession,
    registry_id: str,
    *,
    owner: str,
    status: str,
    summary: str | None,
    failed: bool,
    interval: int,
    failures: int,
) -> None:
    """Record the outcome and release the lease, fenced on our lease owner:
    a poller whose lease expired (and was taken over) never clobbers the
    newer poller's state."""
    now = _now()
    if failed:
        failures += 1
        delay = rp.backoff_seconds(interval, failures)
    else:
        failures = 0
        delay = max(interval, rp.MIN_POLL_INTERVAL_SECONDS)
    values: dict[str, Any] = {
        "last_poll_at": now,
        "last_poll_status": status,
        "last_poll_error_summary": summary,
        "consecutive_failures": failures,
        "next_poll_at": now + timedelta(seconds=delay),
        "poll_lease_owner": None,
        "poll_lease_expires_at": None,
    }
    await db.execute(
        update(SkillMarketplaceRegistry)
        .where(
            SkillMarketplaceRegistry.id == registry_id,
            SkillMarketplaceRegistry.poll_lease_owner == owner,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await db.commit()


def _classify(exc: HTTPException) -> tuple[bool, str]:
    """(package_level, code) for a refused publish. Package-level refusals
    are recorded against the ref; anything else fails the registry poll."""
    detail = exc.detail
    code = ""
    if isinstance(detail, dict):
        code = str(detail.get("code") or detail.get("reason") or "")
        reason = detail.get("reason")
        if exc.status_code == 422 and reason:
            code = f"{code}:{reason}" if code else str(reason)
    status = exc.status_code
    if status in (413, 422):
        return True, code or f"http_{status}"
    if status == 409 and code != "publish_conflict":
        return True, code or "conflict"
    if status == 403 and code in _REJECTED_403_CODES:
        return True, code
    if code == "skill_package_version_limit":
        return True, code
    return False, code or f"http_{status}"


async def _actor_role(db: AsyncSession, org_id: str, user_id: str | None) -> tuple[User, str]:
    if not user_id:
        raise RegistryPollFailure(
            "poll_actor_missing",
            "No organization admin is recorded for polling; an owner or admin must re-save the polling settings.",
        )
    user = (
        await db.execute(select(User).where(User.id == user_id).execution_options(populate_existing=True))
    ).scalar_one_or_none()
    role = (
        await db.execute(select(OrgMember.role).where(OrgMember.org_id == org_id, OrgMember.user_id == user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active or role not in ("owner", "admin"):
        raise RegistryPollFailure(
            "poll_actor_not_admin",
            "The admin who configured polling is no longer an active owner or admin; "
            "an owner or admin must re-save the polling settings.",
        )
    return user, str(role)


async def poll_registry(
    db: AsyncSession,
    registry_id: str,
    *,
    store: BlobStore,
    owner: str,
) -> PollResult:
    """Poll one registry whose lease ``owner`` holds."""
    from sessionfs.server.routes import marketplace as mp

    registry = (
        await db.execute(
            select(SkillMarketplaceRegistry)
            .where(SkillMarketplaceRegistry.id == registry_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if registry is None or registry.poll_lease_owner != owner:
        return PollResult(registry_id=registry_id, status="skipped", summary="lease lost")
    interval = int(registry.poll_interval_seconds or 0)
    failures = int(registry.consecutive_failures or 0)
    previous_status = registry.last_poll_status
    previous_summary = registry.last_poll_error_summary
    secrets_in_play: tuple[str, ...] = ()
    result = PollResult(registry_id=registry_id, status="ok")
    deadline = time.monotonic() + rp.poll_timeout_seconds()
    try:
        if registry.owner_type != "org" or not registry.org_id or interval <= 0:
            raise RegistryPollFailure("not_pollable", "Polling is only available for organization registries.")
        if registry.policy_state != "approved":
            raise RegistryPollFailure(
                "registry_not_approved",
                f"Polling is paused while the registry policy is '{registry.policy_state}'.",
            )
        org = (await db.execute(select(Organization).where(Organization.id == registry.org_id))).scalar_one_or_none()
        if org is None:
            raise RegistryPollFailure("org_missing", "Organization not found.")
        if not await mp._org_has_feature(db, org, "team_skill_library"):
            raise RegistryPollFailure(
                "upgrade_required", "Registry polling requires the organization's team skill library (Team+)."
            )
        actor, role = await _actor_role(db, org.id, registry.poll_actor_user_id)
        pattern = None
        if not registry.branch_only_mode:
            try:
                pattern = rp.compile_tag_pattern(registry.tag_pattern)
            except ValueError as exc:
                raise RegistryPollFailure("invalid_tag_pattern", str(exc)) from exc
        branch = None
        if registry.branch_only_mode:
            try:
                branch = rp.validate_branch(registry.branch or "")
            except ValueError as exc:
                raise RegistryPollFailure("invalid_branch", str(exc)) from exc
        source_url = registry.source_url
        try:
            _normalized, host, _port = rp.check_poll_url(source_url)
        except rp.FetchBlocked as exc:
            raise RegistryPollFailure("fetch_blocked", str(exc)) from exc
        credential = None
        if registry.credential_secret_ref:
            try:
                credential = rp.resolve_credential(registry.credential_secret_ref, org_id=org.id, host=host)
            except rp.CredentialError as exc:
                # The org sees one generic message; the specific cause is for
                # the operator's logs (never the secret itself).
                logger.warning(
                    "registry poll credential refused registry=%s ref=%s reason=%s",
                    registry_id,
                    registry.credential_secret_ref,
                    exc.reason,
                )
                raise RegistryPollFailure("credential_unavailable", str(exc)) from exc
            secrets_in_play = credential.secrets()
        processed = {
            (row.ref_name, row.commit_sha)
            for row in (
                await db.execute(
                    select(SkillRegistryPollRef.ref_name, SkillRegistryPollRef.commit_sha).where(
                        SkillRegistryPollRef.registry_id == registry_id
                    )
                )
            ).all()
        }
        org_id = org.id
        actor_id = actor.id
        alias = registry.alias
        namespace = registry.publish_namespace or "org"
        # No transaction is held across the network work below.
        await db.commit()
        with rp.sandbox_dir() as sandbox:
            try:
                transport = await asyncio.to_thread(rp.prepare_transport, source_url, credential, sandbox)
            except rp.FetchBlocked as exc:
                raise RegistryPollFailure("fetch_blocked", str(exc)) from exc
            try:
                remote = await asyncio.to_thread(rp.list_remote_refs, transport, sandbox, branch=branch)
            except SkillError as exc:
                raise RegistryPollFailure("list_refs_failed", str(exc)) from exc
            if branch is not None:
                candidates = [ref for ref in remote if ref.name == branch]
                if not candidates:
                    raise RegistryPollFailure("branch_not_found", f"Branch '{branch}' was not found.")
            else:
                assert pattern is not None  # noqa: S101 - set above for tag mode
                candidates = sorted(
                    (ref for ref in remote if pattern.fullmatch(ref.name)),
                    key=lambda ref: rp.version_sort_key(ref.name),
                )
            fresh = [ref for ref in candidates if (ref.ref, ref.commit) not in processed]
            backlog = fresh[: -rp.MAX_NEW_REFS_PER_POLL] if len(fresh) > rp.MAX_NEW_REFS_PER_POLL else []
            todo = fresh[len(backlog):]
            for ref in backlog:
                result.refs.append(RefResult(ref=ref.ref, commit=ref.commit, outcome="skipped",
                                             packages=[{"code": "backlog_older_than_newest"}]))
                await _record_ref(db, registry_id, result.refs[-1])
            for ref in todo:
                # Keep the lease ahead of the work (and stop if it was lost).
                await _renew_lease(db, registry_id, owner)
                try:
                    packages = await asyncio.to_thread(rp.fetch_ref_packages, transport, ref, deadline=deadline)
                except GitTransportError as exc:
                    # Network, TLS, HTTP 5xx, auth, timeouts, a ref that moved:
                    # never a verdict on the content. Back off and retry; the
                    # ref is NOT recorded, so the next poll fetches it again.
                    raise RegistryPollFailure("fetch_failed", str(exc)) from exc
                except SkillError as exc:
                    message = str(exc)
                    # A deterministic refusal of the fetched tree itself
                    # (unsafe paths, per-file / file-count / size limits, bad
                    # manifest): recorded against this (ref, commit).
                    ref_result = RefResult(
                        ref=ref.ref,
                        commit=ref.commit,
                        outcome="rejected",
                        packages=[{"code": "tree_rejected", "message": rp.sanitize_summary(message, secrets_in_play, limit=200)}],
                    )
                    result.refs.append(ref_result)
                    await _record_ref(db, registry_id, ref_result)
                    continue
                ref_result = RefResult(ref=ref.ref, commit=ref.commit, outcome="published")
                if not packages:
                    ref_result.outcome = "rejected"
                    ref_result.packages.append({"code": "no_packages"})
                for package in packages:
                    await _renew_lease(db, registry_id, owner)
                    if package.data is None:
                        ref_result.packages.append({"name": package.name, "outcome": "rejected",
                                                    "code": package.error_code or "invalid_package"})
                        continue
                    auth = AuthContext(
                        user=await _reload_actor(db, actor_id),
                        api_key_id=f"registry-poll:{registry_id}"[:64],
                        key_kind="service",
                        scopes=["skills:admin"],
                        org_id=org_id,
                        service_key_id=None,
                        service_key_name=f"registry-poll:{alias}"[:100],
                    )
                    org_row = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
                    try:
                        published = await mp._publish_package(
                            request=None,
                            store=store,
                            db=db,
                            auth=auth,
                            owner_type="org",
                            owner_id=org_id,
                            org=org_row,
                            role=role,
                            upload=None,
                            data=package.data,
                            namespace=namespace,
                            collection="org",
                            visibility="org",
                            source_locator=source_url,
                            source_commit=ref.commit,
                            source_subdir=package.subdir,
                            provenance={
                                "source": "registry_poll",
                                "registry_id": registry_id,
                                "ref": ref.ref,
                                "commit": ref.commit,
                            },
                            reauthorize=functools.partial(mp._fence_org_publisher, org_id=org_id, user_id=actor_id),
                        )
                    except HTTPException as exc:
                        await db.rollback()
                        package_level, code = _classify(exc)
                        if not package_level:
                            raise RegistryPollFailure("publish_failed", f"Publishing was refused ({code}).") from exc
                        ref_result.packages.append({"name": package.name, "outcome": "rejected", "code": code[:100]})
                        continue
                    version_id = str(published.get("version_id") or "")
                    if version_id:
                        ref_result.version_ids.append(version_id)
                    ref_result.packages.append({"name": package.name, "outcome": "published",
                                                "version_id": version_id or None})
                rejected = sum(1 for item in ref_result.packages if item.get("outcome") == "rejected")
                published_count = sum(1 for item in ref_result.packages if item.get("outcome") == "published")
                if ref_result.outcome != "rejected":
                    if published_count == 0:
                        ref_result.outcome = "rejected"
                    elif rejected:
                        ref_result.outcome = "partial"
                result.refs.append(ref_result)
                await _record_ref(db, registry_id, ref_result)
    except _LeaseLost:
        await db.rollback()
        logger.warning("registry poll lease lost registry=%s", registry_id)
        result.status, result.summary = "skipped", "lease lost"
        return result
    except RegistryPollFailure as exc:
        await db.rollback()
        summary = rp.sanitize_summary(f"{exc.code}: {exc}", secrets_in_play)
        result.status = "error"
        result.summary = summary
        logger.warning("registry poll failed registry=%s code=%s", registry_id, exc.code)
        await _finish(db, registry_id, owner=owner, status="error", summary=summary, failed=True,
                      interval=max(interval, rp.MIN_POLL_INTERVAL_SECONDS), failures=failures)
        return result
    except Exception as exc:  # noqa: BLE001 - every failure is recorded, never raised to the scheduler
        await db.rollback()
        summary = rp.sanitize_summary(f"internal_error: {type(exc).__name__}", secrets_in_play)
        result.status = "error"
        result.summary = summary
        logger.exception("registry poll crashed registry=%s", registry_id)
        await _finish(db, registry_id, owner=owner, status="error", summary=summary, failed=True,
                      interval=max(interval, rp.MIN_POLL_INTERVAL_SECONDS), failures=failures)
        return result
    problems = [item for item in result.refs if item.outcome in ("partial", "rejected")]
    if problems:
        result.status = "partial"
        first = problems[0]
        codes = sorted({str(p.get("code")) for p in first.packages if p.get("code")})
        result.summary = rp.sanitize_summary(
            f"{len(problems)} ref(s) had refused packages; {first.ref}: {', '.join(codes) or first.outcome}",
            secrets_in_play,
        )
    elif not result.refs and previous_status == "partial":
        # Nothing new: a refusal from an earlier poll still stands (that ref is
        # not retried until it moves), so keep flagging it instead of "ok".
        result.status, result.summary = "partial", previous_summary
    await _finish(db, registry_id, owner=owner, status=result.status, summary=result.summary,
                  failed=False, interval=interval, failures=failures)
    return result


async def _reload_actor(db: AsyncSession, user_id: str) -> User:
    return (
        await db.execute(select(User).where(User.id == user_id).execution_options(populate_existing=True))
    ).scalar_one()


async def _record_ref(db: AsyncSession, registry_id: str, ref: RefResult) -> None:
    db.add(
        SkillRegistryPollRef(
            id=f"srp_{secrets.token_hex(12)}",
            registry_id=registry_id,
            ref_name=ref.ref[:255],
            commit_sha=ref.commit,
            outcome=ref.outcome,
            version_ids=json.dumps(ref.version_ids),
            detail=json.dumps(ref.packages[: rp.MAX_PACKAGES_PER_REF], sort_keys=True),
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent poller (expired lease) already recorded this ref.
        await db.rollback()


async def run_poll(
    db: AsyncSession,
    *,
    store: BlobStore,
    limit: int = DEFAULT_BATCH,
    registry_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Poll up to ``limit`` due registries (or exactly ``registry_ids``)."""
    if _RUN_LOCK.locked():
        return {"busy": True, "polled": 0, "results": []}
    async with _RUN_LOCK:
        owner = f"poll_{secrets.token_hex(8)}"
        started = time.monotonic()
        per_registry = rp.poll_timeout_seconds() + rp.PUBLISH_SLACK_SECONDS
        budget = rp.run_budget_seconds()
        if budget < per_registry:
            # A budget smaller than one registry's worst case would never claim
            # anything and polling would stop silently: run one at a time.
            logger.warning(
                "registry poll run budget %ss is below one registry's budget %ss; using %ss",
                budget, per_registry, per_registry,
            )
            budget = per_registry
        now = _now()
        limit = max(1, min(limit, MAX_BATCH))
        force = bool(registry_ids)
        ids = list(dict.fromkeys(registry_ids or []))[:limit] if force else await due_registry_ids(
            db, now=now, limit=limit
        )
        results: list[dict[str, Any]] = []
        budget_exhausted = False
        for registry_id in ids:
            if len(results) >= limit:
                break
            if time.monotonic() - started + per_registry > budget:
                # Stop claiming well before the scheduler gives up on us: a
                # registry claimed now could still be running at the deadline.
                budget_exhausted = True
                break
            if not await claim_registry(db, registry_id, owner=owner, now=_now(), force=force):
                continue
            outcome = await poll_registry(db, registry_id, store=store, owner=owner)
            results.append(outcome.as_dict())
        return {
            "busy": False,
            "polled": len(results),
            "budget_exhausted": budget_exhausted,
            "results": results,
        }

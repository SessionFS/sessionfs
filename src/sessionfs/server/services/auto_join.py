"""Auto-join verified-email users to an org by verified domain.

GitHub #59 (v0.17.0): when a user PROVES ownership of their email (the
emailed verification link, or an admin force-verify), and they are
not yet a member of any org, and exactly one org has that email's domain
in its `auto_join_domains` setting (where the domain is itself verified
via OrgDomainVerification and the org's oidc_sso entitlement is active),
add them as a `member`.

Safety invariants:
  - Only runs on proven email ownership. The signup auto-verify path used
    when email verification is disabled must NOT call this.
  - Best-effort: a failure here NEVER fails sign-up / verification.
  - Never admin/owner — always `member`.
  - Seat cap honored (SELECT FOR UPDATE); full → skipped + audit, no error.
  - Previously removed users are not silently re-added: a member_removed
    audit, or any earlier invite or SSO identity for the org, skips them.
  - >1 matching org → no-op. Verified domains are globally unique
    (uq_org_domain_global_verified), so this is defensive-only, but the
    ambiguity path must still be handled rather than guessing.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    ExternalIdentity,
    OrgAuditEvent,
    OrgDomainVerification,
    OrgIdentityProvider,
    OrgInvite,
    OrgMember,
    Organization,
    User,
)
from sessionfs.server.services.entitlements import resolve_entitlement
from sessionfs.server.tiers import Tier, get_features_for_tier

logger = logging.getLogger("sessionfs.auto_join")


def read_auto_join_domains(org: Organization) -> list[str]:
    """Return the org's normalized auto-join domains (``[]`` when unset)."""
    try:
        raw = (
            json.loads(org.settings)
            if isinstance(org.settings, str)
            else (org.settings or {})
        )
    except (ValueError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        return []
    general = raw.get("general", {}) if isinstance(raw, dict) else {}
    domains = general.get("auto_join_domains")
    if not isinstance(domains, list):
        return []
    return [d for d in domains if isinstance(d, str)]


async def org_has_oidc_sso(org: Organization, db: AsyncSession) -> bool:
    """Whether the org currently has the oidc_sso feature (entitlement-first).

    Auto-join is gated on the org's SSO entitlement; if the org loses it,
    auto-join stops — checked at join time, not at settings-write time.
    """
    tier: str | Tier = org.tier
    ent = await resolve_entitlement("org", org.id, db)
    if ent is not None:
        tier = ent.tier
    if not isinstance(tier, Tier):
        try:
            tier_enum = Tier(tier)
        except ValueError:
            tier_enum = Tier.ENTERPRISE if tier == "admin" else Tier.FREE
    else:
        tier_enum = tier
    return "oidc_sso" in get_features_for_tier(tier_enum)


def _email_domain(email: str) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower()


async def maybe_auto_join(user: User, db: AsyncSession) -> None:
    """Best-effort auto-join after the caller committed the verified email.

    Called AFTER the caller's own ``await db.commit()``; does its own
    commit. Never raises into the caller — a failed join must not fail
    sign-up, email verification, or the admin force-verify path.
    """
    try:
        await _auto_join_impl(user.id, db)
    except Exception:
        logger.exception("auto_join failed for user %s", user.id)
        await db.rollback()


async def _auto_join_impl(user_id: str, db: AsyncSession) -> None:
    # Re-fetch — the caller may have committed and expired non-PK columns.
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    # Deactivated accounts never join: platform deletion deactivates the user
    # and drops memberships without a member_removed audit, and a still-valid
    # verification link must not undo that.
    if user is None or not user.email_verified or not user.is_active:
        return

    domain = _email_domain(user.email)
    if domain is None:
        return

    # Only join users who are not yet a member of ANY org.
    already_member = (
        await db.execute(
            select(OrgMember.org_id).where(OrgMember.user_id == user_id).limit(1)
        )
    ).scalar_one_or_none()
    if already_member is not None:
        return

    # Verified domains are globally unique (uq_org_domain_global_verified),
    # so at most one org can match — but the >1 path is handled anyway.
    verified_org_ids = (
        await db.execute(
            select(OrgDomainVerification.org_id).where(
                OrgDomainVerification.domain == domain,
                OrgDomainVerification.status == "verified",
            )
        )
    ).scalars().all()

    candidates: list[Organization] = []
    for org_id in verified_org_ids:
        org = (
            await db.execute(select(Organization).where(Organization.id == org_id))
        ).scalar_one_or_none()
        if org is not None and domain in read_auto_join_domains(org):
            candidates.append(org)

    if len(candidates) != 1:
        if len(candidates) > 1:
            logger.warning(
                "auto_join ambiguous: %d orgs match domain %s for user %s; skipping",
                len(candidates),
                domain,
                user_id,
            )
        return

    candidate_org_id = candidates[0].id

    # Everything above was a cheap pre-filter on unlocked reads. Now serialize
    # with concurrent changes and decide again on fresh rows: lock the user
    # (deactivation updates it) and then the org (settings changes and other
    # seat-consuming joins lock it), re-reading both.
    user = (
        await db.execute(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None or not user.email_verified or not user.is_active:
        return
    org = (
        await db.execute(
            select(Organization)
            .where(Organization.id == candidate_org_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if org is None or domain not in read_auto_join_domains(org):
        return
    # Lock the verification row too: revoking or deleting the domain must
    # wait until this join commits (or see it already done), never slip in
    # between this check and the insert.
    still_verified = (
        await db.execute(
            select(OrgDomainVerification.id)
            .where(
                OrgDomainVerification.org_id == org.id,
                OrgDomainVerification.domain == domain,
                OrgDomainVerification.status == "verified",
            )
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if still_verified is None:
        return
    if (
        await db.execute(
            select(OrgMember.org_id).where(OrgMember.user_id == user_id).limit(1)
        )
    ).scalar_one_or_none() is not None:
        return

    # Entitlement gate (checked at join time — a lost entitlement stops
    # future joins even if auto_join_domains is still set).
    if not await org_has_oidc_sso(org, db):
        return

    # Previously removed → do not silently re-add. OrgMember rows are
    # hard-deleted, so we key off the member_removed audit event.
    removed = (
        await db.execute(
            select(OrgAuditEvent.id)
            .where(
                OrgAuditEvent.org_id == org.id,
                OrgAuditEvent.event_type == "member_removed",
                OrgAuditEvent.target_id == user_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if removed is not None:
        return

    # Removals made before member_removed audits existed left no tombstone, so
    # also skip anyone with an earlier relationship to this org: any invite
    # (accepted = past member, declined = they said no, pending = an admin
    # already chose how they join) or an SSO identity linked to the org.
    prior_invite = (
        await db.execute(
            select(OrgInvite.id)
            .where(
                OrgInvite.org_id == org.id,
                func.lower(OrgInvite.email) == (user.email or "").strip().lower(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if prior_invite is not None:
        return
    prior_sso = (
        await db.execute(
            select(ExternalIdentity.id)
            .join(OrgIdentityProvider, ExternalIdentity.org_idp_id == OrgIdentityProvider.id)
            .where(
                OrgIdentityProvider.org_id == org.id,
                ExternalIdentity.user_id == user_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if prior_sso is not None:
        return

    members = (
        await db.execute(select(OrgMember.id).where(OrgMember.org_id == org.id))
    ).scalars().all()
    # A NULL seats_limit means unlimited — same rule as SSO JIT provisioning.
    if org.seats_limit is not None and len(members) >= org.seats_limit:
        # Record the skip once per user and org, not on every attempt.
        already_logged = (
            await db.execute(
                select(OrgAuditEvent.id)
                .where(
                    OrgAuditEvent.org_id == org.id,
                    OrgAuditEvent.event_type == "auto_join_skipped_seat_cap",
                    OrgAuditEvent.target_id == user_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if already_logged is not None:
            await db.commit()
            return
        db.add(
            OrgAuditEvent(
                id=f"oae_{secrets.token_hex(12)}",
                org_id=org.id,
                org_name_snapshot=org.name,
                event_type="auto_join_skipped_seat_cap",
                target_type="user",
                target_id=user_id,
                target_email_snapshot=user.email,
                after=json.dumps({"domain": domain}),
            )
        )
        await db.commit()
        return

    db.add(
        OrgMember(
            org_id=org.id,
            user_id=user_id,
            role="member",
            joined_at=datetime.now(timezone.utc),
        )
    )
    db.add(
        OrgAuditEvent(
            id=f"oae_{secrets.token_hex(12)}",
            org_id=org.id,
            org_name_snapshot=org.name,
            event_type="member_auto_joined",
            target_type="user",
            target_id=user_id,
            target_email_snapshot=user.email,
            after=json.dumps({"domain": domain, "role": "member"}),
        )
    )
    await db.commit()

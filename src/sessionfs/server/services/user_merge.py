"""Audited merge of a duplicate user account into another (platform admin).

`merge_users` computes the plan for folding SOURCE into TARGET and, when
``apply`` is true, executes it. The caller (routes/admin.py) owns the
transaction, the scope locks and the audit row; nothing here commits.

Every table that references a user falls in one of five groups. The
decisions are deliberate and mirrored in the endpoint docstring.

REASSIGNED (ownership and authorship move to the target): see
``REASSIGN_COLUMNS``, plus ``agent_personas.represented_user_id``.

DEDUPED (a unique key means one side must win; the rule is explicit):
  org_members          higher role wins (owner > admin > member), the target
                       wins ties; sole-owner invariant kept
  team_members         the target's row wins, the source's is deleted
  sync_watchlist       one row per (user, session); the target's wins
  sessions.alias       unique per user; a source alias the target already
                       uses is cleared on the moved session (ids listed)
  skill_devices        the target's row wins, the source's is deleted
  gitlab_settings      the target's row wins, the source's is deleted
  user_judge_settings  the target's row wins, the source's is deleted
  github_installations refuse (409) when both users have one
  entitlements         refuse (409) when both hold an active one

REVOKED / DEACTIVATED (access, not data): api_keys (including service
keys created under the source), trusted_reviewers bindings,
sso_break_glass_grants, external_identities, pending org owner transfers
and pending project transfers involving the source.

REFUSED (409): the source owns skill-marketplace assets (packages,
publishers, listings, registries) or holds Stripe billing pointers; those
need a deliberate manual decision.

LEFT ALONE (history keeps the original actor): see ``LEFT_ALONE``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentPersona,
    Base,
    Organization,
    OrgAuditEvent,
    OrgMember,
    User,
)
from sessionfs.server.roles import ROLE_LEVEL, OrgRole

MAX_IDS = 200

# (table, column): rows where column == source are reassigned to the target.
REASSIGN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("sessions", "user_id"),
    ("projects", "owner_id"),
    ("share_links", "user_id"),
    ("bookmark_folders", "user_id"),
    ("bookmarks", "user_id"),
    ("audit_reports", "user_id"),
    ("project_rules", "created_by"),
    ("rules_versions", "compiled_by"),
    ("handoffs", "sender_id"),
    ("handoffs", "recipient_id"),
    ("handoffs", "recipient_user_id"),
    ("handoff_comments", "author_user_id"),
    ("knowledge_entries", "user_id"),
    ("context_compilations", "user_id"),
    ("agent_personas", "created_by"),
    ("tickets", "created_by_user_id"),
    ("tickets", "resolver_user_id"),
    ("ticket_comments", "author_user_id"),
    ("ticket_edits", "edited_by_user_id"),
    ("ticket_notifications", "user_id"),
    ("wiki_page_revisions", "user_id"),
    ("work_queues", "created_by_user_id"),
    ("work_queue_items", "implementer_user_id"),
    ("org_invites", "invited_by"),
    ("pr_comments", "owner_user_id"),
)

# (table, key column or None): one row per (user, key); the target's wins.
DEDUPE_TABLES: tuple[tuple[str, str | None], ...] = (
    ("team_members", "team_id"),
    ("sync_watchlist", "session_id"),
    ("skill_devices", "device_key"),
    ("gitlab_settings", None),
    ("user_judge_settings", None),
    ("ticket_watchers", "ticket_id"),
    ("ticket_watch_cursors", "persona_name"),
    ("user_notification_settings", None),
)

# Tables referencing users that are deliberately not touched.
LEFT_ALONE: tuple[str, ...] = (
    "admin_actions", "org_audit_events", "activation_attempt", "agent_runs",
    "agent_feedback", "billing_checkout_intents", "billing_reconciliations",
    "handoff_events", "retrieval_audit_contexts", "retrieval_audit_events",
    "project_merge_audit", "project_repos", "project_transfers (history)",
    "org_owner_transfer (history)", "org_members.invited_by",
    "org_domain_verifications", "org_identity_providers", "org_skill_policies",
    "org_skill_assignments", "skill_install_requests", "skill_consent_receipts",
    "skill_package_reports", "skill_package_versions", "skill_blocked_content_hashes",
    "skill_revocation_events", "skill_reserved_names", "skill_trusted_publishers",
    "skill_publisher_verifications", "skill_verification_jobs",
    "residents", "trusted_reviewers.created_by_user_id", "teams.created_by",
    "sso_exchange_codes", "github_claim_attempts", "sessions.deleted_by",
    "knowledge_entries.dismissed_by", "work_queue_items.closed_by_user_id",
    "api_keys.created_by_user_id",
)

# Source-owned marketplace assets block the merge.
_MARKETPLACE_OWNERSHIP: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("skill_packages", "owner_id", {"owner_type": "user"}),
    ("skill_publishers", "user_id", {}),
    ("skill_marketplace_listings", "owner_user_id", {}),
    ("skill_marketplace_registries", "owner_id", {"owner_type": "user"}),
)


def _tbl(name: str):
    return Base.metadata.tables[name]


def _fail(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, **extra})


def _entry(ids: list[str]) -> dict:
    return {"count": len(ids), "ids": ids[:MAX_IDS], "truncated": len(ids) > MAX_IDS}


async def _ids(db: AsyncSession, table, *where) -> list[str]:
    pk = list(table.primary_key.columns)
    rows = (await db.execute(select(*pk).where(*where).order_by(*pk))).all()
    return [":".join(str(v) for v in row) for row in rows]


def _role_level(role: str) -> int:
    try:
        return ROLE_LEVEL[OrgRole(role)]
    except ValueError:
        return 0


def _org_event(
    org: Organization | None, org_id: str, admin: User, event_type: str,
    target_type: str, target_id: str, before: dict, after: dict,
) -> OrgAuditEvent:
    return OrgAuditEvent(
        id=f"oae_{uuid.uuid4().hex[:24]}",
        org_id=org_id,
        org_name_snapshot=(org.name if org else "") or "",
        event_type=event_type,
        actor_user_id=admin.id,
        actor_email_snapshot=admin.email,
        actor_role_at_time="platform_admin",
        target_type=target_type,
        target_id=target_id,
        before=json.dumps(before, sort_keys=True),
        after=json.dumps(after, sort_keys=True),
    )


async def preflight_refusals(
    db: AsyncSession, source: User, target: User
) -> dict[str, Any]:
    """Raise the 409s that depend on rows (call under the locks). Returns
    facts the plan needs."""
    if not target.is_active:
        raise _fail(409, "target_inactive", "The target user is deactivated; merge into an active account.")
    if source.tier == "admin":
        raise _fail(409, "source_is_platform_admin", "The source user is a platform admin and cannot be merged away.")

    if source.stripe_customer_id or source.stripe_subscription_id or source.stripe_pending_subscription_id:
        raise _fail(
            409, "source_has_billing",
            "The source user has Stripe billing pointers. Cancel or move the "
            "subscription in Stripe and clear the pointers first; nothing was changed.",
        )

    blockers: dict[str, int] = {}
    for table_name, column, extra in _MARKETPLACE_OWNERSHIP:
        t = _tbl(table_name)
        conds = [t.c[column] == source.id] + [t.c[k] == v for k, v in extra.items()]
        n = (await db.execute(select(func.count()).select_from(t).where(*conds))).scalar() or 0
        if n:
            blockers[table_name] = n
    if blockers:
        raise _fail(
            409, "marketplace_assets_owned",
            "The source user owns skill marketplace assets; transfer or retire "
            "them first. Nothing was changed.",
            tables=blockers,
        )

    gi = _tbl("github_installations")
    src_gi = await _ids(db, gi, gi.c.user_id == source.id)
    tgt_gi = await _ids(db, gi, gi.c.user_id == target.id)
    if src_gi and tgt_gi:
        raise _fail(
            409, "github_installation_conflict",
            "Both users have a GitHub installation linked; remove one first.",
            source_installations=src_gi, target_installations=tgt_gi,
        )

    ent = _tbl("entitlements")
    active = ent.c.status == "active"
    src_active = await _ids(db, ent, ent.c.owner_type == "user", ent.c.owner_id == source.id, active)
    tgt_active = await _ids(db, ent, ent.c.owner_type == "user", ent.c.owner_id == target.id, active)
    if src_active and tgt_active:
        raise _fail(
            409, "entitlement_conflict",
            "Both users hold an active entitlement. Cancel or expire one of "
            "them first; the merge will not guess which plan to keep.",
            source_entitlements=src_active, target_entitlements=tgt_active,
        )
    return {"source_active_entitlements": src_active, "target_active_entitlements": tgt_active}


async def merge_users(
    db: AsyncSession,
    *,
    admin: User,
    source: User,
    target: User,
    apply: bool,
) -> dict[str, Any]:
    """Plan (and with ``apply``, perform) the merge. Must run under the
    scope locks and billing row locks taken by the route. Does not commit."""
    facts = await preflight_refusals(db, source, target)
    now = datetime.now(timezone.utc)
    sid, tid = source.id, target.id
    plan: dict[str, Any] = {
        "reassigned": {}, "deduped": {}, "revoked": {}, "cancelled": {},
        "org_members": [], "user": {}, "left_alone": list(LEFT_ALONE),
    }

    # --- org memberships ---------------------------------------------------
    src_members = (
        await db.execute(select(OrgMember).where(OrgMember.user_id == sid).order_by(OrgMember.org_id))
    ).scalars().all()
    tgt_members = {
        m.org_id: m
        for m in (await db.execute(select(OrgMember).where(OrgMember.user_id == tid))).scalars().all()
    }
    orgs = {
        o.id: o
        for o in (
            await db.execute(
                select(Organization).where(Organization.id.in_([m.org_id for m in src_members] or [""]))
            )
        ).scalars().all()
    }
    after_orgs = set(tgt_members) | {m.org_id for m in src_members}

    # Org-project owners must stay members of their org.
    proj = _tbl("projects")
    stranded = (
        await db.execute(
            select(proj.c.id, proj.c.org_id).where(
                proj.c.owner_id == sid, proj.c.org_id.is_not(None)
            )
        )
    ).all()
    bad = [pid for pid, oid in stranded if oid not in after_orgs]
    if bad:
        raise _fail(
            409, "project_owner_not_member",
            "The source owns org projects in orgs where neither user is a "
            "member; the target would own them without membership. Transfer "
            "those projects first.",
            project_ids=sorted(bad),
        )

    for m in src_members:
        existing = tgt_members.get(m.org_id)
        if existing is None:
            plan["org_members"].append({
                "org_id": m.org_id, "action": "moved",
                "source_role": m.role, "target_role": None, "resulting_role": m.role,
            })
            if apply:
                m.user_id = tid
                await db.flush()
        else:
            winner = m.role if _role_level(m.role) > _role_level(existing.role) else existing.role
            plan["org_members"].append({
                "org_id": m.org_id, "action": "merged",
                "source_role": m.role, "target_role": existing.role, "resulting_role": winner,
            })
            if apply:
                # Delete the source row first so the one-owner index never
                # sees two owners, then raise the target if the source's
                # role was higher.
                await db.delete(m)
                await db.flush()
                if winner != existing.role:
                    existing.role = winner
                    await db.flush()
        if apply:
            db.add(_org_event(
                orgs.get(m.org_id), m.org_id, admin, "user_merge_membership", "user", tid,
                {"source_user_id": sid, "source_role": m.role,
                 "target_role": existing.role if existing else None},
                {"target_user_id": tid, "role": plan["org_members"][-1]["resulting_role"]},
            ))

    # Aliases are unique per user (idx_sessions_alias, a migration-only
    # index). Clear source aliases the target already uses so the move
    # below cannot collide; the session itself still moves.
    sess = _tbl("sessions")
    tgt_aliases = select(sess.c.alias).where(sess.c.user_id == tid, sess.c.alias.is_not(None))
    alias_clash = await _ids(db, sess, sess.c.user_id == sid, sess.c.alias.in_(tgt_aliases))
    if alias_clash:
        plan["deduped"]["sessions.alias"] = {"cleared": _entry(alias_clash)}
        if apply:
            await db.execute(
                update(sess).where(sess.c.user_id == sid, sess.c.alias.in_(tgt_aliases))
                .values(alias=None)
            )

    # --- simple reassignments ---------------------------------------------
    for table_name, column in REASSIGN_COLUMNS:
        t = _tbl(table_name)
        ids = await _ids(db, t, t.c[column] == sid)
        if ids:
            plan["reassigned"][f"{table_name}.{column}"] = _entry(ids)
            if apply:
                await db.execute(update(t).where(t.c[column] == sid).values({column: tid}))

    # Org personas representing the source: represent the target (a member
    # of the same org after the membership merge), with an audit row each.
    reps = (
        await db.execute(
            select(AgentPersona).where(AgentPersona.represented_user_id == sid)
            .order_by(AgentPersona.id)
        )
    ).scalars().all()
    if reps:
        plan["reassigned"]["agent_personas.represented_user_id"] = _entry([p.id for p in reps])
        if apply:
            for p in reps:
                p.represented_user_id = tid
                org = orgs.get(p.org_id) if p.org_id else None
                db.add(_org_event(
                    org, p.org_id or "", admin, "persona_represented_user_changed", "persona", p.id,
                    {"name": p.name, "represented_user_id": sid},
                    {"name": p.name, "represented_user_id": tid, "reason": "user_merged"},
                ))
            await db.flush()

    # --- deduped tables -----------------------------------------------------
    for table_name, key in DEDUPE_TABLES:
        t = _tbl(table_name)
        src_rows = (
            await db.execute(select(*([t.c[key]] if key else []), *t.primary_key.columns)
                             .where(t.c.user_id == sid))
        ).all()
        if not src_rows:
            continue
        if key:
            tgt_keys = set((await db.execute(select(t.c[key]).where(t.c.user_id == tid))).scalars().all())
            conflict = [r for r in src_rows if r[0] in tgt_keys]
            move = [r for r in src_rows if r[0] not in tgt_keys]
            pk_off = 1
        else:
            has_target = (await db.execute(select(func.count()).select_from(t).where(t.c.user_id == tid))).scalar()
            conflict, move, pk_off = (list(src_rows), [], 0) if has_target else ([], list(src_rows), 0)

        def _rid(r, off=pk_off) -> str:
            return ":".join(str(v) for v in r[off:])

        info = {"moved": _entry([_rid(r) for r in move]), "dropped_target_wins": _entry([_rid(r) for r in conflict])}
        plan["deduped"][table_name] = info
        if apply:
            if conflict:
                if key:
                    await db.execute(delete(t).where(t.c.user_id == sid, t.c[key].in_([r[0] for r in conflict])))
                else:
                    await db.execute(delete(t).where(t.c.user_id == sid))
            if move:
                await db.execute(update(t).where(t.c.user_id == sid).values(user_id=tid))

    gi = _tbl("github_installations")
    gi_ids = await _ids(db, gi, gi.c.user_id == sid)
    if gi_ids:
        plan["reassigned"]["github_installations.user_id"] = _entry(gi_ids)
        if apply:
            await db.execute(update(gi).where(gi.c.user_id == sid).values(user_id=tid))

    # --- entitlements ------------------------------------------------------
    ent = _tbl("entitlements")
    ent_ids = await _ids(db, ent, ent.c.owner_type == "user", ent.c.owner_id == sid)
    if ent_ids:
        plan["reassigned"]["entitlements.owner_id"] = _entry(ent_ids)
        if apply:
            await db.execute(
                update(ent).where(ent.c.owner_type == "user", ent.c.owner_id == sid)
                .values(owner_id=tid)
            )

    # --- revocations --------------------------------------------------------
    ak = _tbl("api_keys")
    key_ids = await _ids(db, ak, ak.c.user_id == sid, ak.c.is_active.is_(True))
    if key_ids:
        plan["revoked"]["api_keys"] = _entry(key_ids)
        if apply:
            await db.execute(
                update(ak).where(ak.c.user_id == sid, ak.c.is_active.is_(True))
                .values(is_active=False, revoked_at=now, revoke_reason="user_merged")
            )
    tr = _tbl("trusted_reviewers")
    tr_ids = await _ids(db, tr, tr.c.user_id == sid, tr.c.is_active.is_(True))
    if tr_ids:
        plan["revoked"]["trusted_reviewers"] = _entry(tr_ids)
        if apply:
            await db.execute(
                update(tr).where(tr.c.user_id == sid, tr.c.is_active.is_(True))
                .values(is_active=False, revoked_at=now)
            )
    bg = _tbl("sso_break_glass_grants")
    bg_ids = await _ids(db, bg, bg.c.admin_user_id == sid, bg.c.revoked_at.is_(None))
    if bg_ids:
        plan["revoked"]["sso_break_glass_grants"] = _entry(bg_ids)
        if apply:
            await db.execute(
                update(bg).where(bg.c.admin_user_id == sid, bg.c.revoked_at.is_(None))
                .values(revoked_at=now)
            )
    ei = _tbl("external_identities")
    ei_ids = await _ids(db, ei, ei.c.user_id == sid, ei.c.deactivated_at.is_(None))
    if ei_ids:
        plan["revoked"]["external_identities"] = _entry(ei_ids)
        if apply:
            await db.execute(
                update(ei).where(ei.c.user_id == sid, ei.c.deactivated_at.is_(None))
                .values(deactivated_at=now)
            )

    ot = _tbl("org_owner_transfer")
    ot_ids = await _ids(
        db, ot, ot.c.status == "pending",
        (ot.c.from_user_id == sid) | (ot.c.to_user_id == sid),
    )
    if ot_ids:
        plan["cancelled"]["org_owner_transfer"] = _entry(ot_ids)
        if apply:
            await db.execute(
                update(ot).where(ot.c.id.in_(ot_ids), ot.c.status == "pending")
                .values(status="cancelled")
            )
    pt = _tbl("project_transfers")
    pt_ids = await _ids(
        db, pt, pt.c.state == "pending",
        (pt.c.initiated_by == sid) | (pt.c.target_user_id == sid),
    )
    if pt_ids:
        plan["cancelled"]["project_transfers"] = _entry(pt_ids)
        if apply:
            await db.execute(
                update(pt).where(pt.c.id.in_(pt_ids), pt.c.state == "pending")
                .values(state="cancelled", updated_at=now)
            )

    # --- the user rows --------------------------------------------------------
    moved_entitlement = bool(facts["source_active_entitlements"])
    user_plan = {
        "source_deactivated": True,
        "storage_used_bytes_added_to_target": source.storage_used_bytes or 0,
        "target_inherits_source_entitlement": moved_entitlement,
    }
    plan["user"] = user_plan
    if apply:
        target.storage_used_bytes = (target.storage_used_bytes or 0) + (source.storage_used_bytes or 0)
        source.storage_used_bytes = 0
        source.is_active = False
        if source.entitlement_id and target.entitlement_id is None:
            target.entitlement_id = source.entitlement_id
        source.entitlement_id = None
        if moved_entitlement and target.tier != "admin":
            target.tier = source.tier
        await db.flush()

    return plan


def trim_state(state: dict, limit: int) -> dict:
    """Drop id lists (keep counts) when the serialized state would exceed
    ``limit`` bytes, so the audit row always fits."""
    if len(json.dumps(state, sort_keys=True, default=str).encode()) <= limit:
        return state

    def strip(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: ([] if k == "ids" else strip(v)) for k, v in o.items()} | (
                {"truncated": True} if "ids" in o else {}
            )
        if isinstance(o, list):
            return [strip(v) for v in o]
        return o

    return strip(state)


__all__ = ["merge_users", "preflight_refusals", "trim_state"]

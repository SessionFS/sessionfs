"""Shared fixtures for the approval-policy tests (GitHub #123)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    ApprovalPolicy,
    Organization,
    OrgMember,
    Project,
    Ticket,
    TicketComment,
    User,
    WorkQueue,
    WorkQueueItem,
)

HEAD = "0123456789abcdef0123456789abcdef01234567"
NEXT = "89abcdef0123456789abcdef0123456789abcdef"
CLEAN = "Codex R1 review on tk_x: VERIFIED-CLEAN\n\nFindings: none."


def hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def make_user(db: AsyncSession, name: str) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.flush()
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{name}-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


async def make_service_key(
    db: AsyncSession, minter: User, org: Organization, scopes: list[str]
) -> tuple[ApiKey, str]:
    raw = generate_api_key()
    key = ApiKey(
        id=f"sk_{uuid.uuid4().hex[:24]}",
        user_id=minter.id,
        created_by_user_id=minter.id,
        key_hash=hash_api_key(raw),
        name=f"svc-{uuid.uuid4().hex[:6]}",
        key_kind="service",
        org_id=org.id,
        scopes=json.dumps(scopes),
        service_key_name="svc",
        created_at=datetime.now(timezone.utc),
    )
    db.add(key)
    await db.commit()
    return key, raw


async def make_org(db: AsyncSession, tier: str = "enterprise") -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Acme",
        slug=f"acme-{uuid.uuid4().hex[:8]}",
        tier=tier,
    )
    db.add(org)
    await db.commit()
    return org


async def add_member(db: AsyncSession, org: Organization, user: User, role: str) -> None:
    db.add(OrgMember(org_id=org.id, user_id=user.id, role=role))
    await db.commit()


async def make_project(db: AsyncSession, owner: User, org: Organization | None) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"p-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/r-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
    )
    db.add(project)
    await db.commit()
    return project


async def make_ticket(
    db: AsyncSession, project: Project, creator: User, *, priority: str = "medium"
) -> Ticket:
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="Auth change",
        priority=priority,
        created_by_user_id=creator.id,
    )
    db.add(ticket)
    await db.commit()
    return ticket


async def seed_implementer(
    db: AsyncSession,
    project: Project,
    ticket: Ticket,
    *,
    user_id: str | None,
    service_key_id: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    queue = WorkQueue(
        id=f"wq_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name=f"q-{uuid.uuid4().hex[:6]}",
        mode="implement_until_done",
        selector="{}",
        created_by_user_id=project.owner_id,
        created_at=now,
        updated_at=now,
    )
    db.add(queue)
    await db.flush()
    db.add(
        WorkQueueItem(
            id=f"wqi_{uuid.uuid4().hex[:16]}",
            work_queue_id=queue.id,
            ticket_id=ticket.id,
            item_status="active",
            implementer_user_id=user_id,
            implementer_service_key_id=service_key_id,
            created_at=now,
            updated_at=now,
        )
    )
    await db.commit()


async def seed_clean(
    db: AsyncSession, ticket: Ticket, reviewer: User, sha: str, content: str = CLEAN
) -> None:
    db.add(
        TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket.id,
            author_user_id=reviewer.id,
            author_persona="codex-reviewer",
            content=content,
            verdict_trusted=True,
            reviewed_commit=sha,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def make_policy(
    db: AsyncSession,
    org: Organization,
    *,
    name: str = "Auth changes",
    requirements: list[dict] | None = None,
    flt: dict | None = None,
    blocking: bool = True,
    target_kind: str = "merge",
) -> ApprovalPolicy:
    pid = f"ap_{uuid.uuid4().hex[:24]}"
    policy = ApprovalPolicy(
        id=pid,
        org_id=org.id,
        lineage_id=pid,
        name=name,
        target_kind=target_kind,
        filter=json.dumps(flt or {}),
        requirements=json.dumps(
            requirements or [{"who": "org_role", "role": "admin", "count": 1}]
        ),
        blocking=blocking,
    )
    db.add(policy)
    await db.commit()
    return policy


class World:
    """An Enterprise org with an owner, admins, a member implementer, a
    reviewer, a project and a ticket that is trusted-clean at HEAD."""

    org: Organization
    owner: User
    owner_key: str
    admin: User
    admin_key: str
    admin2: User
    admin2_key: str
    implementer: User
    implementer_key: str
    reviewer: User
    reviewer_key: str
    outsider: User
    outsider_key: str
    project: Project
    ticket: Ticket


async def make_world(db: AsyncSession, tier: str = "enterprise") -> World:
    w = World()
    w.org = await make_org(db, tier)
    w.owner, w.owner_key = await make_user(db, "owner")
    w.admin, w.admin_key = await make_user(db, "admin")
    w.admin2, w.admin2_key = await make_user(db, "admintwo")
    w.implementer, w.implementer_key = await make_user(db, "impl")
    w.reviewer, w.reviewer_key = await make_user(db, "reviewer")
    w.outsider, w.outsider_key = await make_user(db, "outsider")
    await add_member(db, w.org, w.owner, "owner")
    await add_member(db, w.org, w.admin, "admin")
    await add_member(db, w.org, w.admin2, "admin")
    await add_member(db, w.org, w.implementer, "member")
    await add_member(db, w.org, w.reviewer, "member")
    w.project = await make_project(db, w.owner, w.org)
    w.ticket = await make_ticket(db, w.project, w.implementer)
    await seed_implementer(db, w.project, w.ticket, user_id=w.implementer.id)
    await seed_clean(db, w.ticket, w.reviewer, HEAD)
    return w


async def raised_readiness(db: AsyncSession, ticket: Ticket, sha: str, **kw) -> dict:
    """A trigger then a read: raise the ticket's approval requests (what a
    verdict, a decision, a policy recheck or PR ingest does), then read
    readiness. Readiness reads alone never raise a request."""
    from sessionfs.server.services import approvals

    await approvals.raise_ticket_approvals(db, ticket)
    await db.commit()
    return await approvals.ticket_readiness(db, ticket, sha, **kw)


async def trigger_merge_check(client, w) -> None:
    """The merge-check trigger route (a write path that raises requests)."""
    resp = await client.post(
        f"/api/v1/projects/{w.project.id}/tickets/{w.ticket.id}/merge-check",
        headers=hdrs(w.owner_key),
    )
    assert resp.status_code == 200, resp.text

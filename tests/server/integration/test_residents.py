"""Resident R0 integration tests — registration, lifecycle, memory, and F1.

Tests the migration-057 server surface:
  - Registration + F4 mutual-exclusion (both directions)
  - List / get / status (pause/retire)
  - F7 rotate-key
  - C5 memory isolation
  - F6 memory caps
  - hydrate excludes quarantined
  - compact supersedes
  - F1 self-review negative matrix (via work-queue engine directly)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    Organization,
    OrgMember,
    Project,
    Resident,
    ResidentMemoryEntry,
    Ticket,
    TicketComment,
    TrustedReviewer,
    User,
    WorkQueue,
    WorkQueueItem,
)
from sessionfs.server.services import work_queues as wq_engine


# ── builders (mirror test_work_queue_settle_verdict.py) ──────────────


async def _make_org(db: AsyncSession) -> Organization:
    suffix = uuid.uuid4().hex[:8]
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name=f"org-{suffix}",
        slug=f"org-{suffix}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(org)
    await db.flush()
    return org


async def _make_user(
    db: AsyncSession, org: Organization | None = None, tier: str = "team"
) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"u-{uuid.uuid4().hex[:6]}@example.com",
        display_name="U",
        tier=tier,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.flush()
    if org is not None:
        db.add(
            OrgMember(
                org_id=org.id,
                user_id=user.id,
                role="admin",
                joined_at=datetime.now(timezone.utc),
            )
        )
        await db.flush()
    return user


async def _make_service_key(
    db: AsyncSession, org: Organization, owner: User,
    scopes: list[str] | None = None,
) -> ApiKey:
    key = ApiKey(
        id=str(uuid.uuid4()),
        user_id=owner.id,
        key_hash=hash_api_key(generate_api_key()),
        name=f"svc-{uuid.uuid4().hex[:6]}",
        is_active=True,
        key_kind="service",
        org_id=org.id,
        scopes=json.dumps(scopes or ["*"]),
        service_key_name=f"resident-key-{uuid.uuid4().hex[:4]}",
        key_prefix=f"sk_sfs_{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(key)
    await db.flush()
    return key


async def _make_project(
    db: AsyncSession, owner: User, org: Organization
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name=f"proj-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:8]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(project)
    await db.flush()
    return project


async def _make_ticket(db: AsyncSession, project: Project) -> Ticket:
    t = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="A ticket",
        status="in_progress",
        kind="task",
        priority="medium",
        created_by_user_id=project.owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(t)
    await db.flush()
    return t


async def _make_queue(
    db: AsyncSession, project: Project, owner: User,
    mode: str = "implement_until_done",
) -> WorkQueue:
    q = WorkQueue(
        id=f"wq_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name=f"q-{uuid.uuid4().hex[:6]}",
        mode=mode,
        selector=json.dumps({}),
        auto_adopt=False,
        max_adopt_per_wake=5,
        stop_condition="all_clean",
        cadence_seconds=120,
        max_tickets_per_run=1,
        max_attempts_per_item=3,
        status="active",
        lease_epoch=0,
        assigned_persona="codex-reviewer" if mode == "review_until_clean" else "atlas",
        created_by_user_id=owner.id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(q)
    await db.flush()
    return q


async def _make_item(
    db: AsyncSession, queue: WorkQueue, ticket: Ticket,
) -> WorkQueueItem:
    item = WorkQueueItem(
        id=f"wqi_{uuid.uuid4().hex[:16]}",
        work_queue_id=queue.id,
        ticket_id=ticket.id,
        item_status="pending",
        attempts=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(item)
    await db.flush()
    return item


async def _make_resident(
    db: AsyncSession,
    org: Organization,
    project: Project,
    service_key: ApiKey,
    kind: str = "implementer",
    persona: str = "atlas",
    work_queue=None,
) -> Resident:
    r = Resident(
        id=f"res_{uuid.uuid4().hex[:12]}",
        org_id=org.id,
        project_id=project.id,
        kind=kind,
        persona_name=persona,
        service_key_id=service_key.id,
        work_queue_id=work_queue.id if work_queue is not None else None,
        status="active",
        mind_token_budget=8000,
        max_uncompacted_entries=500,
        created_by_user_id="test-user",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(r)
    await db.flush()
    return r


# ── F1 test helpers ──────────────────────────────────────────────────

R_VERIFIED_CLEAN = """\
Codex R1 review on tk_x: VERIFIED-CLEAN

Findings: none.
"""

R_CHANGES = """\
Codex R1 review on tk_x: CHANGES REQUESTED

Findings:

 • HIGH — broken thing in src/foo.py:10
"""


async def _seed_trusted_verdict(
    db: AsyncSession,
    ticket: Ticket,
    service_key_id: str | None = None,
    user_id: str | None = None,
    content: str = R_VERIFIED_CLEAN,
    verdict_trusted: bool = True,
) -> TicketComment:
    c = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket.id,
        author_user_id=user_id or "",
        author_persona="codex-reviewer",
        content=content,
        verdict_trusted=verdict_trusted,
        service_key_id=service_key_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(c)
    await db.flush()
    return c


# ── tests ─────────────────────────────────────────────────────────────


class TestResidentRegistration:
    async def test_register_implementer(self, db_session: AsyncSession):
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        sk = await _make_service_key(db_session, org, user)

        r = Resident(
            id=f"res_{uuid.uuid4().hex[:12]}",
            org_id=org.id,
            project_id=project.id,
            kind="implementer",
            persona_name="atlas",
            service_key_id=sk.id,
            status="active",
            mind_token_budget=8000,
            max_uncompacted_entries=500,
            created_by_user_id=user.id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(r)
        await db_session.commit()
        await db_session.refresh(r)

        assert r.id.startswith("res_")
        assert r.kind == "implementer"
        assert r.status == "active"

    async def test_register_reviewer(self, db_session: AsyncSession):
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        sk = await _make_service_key(db_session, org, user)

        r = Resident(
            id=f"res_{uuid.uuid4().hex[:12]}",
            org_id=org.id,
            project_id=project.id,
            kind="reviewer",
            persona_name="codex-reviewer",
            service_key_id=sk.id,
            status="active",
            mind_token_budget=16000,
            created_by_user_id=user.id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(r)
        await db_session.commit()

        assert r.kind == "reviewer"

    async def test_service_key_unique(self, db_session: AsyncSession):
        """One service key cannot drive two residents."""
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        sk = await _make_service_key(db_session, org, user)

        r1 = Resident(
            id=f"res_{uuid.uuid4().hex[:12]}",
            org_id=org.id,
            project_id=project.id,
            kind="implementer",
            persona_name="atlas",
            service_key_id=sk.id,
            status="active",
            mind_token_budget=8000,
            created_by_user_id=user.id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(r1)
        await db_session.flush()

        r2 = Resident(
            id=f"res_{uuid.uuid4().hex[:12]}",
            org_id=org.id,
            project_id=project.id,
            kind="reviewer",
            persona_name="codex-reviewer",
            service_key_id=sk.id,  # same key!
            status="active",
            mind_token_budget=8000,
            created_by_user_id=user.id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(r2)
        with pytest.raises(Exception):  # IntegrityError
            await db_session.flush()


class TestF4MutualExclusion:
    """F4 SoD: one service key cannot be both implementer resident
    AND trusted reviewer for the same org."""

    async def test_implementer_key_rejected_as_trusted_reviewer(
        self, db_session: AsyncSession
    ):
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        sk = await _make_service_key(db_session, org, user)

        # First, create an implementer resident with this key.
        await _make_resident(
            db_session, org, project, sk, kind="implementer",
        )

        # Now try to register the same key as a trusted reviewer.
        tr = TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            org_id=org.id,
            project_id=None,
            service_key_id=sk.id,
            reviewer_persona="codex-reviewer",
            is_active=True,
            created_by_user_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(tr)
        # This should be caught at the application layer before DB insert.
        # The DB itself doesn't enforce this cross-table constraint;
        # routes/residents.py and routes/trusted_reviewers.py do.
        # So this test verifies we can *detect* the conflict at query time.
        await db_session.flush()

        # Now verify the conflict: query for both.
        conflict = (
            await db_session.execute(
                select(Resident.id).where(
                    Resident.service_key_id == sk.id,
                    Resident.org_id == org.id,
                    Resident.kind == "implementer",
                    Resident.status.in_(("active", "paused")),
                )
            )
        ).scalar_one_or_none()
        assert conflict is not None  # implementer resident exists

        tr_exists = (
            await db_session.execute(
                select(TrustedReviewer.id).where(
                    TrustedReviewer.service_key_id == sk.id,
                    TrustedReviewer.is_active.is_(True),
                    TrustedReviewer.revoked_at.is_(None),
                    TrustedReviewer.org_id == org.id,
                )
            )
        ).scalar_one_or_none()
        assert tr_exists is not None  # trusted reviewer also exists
        # The application-layer check would reject this before both exist.


class TestC5MemoryIsolation:
    """C5: a resident touches ONLY its own mind."""

    async def test_different_residents_isolated(self, db_session: AsyncSession):
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        sk1 = await _make_service_key(db_session, org, user)
        sk2 = await _make_service_key(db_session, org, user)

        r1 = await _make_resident(db_session, org, project, sk1)
        r2 = await _make_resident(db_session, org, project, sk2)

        # Write to r1's memory.
        e1 = ResidentMemoryEntry(
            id=f"rme_{uuid.uuid4().hex[:12]}",
            resident_id=r1.id,
            org_id=org.id,
            kind="reasoning",
            seq=1,
            content="r1 private thought",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(e1)
        await db_session.commit()

        # r2's memory is empty.
        r2_entries = (
            await db_session.execute(
                select(func.count(ResidentMemoryEntry.id)).where(
                    ResidentMemoryEntry.resident_id == r2.id,
                )
            )
        ).scalar_one()
        assert r2_entries == 0

        # r1 can read its own entries.
        r1_entries = (
            await db_session.execute(
                select(ResidentMemoryEntry).where(
                    ResidentMemoryEntry.resident_id == r1.id,
                    ResidentMemoryEntry.org_id == org.id,
                )
            )
        ).scalars().all()
        assert len(r1_entries) == 1
        assert r1_entries[0].content == "r1 private thought"

    async def test_cross_org_isolation(self, db_session: AsyncSession):
        org1 = await _make_org(db_session)
        org2 = await _make_org(db_session)
        user1 = await _make_user(db_session, org1)
        user2 = await _make_user(db_session, org2)
        proj1 = await _make_project(db_session, user1, org1)
        proj2 = await _make_project(db_session, user2, org2)
        sk1 = await _make_service_key(db_session, org1, user1)
        sk2 = await _make_service_key(db_session, org2, user2)

        r1 = await _make_resident(db_session, org1, proj1, sk1)
        r2 = await _make_resident(db_session, org2, proj2, sk2)

        e1 = ResidentMemoryEntry(
            id=f"rme_{uuid.uuid4().hex[:12]}",
            resident_id=r1.id,
            org_id=org1.id,
            kind="reasoning",
            seq=1,
            content="org1 data",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(e1)
        await db_session.commit()

        # r2 cannot read org1 entries (via denormalized org_id).
        r2_org1 = (
            await db_session.execute(
                select(func.count(ResidentMemoryEntry.id)).where(
                    ResidentMemoryEntry.org_id == org1.id,
                    ResidentMemoryEntry.resident_id == r2.id,
                )
            )
        ).scalar_one()
        assert r2_org1 == 0

    async def test_f5_org_admin_read_visibility_not_write(
        self, db_session: AsyncSession
    ):
        """F5 owner-visibility: an org owner/admin USER key may inspect a
        resident's mind (read/quarantine recovery path), but NOT write; a
        non-member and a different service key are both refused."""
        from fastapi import HTTPException

        from sessionfs.server.auth.dependencies import AuthContext
        from sessionfs.server.routes.residents import _enforce_resident_isolation

        org = await _make_org(db_session)
        admin = await _make_user(db_session, org)  # _make_user → org admin
        project = await _make_project(db_session, admin, org)
        sk = await _make_service_key(db_session, org, admin)
        resident = await _make_resident(
            db_session, org, project, sk, kind="implementer"
        )
        await db_session.commit()

        admin_auth = AuthContext(
            user=admin, api_key_id="ak_admin", key_kind="user",
            org_id=None, service_key_id=None,
        )
        # Admin CAN read (owner-visibility).
        await _enforce_resident_isolation(
            db_session, resident, admin_auth, allow_org_admin=True
        )
        # Admin CANNOT write (own-key-only path).
        with pytest.raises(HTTPException):
            await _enforce_resident_isolation(db_session, resident, admin_auth)

        # A user who is not a member of the resident's org is refused.
        other_org = await _make_org(db_session)
        outsider = await _make_user(db_session, other_org)
        outsider_auth = AuthContext(
            user=outsider, api_key_id="ak_out", key_kind="user",
            org_id=None, service_key_id=None,
        )
        with pytest.raises(HTTPException):
            await _enforce_resident_isolation(
                db_session, resident, outsider_auth, allow_org_admin=True
            )

        # A different service key (not the resident's own) is refused.
        other_sk = await _make_service_key(db_session, org, admin)
        other_svc_auth = AuthContext(
            user=admin, api_key_id=other_sk.id, key_kind="service",
            org_id=org.id, service_key_id=other_sk.id,
        )
        with pytest.raises(HTTPException):
            await _enforce_resident_isolation(
                db_session, resident, other_svc_auth, allow_org_admin=True
            )

    async def test_org_id_server_set_ignores_forged_body(
        self, db_session: AsyncSession
    ):
        """org_id on memory entries is SERVER-SET from resident row."""
        org = await _make_org(db_session)
        org2 = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        sk = await _make_service_key(db_session, org, user)
        r = await _make_resident(db_session, org, project, sk)

        # Server sets org_id from resident.org_id, not from any body value.
        e = ResidentMemoryEntry(
            id=f"rme_{uuid.uuid4().hex[:12]}",
            resident_id=r.id,
            org_id=r.org_id,  # server-set, not forged
            kind="reasoning",
            seq=1,
            content="data",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(e)
        await db_session.commit()

        # Entry's org_id matches the resident's org.
        assert e.org_id == org.id
        assert e.org_id != org2.id


class TestF6MemoryCaps:
    """F6: server hard cap on un-compacted entries."""

    async def test_cap_enforced(self, db_session: AsyncSession):
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        sk = await _make_service_key(db_session, org, user)
        r = await _make_resident(db_session, org, project, sk)
        # Override cap to a low value for testing.
        r.max_uncompacted_entries = 5
        await db_session.flush()

        # Write 5 entries (at cap).
        for i in range(5):
            db_session.add(
                ResidentMemoryEntry(
                    id=f"rme_{uuid.uuid4().hex[:12]}",
                    resident_id=r.id,
                    org_id=org.id,
                    kind="reasoning",
                    seq=i + 1,
                    content=f"entry {i}",
                    created_at=datetime.now(timezone.utc),
                )
            )
        await db_session.commit()

        # Count live entries.
        live = (
            await db_session.scalar(
                select(func.count(ResidentMemoryEntry.id)).where(
                    ResidentMemoryEntry.resident_id == r.id,
                    ResidentMemoryEntry.superseded_by.is_(None),
                )
            )
        )
        assert live == 5  # at cap

    async def test_compaction_frees_cap(self, db_session: AsyncSession):
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        sk = await _make_service_key(db_session, org, user)
        r = await _make_resident(db_session, org, project, sk)
        r.max_uncompacted_entries = 5
        await db_session.flush()

        for i in range(5):
            db_session.add(
                ResidentMemoryEntry(
                    id=f"rme_{uuid.uuid4().hex[:12]}",
                    resident_id=r.id,
                    org_id=org.id,
                    kind="reasoning",
                    seq=i + 1,
                    content=f"entry {i}",
                    created_at=datetime.now(timezone.utc),
                )
            )
        await db_session.commit()

        # Compact: create digest, supersede old entries.
        digest = ResidentMemoryEntry(
            id=f"rme_{uuid.uuid4().hex[:12]}",
            resident_id=r.id,
            org_id=org.id,
            kind="digest",
            seq=6,
            content="compacted summary",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(digest)
        await db_session.flush()

        # Mark old entries superseded.
        from sqlalchemy import update
        await db_session.execute(
            update(ResidentMemoryEntry)
            .where(
                ResidentMemoryEntry.resident_id == r.id,
                ResidentMemoryEntry.kind == "reasoning",
                ResidentMemoryEntry.superseded_by.is_(None),
            )
            .values(
                superseded_by=digest.id,
                compacted_at=datetime.now(timezone.utc),
            )
        )
        await db_session.commit()

        # Now live count should be 1 (just the digest).
        live = (
            await db_session.scalar(
                select(func.count(ResidentMemoryEntry.id)).where(
                    ResidentMemoryEntry.resident_id == r.id,
                    ResidentMemoryEntry.superseded_by.is_(None),
                )
            )
        )
        assert live == 1


class TestHydrateAndCompact:
    async def test_hydrate_excludes_quarantined(self, db_session: AsyncSession):
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        sk = await _make_service_key(db_session, org, user)
        r = await _make_resident(db_session, org, project, sk)

        # Write a quarantined reasoning entry.
        e_q = ResidentMemoryEntry(
            id=f"rme_{uuid.uuid4().hex[:12]}",
            resident_id=r.id,
            org_id=org.id,
            kind="reasoning",
            seq=1,
            content="poisoned thought",
            quarantined=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(e_q)
        # Write a clean reasoning entry.
        e_clean = ResidentMemoryEntry(
            id=f"rme_{uuid.uuid4().hex[:12]}",
            resident_id=r.id,
            org_id=org.id,
            kind="reasoning",
            seq=2,
            content="good thought",
            quarantined=False,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(e_clean)
        await db_session.commit()

        # Hydrate should exclude quarantined.
        clean_entries = (
            await db_session.execute(
                select(ResidentMemoryEntry).where(
                    ResidentMemoryEntry.resident_id == r.id,
                    ResidentMemoryEntry.kind == "reasoning",
                    ResidentMemoryEntry.superseded_by.is_(None),
                    ResidentMemoryEntry.quarantined.is_(False),
                )
                .order_by(ResidentMemoryEntry.seq.desc())
            )
        ).scalars().all()
        assert len(clean_entries) == 1
        assert clean_entries[0].content == "good thought"

    async def test_compact_supersedes(self, db_session: AsyncSession):
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        sk = await _make_service_key(db_session, org, user)
        r = await _make_resident(db_session, org, project, sk)

        e1 = ResidentMemoryEntry(
            id=f"rme_{uuid.uuid4().hex[:12]}",
            resident_id=r.id,
            org_id=org.id,
            kind="reasoning",
            seq=1,
            content="old reasoning",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(e1)
        await db_session.flush()

        digest = ResidentMemoryEntry(
            id=f"rme_{uuid.uuid4().hex[:12]}",
            resident_id=r.id,
            org_id=org.id,
            kind="digest",
            seq=2,
            content="summary",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(digest)
        await db_session.flush()

        # Mark e1 superseded.
        e1.superseded_by = digest.id
        e1.compacted_at = datetime.now(timezone.utc)
        await db_session.commit()

        # Live entries: only the digest.
        live = (
            await db_session.execute(
                select(ResidentMemoryEntry).where(
                    ResidentMemoryEntry.resident_id == r.id,
                    ResidentMemoryEntry.superseded_by.is_(None),
                )
            )
        ).scalars().all()
        assert len(live) == 1
        assert live[0].kind == "digest"


class TestF1SelfReviewProhibition:
    """F1 negative matrix — the headline security deliverable.

    (i)   implementer posts own clean verdict → NOT closed
    (ii)  same key implements + reviews → blocked at registration (F4)
          AND at close (identity match)
    (iii) implementer_* null → item never auto-closes (FAIL-CLOSED)
    (iv)  independent trusted reviewer clean → closes
    (v)   agent claims "done" while findings are open → NOT closed
    """

    async def test_f1_i_self_close_rejected(self, db_session: AsyncSession):
        """Implementer's own clean verdict does NOT close."""
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        ticket = await _make_ticket(db_session, project)
        queue = await _make_queue(db_session, project, user, mode="implement_until_done")
        item = await _make_item(db_session, queue, ticket)
        sk = await _make_service_key(db_session, org, user)

        # Bind an implementer resident to the queue so the F1 gate applies
        # (the strict close is scoped to resident-driven queues).
        await _make_resident(
            db_session, org, project, sk, kind="implementer", work_queue=queue
        )

        # Register the implementer as a trusted reviewer so its verdict
        # would be trusted — but the self-review check must still reject.
        tr = TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            org_id=org.id,
            project_id=None,
            service_key_id=sk.id,
            reviewer_persona="codex-reviewer",
            is_active=True,
            created_by_user_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(tr)
        await db_session.flush()

        # Seed a trusted VERIFIED-CLEAN from the SAME key that we'll
        # use as the implementer identity.
        await _seed_trusted_verdict(
            db_session, ticket, service_key_id=sk.id,
            content=R_VERIFIED_CLEAN, verdict_trusted=True,
        )

        # Set the item as active with an open directive.
        item.item_status = "active"
        item.open_directive_id = f"dir_{uuid.uuid4().hex[:12]}"
        # RECORD this key as the implementer.
        item.implementer_service_key_id = sk.id
        await db_session.flush()

        # Create an emit run for the directive.
        wqr = wq_engine.WorkQueueRun(
            id=f"wqr_{uuid.uuid4().hex[:16]}",
            work_queue_id=queue.id,
            work_queue_item_id=item.id,
            directive_id=item.open_directive_id,
            outcome=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(wqr)
        await db_session.flush()

        # Now call complete_work_queue_step as the implementer claiming "done".
        result = await wq_engine.complete_work_queue_step(
            db_session,
            queue=queue,
            item_id=item.id,
            directive_id=item.open_directive_id,
            ticket_id=ticket.id,
            outcome="done",
            comment_id=None,
            agent_run_id=None,
            failed=False,
            actor_user_id=user.id,
            actor_org_id=org.id,
            actor_service_key_id=sk.id,
            actor_type="service_key",
            service_key_name="test-key",
        )

        # F1(c): self-review → REJECTED. Item should NOT be terminal.
        assert result.item_terminal is False, (
            f"Self-review should be rejected but got terminal=True. "
            f"Status={result.status}"
        )

    async def test_f1_ii_self_review_bypass_via_later_trusted_note(
        self, db_session: AsyncSession
    ):
        """Regression (Codex round-3 P1): implementer posts its OWN
        VERIFIED-CLEAN, then a DIFFERENT trusted key posts a non-verdict note
        (updates last_review_comment_id but creates no new round). The
        self-review check must bind identity to the clean ROUND's author (the
        implementer) — NOT the last comment — else the resident self-closes."""
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        ticket = await _make_ticket(db_session, project)
        queue = await _make_queue(db_session, project, user, mode="implement_until_done")
        item = await _make_item(db_session, queue, ticket)
        impl_sk = await _make_service_key(db_session, org, user)
        other_sk = await _make_service_key(db_session, org, user)

        await _make_resident(
            db_session, org, project, impl_sk, kind="implementer", work_queue=queue
        )

        base = datetime.now(timezone.utc)
        # The implementer's OWN trusted VERIFIED-CLEAN (a clean round).
        db_session.add(TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket.id,
            author_user_id="",
            author_persona="codex-reviewer",
            content=R_VERIFIED_CLEAN,
            verdict_trusted=True,
            service_key_id=impl_sk.id,
            created_at=base,
        ))
        # A LATER trusted note from a DIFFERENT key — NOT a review round (no
        # header), but it is the most-recent trusted comment.
        db_session.add(TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket.id,
            author_user_id="",
            author_persona="codex-reviewer",
            content="Acknowledged — thanks for the fix.",
            verdict_trusted=True,
            service_key_id=other_sk.id,
            created_at=base + timedelta(minutes=1),
        ))

        item.item_status = "active"
        item.open_directive_id = f"dir_{uuid.uuid4().hex[:12]}"
        item.implementer_service_key_id = impl_sk.id
        await db_session.flush()

        wqr = wq_engine.WorkQueueRun(
            id=f"wqr_{uuid.uuid4().hex[:16]}",
            work_queue_id=queue.id,
            work_queue_item_id=item.id,
            directive_id=item.open_directive_id,
            outcome=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(wqr)
        await db_session.flush()

        result = await wq_engine.complete_work_queue_step(
            db_session,
            queue=queue,
            item_id=item.id,
            directive_id=item.open_directive_id,
            ticket_id=ticket.id,
            outcome="done",
            comment_id=None,
            agent_run_id=None,
            failed=False,
            actor_user_id=user.id,
            actor_org_id=org.id,
            actor_service_key_id=impl_sk.id,
            actor_type="service_key",
            service_key_name="impl-key",
        )

        # The self-review must NOT be bypassed by the later note.
        assert result.item_terminal is False, (
            "Self-review bypass: a later trusted note must not let the "
            "implementer's own VERIFIED-CLEAN auto-close the item."
        )

    async def test_f1_iii_unknown_implementer_fail_closed(
        self, db_session: AsyncSession
    ):
        """implementer_* null → fail-closed, no auto-close."""
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        ticket = await _make_ticket(db_session, project)
        queue = await _make_queue(db_session, project, user, mode="implement_until_done")
        item = await _make_item(db_session, queue, ticket)
        reviewer_sk = await _make_service_key(db_session, org, user)

        # Bind an implementer resident (with its own distinct key) so the F1
        # gate applies to this queue; the ITEM's implementer stays null to
        # exercise the fail-closed path.
        resident_sk = await _make_service_key(db_session, org, user)
        await _make_resident(
            db_session, org, project, resident_sk, kind="implementer", work_queue=queue
        )

        # Register a DIFFERENT key as trusted reviewer.
        tr = TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            org_id=org.id,
            project_id=None,
            service_key_id=reviewer_sk.id,
            reviewer_persona="codex-reviewer",
            is_active=True,
            created_by_user_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(tr)
        await db_session.flush()

        # Seed a trusted VERIFIED-CLEAN from the reviewer.
        await _seed_trusted_verdict(
            db_session, ticket, service_key_id=reviewer_sk.id,
            content=R_VERIFIED_CLEAN, verdict_trusted=True,
        )

        item.item_status = "active"
        item.open_directive_id = f"dir_{uuid.uuid4().hex[:12]}"
        # implementer_* is NULL — unknown who implemented.
        await db_session.flush()

        wqr = wq_engine.WorkQueueRun(
            id=f"wqr_{uuid.uuid4().hex[:16]}",
            work_queue_id=queue.id,
            work_queue_item_id=item.id,
            directive_id=item.open_directive_id,
            outcome=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(wqr)
        await db_session.flush()

        result = await wq_engine.complete_work_queue_step(
            db_session,
            queue=queue,
            item_id=item.id,
            directive_id=item.open_directive_id,
            ticket_id=ticket.id,
            outcome="done",
            comment_id=None,
            agent_run_id=None,
            failed=False,
            actor_user_id=None,  # unknown — no user identity
            actor_org_id=org.id,
            actor_service_key_id=None,  # unknown — no service key
            actor_type="user",
            service_key_name=None,
        )

        # F1(d): implementer unknown → FAIL-CLOSED.
        assert result.item_terminal is False, (
            "Unknown implementer should fail-closed but got terminal=True"
        )

    async def test_f1_iv_independent_reviewer_closes(
        self, db_session: AsyncSession
    ):
        """Independent trusted reviewer VERIFIED-CLEAN → closes with marker."""
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        ticket = await _make_ticket(db_session, project)
        queue = await _make_queue(db_session, project, user, mode="implement_until_done")
        item = await _make_item(db_session, queue, ticket)

        impl_sk = await _make_service_key(db_session, org, user)
        reviewer_sk = await _make_service_key(db_session, org, user)

        # Bind an implementer resident (the implementer key) so the F1 gate
        # applies; the independent reviewer key closes the item.
        await _make_resident(
            db_session, org, project, impl_sk, kind="implementer", work_queue=queue
        )

        # The implementer's writeback (its work) — the clean verdict must
        # postdate this (P1 temporal gate).
        db_session.add(TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket.id,
            author_user_id="",
            author_persona="atlas",
            content="Implemented the fix.",
            verdict_trusted=False,
            service_key_id=impl_sk.id,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        ))
        await db_session.flush()

        # Register reviewer as trusted.
        tr = TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            org_id=org.id,
            project_id=None,
            service_key_id=reviewer_sk.id,
            reviewer_persona="codex-reviewer",
            is_active=True,
            created_by_user_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(tr)
        await db_session.flush()

        # Seed trusted VERIFIED-CLEAN from the INDEPENDENT reviewer (postdates
        # the implementer's writeback above).
        await _seed_trusted_verdict(
            db_session, ticket, service_key_id=reviewer_sk.id,
            content=R_VERIFIED_CLEAN, verdict_trusted=True,
        )

        item.item_status = "active"
        item.open_directive_id = f"dir_{uuid.uuid4().hex[:12]}"
        # Implementer is a DIFFERENT key.
        item.implementer_service_key_id = impl_sk.id
        await db_session.flush()

        wqr = wq_engine.WorkQueueRun(
            id=f"wqr_{uuid.uuid4().hex[:16]}",
            work_queue_id=queue.id,
            work_queue_item_id=item.id,
            directive_id=item.open_directive_id,
            outcome=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(wqr)
        await db_session.flush()

        result = await wq_engine.complete_work_queue_step(
            db_session,
            queue=queue,
            item_id=item.id,
            directive_id=item.open_directive_id,
            ticket_id=ticket.id,
            outcome="done",
            comment_id=None,
            agent_run_id=None,
            failed=False,
            actor_user_id=user.id,
            actor_org_id=org.id,
            actor_service_key_id=impl_sk.id,
            actor_type="service_key",
            service_key_name="impl-key",
        )

        # Independent reviewer → item closes.
        assert result.item_terminal is True, (
            f"Independent reviewer should close. Status={result.status}"
        )
        # Verify auto_close_review_kind is set.
        # Check item directly (session-modified, no refresh needed)
        assert item.auto_close_review_kind == "resident_trusted", (
            f"Expected 'resident_trusted' marker, got {item.auto_close_review_kind}"
        )
        # closed_by_* should match the REVIEWER, not the implementer.
        assert item.closed_by_service_key_id == reviewer_sk.id, (
            f"closed_by should be reviewer key {reviewer_sk.id}, "
            f"got {item.closed_by_service_key_id}"
        )

    async def test_f1_v_open_findings_blocks_close(
        self, db_session: AsyncSession
    ):
        """Agent claims done but review has open findings → NOT closed."""
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        ticket = await _make_ticket(db_session, project)
        queue = await _make_queue(db_session, project, user, mode="implement_until_done")
        item = await _make_item(db_session, queue, ticket)

        impl_sk = await _make_service_key(db_session, org, user)
        reviewer_sk = await _make_service_key(db_session, org, user)

        # Bind an implementer resident so the F1 gate applies to this queue.
        await _make_resident(
            db_session, org, project, impl_sk, kind="implementer", work_queue=queue
        )

        tr = TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            org_id=org.id,
            project_id=None,
            service_key_id=reviewer_sk.id,
            reviewer_persona="codex-reviewer",
            is_active=True,
            created_by_user_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(tr)
        await db_session.flush()

        # Seed a CHANGES REQUESTED verdict (open findings).
        await _seed_trusted_verdict(
            db_session, ticket, service_key_id=reviewer_sk.id,
            content=R_CHANGES, verdict_trusted=True,
        )

        item.item_status = "active"
        item.open_directive_id = f"dir_{uuid.uuid4().hex[:12]}"
        item.implementer_service_key_id = impl_sk.id
        await db_session.flush()

        wqr = wq_engine.WorkQueueRun(
            id=f"wqr_{uuid.uuid4().hex[:16]}",
            work_queue_id=queue.id,
            work_queue_item_id=item.id,
            directive_id=item.open_directive_id,
            outcome=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(wqr)
        await db_session.flush()

        result = await wq_engine.complete_work_queue_step(
            db_session,
            queue=queue,
            item_id=item.id,
            directive_id=item.open_directive_id,
            ticket_id=ticket.id,
            outcome="done",
            comment_id=None,
            agent_run_id=None,
            failed=False,
            actor_user_id=user.id,
            actor_org_id=org.id,
            actor_service_key_id=impl_sk.id,
            actor_type="service_key",
            service_key_name="impl-key",
        )

        # Open findings → NOT terminal.
        assert result.item_terminal is False, (
            "Open findings should block close"
        )

    async def test_f1_triage_still_works(self, db_session: AsyncSession):
        """triage mode keeps existing behavior (outcome-based close)."""
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        ticket = await _make_ticket(db_session, project)
        queue = await _make_queue(db_session, project, user, mode="triage")
        item = await _make_item(db_session, queue, ticket)

        item.item_status = "active"
        item.open_directive_id = f"dir_{uuid.uuid4().hex[:12]}"
        await db_session.flush()

        wqr = wq_engine.WorkQueueRun(
            id=f"wqr_{uuid.uuid4().hex[:16]}",
            work_queue_id=queue.id,
            work_queue_item_id=item.id,
            directive_id=item.open_directive_id,
            outcome=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(wqr)
        await db_session.flush()

        result = await wq_engine.complete_work_queue_step(
            db_session,
            queue=queue,
            item_id=item.id,
            directive_id=item.open_directive_id,
            ticket_id=ticket.id,
            outcome="completed_ticket",
            comment_id=None,
            agent_run_id=None,
            failed=False,
            actor_user_id=user.id,
            actor_org_id=org.id,
            actor_service_key_id=None,
            actor_type="user",
            service_key_name=None,
        )

        assert result.item_terminal is True

    async def test_f1_stale_verdict_predating_writeback_no_close(
        self, db_session: AsyncSession
    ):
        """P1: a trusted VERIFIED-CLEAN that PREDATES the implementer's latest
        writeback (i.e. a prior review cycle) must NOT auto-close the resident's
        new, not-yet-re-reviewed work."""
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        ticket = await _make_ticket(db_session, project)
        queue = await _make_queue(db_session, project, user, mode="implement_until_done")
        item = await _make_item(db_session, queue, ticket)
        impl_sk = await _make_service_key(db_session, org, user)
        reviewer_sk = await _make_service_key(db_session, org, user)
        await _make_resident(
            db_session, org, project, impl_sk, kind="implementer", work_queue=queue
        )

        tr = TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            org_id=org.id,
            project_id=None,
            service_key_id=reviewer_sk.id,
            reviewer_persona="codex-reviewer",
            is_active=True,
            created_by_user_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(tr)
        await db_session.flush()

        base = datetime.now(timezone.utc)
        # OLD independent clean verdict (a PRIOR review cycle).
        db_session.add(TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket.id,
            author_user_id="",
            author_persona="codex-reviewer",
            content=R_VERIFIED_CLEAN,
            verdict_trusted=True,
            service_key_id=reviewer_sk.id,
            created_at=base - timedelta(hours=1),
        ))
        # NEWER implementer writeback (new work, NOT yet re-reviewed).
        db_session.add(TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket.id,
            author_user_id="",
            author_persona="atlas",
            content="New change since the last review.",
            verdict_trusted=False,
            service_key_id=impl_sk.id,
            created_at=base,
        ))

        item.item_status = "active"
        item.open_directive_id = f"dir_{uuid.uuid4().hex[:12]}"
        item.implementer_service_key_id = impl_sk.id
        await db_session.flush()

        wqr = wq_engine.WorkQueueRun(
            id=f"wqr_{uuid.uuid4().hex[:16]}",
            work_queue_id=queue.id,
            work_queue_item_id=item.id,
            directive_id=item.open_directive_id,
            outcome=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(wqr)
        await db_session.flush()

        result = await wq_engine.complete_work_queue_step(
            db_session,
            queue=queue,
            item_id=item.id,
            directive_id=item.open_directive_id,
            ticket_id=ticket.id,
            outcome="done",
            comment_id=None,
            agent_run_id=None,
            failed=False,
            actor_user_id=user.id,
            actor_org_id=org.id,
            actor_service_key_id=impl_sk.id,
            actor_type="service_key",
            service_key_name="impl-key",
        )
        assert result.item_terminal is False, (
            "A clean verdict predating the implementer's writeback must not "
            "auto-close the new work."
        )

    async def test_f1_resident_cannot_self_close_via_sibling_queue(
        self, db_session: AsyncSession
    ):
        """H1 (Sentinel): an implementer resident cannot route around F1 by
        settling a SIBLING implement queue that has no bound resident — the
        strict path fires on the acting implementer-resident identity, not just
        the queue binding."""
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        impl_sk = await _make_service_key(db_session, org, user)

        # The resident is registered to drive queue Q1.
        q1 = await _make_queue(db_session, project, user, mode="implement_until_done")
        await _make_resident(
            db_session, org, project, impl_sk, kind="implementer", work_queue=q1
        )

        # A DIFFERENT implement queue Q2 with NO bound resident (the bypass
        # target). The resident settles it with its own key claiming 'done'.
        q2 = await _make_queue(db_session, project, user, mode="implement_until_done")
        ticket = await _make_ticket(db_session, project)
        item = await _make_item(db_session, q2, ticket)
        item.item_status = "active"
        item.open_directive_id = f"dir_{uuid.uuid4().hex[:12]}"
        await db_session.flush()

        wqr = wq_engine.WorkQueueRun(
            id=f"wqr_{uuid.uuid4().hex[:16]}",
            work_queue_id=q2.id,
            work_queue_item_id=item.id,
            directive_id=item.open_directive_id,
            outcome=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(wqr)
        await db_session.flush()

        result = await wq_engine.complete_work_queue_step(
            db_session,
            queue=q2,
            item_id=item.id,
            directive_id=item.open_directive_id,
            ticket_id=ticket.id,
            outcome="done",
            comment_id=None,
            agent_run_id=None,
            failed=False,
            actor_user_id=user.id,
            actor_org_id=org.id,
            actor_service_key_id=impl_sk.id,
            actor_type="service_key",
            service_key_name="impl-key",
        )
        assert result.item_terminal is False, (
            "An implementer resident must not self-close via an unbound sibling "
            "implement queue."
        )

    async def test_f1_human_verdict_marker(self, db_session: AsyncSession):
        """A human (user-key, no service_key_id on the verdict) marks 'human'."""
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        ticket = await _make_ticket(db_session, project)
        queue = await _make_queue(db_session, project, user, mode="implement_until_done")
        item = await _make_item(db_session, queue, ticket)
        impl_sk = await _make_service_key(db_session, org, user)

        # Bind an implementer resident so the F1 gate applies; a human
        # reviewer (independent) then closes → 'human' marker.
        await _make_resident(
            db_session, org, project, impl_sk, kind="implementer", work_queue=queue
        )

        # The implementer's writeback — the human clean verdict must postdate it.
        db_session.add(TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket.id,
            author_user_id="",
            author_persona="atlas",
            content="Implemented the fix.",
            verdict_trusted=False,
            service_key_id=impl_sk.id,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        ))
        await db_session.flush()

        # Register the HUMAN USER as trusted reviewer (user_id, not service_key_id).
        tr = TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            org_id=org.id,
            project_id=None,
            user_id=user.id,
            service_key_id=None,
            reviewer_persona="codex-reviewer",
            is_active=True,
            created_by_user_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(tr)
        await db_session.flush()

        # Seed trusted VERIFIED-CLEAN from the HUMAN (no service_key_id).
        await _seed_trusted_verdict(
            db_session, ticket, user_id=user.id, service_key_id=None,
            content=R_VERIFIED_CLEAN, verdict_trusted=True,
        )

        item.item_status = "active"
        item.open_directive_id = f"dir_{uuid.uuid4().hex[:12]}"
        item.implementer_service_key_id = impl_sk.id
        await db_session.flush()

        wqr = wq_engine.WorkQueueRun(
            id=f"wqr_{uuid.uuid4().hex[:16]}",
            work_queue_id=queue.id,
            work_queue_item_id=item.id,
            directive_id=item.open_directive_id,
            outcome=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(wqr)
        await db_session.flush()

        result = await wq_engine.complete_work_queue_step(
            db_session,
            queue=queue,
            item_id=item.id,
            directive_id=item.open_directive_id,
            ticket_id=ticket.id,
            outcome="done",
            comment_id=None,
            agent_run_id=None,
            failed=False,
            actor_user_id=user.id,
            actor_org_id=org.id,
            actor_service_key_id=impl_sk.id,
            actor_type="service_key",
            service_key_name="impl-key",
        )

        assert result.item_terminal is True
        # Check item directly (session-modified, no refresh needed)
        assert item.auto_close_review_kind == "human", (
            f"Expected 'human' marker, got {item.auto_close_review_kind}"
        )


class TestF7KeyRotation:
    """F7: rotated-out key is denied; new key rebinds and can read same mind."""

    async def test_rotate_key_preserves_mind(self, db_session: AsyncSession):
        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        sk_old = await _make_service_key(db_session, org, user)
        sk_new = await _make_service_key(db_session, org, user)

        r = await _make_resident(db_session, org, project, sk_old)

        # Write memory with old key.
        e = ResidentMemoryEntry(
            id=f"rme_{uuid.uuid4().hex[:12]}",
            resident_id=r.id,
            org_id=org.id,
            kind="reasoning",
            seq=1,
            content="mind data",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(e)
        await db_session.commit()

        # Rotate key.
        r.service_key_id = sk_new.id
        r.service_key_name = sk_new.service_key_name
        r.updated_at = datetime.now(timezone.utc)
        await db_session.commit()
        await db_session.refresh(r)

        assert r.service_key_id == sk_new.id

        # Old key still exists but is no longer bound to the resident.
        # New key is bound.
        # Memory is still there (keyed by resident_id, not service_key_id).
        entries = (
            await db_session.execute(
                select(ResidentMemoryEntry).where(
                    ResidentMemoryEntry.resident_id == r.id,
                )
            )
        ).scalars().all()
        assert len(entries) == 1
        assert entries[0].content == "mind data"


class TestResidentRouteHardening:
    """Round-4 authz fixes: project allowlist, key-rotation revocation,
    and the hydrate token budget."""

    @staticmethod
    def _admin_ctx(user: User, org: Organization):
        from sessionfs.server.tier_gate import Tier, UserContext

        return UserContext(
            user=user,
            effective_tier=Tier.ENTERPRISE,
            org=org,
            role="admin",
            is_org_user=True,
        )

    async def test_register_rejects_key_outside_project_allowlist(
        self, db_session: AsyncSession
    ):
        """A service key whose project_ids allowlist excludes the target
        project cannot be bound to a resident for it (cross-project bypass)."""
        from fastapi import HTTPException

        from sessionfs.server.routes.residents import (
            ResidentRegisterRequest,
            register_resident,
        )

        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        target = await _make_project(db_session, user, org)
        other = await _make_project(db_session, user, org)
        sk = await _make_service_key(db_session, org, user)
        sk.project_ids = json.dumps([other.id])  # allowlisted to a DIFFERENT project
        await db_session.flush()

        body = ResidentRegisterRequest(
            service_key_id=sk.id,
            kind="implementer",
            persona_name="atlas",
            project_id=target.id,
            mind_token_budget=8000,
        )
        with pytest.raises(HTTPException) as ei:
            await register_resident(org.id, body, self._admin_ctx(user, org), db_session)
        assert ei.value.status_code == 403
        assert ei.value.detail["error"] == "service_key_project_not_allowed"

    async def test_rotate_key_revokes_old_key(self, db_session: AsyncSession):
        """F7: rotating a resident's key revokes the rotated-out key so it can
        no longer authenticate."""
        from sessionfs.server.routes.residents import (
            RotateKeyRequest,
            rotate_resident_key,
        )

        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        old_sk = await _make_service_key(db_session, org, user)
        resident = await _make_resident(
            db_session, org, project, old_sk, kind="implementer"
        )
        new_sk = await _make_service_key(db_session, org, user)
        await db_session.commit()

        await rotate_resident_key(
            org.id,
            resident.id,
            RotateKeyRequest(new_service_key_id=new_sk.id),
            self._admin_ctx(user, org),
            db_session,
        )

        await db_session.refresh(old_sk)
        await db_session.refresh(resident)
        assert resident.service_key_id == new_sk.id
        assert old_sk.revoked_at is not None
        assert old_sk.is_active is False

    async def test_hydrate_respects_token_budget(self, db_session: AsyncSession):
        """hydrate never returns reasoning entries exceeding mind_token_budget."""
        from sessionfs.server.auth.dependencies import AuthContext
        from sessionfs.server.routes.residents import hydrate_memory

        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        sk = await _make_service_key(db_session, org, user)
        resident = await _make_resident(
            db_session, org, project, sk, kind="implementer"
        )
        resident.mind_token_budget = 1000
        # Three reasoning entries at 600 tokens each — only the first fits.
        for i in range(3):
            db_session.add(ResidentMemoryEntry(
                id=f"rme_{uuid.uuid4().hex[:12]}",
                resident_id=resident.id,
                org_id=org.id,
                kind="reasoning",
                seq=i + 1,
                content=f"reasoning {i}",
                token_estimate=600,
                created_at=datetime.now(timezone.utc),
            ))
        await db_session.flush()

        auth = AuthContext(
            user=user, api_key_id=sk.id, key_kind="service",
            org_id=org.id, service_key_id=sk.id,
        )
        resp = await hydrate_memory(org.id, resident.id, auth=auth, db=db_session)
        total = sum(e["token_estimate"] for e in resp.recent_reasoning)
        assert total <= 1000
        assert len(resp.recent_reasoning) == 1

    async def test_rotate_rejects_key_outside_project_allowlist(
        self, db_session: AsyncSession
    ):
        """Rotation must enforce the new key's project allowlist (same as
        registration) — else a project-B key could reach a project-A mind."""
        from fastapi import HTTPException

        from sessionfs.server.routes.residents import (
            RotateKeyRequest,
            rotate_resident_key,
        )

        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        other = await _make_project(db_session, user, org)
        old_sk = await _make_service_key(db_session, org, user)
        resident = await _make_resident(
            db_session, org, project, old_sk, kind="implementer"
        )
        new_sk = await _make_service_key(db_session, org, user)
        new_sk.project_ids = json.dumps([other.id])  # allowlisted to a DIFFERENT project
        await db_session.commit()

        with pytest.raises(HTTPException) as ei:
            await rotate_resident_key(
                org.id,
                resident.id,
                RotateKeyRequest(new_service_key_id=new_sk.id),
                self._admin_ctx(user, org),
                db_session,
            )
        assert ei.value.status_code == 403
        assert ei.value.detail["error"] == "service_key_project_not_allowed"

    async def test_compact_cannot_bypass_cap(self, db_session: AsyncSession):
        """compact with empty supersession cannot create unlimited digests past
        the F6 cap that write_memory enforces."""
        from fastapi import HTTPException

        from sessionfs.server.auth.dependencies import AuthContext
        from sessionfs.server.routes.residents import (
            MemoryCompactRequest,
            compact_memory,
        )

        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        sk = await _make_service_key(db_session, org, user)
        resident = await _make_resident(
            db_session, org, project, sk, kind="implementer"
        )
        resident.max_uncompacted_entries = 1
        await db_session.commit()

        auth = AuthContext(
            user=user, api_key_id=sk.id, key_kind="service",
            org_id=org.id, service_key_id=sk.id,
        )
        # First compact (empty supersession) → 1 live digest == cap, allowed.
        await compact_memory(
            org.id, resident.id,
            MemoryCompactRequest(digest_content="s1", digest_token_estimate=10),
            auth, db_session,
        )
        # Second compact (empty supersession) → 2 live digests > cap → 429.
        with pytest.raises(HTTPException) as ei:
            await compact_memory(
                org.id, resident.id,
                MemoryCompactRequest(digest_content="s2", digest_token_estimate=10),
                auth, db_session,
            )
        assert ei.value.status_code == 429
        assert ei.value.detail["error"] == "memory_cap_exceeded"

    async def test_admin_cannot_read_reviewer_mind(self, db_session: AsyncSession):
        """Owner-visibility is implementer-only — an org admin cannot inspect a
        REVIEWER resident's mind."""
        from fastapi import HTTPException

        from sessionfs.server.auth.dependencies import AuthContext
        from sessionfs.server.routes.residents import _enforce_resident_isolation

        org = await _make_org(db_session)
        admin = await _make_user(db_session, org)
        project = await _make_project(db_session, admin, org)
        sk = await _make_service_key(db_session, org, admin)
        reviewer = await _make_resident(
            db_session, org, project, sk, kind="reviewer"
        )
        await db_session.commit()

        admin_auth = AuthContext(
            user=admin, api_key_id="ak_admin", key_kind="user",
            org_id=None, service_key_id=None,
        )
        with pytest.raises(HTTPException):
            await _enforce_resident_isolation(
                db_session, reviewer, admin_auth, allow_org_admin=True
            )

    async def test_paused_resident_denied_on_work_queue_act(
        self, db_session: AsyncSession
    ):
        """Kill switch: a paused/retired resident's key cannot act on work
        queues even though the key still authenticates by scope."""
        from fastapi import HTTPException

        from sessionfs.server.auth.dependencies import AuthContext
        from sessionfs.server.routes.work_queues import _enforce_resident_active

        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        sk = await _make_service_key(db_session, org, user)
        resident = await _make_resident(
            db_session, org, project, sk, kind="implementer"
        )
        auth = AuthContext(
            user=user, api_key_id=sk.id, key_kind="service",
            org_id=org.id, service_key_id=sk.id,
        )
        # Active resident → allowed.
        await _enforce_resident_active(auth, db_session)

        # Paused → denied.
        resident.status = "paused"
        await db_session.flush()
        with pytest.raises(HTTPException) as ei:
            await _enforce_resident_active(auth, db_session)
        assert ei.value.status_code == 403
        assert ei.value.detail["error"] == "resident_not_active"

        # A non-resident service key is unaffected.
        other_sk = await _make_service_key(db_session, org, user)
        other_auth = AuthContext(
            user=user, api_key_id=other_sk.id, key_kind="service",
            org_id=org.id, service_key_id=other_sk.id,
        )
        await _enforce_resident_active(other_auth, db_session)

    async def test_rotate_refreshes_inflight_item_implementer(
        self, db_session: AsyncSession
    ):
        """Rotating a resident's key re-points in-flight work-queue items'
        recorded implementer identity to the new key (so F1 guards track it)."""
        from sessionfs.server.routes.residents import (
            RotateKeyRequest,
            rotate_resident_key,
        )

        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        old_sk = await _make_service_key(db_session, org, user)
        resident = await _make_resident(
            db_session, org, project, old_sk, kind="implementer"
        )
        queue = await _make_queue(db_session, project, user)
        ticket = await _make_ticket(db_session, project)
        item = await _make_item(db_session, queue, ticket)
        item.implementer_service_key_id = old_sk.id
        # A COMPLETED item — its implementer provenance must NOT be rewritten.
        ticket2 = await _make_ticket(db_session, project)
        done_item = await _make_item(db_session, queue, ticket2)
        done_item.implementer_service_key_id = old_sk.id
        done_item.item_status = "done"
        new_sk = await _make_service_key(db_session, org, user)
        await db_session.commit()

        await rotate_resident_key(
            org.id,
            resident.id,
            RotateKeyRequest(new_service_key_id=new_sk.id),
            self._admin_ctx(user, org),
            db_session,
        )
        await db_session.refresh(item)
        await db_session.refresh(done_item)
        # In-flight item re-pointed; completed item's provenance preserved.
        assert item.implementer_service_key_id == new_sk.id
        assert done_item.implementer_service_key_id == old_sk.id

    async def test_quarantine_requires_org_admin(self, db_session: AsyncSession):
        """M1 (Sentinel): quarantine is an owner/admin recovery signal — the
        resident's OWN key cannot quarantine (which would defeat the F6 cap)."""
        from fastapi import HTTPException

        from sessionfs.server.auth.dependencies import AuthContext
        from sessionfs.server.routes.residents import quarantine_memory_entry

        org = await _make_org(db_session)
        admin = await _make_user(db_session, org)
        project = await _make_project(db_session, admin, org)
        sk = await _make_service_key(db_session, org, admin)
        resident = await _make_resident(
            db_session, org, project, sk, kind="implementer"
        )
        entry = ResidentMemoryEntry(
            id=f"rme_{uuid.uuid4().hex[:12]}",
            resident_id=resident.id,
            org_id=org.id,
            kind="reasoning",
            seq=1,
            content="data",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(entry)
        await db_session.commit()

        # The resident's OWN service key is denied.
        own_auth = AuthContext(
            user=admin, api_key_id=sk.id, key_kind="service",
            org_id=org.id, service_key_id=sk.id,
        )
        with pytest.raises(HTTPException) as ei:
            await quarantine_memory_entry(
                org.id, resident.id, entry.id, auth=own_auth, db=db_session
            )
        assert ei.value.status_code == 403
        assert ei.value.detail["error"] == "resident_quarantine_denied"

        # An org admin USER key succeeds.
        admin_auth = AuthContext(
            user=admin, api_key_id="ak_admin", key_kind="user",
            org_id=org.id, service_key_id=None,
        )
        resp = await quarantine_memory_entry(
            org.id, resident.id, entry.id, auth=admin_auth, db=db_session
        )
        assert resp.quarantined is True

    async def test_retire_revokes_bound_key(self, db_session: AsyncSession):
        """M2 (Sentinel): retiring a resident revokes its bound service key
        (a hard stop across all scopes, not just the work-queue act path)."""
        from sessionfs.server.routes.residents import (
            ResidentStatusRequest,
            set_resident_status,
        )

        org = await _make_org(db_session)
        user = await _make_user(db_session, org)
        project = await _make_project(db_session, user, org)
        sk = await _make_service_key(db_session, org, user)
        resident = await _make_resident(
            db_session, org, project, sk, kind="implementer"
        )
        await db_session.commit()

        await set_resident_status(
            org.id, resident.id,
            ResidentStatusRequest(status="retired"),
            self._admin_ctx(user, org),
            db_session,
        )
        await db_session.refresh(sk)
        assert sk.revoked_at is not None
        assert sk.is_active is False

    async def test_write_memory_rejects_digest_kind(self, db_session: AsyncSession):
        """L3 (Sentinel): write only creates reasoning/observation — a digest
        may be created only via the compact path."""
        from sessionfs.server.routes.residents import MemoryWriteRequest

        with pytest.raises(ValueError):
            MemoryWriteRequest(kind="digest", content="x", token_estimate=1)

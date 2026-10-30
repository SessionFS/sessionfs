"""Review Loop B2 (tk_50bfb43bcd45457c) — merge readiness endpoint.

`GET /api/v1/projects/{p}/tickets/{t}/readiness?sha=<head>` is a pure read
that gates a merge on four server-derived facts: a trusted STRICT
VERIFIED-CLEAN verdict exists, its reviewed_commit equals the requested head
exactly, the reviewer's identity differs from every implementer identity, and
no open finding is harmful. The matrix below seeds TicketComment rows with
EXPLICIT verdict_trusted / reviewed_commit / identity values (mirroring
test_work_queue_review_oracle.py) so each predicate is exercised in
isolation without the trusted_reviewers registry write path.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    Project,
    RetrievalAuditContext,
    TicketComment,
    User,
    WorkQueue,
    WorkQueueItem,
)

HEAD = "0123456789abcdef0123456789abcdef01234567"  # 40-char SHA-1
OTHER = "89abcdef0123456789abcdef0123456789abcdef"  # 40-char, distinct

CLEAN = "Codex R1 review on tk_x: VERIFIED-CLEAN\n\nFindings: none."


async def _make_user(
    db: AsyncSession, name: str = "owner", tier: str = "team"
) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier=tier,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
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


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_project(db: AsyncSession, owner: User) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"ready-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/r-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_ticket(
    client: AsyncClient, project: Project, key: str, title: str = "t"
) -> str:
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": title},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _seed_clean_verdict(
    db: AsyncSession,
    ticket_id: str,
    reviewer: User,
    *,
    reviewed_commit: str,
    content: str = CLEAN,
    service_key_id: str | None = None,
    created_at: datetime | None = None,
) -> TicketComment:
    c = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket_id,
        author_user_id=reviewer.id,
        author_persona="codex-reviewer",
        content=content,
        verdict_trusted=True,
        reviewed_commit=reviewed_commit,
        service_key_id=service_key_id,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


async def _make_service_key(db: AsyncSession, minter: User) -> ApiKey:
    """A scoped service key minted by `minter` (the reviewer credential)."""
    key = ApiKey(
        id=f"sk_{uuid.uuid4().hex[:24]}",
        user_id=minter.id,
        key_hash=hash_api_key(generate_api_key()),
        name=f"svc-{uuid.uuid4().hex[:6]}",
        key_kind="service",
        scopes='["tickets:read","tickets:write"]',
        created_at=datetime.now(timezone.utc),
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)
    return key


async def _seed_start_context(
    db: AsyncSession,
    ticket_id: str,
    *,
    user_id: str | None,
    service_key_id: str | None = None,
    lease_epoch: int = 0,
) -> None:
    db.add(
        RetrievalAuditContext(
            id=f"ra_{uuid.uuid4().hex[:24]}",
            project_id=None,
            ticket_id=ticket_id,
            lease_epoch=lease_epoch,
            created_by_user_id=user_id,
            actor_type="service_key" if service_key_id else "user",
            service_key_id=service_key_id,
        )
    )
    await db.commit()


async def _seed_implementer(
    db: AsyncSession,
    project: Project,
    ticket_id: str,
    *,
    user_id: str | None,
    service_key_id: str | None = None,
) -> None:
    queue = WorkQueue(
        id=f"wq_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name=f"q-{uuid.uuid4().hex[:6]}",
        mode="implement_until_done",
        selector="{}",
        created_by_user_id=project.owner_id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(queue)
    await db.flush()
    db.add(
        WorkQueueItem(
            id=f"wqi_{uuid.uuid4().hex[:16]}",
            work_queue_id=queue.id,
            ticket_id=ticket_id,
            item_status="active",
            implementer_user_id=user_id,
            implementer_service_key_id=service_key_id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def _readiness(
    client: AsyncClient, project: Project, ticket_id: str, key: str, sha: str
):
    return await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}/readiness",
        headers=_hdrs(key),
        params={"sha": sha},
    )


@pytest.mark.asyncio
async def test_readiness_false_without_trusted_clean(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket_id = await _make_ticket(client, project, key)

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    assert body["verdict"] is None
    assert "no trusted strict VERIFIED-CLEAN verdict" in body["reasons"]


@pytest.mark.asyncio
async def test_readiness_rejects_short_sha(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket_id = await _make_ticket(client, project, key)

    # A 16-char hex prefix is no longer a full commit id — reject 422 before
    # any state is read.
    resp = await _readiness(client, project, ticket_id, key, "a1b2c3d4e5f6a7b8")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_readiness_false_when_sha_moved(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket_id = await _make_ticket(client, project, key)
    await _seed_clean_verdict(
        db_session, ticket_id, user, reviewed_commit=OTHER
    )

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    assert body["verdict"] is not None
    assert body["verdict"]["reviewed_commit"] == OTHER
    assert any(
        "not bound to the requested sha" in r for r in body["reasons"]
    )


@pytest.mark.asyncio
async def test_readiness_false_when_reviewer_is_implementer(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket_id = await _make_ticket(client, project, key)
    # The reviewer is the same identity that implemented the item.
    await _seed_clean_verdict(
        db_session, ticket_id, owner, reviewed_commit=HEAD
    )
    await _seed_implementer(
        db_session, project, ticket_id, user_id=owner.id
    )

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    assert any(
        "reviewer identity matches an implementer identity" in r
        for r in body["reasons"]
    )


@pytest.mark.asyncio
async def test_readiness_false_when_harmful_finding_open(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket_id = await _make_ticket(client, project, key)
    # A strict clean verdict whose own round also raises a HIGH finding —
    # no later clean round closes it, so it stays open. The finding has no
    # harm tag, so MEDIUM+ fails closed.
    content = (
        "Codex R1 review on tk_x: VERIFIED-CLEAN\n\n"
        "Findings:\n\n • HIGH — residual crash risk in src/foo.py:10\n"
    )
    await _seed_clean_verdict(
        db_session, ticket_id, user, reviewed_commit=HEAD, content=content
    )

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    assert any("open harmful finding" in r for r in body["reasons"])


@pytest.mark.asyncio
async def test_readiness_false_when_latest_round_is_out_of_order_changes(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket_id = await _make_ticket(client, project, key)

    reviewer, _ = await _make_user(db_session, name="reviewer")
    # A trusted R9 VERIFIED-CLEAN posts first (bound to HEAD), then a LATER
    # R2 CHANGES_REQUESTED with a HIGH finding. Chronology — not the round
    # number — decides the latest verdict, so readiness must fail closed.
    await _seed_clean_verdict(
        db_session,
        ticket_id,
        reviewer,
        reviewed_commit=HEAD,
        content="Codex R9 review on tk_x: VERIFIED-CLEAN\n\nFindings: none.",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db_session.add(
        TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket_id,
            author_user_id=reviewer.id,
            author_persona="codex-reviewer",
            content=(
                "Codex R2 review on tk_x: CHANGES REQUESTED\n\n"
                "Findings:\n\n • HIGH — residual crash risk in src/foo.py:10\n"
            ),
            verdict_trusted=True,
            created_at=datetime.now(timezone.utc),
        )
    )
    await _seed_implementer(
        db_session, project, ticket_id, user_id=owner.id
    )

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    assert "no trusted strict VERIFIED-CLEAN verdict" in body["reasons"]


@pytest.mark.asyncio
async def test_readiness_true_when_all_conditions_hold(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket_id = await _make_ticket(client, project, key)

    # A different human implemented the item.
    reviewer, _ = await _make_user(db_session, name="reviewer")
    await _seed_clean_verdict(
        db_session, ticket_id, reviewer, reviewed_commit=HEAD
    )
    await _seed_implementer(
        db_session, project, ticket_id, user_id=owner.id
    )

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["reasons"] == []
    assert body["verdict"]["reviewed_commit"] == HEAD
    assert body["verdict"]["reviewer"] == reviewer.id


@pytest.mark.asyncio
async def test_readiness_false_when_no_implementer_recorded(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket_id = await _make_ticket(client, project, key)

    # A distinct reviewer leaves a trusted clean verdict bound to the head,
    # but no work-queue row, agent run, start-lease context, or resolver ever
    # records WHO implemented the ticket. Readiness must fail closed — an
    # unknown implementer can't be proven different from the reviewer.
    reviewer, _ = await _make_user(db_session, name="reviewer")
    await _seed_clean_verdict(
        db_session, ticket_id, reviewer, reviewed_commit=HEAD
    )

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    assert "implementer unknown" in body["reasons"]


@pytest.mark.asyncio
async def test_readiness_fails_closed_when_service_key_reviewer_is_minter_of_user_only_start(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket_id = await _make_ticket(client, project, key)

    # A service key minted by `minter` leaves a trusted clean verdict.
    minter, _ = await _make_user(db_session, name="minter")
    svc = await _make_service_key(db_session, minter)
    await _seed_clean_verdict(
        db_session,
        ticket_id,
        minter,
        reviewed_commit=HEAD,
        service_key_id=svc.id,
    )
    # The start-lease context is USER-ONLY (pre-109 shape): it stores the
    # key's backing user but no service_key_id. Because a service key IS its
    # minter, this is the SAME identity and must fail closed.
    await _seed_start_context(
        db_session, ticket_id, user_id=minter.id, service_key_id=None
    )

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    assert any(
        "reviewer identity matches an implementer identity" in r
        for r in body["reasons"]
    )


@pytest.mark.asyncio
async def test_readiness_true_when_user_only_start_is_not_the_reviewer_minter(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket_id = await _make_ticket(client, project, key)

    reviewer, _ = await _make_user(db_session, name="reviewer")
    implementer, _ = await _make_user(db_session, name="implementer")
    svc = await _make_service_key(db_session, reviewer)
    await _seed_clean_verdict(
        db_session,
        ticket_id,
        reviewer,
        reviewed_commit=HEAD,
        service_key_id=svc.id,
    )
    # User-only start context whose user is NOT the reviewer key's minter —
    # an independent implementer identity, so readiness still holds.
    await _seed_start_context(
        db_session, ticket_id, user_id=implementer.id, service_key_id=None
    )

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["reasons"] == []


@pytest.mark.asyncio
async def test_readiness_false_when_human_reviewer_minted_the_implementer_key(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket_id = await _make_ticket(client, project, key)

    # A human reviewer leaves a strict clean verdict, and the work was done
    # by a service key THEY minted. The key IS its minter, so reviewing work
    # their own key did is self-review and must fail closed.
    reviewer, _ = await _make_user(db_session, name="reviewer")
    svc = await _make_service_key(db_session, reviewer)
    await _seed_clean_verdict(
        db_session, ticket_id, reviewer, reviewed_commit=HEAD
    )
    await _seed_implementer(
        db_session, project, ticket_id, user_id=None, service_key_id=svc.id
    )

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    assert any(
        "reviewer identity matches an implementer identity" in r
        for r in body["reasons"]
    )


@pytest.mark.asyncio
async def test_readiness_true_when_implementer_key_minted_by_someone_else(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket_id = await _make_ticket(client, project, key)

    reviewer, _ = await _make_user(db_session, name="reviewer")
    implementer, _ = await _make_user(db_session, name="implementer")
    # The implementer service key is minted by a DIFFERENT user than the
    # human reviewer, so the reviewer is not reviewing their own work.
    svc = await _make_service_key(db_session, implementer)
    await _seed_clean_verdict(
        db_session, ticket_id, reviewer, reviewed_commit=HEAD
    )
    await _seed_implementer(
        db_session, project, ticket_id, user_id=None, service_key_id=svc.id
    )

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["reasons"] == []


@pytest.mark.asyncio
async def test_readiness_fails_closed_when_implementer_key_minter_unresolvable(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket_id = await _make_ticket(client, project, key)

    reviewer, _ = await _make_user(db_session, name="reviewer")
    await _seed_clean_verdict(
        db_session, ticket_id, reviewer, reviewed_commit=HEAD
    )
    # An implementer service-key id with no ApiKey row — the key's minter
    # cannot be resolved, so readiness must fail closed rather than treat an
    # anonymous key as independent of the reviewer.
    await _seed_implementer(
        db_session, project, ticket_id, user_id=None, service_key_id="sk_missing"
    )

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    assert any(
        "reviewer identity matches an implementer identity" in r
        for r in body["reasons"]
    )


@pytest.mark.asyncio
async def test_readiness_outsider_gets_403(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    owner_key = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=owner.id,
            key_hash=hash_api_key(owner_key),
            name="owner-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    ticket_id = await _make_ticket(client, project, owner_key)

    # An outsider with no read standing on the project hits the SAME
    # project-access gate as GET ticket (403), never leaking the ticket.
    outsider, outsider_key = await _make_user(db_session, name="outsider")
    resp = await _readiness(client, project, ticket_id, outsider_key, HEAD)
    assert resp.status_code == 403
    assert outsider.id not in {owner.id}


@pytest.mark.asyncio
async def test_readiness_cross_org_service_key_gets_opaque_404(
    client: AsyncClient, db_session: AsyncSession
):
    """Review checklist #2 — a service key whose org does not include the
    project gets a 404 that is byte-identical to a project miss, never a
    cross_org_denied probe."""
    from sessionfs.server.db.models import Organization

    owner, key = await _make_user(db_session)
    org_a = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name=f"OrgA-{uuid.uuid4().hex[:6]}",
        slug=f"oa-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(org_a)
    await db_session.commit()
    await db_session.refresh(org_a)

    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"ready-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/r-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
        org_id=org_a.id,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    ticket_id = await _make_ticket(client, project, key)

    other_user, _ = await _make_user(db_session, name="other")
    org_b = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name=f"OrgB-{uuid.uuid4().hex[:6]}",
        slug=f"ob-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(org_b)
    await db_session.commit()
    await db_session.refresh(org_b)

    raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=other_user.id,
            key_hash=hash_api_key(raw),
            name="svc-cross",
            key_kind="service",
            org_id=org_b.id,
            scopes='["tickets:read"]',
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    resp = await _readiness(client, project, ticket_id, raw, HEAD)
    assert resp.status_code == 404
    assert resp.json() == {
        "error": {"code": "404", "details": {}, "message": "Project not found"}
    }


@pytest.mark.asyncio
async def test_readiness_fails_closed_when_human_reviewer_minted_the_implementer_key(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket_id = await _make_ticket(client, project, key)

    # The implementer is a service key minted by `reviewer`; the same person
    # then leaves a trusted clean verdict with their own user key. A service
    # key IS its minter, so this is self-review and must not be ready.
    reviewer, _ = await _make_user(db_session, name="reviewer")
    svc = await _make_service_key(db_session, reviewer)
    await _seed_clean_verdict(
        db_session, ticket_id, reviewer, reviewed_commit=HEAD
    )
    await _seed_implementer(
        db_session, project, ticket_id, user_id=None, service_key_id=svc.id
    )

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    assert any(
        "reviewer identity matches an implementer identity" in r
        for r in body["reasons"]
    )


@pytest.mark.asyncio
async def test_readiness_true_when_service_key_implementer_minted_by_someone_else(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket_id = await _make_ticket(client, project, key)

    reviewer, _ = await _make_user(db_session, name="reviewer")
    implementer, _ = await _make_user(db_session, name="implementer")
    svc = await _make_service_key(db_session, implementer)
    await _seed_clean_verdict(
        db_session, ticket_id, reviewer, reviewed_commit=HEAD
    )
    await _seed_implementer(
        db_session, project, ticket_id, user_id=implementer.id, service_key_id=svc.id
    )

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["reasons"] == []


@pytest.mark.asyncio
async def test_readiness_false_when_reviewer_and_implementer_keys_share_minter(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket_id = await _make_ticket(client, project, key)

    # A service key IS its minter. Two DIFFERENT keys minted by the same user
    # are therefore the same identity — the reviewer is reviewing work their
    # own key did, so readiness must fail closed.
    minter, _ = await _make_user(db_session, name="minter")
    reviewer_svc = await _make_service_key(db_session, minter)
    impl_svc = await _make_service_key(db_session, minter)
    await _seed_clean_verdict(
        db_session,
        ticket_id,
        minter,
        reviewed_commit=HEAD,
        service_key_id=reviewer_svc.id,
    )
    await _seed_implementer(
        db_session, project, ticket_id, user_id=None, service_key_id=impl_svc.id
    )

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    assert any(
        "reviewer identity matches an implementer identity" in r
        for r in body["reasons"]
    )


@pytest.mark.asyncio
async def test_readiness_true_when_reviewer_and_implementer_keys_have_distinct_minters(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket_id = await _make_ticket(client, project, key)

    reviewer, _ = await _make_user(db_session, name="reviewer")
    implementer, _ = await _make_user(db_session, name="implementer")
    reviewer_svc = await _make_service_key(db_session, reviewer)
    impl_svc = await _make_service_key(db_session, implementer)
    await _seed_clean_verdict(
        db_session,
        ticket_id,
        reviewer,
        reviewed_commit=HEAD,
        service_key_id=reviewer_svc.id,
    )
    await _seed_implementer(
        db_session, project, ticket_id, user_id=None, service_key_id=impl_svc.id
    )

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["reasons"] == []


@pytest.mark.asyncio
async def test_readiness_fails_closed_when_service_key_reviewer_and_unresolvable_implementer_key(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket_id = await _make_ticket(client, project, key)

    # Both identities are service keys, but the implementer key has no ApiKey
    # row. Its minter can't be resolved, so the two keys can't be proven
    # independent — fail closed.
    reviewer, _ = await _make_user(db_session, name="reviewer")
    reviewer_svc = await _make_service_key(db_session, reviewer)
    await _seed_clean_verdict(
        db_session,
        ticket_id,
        reviewer,
        reviewed_commit=HEAD,
        service_key_id=reviewer_svc.id,
    )
    await _seed_implementer(
        db_session, project, ticket_id, user_id=None, service_key_id="sk_missing"
    )

    resp = await _readiness(client, project, ticket_id, key, HEAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    assert any(
        "reviewer identity matches an implementer identity" in r
        for r in body["reasons"]
    )

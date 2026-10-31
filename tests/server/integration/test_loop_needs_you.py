"""Review Loop R3 (Brief C3) — the org-wide "Needs you" inbox tests.

Covers the /me/loops/needs-you feed end to end: ready / needs_human /
proposed_checklist items across every project the caller can read (and the
absence of projects they cannot), counts and the /count badge, service-key
403, editor vs captured-reader ``can_act``, and the no-per-project N+1
guarantee.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentRun,
    ApiKey,
    LoopChecklistItem,
    Organization,
    Project,
    Session,
    Ticket,
    TicketComment,
    User,
)
from sessionfs.server.services.loop_needs_you import needs_you

HEAD = "a1b2c3d4e5f6a7b8"
CLEAN = "Codex R1 review on tk_x: VERIFIED-CLEAN\n\nFindings: none."


async def _make_user(
    db: AsyncSession, name: str = "alice", tier: str = "team"
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
        name=f"loop-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/loop-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_parent(
    db: AsyncSession,
    project: Project,
    owner: User,
    config: dict,
    n: int = 1,
) -> tuple[Ticket, list[str]]:
    parent = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title=f"loop-{uuid.uuid4().hex[:6]}",
        kind="issue",
        status="open",
        created_by_user_id=owner.id,
        loop_config=json.dumps(config),
    )
    db.add(parent)
    await db.flush()
    children: list[str] = []
    for _ in range(n):
        child = Ticket(
            id=f"tk_{uuid.uuid4().hex[:16]}",
            project_id=project.id,
            title=f"child-{uuid.uuid4().hex[:6]}",
            kind="task",
            status="open",
            parent_ticket_id=parent.id,
            created_by_user_id=owner.id,
        )
        db.add(child)
        children.append(child.id)
    await db.commit()
    await db.refresh(parent)
    return parent, children


async def _make_run(
    db: AsyncSession,
    project: Project,
    *,
    ticket_id: str,
    input_tokens: int = 0,
    triggered_by_user_id: str | None = None,
) -> AgentRun:
    run = AgentRun(
        id=f"run_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        persona_name="atlas",
        role="implementer",
        ticket_id=ticket_id,
        head_sha=HEAD,
        branch="feat/loop",
        input_tokens=input_tokens,
        triggered_by_user_id=triggered_by_user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def _seed_clean_verdict(
    db: AsyncSession, ticket_id: str, reviewer: User
) -> TicketComment:
    c = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket_id,
        author_user_id=reviewer.id,
        author_persona="codex-reviewer",
        content=CLEAN,
        verdict_trusted=True,
        reviewed_commit=HEAD,
        created_at=datetime.now(timezone.utc),
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


async def _make_proposed_item(
    db: AsyncSession, project: Project, *, slug: str, title: str = "A trap"
) -> LoopChecklistItem:
    item = LoopChecklistItem(
        id=f"lci_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        slug=slug,
        title=title,
        check_text="Check it.",
        status="proposed",
        source="manual",
        times_found=0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def _seed_ready_child(
    db: AsyncSession, project: Project, owner: User, child_id: str
) -> None:
    """A trusted VERIFIED-CLEAN bound to head by a different identity."""
    await _make_run(db, project, ticket_id=child_id, triggered_by_user_id=owner.id)
    reviewer, _ = await _make_user(db, name="reviewer")
    await _seed_clean_verdict(db, child_id, reviewer)


async def _seed_token_hit_child(
    db: AsyncSession, project: Project, child_id: str
) -> None:
    """20 tokens against a 10-token budget → needs_human, never ready."""
    await _make_run(db, project, ticket_id=child_id, input_tokens=20)


def _by_kind(items: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for item in items:
        out.setdefault(item["kind"], []).append(item)
    return out


@pytest.mark.asyncio
async def test_needs_you_across_projects_hides_unreadable_and_counts(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    p1 = await _make_project(db_session, owner)
    p2 = await _make_project(db_session, owner)

    # p1: a ready child + a token-budget-hit child, plus a proposed trap.
    parent, children = await _make_parent(
        db_session, p1, owner, {"token_budget": 10, "reviewer_persona": "reviewer"}, n=2
    )
    await _seed_ready_child(db_session, p1, owner, children[0])
    await _seed_token_hit_child(db_session, p1, children[1])
    proposed = await _make_proposed_item(db_session, p1, slug="a-trap")

    # p2: a ready child.
    parent2, children2 = await _make_parent(
        db_session, p2, owner, {"reviewer_persona": "reviewer"}, n=1
    )
    await _seed_ready_child(db_session, p2, owner, children2[0])

    # A project the caller cannot read must never leak into the feed.
    other, _ = await _make_user(db_session, name="bob")
    p3 = await _make_project(db_session, other)
    parent3, children3 = await _make_parent(
        db_session, p3, other, {"reviewer_persona": "reviewer"}, n=1
    )
    await _seed_ready_child(db_session, p3, other, children3[0])

    resp = await client.get("/api/v1/me/loops/needs-you", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["counts"] == {
        "ready": 2,
        "needs_human": 1,
        "proposed_checklist": 1,
        "total": 4,
    }
    assert len(body["items"]) == 4

    project_ids = {item["project_id"] for item in body["items"]}
    assert project_ids == {p1.id, p2.id}
    assert p3.id not in project_ids

    by_kind = _by_kind(body["items"])
    ready = by_kind["ready"]
    assert {r["ticket_id"] for r in ready} == {children[0], children2[0]}
    for r in ready:
        assert r["kind"] == "ready"
        assert r["title"] == "Merge ready"
        assert r["reason"] == "Trusted VERIFIED-CLEAN bound to head"
        assert r["loop_id"] in (parent.id, parent2.id)
        assert r["ticket_title"].startswith("child-")
        assert r["can_act"] is True

    human = by_kind["needs_human"]
    assert len(human) == 1
    assert human[0]["ticket_id"] == children[1]
    assert human[0]["title"] == "Token budget hit"
    assert human[0]["reason"] == "token budget hit (20 tokens spent)"

    proposed_items = by_kind["proposed_checklist"]
    assert len(proposed_items) == 1
    assert proposed_items[0]["checklist_item_id"] == proposed.id
    assert proposed_items[0]["project_id"] == p1.id
    assert proposed_items[0]["title"] == "A trap"

    # Every item the owner acts on is editable.
    assert all(item["can_act"] for item in body["items"])


@pytest.mark.asyncio
async def test_needs_you_count(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session)
    p1 = await _make_project(db_session, owner)

    parent, children = await _make_parent(
        db_session, p1, owner, {"reviewer_persona": "reviewer"}, n=1
    )
    await _seed_ready_child(db_session, p1, owner, children[0])
    await _make_proposed_item(db_session, p1, slug="a-trap")

    resp = await client.get("/api/v1/me/loops/needs-you/count", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"count": 2}


@pytest.mark.asyncio
async def test_needs_you_service_key_403(client: AsyncClient, db_session: AsyncSession):
    admin, _ = await _make_user(db_session, name="admin")
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name="Org",
        slug=f"o-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(org)
    await db_session.commit()

    raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=admin.id,
            key_hash=hash_api_key(raw),
            name="svc-key",
            is_active=True,
            key_kind="service",
            org_id=org.id,
            scopes=json.dumps(["loops:read"]),
            created_by_user_id=admin.id,
            service_key_name="svc-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    resp = await client.get("/api/v1/me/loops/needs-you", headers=_hdrs(raw))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "service_key_not_allowed"

    resp = await client.get("/api/v1/me/loops/needs-you/count", headers=_hdrs(raw))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_needs_you_editor_true_reader_false(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    parent, children = await _make_parent(
        db_session, project, owner, {"reviewer_persona": "reviewer"}, n=1
    )
    await _seed_ready_child(db_session, project, owner, children[0])

    # A captured-session reader can read the project but not edit it.
    reader, reader_key = await _make_user(db_session, name="reader")
    db_session.add(
        Session(
            id=f"ses_{uuid.uuid4().hex[:16]}",
            user_id=reader.id,
            title="synced from the same repo",
            tags="[]",
            source_tool="claude-code",
            blob_key="x",
            blob_size_bytes=0,
            etag="x",
            git_remote_normalized=project.git_remote_normalized,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            uploaded_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    owner_resp = await client.get("/api/v1/me/loops/needs-you", headers=_hdrs(owner_key))
    assert owner_resp.status_code == 200, owner_resp.text
    assert all(item["can_act"] for item in owner_resp.json()["items"])

    reader_resp = await client.get(
        "/api/v1/me/loops/needs-you", headers=_hdrs(reader_key)
    )
    assert reader_resp.status_code == 200, reader_resp.text
    reader_items = reader_resp.json()["items"]
    assert len(reader_items) == 1
    assert reader_items[0]["ticket_id"] == children[0]
    assert reader_items[0]["can_act"] is False


@pytest.mark.asyncio
async def test_needs_you_has_no_per_project_n_plus_one(db_engine, db_session):
    """The feed must issue a constant number of SQL statements whether the
    caller reads 1 project with 3 children or 3 projects with 1 child each —
    every record kind is fetched once across all projects, never per project."""
    owner_a, _ = await _make_user(db_session, name="alpha")
    project_a = await _make_project(db_session, owner_a)
    await _make_parent(
        db_session, project_a, owner_a, {"reviewer_persona": "reviewer"}, n=3
    )

    owner_b, _ = await _make_user(db_session, name="beta")
    for _ in range(3):
        project = await _make_project(db_session, owner_b)
        await _make_parent(
            db_session, project, owner_b, {"reviewer_persona": "reviewer"}, n=1
        )

    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", _capture)
    try:
        await needs_you(db_session, user_id=owner_a.id)
        count_1_project = len(captured)
        captured.clear()
        await needs_you(db_session, user_id=owner_b.id)
        count_3_projects = len(captured)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _capture)

    assert count_1_project == count_3_projects, captured

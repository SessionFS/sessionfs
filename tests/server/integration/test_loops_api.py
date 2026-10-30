"""Review Loop R3 (Brief D1) — loop start + status API tests.

Covers the two loop surfaces end to end:
- POST /loops: create a parent issue + child tasks + review_until_clean
  queue; adopt an existing issue; dollar-budget admin gate; reviewer
  trusted/untrusted hint.
- GET /loops + /loops/{id}: status shape matching
  ``dashboard/src/api/reviewLoop.ts`` (rounds / harmful / ready / verdict /
  not_ready_reasons), existence-oracle 404 for outsiders and other-org
  members, service-key scope boundaries, and no per-child N+1.
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
    OrgMember,
    Organization,
    Project,
    Ticket,
    TicketComment,
    TicketEvent,
    TicketWatcher,
    TrustedReviewer,
    User,
)
from sessionfs.server.services.loops import load_loops

HEAD = "a1b2c3d4e5f6a7b8"
CLEAN = "Codex R1 review on tk_x: VERIFIED-CLEAN\n\nFindings: none."
HARMFUL_CLEAN = (
    "Codex R1 review on tk_x: VERIFIED-CLEAN\n\n"
    "Findings:\n\n • HIGH — crash risk in src/foo.py:10 harm: crash\n"
)


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


async def _make_org(db: AsyncSession) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name=f"Org-{uuid.uuid4().hex[:6]}",
        slug=f"o-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


async def _add_org_member(db: AsyncSession, org: Organization, user: User, role: str) -> None:
    db.add(
        OrgMember(
            org_id=org.id,
            user_id=user.id,
            role=role,
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def _make_project(
    db: AsyncSession, owner: User, org: Organization | None = None
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"loop-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/loop-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_service_key(
    db: AsyncSession,
    org: Organization,
    minter: User,
    scopes: list[str],
    *,
    project_ids: list[str] | None = None,
) -> tuple[ApiKey, str]:
    raw = generate_api_key()
    row = ApiKey(
        id=str(uuid.uuid4()),
        user_id=minter.id,
        key_hash=hash_api_key(raw),
        name=f"svc-{uuid.uuid4().hex[:6]}",
        is_active=True,
        key_kind="service",
        org_id=org.id,
        scopes=json.dumps(scopes),
        created_by_user_id=minter.id,
        service_key_name=f"svc-{uuid.uuid4().hex[:6]}",
        project_ids=json.dumps(project_ids) if project_ids else None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row, raw


async def _make_run(
    db: AsyncSession,
    project: Project,
    *,
    ticket_id: str,
    head_sha: str = HEAD,
    branch: str = "feat/loop",
    input_tokens: int = 0,
    output_tokens: int = 0,
    role: str | None = "implementer",
    cost_basis: str | None = None,
    cost_usd: str | None = None,
) -> AgentRun:
    run = AgentRun(
        id=f"run_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        persona_name="atlas",
        role=role,
        ticket_id=ticket_id,
        head_sha=head_sha,
        branch=branch,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_basis=cost_basis,
        cost_usd=cost_usd,
        created_at=datetime.now(timezone.utc),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


def _loop_body(tickets: list[dict], reviewer_persona: str = "reviewer", **extra) -> dict:
    body: dict = {"tickets": tickets, "reviewer_persona": reviewer_persona}
    body.update(extra)
    return body


async def _seed_clean_verdict(
    db: AsyncSession,
    ticket_id: str,
    reviewer: User,
    *,
    content: str = CLEAN,
    reviewed_commit: str = HEAD,
) -> TicketComment:
    c = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket_id,
        author_user_id=reviewer.id,
        author_persona="codex-reviewer",
        content=content,
        verdict_trusted=True,
        reviewed_commit=reviewed_commit,
        created_at=datetime.now(timezone.utc),
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


async def _set_implementer(
    db: AsyncSession, project: Project, ticket_id: str, user: User
) -> None:
    """Mark ``user`` as the implementer of ``ticket_id``.

    The loop POST already created a ``review_until_clean`` work-queue item
    for the child (implementer NULL); update that row in place rather than
    add a second item (the ``(work_queue_id, ticket_id)`` unique constraint
    would reject a duplicate in the same queue).
    """
    from sqlalchemy import select as _select

    from sessionfs.server.db.models import WorkQueueItem

    items = (
        await db.execute(
            _select(WorkQueueItem).where(WorkQueueItem.ticket_id == ticket_id)
        )
    ).scalars().all()
    assert items, f"no work-queue item for {ticket_id}"
    items[0].implementer_user_id = user.id
    await db.commit()


async def _start_loop(
    client: AsyncClient, project: Project, key: str, body: dict
):
    return await client.post(
        f"/api/v1/projects/{project.id}/loops", headers=_hdrs(key), json=body
    )


# ── create ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_loop_files_children_and_queue(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    resp = await _start_loop(
        client,
        project,
        key,
        _loop_body(
            [
                {"title": "Child one", "description": "do A"},
                {"title": "Child two", "description": "do B", "acceptance_criteria": ["x"]},
            ],
            title="My review loop",
            token_budget=5000,
            max_review_rounds=3,
        ),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["loop_id"].startswith("tk_")
    assert body["queue_id"].startswith("wq_")
    assert body["reviewer"] == {
        "persona": "reviewer",
        "trusted": False,
        "setup_hint": (
            "No active trusted reviewer is registered for persona 'reviewer' "
            "in this project (or org-wide). Register one before the loop can "
            "self-close."
        ),
    }
    assert body["env"]["implementer"] == {"SESSIONFS_PROFILE": "implementer"}
    assert body["env"]["reviewer"] == {"SESSIONFS_PROFILE": "reviewer"}

    tickets = body["tickets"]
    assert [t["title"] for t in tickets] == ["Child one", "Child two"]
    assert all(t["id"].startswith("tk_") for t in tickets)
    # acceptance criteria rendered into the brief markdown
    assert "## Acceptance criteria\n- x" in tickets[1]["brief_markdown"]

    # The parent is an issue carrying the loop_config JSON; children are tasks.
    parent = await db_session.get(Ticket, body["loop_id"])
    config = json.loads(parent.loop_config)
    assert config["queue_id"] == body["queue_id"]
    assert config["reviewer_persona"] == "reviewer"
    assert config["token_budget"] == 5000
    assert config["max_review_rounds"] == 3
    for t in tickets:
        child = await db_session.get(Ticket, t["id"])
        assert child.kind == "task"
        assert child.parent_ticket_id == parent.id


@pytest.mark.asyncio
async def test_create_loop_reviewer_trusted_flips_hint(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    # Untrusted first — setup hint present.
    resp = await _start_loop(
        client,
        project,
        key,
        _loop_body([{"title": "c1"}], reviewer_persona="codex", title="Loop"),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["reviewer"]["trusted"] is False
    assert resp.json()["reviewer"]["setup_hint"]

    # Register the persona as a trusted reviewer for this project.
    db_session.add(
        TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            project_id=project.id,
            user_id=owner.id,
            reviewer_persona="codex",
            is_active=True,
            created_by_user_id=owner.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    resp2 = await _start_loop(
        client,
        project,
        key,
        _loop_body([{"title": "c2"}], reviewer_persona="codex", title="Loop 2"),
    )
    assert resp2.status_code == 201, resp2.text
    assert resp2.json()["reviewer"]["trusted"] is True
    assert resp2.json()["reviewer"]["setup_hint"] is None


# ── adopt an existing issue ───────────────────────────────────────


@pytest.mark.asyncio
async def test_adopt_existing_issue(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Loop parent", "kind": "issue"},
    )
    assert create.status_code == 201, create.text
    parent_id = create.json()["id"]

    resp = await _start_loop(
        client,
        project,
        key,
        _loop_body([{"title": "child"}], from_ticket_id=parent_id),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["loop_id"] == parent_id

    parent = await db_session.get(Ticket, parent_id)
    assert parent.title == "Loop parent"
    assert json.loads(parent.loop_config)["reviewer_persona"] == "reviewer"


@pytest.mark.asyncio
async def test_adopt_non_issue_422(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "a task"},
    )
    task_id = create.json()["id"]

    resp = await _start_loop(
        client, project, key, _loop_body([{"title": "child"}], from_ticket_id=task_id)
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_adopt_already_a_loop_409(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    first = await _start_loop(
        client, project, key, _loop_body([{"title": "child"}], title="Loop")
    )
    assert first.status_code == 201, first.text
    loop_id = first.json()["loop_id"]

    resp = await _start_loop(
        client, project, key, _loop_body([{"title": "child"}], from_ticket_id=loop_id)
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_adopt_loses_claim_when_parent_flipped_after_load(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Review checklist #11 — adopting an issue claims the parent with a
    rowcount-1 guarded ``UPDATE ... WHERE loop_config IS NULL``. When a
    competing adopter wins the claim between the parent load and that UPDATE,
    the loser must abort with 409 and NOT stand up a second queue (only the
    winner's queue exists)."""
    from sqlalchemy import update

    from sessionfs.server.db.models import WorkQueue
    from sessionfs.server.routes import loops as loops_routes

    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Loop parent", "kind": "issue"},
    )
    assert create.status_code == 201, create.text
    parent_id = create.json()["id"]

    # The concurrent winner already claimed the parent and stood up its queue.
    winner_queue = WorkQueue(
        id=f"wq_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name=f"winner-{uuid.uuid4().hex[:6]}",
        mode="review_until_clean",
        selector=json.dumps({"ticket_ids": []}),
        auto_adopt=True,
        max_adopt_per_wake=5,
        stop_condition="all_clean",
        cadence_seconds=300,
        max_tickets_per_run=1,
        max_attempts_per_item=3,
        status="active",
        lease_epoch=0,
        created_by_user_id=owner.id,
    )
    db_session.add(winner_queue)
    await db_session.commit()

    real_load = loops_routes._load_loop_ticket

    async def flip_after_load(db, project_id, ticket_id):
        parent = await real_load(db, project_id, ticket_id)
        # The competing adopter flips loop_config to non-NULL right after this
        # route read the parent but before its guarded UPDATE. synchronize_session
        # is disabled so the loaded ORM object still looks unclaimed (this is the
        # stale read the old read-check-write code would have trusted).
        await db.execute(
            update(Ticket)
            .where(Ticket.id == parent.id)
            .values(loop_config=json.dumps({"queue_id": winner_queue.id}))
            .execution_options(synchronize_session=False)
        )
        return parent

    monkeypatch.setattr(loops_routes, "_load_loop_ticket", flip_after_load)

    resp = await _start_loop(
        client,
        project,
        key,
        _loop_body([{"title": "child"}], from_ticket_id=parent_id),
    )
    assert resp.status_code == 409, resp.text

    queues = (await db_session.execute(select(WorkQueue))).scalars().all()
    assert [q.id for q in queues] == [winner_queue.id]


async def test_create_loop_files_create_events_and_subscribes_creator(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    resp = await _start_loop(
        client,
        project,
        key,
        _loop_body([{"title": "child one"}, {"title": "child two"}], title="Loop"),
    )
    assert resp.status_code == 201, resp.text
    started = resp.json()
    loop_id = started["loop_id"]
    child_ids = [t["id"] for t in started["tickets"]]

    # Parent + every child get a timeline ``created`` event.
    for ticket_id in [loop_id, *child_ids]:
        actions = (
            (
                await db_session.execute(
                    select(TicketEvent.action).where(TicketEvent.ticket_id == ticket_id)
                )
            )
            .scalars()
            .all()
        )
        assert actions == ["created"], ticket_id

    # The creator follows the parent and every child (source="creator").
    for ticket_id in [loop_id, *child_ids]:
        watcher = await db_session.get(TicketWatcher, (ticket_id, owner.id))
        assert watcher is not None, ticket_id
        assert watcher.source == "creator"


# ── create validation (Brief D2) ─────────────────────────────────


@pytest.mark.asyncio
async def test_create_loop_rejects_bool_and_float_budgets(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    for bad in (
        {"token_budget": True},
        {"max_review_rounds": False},
        {"cost_budget_usd": True},
    ):
        resp = await _start_loop(
            client, project, key, _loop_body([{"title": "c"}], title="Loop", **bad)
        )
        assert resp.status_code == 422, (bad, resp.text)


@pytest.mark.asyncio
async def test_create_loop_rejects_oversized_fields(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    cases = [
        _loop_body([{"title": "c"}] * 51, title="Loop"),
        _loop_body([{"title": "c"}], title="x" * 201),
        _loop_body([{"title": "c", "description": "x" * 20001}], title="Loop"),
        _loop_body([{"title": "c", "acceptance_criteria": ["x"] * 51}], title="Loop"),
        _loop_body(
            [{"title": "c", "acceptance_criteria": ["x" * 1001]}], title="Loop"
        ),
    ]
    for body in cases:
        resp = await _start_loop(client, project, key, body)
        assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_loop_sanitizes_text_fields(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    aws_key = "AKIAIOSFODNN7EXAMPLE"
    resp = await _start_loop(
        client,
        project,
        key,
        _loop_body(
            [
                {
                    "title": f"leak {aws_key}\x00",
                    "description": f"desc with {aws_key}",
                    "acceptance_criteria": ["plain"],
                }
            ],
            title="Loop",
        ),
    )
    assert resp.status_code == 201, resp.text
    child_id = resp.json()["tickets"][0]["id"]
    child = await db_session.get(Ticket, child_id)

    assert aws_key not in child.title
    assert "\x00" not in child.title
    assert "[REDACTED:aws_access_key_id]" in child.title
    assert aws_key not in child.description
    assert "[REDACTED:aws_access_key_id]" in child.description


# ── dollar budget admin gate ──────────────────────────────────────


@pytest.mark.asyncio
async def test_cost_budget_refused_for_non_admin(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session, name="admin")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)

    member, member_key = await _make_user(db_session, name="member")
    await _add_org_member(db_session, org, member, "member")

    resp = await _start_loop(
        client,
        project,
        member_key,
        _loop_body([{"title": "child"}], title="Loop", cost_budget_usd=12.5),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_cost_budget_ok_for_org_admin(
    client: AsyncClient, db_session: AsyncSession
):
    admin, admin_key = await _make_user(db_session, name="admin")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)

    resp = await _start_loop(
        client,
        project,
        admin_key,
        _loop_body([{"title": "child"}], title="Loop", cost_budget_usd=12.5),
    )
    assert resp.status_code == 201, resp.text
    parent = await db_session.get(Ticket, resp.json()["loop_id"])
    assert json.loads(parent.loop_config)["cost_budget_usd"] == 12.5


# ── status shape ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_shape_matches_dashboard_contract(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    resp = await _start_loop(
        client,
        project,
        key,
        _loop_body(
            [{"title": "clean"}, {"title": "harmful"}],
            title="Status loop",
            token_budget=1000,
        ),
    )
    assert resp.status_code == 201, resp.text
    started = resp.json()
    loop_id = started["loop_id"]
    clean_id, harmful_id = [t["id"] for t in started["tickets"]]

    reviewer, _ = await _make_user(db_session, name="reviewer")

    # Clean child: trusted VERIFIED-CLEAN bound to head, different implementer.
    await _make_run(db_session, project, ticket_id=clean_id, input_tokens=10)
    await _seed_clean_verdict(db_session, clean_id, reviewer)
    await _set_implementer(db_session, project, clean_id, owner)

    # Harmful child: same trusted clean verdict, but its own round raises a
    # HIGH harm:crash finding that never gets closed (no later clean round).
    await _make_run(db_session, project, ticket_id=harmful_id, input_tokens=20)
    await _seed_clean_verdict(db_session, harmful_id, reviewer, content=HARMFUL_CLEAN)
    await _set_implementer(db_session, project, harmful_id, owner)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/loops/{loop_id}", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    loop = resp.json()

    assert loop["id"] == loop_id
    assert loop["title"] == "Status loop"
    children = {c["title"]: c for c in loop["children"]}
    assert set(children) == {"clean", "harmful"}

    clean = children["clean"]
    assert clean["ticket_id"] == clean_id
    assert clean["status"] == "open"
    assert clean["branch"] == "feat/loop"
    assert clean["head_sha"] == HEAD
    assert clean["round"] == 1
    assert clean["harmful_open"] == 0
    assert clean["findings_by_harm"] == {}
    assert clean["rounds"] == []
    assert clean["tokens"] == 10
    assert clean["budget"] == {"tokens": 1000}
    assert clean["ready"] is True
    assert clean["verdict"]["reviewed_commit"] == HEAD
    assert clean["verdict"]["reviewer"] == reviewer.id
    assert clean["not_ready_reasons"] == []

    harmful = children["harmful"]
    assert harmful["harmful_open"] == 1
    assert harmful["findings_by_harm"] == {"crash": 1}
    assert harmful["rounds"] == [{"round": 1, "harmful": 1}]
    assert harmful["tokens"] == 20
    assert harmful["ready"] is False
    assert harmful["verdict"]["reviewed_commit"] == HEAD
    assert harmful["not_ready_reasons"] == ["1 open harmful finding(s)"]


@pytest.mark.asyncio
async def test_status_cost_matches_budget_engine_cost_basis(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    resp = await _start_loop(
        client, project, key, _loop_body([{"title": "c"}], title="Loop")
    )
    assert resp.status_code == 201, resp.text
    loop_id = resp.json()["loop_id"]
    child_id = resp.json()["tickets"][0]["id"]

    # reported dollars count toward the loop's cost; plan/unknown do not
    # (the budget engine only counts reported|estimated — the status view
    # must agree with it).
    await _make_run(
        db_session, project, ticket_id=child_id,
        cost_basis="reported", cost_usd="0.500000",
    )
    await _make_run(
        db_session, project, ticket_id=child_id,
        cost_basis="plan", cost_usd="9.990000",
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/loops/{loop_id}", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    child = resp.json()["children"][0]
    assert child["tokens"] == 0
    assert child["cost_usd"] == pytest.approx(0.5, abs=1e-6)


async def test_list_loops_returns_all_parents(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    await _start_loop(client, project, key, _loop_body([{"title": "c"}], title="A"))
    await _start_loop(client, project, key, _loop_body([{"title": "c"}], title="B"))

    resp = await client.get(f"/api/v1/projects/{project.id}/loops", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    loops = resp.json()
    assert {loop["title"] for loop in loops} == {"A", "B"}
    assert all("children" in loop for loop in loops)


# ── existence oracle + service-key scopes ─────────────────────────


@pytest.mark.asyncio
async def test_outsider_and_other_org_get_404(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session)
    org_b = await _make_org(db_session)
    project = await _make_project(db_session, owner, org=org_b)
    await _start_loop(
        client, project, owner_key, _loop_body([{"title": "c"}], title="Loop")
    )

    outsider, outsider_key = await _make_user(db_session, name="outsider")
    resp = await client.get(
        f"/api/v1/projects/{project.id}/loops", headers=_hdrs(outsider_key)
    )
    assert resp.status_code == 404

    org_a = await _make_org(db_session)
    other_member, other_key = await _make_user(db_session, name="other")
    await _add_org_member(db_session, org_a, other_member, "member")
    resp2 = await client.get(
        f"/api/v1/projects/{project.id}/loops", headers=_hdrs(other_key)
    )
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_service_key_cross_org_read_gets_opaque_404(
    client: AsyncClient, db_session: AsyncSession
):
    """Review checklist #2 — a service key whose org (or allowlist) does not
    include the project gets a 404 that is byte-identical to a project miss,
    never a cross_org_denied / project_not_in_allowlist probe."""
    admin, _ = await _make_user(db_session, name="admin")
    org_a = await _make_org(db_session)
    await _add_org_member(db_session, org_a, admin, "admin")
    project = await _make_project(db_session, admin, org=org_a)

    other_admin, _ = await _make_user(db_session, name="other-admin")
    org_b = await _make_org(db_session)
    await _add_org_member(db_session, org_b, other_admin, "admin")
    _, svc_key = await _make_service_key(
        db_session, org_b, other_admin, scopes=["loops:read"]
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/loops", headers=_hdrs(svc_key)
    )
    assert resp.status_code == 404
    assert resp.json() == {
        "error": {"code": "404", "details": {}, "message": "Loop not found"}
    }


@pytest.mark.asyncio
async def test_loop_routes_share_one_404_body_across_miss_kinds(
    client: AsyncClient, db_session: AsyncSession
):
    """Review checklist #2 — a loop route's project / loop / child / queue-item
    misses are byte-identical 404s, so none of those ids can be probed."""
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    resp = await _start_loop(
        client, project, key, _loop_body([{"title": "child"}], title="Loop")
    )
    assert resp.status_code == 201, resp.text
    loop_id = resp.json()["loop_id"]

    loop_404 = {
        "error": {"code": "404", "details": {}, "message": "Loop not found"}
    }

    # loop miss (read)
    r = await client.get(
        f"/api/v1/projects/{project.id}/loops/tk_missing", headers=_hdrs(key)
    )
    assert r.status_code == 404
    assert r.json() == loop_404

    # project miss (read)
    r = await client.get(
        f"/api/v1/projects/proj_missing/loops", headers=_hdrs(key)
    )
    assert r.status_code == 404
    assert r.json() == loop_404

    # project miss (write)
    r = await client.patch(
        f"/api/v1/projects/proj_missing/loops/{loop_id}",
        headers=_hdrs(key),
        json={"token_budget": 1},
    )
    assert r.status_code == 404
    assert r.json() == loop_404

    # child miss — a ticket id that isn't a child of the loop
    r = await client.post(
        f"/api/v1/projects/{project.id}/loops/{loop_id}/children/tk_missing/close",
        headers=_hdrs(key),
    )
    assert r.status_code == 404
    assert r.json() == loop_404

    # queue-item miss — a real child ticket with no work-queue item
    orphan = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="orphan child",
        kind="task",
        parent_ticket_id=loop_id,
        status="open",
        created_by_user_id=owner.id,
    )
    db_session.add(orphan)
    await db_session.commit()
    r = await client.post(
        f"/api/v1/projects/{project.id}/loops/{loop_id}/children/{orphan.id}/close",
        headers=_hdrs(key),
    )
    assert r.status_code == 404
    assert r.json() == loop_404


@pytest.mark.asyncio
async def test_missing_loop_on_gated_project_returns_404_not_plan_error(
    client: AsyncClient, db_session: AsyncSession
):
    """Review checklist #2 — the plan gate runs AFTER loop visibility, so a
    project without ``agent_tickets`` still 404s on a missing loop instead of
    leaking its plan via ``upgrade_required``."""
    owner, key = await _make_user(db_session, tier="pro")  # no agent_tickets
    project = await _make_project(db_session, owner)  # personal, owner tier=pro

    r = await client.get(
        f"/api/v1/projects/{project.id}/loops/tk_missing", headers=_hdrs(key)
    )
    assert r.status_code == 404
    assert r.json() == {
        "error": {"code": "404", "details": {}, "message": "Loop not found"}
    }


@pytest.mark.asyncio
async def test_service_key_write_and_read_scopes(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session, name="admin")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)

    # write scope → can start a loop
    _, write_key = await _make_service_key(
        db_session,
        org,
        admin,
        scopes=["loops:write", "tickets:write", "work_queues:write"],
    )
    resp = await _start_loop(
        client, project, write_key, _loop_body([{"title": "c"}], title="Loop")
    )
    assert resp.status_code == 201, resp.text
    loop_id = resp.json()["loop_id"]

    # read scope → can read status
    _, read_key = await _make_service_key(
        db_session, org, admin, scopes=["loops:read"]
    )
    resp = await client.get(
        f"/api/v1/projects/{project.id}/loops/{loop_id}", headers=_hdrs(read_key)
    )
    assert resp.status_code == 200, resp.text

    # write-only key cannot read
    resp = await client.get(
        f"/api/v1/projects/{project.id}/loops/{loop_id}", headers=_hdrs(write_key)
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_start_loop_service_key_requires_downstream_scopes_and_records_provenance(
    client: AsyncClient, db_session: AsyncSession
):
    from sessionfs.server.db.models import WorkQueue

    admin, _ = await _make_user(db_session, name="admin")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)

    # A service key with only loops:write cannot start a loop — the loop files
    # child tickets and stands up a work queue, so it needs those write scopes
    # too (same downstream-scope rule as the work-queue act paths).
    _, loops_only = await _make_service_key(
        db_session, org, admin, scopes=["loops:write"]
    )
    resp = await _start_loop(
        client, project, loops_only, _loop_body([{"title": "c"}], title="Loop")
    )
    assert resp.status_code == 403, resp.text

    # With all three scopes the loop starts, and the work queue records the
    # service-key provenance the same way the ticket create route does.
    svc_row, svc_key = await _make_service_key(
        db_session,
        org,
        admin,
        scopes=["loops:write", "tickets:write", "work_queues:write"],
    )
    resp = await _start_loop(
        client, project, svc_key, _loop_body([{"title": "c"}], title="Loop")
    )
    assert resp.status_code == 201, resp.text
    queue = (
        await db_session.execute(
            select(WorkQueue).where(WorkQueue.id == resp.json()["queue_id"])
        )
    ).scalars().one()
    assert queue.actor_type == "service_key"
    assert queue.service_key_id == svc_row.id
    assert queue.service_key_name == svc_row.service_key_name


@pytest.mark.asyncio
async def test_service_key_without_loops_scope_403(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session, name="admin")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)

    _, other_key = await _make_service_key(
        db_session, org, admin, scopes=["tickets:read"]
    )
    resp = await _start_loop(
        client, project, other_key, _loop_body([{"title": "c"}], title="Loop")
    )
    assert resp.status_code == 403


# ── no N+1 ────────────────────────────────────────────────────────


async def _make_parent_with_children(
    db: AsyncSession, project: Project, owner: User, n: int
) -> Ticket:
    parent = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title=f"parent-{n}",
        kind="issue",
        status="open",
        created_by_user_id=owner.id,
        loop_config=json.dumps({"queue_id": "wq_x", "reviewer_persona": "reviewer"}),
    )
    db.add(parent)
    await db.flush()
    for _ in range(n):
        db.add(
            Ticket(
                id=f"tk_{uuid.uuid4().hex[:16]}",
                project_id=project.id,
                title=f"child-{uuid.uuid4().hex[:6]}",
                kind="task",
                status="open",
                parent_ticket_id=parent.id,
                created_by_user_id=owner.id,
            )
        )
    await db.commit()
    await db.refresh(parent)
    return parent


@pytest.mark.asyncio
async def test_load_loops_has_no_per_child_n_plus_one(
    db_engine, db_session: AsyncSession
):
    """A loop rollup must issue a constant number of SQL statements no matter
    how many children it has (children, runs, comments, work-queue items are
    each fetched once across all children, plus the batched implementer
    identity + key-minter loads — a fixed count regardless of child count)."""
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    p3 = await _make_parent_with_children(db_session, project, owner, 3)
    p6 = await _make_parent_with_children(db_session, project, owner, 6)

    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", _capture)
    try:
        await load_loops(db_session, loop_tickets=[p3], show_dollars=False)
        count_3 = len(captured)
        captured.clear()
        await load_loops(db_session, loop_tickets=[p6], show_dollars=False)
        count_6 = len(captured)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _capture)

    assert count_3 == count_6 == 6, captured


# ── update (PATCH) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_loop_raises_and_clears_budgets(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    resp = await _start_loop(
        client,
        project,
        key,
        _loop_body([{"title": "c"}], title="Loop", token_budget=1000),
    )
    assert resp.status_code == 201, resp.text
    loop_id = resp.json()["loop_id"]

    # Raise both integer caps; cost is left alone (absent).
    resp = await client.patch(
        f"/api/v1/projects/{project.id}/loops/{loop_id}",
        headers=_hdrs(key),
        json={"token_budget": 5000, "max_review_rounds": 4},
    )
    assert resp.status_code == 200, resp.text
    parent = await db_session.get(Ticket, loop_id)
    config = json.loads(parent.loop_config)
    assert config["token_budget"] == 5000
    assert config["max_review_rounds"] == 4
    # Absent fields are untouched.
    assert "cost_budget_usd" not in config

    # Explicit null clears a cap.
    resp = await client.patch(
        f"/api/v1/projects/{project.id}/loops/{loop_id}",
        headers=_hdrs(key),
        json={"token_budget": None},
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(parent)
    config = json.loads(parent.loop_config)
    assert "token_budget" not in config
    assert config["max_review_rounds"] == 4


@pytest.mark.asyncio
async def test_update_loop_rejects_bool_and_float_budgets(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    resp = await _start_loop(
        client, project, key, _loop_body([{"title": "c"}], title="Loop")
    )
    assert resp.status_code == 201, resp.text
    loop_id = resp.json()["loop_id"]

    for bad in ({"token_budget": True}, {"token_budget": 1.5}, {"max_review_rounds": False}):
        resp = await client.patch(
            f"/api/v1/projects/{project.id}/loops/{loop_id}",
            headers=_hdrs(key),
            json=bad,
        )
        assert resp.status_code == 422, (bad, resp.text)


@pytest.mark.asyncio
async def test_update_loop_cost_budget_gate(
    client: AsyncClient, db_session: AsyncSession
):
    admin, admin_key = await _make_user(db_session, name="admin")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)

    member, member_key = await _make_user(db_session, name="member")
    await _add_org_member(db_session, org, member, "member")

    resp = await _start_loop(
        client, project, member_key, _loop_body([{"title": "c"}], title="Loop")
    )
    assert resp.status_code == 201, resp.text
    loop_id = resp.json()["loop_id"]

    # A plain member is an editor but not dollar-visible.
    resp = await client.patch(
        f"/api/v1/projects/{project.id}/loops/{loop_id}",
        headers=_hdrs(member_key),
        json={"cost_budget_usd": 12.5},
    )
    assert resp.status_code == 403

    # Members can still raise the token/round caps.
    resp = await client.patch(
        f"/api/v1/projects/{project.id}/loops/{loop_id}",
        headers=_hdrs(member_key),
        json={"token_budget": 9000},
    )
    assert resp.status_code == 200, resp.text

    # The admin can set the dollar budget.
    resp = await client.patch(
        f"/api/v1/projects/{project.id}/loops/{loop_id}",
        headers=_hdrs(admin_key),
        json={"cost_budget_usd": 12.5},
    )
    assert resp.status_code == 200, resp.text
    parent = await db_session.get(Ticket, loop_id)
    assert json.loads(parent.loop_config)["cost_budget_usd"] == 12.5


@pytest.mark.asyncio
async def test_update_loop_service_key_may_not_touch_dollars(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session, name="admin")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)

    _, svc_key = await _make_service_key(
        db_session,
        org,
        admin,
        scopes=["loops:write", "tickets:write", "work_queues:write"],
    )
    resp = await _start_loop(
        client, project, svc_key, _loop_body([{"title": "c"}], title="Loop")
    )
    assert resp.status_code == 201, resp.text
    loop_id = resp.json()["loop_id"]

    resp = await client.patch(
        f"/api/v1/projects/{project.id}/loops/{loop_id}",
        headers=_hdrs(svc_key),
        json={"cost_budget_usd": 12.5},
    )
    assert resp.status_code == 403

    # Token/round caps are fine for a loops:write service key.
    resp = await client.patch(
        f"/api/v1/projects/{project.id}/loops/{loop_id}",
        headers=_hdrs(svc_key),
        json={"token_budget": 9000},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_update_loop_404(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    resp = await client.patch(
        f"/api/v1/projects/{project.id}/loops/tk_missing",
        headers=_hdrs(key),
        json={"token_budget": 1},
    )
    assert resp.status_code == 404


# ── per-item close ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_needs_human_item_settles_queue_without_touching_ticket(
    client: AsyncClient, db_session: AsyncSession
):
    from sessionfs.server.db.models import WorkQueueItem

    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    resp = await _start_loop(
        client, project, key, _loop_body([{"title": "child"}], title="Loop")
    )
    assert resp.status_code == 201, resp.text
    loop_id = resp.json()["loop_id"]
    child_id = resp.json()["tickets"][0]["id"]

    item = (
        await db_session.execute(
            select(WorkQueueItem).where(WorkQueueItem.ticket_id == child_id)
        )
    ).scalars().one()
    item.item_status = "needs_human"
    await db_session.commit()

    child = await db_session.get(Ticket, child_id)
    assert child.status == "open"

    resp = await client.post(
        f"/api/v1/projects/{project.id}/loops/{loop_id}/children/{child_id}/close",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticket_id"] == child_id
    assert body["item_status"] == "done"
    assert body["closed_by"] == {"kind": "user", "user_id": owner.id}

    await db_session.refresh(item)
    assert item.item_status == "done"
    assert item.auto_close_review_kind == "human"
    assert item.closed_by_user_id == owner.id

    await db_session.refresh(child)
    assert child.status == "open"


@pytest.mark.asyncio
async def test_close_item_refuses_non_needs_human(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    resp = await _start_loop(
        client, project, key, _loop_body([{"title": "child"}], title="Loop")
    )
    assert resp.status_code == 201, resp.text
    loop_id = resp.json()["loop_id"]
    child_id = resp.json()["tickets"][0]["id"]

    # Item is still "pending" — closing it by hand must be refused.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/loops/{loop_id}/children/{child_id}/close",
        headers=_hdrs(key),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_close_item_service_key_rejected_people_only(
    client: AsyncClient, db_session: AsyncSession
):
    from sessionfs.server.db.models import WorkQueueItem

    admin, admin_key = await _make_user(db_session, name="admin")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)

    resp = await _start_loop(
        client, project, admin_key, _loop_body([{"title": "child"}], title="Loop")
    )
    assert resp.status_code == 201, resp.text
    loop_id = resp.json()["loop_id"]
    child_id = resp.json()["tickets"][0]["id"]

    item = (
        await db_session.execute(
            select(WorkQueueItem).where(WorkQueueItem.ticket_id == child_id)
        )
    ).scalars().one()
    item.item_status = "needs_human"
    await db_session.commit()

    # Closing a needs_human child is a people-only action: a service key with
    # every loop write scope is still rejected up front (service_key_not_allowed).
    _, svc_key = await _make_service_key(
        db_session,
        org,
        admin,
        scopes=["loops:write", "tickets:write", "work_queues:write"],
    )
    resp = await client.post(
        f"/api/v1/projects/{project.id}/loops/{loop_id}/children/{child_id}/close",
        headers=_hdrs(svc_key),
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "service_key_not_allowed"

    # The item was left untouched.
    await db_session.refresh(item)
    assert item.item_status == "needs_human"


@pytest.mark.asyncio
async def test_needs_human_status_exposes_spent_tokens(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    resp = await _start_loop(
        client,
        project,
        key,
        _loop_body([{"title": "over"}], title="Loop", token_budget=1000),
    )
    assert resp.status_code == 201, resp.text
    loop_id = resp.json()["loop_id"]
    child_id = resp.json()["tickets"][0]["id"]

    await _make_run(db_session, project, ticket_id=child_id, input_tokens=1000)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/loops/{loop_id}", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    child = resp.json()["children"][0]
    assert child["tokens"] == 1000
    assert child["needs_human"] == "token budget hit (1,000 tokens spent)"


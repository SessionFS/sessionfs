"""Agent-run spend rollups (Review Loop R3, Brief A2) — API tests.

Covers the project and ticket spend endpoints end to end:
- totals + each group_by (persona/model/ticket/role/day)
- date-range filtering
- dollar visibility (org admin / personal owner see dollars; plain
  org member and service keys see token totals only)
- existence-oracle 404 for outsiders and other-org members
- empty-project zeros
- one SQL rollup per request (no N+1)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import event
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
    User,
)
from sessionfs.server.services.agent_spend import rollup


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
        name=f"spend-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/spend-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_run(
    db: AsyncSession,
    project: Project,
    *,
    persona: str = "atlas",
    model: str | None = "claude-opus",
    role: str | None = "implementer",
    ticket_id: str | None = None,
    cost_basis: str | None = "reported",
    cost_usd: str | None = "0.000000",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    created_at: datetime | None = None,
) -> AgentRun:
    run = AgentRun(
        id=f"run_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        persona_name=persona,
        model=model,
        role=role,
        ticket_id=ticket_id,
        cost_basis=cost_basis,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def _make_ticket(db: AsyncSession, project: Project, owner: User) -> Ticket:
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title=f"spend ticket {uuid.uuid4().hex[:6]}",
        description="spend rollup coverage",
        created_by_user_id=owner.id,
        status="open",
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


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


# ── totals + currency split ───────────────────────────────────────


@pytest.mark.asyncio
async def test_project_totals_split_reported_vs_plan_dollars(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    await _make_run(
        db_session, project,
        cost_basis="reported", cost_usd="0.123456",
        input_tokens=100, output_tokens=50, cache_read_tokens=10, cache_write_tokens=5,
    )
    await _make_run(
        db_session, project,
        cost_basis="estimated", cost_usd="0.500000",
        input_tokens=200, output_tokens=100,
    )
    await _make_run(
        db_session, project,
        cost_basis="plan", cost_usd=None,
        input_tokens=1000, output_tokens=500, cache_read_tokens=100, cache_write_tokens=50,
    )
    await _make_run(
        db_session, project,
        cost_basis="unknown", cost_usd=None,
        input_tokens=10, output_tokens=20,
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/spend", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    totals = resp.json()["totals"]

    assert totals["runs"] == 4
    assert totals["input_tokens"] == 1310
    assert totals["output_tokens"] == 670
    assert totals["cache_read_tokens"] == 110
    assert totals["cache_write_tokens"] == 55
    # reported + estimated only.
    assert totals["cost_usd"] == pytest.approx(0.623456, abs=1e-6)
    # plan + unknown token totals, no dollars.
    assert totals["tokens_on_plans"] == 1680


# ── group_by ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_group_by_persona_model_role_day(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    await _make_run(db_session, project, persona="atlas", model="opus", role="implementer",
                    input_tokens=10, created_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    await _make_run(db_session, project, persona="scout", model="sonnet", role="reviewer",
                    input_tokens=20, created_at=datetime(2026, 10, 2, tzinfo=timezone.utc))

    by_persona = await client.get(
        f"/api/v1/projects/{project.id}/spend?group_by=persona", headers=_hdrs(key)
    )
    assert by_persona.status_code == 200, by_persona.text
    assert {g["key"] for g in by_persona.json()["groups"]} == {"atlas", "scout"}

    by_model = await client.get(
        f"/api/v1/projects/{project.id}/spend?group_by=model", headers=_hdrs(key)
    )
    assert {g["key"] for g in by_model.json()["groups"]} == {"opus", "sonnet"}

    by_role = await client.get(
        f"/api/v1/projects/{project.id}/spend?group_by=role", headers=_hdrs(key)
    )
    assert {g["key"] for g in by_role.json()["groups"]} == {"implementer", "reviewer"}

    by_day = await client.get(
        f"/api/v1/projects/{project.id}/spend?group_by=day", headers=_hdrs(key)
    )
    assert {g["key"] for g in by_day.json()["groups"]} == {"2026-10-01", "2026-10-02"}


@pytest.mark.asyncio
async def test_group_by_ticket(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    t1 = await _make_ticket(db_session, project, owner)
    t2 = await _make_ticket(db_session, project, owner)

    await _make_run(db_session, project, ticket_id=t1.id, input_tokens=5)
    await _make_run(db_session, project, ticket_id=t2.id, input_tokens=7)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/spend?group_by=ticket", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    assert {g["key"] for g in resp.json()["groups"]} == {t1.id, t2.id}


@pytest.mark.asyncio
async def test_group_by_invalid_value_422(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    resp = await client.get(
        f"/api/v1/projects/{project.id}/spend?group_by=nope", headers=_hdrs(key)
    )
    assert resp.status_code == 422


# ── date filter ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_date_filter_bounds_window(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    await _make_run(db_session, project, input_tokens=1,
                    created_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    await _make_run(db_session, project, input_tokens=2,
                    created_at=datetime(2026, 10, 15, tzinfo=timezone.utc))
    await _make_run(db_session, project, input_tokens=4,
                    created_at=datetime(2026, 10, 30, tzinfo=timezone.utc))

    resp = await client.get(
        f"/api/v1/projects/{project.id}/spend"
        "?from=2026-10-10T00:00:00&to=2026-10-20T00:00:00",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    totals = resp.json()["totals"]
    assert totals["runs"] == 1
    assert totals["input_tokens"] == 2


@pytest.mark.asyncio
async def test_date_filter_from_after_to_422(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    resp = await client.get(
        f"/api/v1/projects/{project.id}/spend"
        "?from=2026-10-20T00:00:00&to=2026-10-10T00:00:00",
        headers=_hdrs(key),
    )
    assert resp.status_code == 422


# ── access: existence oracle + dollar visibility ─────────────────


@pytest.mark.asyncio
async def test_outsider_and_other_org_get_404(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session)
    org_b = await _make_org(db_session)
    project = await _make_project(db_session, owner, org=org_b)
    await _make_run(db_session, project, input_tokens=1)

    # A user with no relation to the project.
    outsider, outsider_key = await _make_user(db_session)
    resp = await client.get(
        f"/api/v1/projects/{project.id}/spend", headers=_hdrs(outsider_key)
    )
    assert resp.status_code == 404

    # A member of a DIFFERENT org.
    org_a = await _make_org(db_session)
    other_member, other_key = await _make_user(db_session)
    await _add_org_member(db_session, org_a, other_member, "member")
    resp2 = await client.get(
        f"/api/v1/projects/{project.id}/spend", headers=_hdrs(other_key)
    )
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_org_member_sees_tokens_but_no_dollars(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)
    await _make_run(db_session, project, cost_basis="reported", cost_usd="1.500000",
                    input_tokens=42)

    member, member_key = await _make_user(db_session)
    await _add_org_member(db_session, org, member, "member")

    resp = await client.get(
        f"/api/v1/projects/{project.id}/spend", headers=_hdrs(member_key)
    )
    assert resp.status_code == 200, resp.text
    totals = resp.json()["totals"]
    assert totals["input_tokens"] == 42
    assert "cost_usd" not in totals


@pytest.mark.asyncio
async def test_org_admin_sees_dollars(client: AsyncClient, db_session: AsyncSession):
    admin, admin_key = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)
    await _make_run(db_session, project, cost_basis="reported", cost_usd="1.500000",
                    input_tokens=42)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/spend", headers=_hdrs(admin_key)
    )
    assert resp.status_code == 200, resp.text
    totals = resp.json()["totals"]
    assert totals["cost_usd"] == pytest.approx(1.5, abs=1e-6)


@pytest.mark.asyncio
async def test_personal_owner_sees_dollars(client: AsyncClient, db_session: AsyncSession):
    owner, owner_key = await _make_user(db_session)
    project = await _make_project(db_session, owner)  # org_id NULL
    await _make_run(db_session, project, cost_basis="reported", cost_usd="2.250000",
                    input_tokens=7)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/spend", headers=_hdrs(owner_key)
    )
    assert resp.status_code == 200, resp.text
    totals = resp.json()["totals"]
    assert totals["cost_usd"] == pytest.approx(2.25, abs=1e-6)


@pytest.mark.asyncio
async def test_service_key_sees_no_dollars(client: AsyncClient, db_session: AsyncSession):
    admin, _ = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)
    await _make_run(db_session, project, cost_basis="reported", cost_usd="3.000000",
                    input_tokens=9)

    _, svc_key = await _make_service_key(
        db_session, org, admin, scopes=["agent_runs:read"]
    )
    resp = await client.get(
        f"/api/v1/projects/{project.id}/spend", headers=_hdrs(svc_key)
    )
    assert resp.status_code == 200, resp.text
    totals = resp.json()["totals"]
    assert totals["input_tokens"] == 9
    assert "cost_usd" not in totals


# ── empty project ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_project_zeros(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/spend", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    totals = resp.json()["totals"]
    assert totals["runs"] == 0
    assert totals["input_tokens"] == 0
    assert totals["output_tokens"] == 0
    assert totals["cache_read_tokens"] == 0
    assert totals["cache_write_tokens"] == 0
    assert totals["tokens_on_plans"] == 0
    assert totals["cost_usd"] == 0.0


# ── ticket spend ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ticket_spend_persona_model_and_review_rounds(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)

    await _make_run(db_session, project, ticket_id=ticket.id, persona="atlas",
                    model="opus", input_tokens=100, output_tokens=50)
    await _make_run(db_session, project, ticket_id=ticket.id, persona="scout",
                    model="sonnet", input_tokens=30, output_tokens=10)

    # One trusted review verdict → one review round.
    db_session.add(
        TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket.id,
            author_user_id=owner.id,
            author_persona="codex-reviewer",
            content="Codex R1 review on tk_x: CHANGES REQUESTED\n\nFindings:\n\n • LOW — a.py:1: thing",
            verdict_trusted=True,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/spend", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["totals"]["runs"] == 2
    assert body["totals"]["input_tokens"] == 130
    assert body["totals"]["output_tokens"] == 60
    assert {g["key"] for g in body["per_persona"]} == {"atlas", "scout"}
    assert {g["key"] for g in body["per_model"]} == {"opus", "sonnet"}
    assert body["review_rounds"] == 1


@pytest.mark.asyncio
async def test_ticket_spend_zero_review_rounds_without_trusted_comment(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    await _make_run(db_session, project, ticket_id=ticket.id, input_tokens=5)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/spend", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["review_rounds"] == 0


# ── no N+1 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rollup_is_a_single_sql_query(
    db_engine, db_session: AsyncSession
):
    """A grouped rollup must be ONE SQL statement over agent_runs — no
    per-row queries. The endpoint's access checks touch other tables, so
    this asserts directly against the service (the rollup is the unit)."""
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    for persona in ("atlas", "scout", "sentinel"):
        await _make_run(db_session, project, persona=persona, input_tokens=1)

    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", _capture)
    try:
        result = await rollup(
            db_session,
            group_by="persona",
            project_id=project.id,
            ticket_id=None,
            readable_projects=None,
            from_dt=None,
            to_dt=None,
            show_dollars=True,
        )
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _capture)

    agent_run_queries = [s for s in captured if "agent_runs" in s]
    assert len(agent_run_queries) == 1, agent_run_queries
    assert "GROUP BY" in agent_run_queries[0].upper()
    assert {g["key"] for g in result["groups"]} == {"atlas", "scout", "sentinel"}

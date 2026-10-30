"""Review Loop rollups (Review Loop R3, Brief E2) — API tests.

Covers the three read-only surfaces end to end:
- per-ticket loop report (rounds, findings by harm, checklist/new/design
  counts, token + dollar attribution to a run's primary round)
- project loop insights (caught = harmful AND fixed only, cost per accepted
  change with LEFT-JOIN denominator, zero-filled learning series)
- ticket story (ordered brief → run → finding → fix → round → verdict →
  merge events, incl. a trusted VERIFIED-CLEAN verdict)
- dollar visibility (personal owner / org admin see cost_usd; plain org
  member and service keys see the key ABSENT)
- existence-oracle 404 for outsiders and other-org members
- empty-project zeros
- one/two SQL statements over agent_runs (no N+1)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentRun,
    ApiKey,
    LoopChecklistItem,
    OrgMember,
    Organization,
    Project,
    Ticket,
    TicketComment,
    User,
)
from sessionfs.server.services import review_loop


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


async def _make_ticket(
    db: AsyncSession,
    project: Project,
    owner: User,
    *,
    status: str = "open",
    resolved_at: datetime | None = None,
) -> Ticket:
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title=f"loop ticket {uuid.uuid4().hex[:6]}",
        description="review-loop rollup coverage",
        created_by_user_id=owner.id,
        status=status,
        resolved_at=resolved_at,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


async def _make_run(
    db: AsyncSession,
    project: Project,
    *,
    persona: str = "atlas",
    role: str | None = "implementer",
    ticket_id: str | None = None,
    findings: list[dict] | None = None,
    result_summary: str | None = None,
    occurred_at: datetime | None = None,
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
        role=role,
        ticket_id=ticket_id,
        findings=json.dumps(findings or []),
        result_summary=result_summary,
        occurred_at=occurred_at,
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


async def _make_comment(
    db: AsyncSession,
    ticket: Ticket,
    user: User,
    *,
    content: str,
    author_persona: str | None = None,
    verdict_trusted: bool = False,
    reviewed_commit: str | None = None,
    created_at: datetime | None = None,
) -> TicketComment:
    comment = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket.id,
        author_user_id=user.id,
        author_persona=author_persona,
        content=content,
        verdict_trusted=verdict_trusted,
        reviewed_commit=reviewed_commit,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    return comment


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


async def _insert_checklist_item(
    db: AsyncSession,
    *,
    project_id: str,
    slug: str,
    title: str,
    updated_at: datetime,
    accepted_at: datetime | None = None,
) -> None:
    """Insert an ACTIVE checklist item into the real ``loop_checklist_items``
    table (migration 112 always provides ``accepted_at``)."""
    db.add(
        LoopChecklistItem(
            id=f"lci_{uuid.uuid4().hex[:24]}",
            project_id=project_id,
            slug=slug,
            title=title,
            check_text=f"check {slug}",
            status="active",
            source="manual",
            times_found=0,
            accepted_at=accepted_at,
            created_at=updated_at,
            updated_at=updated_at,
        )
    )
    await db.commit()


# ── loop report ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_report_rounds_counts_and_primary_round_attribution(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)

    # One run spanning rounds 1 (design) + 2 (code): its tokens/dollars are
    # attributed to its PRIMARY round = the first (round, stage) key = round 1.
    await _make_run(
        db_session,
        project,
        ticket_id=ticket.id,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=10,
        cache_write_tokens=5,
        cost_basis="reported",
        cost_usd="0.123456",
        findings=[
            {"round": 1, "stage": "design", "harm": "money-spent", "class": "sql-injection", "title": "a"},
            {"round": 1, "stage": "design", "harm": "none", "class": "new", "title": "b"},
            {"round": 2, "stage": "code", "harm": "data-exposed", "class": "xss", "title": "c"},
            {"round": 2, "stage": "code", "harm": "crash", "class": "sql-injection", "title": "d"},
        ],
    )
    # A second run attributed to round 2 (code).
    await _make_run(
        db_session,
        project,
        ticket_id=ticket.id,
        input_tokens=10,
        cost_basis="reported",
        cost_usd="0.500000",
        findings=[
            {"round": 2, "stage": "code", "harm": "other", "class": "xss", "title": "e"},
        ],
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/loop-report",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    rounds = resp.json()["rounds"]
    assert [r["round"] for r in rounds] == [1, 2]

    r1, r2 = rounds
    assert r1["stage"] == "design"
    assert r1["findings_by_harm"] == {"money-spent": 1, "none": 1}
    assert r1["checklist_class_count"] == 1  # sql-injection only (new excluded)
    assert r1["new_class_count"] == 1
    assert r1["design_stage_count"] == 2
    assert r1["tokens"] == 165
    assert r1["cost_usd"] == pytest.approx(0.123456, abs=1e-6)

    assert r2["stage"] == "code"
    assert r2["findings_by_harm"] == {"data-exposed": 1, "crash": 1, "other": 1}
    assert r2["checklist_class_count"] == 3  # xss + xss + sql-injection
    assert r2["new_class_count"] == 0
    assert r2["design_stage_count"] == 0
    assert r2["tokens"] == 10
    assert r2["cost_usd"] == pytest.approx(0.5, abs=1e-6)


@pytest.mark.asyncio
async def test_loop_report_org_member_sees_no_dollars(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)
    ticket = await _make_ticket(db_session, project, admin)
    await _make_run(
        db_session,
        project,
        ticket_id=ticket.id,
        input_tokens=42,
        cost_basis="reported",
        cost_usd="1.500000",
        findings=[{"round": 1, "stage": "code", "harm": "crash", "class": "xss", "title": "x"}],
    )

    member, member_key = await _make_user(db_session)
    await _add_org_member(db_session, org, member, "member")

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/loop-report",
        headers=_hdrs(member_key),
    )
    assert resp.status_code == 200, resp.text
    rounds = resp.json()["rounds"]
    assert rounds[0]["tokens"] == 42
    assert "cost_usd" not in rounds[0]


@pytest.mark.asyncio
async def test_loop_report_org_admin_sees_dollars(
    client: AsyncClient, db_session: AsyncSession
):
    admin, admin_key = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)
    ticket = await _make_ticket(db_session, project, admin)
    await _make_run(
        db_session,
        project,
        ticket_id=ticket.id,
        input_tokens=42,
        cost_basis="reported",
        cost_usd="1.500000",
        findings=[{"round": 1, "stage": "code", "harm": "crash", "class": "xss", "title": "x"}],
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/loop-report",
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200, resp.text
    rounds = resp.json()["rounds"]
    assert rounds[0]["cost_usd"] == pytest.approx(1.5, abs=1e-6)


# ── loop insights ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_insights_caught_only_harmful_and_fixed(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)

    # 1. harmful + status=fixed → caught
    # 2. harmful + fixed_in_sha set → caught
    # 3. harmful + open (no fix) → NOT caught
    # 4. harm=none + fixed → NOT caught (harmless)
    # 5. harm=None + fixed → NOT caught (no harm tag)
    await _make_run(
        db_session,
        project,
        ticket_id=ticket.id,
        occurred_at=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc),
        findings=[
            {"round": 1, "stage": "code", "harm": "data-exposed", "class": "xss",
             "file": "a.py", "line": 10, "title": "stored XSS", "status": "fixed",
             "fixed_in_sha": "abc1234567"},
            {"round": 1, "stage": "code", "harm": "money-spent", "class": "sql-injection",
             "file": "b.py", "line": 20, "title": "SQLi", "fixed_in_sha": "def2345678"},
            {"round": 1, "stage": "code", "harm": "crash", "class": "null-deref",
             "file": "c.py", "line": 30, "title": "NPE", "status": "open"},
            {"round": 1, "stage": "code", "harm": "none", "class": "xss",
             "file": "d.py", "line": 40, "title": "benign", "status": "fixed"},
            {"round": 1, "stage": "code", "class": "xss",
             "file": "e.py", "line": 50, "title": "untagged", "status": "fixed"},
        ],
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/loop-insights"
        "?from=2026-10-01T00:00:00Z&to=2026-11-01T00:00:00Z",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    caught = resp.json()["caught"]
    assert [c["finding"] for c in caught] == ["stored XSS", "SQLi"]
    first = caught[0]
    assert first["ticket_id"] == ticket.id
    assert first["harm"] == "data-exposed"
    assert first["class"] == "xss"
    assert first["file"] == "a.py"
    assert first["line"] == 10
    assert first["fixed_in_sha"] == "abc1234567"


@pytest.mark.asyncio
async def test_insights_cost_per_accepted_change_left_join(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    # ticket1: done in October, has a run worth 150 tokens / $0.30.
    t1 = await _make_ticket(
        db_session, project, owner,
        status="done", resolved_at=datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc),
    )
    await _make_run(
        db_session, project, ticket_id=t1.id,
        input_tokens=100, output_tokens=50, cost_basis="reported", cost_usd="0.300000",
    )
    # ticket2: done in October, NO runs — still in the denominator (LEFT JOIN).
    await _make_ticket(
        db_session, project, owner,
        status="done", resolved_at=datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc),
    )
    # ticket3: NOT done (open) — excluded from both numerator and denominator.
    t3 = await _make_ticket(db_session, project, owner, status="open")
    await _make_run(
        db_session, project, ticket_id=t3.id, input_tokens=300,
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/loop-insights"
        "?from=2026-10-01T00:00:00Z&to=2026-11-01T00:00:00Z",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    cost = resp.json()["cost_per_accepted_change"]
    assert len(cost) == 1
    point = cost[0]
    assert point["period"] == "2026-10"
    assert point["tokens"] == pytest.approx(75.0)  # 150 / 2
    assert point["cost_usd"] == pytest.approx(0.15, abs=1e-6)  # 0.30 / 2


@pytest.mark.asyncio
async def test_insights_learning_zero_filled_series_and_no_join_date(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)

    # One finding in the week of 2026-10-05; window spans three Mondays
    # (10-05, 10-12, 10-19) so the series is zero-filled after the hit.
    await _make_run(
        db_session,
        project,
        ticket_id=ticket.id,
        occurred_at=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc),
        findings=[
            {"round": 1, "stage": "code", "harm": "money-spent", "class": "sql-injection", "title": "sqli"},
        ],
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/loop-insights"
        "?from=2026-10-05T00:00:00Z&to=2026-10-19T00:00:00Z",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    learning = resp.json()["learning"]
    assert [l["class"] for l in learning] == ["sql-injection"]
    item = learning[0]
    assert item["title"] == "sql-injection"
    assert item["joined_at"] is None  # no checklist item for this class
    assert item["series"] == [
        {"week": "2026-10-05", "count": 1},
        {"week": "2026-10-12", "count": 0},
        {"week": "2026-10-19", "count": 0},
    ]


@pytest.mark.asyncio
async def test_insights_checklist_join_date_from_accepted_at(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    await _insert_checklist_item(
        db_session,
        project_id=project.id,
        slug="xss",
        title="Stored XSS",
        updated_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
        accepted_at=datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc),
    )
    # Active item that was never accepted — joined_at must be null, not the
    # updated_at value.
    await _insert_checklist_item(
        db_session,
        project_id=project.id,
        slug="sql-injection",
        title="SQLi",
        updated_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
    )
    await _make_run(
        db_session,
        project,
        ticket_id=ticket.id,
        occurred_at=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc),
        findings=[
            {"round": 1, "stage": "code", "harm": "data-exposed", "class": "xss",
             "title": "x", "status": "fixed"},
            {"round": 1, "stage": "code", "harm": "money-spent", "class": "sql-injection",
             "title": "s", "status": "fixed"},
        ],
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/loop-insights"
        "?from=2026-10-01T00:00:00Z&to=2026-11-01T00:00:00Z",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    learning = {l["class"]: l for l in resp.json()["learning"]}
    assert learning["xss"]["title"] == "Stored XSS"
    assert learning["xss"]["joined_at"] == "2026-03-01T10:00:00+00:00"
    assert learning["sql-injection"]["title"] == "SQLi"
    assert learning["sql-injection"]["joined_at"] is None


@pytest.mark.asyncio
async def test_insights_learning_excludes_new_and_missing_class(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    await _make_run(
        db_session,
        project,
        ticket_id=ticket.id,
        occurred_at=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc),
        findings=[
            {"round": 1, "stage": "code", "harm": "data-exposed", "class": "xss",
             "title": "x", "status": "fixed"},
            {"round": 1, "stage": "code", "harm": "crash", "class": "new",
             "title": "brand new", "status": "fixed"},
            {"round": 1, "stage": "code", "harm": "money-spent",
             "title": "no class", "status": "fixed"},
        ],
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/loop-insights"
        "?from=2026-10-01T00:00:00Z&to=2026-11-01T00:00:00Z",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    assert [l["class"] for l in resp.json()["learning"]] == ["xss"]


@pytest.mark.asyncio
async def test_insights_window_capped_at_366_days(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    await _make_run(
        db_session,
        project,
        ticket_id=ticket.id,
        occurred_at=datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc),
        findings=[
            {"round": 1, "stage": "code", "harm": "crash", "class": "xss",
             "title": "x", "status": "fixed"},
        ],
    )

    # Exactly 366 days (2025-01-01 .. 2026-01-02) is allowed; the weekly
    # learning series is bounded to 53 Monday buckets.
    ok = await client.get(
        f"/api/v1/projects/{project.id}/loop-insights"
        "?from=2025-01-01T00:00:00Z&to=2026-01-02T00:00:00Z",
        headers=_hdrs(key),
    )
    assert ok.status_code == 200, ok.text
    learning = ok.json()["learning"]
    assert [l["class"] for l in learning] == ["xss"]
    assert len(learning[0]["series"]) == 53

    # 367 days → 422.
    bad = await client.get(
        f"/api/v1/projects/{project.id}/loop-insights"
        "?from=2025-01-01T00:00:00Z&to=2026-01-03T00:00:00Z",
        headers=_hdrs(key),
    )
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_insights_caught_capped_to_100_most_recent_with_total(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)

    # 150 caught findings: 120 "old" across days 1-12, then 30 "new" across
    # days 13-15. The 100 most recent drop the 50 oldest (days 1-5).
    for day in range(1, 13):
        await _make_run(
            db_session,
            project,
            ticket_id=ticket.id,
            occurred_at=datetime(2026, 10, day, 12, 0, tzinfo=timezone.utc),
            findings=[
                {"round": 1, "stage": "code", "harm": "crash", "class": "xss",
                 "title": f"old-{day}-{i}", "status": "fixed"}
                for i in range(10)
            ],
        )
    for day in range(13, 16):
        await _make_run(
            db_session,
            project,
            ticket_id=ticket.id,
            occurred_at=datetime(2026, 10, day, 12, 0, tzinfo=timezone.utc),
            findings=[
                {"round": 1, "stage": "code", "harm": "crash", "class": "xss",
                 "title": f"new-{day}-{i}", "status": "fixed"}
                for i in range(10)
            ],
        )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/loop-insights"
        "?from=2026-10-01T00:00:00Z&to=2026-11-01T00:00:00Z",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caught_total"] == 150
    assert len(body["caught"]) == 100
    titles = [c["finding"] for c in body["caught"]]
    assert titles[0] == "old-6-0"  # oldest retained
    assert titles[-1] == "new-15-9"  # most recent


@pytest.mark.asyncio
async def test_insights_selects_only_needed_columns(db_engine, db_session: AsyncSession):
    """The window query must select only the columns the insights rollup reads,
    not the full ORM entity (whose token/cost columns are irrelevant here)."""
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    await _make_run(
        db_session,
        project,
        ticket_id=ticket.id,
        occurred_at=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc),
        findings=[
            {"round": 1, "stage": "code", "harm": "crash", "class": "xss",
             "title": "x", "status": "fixed"},
        ],
    )

    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", _capture)
    try:
        await review_loop.loop_insights(
            db_session,
            project_id=project.id,
            from_dt=datetime(2026, 10, 1, tzinfo=timezone.utc),
            to_dt=datetime(2026, 11, 1, tzinfo=timezone.utc),
            show_dollars=True,
        )
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _capture)

    runs_queries = [s for s in captured if "agent_runs" in s]
    assert runs_queries, captured
    window_sql = runs_queries[0]
    assert "ticket_id" in window_sql
    assert "findings" in window_sql
    assert "input_tokens" not in window_sql
    assert "cost_usd" not in window_sql


# ── ticket story ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_story_ordered_events_with_trusted_clean_verdict(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    ticket.created_at = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    await db_session.commit()

    await _make_run(
        db_session,
        project,
        ticket_id=ticket.id,
        role="implementer",
        occurred_at=datetime(2026, 10, 2, 11, 0, tzinfo=timezone.utc),
        input_tokens=100,
        output_tokens=50,
        cost_basis="reported",
        cost_usd="1.000000",
        findings=[
            {"round": 2, "stage": "code", "severity": "high", "class": "xss",
             "harm": "data-exposed", "file": "a.py", "line": 10,
             "title": "stored XSS", "status": "fixed", "fixed_in_sha": "abc1234567"},
        ],
    )
    await _make_comment(
        db_session,
        ticket,
        owner,
        content=f"Codex R2 review on {ticket.id}: VERIFIED-CLEAN",
        author_persona="codex-reviewer",
        verdict_trusted=True,
        reviewed_commit="abc1234567",
        created_at=datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc),
    )
    ticket.resolved_at = datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc)
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/story", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticket_id"] == ticket.id
    assert body["title"] == ticket.title

    events = body["events"]
    assert [e["kind"] for e in events] == [
        "brief", "run", "finding", "fix", "round", "verdict", "merge",
    ]

    run = events[1]
    assert run["run_id"].startswith("run_")
    assert run["role"] == "implementer"
    assert run["tokens"] == 150
    assert run["cost_usd"] == pytest.approx(1.0, abs=1e-6)

    finding = events[2]
    assert finding["harm"] == "data-exposed"
    assert finding["class"] == "xss"
    assert finding["file"] == "a.py"
    assert finding["line"] == 10
    assert finding["round"] == 2
    assert finding["title"] == "stored XSS"

    fix = events[3]
    assert fix["sha"] == "abc1234567"

    rnd = events[4]
    assert rnd["round"] == 2
    assert rnd["reviewer"] == "codex-reviewer"
    assert rnd["title"] == "VERIFIED-CLEAN"

    verdict = events[5]
    assert verdict["reviewer"] == "codex-reviewer"
    assert verdict["sha"] == "abc1234567"

    assert events[6]["kind"] == "merge"


@pytest.mark.asyncio
async def test_story_no_verdict_without_trusted_comment(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    await _make_run(db_session, project, ticket_id=ticket.id, input_tokens=5)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/story", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    kinds = [e["kind"] for e in resp.json()["events"]]
    assert kinds == ["brief", "run"]  # no round/verdict/merge without them


# ── access: existence oracle ──────────────────────────────────────


@pytest.mark.asyncio
async def test_outsider_and_other_org_get_404(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session)
    org_b = await _make_org(db_session)
    project = await _make_project(db_session, owner, org=org_b)
    ticket = await _make_ticket(db_session, project, owner)
    await _make_run(db_session, project, ticket_id=ticket.id, input_tokens=1)

    outsider, outsider_key = await _make_user(db_session)
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/loop-report",
        headers=_hdrs(outsider_key),
    )
    assert resp.status_code == 404

    org_a = await _make_org(db_session)
    other_member, other_key = await _make_user(db_session)
    await _add_org_member(db_session, org_a, other_member, "member")
    resp2 = await client.get(
        f"/api/v1/projects/{project.id}/loop-insights", headers=_hdrs(other_key)
    )
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_service_key_sees_no_dollars(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)
    ticket = await _make_ticket(db_session, project, admin)
    await _make_run(
        db_session,
        project,
        ticket_id=ticket.id,
        input_tokens=9,
        cost_basis="reported",
        cost_usd="3.000000",
        findings=[{"round": 1, "stage": "code", "harm": "crash", "class": "xss", "title": "x"}],
    )

    _, svc_key = await _make_service_key(db_session, org, admin, scopes=["loops:read"])
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/loop-report",
        headers=_hdrs(svc_key),
    )
    assert resp.status_code == 200, resp.text
    rounds = resp.json()["rounds"]
    assert rounds[0]["tokens"] == 9
    assert "cost_usd" not in rounds[0]


# ── empty project / ticket ────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_project_insights_and_report_zeros(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)

    report = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/loop-report",
        headers=_hdrs(key),
    )
    assert report.status_code == 200, report.text
    assert report.json()["rounds"] == []

    insights = await client.get(
        f"/api/v1/projects/{project.id}/loop-insights", headers=_hdrs(key)
    )
    assert insights.status_code == 200, insights.text
    body = insights.json()
    assert body["caught"] == []
    assert body["caught_total"] == 0
    assert body["cost_per_accepted_change"] == []
    assert body["learning"] == []


# ── no N+1 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rollups_are_bounded_sql_statements(db_engine, db_session: AsyncSession):
    """A rollup must NOT query agent_runs once per run. The report is one
    statement; insights is two (window runs + cost join) — constant in the
    number of rows."""
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    for _ in range(5):
        await _make_run(
            db_session, project, ticket_id=ticket.id, input_tokens=1,
            findings=[{"round": 1, "stage": "code", "harm": "crash", "class": "xss", "title": "x"}],
        )

    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", _capture)
    try:
        await review_loop.loop_report(
            db_session, project_id=project.id, ticket_id=ticket.id, show_dollars=True
        )
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _capture)

    report_queries = [s for s in captured if "agent_runs" in s]
    assert len(report_queries) == 1, report_queries

    captured.clear()
    event.listen(db_engine.sync_engine, "before_cursor_execute", _capture)
    try:
        await review_loop.loop_insights(
            db_session,
            project_id=project.id,
            from_dt=datetime.now(timezone.utc) - timedelta(days=30),
            to_dt=datetime.now(timezone.utc) + timedelta(days=1),
            show_dollars=True,
        )
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _capture)

    insights_queries = [s for s in captured if "agent_runs" in s]
    assert len(insights_queries) == 2, insights_queries

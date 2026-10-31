"""PRs, releases, gates, deployments and the Ship Report (track F1) — API tests.

Covers end to end:
- PR upsert + ticket auto-link (``tk_…`` in title, cross-project ignored)
- release idempotency + PR linking
- gate upsert + deployment append
- Ship Report numbers over a seeded loop (findings/harmful counts,
  all_fixed_before_merge, cost per accepted change, checklist classes learned
  between releases, gates, deployments, PRs)
- dollar visibility (personal owner sees ``cost_usd``; org member and service
  keys see the key ABSENT, and markdown never renders dollars for them)
- month Ship Report
- model scoreboard grouping in loop insights
- ticket story gains prs[] / releases[] / deployments[]
- outsider 404 + service-key scopes
- bounded SQL (no N+1)
"""

from __future__ import annotations

import hashlib
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
    LoopChecklistItem,
    OrgMember,
    Organization,
    Project,
    Ticket,
    TicketComment,
    User,
)
from sessionfs.server.services import releases


# ── fixtures/helpers ───────────────────────────────────────────────


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


async def _add_org_member(
    db: AsyncSession, org: Organization, user: User, role: str
) -> None:
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
        name=f"rel-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/rel-{uuid.uuid4().hex[:6]}",
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
    loop_config: str | None = None,
    parent_ticket_id: str | None = None,
) -> Ticket:
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title=f"rel ticket {uuid.uuid4().hex[:6]}",
        description="release coverage",
        created_by_user_id=owner.id,
        status=status,
        resolved_at=resolved_at,
        loop_config=loop_config,
        parent_ticket_id=parent_ticket_id,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


async def _make_run(
    db: AsyncSession,
    project: Project,
    *,
    ticket_id: str | None = None,
    persona_name: str = "atlas",
    role: str | None = "implementer",
    model: str | None = None,
    branch: str | None = None,
    findings: list[dict] | None = None,
    occurred_at: datetime | None = None,
    cost_basis: str | None = "reported",
    cost_usd: str | None = "0.000000",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> AgentRun:
    run = AgentRun(
        id=f"run_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        persona_name=persona_name,
        role=role,
        model=model,
        branch=branch,
        ticket_id=ticket_id,
        findings=json.dumps(findings or []),
        occurred_at=occurred_at,
        cost_basis=cost_basis,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        created_at=datetime.now(timezone.utc),
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
    accepted_at: datetime,
) -> None:
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
            created_at=accepted_at,
            updated_at=accepted_at,
        )
    )
    await db.commit()


async def _upsert_pr(
    client: AsyncClient, project: Project, key: str, **overrides
) -> dict:
    body = {
        "provider": "github",
        "repo": "o/r",
        "number": 1,
        "title": "ship it",
        "state": "open",
        "branch": "main",
        **overrides,
    }
    resp = await client.post(
        f"/api/v1/projects/{project.id}/pull-requests",
        json=body,
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── PR upsert + auto-link ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_pr_upsert_and_auto_link_cross_project_ignored(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)

    other_owner, _ = await _make_user(db_session)
    other_project = await _make_project(db_session, other_owner)
    other_ticket = await _make_ticket(db_session, other_project, other_owner)

    # Title carries a same-project id, a cross-project id, and a bogus id.
    pr = await _upsert_pr(
        client,
        project,
        key,
        number=7,
        title=f"Fix {ticket.id} and {other_ticket.id} and tk_{'0' * 16}",
    )
    assert pr["id"].startswith("pr_")
    assert pr["linked_ticket_ids"] == [ticket.id]  # cross-project + bogus ignored
    assert pr["state"] == "open"

    # Upsert: same natural key updates state/title but keeps the id. The link
    # is recomputed from the current title, so keep the id in it.
    again = await _upsert_pr(
        client, project, key, number=7,
        title=f"changed title {ticket.id}", state="merged",
    )
    assert again["id"] == pr["id"]
    assert again["state"] == "merged"
    assert again["title"] == f"changed title {ticket.id}"
    assert again["linked_ticket_ids"] == [ticket.id]


@pytest.mark.asyncio
async def test_pr_auto_links_from_loop_branch(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    await _make_run(
        db_session, project, ticket_id=ticket.id, branch="feat/x", model="m1"
    )

    pr = await _upsert_pr(client, project, key, number=9, branch="feat/x")
    assert pr["linked_ticket_ids"] == [ticket.id]


# ── releases / gates / deployments ────────────────────────────────


@pytest.mark.asyncio
async def test_release_idempotent_and_links_prs(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    pr = await _upsert_pr(
        client, project, key, number=11, title=f"ship {ticket.id}"
    )

    body = {"version": "1.0.0", "tag_sha": "a" * 40, "pr_numbers": [11]}
    resp = await client.post(
        f"/api/v1/projects/{project.id}/releases", json=body, headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    rel = resp.json()
    assert rel["version"] == "1.0.0"
    assert rel["pr_ids"] == [pr["id"]]

    # Re-record: id stable, tag updated, no duplicate link.
    again = await client.post(
        f"/api/v1/projects/{project.id}/releases",
        json={"version": "1.0.0", "tag_sha": "b" * 40, "pr_numbers": [11]},
        headers=_hdrs(key),
    )
    assert again.status_code == 200, again.text
    rel2 = again.json()
    assert rel2["id"] == rel["id"]
    assert rel2["tag_sha"] == "b" * 40
    assert rel2["pr_ids"] == [pr["id"]]

    listing = await client.get(
        f"/api/v1/projects/{project.id}/releases", headers=_hdrs(key)
    )
    assert listing.status_code == 200, listing.text
    versions = [r["version"] for r in listing.json()["releases"]]
    assert versions == ["1.0.0"]


@pytest.mark.asyncio
async def test_gates_idempotent_and_deployments_append(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    await client.post(
        f"/api/v1/projects/{project.id}/releases",
        json={"version": "1.0.0", "tag_sha": "a" * 40},
        headers=_hdrs(key),
    )

    g1 = await client.post(
        f"/api/v1/projects/{project.id}/releases/1.0.0/gates",
        json={"kind": "ci", "status": "passed"},
        headers=_hdrs(key),
    )
    assert g1.status_code == 200, g1.text
    gate = g1.json()
    assert gate["kind"] == "ci"
    assert gate["status"] == "passed"

    # Same kind upserts (status flips, id stable).
    g2 = await client.post(
        f"/api/v1/projects/{project.id}/releases/1.0.0/gates",
        json={"kind": "ci", "status": "failed"},
        headers=_hdrs(key),
    )
    assert g2.status_code == 200, g2.text
    assert g2.json()["id"] == gate["id"]
    assert g2.json()["status"] == "failed"

    d1 = await client.post(
        f"/api/v1/projects/{project.id}/releases/1.0.0/deployments",
        json={"environment": "prod"},
        headers=_hdrs(key),
    )
    assert d1.status_code == 200, d1.text
    assert d1.json()["environment"] == "prod"

    # Deployments append — same environment twice is two records.
    d2 = await client.post(
        f"/api/v1/projects/{project.id}/releases/1.0.0/deployments",
        json={"environment": "prod", "source": "argocd"},
        headers=_hdrs(key),
    )
    assert d2.status_code == 200, d2.text
    assert d2.json()["id"] != d1.json()["id"]


# ── Ship Report ───────────────────────────────────────────────────


async def _seed_shipped_loop(
    db: AsyncSession, client: AsyncClient, project: Project, owner: User, key: str
) -> tuple[Ticket, dict, dict]:
    """One done ticket shipped by a PR, in release 1.0.0, with findings/runs."""
    ticket = await _make_ticket(
        db, project, owner,
        status="done", resolved_at=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc),
        loop_config="{}",
    )
    pr = await _upsert_pr(
        client, project, key, number=21, title=f"ship {ticket.id}", branch="feat/a"
    )
    rel = await client.post(
        f"/api/v1/projects/{project.id}/releases",
        json={
            "version": "1.0.0",
            "tag_sha": "a" * 40,
            "released_at": "2026-10-10T00:00:00Z",
            "pr_numbers": [21],
        },
        headers=_hdrs(key),
    )
    assert rel.status_code == 200, rel.text
    await _make_run(
        db, project,
        ticket_id=ticket.id, role="implementer", model="model-impl",
        occurred_at=datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc),
        input_tokens=100, output_tokens=50,
        cost_basis="reported", cost_usd="1.000000",
        findings=[
            {"round": 1, "stage": "code", "harm": "data-exposed", "class": "xss",
             "title": "stored xss", "status": "fixed", "fixed_in_sha": "sha1"},
            {"round": 1, "stage": "code", "harm": "crash", "class": "null-deref",
             "title": "npe", "status": "open"},
            {"round": 1, "stage": "code", "harm": "none", "class": "style",
             "title": "fmt"},
            {"round": 2, "stage": "code", "harm": "money-spent",
             "class": "sql-injection", "title": "sqli", "status": "fixed"},
        ],
    )
    await _make_comment(
        db, ticket, owner,
        content=f"Codex R2 review on {ticket.id}: VERIFIED-CLEAN",
        author_persona="codex-reviewer",
        verdict_trusted=True,
        reviewed_commit="sha1",
        created_at=datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc),
    )
    await client.post(
        f"/api/v1/projects/{project.id}/releases/1.0.0/gates",
        json={"kind": "ci", "status": "passed"},
        headers=_hdrs(key),
    )
    await client.post(
        f"/api/v1/projects/{project.id}/releases/1.0.0/deployments",
        json={"environment": "prod", "source": "manual"},
        headers=_hdrs(key),
    )
    return ticket, pr, rel.json()


@pytest.mark.asyncio
async def test_ship_report_numbers_owner_sees_dollars(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    await _seed_shipped_loop(db_session, client, project, owner, key)
    await _insert_checklist_item(
        db_session, project_id=project.id, slug="xss", title="Stored XSS",
        accepted_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/releases/1.0.0/ship-report",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == "1.0.0"
    assert body["released_at"] == "2026-10-10T00:00:00+00:00"
    assert body["loops"] == 1
    assert body["tickets"] == 1
    assert body["review_rounds"] == 1
    assert body["findings"] == 4
    assert body["harmful"] == 3
    assert body["harmful_by_harm"] == {
        "data-exposed": 1, "crash": 1, "money-spent": 1,
    }
    assert body["all_fixed_before_merge"] is False  # crash still open
    assert body["cost_per_accepted_change"]["tokens"] == pytest.approx(150.0)
    assert body["cost_per_accepted_change"]["cost_usd"] == pytest.approx(1.0)
    assert [c["class"] for c in body["checklist_classes_learned"]] == ["xss"]
    assert [g["kind"] for g in body["gates"]] == ["ci"]
    assert [d["environment"] for d in body["deployments"]] == ["prod"]
    assert len(body["prs"]) == 1
    assert "markdown" in body
    assert "$" in body["markdown"]  # owner sees dollars


@pytest.mark.asyncio
async def test_ship_report_member_no_dollars_and_markdown(
    client: AsyncClient, db_session: AsyncSession
):
    admin, admin_key = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)
    await _seed_shipped_loop(db_session, client, project, admin, admin_key)

    member, member_key = await _make_user(db_session)
    await _add_org_member(db_session, org, member, "member")

    resp = await client.get(
        f"/api/v1/projects/{project.id}/releases/1.0.0/ship-report",
        headers=_hdrs(member_key),
    )
    assert resp.status_code == 200, resp.text
    cost = resp.json()["cost_per_accepted_change"]
    assert cost["tokens"] == pytest.approx(150.0)
    assert "cost_usd" not in cost  # key ABSENT for a plain member
    assert "$" not in resp.json()["markdown"]  # markdown never renders dollars


@pytest.mark.asyncio
async def test_ship_report_month_and_between_releases(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    pr = await _upsert_pr(client, project, key, number=31, title=f"ship {ticket.id}")

    for version, released_at in (("1.0.0", "2026-10-05T00:00:00Z"),
                                 ("2.0.0", "2026-10-20T00:00:00Z")):
        resp = await client.post(
            f"/api/v1/projects/{project.id}/releases",
            json={
                "version": version, "tag_sha": hashlib.sha1(version.encode()).hexdigest(),
                "released_at": released_at, "pr_numbers": [31],
            },
            headers=_hdrs(key),
        )
        assert resp.status_code == 200, resp.text

    # Learned on 10-15: after 1.0.0, before 2.0.0.
    await _insert_checklist_item(
        db_session, project_id=project.id, slug="xss", title="Stored XSS",
        accepted_at=datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc),
    )

    r1 = await client.get(
        f"/api/v1/projects/{project.id}/releases/1.0.0/ship-report",
        headers=_hdrs(key),
    )
    assert r1.json()["checklist_classes_learned"] == []  # before 1.0.0's window

    r2 = await client.get(
        f"/api/v1/projects/{project.id}/releases/2.0.0/ship-report",
        headers=_hdrs(key),
    )
    assert [c["class"] for c in r2.json()["checklist_classes_learned"]] == ["xss"]

    month = await client.get(
        f"/api/v1/projects/{project.id}/ship-report?month=2026-10",
        headers=_hdrs(key),
    )
    assert month.status_code == 200, month.text
    body = month.json()
    assert body["version"] == "2026-10"
    assert body["tickets"] == 1
    assert [p["id"] for p in body["prs"]] == [pr["id"]]
    assert [c["class"] for c in body["checklist_classes_learned"]] == ["xss"]


@pytest.mark.asyncio
async def test_ship_report_missing_release_404_and_bad_month_422(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    miss = await client.get(
        f"/api/v1/projects/{project.id}/releases/nope/ship-report",
        headers=_hdrs(key),
    )
    assert miss.status_code == 404
    assert miss.json()["error"]["message"] == "Release not found"

    bad = await client.get(
        f"/api/v1/projects/{project.id}/ship-report?month=2026-13",
        headers=_hdrs(key),
    )
    assert bad.status_code == 422


# ── scoreboard ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scoreboard_grouping(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    # Two done tickets: pair (impl-a, rev-b) twice; a third done with no runs.
    for i in range(2):
        t = await _make_ticket(
            db_session, project, owner,
            status="done", resolved_at=datetime(2026, 10, 5 + i, 12, 0, tzinfo=timezone.utc),
        )
        await _make_run(
            db_session, project, ticket_id=t.id, role="implementer", model="impl-a",
            input_tokens=100, cost_basis="reported", cost_usd="0.500000",
            findings=[{"round": 1, "stage": "code", "harm": "crash", "class": "xss", "title": "x"}],
        )
        await _make_run(
            db_session, project, ticket_id=t.id, role="reviewer", model="rev-b",
        )
    await _make_ticket(
        db_session, project, owner,
        status="done", resolved_at=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc),
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/loop-insights"
        "?from=2026-10-01T00:00:00Z&to=2026-11-01T00:00:00Z",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    scoreboard = resp.json()["scoreboard"]
    assert len(scoreboard) == 2  # (impl-a, rev-b) + (None, None)
    row = next(s for s in scoreboard if s["implementer_model"] == "impl-a")
    assert row["reviewer_model"] == "rev-b"
    assert row["accepted_changes"] == 2
    assert row["harmful_findings_per_accepted_change"] == pytest.approx(1.0)
    assert row["cost_per_accepted_change"]["tokens"] == pytest.approx(100.0)
    assert row["cost_per_accepted_change"]["cost_usd"] == pytest.approx(0.5)

    null_row = next(s for s in scoreboard if s["implementer_model"] is None)
    assert null_row["accepted_changes"] == 1
    assert null_row["cost_per_accepted_change"]["tokens"] == pytest.approx(0.0)


# ── ticket story additions ────────────────────────────────────────


@pytest.mark.asyncio
async def test_story_includes_prs_releases_deployments(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    pr = await _upsert_pr(client, project, key, number=41, title=f"ship {ticket.id}")
    await client.post(
        f"/api/v1/projects/{project.id}/releases",
        json={
            "version": "1.0.0", "tag_sha": "a" * 40,
            "released_at": "2026-10-10T00:00:00Z", "pr_numbers": [41],
        },
        headers=_hdrs(key),
    )
    await client.post(
        f"/api/v1/projects/{project.id}/releases/1.0.0/deployments",
        json={"environment": "prod"},
        headers=_hdrs(key),
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/story",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [p["id"] for p in body["prs"]] == [pr["id"]]
    assert [r["version"] for r in body["releases"]] == ["1.0.0"]
    assert [d["environment"] for d in body["deployments"]] == ["prod"]


# ── direct ticket links (PR-less release flows) ───────────────────


@pytest.mark.asyncio
async def test_record_release_links_tickets_and_idempotent(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)

    body = {
        "version": "1.0.0", "tag_sha": "a" * 40, "ticket_ids": [ticket.id],
    }
    resp = await client.post(
        f"/api/v1/projects/{project.id}/releases", json=body, headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ticket_ids"] == [ticket.id]

    # Re-record: id stable, no duplicate link.
    again = await client.post(
        f"/api/v1/projects/{project.id}/releases",
        json={"version": "1.0.0", "tag_sha": "b" * 40, "ticket_ids": [ticket.id]},
        headers=_hdrs(key),
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == resp.json()["id"]
    assert again.json()["ticket_ids"] == [ticket.id]

    # The Ship Report counts the directly-linked ticket once.
    report = await client.get(
        f"/api/v1/projects/{project.id}/releases/1.0.0/ship-report",
        headers=_hdrs(key),
    )
    assert report.json()["tickets"] == 1


@pytest.mark.asyncio
async def test_link_tickets_endpoint_idempotent(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    await client.post(
        f"/api/v1/projects/{project.id}/releases",
        json={"version": "1.0.0", "tag_sha": "a" * 40},
        headers=_hdrs(key),
    )

    for _ in range(2):
        resp = await client.post(
            f"/api/v1/projects/{project.id}/releases/1.0.0/tickets",
            json={"ticket_ids": [ticket.id]},
            headers=_hdrs(key),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["version"] == "1.0.0"
        assert resp.json()["ticket_ids"] == [ticket.id]

    report = await client.get(
        f"/api/v1/projects/{project.id}/releases/1.0.0/ship-report",
        headers=_hdrs(key),
    )
    assert report.json()["tickets"] == 1  # not duplicated


@pytest.mark.asyncio
async def test_cross_project_ticket_404(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    other_owner, _ = await _make_user(db_session)
    other_project = await _make_project(db_session, other_owner)
    other_ticket = await _make_ticket(db_session, other_project, other_owner)

    # Cross-project and bogus ids share one byte-identical 404 body.
    cross = await client.post(
        f"/api/v1/projects/{project.id}/releases",
        json={"version": "1.0.0", "tag_sha": "a" * 40, "ticket_ids": [other_ticket.id]},
        headers=_hdrs(key),
    )
    assert cross.status_code == 404
    bogus = await client.post(
        f"/api/v1/projects/{project.id}/releases",
        json={"version": "1.0.0", "tag_sha": "a" * 40, "ticket_ids": [f"tk_{'0' * 16}"]},
        headers=_hdrs(key),
    )
    assert bogus.status_code == 404
    assert cross.json() == bogus.json()
    assert cross.json()["error"]["message"] == "Ticket not found"


@pytest.mark.asyncio
async def test_ship_report_direct_links_union_pr_links_no_double_counting(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    # A ships via PR only; B ships direct only; C ships both ways.
    ticket_a = await _make_ticket(
        db_session, project, owner,
        status="done", resolved_at=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc),
    )
    ticket_b = await _make_ticket(
        db_session, project, owner,
        status="done", resolved_at=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc),
    )
    ticket_c = await _make_ticket(
        db_session, project, owner,
        status="done", resolved_at=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc),
    )
    pr_a = await _upsert_pr(client, project, key, number=71, title=f"ship {ticket_a.id}")
    pr_c = await _upsert_pr(client, project, key, number=72, title=f"ship {ticket_c.id}")

    for ticket, cls in ((ticket_a, "xss"), (ticket_b, "crash"), (ticket_c, "sqli")):
        await _make_run(
            db_session, project, ticket_id=ticket.id, role="implementer",
            model="model-impl",
            occurred_at=datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc),
            input_tokens=100, output_tokens=0,
            cost_basis="reported", cost_usd="1.000000",
            findings=[
                {"round": 1, "stage": "code", "harm": "crash", "class": cls,
                 "title": cls, "status": "fixed"},
            ],
        )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/releases",
        json={
            "version": "1.0.0", "tag_sha": "a" * 40,
            "released_at": "2026-10-10T00:00:00Z",
            "pr_numbers": [71, 72],
            "ticket_ids": [ticket_b.id, ticket_c.id],
        },
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["pr_ids"]) == {pr_a["id"], pr_c["id"]}
    assert set(resp.json()["ticket_ids"]) == {ticket_b.id, ticket_c.id}

    report = await client.get(
        f"/api/v1/projects/{project.id}/releases/1.0.0/ship-report",
        headers=_hdrs(key),
    )
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["tickets"] == 3  # A, B, C — C counted once
    assert body["findings"] == 3
    assert body["harmful"] == 3
    assert body["cost_per_accepted_change"]["tokens"] == pytest.approx(100.0)
    assert body["cost_per_accepted_change"]["cost_usd"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_story_includes_direct_release(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    await client.post(
        f"/api/v1/projects/{project.id}/releases",
        json={
            "version": "1.0.0", "tag_sha": "a" * 40,
            "released_at": "2026-10-10T00:00:00Z", "ticket_ids": [ticket.id],
        },
        headers=_hdrs(key),
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/story",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    assert [r["version"] for r in resp.json()["releases"]] == ["1.0.0"]
    assert resp.json()["prs"] == []


# ── access: oracle + scopes ───────────────────────────────────────


@pytest.mark.asyncio
async def test_outsider_404_and_service_key_scopes(
    client: AsyncClient, db_session: AsyncSession
):
    admin, admin_key = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)
    ticket = await _make_ticket(db_session, project, admin)
    await _upsert_pr(client, project, admin_key, number=51, title=f"ship {ticket.id}")
    await client.post(
        f"/api/v1/projects/{project.id}/releases",
        json={"version": "1.0.0", "tag_sha": "a" * 40},
        headers=_hdrs(admin_key),
    )

    # Outsider user → byte-identical 404 for read and write.
    outsider, outsider_key = await _make_user(db_session)
    r = await client.get(
        f"/api/v1/projects/{project.id}/releases", headers=_hdrs(outsider_key)
    )
    assert r.status_code == 404
    w = await client.post(
        f"/api/v1/projects/{project.id}/releases",
        json={"version": "2.0.0", "tag_sha": "b" * 40},
        headers=_hdrs(outsider_key),
    )
    assert w.status_code == 404

    # Cross-org member → byte-identical 404 (member of a different org).
    other_org = await _make_org(db_session)
    other_member, other_key = await _make_user(db_session)
    await _add_org_member(db_session, other_org, other_member, "member")
    cross = await client.get(
        f"/api/v1/projects/{project.id}/releases", headers=_hdrs(other_key)
    )
    assert cross.status_code == 404
    assert cross.json() == r.json()  # byte-identical 404 oracle

    # Service key with loops:read can read, sees no dollars.
    _, read_key = await _make_service_key(db_session, org, admin, scopes=["loops:read"])
    rr = await client.get(
        f"/api/v1/projects/{project.id}/releases/1.0.0/ship-report",
        headers=_hdrs(read_key),
    )
    assert rr.status_code == 200, rr.text
    assert "cost_usd" not in rr.json()["cost_per_accepted_change"]

    # Service key with loops:write can write.
    _, write_key = await _make_service_key(db_session, org, admin, scopes=["loops:write"])
    ww = await client.post(
        f"/api/v1/projects/{project.id}/releases",
        json={"version": "3.0.0", "tag_sha": "c" * 40},
        headers=_hdrs(write_key),
    )
    assert ww.status_code == 200, ww.text

    # Service key without the scope → 403 insufficient_scope.
    _, no_scope = await _make_service_key(db_session, org, admin, scopes=["tickets:read"])
    ns = await client.get(
        f"/api/v1/projects/{project.id}/releases", headers=_hdrs(no_scope)
    )
    assert ns.status_code == 403


# ── bounded SQL ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ship_report_bounded_sql(db_engine, db_session: AsyncSession):
    """The Ship Report must not query agent_runs / ticket_comments /
    ticket_pr_links once per ticket. One fixed-size statement each."""
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    pr_numbers: list[int] = []
    for i in range(5):
        t = await _make_ticket(db_session, project, owner)
        await _make_run(
            db_session, project, ticket_id=t.id,
            findings=[{"round": 1, "stage": "code", "harm": "crash", "class": "xss", "title": "x"}],
        )
        await releases.upsert_pull_request(
            db_session, project_id=project.id, provider="github", repo="o/r",
            number=61 + i, title=f"ship {t.id}",
        )
        pr_numbers.append(61 + i)
    await releases.record_release(
        db_session, project_id=project.id, version="1.0.0", tag_sha="aaaa",
        pr_numbers=pr_numbers,
    )

    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", _capture)
    try:
        await releases.ship_report(
            db_session, project_id=project.id, show_dollars=True, version="1.0.0"
        )
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _capture)

    runs = [s for s in captured if "agent_runs" in s]
    comments = [s for s in captured if "ticket_comments" in s]
    pr_links = [s for s in captured if "ticket_pr_links" in s]
    assert len(runs) == 1, captured
    assert len(comments) == 1, captured
    assert len(pr_links) == 1, captured

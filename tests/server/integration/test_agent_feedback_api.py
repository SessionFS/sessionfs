"""v0.17.0 — AgentFeedback API regression tests.

Covers:
- create (append-only) + list roundtrip; source forced to "human".
- vocabulary validation (bad type / verdict / label mismatch / no target).
- cross-project target rejection (404, never a leak) incl. comment→ticket
  project mismatch.
- list filters (feedback_type/verdict/label/persona) + cursor pagination.
- summary grouping (pure SQL GROUP BY, no LLM).
- tier gate (pro → 403; team unlocks).
- service-key scope enforcement (agent_feedback:read alone cannot write).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
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


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _structured_error(body: dict) -> dict:
    """Navigate the {error: {details}} envelope to the raised detail."""
    if not isinstance(body, dict):
        return {}
    err = body.get("error") or body.get("detail") or body
    if isinstance(err, dict):
        inner = err.get("details") or err.get("detail")
        if isinstance(inner, dict):
            return inner
        return err
    return {}


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
    await db.flush()
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{name}-key",
            is_active=True,
            key_kind="user",
            scopes=json.dumps(["*"]),
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


async def _make_project(
    db: AsyncSession, owner: User, org: Organization | None = None
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name=f"fb-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:8]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_ticket(db: AsyncSession, project: Project) -> Ticket:
    t = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="A ticket",
        status="open",
        kind="task",
        priority="medium",
        created_by_user_id=project.owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


async def _make_run(db: AsyncSession, project: Project, persona: str = "atlas") -> AgentRun:
    r = AgentRun(
        id=f"run_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        persona_name=persona,
    )
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return r


async def _make_comment(
    db: AsyncSession, ticket: Ticket, author: User
) -> TicketComment:
    c = TicketComment(
        id=f"cm_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket.id,
        author_user_id=author.id,
        content="looks good",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


async def _make_org_with_admin(
    db: AsyncSession,
) -> tuple[Organization, User, str]:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name=f"Org-{uuid.uuid4().hex[:6]}",
        slug=f"o-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db.add(org)
    await db.flush()
    admin_user, raw = await _make_user(db, name="admin", tier="team")
    db.add(
        OrgMember(
            org_id=org.id,
            user_id=admin_user.id,
            role="admin",
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return org, admin_user, raw


async def _make_service_key(
    db: AsyncSession,
    org_id: str,
    minter: User,
    scopes: list[str],
    project_ids: list[str] | None = None,
) -> str:
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=minter.id,
            key_hash=hash_api_key(raw),
            name=f"svc-{uuid.uuid4().hex[:6]}",
            is_active=True,
            key_kind="service",
            org_id=org_id,
            scopes=json.dumps(scopes),
            created_by_user_id=minter.id,
            service_key_name=f"svc-{uuid.uuid4().hex[:6]}",
            project_ids=json.dumps(project_ids) if project_ids else None,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return raw


def _url(project_id: str) -> str:
    return f"/api/v1/projects/{project_id}/agent-feedback"


# ── create + list ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_list_roundtrip(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)

    resp = await client.post(
        _url(project.id),
        headers=_hdrs(key),
        json={
            "feedback_type": "route",
            "verdict": "negative",
            "label": "wrong_route",
            "final_owner": "sentinel",
            "notes": "misrouted during triage",
            "ticket_id": ticket.id,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"].startswith("afb_")
    assert body["project_id"] == project.id
    assert body["feedback_type"] == "route"
    assert body["verdict"] == "negative"
    assert body["label"] == "wrong_route"
    assert body["final_owner"] == "sentinel"
    assert body["ticket_id"] == ticket.id
    # source is never caller-settable — the route records "human".
    assert body["source"] == "human"

    lst = await client.get(_url(project.id), headers=_hdrs(key))
    assert lst.status_code == 200
    rows = lst.json()
    assert len(rows) == 1
    assert rows[0]["id"] == body["id"]


@pytest.mark.asyncio
async def test_create_requires_target(client: AsyncClient, db_session: AsyncSession):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    resp = await client.post(
        _url(project.id),
        headers=_hdrs(key),
        json={"feedback_type": "route", "verdict": "negative"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_rejects_bad_vocabulary(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)

    bad_type = await client.post(
        _url(project.id),
        headers=_hdrs(key),
        json={"feedback_type": "bogus", "verdict": "negative", "ticket_id": ticket.id},
    )
    assert bad_type.status_code == 422

    bad_label = await client.post(
        _url(project.id),
        headers=_hdrs(key),
        json={
            "feedback_type": "duplicate",
            "verdict": "positive",
            "label": "wrong_route",  # not valid for duplicate
            "ticket_id": ticket.id,
        },
    )
    assert bad_label.status_code == 422


@pytest.mark.asyncio
async def test_create_with_run_target(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    run = await _make_run(db_session, project, "atlas")

    resp = await client.post(
        _url(project.id),
        headers=_hdrs(key),
        json={
            "feedback_type": "actionability",
            "verdict": "positive",
            "label": "actionable",
            "agent_run_id": run.id,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["agent_run_id"] == run.id


# ── cross-project guards ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_project_ticket_rejected_404(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project_a = await _make_project(db_session, user)
    project_b = await _make_project(db_session, user)
    ticket_a = await _make_ticket(db_session, project_a)

    resp = await client.post(
        _url(project_b.id),
        headers=_hdrs(key),
        json={
            "feedback_type": "route",
            "verdict": "negative",
            "label": "wrong_route",
            "ticket_id": ticket_a.id,
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_comment_ticket_project_mismatch_404(
    client: AsyncClient, db_session: AsyncSession
):
    """A comment whose ticket lives in project A must not be recordable
    from project B even if the raw comment id is guessed."""
    user, key = await _make_user(db_session)
    project_a = await _make_project(db_session, user)
    project_b = await _make_project(db_session, user)
    ticket_a = await _make_ticket(db_session, project_a)
    comment_a = await _make_comment(db_session, ticket_a, user)

    resp = await client.post(
        _url(project_b.id),
        headers=_hdrs(key),
        json={
            "feedback_type": "resolution",
            "verdict": "neutral",
            "comment_id": comment_a.id,
        },
    )
    assert resp.status_code == 404


# ── list filters + cursor ────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_filters(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)

    for ftype, verdict, label in (
        ("route", "negative", "wrong_route"),
        ("route", "positive", "correct_route"),
        ("summary", "negative", "summary_bad"),
    ):
        await client.post(
            _url(project.id),
            headers=_hdrs(key),
            json={
                "feedback_type": ftype,
                "verdict": verdict,
                "label": label,
                "ticket_id": ticket.id,
            },
        )

    by_type = await client.get(
        _url(project.id), headers=_hdrs(key), params={"feedback_type": "route"}
    )
    assert len(by_type.json()) == 2

    by_verdict = await client.get(
        _url(project.id), headers=_hdrs(key), params={"verdict": "negative"}
    )
    assert len(by_verdict.json()) == 2

    by_label = await client.get(
        _url(project.id), headers=_hdrs(key), params={"label": "summary_bad"}
    )
    assert len(by_label.json()) == 1
    assert by_label.json()[0]["feedback_type"] == "summary"


@pytest.mark.asyncio
async def test_list_cursor_pagination(
    client: AsyncClient, db_session: AsyncSession
):
    """The (created_at, id) cursor must not skip a same-timestamp sibling.

    Mirrors the ticket-comment tiebreaker test: two rows forced onto the
    same timestamp advance one id at a time without dropping either."""
    from sessionfs.server.db.models import AgentFeedback

    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)

    shared_ts = datetime.now(timezone.utc)
    db_session.add_all([
        AgentFeedback(
            id="afb_a",
            project_id=project.id,
            ticket_id=ticket.id,
            feedback_type="route",
            verdict="negative",
            label="wrong_route",
            created_at=shared_ts,
        ),
        AgentFeedback(
            id="afb_b",
            project_id=project.id,
            ticket_id=ticket.id,
            feedback_type="route",
            verdict="negative",
            label="wrong_route",
            created_at=shared_ts,
        ),
    ])
    await db_session.commit()

    first = await client.get(_url(project.id), headers=_hdrs(key))
    rows = first.json()
    assert [r["id"] for r in rows] == ["afb_a", "afb_b"]

    follow = await client.get(
        _url(project.id),
        headers=_hdrs(key),
        params={"since": rows[0]["created_at"], "since_id": "afb_a"},
    )
    assert [r["id"] for r in follow.json()] == ["afb_b"]

    done = await client.get(
        _url(project.id),
        headers=_hdrs(key),
        params={"since": rows[1]["created_at"], "since_id": "afb_b"},
    )
    assert done.json() == []


# ── summary ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summary_grouping(client: AsyncClient, db_session: AsyncSession):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)

    for ftype, verdict, label in (
        ("route", "negative", "wrong_route"),
        ("route", "negative", "wrong_route"),
        ("summary", "positive", "summary_good"),
    ):
        await client.post(
            _url(project.id),
            headers=_hdrs(key),
            json={
                "feedback_type": ftype,
                "verdict": verdict,
                "label": label,
                "ticket_id": ticket.id,
            },
        )

    resp = await client.get(f"{_url(project.id)}/summary", headers=_hdrs(key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    groups = {frozenset((g["feedback_type"], g["verdict"], g["label"])): g["count"] for g in body["groups"]}
    assert groups[frozenset(("route", "negative", "wrong_route"))] == 2
    assert groups[frozenset(("summary", "positive", "summary_good"))] == 1


# ── tier gate ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pro_tier_blocked(client: AsyncClient, db_session: AsyncSession):
    user, key = await _make_user(db_session, tier="pro")
    project = await _make_project(db_session, user)
    resp = await client.get(_url(project.id), headers=_hdrs(key))
    assert resp.status_code == 403
    assert _structured_error(resp.json()).get("error") == "upgrade_required"


# ── service-key scope enforcement ────────────────────────────────


@pytest.mark.asyncio
async def test_service_key_read_scope_cannot_write(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session)
    project = await _make_project(db_session, admin, org=org)
    ticket = await _make_ticket(db_session, project)

    read_only = await _make_service_key(
        db_session, org.id, admin, ["agent_feedback:read"]
    )
    resp = await client.post(
        _url(project.id),
        headers=_hdrs(read_only),
        json={
            "feedback_type": "route",
            "verdict": "negative",
            "label": "wrong_route",
            "ticket_id": ticket.id,
        },
    )
    assert resp.status_code == 403
    assert _structured_error(resp.json()).get("error") == "insufficient_scope"


@pytest.mark.asyncio
async def test_service_key_write_scope_can_write(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session)
    project = await _make_project(db_session, admin, org=org)
    ticket = await _make_ticket(db_session, project)

    write_key = await _make_service_key(
        db_session, org.id, admin, ["agent_feedback:write"]
    )
    resp = await client.post(
        _url(project.id),
        headers=_hdrs(write_key),
        json={
            "feedback_type": "route",
            "verdict": "negative",
            "label": "wrong_route",
            "ticket_id": ticket.id,
        },
    )
    assert resp.status_code == 201, resp.text
    # Service-key provenance is stamped on the audit triple.
    assert resp.json()["actor_type"] == "service_key"
    assert resp.json()["service_key_name"].startswith("svc-")


# ── auto-capture on reassignment ─────────────────────────────────


@pytest.mark.asyncio
async def test_auto_capture_route_feedback_on_reassignment(
    client: AsyncClient, db_session: AsyncSession
):
    """An agent-created ticket reassigned to a different persona records
    one `route / negative / wrong_route` row (source=auto), exactly once
    per (ticket, new owner). A human-created ticket records nothing."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={
            "title": "agent-opened",
            "created_by_persona": "atlas",
            "assigned_to": "atlas",
        },
    )
    assert tk.status_code == 201, tk.text
    ticket_id = tk.json()["id"]

    up = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}",
        headers=_hdrs(key),
        json={"assigned_to": "sentinel"},
    )
    assert up.status_code == 200, up.text

    rows = (await client.get(_url(project.id), headers=_hdrs(key))).json()
    assert len(rows) == 1
    row = rows[0]
    assert row["feedback_type"] == "route"
    assert row["verdict"] == "negative"
    assert row["label"] == "wrong_route"
    assert row["final_owner"] == "sentinel"
    assert row["source"] == "auto"
    assert row["ticket_id"] == ticket_id

    # Idempotent: reassigning to the same owner records nothing new.
    await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}",
        headers=_hdrs(key),
        json={"assigned_to": "sentinel"},
    )
    rows = (await client.get(_url(project.id), headers=_hdrs(key))).json()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_no_auto_capture_for_human_ticket(
    client: AsyncClient, db_session: AsyncSession
):
    """A human-created ticket (no created_by_persona, user actor) records
    no route feedback when reassigned."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "human-opened", "assigned_to": "atlas"},
    )
    ticket_id = tk.json()["id"]

    await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}",
        headers=_hdrs(key),
        json={"assigned_to": "sentinel"},
    )

    rows = (await client.get(_url(project.id), headers=_hdrs(key))).json()
    assert rows == []


@pytest.mark.asyncio
async def test_first_assignment_is_not_a_routing_mistake(
    client: AsyncClient, db_session: AsyncSession
):
    """An agent-created ticket that had no owner and then gets one was not
    rerouted, so no wrong_route feedback is recorded."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "agent-opened, unassigned", "created_by_persona": "atlas"},
    )
    assert tk.status_code == 201, tk.text
    up = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tk.json()['id']}",
        headers=_hdrs(key),
        json={"assigned_to": "sentinel"},
    )
    assert up.status_code == 200, up.text

    rows = (await client.get(_url(project.id), headers=_hdrs(key))).json()
    assert rows == []


@pytest.mark.asyncio
async def test_auto_captured_feedback_is_attributed_to_the_reassigner(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "agent-opened", "created_by_persona": "atlas", "assigned_to": "atlas"},
    )
    await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tk.json()['id']}",
        headers=_hdrs(key),
        json={"assigned_to": "sentinel"},
    )
    rows = (await client.get(_url(project.id), headers=_hdrs(key))).json()
    assert len(rows) == 1
    assert rows[0]["created_by_user_id"] == user.id
    assert rows[0]["actor_type"] == "user"
    assert rows[0]["service_key_id"] is None


@pytest.mark.asyncio
async def test_comment_must_belong_to_the_given_ticket(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket_one = await _make_ticket(db_session, project)
    ticket_two = await _make_ticket(db_session, project)
    comment_on_one = await _make_comment(db_session, ticket_one, user)

    resp = await client.post(
        _url(project.id),
        headers=_hdrs(key),
        json={
            "feedback_type": "summary",
            "verdict": "positive",
            "ticket_id": ticket_two.id,
            "comment_id": comment_on_one.id,
        },
    )
    assert resp.status_code == 422, resp.text

    ok = await client.post(
        _url(project.id),
        headers=_hdrs(key),
        json={
            "feedback_type": "summary",
            "verdict": "positive",
            "ticket_id": ticket_one.id,
            "comment_id": comment_on_one.id,
        },
    )
    assert ok.status_code == 201, ok.text


@pytest.mark.asyncio
async def test_persona_filter_credits_wrong_route_to_the_routing_agent(
    client: AsyncClient, db_session: AsyncSession
):
    """A wrong_route record must show up under the agent that created and
    mis-routed the ticket, not only under the persona it was rerouted to."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "agent-opened", "created_by_persona": "atlas", "assigned_to": "atlas"},
    )
    await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tk.json()['id']}",
        headers=_hdrs(key),
        json={"assigned_to": "sentinel"},
    )
    by_creator = (
        await client.get(_url(project.id), headers=_hdrs(key), params={"persona": "atlas"})
    ).json()
    assert [r["label"] for r in by_creator] == ["wrong_route"]
    summary = (
        await client.get(
            _url(project.id) + "/summary", headers=_hdrs(key), params={"persona": "atlas"}
        )
    ).json()
    assert summary["total"] == 1


@pytest.mark.asyncio
async def test_nul_in_free_text_is_a_422_not_a_500(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)
    for field in ("notes", "final_owner"):
        resp = await client.post(
            _url(project.id),
            headers=_hdrs(key),
            json={
                "feedback_type": "route",
                "verdict": "negative",
                "ticket_id": ticket.id,
                field: "bad\x00value",
            },
        )
        assert resp.status_code == 422, (field, resp.text)


@pytest.mark.asyncio
async def test_duplicate_auto_route_record_is_rejected_by_the_database(
    db_session: AsyncSession,
):
    """The partial unique index keeps one automatic wrong_route record per
    (ticket, new owner) even if two reassignments race past the pre-check."""
    from sqlalchemy.exc import IntegrityError as _IntegrityError

    from sessionfs.server.db.models import AgentFeedback

    user, _key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)
    project_id, ticket_id = project.id, ticket.id  # rollback expires the objects

    def row(n: int, source: str = "auto") -> AgentFeedback:
        return AgentFeedback(
            id=f"afb_{n:024d}",
            project_id=project_id,
            ticket_id=ticket_id,
            feedback_type="route",
            verdict="negative",
            label="wrong_route",
            final_owner="sentinel",
            source=source,
        )

    db_session.add(row(1))
    await db_session.commit()
    db_session.add(row(2))
    with pytest.raises(_IntegrityError):
        await db_session.commit()
    await db_session.rollback()
    # A human may still record the same judgement explicitly.
    db_session.add(row(3, source="human"))
    await db_session.commit()


@pytest.mark.asyncio
async def test_human_record_does_not_suppress_the_automatic_one(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "agent-opened", "created_by_persona": "atlas", "assigned_to": "atlas"},
    )
    ticket_id = tk.json()["id"]
    for _ in range(2):  # two matching human rows must not break the check either
        human = await client.post(
            _url(project.id),
            headers=_hdrs(key),
            json={
                "feedback_type": "route",
                "verdict": "negative",
                "label": "wrong_route",
                "final_owner": "sentinel",
                "ticket_id": ticket_id,
            },
        )
        assert human.status_code == 201, human.text

    await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}",
        headers=_hdrs(key),
        json={"assigned_to": "sentinel"},
    )
    rows = (await client.get(_url(project.id), headers=_hdrs(key))).json()
    assert sorted(r["source"] for r in rows) == ["auto", "human", "human"]


@pytest.mark.asyncio
async def test_feedback_capture_failure_never_breaks_the_reassignment(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """If recording the automatic feedback fails, the reassignment and its
    audit still commit (the failure is confined to a savepoint)."""
    from sessionfs.server.db import models as models_module

    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "agent-opened", "created_by_persona": "atlas", "assigned_to": "atlas"},
    )
    ticket_id = tk.json()["id"]

    original_init = models_module.AgentFeedback.__init__

    def boom(self, *args, **kwargs):  # simulate a failing feedback write
        original_init(self, *args, **kwargs)
        self.feedback_type = "not-a-valid-type"  # violates the CHECK on flush

    monkeypatch.setattr(models_module.AgentFeedback, "__init__", boom)
    up = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}",
        headers=_hdrs(key),
        json={"assigned_to": "sentinel"},
    )
    assert up.status_code == 200, up.text
    assert up.json()["assigned_to"] == "sentinel"
    monkeypatch.setattr(models_module.AgentFeedback, "__init__", original_init)

    fresh = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}", headers=_hdrs(key)
    )
    assert fresh.json()["assigned_to"] == "sentinel"
    rows = (await client.get(_url(project.id), headers=_hdrs(key))).json()
    assert rows == []


@pytest.mark.asyncio
async def test_persona_filter_includes_comment_only_feedback(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "agent-opened", "created_by_persona": "atlas", "assigned_to": "atlas"},
    )
    ticket_id = tk.json()["id"]
    comment = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket_id}/comments",
        headers=_hdrs(key),
        json={"content": "summary of the failure", "author_persona": "atlas"},
    )
    assert comment.status_code in (200, 201), comment.text
    comment_id = comment.json()["id"]

    created = await client.post(
        _url(project.id),
        headers=_hdrs(key),
        json={"feedback_type": "summary", "verdict": "positive", "label": "summary_good", "comment_id": comment_id},
    )
    assert created.status_code == 201, created.text

    rows = (await client.get(_url(project.id), headers=_hdrs(key), params={"persona": "atlas"})).json()
    assert [r["comment_id"] for r in rows] == [comment_id]
    summary = (
        await client.get(_url(project.id) + "/summary", headers=_hdrs(key), params={"persona": "atlas"})
    ).json()
    assert summary["total"] == 1


@pytest.mark.asyncio
async def test_nul_in_target_ids_and_filters_is_a_422(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    for field in ("agent_run_id", "ticket_id", "comment_id"):
        resp = await client.post(
            _url(project.id),
            headers=_hdrs(key),
            json={"feedback_type": "route", "verdict": "negative", field: "tk_bad\x00id"},
        )
        assert resp.status_code == 422, (field, resp.text)
    too_long = await client.post(
        _url(project.id),
        headers=_hdrs(key),
        json={"feedback_type": "route", "verdict": "negative", "ticket_id": "t" * 65},
    )
    assert too_long.status_code == 422
    listed = await client.get(_url(project.id), headers=_hdrs(key), params={"ticket_id": "a\x00b"})
    assert listed.status_code == 422
    summary = await client.get(_url(project.id) + "/summary", headers=_hdrs(key), params={"persona": "a\x00b"})
    assert summary.status_code == 422


@pytest.mark.asyncio
async def test_since_with_a_timezone_offset_is_normalized_to_utc(
    client: AsyncClient, db_session: AsyncSession
):
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)
    created = await client.post(
        _url(project.id),
        headers=_hdrs(key),
        json={"feedback_type": "summary", "verdict": "positive", "ticket_id": ticket.id},
    )
    assert created.status_code == 201
    one_hour_ago_utc = _dt.now(_tz.utc) - _td(hours=1)
    # Same instant, written in UTC-05:00: must still include the record.
    offset = one_hour_ago_utc.astimezone(_tz(_td(hours=-5))).isoformat()
    rows = (await client.get(_url(project.id), headers=_hdrs(key), params={"since": offset})).json()
    assert len(rows) == 1
    summary = (
        await client.get(_url(project.id) + "/summary", headers=_hdrs(key), params={"since": offset})
    ).json()
    assert summary["total"] == 1
    # And a cutoff one hour in the FUTURE, in UTC-05:00, must exclude it.
    future = (_dt.now(_tz.utc) + _td(hours=1)).astimezone(_tz(_td(hours=-5))).isoformat()
    rows = (await client.get(_url(project.id), headers=_hdrs(key), params={"since": future})).json()
    assert rows == []
    summary = (
        await client.get(_url(project.id) + "/summary", headers=_hdrs(key), params={"since": future})
    ).json()
    assert summary["total"] == 0


@pytest.mark.asyncio
async def test_cursor_does_not_skip_rows_with_server_default_timestamps(
    client: AsyncClient, db_session: AsyncSession
):
    """Rows whose created_at comes from the database default (no fractional
    seconds on SQLite) and share a second must all be reachable by cursor."""
    from sessionfs.server.db.models import AgentFeedback

    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, project)
    for n in (1, 2, 3):
        db_session.add(
            AgentFeedback(
                id=f"afb_{n:024d}",
                project_id=project.id,
                ticket_id=ticket.id,
                feedback_type="summary",
                verdict="positive",
            )
        )
    await db_session.commit()

    seen: list[str] = []
    params: dict = {"limit": 1}
    for _ in range(5):
        page = (await client.get(_url(project.id), headers=_hdrs(key), params=params)).json()
        if not page:
            break
        seen.append(page[0]["id"])
        params = {"limit": 1, "since": page[0]["created_at"], "since_id": page[0]["id"]}
    assert seen == [f"afb_{n:024d}" for n in (1, 2, 3)]


@pytest.mark.asyncio
async def test_nul_in_the_project_id_path_is_a_422(client: AsyncClient, db_session: AsyncSession):
    user, key = await _make_user(db_session)
    for path in ("/api/v1/projects/%00/agent-feedback", "/api/v1/projects/%00/agent-feedback/summary"):
        resp = await client.get(path, headers=_hdrs(key))
        assert resp.status_code == 422, (path, resp.status_code, resp.text)
    resp = await client.post(
        "/api/v1/projects/%00/agent-feedback",
        headers=_hdrs(key),
        json={"feedback_type": "route", "verdict": "negative", "ticket_id": "tk_x"},
    )
    assert resp.status_code == 422, resp.text

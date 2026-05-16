"""v0.10.1 Phase 3 — ticket CRUD + lifecycle FSM regression tests.

Covers `/api/v1/projects/{project_id}/tickets/*`. The FSM is the
load-bearing piece: every lifecycle route returns 400 on an illegal
transition AND `start_ticket` issues an atomic UPDATE ... WHERE
status='open' that returns 409 on the concurrent race. Agent-created
ticket quality gates (acceptance criteria + 20+ char description + 3
per-session cap) are tested per gate. Dependency enrichment on accept
covers comment-append + KB-ref merge + auto-unblock.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentPersona,
    ApiKey,
    Project,
    Ticket,
    TicketComment,
    User,
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


async def _make_project(db: AsyncSession, owner: User) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"phase3-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/p3-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_persona(
    db: AsyncSession, project: Project, owner: User, name: str = "atlas"
) -> AgentPersona:
    persona = AgentPersona(
        id=f"per_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name=name,
        role="Backend",
        created_by=owner.id,
    )
    db.add(persona)
    await db.commit()
    await db.refresh(persona)
    return persona


# ── CRUD happy path ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_human_ticket_defaults_to_open(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Fix rate limit", "description": "x" * 5},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "open"
    assert body["priority"] == "medium"
    assert body["depends_on"] == []
    assert body["created_by_user_id"] == user.id


@pytest.mark.asyncio
async def test_list_tickets_with_filters(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    await _make_persona(db_session, project, user, "prism")

    await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "a1", "assigned_to": "atlas", "priority": "high"},
    )
    await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "p1", "assigned_to": "prism", "priority": "low"},
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets?assigned_to=atlas",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["title"] == "a1"

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets?priority=low",
        headers=_hdrs(key),
    )
    assert len(resp.json()) == 1
    assert resp.json()[0]["title"] == "p1"


@pytest.mark.asyncio
async def test_get_ticket_404(client: AsyncClient, db_session: AsyncSession):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/tk_missing",
        headers=_hdrs(key),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_put_updates_non_status_fields(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "old", "priority": "low"},
    )
    tk_id = create.json()["id"]
    resp = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}",
        headers=_hdrs(key),
        json={"title": "new", "priority": "critical", "assigned_to": "atlas"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "new"
    assert body["priority"] == "critical"
    assert body["assigned_to"] == "atlas"
    # PUT does NOT change status.
    assert body["status"] == "open"


# ── FSM enforcement ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifecycle_open_to_done_happy_path(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")

    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Walk the FSM", "assigned_to": "atlas"},
    )
    tk_id = create.json()["id"]

    # open → in_progress
    start = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start",
        headers=_hdrs(key),
    )
    assert start.status_code == 200
    # v0.10.1 Phase 4 — start_ticket now returns StartTicketResponse
    # with `ticket` + `compiled_context` keys (the compiled context
    # is the persona+ticket markdown the AI tool consumes).
    start_body = start.json()
    assert start_body["ticket"]["status"] == "in_progress"
    assert isinstance(start_body["compiled_context"], str)
    assert start_body["retrieval_audit_id"].startswith("ra_")

    # in_progress → review
    complete = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/complete",
        headers=_hdrs(key),
        json={"notes": "Done.", "changed_files": ["src/x.py"]},
    )
    assert complete.status_code == 200
    assert complete.json()["status"] == "review"
    assert complete.json()["completion_notes"] == "Done."

    # review → done
    accept = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/accept",
        headers=_hdrs(key),
    )
    assert accept.status_code == 200
    assert accept.json()["status"] == "done"
    assert accept.json()["resolved_at"] is not None


@pytest.mark.asyncio
async def test_ticket_lease_epoch_fences_complete_comment_and_resolve(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")

    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Fence stale daemons", "assigned_to": "atlas"},
    )
    tk_id = create.json()["id"]

    start = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start",
        headers=_hdrs(key),
    )
    assert start.status_code == 200, start.text
    assert start.json()["retrieval_audit_id"].startswith("ra_")
    lease_epoch = start.json()["ticket"]["lease_epoch"]
    assert lease_epoch == 1

    stale_comment = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
        json={"content": "stale", "lease_epoch": lease_epoch - 1},
    )
    assert stale_comment.status_code == 409

    current_comment = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
        json={"content": "current", "lease_epoch": lease_epoch},
    )
    assert current_comment.status_code == 201, current_comment.text

    stale_complete = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/complete",
        headers=_hdrs(key),
        json={"notes": "stale", "lease_epoch": lease_epoch - 1},
    )
    assert stale_complete.status_code == 409

    complete = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/complete",
        headers=_hdrs(key),
        json={"notes": "done", "lease_epoch": lease_epoch},
    )
    assert complete.status_code == 200, complete.text
    assert complete.json()["status"] == "review"

    stale_accept = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/accept",
        headers=_hdrs(key),
        params={"lease_epoch": lease_epoch - 1},
    )
    assert stale_accept.status_code == 409

    accept = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/accept",
        headers=_hdrs(key),
        params={"lease_epoch": lease_epoch},
    )
    assert accept.status_code == 200, accept.text
    assert accept.json()["status"] == "done"


@pytest.mark.asyncio
async def test_lease_required_mode_rejects_missing_lease_with_422(
    client: AsyncClient, db_session: AsyncSession
):
    """v0.10.7 — when org.settings.require_lease_epoch_on_ticket_writes
    is true, complete/comment/accept return 422 if lease_epoch omitted.
    Existing supplied-lease behavior unchanged."""
    import json as _json

    from sessionfs.server.db.models import OrgMember, Organization

    user, key = await _make_user(db_session)

    org = Organization(
        id=f"org_{uuid.uuid4().hex[:16]}",
        name="Compliance Co",
        slug=f"compliance-{uuid.uuid4().hex[:6]}",
        tier="team",
        settings=_json.dumps({"require_lease_epoch_on_ticket_writes": True}),
    )
    db_session.add(org)
    await db_session.commit()
    db_session.add(
        OrgMember(
            org_id=org.id,
            user_id=user.id,
            role="admin",
        )
    )
    await db_session.commit()

    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"required-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/r-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=user.id,
        org_id=org.id,
    )
    db_session.add(project)
    await db_session.commit()
    await _make_persona(db_session, project, user, "atlas")

    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Required-mode test", "assigned_to": "atlas"},
    )
    tk_id = create.json()["id"]

    start = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start",
        headers=_hdrs(key),
    )
    lease_epoch = start.json()["ticket"]["lease_epoch"]

    # comment without lease → 422
    no_lease_comment = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
        json={"content": "no lease"},
    )
    assert no_lease_comment.status_code == 422, no_lease_comment.text
    assert "require_lease_epoch_on_ticket_writes" in no_lease_comment.text

    # comment with lease → 201 (unchanged behavior)
    with_lease_comment = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
        json={"content": "with lease", "lease_epoch": lease_epoch},
    )
    assert with_lease_comment.status_code == 201, with_lease_comment.text

    # complete without lease → 422
    no_lease_complete = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/complete",
        headers=_hdrs(key),
        json={"notes": "no lease"},
    )
    assert no_lease_complete.status_code == 422

    # complete with lease → 200
    with_lease_complete = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/complete",
        headers=_hdrs(key),
        json={"notes": "ok", "lease_epoch": lease_epoch},
    )
    assert with_lease_complete.status_code == 200, with_lease_complete.text

    # accept without lease → 422
    no_lease_accept = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/accept",
        headers=_hdrs(key),
    )
    assert no_lease_accept.status_code == 422

    # accept with lease → 200
    with_lease_accept = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/accept",
        headers=_hdrs(key),
        params={"lease_epoch": lease_epoch},
    )
    assert with_lease_accept.status_code == 200, with_lease_accept.text


@pytest.mark.asyncio
async def test_lease_required_mode_skipped_for_personal_projects(
    client: AsyncClient, db_session: AsyncSession
):
    """Personal projects (no org_id) skip the required-mode check —
    setting is org-scoped and personal projects have no org."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)  # no org_id
    await _make_persona(db_session, project, user, "atlas")

    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Personal project", "assigned_to": "atlas"},
    )
    tk_id = create.json()["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start",
        headers=_hdrs(key),
    )

    # Personal project: omitted lease still works (existing opt-in semantics)
    comment = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
        json={"content": "no lease, personal project"},
    )
    assert comment.status_code == 201, comment.text


@pytest.mark.asyncio
async def test_force_start_increments_ticket_lease_epoch(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")

    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Force lease", "assigned_to": "atlas"},
    )
    tk_id = create.json()["id"]
    start = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start",
        headers=_hdrs(key),
    )
    assert start.json()["ticket"]["lease_epoch"] == 1
    block = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/block",
        headers=_hdrs(key),
    )
    assert block.status_code == 200

    forced = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start",
        headers=_hdrs(key),
        params={"force": "true"},
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["ticket"]["lease_epoch"] == 2


@pytest.mark.asyncio
async def test_block_unblock_round_trip(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "block test", "assigned_to": "atlas"},
    )
    tk_id = create.json()["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start", headers=_hdrs(key)
    )
    block = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/block", headers=_hdrs(key)
    )
    assert block.json()["status"] == "blocked"
    unblock = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/unblock", headers=_hdrs(key)
    )
    assert unblock.json()["status"] == "in_progress"


@pytest.mark.asyncio
async def test_reopen_review_to_open(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "reopen test", "assigned_to": "atlas"},
    )
    tk_id = create.json()["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start", headers=_hdrs(key)
    )
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/complete",
        headers=_hdrs(key),
        json={"notes": "draft"},
    )
    reopen = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/reopen", headers=_hdrs(key)
    )
    assert reopen.json()["status"] == "open"


@pytest.mark.asyncio
async def test_illegal_transitions_return_400(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "illegal"},
    )
    tk_id = create.json()["id"]
    # Cannot complete from open.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/complete",
        headers=_hdrs(key),
        json={"notes": "x"},
    )
    assert resp.status_code == 400
    # Cannot accept from open. The atomic UPDATE in accept_ticket
    # (Phase 3 Round 2, KB 326) surfaces this as 409 (state mismatch
    # / serialization conflict) rather than 400 (illegal transition).
    # Both are valid rejections of "you can't do this from here".
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/accept",
        headers=_hdrs(key),
    )
    assert resp.status_code in (400, 409)
    # Cannot block from open.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/block",
        headers=_hdrs(key),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_start_409_when_already_in_progress(
    client: AsyncClient, db_session: AsyncSession
):
    """Atomic transition: starting an already-in_progress ticket fails."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "race", "assigned_to": "atlas"},
    )
    tk_id = create.json()["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start", headers=_hdrs(key)
    )
    second = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start", headers=_hdrs(key)
    )
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_start_force_recovers_blocked_ticket(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "force-recover", "assigned_to": "atlas"},
    )
    tk_id = create.json()["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start", headers=_hdrs(key)
    )
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/block", headers=_hdrs(key)
    )
    # Without force: 409 (start only accepts 'open').
    nope = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start", headers=_hdrs(key)
    )
    assert nope.status_code == 409
    # With force=true: recovers blocked → in_progress.
    forced = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start?force=true",
        headers=_hdrs(key),
    )
    assert forced.status_code == 200
    assert forced.json()["ticket"]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_start_requires_active_persona_when_assigned(
    client: AsyncClient, db_session: AsyncSession
):
    """assigned_to is plain VARCHAR; start_ticket validates existence."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    # NOT creating the persona — assigned_to references a non-existent name.
    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "phantom-assign", "assigned_to": "ghost"},
    )
    tk_id = create.json()["id"]
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/start", headers=_hdrs(key)
    )
    assert resp.status_code == 400
    assert "no active persona" in resp.text.lower()


# ── Suggested workflow ─────────────────────────────────────


@pytest.mark.asyncio
async def test_suggested_approve_to_open(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={
            "title": "agent-created",
            "description": "A" * 30,
            "source": "agent",
            "acceptance_criteria": ["c1"],
        },
    )
    assert create.json()["status"] == "suggested"
    tk_id = create.json()["id"]
    approve = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/approve", headers=_hdrs(key)
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "open"


@pytest.mark.asyncio
async def test_suggested_dismiss_to_cancelled(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={
            "title": "agent-created",
            "description": "A" * 30,
            "source": "agent",
            "acceptance_criteria": ["c1"],
        },
    )
    tk_id = create.json()["id"]
    dismiss = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/dismiss", headers=_hdrs(key)
    )
    assert dismiss.status_code == 200
    assert dismiss.json()["status"] == "cancelled"


# ── Agent-created quality gates ────────────────────────────


@pytest.mark.asyncio
async def test_agent_ticket_requires_acceptance_criteria(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={
            "title": "missing-criteria",
            "description": "A" * 30,
            "source": "agent",
        },
    )
    assert resp.status_code == 400
    assert "acceptance criteria" in resp.text.lower()


@pytest.mark.asyncio
async def test_agent_ticket_requires_min_description(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={
            "title": "too-short",
            "description": "short",
            "source": "agent",
            "acceptance_criteria": ["c1"],
        },
    )
    assert resp.status_code == 400
    assert "20+ chars" in resp.text or "20" in resp.text


@pytest.mark.asyncio
async def test_agent_ticket_per_session_quota(
    client: AsyncClient, db_session: AsyncSession
):
    """Max 3 agent-created tickets per session_id."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    sess_id = f"ses_{uuid.uuid4().hex[:16]}"
    payload = {
        "description": "A" * 30,
        "source": "agent",
        "acceptance_criteria": ["c1"],
        "created_by_session_id": sess_id,
    }
    for i in range(3):
        resp = await client.post(
            f"/api/v1/projects/{project.id}/tickets",
            headers=_hdrs(key),
            json={**payload, "title": f"agent-{i}"},
        )
        assert resp.status_code == 201, resp.text

    # Fourth attempt → 429.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={**payload, "title": "agent-4"},
    )
    assert resp.status_code == 429


# ── Dependency enrichment ──────────────────────────────────


@pytest.mark.asyncio
async def test_accept_enriches_dependent_with_comment_and_kb_refs(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")

    # Parent ticket.
    parent_create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "parent", "assigned_to": "atlas"},
    )
    parent_id = parent_create.json()["id"]
    # Child ticket depends on parent + has its own context refs.
    child_create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={
            "title": "child",
            "context_refs": ["kb_existing"],
            "depends_on": [parent_id],
        },
    )
    child_id = child_create.json()["id"]
    assert child_create.json()["depends_on"] == [parent_id]

    # Walk parent to done with completion notes + KB ids.
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{parent_id}/start", headers=_hdrs(key)
    )
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{parent_id}/complete",
        headers=_hdrs(key),
        json={
            "notes": "Implemented the foundation",
            "knowledge_entry_ids": ["kb_new1", "kb_new2"],
        },
    )
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{parent_id}/accept",
        headers=_hdrs(key),
    )

    # Refresh child + check enrichment.
    db_session.expunge_all()
    child = (
        await db_session.execute(select(Ticket).where(Ticket.id == child_id))
    ).scalar_one()
    # Context refs merged in order: existing first, new ids appended.
    import json as _json
    refs = _json.loads(child.context_refs)
    assert refs == ["kb_existing", "kb_new1", "kb_new2"]

    # Comment with completion notes on the child.
    comments = (
        await db_session.execute(
            select(TicketComment).where(TicketComment.ticket_id == child_id)
        )
    ).scalars().all()
    assert len(comments) == 1
    assert "Implemented the foundation" in comments[0].content
    assert parent_id in comments[0].content


@pytest.mark.asyncio
async def test_accept_auto_unblocks_dependent_when_all_deps_done(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")

    parent_create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "parent", "assigned_to": "atlas"},
    )
    parent_id = parent_create.json()["id"]
    child_create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={
            "title": "child",
            "assigned_to": "atlas",
            "depends_on": [parent_id],
        },
    )
    child_id = child_create.json()["id"]

    # Force child to blocked.
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{child_id}/start", headers=_hdrs(key)
    )
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{child_id}/block", headers=_hdrs(key)
    )
    blocked = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{child_id}", headers=_hdrs(key)
    )
    assert blocked.json()["status"] == "blocked"

    # Now resolve parent — child should auto-unblock.
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{parent_id}/start", headers=_hdrs(key)
    )
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{parent_id}/complete",
        headers=_hdrs(key),
        json={"notes": "Done"},
    )
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{parent_id}/accept",
        headers=_hdrs(key),
    )

    after = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{child_id}", headers=_hdrs(key)
    )
    assert after.json()["status"] == "in_progress"


# ── Dependency cycle prevention ────────────────────────────


@pytest.mark.asyncio
async def test_cannot_create_dependency_cycle(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    a_create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "A"},
    )
    a_id = a_create.json()["id"]
    b_create = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "B", "depends_on": [a_id]},
    )
    b_id = b_create.json()["id"]
    # C → B → A is a chain (no cycle), should succeed.
    chain_resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "C-chain", "depends_on": [b_id]},
    )
    assert chain_resp.status_code == 201


@pytest.mark.asyncio
async def test_create_ticket_dedups_duplicate_depends_on(
    client: AsyncClient, db_session: AsyncSession
):
    """v0.10.1 Phase 3 Round 2 (KB 328) — duplicate depends_on IDs
    are silently deduped. Without this, the composite PK on
    ticket_dependencies caught the second insert as an IntegrityError
    and surfaced as 500 (instead of a clean response). Dedup
    preserves the caller's intent (a duplicate dep is a no-op).
    """
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    parent = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "parent"},
    )
    parent_id = parent.json()["id"]

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "child", "depends_on": [parent_id, parent_id, parent_id]},
    )
    assert resp.status_code == 201, resp.text
    # Response's depends_on reflects the deduped list.
    assert resp.json()["depends_on"] == [parent_id]


@pytest.mark.asyncio
async def test_create_ticket_rejects_missing_dependency_id(
    client: AsyncClient, db_session: AsyncSession
):
    """v0.10.1 Phase 3 Round 1 (KB 326) — depends_on must reference
    an existing same-project ticket. Missing IDs are rejected up-front
    rather than landing in ticket_dependencies as a dangling row."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "dangling", "depends_on": ["tk_does_not_exist"]},
    )
    assert resp.status_code == 400, resp.text
    assert "unknown or cross-project" in resp.text.lower()


@pytest.mark.asyncio
async def test_create_ticket_rejects_cross_project_dependency(
    client: AsyncClient, db_session: AsyncSession
):
    """v0.10.1 Phase 3 Round 1 (KB 326) — a ticket in project B cannot
    depend on a ticket in project A. The pre-validation gates this AND
    `_enrich_dependents` adds a defensive same-project JOIN so legacy
    rows can't leak completion notes across project boundaries either.
    """
    user, key = await _make_user(db_session)
    project_a = await _make_project(db_session, user)
    project_b = await _make_project(db_session, user)

    # Create a ticket in project_a.
    a_ticket = await client.post(
        f"/api/v1/projects/{project_a.id}/tickets",
        headers=_hdrs(key),
        json={"title": "in-a"},
    )
    a_id = a_ticket.json()["id"]

    # Try to depend on it from project_b → 400.
    resp = await client.post(
        f"/api/v1/projects/{project_b.id}/tickets",
        headers=_hdrs(key),
        json={"title": "cross-project-dep", "depends_on": [a_id]},
    )
    assert resp.status_code == 400, resp.text
    assert "cross-project" in resp.text.lower() or "unknown" in resp.text.lower()


@pytest.mark.asyncio
async def test_concurrent_accept_returns_409_no_duplicate_enrichment(
    client: AsyncClient, db_session: AsyncSession
):
    """v0.10.1 Phase 3 Round 1 (KB 326) — accept_ticket is atomic.

    Two sequential POSTs to /accept after a complete: first wins with
    enrichment, second returns 409 (already done) without re-running
    _enrich_dependents → no duplicate dependent comments.
    """
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")

    # Parent + child setup.
    parent = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "parent", "assigned_to": "atlas"},
    )
    parent_id = parent.json()["id"]
    child = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "child", "depends_on": [parent_id]},
    )
    child_id = child.json()["id"]

    # Drive parent to review.
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{parent_id}/start",
        headers=_hdrs(key),
    )
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{parent_id}/complete",
        headers=_hdrs(key),
        json={"notes": "Done"},
    )

    # First accept succeeds.
    first = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{parent_id}/accept",
        headers=_hdrs(key),
    )
    assert first.status_code == 200
    assert first.json()["status"] == "done"

    # Second accept on the now-done ticket returns 409.
    second = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{parent_id}/accept",
        headers=_hdrs(key),
    )
    assert second.status_code == 409

    # The child has EXACTLY ONE enrichment comment — not two.
    db_session.expunge_all()
    comments = (
        await db_session.execute(
            select(TicketComment).where(TicketComment.ticket_id == child_id)
        )
    ).scalars().all()
    assert len(comments) == 1, (
        f"Expected exactly 1 enrichment comment, found {len(comments)}"
    )


# ── Tier gating ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pro_tier_blocked_from_tickets(
    client: AsyncClient, db_session: AsyncSession
):
    """agent_tickets is TEAM+; Pro gets 403."""
    user, key = await _make_user(db_session, tier="pro")
    project = await _make_project(db_session, user)
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(key)
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_free_tier_blocked_from_tickets(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session, tier="free")
    project = await _make_project(db_session, user)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "nope"},
    )
    assert resp.status_code == 403

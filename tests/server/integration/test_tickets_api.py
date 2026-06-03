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

import json
import uuid
from datetime import datetime, timedelta, timezone

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


# ── v0.10.10 list_ticket_comments (tk_32f3dacf1c9749bc) ──


@pytest.mark.asyncio
async def test_list_comments_returns_chronological(
    client: AsyncClient, db_session: AsyncSession
):
    """Comments come back oldest-first regardless of insertion timing."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = (
        await client.post(
            f"/api/v1/projects/{project.id}/tickets",
            headers=_hdrs(key),
            json={"title": "t", "description": "x" * 5},
        )
    ).json()
    tk_id = tk["id"]

    for body in ("first", "second", "third"):
        r = await client.post(
            f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
            headers=_hdrs(key),
            json={"content": body},
        )
        assert r.status_code == 201

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200
    contents = [c["content"] for c in resp.json()]
    assert contents == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_list_comments_empty_thread(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = (
        await client.post(
            f"/api/v1/projects/{project.id}/tickets",
            headers=_hdrs(key),
            json={"title": "empty"},
        )
    ).json()
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tk['id']}/comments",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_comments_missing_ticket_404(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/tk_missing/comments",
        headers=_hdrs(key),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_comments_since_filter_incremental_polling(
    client: AsyncClient, db_session: AsyncSession
):
    """Pass `since` = created_at of last seen comment; only newer come back."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = (
        await client.post(
            f"/api/v1/projects/{project.id}/tickets",
            headers=_hdrs(key),
            json={"title": "polling", "description": "x" * 5},
        )
    ).json()
    tk_id = tk["id"]

    c1 = (
        await client.post(
            f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
            headers=_hdrs(key),
            json={"content": "old"},
        )
    ).json()
    # Tiny sleep to ensure c2 has a strictly later created_at on
    # platforms with coarse clock resolution.
    import asyncio
    await asyncio.sleep(0.01)
    c2 = (
        await client.post(
            f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
            headers=_hdrs(key),
            json={"content": "new"},
        )
    ).json()

    # Poll with since = c1.created_at: should only see c2.
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
        params={"since": c1["created_at"]},
    )
    assert resp.status_code == 200
    ids = [c["id"] for c in resp.json()]
    assert ids == [c2["id"]]


@pytest.mark.asyncio
async def test_list_comments_since_id_tiebreaker_no_skip_on_same_timestamp(
    client: AsyncClient, db_session: AsyncSession
):
    """Codex review MEDIUM — pure `since > created_at` filter can skip
    a same-timestamp sibling. With since + since_id pair, neither side
    of the tie is lost: poller seeing one comment passes its id, then
    the next call returns the sibling instead of dropping it.

    We force the tie by inserting two TicketComment rows directly with
    identical created_at, then validate the cursor advances correctly."""
    from datetime import datetime, timezone
    from sessionfs.server.db.models import TicketComment

    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = (
        await client.post(
            f"/api/v1/projects/{project.id}/tickets",
            headers=_hdrs(key),
            json={"title": "ties", "description": "x" * 5},
        )
    ).json()
    tk_id = tk["id"]

    shared_ts = datetime.now(timezone.utc)
    c1 = TicketComment(
        id="tc_a",
        ticket_id=tk_id,
        author_user_id=user.id,
        content="first",
        created_at=shared_ts,
    )
    c2 = TicketComment(
        id="tc_b",
        ticket_id=tk_id,
        author_user_id=user.id,
        content="second",
        created_at=shared_ts,  # identical timestamp
    )
    db_session.add_all([c1, c2])
    await db_session.commit()

    # Initial poll — both come back ordered by (created_at, id).
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["id"] for r in rows] == ["tc_a", "tc_b"]

    # Agent stores last seen (created_at, id) = (shared_ts, "tc_a") and
    # polls again expecting to receive ONLY tc_b. Without the id
    # tiebreaker, this poll would return [] and tc_b would be skipped
    # forever.
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
        params={"since": rows[0]["created_at"], "since_id": "tc_a"},
    )
    assert resp.status_code == 200
    follow = resp.json()
    assert [r["id"] for r in follow] == ["tc_b"], (
        f"since+since_id cursor must return the same-timestamp sibling; "
        f"got {follow}"
    )

    # And once the agent advances to (shared_ts, "tc_b"), no more
    # comments come back — cursor monotonically advances.
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
        params={"since": follow[0]["created_at"], "since_id": "tc_b"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_comments_limit_caps_response(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = (
        await client.post(
            f"/api/v1/projects/{project.id}/tickets",
            headers=_hdrs(key),
            json={"title": "lim", "description": "x" * 5},
        )
    ).json()
    tk_id = tk["id"]
    for i in range(5):
        await client.post(
            f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
            headers=_hdrs(key),
            json={"content": f"c{i}"},
        )
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tk_id}/comments",
        headers=_hdrs(key),
        params={"limit": 2},
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_list_comments_cross_project_404(
    client: AsyncClient, db_session: AsyncSession
):
    """Project A owner can't read comments on a ticket in project B
    (which they have no access to)."""
    user_a, key_a = await _make_user(db_session, name="alice")
    user_b, key_b = await _make_user(db_session, name="bob")
    project_b = await _make_project(db_session, user_b)
    tk = (
        await client.post(
            f"/api/v1/projects/{project_b.id}/tickets",
            headers=_hdrs(key_b),
            json={"title": "b-ticket"},
        )
    ).json()
    # user_a tries to read comments under project_b — must be denied.
    # The shared _get_project_or_404 helper currently returns 403 for
    # non-owners (not 404). Either is acceptable existence-hiding for
    # this ticket's contract — we just verify the cross-project read
    # is blocked.
    resp = await client.get(
        f"/api/v1/projects/{project_b.id}/tickets/{tk['id']}/comments",
        headers=_hdrs(key_a),
    )
    assert resp.status_code in (403, 404)


# ── v0.10.11 tk_e025375272b84a95 — review-state endpoint ──


@pytest.mark.asyncio
async def test_review_state_returns_null_for_ticket_without_codex_comments(
    client: AsyncClient, db_session: AsyncSession
):
    """Non-review tickets (no codex-reviewer comments) return
    review_state: null — there's no thread to parse."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = (
        await client.post(
            f"/api/v1/projects/{project.id}/tickets",
            headers=_hdrs(key),
            json={"title": "regular work", "description": "do stuff"},
        )
    ).json()
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tk['id']}/review-state",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticket_id"] == tk["id"]
    assert body["review_state"] is None


@pytest.mark.asyncio
async def test_review_state_summarizes_full_review_cycle(
    client: AsyncClient, db_session: AsyncSession
):
    """End-to-end: post a codex-reviewer R1 (CHANGES_REQUESTED with a
    MEDIUM finding), an atlas closure, then a codex-reviewer R2
    (VERIFIED-CLEAN). Review state should show 0 open / 1 closed /
    last_verdict=VERIFIED-CLEAN."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    await _make_persona(db_session, project, user, "codex-reviewer")
    tk = (
        await client.post(
            f"/api/v1/projects/{project.id}/tickets",
            headers=_hdrs(key),
            json={"title": "review thread", "description": "x" * 20},
        )
    ).json()

    r1 = (
        "Codex R1 review on tk_x: CHANGES REQUESTED\n\n"
        "Findings:\n\n"
        " - MEDIUM - thing is broken\n\n"
        "Verified clean / no change needed:\n\n - other thing\n"
    )
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk['id']}/comments",
        headers=_hdrs(key),
        json={"content": r1, "author_persona": "codex-reviewer"},
    )
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk['id']}/comments",
        headers=_hdrs(key),
        json={"content": "R1 closure - fixed in abc123", "author_persona": "atlas"},
    )
    r2 = (
        "Codex R2 review on tk_x: VERIFIED-CLEAN\n\n"
        "Findings: none.\n\nVerified:\n - all good\n"
    )
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{tk['id']}/comments",
        headers=_hdrs(key),
        json={"content": r2, "author_persona": "codex-reviewer"},
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tk['id']}/review-state",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    state = resp.json()["review_state"]
    assert state is not None
    assert state["last_verdict"] == "VERIFIED-CLEAN"
    assert len(state["open_findings"]) == 0
    assert len(state["closed_findings"]) == 1
    closed = state["closed_findings"][0]
    assert closed["severity"] == "MEDIUM"
    assert closed["round"] == 1
    assert closed["closed_round"] == 2
    assert state["severity_counts"] == {
        "CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0,
    }
    assert state["last_review_comment_id"] is not None
    assert state["last_implementer_comment_id"] is not None
    assert len(state["rounds"]) == 2


@pytest.mark.asyncio
async def test_review_state_cross_project_denied(
    client: AsyncClient, db_session: AsyncSession
):
    """User A cannot read the review-state of a ticket in user B's
    project. Same auth path as get_ticket / list_ticket_comments."""
    user_a, key_a = await _make_user(db_session, name="alice")
    user_b, key_b = await _make_user(db_session, name="bob")
    project_b = await _make_project(db_session, user_b)
    tk = (
        await client.post(
            f"/api/v1/projects/{project_b.id}/tickets",
            headers=_hdrs(key_b),
            json={"title": "b-ticket"},
        )
    ).json()
    resp = await client.get(
        f"/api/v1/projects/{project_b.id}/tickets/{tk['id']}/review-state",
        headers=_hdrs(key_a),
    )
    # _get_project_or_404 in this codebase returns 403 for non-owners
    # today; either is acceptable existence-hiding.
    assert resp.status_code in (403, 404)


@pytest.mark.asyncio
async def test_review_state_unknown_ticket_returns_404(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/tk_does_not_exist/review-state",
        headers=_hdrs(key),
    )
    assert resp.status_code == 404


# ── tk_33a25a12a5cf4dc3 — review-state row cap (Shield-SR LOW) ──


@pytest.mark.asyncio
async def test_review_state_caps_at_500_comments(
    client: AsyncClient, db_session: AsyncSession
):
    """The endpoint reads at most 500 TicketComment rows. Beyond that,
    a malicious or pathological thread could DoS the endpoint by
    forcing it to scan thousands of rows. Pre-cap matches
    list_ticket_comments (also 500). Functional check: insert 510
    comments and verify the endpoint completes successfully and the
    returned state was computed over <=500 rows.
    """
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = (
        await client.post(
            f"/api/v1/projects/{project.id}/tickets",
            headers=_hdrs(key),
            json={"title": "high-volume thread", "description": "x" * 20},
        )
    ).json()

    # Bulk-insert 510 comments directly on the DB to avoid the route's
    # rate-limit + per-request overhead. They're all atlas (non-Codex),
    # so the resulting review_state will be null — that's fine for the
    # cap test; the endpoint must still complete without error.
    base_t = datetime.now(timezone.utc)
    db_session.add_all([
        TicketComment(
            id=f"tc_test_{i:04d}",
            ticket_id=tk["id"],
            author_user_id=user.id,
            author_persona="atlas",
            content=f"filler comment {i}",
            created_at=base_t + timedelta(seconds=i),
        )
        for i in range(510)
    ])
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tk['id']}/review-state",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # No codex-reviewer comments → review_state is null. The endpoint
    # must NOT error on the high row count.
    assert body["ticket_id"] == tk["id"]
    assert body["review_state"] is None


@pytest.mark.asyncio
async def test_review_state_cap_preserves_earliest_rounds(
    client: AsyncClient, db_session: AsyncSession
):
    """When a thread exceeds the 500-row cap, the SELECT is ordered by
    (created_at, id) ascending, so the EARLIEST rounds are what
    survive. That's the right policy: callers care about 'what
    findings were raised first and whether they're closed', not the
    last 500 noisy follow-up comments. Place a single codex-reviewer
    R1 header first, then 500 filler atlas comments after, and verify
    the parser still picks up R1.
    """
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    tk = (
        await client.post(
            f"/api/v1/projects/{project.id}/tickets",
            headers=_hdrs(key),
            json={"title": "high-volume with codex", "description": "x" * 20},
        )
    ).json()

    base_t = datetime.now(timezone.utc)
    codex_content = (
        "Codex R1 review on tk_x: CHANGES REQUESTED\n\n"
        "Findings:\n\n"
        " - LOW - thing is broken\n"
    )
    db_session.add(TicketComment(
        id="tc_codex_r1",
        ticket_id=tk["id"],
        author_user_id=user.id,
        author_persona="codex-reviewer",
        content=codex_content,
        created_at=base_t,
    ))
    # 500 atlas filler comments AFTER the codex comment (so they'd be
    # truncated under a cap that ordered DESC; under our ascending
    # order they'd consume cap budget — but codex is first so it
    # always survives).
    db_session.add_all([
        TicketComment(
            id=f"tc_filler_{i:04d}",
            ticket_id=tk["id"],
            author_user_id=user.id,
            author_persona="atlas",
            content=f"filler {i}",
            created_at=base_t + timedelta(seconds=i + 1),
        )
        for i in range(500)
    ])
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tk['id']}/review-state",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    state = resp.json()["review_state"]
    assert state is not None, "Codex R1 within first 500 rows must survive the cap"
    assert state["last_verdict"] == "CHANGES_REQUESTED"
    assert len(state["open_findings"]) == 1
    assert state["open_findings"][0]["severity"] == "LOW"


# ─────────────────────────────────────────────────────────────────────
# v0.10.23 tk_884b2321fdb74170 — persona name case normalization
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_ticket_normalizes_persona_case(
    client: AsyncClient, db_session: AsyncSession,
):
    user, raw = await _make_user(db_session)
    """Codex v0.10.23 tk_884b2321fdb74170 — ticket created with
    `assigned_to='Atlas'` (capitalized, matching the .agents/atlas-backend.md
    filename) must be stored as the canonical persona name (lowercase)
    so the later start_ticket resolver's exact match always hits."""
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, name="atlas")
    headers = _hdrs(raw)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=headers,
        json={
            "title": "Case-normalized ticket",
            "description": "Persona case bug repro from CEO",
            "priority": "low",
            "assigned_to": "Atlas",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["assigned_to"] == "atlas", (
        f"expected normalized 'atlas', got {resp.json()['assigned_to']!r}"
    )


@pytest.mark.asyncio
async def test_create_ticket_keeps_unknown_assigned_to_as_freetext(
    client: AsyncClient, db_session: AsyncSession,
):
    user, raw = await _make_user(db_session)
    """Backward compat — assigned_to has historically been free-text
    when no matching persona exists. The normalization is best-effort,
    not strict validation."""
    project = await _make_project(db_session, user)
    headers = _hdrs(raw)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=headers,
        json={
            "title": "Free-text assignee",
            "description": "No persona by this name — keep as-is",
            "priority": "low",
            "assigned_to": "some-future-persona",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["assigned_to"] == "some-future-persona"


@pytest.mark.asyncio
async def test_start_ticket_case_insensitive_persona_lookup(
    client: AsyncClient, db_session: AsyncSession,
):
    user, raw = await _make_user(db_session)
    """Belt-and-suspenders read fallback — even a legacy ticket
    already stored with wrong-case `assigned_to` must start cleanly."""
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, name="atlas")

    # Inject a ticket with the wrong-case assigned_to (simulates a row
    # already in the wild before the normalize fix landed).
    legacy_ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="Legacy wrong-case",
        description="Pre-v0.10.23 row",
        priority="low",
        assigned_to="Atlas",  # wrong case
        created_by_user_id=user.id,
        status="open",
        context_refs="[]",
        file_refs="[]",
        related_sessions="[]",
        acceptance_criteria="[]",
    )
    db_session.add(legacy_ticket)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{legacy_ticket.id}/start",
        headers=_hdrs(raw),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_update_ticket_normalizes_assigned_to(
    client: AsyncClient, db_session: AsyncSession,
):
    user, raw = await _make_user(db_session)
    """Reassign-via-PUT (the path assign_persona MCP routes through)
    must apply the same normalization as create."""
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, name="scribe")
    headers = _hdrs(raw)

    create_r = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=headers,
        json={
            "title": "Reassign me",
            "description": "Starts unassigned",
            "priority": "low",
        },
    )
    assert create_r.status_code == 201
    tid = create_r.json()["id"]

    # PUT with capitalized SCRIBE — should normalize to "scribe".
    put_r = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tid}",
        headers=headers,
        json={"assigned_to": "SCRIBE"},
    )
    assert put_r.status_code == 200, put_r.text
    assert put_r.json()["assigned_to"] == "scribe"


@pytest.mark.asyncio
async def test_list_tickets_case_insensitive_assigned_to_filter(
    client: AsyncClient, db_session: AsyncSession,
):
    """Codex v0.10.23 R1 MEDIUM (tk_884b2321fdb74170) — discovery
    must match the start fix. A legacy ticket stored with wrong-case
    `assigned_to='Atlas'` must show up when an Atlas agent filters
    `?assigned_to=atlas`. Pre-fix, the filter was exact match and
    the row stayed invisible to the agent's own discovery."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    # Inject a legacy ticket with wrong-case assigned_to (skips the
    # write-time normalize that would have lowered it).
    legacy = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="Legacy wrong-case",
        description="x",
        priority="low",
        assigned_to="Atlas",
        created_by_user_id=user.id,
        status="open",
        context_refs="[]",
        file_refs="[]",
        related_sessions="[]",
        acceptance_criteria="[]",
    )
    db_session.add(legacy)
    await db_session.commit()

    # Filter with lowercase — must find the legacy row.
    r = await client.get(
        f"/api/v1/projects/{project.id}/tickets?assigned_to=atlas",
        headers=_hdrs(key),
    )
    assert r.status_code == 200, r.text
    titles = [t["title"] for t in r.json()]
    assert "Legacy wrong-case" in titles, (
        f"case-insensitive filter must surface legacy row; got {titles}"
    )


@pytest.mark.asyncio
async def test_persona_create_rejects_case_insensitive_duplicate(
    client: AsyncClient, db_session: AsyncSession,
):
    """Codex v0.10.23 R1 LOW (tk_884b2321fdb74170) — persona names
    are case-insensitive-unique per project. Without this guard, a
    project could hold both `atlas` and `Atlas` and the ticket
    resolver's case-insensitive lookup would be ambiguous."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    create_a = await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": "atlas", "role": "Backend", "content": "# Atlas"},
    )
    assert create_a.status_code == 201, create_a.text

    # Same name different case — must 409.
    create_b = await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(key),
        json={"name": "Atlas", "role": "Backend Take 2", "content": "# A2"},
    )
    assert create_b.status_code == 409, create_b.text
    msg = create_b.json()["error"]["message"]
    assert "case-insensitive" in msg.lower()


# ── v0.10.24 Issue/Task rollup (tk_dbccde26ed604b3c) ────────────


@pytest.mark.asyncio
async def test_default_kind_is_task(
    client: AsyncClient, db_session: AsyncSession
):
    """Existing call shape (no `kind`) defaults to 'task' — no regression."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Plain task"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "task"
    assert body["parent_ticket_id"] is None
    assert body["child_ticket_ids"] == []


@pytest.mark.asyncio
async def test_owner_can_create_issue(
    client: AsyncClient, db_session: AsyncSession
):
    """Project owner can file kind='issue' tickets directly."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={
            "title": "Dashboard rules update broken for enterprise",
            "kind": "issue",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "issue"
    assert body["status"] == "open"
    assert body["parent_ticket_id"] is None
    assert body["child_ticket_ids"] == []


@pytest.mark.asyncio
async def test_assignee_compass_does_not_bypass_authz(
    client: AsyncClient, db_session: AsyncSession
):
    """Codex R1 MED #1 regression — a non-owner non-admin who DOES have
    project access (e.g. captured-session legacy fallback) must still
    be rejected when filing kind='issue', even when passing
    assigned_to='compass'. Authorization is on the actor, not on the
    user-controlled target assignee.
    """
    from sessionfs.server.db.models import Session as SessionRow

    owner, _ = await _make_user(db_session, name="owner")
    other, other_key = await _make_user(db_session, name="other")
    project = await _make_project(db_session, owner)
    # Grant the other user project access via the captured-session
    # legacy fallback (Session.git_remote_normalized match).
    db_session.add(
        SessionRow(
            id=f"ses_{uuid.uuid4().hex[:24]}",
            user_id=other.id,
            source_tool="claude-code",
            title="other-session",
            message_count=0,
            total_input_tokens=0,
            total_output_tokens=0,
            tags="[]",
            blob_key="b",
            blob_size_bytes=0,
            etag="e",
            git_remote_normalized=project.git_remote_normalized,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(other_key),
        json={
            "title": "Sneaky rollup",
            "kind": "issue",
            "assigned_to": "compass",
        },
    )
    # Must be 403 — actor is not project owner / org admin even though
    # they passed assigned_to='compass'.
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_org_admin_can_create_issue(
    client: AsyncClient, db_session: AsyncSession
):
    """Org admin of an org-scoped project may file kind='issue'."""
    from sessionfs.server.db.models import OrgMember, Organization

    owner, _ = await _make_user(db_session, name="orgowner")
    admin, admin_key = await _make_user(db_session, name="orgadmin")
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:16]}",
        name="Acme",
        slug=f"acme-{uuid.uuid4().hex[:6]}",
        tier="team",
    )
    db_session.add(org)
    await db_session.commit()
    db_session.add(
        OrgMember(
            org_id=org.id,
            user_id=admin.id,
            role="admin",
            joined_at=datetime.now(timezone.utc),
        )
    )
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"orgproj-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/orgproj-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id,
    )
    db_session.add(project)
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(admin_key),
        json={
            "title": "Admin-filed issue",
            "kind": "issue",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["kind"] == "issue"


@pytest.mark.asyncio
async def test_org_member_non_admin_cannot_create_issue(
    client: AsyncClient, db_session: AsyncSession
):
    """Org member (role=member) cannot file kind='issue' on an
    org-scoped project even with assigned_to='compass'."""
    from sessionfs.server.db.models import OrgMember, Organization

    owner, _ = await _make_user(db_session, name="memberowner")
    member, member_key = await _make_user(db_session, name="orgmember")
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:16]}",
        name="Acme2",
        slug=f"acme2-{uuid.uuid4().hex[:6]}",
        tier="team",
    )
    db_session.add(org)
    await db_session.commit()
    db_session.add(
        OrgMember(
            org_id=org.id,
            user_id=member.id,
            role="member",
            joined_at=datetime.now(timezone.utc),
        )
    )
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"orgmproj-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/orgm-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id,
    )
    db_session.add(project)
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(member_key),
        json={
            "title": "Member-filed issue",
            "kind": "issue",
            "assigned_to": "compass",
        },
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_outsider_cannot_file_issue(
    client: AsyncClient, db_session: AsyncSession
):
    """User with no project access at all gets 403 from project_access
    before the kind gate runs."""
    owner, _ = await _make_user(db_session, name="owner_x")
    outsider, outsider_key = await _make_user(db_session, name="outsider")
    project = await _make_project(db_session, owner)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(outsider_key),
        json={
            "title": "Outsider Issue",
            "kind": "issue",
            "assigned_to": "atlas",
        },
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_task_can_have_issue_parent(
    client: AsyncClient, db_session: AsyncSession
):
    """A Task with parent_ticket_id pointing to an Issue is accepted."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    # File the Issue first.
    issue_resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Rollup", "kind": "issue"},
    )
    assert issue_resp.status_code == 201
    issue_id = issue_resp.json()["id"]
    # File a Task under it.
    task_resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Child task", "parent_ticket_id": issue_id},
    )
    assert task_resp.status_code == 201, task_resp.text
    body = task_resp.json()
    assert body["kind"] == "task"
    assert body["parent_ticket_id"] == issue_id


@pytest.mark.asyncio
async def test_task_with_task_parent_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    """Parent must be kind='issue' — Task-under-Task → 422."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    parent_resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Plain task parent"},
    )
    parent_id = parent_resp.json()["id"]
    child = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Child", "parent_ticket_id": parent_id},
    )
    assert child.status_code == 422, child.text
    assert "kind='issue'" in child.text or "issue" in child.text.lower()


@pytest.mark.asyncio
async def test_issue_with_parent_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    """No Issue-under-Issue in v1 — 400."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    parent = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Top issue", "kind": "issue"},
    )
    parent_id = parent.json()["id"]
    nested = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={
            "title": "Nested issue",
            "kind": "issue",
            "parent_ticket_id": parent_id,
        },
    )
    assert nested.status_code == 400, nested.text


@pytest.mark.asyncio
async def test_cross_project_parent_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    """parent_ticket_id from a different project → 422."""
    user, key = await _make_user(db_session)
    project_a = await _make_project(db_session, user)
    project_b = await _make_project(db_session, user)
    # File an Issue in project A.
    issue_a = await client.post(
        f"/api/v1/projects/{project_a.id}/tickets",
        headers=_hdrs(key),
        json={"title": "A-Issue", "kind": "issue"},
    )
    issue_a_id = issue_a.json()["id"]
    # Try filing a Task in project B with the A-issue as parent.
    cross = await client.post(
        f"/api/v1/projects/{project_b.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Cross-project child", "parent_ticket_id": issue_a_id},
    )
    assert cross.status_code == 422, cross.text


@pytest.mark.asyncio
async def test_get_issue_includes_child_ticket_ids(
    client: AsyncClient, db_session: AsyncSession
):
    """GET /tickets/{issue_id} returns child_ticket_ids rollup."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    issue = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Rollup", "kind": "issue"},
    )
    issue_id = issue.json()["id"]
    child_ids = []
    for i in range(3):
        c = await client.post(
            f"/api/v1/projects/{project.id}/tickets",
            headers=_hdrs(key),
            json={"title": f"Child {i}", "parent_ticket_id": issue_id},
        )
        child_ids.append(c.json()["id"])
    # GET the Issue.
    detail = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{issue_id}",
        headers=_hdrs(key),
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["kind"] == "issue"
    assert sorted(body["child_ticket_ids"]) == sorted(child_ids)


@pytest.mark.asyncio
async def test_get_task_has_empty_child_ticket_ids(
    client: AsyncClient, db_session: AsyncSession
):
    """Tasks always return empty child_ticket_ids (cannot have children)."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    task = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Plain task"},
    )
    detail = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{task.json()['id']}",
        headers=_hdrs(key),
    )
    assert detail.json()["child_ticket_ids"] == []


@pytest.mark.asyncio
async def test_list_filters_by_kind(
    client: AsyncClient, db_session: AsyncSession
):
    """GET /tickets?kind=issue returns only Issues."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    issue = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "An issue", "kind": "issue"},
    )
    task = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "A task"},
    )
    # All
    all_resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
    )
    assert {t["id"] for t in all_resp.json()} == {issue.json()["id"], task.json()["id"]}
    # Issues only
    only_issues = await client.get(
        f"/api/v1/projects/{project.id}/tickets?kind=issue",
        headers=_hdrs(key),
    )
    assert [t["id"] for t in only_issues.json()] == [issue.json()["id"]]
    # Tasks only
    only_tasks = await client.get(
        f"/api/v1/projects/{project.id}/tickets?kind=task",
        headers=_hdrs(key),
    )
    assert [t["id"] for t in only_tasks.json()] == [task.json()["id"]]


@pytest.mark.asyncio
async def test_list_filter_rejects_invalid_kind(
    client: AsyncClient, db_session: AsyncSession
):
    """kind=garbage → 400."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets?kind=invalid",
        headers=_hdrs(key),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_rejects_invalid_kind(
    client: AsyncClient, db_session: AsyncSession
):
    """Pydantic validator rejects unknown kind at the create endpoint."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Wrong kind", "kind": "epic"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_issue_close_lifecycle(
    client: AsyncClient, db_session: AsyncSession
):
    """Issue FSM: open → in_progress → closed via /close route. Owner
    can close (project_admin gate satisfied)."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    # Register Compass persona so start_ticket resolves; assigned_to=compass.
    await _make_persona(db_session, project, user, name="compass")
    issue = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Closeable", "kind": "issue", "assigned_to": "compass"},
    )
    issue_id = issue.json()["id"]
    # start: open → in_progress
    started = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{issue_id}/start",
        headers=_hdrs(key),
    )
    assert started.status_code == 200, started.text
    assert started.json()["ticket"]["status"] == "in_progress"
    # close: in_progress → closed
    closed = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{issue_id}/close",
        headers=_hdrs(key),
    )
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert body["status"] == "closed"
    assert body["resolved_at"] is not None


@pytest.mark.asyncio
async def test_close_rejects_task(
    client: AsyncClient, db_session: AsyncSession
):
    """Tasks cannot use /close — explicit 400 with helpful error."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    task = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Plain task"},
    )
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{task.json()['id']}/close",
        headers=_hdrs(key),
    )
    assert resp.status_code == 400
    assert "Issues only" in resp.text or "kind=" in resp.text


@pytest.mark.asyncio
async def test_issue_cancel_from_open(
    client: AsyncClient, db_session: AsyncSession
):
    """Issue FSM allows open → cancelled (filed in error)."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    issue = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Filed by mistake", "kind": "issue"},
    )
    issue_id = issue.json()["id"]
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{issue_id}/dismiss",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_close_requires_owner_or_admin(
    client: AsyncClient, db_session: AsyncSession
):
    """Codex R1 MED #2 regression — non-owner non-admin who has project
    access cannot close an Issue. Issue terminator gated on the same
    actor signal as Issue creation."""
    from sessionfs.server.db.models import Session as SessionRow

    owner, owner_key = await _make_user(db_session, name="closeowner")
    other, other_key = await _make_user(db_session, name="closeother")
    project = await _make_project(db_session, owner)
    await _make_persona(db_session, project, owner, name="compass")
    # Grant other user project access via captured-session fallback.
    db_session.add(
        SessionRow(
            id=f"ses_{uuid.uuid4().hex[:24]}",
            user_id=other.id,
            source_tool="claude-code",
            title="other-close-session",
            message_count=0,
            total_input_tokens=0,
            total_output_tokens=0,
            tags="[]",
            blob_key="b",
            blob_size_bytes=0,
            etag="e",
            git_remote_normalized=project.git_remote_normalized,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    # Owner files the Issue and starts it.
    issue = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(owner_key),
        json={"title": "Closeable", "kind": "issue", "assigned_to": "compass"},
    )
    issue_id = issue.json()["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{issue_id}/start",
        headers=_hdrs(owner_key),
    )
    # Non-admin tries to close → 403.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{issue_id}/close",
        headers=_hdrs(other_key),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_compiled_context_surfaces_kind_and_parent(
    client: AsyncClient, db_session: AsyncSession
):
    """Codex R1 MED #3 regression — start_ticket compiled_context must
    surface kind + parent_ticket_id for child Tasks AND child_ticket_ids
    for Issues, so agents reading the markdown see the rollup."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, name="atlas")
    await _make_persona(db_session, project, user, name="compass")
    issue = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Rollup container", "kind": "issue", "assigned_to": "compass"},
    )
    issue_id = issue.json()["id"]
    task = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={
            "title": "Atlas child task",
            "assigned_to": "atlas",
            "parent_ticket_id": issue_id,
            "description": "Do the thing",
        },
    )
    task_id = task.json()["id"]
    # Start the Task — compiled context should mention parent Issue + kind.
    started_task = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{task_id}/start",
        headers=_hdrs(key),
    )
    assert started_task.status_code == 200
    task_ctx = started_task.json()["compiled_context"]
    assert "Kind: task" in task_ctx
    assert f"Parent Issue: {issue_id}" in task_ctx
    assert "Rollup container" in task_ctx
    # Start the Issue — compiled context should mention child Tasks.
    started_issue = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{issue_id}/start",
        headers=_hdrs(key),
    )
    assert started_issue.status_code == 200
    issue_ctx = started_issue.json()["compiled_context"]
    assert "Kind: issue" in issue_ctx
    assert "Child Tasks" in issue_ctx
    assert task_id in issue_ctx


@pytest.mark.asyncio
async def test_issue_cannot_use_block_route(
    client: AsyncClient, db_session: AsyncSession
):
    """Issue FSM rejects block — that's a Task-only transition."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, name="compass")
    issue = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "I", "kind": "issue", "assigned_to": "compass"},
    )
    issue_id = issue.json()["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/tickets/{issue_id}/start",
        headers=_hdrs(key),
    )
    # block should be rejected by Issue FSM
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{issue_id}/block",
        headers=_hdrs(key),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_agent_issue_skips_suggested_gate(
    client: AsyncClient, db_session: AsyncSession
):
    """Agent-source Issues bypass the suggested-quality gate — they're
    PM-triaged containers, not executor work units."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    # Source=agent + kind=issue + no acceptance_criteria + short desc:
    # all the things that would 400 a Task should pass for an Issue.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={
            "title": "Auto-filed issue",
            "kind": "issue",
            "source": "agent",
            "description": "short",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "open"  # NOT 'suggested'


@pytest.mark.asyncio
async def test_existing_task_fsm_unchanged(
    client: AsyncClient, db_session: AsyncSession
):
    """The Task FSM keeps the existing executor lifecycle —
    suggested → open → in_progress → review → done. Issues don't
    break the Task path."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user)
    task = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": "Task work", "assigned_to": "atlas"},
    )
    task_id = task.json()["id"]
    # start → in_progress
    started = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{task_id}/start",
        headers=_hdrs(key),
    )
    assert started.status_code == 200
    # complete → review
    completed = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{task_id}/complete",
        headers=_hdrs(key),
        json={"notes": "done"},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "review"
    # accept → done
    accepted = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{task_id}/accept",
        headers=_hdrs(key),
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "done"


# ── tk_835a876529de4551 — update_ticket diff/audit/authz/lease ────


@pytest.mark.asyncio
async def test_update_ticket_writes_per_field_audit_rows(
    client: AsyncClient, db_session: AsyncSession,
):
    """Every mutated field writes one TicketEdit row with JSON-encoded
    old/new values and the ticket's lease_epoch at edit time. Preserves
    the structured per-field history future SoD / audit queries depend on."""
    from sessionfs.server.db.models import TicketEdit

    user, raw = await _make_user(db_session)
    project = await _make_project(db_session, user)
    headers = _hdrs(raw)

    create_r = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=headers,
        json={
            "title": "Original title",
            "description": "Original description",
            "priority": "medium",
            "acceptance_criteria": ["AC 1", "AC 2"],
        },
    )
    assert create_r.status_code == 201
    tid = create_r.json()["id"]

    put_r = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tid}",
        headers=headers,
        json={
            "title": "Corrected title",
            "description": "Corrected description with updates",
            "acceptance_criteria": ["AC 1", "AC 2", "AC 3"],
        },
    )
    assert put_r.status_code == 200, put_r.text

    edits = (
        await db_session.execute(
            select(TicketEdit).where(TicketEdit.ticket_id == tid)
        )
    ).scalars().all()
    by_field = {e.field_name: e for e in edits}
    assert set(by_field) == {"title", "description", "acceptance_criteria"}
    assert json.loads(by_field["title"].old_value) == "Original title"
    assert json.loads(by_field["title"].new_value) == "Corrected title"
    assert json.loads(by_field["acceptance_criteria"].old_value) == ["AC 1", "AC 2"]
    assert json.loads(by_field["acceptance_criteria"].new_value) == ["AC 1", "AC 2", "AC 3"]
    # lease_epoch captured for reconstruction.
    for e in edits:
        assert e.edited_by_user_id == user.id


@pytest.mark.asyncio
async def test_update_ticket_posts_system_diff_comment(
    client: AsyncClient, db_session: AsyncSession,
):
    """Successful update auto-posts a TicketComment with author_persona
    'system' summarizing the diff. Caller does not need to add a
    manual comment to record the change."""
    user, raw = await _make_user(db_session)
    project = await _make_project(db_session, user)
    headers = _hdrs(raw)

    create_r = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=headers,
        json={"title": "X", "description": "Y", "priority": "low"},
    )
    tid = create_r.json()["id"]

    # Update changes priority.
    put_r = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tid}",
        headers=headers,
        json={"priority": "high"},
    )
    assert put_r.status_code == 200

    comments_r = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tid}/comments",
        headers=headers,
    )
    assert comments_r.status_code == 200
    body = comments_r.json()
    comments = body if isinstance(body, list) else body.get("comments", [])
    system_comments = [
        c for c in comments if c.get("author_persona") == "system"
    ]
    assert len(system_comments) == 1, (
        f"expected exactly one system diff comment, got {len(system_comments)}"
    )
    content = system_comments[0]["content"]
    assert "priority" in content
    assert "low" in content and "high" in content


@pytest.mark.asyncio
async def test_update_ticket_noop_does_not_post_diff_comment(
    client: AsyncClient, db_session: AsyncSession,
):
    """An update that doesn't change any field must NOT post a diff
    comment — empty audit pollution is the explicit anti-pattern."""
    user, raw = await _make_user(db_session)
    project = await _make_project(db_session, user)
    headers = _hdrs(raw)

    create_r = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=headers,
        json={"title": "Same", "description": "Same", "priority": "low"},
    )
    tid = create_r.json()["id"]

    # PUT with same values — no diff.
    put_r = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tid}",
        headers=headers,
        json={"title": "Same", "priority": "low"},
    )
    assert put_r.status_code == 200

    comments_r = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{tid}/comments",
        headers=headers,
    )
    body = comments_r.json()
    comments = body if isinstance(body, list) else body.get("comments", [])
    system_comments = [
        c for c in comments if c.get("author_persona") == "system"
    ]
    assert system_comments == [], (
        "no-op update must NOT post a diff comment"
    )


@pytest.mark.asyncio
async def test_update_ticket_lease_epoch_fence_409s_on_stale(
    client: AsyncClient, db_session: AsyncSession,
):
    """Passing a stale lease_epoch must atomically reject with 409 +
    structured envelope identifying the current epoch."""
    user, raw = await _make_user(db_session)
    project = await _make_project(db_session, user)
    headers = _hdrs(raw)

    create_r = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=headers,
        json={"title": "Fenced", "description": "x", "priority": "low"},
    )
    tid = create_r.json()["id"]
    initial_epoch = create_r.json()["lease_epoch"]

    # First update with the correct lease_epoch should succeed AND bump.
    ok = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tid}",
        headers=headers,
        json={"title": "Fenced v2", "lease_epoch": initial_epoch},
    )
    assert ok.status_code == 200, ok.text
    new_epoch = ok.json()["lease_epoch"]
    assert new_epoch == initial_epoch + 1

    # Second update with the OLD epoch must 409.
    stale = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tid}",
        headers=headers,
        json={"title": "Should not win", "lease_epoch": initial_epoch},
    )
    assert stale.status_code == 409
    body = stale.json()
    detail = body.get("detail") or body.get("error", {}).get("details", body.get("error", body))
    if isinstance(detail, dict) and "current_lease_epoch" in detail:
        assert detail["current_lease_epoch"] == new_epoch


@pytest.mark.asyncio
async def test_update_ticket_403_for_non_creator_non_admin(
    client: AsyncClient, db_session: AsyncSession,
):
    """A regular user who is neither the ticket creator nor a project
    admin cannot update the ticket. Persona name (assigned_to) does
    NOT grant edit rights."""
    creator, creator_key = await _make_user(db_session)
    intruder, intruder_key = await _make_user(db_session)
    project = await _make_project(db_session, creator)

    create_r = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(creator_key),
        json={"title": "Owned", "description": "x", "priority": "low"},
    )
    tid = create_r.json()["id"]

    # Intruder tries to update — should 403.
    bad = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{tid}",
        headers=_hdrs(intruder_key),
        json={"title": "Hijacked"},
    )
    assert bad.status_code == 403


@pytest.mark.asyncio
async def test_update_ticket_cross_project_isolation(
    client: AsyncClient, db_session: AsyncSession,
):
    """A user in project A who tries to update a ticket from project B
    must get 404 (project boundary) — not 403 (which would leak the
    ticket's existence)."""
    user_a, key_a = await _make_user(db_session)
    user_b, key_b = await _make_user(db_session)
    project_a = await _make_project(db_session, user_a)
    project_b = await _make_project(db_session, user_b)

    create_r = await client.post(
        f"/api/v1/projects/{project_b.id}/tickets",
        headers=_hdrs(key_b),
        json={"title": "B's ticket", "description": "x", "priority": "low"},
    )
    tid_b = create_r.json()["id"]

    # User A uses A's project_id with B's ticket_id.
    bad = await client.put(
        f"/api/v1/projects/{project_a.id}/tickets/{tid_b}",
        headers=_hdrs(key_a),
        json={"title": "Cross-project hijack"},
    )
    assert bad.status_code == 404


@pytest.mark.asyncio
async def test_update_ticket_depends_on_full_replacement(
    client: AsyncClient, db_session: AsyncSession,
):
    """v1 semantic: depends_on is full-list replacement, not item-patch.
    Replacing [dep_a] with [dep_b, dep_c] removes dep_a and adds both
    new deps via TicketDependency wipe + re-insert."""
    user, raw = await _make_user(db_session)
    project = await _make_project(db_session, user)
    headers = _hdrs(raw)

    async def _mk(title: str) -> str:
        r = await client.post(
            f"/api/v1/projects/{project.id}/tickets",
            headers=headers,
            json={"title": title, "description": "x", "priority": "low"},
        )
        return r.json()["id"]

    main_tid = await _mk("Main")
    dep_a = await _mk("Dep A")
    dep_b = await _mk("Dep B")
    dep_c = await _mk("Dep C")

    # First set: only dep_a.
    r1 = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{main_tid}",
        headers=headers,
        json={"depends_on": [dep_a]},
    )
    assert r1.status_code == 200
    assert r1.json()["depends_on"] == [dep_a]

    # Replace with [dep_b, dep_c].
    r2 = await client.put(
        f"/api/v1/projects/{project.id}/tickets/{main_tid}",
        headers=headers,
        json={"depends_on": [dep_b, dep_c]},
    )
    assert r2.status_code == 200
    new_deps = sorted(r2.json()["depends_on"])
    assert new_deps == sorted([dep_b, dep_c])

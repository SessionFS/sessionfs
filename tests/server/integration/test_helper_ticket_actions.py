"""Session-helper ticket write-actions (GitHub #104 part 2, item 5).

``propose_ticket`` / ``propose_assignment`` / ``propose_comment`` only
PROPOSE; nothing mutates until the owner confirms the pending ``helper_actions``
row.  Confirm re-runs the same rules as the ticket routes (access, plan gate,
assignment auth, never a trusted verdict) and applies the caller's edits.
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
    HelperAction,
    HelperConversation,
    OrgMember,
    Organization,
    Project,
    Session,
    Ticket,
    TicketComment,
    TicketEdit,
    User,
)
from sessionfs.server.helper.context import HelperContext, HelperScope, RetrievalBudget
from sessionfs.server.helper.tools import TOOLS, ToolArgumentError


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


async def _make_user(
    db: AsyncSession, name: str = "alice", *, tier: str = "enterprise"
) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier=tier,
        email_verified=True,
        is_active=True,
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


async def _make_org_with(
    db: AsyncSession, admin: User, member: User | None = None, *, tier: str = "enterprise"
) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Test Org",
        slug=f"test-{uuid.uuid4().hex[:6]}",
        tier=tier,
        seats_limit=10,
        settings="{}",
    )
    db.add(org)
    await db.commit()
    db.add(OrgMember(org_id=org.id, user_id=admin.id, role="admin"))
    if member is not None:
        db.add(OrgMember(org_id=org.id, user_id=member.id, role="member"))
    await db.commit()
    await db.refresh(org)
    return org


async def _make_project(
    db: AsyncSession, user: User, org_id: str | None = None, name: str = "Repo"
) -> Project:
    project = Project(
        id=f"prj_{uuid.uuid4().hex[:12]}",
        name=name,
        owner_id=user.id,
        org_id=org_id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_session(
    db: AsyncSession, user: User, title: str = "Test Session", *, project_id: str | None = None
) -> Session:
    now = datetime.now(timezone.utc)
    session = Session(
        id=f"ses_{uuid.uuid4().hex[:16]}",
        user_id=user.id,
        title=title,
        source_tool="claude-code",
        blob_key=f"sessions/{user.id}/x.tar.gz",
        etag=uuid.uuid4().hex,
        created_at=now,
        updated_at=now,
        uploaded_at=now,
        project_id=project_id,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def _make_conversation(
    db: AsyncSession, user: User, org_id: str | None = None
) -> HelperConversation:
    conv = HelperConversation(
        id=f"hc_{uuid.uuid4().hex[:24]}",
        user_id=user.id,
        org_id=org_id,
        title="Actions thread",
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def _make_action(
    db: AsyncSession,
    user: User,
    *,
    kind: str,
    params: dict,
    conversation: HelperConversation,
    expires_at: datetime | None = None,
    status: str = "pending",
) -> HelperAction:
    action = HelperAction(
        id=f"ha_{uuid.uuid4().hex[:24]}",
        user_id=user.id,
        conversation_id=conversation.id,
        kind=kind,
        params=json.dumps(params, sort_keys=True),
        preview=json.dumps({"summary": "test", "targets": []}, sort_keys=True),
        status=status,
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(minutes=15),
        created_at=datetime.now(timezone.utc),
    )
    db.add(action)
    await db.commit()
    await db.refresh(action)
    return action


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


async def _make_org_persona(
    db: AsyncSession,
    org: Organization,
    owner: User,
    name: str,
    *,
    represented_user_id: str | None = None,
) -> AgentPersona:
    persona = AgentPersona(
        id=f"per_{uuid.uuid4().hex[:16]}",
        org_id=org.id,
        name=name,
        role="Backend",
        created_by=owner.id,
        represented_user_id=represented_user_id,
    )
    db.add(persona)
    await db.commit()
    await db.refresh(persona)
    return persona


async def _make_ticket(
    db: AsyncSession,
    user: User,
    project: Project,
    *,
    title: str = "Flaky CI",
    status: str = "open",
    assigned_to: str | None = None,
) -> Ticket:
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title=title,
        description="old description",
        priority="medium",
        kind="task",
        status=status,
        created_by_user_id=user.id,
        assigned_to=assigned_to,
        context_refs="[]",
        file_refs="[]",
        related_sessions="[]",
        acceptance_criteria="[]",
        actor_type="user",
        lease_epoch=0,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


def _ctx(
    db: AsyncSession,
    user: User,
    *,
    project_id: str | None = None,
    session_ids: tuple[str, ...] = (),
) -> HelperContext:
    return HelperContext(
        user=user,
        db=db,
        org_id=None,
        scope=HelperScope(project_id=project_id, session_ids=session_ids),
        budget=RetrievalBudget(),
        audit=None,
    )


def _ticket_sources() -> dict[str, list[str]]:
    return {
        "title": ["user"],
        "description": ["user"],
        "acceptance_criteria": ["user"],
    }


def _ticket_args(project: Project, **overrides: object) -> dict:
    args: dict[str, object] = {
        "project_id": project.id,
        "title": "Fix the flaky test",
        "description": "The session showed the CI job failing intermittently.",
        "acceptance_criteria": ["CI is green on 3 consecutive runs"],
        "priority": "high",
        "kind": "task",
        "sources": _ticket_sources(),
    }
    args.update(overrides)
    return args


# ---------------------------------------------------------------------------
# Proposal tools never mutate + source validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_ticket_never_mutates(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_ticket"].handler(ctx, _ticket_args(project))

    assert "Proposed action" in output.text
    assert len(ctx.pending_actions) == 1
    proposal = ctx.pending_actions[0]
    assert proposal["kind"] == "create_ticket"
    assert proposal["params"]["title"] == "Fix the flaky test"
    assert proposal["params"]["sources"] == _ticket_sources()

    assert (await db_session.execute(select(Ticket))).scalars().all() == []


@pytest.mark.asyncio
async def test_propose_ticket_unsourced_field_rejected(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ctx = _ctx(db_session, user)

    sources = _ticket_sources()
    del sources["description"]
    with pytest.raises(ToolArgumentError, match="description.*at least one source"):
        await TOOLS["propose_ticket"].handler(
            ctx, _ticket_args(project, sources=sources)
        )
    assert ctx.pending_actions == []


@pytest.mark.asyncio
async def test_propose_ticket_unseen_source_rejected(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ctx = _ctx(db_session, user)

    sources = _ticket_sources()
    sources["title"] = [f"[ticket:tk_{'0' * 16}]"]
    with pytest.raises(ToolArgumentError, match="was not returned by a tool"):
        await TOOLS["propose_ticket"].handler(
            ctx, _ticket_args(project, sources=sources)
        )
    assert ctx.pending_actions == []


@pytest.mark.asyncio
async def test_propose_ticket_non_object_sources_rejected(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ctx = _ctx(db_session, user)

    with pytest.raises(ToolArgumentError, match="object mapping"):
        await TOOLS["propose_ticket"].handler(
            ctx, _ticket_args(project, sources=["user"])
        )
    assert ctx.pending_actions == []


@pytest.mark.asyncio
async def test_propose_assignment_resolves_persona_without_mutating(
    db_session: AsyncSession,
):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, user, project)
    await _make_persona(db_session, project, user, "atlas")
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_assignment"].handler(
        ctx, {"ticket_id": ticket.id, "persona_name": "atlas"}
    )

    assert "Proposed action" in output.text
    proposal = ctx.pending_actions[0]
    assert proposal["kind"] == "assign_ticket"
    assert proposal["params"] == {
        "ticket_id": ticket.id,
        "project_id": project.id,
        "assigned_to": "atlas",
    }

    await db_session.refresh(ticket)
    assert ticket.assigned_to is None


@pytest.mark.asyncio
async def test_propose_assignment_represented_human_resolves_persona(
    db_session: AsyncSession,
):
    owner, _ = await _make_user(db_session, name="owner")
    human, _ = await _make_user(db_session, name="human")
    org = await _make_org_with(db_session, owner, human)
    project = await _make_project(db_session, owner, org_id=org.id)
    ticket = await _make_ticket(db_session, owner, project)
    await _make_org_persona(db_session, org, owner, "scribe", represented_user_id=human.id)
    ctx = _ctx(db_session, owner)

    output = await TOOLS["propose_assignment"].handler(
        ctx, {"ticket_id": ticket.id, "persona_name": "Human"}
    )

    assert ctx.pending_actions[0]["params"] == {
        "ticket_id": ticket.id,
        "project_id": project.id,
        "assigned_to": "scribe",
    }
    assert "scribe" in output.text


@pytest.mark.asyncio
async def test_propose_assignment_ambiguous_human_is_tool_error(
    db_session: AsyncSession,
):
    owner, _ = await _make_user(db_session, name="owner")
    human, _ = await _make_user(db_session, name="human")
    org = await _make_org_with(db_session, owner, human)
    project = await _make_project(db_session, owner, org_id=org.id)
    ticket = await _make_ticket(db_session, owner, project)
    await _make_org_persona(db_session, org, owner, "alpha", represented_user_id=human.id)
    await _make_org_persona(db_session, org, owner, "beta", represented_user_id=human.id)
    ctx = _ctx(db_session, owner)

    with pytest.raises(ToolArgumentError, match="Ambiguous.*alpha.*beta"):
        await TOOLS["propose_assignment"].handler(
            ctx, {"ticket_id": ticket.id, "persona_name": "Human"}
        )
    assert ctx.pending_actions == []


@pytest.mark.asyncio
async def test_propose_assignment_denies_non_creator_non_admin(
    db_session: AsyncSession,
):
    owner, _ = await _make_user(db_session, name="owner")
    member, _ = await _make_user(db_session, name="member")
    org = await _make_org_with(db_session, owner, member)
    project = await _make_project(db_session, owner, org_id=org.id)
    ticket = await _make_ticket(db_session, owner, project)
    await _make_org_persona(db_session, org, owner, "atlas")
    ctx = _ctx(db_session, member)

    output = await TOOLS["propose_assignment"].handler(
        ctx, {"ticket_id": ticket.id, "persona_name": "atlas"}
    )

    assert ctx.pending_actions == []
    assert "only the ticket creator or a project admin" in output.text.lower()
    await db_session.refresh(ticket)
    assert ticket.assigned_to is None


@pytest.mark.asyncio
async def test_propose_comment_never_mutates(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, user, project)
    ctx = _ctx(db_session, user)

    output = await TOOLS["propose_comment"].handler(
        ctx,
        {
            "ticket_id": ticket.id,
            "body": "The user confirmed this is fixed.",
            "sources": {"body": ["user"]},
        },
    )

    assert "Proposed action" in output.text
    assert ctx.pending_actions[0]["kind"] == "comment_ticket"
    assert (await db_session.execute(select(TicketComment))).scalars().all() == []


@pytest.mark.asyncio
async def test_propose_comment_refuses_verdict_marker(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, user, project)
    ctx = _ctx(db_session, user)

    # A plain tool error the model can read, not a generic "failed".
    with pytest.raises(ToolArgumentError, match="cannot carry a review verdict"):
        await TOOLS["propose_comment"].handler(
            ctx,
            {
                "ticket_id": ticket.id,
                "body": "VERIFIED-CLEAN: no issues found",
                "sources": {"body": ["user"]},
            },
        )
    assert ctx.pending_actions == []


@pytest.mark.asyncio
async def test_project_scoped_cannot_propose_ticket_for_other_project(
    db_session: AsyncSession,
):
    user, _ = await _make_user(db_session)
    mine = await _make_project(db_session, user, name="Mine")
    other = await _make_project(db_session, user, name="Other")
    ctx = _ctx(db_session, user, project_id=mine.id)

    output = await TOOLS["propose_ticket"].handler(ctx, _ticket_args(other))

    assert ctx.pending_actions == []
    assert output.text == "Project not found."
    assert "Other" not in output.text


# ---------------------------------------------------------------------------
# Confirm: create ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_creates_exactly_user_edited_ticket(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params={
            "project_id": project.id,
            "title": "Draft title",
            "description": "draft description",
            "acceptance_criteria": ["draft ac"],
            "priority": "medium",
            "kind": "task",
            "assigned_to": None,
            "sources": _ticket_sources(),
        },
        conversation=conv,
    )

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm",
        headers=_hdrs(key),
        json={
            "title": "Final title",
            "description": "final description",
            "acceptance_criteria": ["final ac one", "final ac two"],
            "priority": "high",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["result"]["targets"][0]["ok"] is True
    ticket_id = body["result"]["targets"][0]["ticket_id"]

    ticket = (
        await db_session.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one()
    assert ticket.project_id == project.id
    assert ticket.title == "Final title"
    assert ticket.description == (
        "final description"
        + "\n\nDrafted with the SessionFS helper from conversation "
        + f"{conv.id} — confirmed by {user.display_name}"
    )
    assert json.loads(ticket.acceptance_criteria) == ["final ac one", "final ac two"]
    assert ticket.priority == "high"
    assert ticket.status == "open"

    # The stored proposal keeps both the model draft and the final edits.
    await db_session.refresh(action)
    stored = json.loads(action.params)
    assert stored["title"] == "Draft title"
    assert stored["edited"]["title"] == "Final title"
    assert stored["edited"]["description"] == "final description"

    # Outcome note names the new ticket id.
    from sessionfs.server.db.models import HelperMessage

    note = (
        await db_session.execute(
            select(HelperMessage).where(HelperMessage.conversation_id == conv.id)
        )
    ).scalar_one()
    assert note.content.endswith(f"confirmed: created ticket [ticket:{ticket_id}]")


@pytest.mark.asyncio
async def test_confirm_create_ticket_without_edits_uses_draft(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params={
            "project_id": project.id,
            "title": "Draft title",
            "description": "draft description",
            "acceptance_criteria": ["draft ac"],
            "priority": "low",
            "kind": "task",
            "assigned_to": None,
            "sources": _ticket_sources(),
        },
        conversation=conv,
    )

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    ticket_id = resp.json()["result"]["targets"][0]["ticket_id"]
    ticket = (
        await db_session.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one()
    assert ticket.title == "Draft title"
    assert ticket.priority == "low"
    assert json.loads(ticket.acceptance_criteria) == ["draft ac"]


@pytest.mark.asyncio
async def test_captured_session_reader_creates_suggested_ticket(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session, name="owner")
    reader, reader_key = await _make_user(db_session, name="reader")
    project = await _make_project(db_session, owner, name="Shared")
    await _make_session(db_session, reader, project_id=project.id)
    conv = await _make_conversation(db_session, reader)
    action = await _make_action(
        db_session,
        reader,
        kind="create_ticket",
        params={
            "project_id": project.id,
            "title": "Reader ticket",
            "description": "spotted during a captured session",
            "acceptance_criteria": [],
            "priority": "medium",
            "kind": "task",
            "assigned_to": None,
            "sources": _ticket_sources(),
        },
        conversation=conv,
    )

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(reader_key)
    )
    assert resp.status_code == 200, resp.text
    ticket_id = resp.json()["result"]["targets"][0]["ticket_id"]
    ticket = (
        await db_session.execute(select(Ticket).where(Ticket.id == ticket_id))
    ).scalar_one()
    assert ticket.status == "suggested"


@pytest.mark.asyncio
async def test_non_reader_confirm_fails_target(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session, name="owner")
    stranger, stranger_key = await _make_user(db_session, name="stranger")
    project = await _make_project(db_session, owner, name="Private")
    conv = await _make_conversation(db_session, stranger)
    action = await _make_action(
        db_session,
        stranger,
        kind="create_ticket",
        params={
            "project_id": project.id,
            "title": "Should not exist",
            "description": "no access",
            "acceptance_criteria": [],
            "priority": "medium",
            "kind": "task",
            "assigned_to": None,
            "sources": _ticket_sources(),
        },
        conversation=conv,
    )

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(stranger_key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["result"]["targets"] == [
        {"id": project.id, "ok": False, "error": "forbidden"}
    ]
    assert (await db_session.execute(select(Ticket))).scalars().all() == []


@pytest.mark.asyncio
async def test_confirm_create_ticket_body_validation_422(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params={
            "project_id": project.id,
            "title": "Draft title",
            "description": "draft",
            "acceptance_criteria": [],
            "priority": "medium",
            "kind": "task",
            "assigned_to": None,
            "sources": _ticket_sources(),
        },
        conversation=conv,
    )

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm",
        headers=_hdrs(key),
        json={"title": "   "},
    )
    assert resp.status_code == 422, resp.text

    # The failed edit rolled back the claim: the action is still pending and
    # nothing was created.
    await db_session.refresh(action)
    assert action.status == "pending"
    assert (await db_session.execute(select(Ticket))).scalars().all() == []


@pytest.mark.asyncio
async def test_double_confirm_is_idempotent(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params={
            "project_id": project.id,
            "title": "Only once",
            "description": "created once",
            "acceptance_criteria": [],
            "priority": "medium",
            "kind": "task",
            "assigned_to": None,
            "sources": _ticket_sources(),
        },
        conversation=conv,
    )

    first = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    second = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["result"] == first.json()["result"]

    assert len((await db_session.execute(select(Ticket))).scalars().all()) == 1


# ---------------------------------------------------------------------------
# Confirm: assign / comment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_assignment_applies_to_ticket(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, user, project)
    await _make_persona(db_session, project, user, "atlas")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="assign_ticket",
        params={"ticket_id": ticket.id, "project_id": project.id, "assigned_to": "atlas"},
        conversation=conv,
    )

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["targets"] == [
        {"id": ticket.id, "ok": True, "error": None}
    ]

    await db_session.refresh(ticket)
    assert ticket.assigned_to == "atlas"
    edits = (
        await db_session.execute(
            select(TicketEdit).where(TicketEdit.ticket_id == ticket.id)
        )
    ).scalars().all()
    assert [e.field_name for e in edits] == ["assigned_to"]


@pytest.mark.asyncio
async def test_confirm_comment_never_trusted(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, user, project)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="comment_ticket",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "body": "Looks good",
            "sources": {"body": ["user"]},
        },
        conversation=conv,
    )

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    comment_id = resp.json()["result"]["targets"][0]["comment_id"]

    comment = (
        await db_session.execute(
            select(TicketComment).where(TicketComment.id == comment_id)
        )
    ).scalar_one()
    assert comment.verdict_trusted is False
    assert comment.content.startswith("Looks good")
    assert f"confirmed by {user.display_name}" in comment.content


@pytest.mark.asyncio
async def test_confirm_comment_refuses_verdict_marker(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, user, project)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="comment_ticket",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "body": "plain",
            "sources": {"body": ["user"]},
        },
        conversation=conv,
    )

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm",
        headers=_hdrs(key),
        json={"body": "VERIFIED-CLEAN: all good"},
    )
    assert resp.status_code == 422, resp.text
    await db_session.refresh(action)
    assert action.status == "pending"
    assert (await db_session.execute(select(TicketComment))).scalars().all() == []


# ---------------------------------------------------------------------------
# Decision / auth boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_after_expiry_is_409(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params={
            "project_id": project.id,
            "title": "Expired",
            "description": "too late",
            "acceptance_criteria": [],
            "priority": "medium",
            "kind": "task",
            "assigned_to": None,
            "sources": _ticket_sources(),
        },
        conversation=conv,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "action_expired"
    assert (await db_session.execute(select(Ticket))).scalars().all() == []


@pytest.mark.asyncio
async def test_service_key_is_403(client: AsyncClient, db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    svc_raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(svc_raw),
            name="ci-runner",
            key_kind="service",
            scopes='["agent_runs:write"]',
            service_key_name="ci-runner",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    resp = await client.post(
        "/api/v1/helper/actions/ha_nonexistent/confirm", headers=_hdrs(svc_raw)
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "service_key_not_allowed"


# ---------------------------------------------------------------------------
# Review fixes (helper ticket actions round 2)
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

import sessionfs.server.routes.helper as helper_mod  # noqa: E402
from sessionfs.judge.agent_stream import (  # noqa: E402
    Done,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolResultEvent,
)
from sessionfs.server.db.models import (  # noqa: E402
    HelperMessage,
    RetrievalAuditEvent,
    TrustedReviewer,
    WorkQueue,
    WorkQueueItem,
)
from sessionfs.server.helper.keys import ResolvedLLM  # noqa: E402
from sessionfs.server.services.helper_ticket_actions import (  # noqa: E402
    MAX_COMMENT_CHARS,
    MAX_CRITERION_CHARS,
    MAX_DESCRIPTION_CHARS,
    has_verdict_marker,
)


async def _confirm(client: AsyncClient, key: str, action: HelperAction, body: dict | None = None):
    kwargs = {"json": body} if body is not None else {}
    return await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key), **kwargs
    )


async def _make_reader_setup(db: AsyncSession, *, status: str):
    """A personal project, a captured-session reader who created a ticket on it,
    and a project persona to assign to."""
    owner, _ = await _make_user(db, name="owner")
    reader, reader_key = await _make_user(db, name="reader")
    project = await _make_project(db, owner, name="Shared")
    await _make_session(db, reader, project_id=project.id)
    ticket = await _make_ticket(db, reader, project, status=status)
    await _make_persona(db, project, owner, "atlas")
    return owner, reader, reader_key, project, ticket


# -- 1. Reassignment follows the assign route's edit-access rules -----------


@pytest.mark.asyncio
async def test_reader_cannot_reassign_in_progress_ticket_via_helper(
    client: AsyncClient, db_session: AsyncSession
):
    _, reader, reader_key, project, ticket = await _make_reader_setup(
        db_session, status="in_progress"
    )

    # Proposal time: a clean refusal for the model, no proposal.
    ctx = _ctx(db_session, reader)
    output = await TOOLS["propose_assignment"].handler(
        ctx, {"ticket_id": ticket.id, "persona_name": "atlas"}
    )
    assert ctx.pending_actions == []
    assert "You cannot assign this ticket" in output.text
    assert "in_progress" in output.text

    # Confirm time (a proposal made before the ticket was started): refused.
    conv = await _make_conversation(db_session, reader)
    action = await _make_action(
        db_session,
        reader,
        kind="assign_ticket",
        params={"ticket_id": ticket.id, "project_id": project.id, "assigned_to": "atlas"},
        conversation=conv,
    )
    resp = await _confirm(client, reader_key, action)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"
    assert resp.json()["result"]["targets"] == [
        {"id": ticket.id, "ok": False, "error": "not_editor"}
    ]
    await db_session.refresh(ticket)
    assert ticket.assigned_to is None
    assert ticket.status == "in_progress"


@pytest.mark.asyncio
async def test_reader_cannot_reassign_ticket_held_by_work_queue(
    client: AsyncClient, db_session: AsyncSession
):
    owner, reader, reader_key, project, ticket = await _make_reader_setup(
        db_session, status="open"
    )
    queue = WorkQueue(
        id=f"wq_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name="loop",
        mode="implement_until_done",
        created_by_user_id=owner.id,
    )
    db_session.add(queue)
    await db_session.commit()
    db_session.add(
        WorkQueueItem(
            id=f"wqi_{uuid.uuid4().hex[:16]}",
            work_queue_id=queue.id,
            ticket_id=ticket.id,
            item_status="pending",
        )
    )
    await db_session.commit()

    ctx = _ctx(db_session, reader)
    output = await TOOLS["propose_assignment"].handler(
        ctx, {"ticket_id": ticket.id, "persona_name": "atlas"}
    )
    assert ctx.pending_actions == []
    assert "work queue" in output.text


@pytest.mark.asyncio
async def test_reader_reassigning_open_ticket_sends_it_back_to_suggested(
    client: AsyncClient, db_session: AsyncSession
):
    _, reader, reader_key, project, ticket = await _make_reader_setup(
        db_session, status="open"
    )
    ctx = _ctx(db_session, reader)
    await TOOLS["propose_assignment"].handler(
        ctx, {"ticket_id": ticket.id, "persona_name": "Atlas"}
    )
    params = ctx.pending_actions[0]["params"]
    conv = await _make_conversation(db_session, reader)
    action = await _make_action(
        db_session, reader, kind="assign_ticket", params=params, conversation=conv
    )

    resp = await _confirm(client, reader_key, action)
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["targets"] == [{"id": ticket.id, "ok": True, "error": None}]

    await db_session.refresh(ticket)
    assert ticket.assigned_to == "atlas"
    assert ticket.status == "suggested"
    edits = (
        await db_session.execute(select(TicketEdit).where(TicketEdit.ticket_id == ticket.id))
    ).scalars().all()
    assert sorted(e.field_name for e in edits) == ["assigned_to", "status"]
    system_comments = (
        await db_session.execute(
            select(TicketComment.content).where(
                TicketComment.ticket_id == ticket.id,
                TicketComment.author_persona == "system",
            )
        )
    ).scalars().all()
    assert any("needs approval again" in c for c in system_comments)
    assert any("Ticket fields updated" in c for c in system_comments)


@pytest.mark.asyncio
async def test_editor_can_reassign_in_progress_ticket_via_helper(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session, name="owner")
    member, member_key = await _make_user(db_session, name="member")
    org = await _make_org_with(db_session, owner, member)
    project = await _make_project(db_session, owner, org_id=org.id)
    ticket = await _make_ticket(db_session, member, project, status="in_progress")
    await _make_org_persona(db_session, org, owner, "atlas")

    ctx = _ctx(db_session, member)
    await TOOLS["propose_assignment"].handler(
        ctx, {"ticket_id": ticket.id, "persona_name": "atlas"}
    )
    assert len(ctx.pending_actions) == 1
    conv = await _make_conversation(db_session, member)
    action = await _make_action(
        db_session,
        member,
        kind="assign_ticket",
        params=ctx.pending_actions[0]["params"],
        conversation=conv,
    )

    resp = await _confirm(client, member_key, action)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "confirmed"
    await db_session.refresh(ticket)
    assert ticket.assigned_to == "atlas"
    assert ticket.status == "in_progress"
    edits = (
        await db_session.execute(select(TicketEdit).where(TicketEdit.ticket_id == ticket.id))
    ).scalars().all()
    assert [e.field_name for e in edits] == ["assigned_to"]


# -- 2. Comments honour the org lease-epoch setting --------------------------


@pytest.mark.asyncio
async def test_helper_comment_honours_org_lease_epoch_requirement(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="owner")
    org = await _make_org_with(db_session, owner)
    org.settings = json.dumps({"require_lease_epoch_on_ticket_writes": True})
    await db_session.commit()
    project = await _make_project(db_session, owner, org_id=org.id)
    ticket = await _make_ticket(db_session, owner, project)
    conv = await _make_conversation(db_session, owner)
    action = await _make_action(
        db_session,
        owner,
        kind="comment_ticket",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "body": "status update",
            "sources": {"body": ["user"]},
        },
        conversation=conv,
    )

    resp = await _confirm(client, owner_key, action)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"
    assert resp.json()["result"]["targets"] == [
        {"id": ticket.id, "ok": False, "error": "lease_epoch_required"}
    ]
    assert (await db_session.execute(select(TicketComment))).scalars().all() == []


# -- 3. Confirm edits go through the route models ----------------------------


def _create_params(project: Project, **overrides: object) -> dict:
    params: dict[str, object] = {
        "project_id": project.id,
        "title": "Draft title",
        "description": "draft",
        "acceptance_criteria": [],
        "priority": "medium",
        "kind": "task",
        "assigned_to": None,
        "sources": _ticket_sources(),
    }
    params.update(overrides)
    return params


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edit",
    [
        {"description": "d" * (MAX_DESCRIPTION_CHARS + 1)},
        {"title": "t" * 201},
        {"acceptance_criteria": ["c" * (MAX_CRITERION_CHARS + 1)]},
        {"priority": "urgent"},
    ],
)
async def test_confirm_create_edit_limits_are_422(
    client: AsyncClient, db_session: AsyncSession, edit: dict
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session, user, kind="create_ticket", params=_create_params(project), conversation=conv
    )
    resp = await _confirm(client, key, action, edit)
    assert resp.status_code == 422, resp.text
    await db_session.refresh(action)
    assert action.status == "pending"
    assert (await db_session.execute(select(Ticket))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_body", ["   \n\t ", "x" * (MAX_COMMENT_CHARS + 1)])
async def test_confirm_comment_edit_whitespace_or_oversized_is_422(
    client: AsyncClient, db_session: AsyncSession, bad_body: str
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, user, project)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="comment_ticket",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "body": "draft",
            "sources": {"body": ["user"]},
        },
        conversation=conv,
    )
    resp = await _confirm(client, key, action, {"body": bad_body})
    assert resp.status_code == 422, resp.text
    await db_session.refresh(action)
    assert action.status == "pending"
    assert (await db_session.execute(select(TicketComment))).scalars().all() == []


@pytest.mark.asyncio
async def test_confirm_comment_edit_is_stored_stripped(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, user, project)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="comment_ticket",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "body": "draft",
            "sources": {"body": ["user"]},
        },
        conversation=conv,
    )
    resp = await _confirm(client, key, action, {"body": "  edited body \n"})
    assert resp.status_code == 200, resp.text
    await db_session.refresh(action)
    assert json.loads(action.params)["edited"]["body"] == "edited body"
    comment = (await db_session.execute(select(TicketComment))).scalar_one()
    assert comment.content.startswith("edited body\n\nDrafted with the SessionFS helper")


# -- 4. Blank and edited assignees -------------------------------------------


@pytest.mark.asyncio
async def test_confirm_blank_assignee_stores_null(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params=_create_params(project, assigned_to="atlas"),
        conversation=conv,
    )
    resp = await _confirm(client, key, action, {"assigned_to": "  "})
    assert resp.status_code == 200, resp.text
    ticket = (await db_session.execute(select(Ticket))).scalar_one()
    assert ticket.assigned_to is None


@pytest.mark.asyncio
async def test_confirm_draft_with_empty_assignee_stores_null(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params=_create_params(project, assigned_to=""),
        conversation=conv,
    )
    resp = await _confirm(client, key, action)
    assert resp.status_code == 200, resp.text
    ticket = (await db_session.execute(select(Ticket))).scalar_one()
    assert ticket.assigned_to is None


@pytest.mark.asyncio
async def test_confirm_edited_assignee_resolves_represented_human(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="owner")
    human, _ = await _make_user(db_session, name="human")
    org = await _make_org_with(db_session, owner, human)
    project = await _make_project(db_session, owner, org_id=org.id)
    await _make_org_persona(db_session, org, owner, "scribe", represented_user_id=human.id)
    conv = await _make_conversation(db_session, owner)
    action = await _make_action(
        db_session, owner, kind="create_ticket", params=_create_params(project), conversation=conv
    )
    resp = await _confirm(client, owner_key, action, {"assigned_to": "Human"})
    assert resp.status_code == 200, resp.text
    ticket = (await db_session.execute(select(Ticket))).scalar_one()
    assert ticket.assigned_to == "scribe"


@pytest.mark.asyncio
async def test_confirm_edited_assignee_unknown_or_ambiguous_is_422(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="owner")
    human, _ = await _make_user(db_session, name="human")
    org = await _make_org_with(db_session, owner, human)
    project = await _make_project(db_session, owner, org_id=org.id)
    await _make_org_persona(db_session, org, owner, "alpha", represented_user_id=human.id)
    await _make_org_persona(db_session, org, owner, "beta", represented_user_id=human.id)
    conv = await _make_conversation(db_session, owner)
    action = await _make_action(
        db_session, owner, kind="create_ticket", params=_create_params(project), conversation=conv
    )

    resp = await _confirm(client, owner_key, action, {"assigned_to": "Human"})
    assert resp.status_code == 422, resp.text
    assert "alpha" in resp.text and "beta" in resp.text

    resp = await _confirm(client, owner_key, action, {"assigned_to": "nobody-here"})
    assert resp.status_code == 422, resp.text
    assert "nobody-here" in resp.text
    assert (await db_session.execute(select(Ticket))).scalars().all() == []


# -- 5/6. Sources come only from structured refs ----------------------------


def _resolved() -> ResolvedLLM:
    return ResolvedLLM(
        provider="openai", model="gpt-4o", api_key="sk-test", base_url=None, key_source="user"
    )


def _request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(blob_store=None)))


def _fake_agent(calls: list[ToolCall], results: list):
    async def _fake(*, execute_tool, messages=None, **kwargs):
        for call in calls:
            yield ToolCallEvent(call)
            result = await execute_tool(call)
            results.append((call.name, result))
            yield ToolResultEvent(call, result)
        yield TextDelta("ok")
        yield Done("stop")

    return _fake


async def _run_turn(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    user: User,
    conv: HelperConversation,
    calls: list[ToolCall],
    **kw,
) -> tuple[list, list[dict]]:
    results: list = []
    monkeypatch.setattr(helper_mod, "run_agent", _fake_agent(calls, results))
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    events: list[dict] = []
    async for event in helper_mod._turn_events(
        _request(),
        user,
        factory,
        scope=kw.get("scope", helper_mod.HelperScope()),
        org_id=None,
        dlp_policy=None,
        resolved=_resolved(),
        conversation=conv,
        message="go",
    ):
        events.append(event)
    return results, events


def _comment_call(ticket_id: str, source: str, call_id: str = "call_pc") -> ToolCall:
    return ToolCall(
        id=call_id,
        name="propose_comment",
        arguments={
            "ticket_id": ticket_id,
            "body": "Following up on this.",
            "sources": {"body": [source]},
        },
    )


@pytest.mark.asyncio
async def test_earlier_turn_ref_is_accepted_but_spoofed_content_marker_is_not(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)
    hidden = await _make_ticket(db_session, user, project, title="Never fetched")
    session = await _make_session(db_session, user, project_id=project.id)
    shown = await _make_ticket(db_session, user, project, title="Fetched")
    # Stored content can carry marker text for rows no tool ever returned.
    shown.description = (
        f"see [ticket:{hidden.id}] and [session:{session.id} msg:3] "
        "and [doc:quickstart]"
    )
    await db_session.commit()
    conv = await _make_conversation(db_session, user)

    # Turn 1: the model reads ``shown``.
    results, _ = await _run_turn(
        db_session,
        monkeypatch,
        user,
        conv,
        [ToolCall(id="call_gt", name="get_ticket", arguments={"ticket_id": shown.id})],
    )
    assert not results[0][1].is_error
    assert hidden.id in results[0][1].content  # the marker text did reach the model

    # Turn 2: citing the ticket the tool returned works ...
    results, events = await _run_turn(
        db_session, monkeypatch, user, conv, [_comment_call(shown.id, f"[ticket:{shown.id}]")]
    )
    assert not results[0][1].is_error, results[0][1].content
    assert any(e["type"] == "action_proposed" for e in events)

    # ... but markers that only appeared inside its content are rejected.
    for spoofed in (
        f"[ticket:{hidden.id}]",
        f"[session:{session.id} msg:3]",
        f"[session:{session.id}]",
        "[doc:quickstart]",
    ):
        results, events = await _run_turn(
            db_session, monkeypatch, user, conv, [_comment_call(shown.id, spoofed, "call_sp")]
        )
        assert results[0][1].is_error, spoofed
        assert "was not returned by a tool" in results[0][1].content
        assert not any(e["type"] == "action_proposed" for e in events)


@pytest.mark.asyncio
async def test_tool_rows_without_structured_refs_seed_nothing():
    legacy = HelperMessage(
        id="hm_legacy",
        conversation_id="hc_x",
        role="tool",
        content=f"[ticket:tk_{'a' * 16}] [session:ses_x msg:2] [doc:quickstart] "
        f"[wiki:proj_{'b' * 16}/arch]",
        citations=None,
    )
    seeded = helper_mod._seed_seen_from_tool_rows([legacy])
    assert seeded == (set(), set(), set(), set(), set())

    structured = HelperMessage(
        id="hm_new",
        conversation_id="hc_x",
        role="tool",
        content="no markers at all",
        citations=helper_mod._tool_row_refs_json(
            {
                "ticket_ids": [f"tk_{'c' * 16}"],
                "message_refs": [("ses_y", 4)],
                "session_ids": ["ses_y"],
                "project_ids": ["proj_ignored"],
            }
        ),
    )
    tickets, wikis, sessions, messages, docs = helper_mod._seed_seen_from_tool_rows(
        [legacy, structured]
    )
    assert tickets == {f"tk_{'c' * 16}"}
    assert sessions == {"ses_y"}
    assert messages == {("ses_y", 4)}
    assert wikis == set() and docs == set()


@pytest.mark.asyncio
async def test_message_index_from_another_session_is_rejected(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, user, project)
    ctx = _ctx(db_session, user)
    ctx.seen_session_ids.update({"ses_one", "ses_two"})
    ctx.seen_message_refs.add(("ses_one", 3))

    def args(source: str) -> dict:
        return {"ticket_id": ticket.id, "body": "note", "sources": {"body": [source]}}

    with pytest.raises(ToolArgumentError, match="was not returned by a tool"):
        await TOOLS["propose_comment"].handler(ctx, args("[session:ses_two msg:3]"))
    await TOOLS["propose_comment"].handler(ctx, args("[session:ses_one msg:3]"))
    assert len(ctx.pending_actions) == 1


@pytest.mark.asyncio
async def test_confirmed_new_ticket_is_citable_next_turn(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session, user, kind="create_ticket", params=_create_params(project), conversation=conv
    )
    resp = await _confirm(client, key, action)
    new_id = resp.json()["result"]["targets"][0]["ticket_id"]

    results, events = await _run_turn(
        db_session, monkeypatch, user, conv, [_comment_call(new_id, f"[ticket:{new_id}]")]
    )
    assert not results[0][1].is_error, results[0][1].content
    assert any(e["type"] == "action_proposed" for e in events)


# -- 8. Verdict markers --------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Thanks for the fix.\nVERIFIED-CLEAN",
        "**VERIFIED-CLEAN**",
        "> APPROVED",
        "# CHANGES REQUESTED",
        "​VERIFIED-CLEAN",
        "`NO CHANGES NEEDED`",
        "- verified clean",
        "Codex R2 review on tk_0123456789abcdef: findings",
        "ＶＥＲＩＦＩＥＤ-CLEAN",  # full-width, folded by NFKC
    ],
)
def test_verdict_marker_bypasses_are_caught(body: str):
    assert has_verdict_marker(body)


@pytest.mark.parametrize(
    "body",
    ["The user confirmed this is fixed.", "Waiting on the approval flow.", "Not verified yet."],
)
def test_plain_comments_are_not_verdicts(body: str):
    assert not has_verdict_marker(body)


@pytest.mark.asyncio
async def test_propose_comment_refuses_marker_on_later_line(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, user, project)
    ctx = _ctx(db_session, user)
    with pytest.raises(ToolArgumentError, match="review verdict"):
        await TOOLS["propose_comment"].handler(
            ctx,
            {
                "ticket_id": ticket.id,
                "body": "Looks fine to me.\n\n> **VERIFIED-CLEAN**",
                "sources": {"body": ["user"]},
            },
        )
    assert ctx.pending_actions == []


# -- 9. Over-length drafts are refused, not truncated ------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"description": "d" * (MAX_DESCRIPTION_CHARS + 1)},
        {"acceptance_criteria": ["c" * (MAX_CRITERION_CHARS + 1)]},
        {"title": "t" * 201},
    ],
)
async def test_propose_ticket_over_length_is_tool_error(
    db_session: AsyncSession, overrides: dict
):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ctx = _ctx(db_session, user)
    with pytest.raises(ToolArgumentError):
        await TOOLS["propose_ticket"].handler(ctx, _ticket_args(project, **overrides))
    assert ctx.pending_actions == []


@pytest.mark.asyncio
async def test_propose_comment_over_length_is_tool_error(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, user, project)
    ctx = _ctx(db_session, user)
    with pytest.raises(ToolArgumentError, match="10000"):
        await TOOLS["propose_comment"].handler(
            ctx,
            {
                "ticket_id": ticket.id,
                "body": "x" * (MAX_COMMENT_CHARS + 1),
                "sources": {"body": ["user"]},
            },
        )
    assert ctx.pending_actions == []


@pytest.mark.asyncio
async def test_propose_ticket_keeps_full_description(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ctx = _ctx(db_session, user)
    long = "d" * 10_000
    await TOOLS["propose_ticket"].handler(ctx, _ticket_args(project, description=long))
    assert ctx.pending_actions[0]["params"]["description"] == long


# -- 10. Provenance never shows an email --------------------------------------


@pytest.mark.asyncio
async def test_provenance_line_never_uses_email(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    user.display_name = None
    await db_session.commit()
    project = await _make_project(db_session, user)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session, user, kind="create_ticket", params=_create_params(project), conversation=conv
    )
    resp = await _confirm(client, key, action)
    assert resp.status_code == 200, resp.text
    ticket = (await db_session.execute(select(Ticket))).scalar_one()
    assert ticket.description.endswith("— confirmed by a project member")
    assert user.email not in ticket.description


# -- 11. The ticket can't move projects before confirm ----------------------


@pytest.mark.asyncio
async def test_confirm_refuses_ticket_moved_to_another_project(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    first = await _make_project(db_session, user, name="First")
    second = await _make_project(db_session, user, name="Second")
    ticket = await _make_ticket(db_session, user, first)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="comment_ticket",
        params={
            "ticket_id": ticket.id,
            "project_id": first.id,
            "body": "hello",
            "sources": {"body": ["user"]},
        },
        conversation=conv,
    )
    ticket.project_id = second.id
    await db_session.commit()

    resp = await _confirm(client, key, action)
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["targets"] == [
        {"id": ticket.id, "ok": False, "error": "ticket_moved"}
    ]
    assert (await db_session.execute(select(TicketComment))).scalars().all() == []


@pytest.mark.asyncio
async def test_confirm_refuses_project_outside_conversation_scope(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    mine = await _make_project(db_session, user, name="Mine")
    other = await _make_project(db_session, user, name="Other")
    ticket = await _make_ticket(db_session, user, other)
    conv = await _make_conversation(db_session, user)
    conv.scope = json.dumps({"project_id": mine.id})
    await db_session.commit()
    action = await _make_action(
        db_session,
        user,
        kind="assign_ticket",
        params={"ticket_id": ticket.id, "project_id": other.id, "assigned_to": "atlas"},
        conversation=conv,
    )
    resp = await _confirm(client, key, action)
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["targets"] == [
        {"id": ticket.id, "ok": False, "error": "not_in_scope"}
    ]


# -- Missing coverage ----------------------------------------------------------


@pytest.mark.asyncio
async def test_access_revoked_between_proposal_and_confirm(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session, name="owner")
    member, member_key = await _make_user(db_session, name="member")
    org = await _make_org_with(db_session, owner, member)
    project = await _make_project(db_session, owner, org_id=org.id)
    ticket = await _make_ticket(db_session, owner, project)

    ctx = _ctx(db_session, member)
    await TOOLS["propose_comment"].handler(
        ctx,
        {"ticket_id": ticket.id, "body": "On it.", "sources": {"body": ["user"]}},
    )
    conv = await _make_conversation(db_session, member)
    action = await _make_action(
        db_session,
        member,
        kind="comment_ticket",
        params=ctx.pending_actions[0]["params"],
        conversation=conv,
    )

    membership = (
        await db_session.execute(
            select(OrgMember).where(OrgMember.org_id == org.id, OrgMember.user_id == member.id)
        )
    ).scalar_one()
    await db_session.delete(membership)
    await db_session.commit()

    resp = await _confirm(client, member_key, action)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"
    assert resp.json()["result"]["targets"][0]["ok"] is False
    assert (await db_session.execute(select(TicketComment))).scalars().all() == []


@pytest.mark.asyncio
async def test_pending_cap_applies_to_ticket_kinds(db_session: AsyncSession):
    from sessionfs.server.helper.tools import _new_action_id, _proposal_time

    user, _ = await _make_user(db_session)
    # A real project: expiring the oldest proposals audits them, and the
    # audit rows' project_id is a foreign key on PostgreSQL.
    project = await _make_project(db_session, user)
    conv = await _make_conversation(db_session, user)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)

    def proposal(kind: str) -> dict:
        created = _proposal_time()
        return {
            "id": _new_action_id(),
            "kind": kind,
            "params": {"ticket_id": f"tk_{'0' * 16}", "project_id": project.id},
            "preview": {"summary": "s", "targets": []},
            "created_at": created,
            "expires_at": created + timedelta(minutes=15),
        }

    kinds = ["create_ticket", "assign_ticket", "comment_ticket"] * 2 + ["comment_ticket"]
    batch = [proposal(k) for k in kinds]
    await helper_mod._persist_proposals(
        factory, user_id=user.id, conversation_id=conv.id, proposals=batch, message_id="hm_z"
    )
    db_session.expire_all()
    status = {
        a.id: a.status for a in (await db_session.execute(select(HelperAction))).scalars().all()
    }
    pending = [p for p in batch if status[p["id"]] == "pending"]
    assert len(pending) == helper_mod._MAX_PENDING_ACTIONS
    assert [status[p["id"]] for p in batch[:2]] == ["expired", "expired"]


@pytest.mark.asyncio
async def test_ticket_kinds_write_audit_events_and_outcome_notes(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, user, project)
    await _make_persona(db_session, project, user, "atlas")
    conv = await _make_conversation(db_session, user)

    create = await _make_action(
        db_session, user, kind="create_ticket", params=_create_params(project), conversation=conv
    )
    new_id = (await _confirm(client, key, create)).json()["result"]["targets"][0]["ticket_id"]
    assign = await _make_action(
        db_session,
        user,
        kind="assign_ticket",
        params={"ticket_id": ticket.id, "project_id": project.id, "assigned_to": "atlas"},
        conversation=conv,
    )
    assert (await _confirm(client, key, assign)).status_code == 200
    comment = await _make_action(
        db_session,
        user,
        kind="comment_ticket",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "body": "noted",
            "sources": {"body": ["user"]},
        },
        conversation=conv,
    )
    assert (await _confirm(client, key, comment)).status_code == 200

    events = (await db_session.execute(select(RetrievalAuditEvent))).scalars().all()
    by_action = {json.loads(e.arguments)["action_id"]: e for e in events}
    expected_tools = {
        create.id: "propose_ticket",
        assign.id: "propose_assignment",
        comment.id: "propose_comment",
    }
    for action_id, tool in expected_tools.items():
        event = by_action[action_id]
        assert event.tool_name == tool
        assert event.project_id == project.id
        assert event.source == "helper"
        assert json.loads(event.arguments)["decision"] == "confirmed"

    notes = (
        await db_session.execute(
            select(HelperMessage.content)
            .where(HelperMessage.conversation_id == conv.id, HelperMessage.role == "note")
            .order_by(HelperMessage.created_at)
        )
    ).scalars().all()
    assert notes == [
        f"[Action {create.id} (propose_ticket) confirmed: created ticket [ticket:{new_id}]",
        f"[Action {assign.id} (propose_assignment) confirmed: ticket [ticket:{ticket.id}]",
        f"[Action {comment.id} (propose_comment) confirmed: ticket [ticket:{ticket.id}]",
    ]


@pytest.mark.asyncio
async def test_trusted_reviewer_helper_comment_is_never_trusted(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    org = await _make_org_with(db_session, user)
    project = await _make_project(db_session, user, org_id=org.id)
    ticket = await _make_ticket(db_session, user, project)
    db_session.add(
        TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            org_id=org.id,
            project_id=project.id,
            user_id=user.id,
            service_key_id=None,
            reviewer_persona="codex-reviewer",
            is_active=True,
            created_by_user_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="comment_ticket",
        params={
            "ticket_id": ticket.id,
            "project_id": project.id,
            "body": "Reviewed the change; one nit below.",
            "sources": {"body": ["user"]},
        },
        conversation=conv,
    )
    resp = await _confirm(client, key, action)
    assert resp.status_code == 200, resp.text
    comment = (await db_session.execute(select(TicketComment))).scalar_one()
    assert comment.verdict_trusted is False
    assert comment.author_persona is None



@pytest.mark.asyncio
async def test_decision_audit_skips_projects_that_no_longer_exist(db_session: AsyncSession):
    """A proposal's stored project may be gone when its decision is audited;
    the audit rows' project_id is a foreign key, so it is never attributed."""
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)
    conv = await _make_conversation(db_session, user)

    gone = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params={"project_id": "prj_deleted"},
        conversation=conv,
        status="expired",
    )
    narrative = await _make_action(
        db_session,
        user,
        kind="set_auto_narrative",
        params={"project_ids": ["prj_deleted", project.id], "enabled": True},
        conversation=conv,
        status="expired",
    )
    real = await _make_action(
        db_session,
        user,
        kind="create_ticket",
        params={"project_id": project.id},
        conversation=conv,
        status="expired",
    )

    assert await helper_mod._audit_project_id(db_session, gone, {"project_id": "prj_deleted"}) is None
    assert await helper_mod._audit_project_id(db_session, real, {"project_id": project.id}) == project.id

    for action in (gone, narrative, real):
        await helper_mod._record_action_audit(db_session, user.id, action)
    await db_session.commit()

    events = (await db_session.execute(select(RetrievalAuditEvent))).scalars().all()
    by_action: dict[str, list[RetrievalAuditEvent]] = {}
    for e in events:
        by_action.setdefault(json.loads(e.arguments)["action_id"], []).append(e)
    assert [e.project_id for e in by_action[gone.id]] == [None]
    # Only the surviving project gets a row; the deleted one is dropped.
    assert [e.project_id for e in by_action[narrative.id]] == [project.id]
    assert [e.project_id for e in by_action[real.id]] == [project.id]

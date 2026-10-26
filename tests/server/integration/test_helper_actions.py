"""Session-helper write actions (piece 6): propose-only tools + confirm/cancel.

The proposal tools never mutate; they queue a pending ``helper_actions`` row
that only the owner can confirm/cancel through ``/api/v1/helper/actions/*``.
Access is re-checked per target at confirm time, so a proposal made as an org
admin fails cleanly if that standing is revoked before the click.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    HelperAction,
    HelperConversation,
    OrgMember,
    Organization,
    Project,
    Session,
    User,
)
from sessionfs.server.helper.context import HelperContext, HelperScope, RetrievalBudget
from sessionfs.server.helper.tools import TOOLS


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
    result: dict | None = None,
    created_at: datetime | None = None,
    decided_at: datetime | None = None,
) -> HelperAction:
    action = HelperAction(
        id=f"ha_{uuid.uuid4().hex[:24]}",
        user_id=user.id,
        conversation_id=conversation.id,
        kind=kind,
        params=json.dumps(params, sort_keys=True),
        preview=json.dumps({"summary": "test", "targets": []}, sort_keys=True),
        status=status,
        result=json.dumps(result, sort_keys=True) if result is not None else None,
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(minutes=15),
        created_at=created_at or datetime.now(timezone.utc),
        decided_at=decided_at,
    )
    db.add(action)
    await db.commit()
    await db.refresh(action)
    return action


def _ctx(
    db: AsyncSession,
    user: User,
    *,
    project_id: str | None = None,
    session_ids: tuple[str, ...] = (),
    dlp_policy: dict | None = None,
) -> HelperContext:
    return HelperContext(
        user=user,
        db=db,
        org_id=None,
        scope=HelperScope(project_id=project_id, session_ids=session_ids),
        budget=RetrievalBudget(),
        audit=None,
        dlp_policy=dlp_policy,
    )


def _project_ids(text: str) -> set[str]:
    return set(re.findall(r"\[project:([A-Za-z0-9_-]+)\]", text))


# ---------------------------------------------------------------------------
# Proposal tools never mutate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_auto_narrative_proposes_without_mutating(
    db_session: AsyncSession,
):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ctx = _ctx(db_session, user)

    output = await TOOLS["set_project_auto_narrative"].handler(
        ctx, {"project_ids": [project.id], "enabled": True}
    )

    assert "Proposed action" in output.text
    assert len(ctx.pending_actions) == 1
    assert ctx.pending_actions[0]["kind"] == "set_auto_narrative"

    await db_session.refresh(project)
    assert project.auto_narrative is False

    rows = (await db_session.execute(select(HelperAction))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_trash_sessions_proposes_without_mutating(
    db_session: AsyncSession,
):
    user, _ = await _make_user(db_session)
    session = await _make_session(db_session, user)
    ctx = _ctx(db_session, user)

    output = await TOOLS["trash_sessions"].handler(
        ctx, {"session_ids": [session.id], "reason": "cleanup"}
    )

    assert "Proposed action" in output.text
    assert len(ctx.pending_actions) == 1
    assert ctx.pending_actions[0]["kind"] == "trash_sessions"

    await db_session.refresh(session)
    assert session.is_deleted is False
    assert session.deleted_at is None


@pytest.mark.asyncio
async def test_project_scoped_cannot_propose_other_project(
    db_session: AsyncSession,
):
    user, _ = await _make_user(db_session)
    mine = await _make_project(db_session, user, name="Mine")
    other = await _make_project(db_session, user, name="Other")
    ctx = _ctx(db_session, user, project_id=mine.id)

    output = await TOOLS["set_project_auto_narrative"].handler(
        ctx, {"project_ids": [mine.id, other.id], "enabled": True}
    )

    # Only the in-scope project is proposed; the other is skipped as if it
    # didn't exist (no name leaks), and nothing is mutated.
    assert len(ctx.pending_actions) == 1
    proposal = ctx.pending_actions[0]
    assert proposal["params"] == {"project_ids": [mine.id], "enabled": True}
    by_id = {t["id"]: t for t in proposal["preview"]["targets"]}
    assert by_id[mine.id] == {
        "id": mine.id, "name": "Mine", "change": "will_change", "before": False, "after": True,
    }
    assert by_id[other.id] == {
        "id": other.id, "name": other.id, "change": "skipped", "reason": "not_accessible",
    }
    assert f"- [{other.id}] {other.id}: skipped (not_accessible)" in output.text
    assert "Other" not in output.text
    await db_session.refresh(other)
    assert other.auto_narrative is False


# ---------------------------------------------------------------------------
# Read tools: list parity + no existence leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_projects_matches_projects_route(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    mine = await _make_project(db_session, user, name="Personal")
    org = await _make_org_with(db_session, user)
    org_project = await _make_project(db_session, user, org_id=org.id, name="Org Repo")
    other, _ = await _make_user(db_session, name="bob")
    await _make_project(db_session, other, name="Foreign")

    resp = await client.get("/api/v1/projects/", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    route_ids = {p["id"] for p in resp.json()}

    ctx = _ctx(db_session, user)
    tool_out = await TOOLS["list_projects"].handler(ctx, {})
    tool_ids = _project_ids(tool_out.text)

    assert route_ids == tool_ids
    assert {mine.id, org_project.id} <= route_ids


@pytest.mark.asyncio
async def test_get_project_settings_inaccessible_is_not_found(
    db_session: AsyncSession,
):
    user, _ = await _make_user(db_session)
    other, _ = await _make_user(db_session, name="bob")
    foreign = await _make_project(db_session, other, name="Foreign")
    ctx = _ctx(db_session, user)

    output = await TOOLS["get_project_settings"].handler(
        ctx, {"project_id": foreign.id}
    )
    assert output.text == "Project not found."


# ---------------------------------------------------------------------------
# Confirm / cancel endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_applies_stored_targets(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="set_auto_narrative",
        params={"project_ids": [project.id], "enabled": True},
        conversation=conv,
    )

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["result"]["targets"] == [{"id": project.id, "ok": True, "error": None}]

    await db_session.refresh(project)
    assert project.auto_narrative is True

    # A retrieval-audit event (source=helper, the real tool name, the
    # per-target outcome) and an outcome note were written.
    from sessionfs.server.db.models import (
        HelperMessage,
        RetrievalAuditContext,
        RetrievalAuditEvent,
    )

    notes = (
        await db_session.execute(
            select(HelperMessage).where(HelperMessage.conversation_id == conv.id)
        )
    ).scalars().all()
    assert [(n.role, n.content) for n in notes] == [
        ("note", f"[Action {action.id} (set_project_auto_narrative) confirmed: 1 changed, 0 failed]")
    ]
    assert json.loads(notes[0].tool_args) == {"action_id": action.id, "status": "confirmed"}
    events = (await db_session.execute(select(RetrievalAuditEvent))).scalars().all()
    event = next(e for e in events if e.tool_name == "set_project_auto_narrative")
    assert event.source == "helper"
    assert event.project_id == project.id
    args = json.loads(event.arguments)
    assert args["action_id"] == action.id and args["decision"] == "confirmed"
    assert json.loads(event.returned_refs)["outcome"] == [
        {"id": project.id, "ok": True, "error": None}
    ]
    context = (
        await db_session.execute(
            select(RetrievalAuditContext).where(RetrievalAuditContext.id == event.context_id)
        )
    ).scalar_one()
    assert context.project_id == project.id


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
        kind="set_auto_narrative",
        params={"project_ids": [project.id], "enabled": True},
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

    await db_session.refresh(project)
    assert project.auto_narrative is True


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
        kind="set_auto_narrative",
        params={"project_ids": [project.id], "enabled": True},
        conversation=conv,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "action_expired"

    await db_session.refresh(project)
    assert project.auto_narrative is False


@pytest.mark.asyncio
async def test_cancel_then_confirm_is_409(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="set_auto_narrative",
        params={"project_ids": [project.id], "enabled": True},
        conversation=conv,
    )

    cancel = await client.post(
        f"/api/v1/helper/actions/{action.id}/cancel", headers=_hdrs(key)
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "cancelled"

    confirm = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert confirm.status_code == 409, confirm.text
    assert confirm.json()["error"]["code"] == "action_cancelled"

    await db_session.refresh(project)
    assert project.auto_narrative is False


@pytest.mark.asyncio
async def test_other_users_action_is_404(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session, name="owner")
    other, other_key = await _make_user(db_session, name="other")
    project = await _make_project(db_session, owner)
    conv = await _make_conversation(db_session, owner)
    action = await _make_action(
        db_session,
        owner,
        kind="set_auto_narrative",
        params={"project_ids": [project.id], "enabled": True},
        conversation=conv,
    )

    resp = await client.get(
        f"/api/v1/helper/actions/{action.id}", headers=_hdrs(other_key)
    )
    assert resp.status_code == 404

    confirm = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(other_key)
    )
    assert confirm.status_code == 404


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


@pytest.mark.asyncio
async def test_access_rechecked_at_confirm(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    org = await _make_org_with(db_session, user)
    personal = await _make_project(db_session, user, name="Personal")
    # An org project the user does NOT own (admin standing comes from the org).
    colleague, _ = await _make_user(db_session, name="colleague")
    org_project = await _make_project(db_session, colleague, org_id=org.id, name="Org Repo")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="set_auto_narrative",
        params={"project_ids": [org_project.id, personal.id], "enabled": True},
        conversation=conv,
    )

    # Leave the org before confirming: access to its project is gone.
    membership = (
        await db_session.execute(
            select(OrgMember).where(
                OrgMember.org_id == org.id, OrgMember.user_id == user.id
            )
        )
    ).scalar_one()
    await db_session.delete(membership)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "confirmed"  # one target still succeeded
    by_id = {t["id"]: t for t in body["result"]["targets"]}
    assert by_id[org_project.id] == {"id": org_project.id, "ok": False, "error": "forbidden"}
    assert by_id[personal.id] == {"id": personal.id, "ok": True, "error": None}

    await db_session.refresh(personal)
    await db_session.refresh(org_project)
    assert personal.auto_narrative is True
    assert org_project.auto_narrative is False


@pytest.mark.asyncio
async def test_confirm_trash_moves_sessions_to_trash(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    session = await _make_session(db_session, user)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="trash_sessions",
        params={"session_ids": [session.id]},
        conversation=conv,
    )

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["targets"] == [
        {"id": session.id, "ok": True, "error": None}
    ]

    await db_session.refresh(session)
    assert session.is_deleted is True
    assert session.deleted_by == user.id
    assert session.delete_scope == "cloud"
    assert session.purge_after is not None

    # Restorable: clearing the soft-delete flag is all it takes.
    session.is_deleted = False
    session.deleted_at = None
    await db_session.commit()
    await db_session.refresh(session)
    assert session.is_deleted is False


# ---------------------------------------------------------------------------
# Turn path: persist before emit, drop on redaction failure (M1 / M3)
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

import sessionfs.server.routes.helper as helper_mod  # noqa: E402
from sessionfs.judge.agent_stream import (  # noqa: E402
    Done,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolResultEvent,
    _anthropic_messages,
    _google_messages,
    _openai_messages,
)
from sessionfs.server.db.models import (  # noqa: E402
    HelperMessage,
    RetrievalAuditEvent,
)
from sessionfs.server.helper.keys import ResolvedLLM  # noqa: E402
from sessionfs.server.helper.tools import tool_specs  # noqa: E402


def _resolved() -> ResolvedLLM:
    return ResolvedLLM(
        provider="openai", model="gpt-4o", api_key="sk-test", base_url=None, key_source="user"
    )


def _request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(blob_store=None)))


def _fake_agent_proposing(call: ToolCall, gate: asyncio.Event | None = None, seen: list | None = None):
    async def _fake(*, execute_tool, messages=None, **kwargs):
        if seen is not None:
            seen.append(messages)
        yield ToolCallEvent(call)
        result = await execute_tool(call)
        yield ToolResultEvent(call, result)
        if gate is not None:
            await gate.wait()
        yield TextDelta("Proposed — confirm it in the card.")
        yield Done("stop")

    return _fake


def _turn(db_session: AsyncSession, user: User, conv: HelperConversation, message: str, **kw):
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    return helper_mod._turn_events(
        _request(),
        user,
        factory,
        scope=kw.get("scope", helper_mod.HelperScope()),
        org_id=None,
        dlp_policy=kw.get("dlp_policy"),
        resolved=_resolved(),
        conversation=conv,
        message=message,
    )


@pytest.mark.asyncio
async def test_turn_persists_action_before_event_and_confirm_works_mid_turn(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user, name="Acme")
    conv = await _make_conversation(db_session, user)
    gate = asyncio.Event()
    call = ToolCall(
        id="call_an",
        name="set_project_auto_narrative",
        arguments={"project_ids": [project.id], "enabled": True},
    )
    monkeypatch.setattr(helper_mod, "run_agent", _fake_agent_proposing(call, gate))

    events = _turn(db_session, user, conv, "turn narrative on")
    proposed = None
    async for event in events:
        if event["type"] == "action_proposed":
            proposed = event["action"]
            break
    assert proposed is not None

    # The row is committed by the time the event is emitted ...
    row = (
        await db_session.execute(select(HelperAction).where(HelperAction.id == proposed["id"]))
    ).scalar_one()
    assert row.status == "pending" and row.message_id

    # ... so a Confirm while the turn is still running succeeds.
    resp = await client.post(f"/api/v1/helper/actions/{row.id}/confirm", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "confirmed"

    gate.set()
    async for _ in events:
        pass

    await db_session.refresh(project)
    assert project.auto_narrative is True
    # The tool row that carried the proposal is the one the action points at,
    # and the thread exposes the link structurally.
    tool_row = (
        await db_session.execute(
            select(HelperMessage).where(
                HelperMessage.conversation_id == conv.id, HelperMessage.role == "tool"
            )
        )
    ).scalar_one()
    assert tool_row.id == row.message_id
    detail = await client.get(f"/api/v1/helper/conversations/{conv.id}", headers=_hdrs(key))
    assert detail.status_code == 200, detail.text
    tool_msgs = [m for m in detail.json()["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["action_id"] == row.id


@pytest.mark.asyncio
async def test_turn_drops_proposal_when_redaction_fails(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    from sessionfs.server.helper import tools as tools_mod
    from sessionfs.server.helper.redaction import RedactionError

    user, _ = await _make_user(db_session)
    session = await _make_session(db_session, user, title="Old spike")
    conv = await _make_conversation(db_session, user)

    def _broken(text, policy):
        raise RedactionError("bad custom pattern")

    monkeypatch.setattr(tools_mod, "redact", _broken)
    call = ToolCall(id="call_tr", name="trash_sessions", arguments={"session_ids": [session.id]})
    monkeypatch.setattr(helper_mod, "run_agent", _fake_agent_proposing(call))

    kinds: list[str] = []
    results: list[dict] = []
    async for event in _turn(db_session, user, conv, "clean up"):
        kinds.append(event["type"])
        if event["type"] == "tool_result":
            results.append(event)

    assert "action_proposed" not in kinds
    assert results and results[0]["ok"] is False
    assert (await db_session.execute(select(HelperAction))).scalars().all() == []
    await db_session.refresh(session)
    assert session.is_deleted is False


@pytest.mark.asyncio
async def test_turn_redacts_preview_names_and_reason(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, _ = await _make_user(db_session)
    org = await _make_org_with(db_session, user)
    org.settings = json.dumps({"dlp": {"enabled": True, "mode": "redact", "categories": ["phi"]}})
    await db_session.commit()
    session = await _make_session(db_session, user, title="SSN 123-45-6789 notes")
    conv = await _make_conversation(db_session, user)
    policy = await helper_mod._resolve_dlp_policy(db_session, user, None)

    call = ToolCall(
        id="call_tr",
        name="trash_sessions",
        arguments={"session_ids": [session.id], "reason": "holds 987-65-4321"},
    )
    monkeypatch.setattr(helper_mod, "run_agent", _fake_agent_proposing(call))
    proposed = []
    async for event in _turn(db_session, user, conv, "clean up", dlp_policy=policy):
        if event["type"] == "action_proposed":
            proposed.append(event["action"])

    assert len(proposed) == 1
    assert "123-45-6789" not in json.dumps(proposed)
    row = (await db_session.execute(select(HelperAction))).scalar_one()
    assert "123-45-6789" not in (row.preview or "")
    assert "987-65-4321" not in (row.params or "")
    assert "[REDACTED:ssn]" in row.preview and "[REDACTED:ssn]" in row.params


# ---------------------------------------------------------------------------
# Pending cap across turns (L7)
# ---------------------------------------------------------------------------


def _proposal(kind: str = "trash_sessions") -> dict:
    from sessionfs.server.helper.tools import _new_action_id, _proposal_time

    created = _proposal_time()
    return {
        "id": _new_action_id(),
        "kind": kind,
        "params": {"session_ids": ["ses_x"]},
        "preview": {"summary": "s", "targets": []},
        "created_at": created,
        "expires_at": created + timedelta(minutes=15),
    }


@pytest.mark.asyncio
async def test_pending_cap_expires_oldest_across_turns(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    conv = await _make_conversation(db_session, user)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    old = [
        await _make_action(
            db_session,
            user,
            kind="trash_sessions",
            params={"session_ids": ["ses_x"]},
            conversation=conv,
            created_at=base + timedelta(seconds=i),
        )
        for i in range(5)
    ]

    # Next turn: two more proposals -> the two oldest expire, never the new ones.
    new = [_proposal(), _proposal()]
    await helper_mod._persist_proposals(
        factory, user_id=user.id, conversation_id=conv.id, proposals=new, message_id="hm_x"
    )
    db_session.expire_all()
    status = {
        a.id: a.status
        for a in (await db_session.execute(select(HelperAction))).scalars().all()
    }
    assert status[old[0].id] == "expired" and status[old[1].id] == "expired"
    assert all(status[a.id] == "pending" for a in old[2:])
    assert all(status[p["id"]] == "pending" for p in new)

    # Cap expiry is audited and noted like any other decision.
    events = (await db_session.execute(select(RetrievalAuditEvent))).scalars().all()
    decided = {json.loads(e.arguments)["action_id"] for e in events if e.tool_name == "trash_sessions"}
    assert decided == {old[0].id, old[1].id}


@pytest.mark.asyncio
async def test_pending_cap_never_expires_newest_of_a_turn(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    conv = await _make_conversation(db_session, user)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    batch = [_proposal() for _ in range(helper_mod._MAX_PENDING_ACTIONS + 2)]
    await helper_mod._persist_proposals(
        factory, user_id=user.id, conversation_id=conv.id, proposals=batch, message_id="hm_y"
    )
    db_session.expire_all()
    status = {
        a.id: a.status
        for a in (await db_session.execute(select(HelperAction))).scalars().all()
    }
    assert [status[p["id"]] for p in batch] == ["expired", "expired"] + ["pending"] * 5


# ---------------------------------------------------------------------------
# Plan gate (L4) + settings-route authorization parity (L3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_free_project_proposal_marks_upgrade_required(db_session: AsyncSession):
    user, _ = await _make_user(db_session, tier="free")
    project = await _make_project(db_session, user, name="Free repo")
    ctx = _ctx(db_session, user)

    output = await TOOLS["set_project_auto_narrative"].handler(
        ctx, {"project_ids": [project.id], "enabled": True}
    )
    assert ctx.pending_actions == []
    assert "Nothing to change." in output.text
    assert f"- [{project.id}] Free repo: skipped (upgrade_required)" in output.text


@pytest.mark.asyncio
async def test_confirm_fails_project_context_gate_on_free_project(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session, tier="free")
    project = await _make_project(db_session, user, name="Free repo")
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="set_auto_narrative",
        params={"project_ids": [project.id], "enabled": True},
        conversation=conv,
    )
    resp = await client.post(f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["result"]["targets"] == [
        {"id": project.id, "ok": False, "error": "upgrade_required"}
    ]
    await db_session.refresh(project)
    assert project.auto_narrative is False


@pytest.mark.asyncio
async def test_org_member_may_toggle_like_the_settings_route(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session, name="owner")
    member, member_key = await _make_user(db_session, name="member")
    org = await _make_org_with(db_session, owner, member)
    project = await _make_project(db_session, owner, org_id=org.id, name="Team repo")

    # The settings route lets a plain member flip it ...
    route = await client.put(
        f"/api/v1/projects/{project.id}/settings",
        headers=_hdrs(member_key),
        json={"auto_narrative": True},
    )
    assert route.status_code == 200, route.text
    await db_session.refresh(project)
    assert project.auto_narrative is True

    # ... so the helper proposes and confirms it for them too.
    ctx = _ctx(db_session, member)
    await TOOLS["set_project_auto_narrative"].handler(
        ctx, {"project_ids": [project.id], "enabled": False}
    )
    assert ctx.pending_actions[0]["params"]["project_ids"] == [project.id]
    conv = await _make_conversation(db_session, member)
    action = await _make_action(
        db_session,
        member,
        kind="set_auto_narrative",
        params={"project_ids": [project.id], "enabled": False},
        conversation=conv,
    )
    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(member_key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["targets"] == [{"id": project.id, "ok": True, "error": None}]
    await db_session.refresh(project)
    assert project.auto_narrative is False


@pytest.mark.asyncio
async def test_captured_session_reader_proposal_marks_not_editor(
    db_session: AsyncSession,
):
    """A reader whose only access is a session on the project can view it
    but not change it, so the proposal skips the target as ``not_editor``."""
    owner, _ = await _make_user(db_session, name="owner")
    reader, _ = await _make_user(db_session, name="reader")
    project = await _make_project(db_session, owner, name="Shared")
    await _make_session(db_session, reader, project_id=project.id)

    ctx = _ctx(db_session, reader)
    output = await TOOLS["set_project_auto_narrative"].handler(
        ctx, {"project_ids": [project.id], "enabled": True}
    )
    assert ctx.pending_actions == []
    assert "not_editor" in output.text
    await db_session.refresh(project)
    assert project.auto_narrative is False

    # Reading the settings is still fine.
    read = await TOOLS["get_project_settings"].handler(ctx, {"project_id": project.id})
    assert "auto_narrative: off" in read.text


@pytest.mark.asyncio
async def test_confirm_refuses_captured_session_reader(
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
        kind="set_auto_narrative",
        params={"project_ids": [project.id], "enabled": True},
        conversation=conv,
    )

    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(reader_key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["result"]["targets"] == [
        {"id": project.id, "ok": False, "error": "not_editor"}
    ]
    await db_session.refresh(project)
    assert project.auto_narrative is False


# ---------------------------------------------------------------------------
# Session scope (L5 + trash)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trash_under_session_scope(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    inside = await _make_session(db_session, user, title="Inside")
    outside = await _make_session(db_session, user, title="Outside")
    ctx = _ctx(db_session, user, session_ids=(inside.id,))

    await TOOLS["trash_sessions"].handler(ctx, {"session_ids": [inside.id, outside.id]})
    proposal = ctx.pending_actions[0]
    assert proposal["params"] == {"session_ids": [inside.id]}
    by_id = {t["id"]: t for t in proposal["preview"]["targets"]}
    assert by_id[outside.id]["change"] == "skipped"
    assert by_id[outside.id]["reason"] == "not_in_scope"
    target = by_id[inside.id]
    assert target["change"] == "will_change"
    assert target["tool"] == "claude-code"
    assert datetime.fromisoformat(target["created_at"]).tzinfo is not None


@pytest.mark.asyncio
async def test_list_projects_parity_for_org_member_and_session_scope(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session, name="owner")
    member, member_key = await _make_user(db_session, name="member")
    org = await _make_org_with(db_session, owner, member)
    team_a = await _make_project(db_session, owner, org_id=org.id, name="Team A")
    team_b = await _make_project(db_session, owner, org_id=org.id, name="Team B")
    await _make_project(db_session, owner, name="Owner personal")

    resp = await client.get("/api/v1/projects/", headers=_hdrs(member_key))
    assert resp.status_code == 200, resp.text
    route_ids = {p["id"] for p in resp.json()}
    tool_ids = _project_ids((await TOOLS["list_projects"].handler(_ctx(db_session, member), {})).text)
    assert route_ids == tool_ids == {team_a.id, team_b.id}

    session = await _make_session(db_session, member, project_id=team_a.id)
    scoped = _ctx(db_session, member, session_ids=(session.id,))
    scoped_ids = _project_ids((await TOOLS["list_projects"].handler(scoped, {})).text)
    assert scoped_ids == {team_a.id}


# ---------------------------------------------------------------------------
# Decision races (M2) + org-bound actions (L6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_after_confirm_returns_stored_state(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="set_auto_narrative",
        params={"project_ids": [project.id], "enabled": True},
        conversation=conv,
    )
    confirm = await client.post(f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key))
    assert confirm.status_code == 200, confirm.text

    cancel = await client.post(f"/api/v1/helper/actions/{action.id}/cancel", headers=_hdrs(key))
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "confirmed"
    assert cancel.json()["result"] == confirm.json()["result"]

    again = await client.post(f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key))
    assert again.status_code == 200
    assert again.json()["status"] == "confirmed"

    row = (
        await db_session.execute(
            select(HelperAction)
            .where(HelperAction.id == action.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.status == "confirmed"
    notes = (
        await db_session.execute(
            select(HelperMessage).where(HelperMessage.conversation_id == conv.id)
        )
    ).scalars().all()
    assert len(notes) == 1  # the losing cancel wrote nothing


@pytest.mark.asyncio
async def test_lazy_expiry_never_overwrites_a_confirmed_row(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="trash_sessions",
        params={"session_ids": ["ses_x"]},
        conversation=conv,
        status="confirmed",
        result={"targets": [{"id": "ses_x", "ok": True, "error": None}]},
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    for method, path in (
        ("GET", ""),
        ("POST", "/cancel"),
        ("POST", "/confirm"),
    ):
        resp = await client.request(method, f"/api/v1/helper/actions/{action.id}{path}", headers=_hdrs(key))
        assert resp.status_code == 200, (path, resp.text)
        assert resp.json()["status"] == "confirmed"


@pytest.mark.asyncio
async def test_concurrent_confirm_and_cancel_decide_once(tmp_path):
    """Best-effort race on SQLite: a file database, so each request has its own
    connection (the in-memory test engine shares one connection, which can't
    run two transactions at once).  Exactly one decision lands and both
    callers answer from it."""
    from fastapi import HTTPException
    from sqlalchemy.ext.asyncio import create_async_engine

    from sessionfs.server.db.engine import install_sqlite_unicode_lower
    from sessionfs.server.db.models import Base
    from sessionfs.server.tier_gate import build_user_context

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    install_sqlite_unicode_lower(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        for _ in range(5):
            async with factory() as setup:
                user, _ = await _make_user(setup)
                project = await _make_project(setup, user)
                conv = await _make_conversation(setup, user)
                action = await _make_action(
                    setup,
                    user,
                    kind="set_auto_narrative",
                    params={"project_ids": [project.id], "enabled": True},
                    conversation=conv,
                )

            async def do_confirm():
                async with factory() as db:
                    ctx = await build_user_context(user, db)
                    try:
                        return await helper_mod.confirm_helper_action(action.id, user, ctx, db)
                    except HTTPException as exc:
                        return exc

            async def do_cancel():
                async with factory() as db:
                    return await helper_mod.cancel_helper_action(action.id, user, db)

            confirm, cancel = await asyncio.gather(do_confirm(), do_cancel())

            async with factory() as check:
                row = (
                    await check.execute(select(HelperAction).where(HelperAction.id == action.id))
                ).scalar_one()
                narrative = (
                    await check.execute(select(Project.auto_narrative).where(Project.id == project.id))
                ).scalar_one()
                notes = (
                    await check.execute(
                        select(HelperMessage).where(HelperMessage.conversation_id == conv.id)
                    )
                ).scalars().all()
            assert row.status in ("confirmed", "cancelled")
            assert cancel["status"] == row.status
            assert len(notes) == 1
            if row.status == "confirmed":
                assert isinstance(confirm, dict) and confirm["status"] == "confirmed"
                assert narrative is True
            else:
                assert isinstance(confirm, HTTPException) and confirm.status_code == 409
                assert confirm.detail["code"] == "action_cancelled"
                assert narrative is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_and_expiry_are_audited(client: AsyncClient, db_session: AsyncSession):
    user, key = await _make_user(db_session)
    conv = await _make_conversation(db_session, user)
    live = await _make_action(
        db_session, user, kind="trash_sessions", params={"session_ids": ["ses_a"]}, conversation=conv
    )
    stale = await _make_action(
        db_session,
        user,
        kind="trash_sessions",
        params={"session_ids": ["ses_b"]},
        conversation=conv,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    assert (await client.post(f"/api/v1/helper/actions/{live.id}/cancel", headers=_hdrs(key))).json()["status"] == "cancelled"
    assert (await client.get(f"/api/v1/helper/actions/{stale.id}", headers=_hdrs(key))).json()["status"] == "expired"

    events = (await db_session.execute(select(RetrievalAuditEvent))).scalars().all()
    decisions = {
        json.loads(e.arguments)["action_id"]: json.loads(e.arguments)["decision"]
        for e in events
        if e.tool_name == "trash_sessions"
    }
    assert decisions == {live.id: "cancelled", stale.id: "expired"}


@pytest.mark.asyncio
async def test_action_in_org_thread_is_404_after_leaving_org(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session, name="owner")
    member, member_key = await _make_user(db_session, name="member")
    org = await _make_org_with(db_session, owner, member)
    conv = await _make_conversation(db_session, member, org_id=org.id)
    action = await _make_action(
        db_session, member, kind="trash_sessions", params={"session_ids": ["ses_a"]}, conversation=conv
    )
    assert (await client.get(f"/api/v1/helper/actions/{action.id}", headers=_hdrs(member_key))).status_code == 200

    membership = (
        await db_session.execute(
            select(OrgMember).where(OrgMember.org_id == org.id, OrgMember.user_id == member.id)
        )
    ).scalar_one()
    await db_session.delete(membership)
    await db_session.commit()

    for method, path in (("GET", ""), ("POST", "/confirm"), ("POST", "/cancel")):
        resp = await client.request(method, f"/api/v1/helper/actions/{action.id}{path}", headers=_hdrs(member_key))
        assert resp.status_code == 404, (path, resp.text)


# ---------------------------------------------------------------------------
# Retention sweep (M3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_sweep_deletes_old_actions(client: AsyncClient, db_session: AsyncSession):
    admin, key = await _make_user(db_session, "admin", tier="admin")
    org = await _make_org_with(db_session, admin)
    org.settings = json.dumps({"general": {"helper_retention_days": 1}})
    await db_session.commit()
    conv = await _make_conversation(db_session, admin, org_id=org.id)
    db_session.add(
        HelperMessage(id=f"hm_{uuid.uuid4().hex[:24]}", conversation_id=conv.id, role="user", content="hi")
    )
    await db_session.commit()
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=2)

    def mk(**kw):
        return _make_action(
            db_session, admin, kind="trash_sessions", params={"session_ids": ["s"]}, conversation=conv, **kw
        )

    old_decided = await mk(status="confirmed", created_at=old, decided_at=old, expires_at=old)
    old_expired = await mk(status="expired", created_at=old, decided_at=old, expires_at=old)
    old_pending = await mk(created_at=old, expires_at=old)
    recent_decided = await mk(status="cancelled", created_at=old, decided_at=now, expires_at=old)
    live_pending = await mk()

    resp = await client.post("/api/v1/admin/sweeps/helper-retention", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    assert resp.json()["actions_deleted"] == 3

    remaining = set((await db_session.execute(select(HelperAction.id))).scalars().all())
    assert remaining == {recent_decided.id, live_pending.id}
    assert not remaining & {old_decided.id, old_expired.id, old_pending.id}


# ---------------------------------------------------------------------------
# History replay after a confirm (L2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_after_confirm_has_no_undeclared_tool_calls(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user, name="Acme")
    conv = await _make_conversation(db_session, user)
    call = ToolCall(
        id="call_an",
        name="set_project_auto_narrative",
        arguments={"project_ids": [project.id], "enabled": True},
    )
    monkeypatch.setattr(helper_mod, "run_agent", _fake_agent_proposing(call))
    action_id = None
    async for event in _turn(db_session, user, conv, "turn narrative on"):
        if event["type"] == "action_proposed":
            action_id = event["action"]["id"]
    assert action_id
    resp = await client.post(f"/api/v1/helper/actions/{action_id}/confirm", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text

    # The next turn's history, as the route builds it.
    seen: list = []
    monkeypatch.setattr(
        helper_mod,
        "run_agent",
        _fake_agent_proposing(
            ToolCall(id="call_ls", name="list_projects", arguments={}), seen=seen
        ),
    )
    async for _ in _turn(db_session, user, conv, "is it on now?"):
        pass
    history = seen[0]
    declared = {spec.name for spec in tool_specs()}
    note = f"[Action {action_id} (set_project_auto_narrative) confirmed: 1 changed, 0 failed]"

    assert history[-1]["role"] == "user"
    assert note in history[-1]["content"] and "is it on now?" in history[-1]["content"]

    anthropic = _anthropic_messages(history)
    for m in anthropic:
        for block in m["content"]:
            if block.get("type") == "tool_use":
                assert block["name"] in declared
    roles = [m["role"] for m in anthropic]
    assert all(a != b for a, b in zip(roles, roles[1:])), roles

    openai = _openai_messages(history)
    for m in openai:
        for tc in m.get("tool_calls") or []:
            assert tc["function"]["name"] in declared
    tool_ids = {tc["id"] for m in openai for tc in m.get("tool_calls") or []}
    assert all(m["tool_call_id"] in tool_ids for m in openai if m["role"] == "tool")

    google = _google_messages(history)
    for m in google:
        for part in m["parts"]:
            if "functionCall" in part:
                assert part["functionCall"]["name"] in declared
            if "functionResponse" in part:
                assert part["functionResponse"]["name"] in declared
    assert note in google[-1]["parts"][0]["text"]


# ---------------------------------------------------------------------------
# Lost conditional UPDATE through the real HTTP client (round 2 regression)
# ---------------------------------------------------------------------------


def _lose_once(monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession, winner: dict):
    """Make the first ``_transition`` lose: another request commits ``winner``
    first, and this one's conditional UPDATE matches nothing."""
    from sqlalchemy import update as sa_update

    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    real = helper_mod._transition
    state = {"lost": False}

    async def losing(db, action, to_status, now, **kwargs):
        if not state["lost"]:
            state["lost"] = True
            async with factory() as other:
                await other.execute(
                    sa_update(HelperAction)
                    .where(HelperAction.id == action.id)
                    .values(decided_at=now, **winner)
                )
                await other.commit()
            return False
        return await real(db, action, to_status, now, **kwargs)

    monkeypatch.setattr(helper_mod, "_transition", losing)
    return state


_WON_CONFIRM = {
    "status": "confirmed",
    "result": json.dumps({"targets": [{"id": "x", "ok": True, "error": None}]}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expired", "winner", "code", "body_status", "error_code"),
    [
        ("/confirm", False, _WON_CONFIRM, 200, "confirmed", None),
        ("/confirm", False, {"status": "cancelled"}, 409, None, "action_cancelled"),
        ("/confirm", True, _WON_CONFIRM, 200, "confirmed", None),
        ("/cancel", False, _WON_CONFIRM, 200, "confirmed", None),
        ("/cancel", True, {"status": "cancelled"}, 200, "cancelled", None),
        ("", True, _WON_CONFIRM, 200, "confirmed", None),
    ],
)
async def test_lost_race_returns_stored_state_not_500(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    expired: bool,
    winner: dict,
    code: int,
    body_status: str | None,
    error_code: str | None,
):
    user, key = await _make_user(db_session)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="trash_sessions",
        params={"session_ids": ["ses_x"]},
        conversation=conv,
        expires_at=(
            datetime.now(timezone.utc) - timedelta(minutes=1)
            if expired
            else datetime.now(timezone.utc) + timedelta(minutes=10)
        ),
    )
    state = _lose_once(monkeypatch, db_session, winner)

    method = "GET" if path == "" else "POST"
    resp = await client.request(method, f"/api/v1/helper/actions/{action.id}{path}", headers=_hdrs(key))
    assert state["lost"] is True
    assert resp.status_code == code, resp.text
    if error_code:
        assert resp.json()["error"]["code"] == error_code
    else:
        assert resp.json()["status"] == body_status
    # The loser wrote no note of its own.
    notes = (
        await db_session.execute(
            select(HelperMessage).where(HelperMessage.conversation_id == conv.id)
        )
    ).scalars().all()
    assert notes == []

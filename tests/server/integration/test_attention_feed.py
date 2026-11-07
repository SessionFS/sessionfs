"""For You (tk_dc118ffd6e804a9a) — aggregator, providers, hides and REST.

Covers ranking, each provider's access predicate (a card disappears when
access is lost), hides that come back when the source changes, the silent
no-op for unknown keys, ETag 304, a slow provider degrading to a section
error, service-key minter + allowlist scoping, proof dollars for admins
only, the context endpoint, the sweep and the no-N+1 guarantee.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentPersona,
    AgentRun,
    ApiKey,
    AttentionHide,
    KnowledgeEntry,
    LoopChecklistItem,
    MeetingActionItem,
    MeetingParticipant,
    MeetingTranscript,
    OrgMember,
    Organization,
    Project,
    Reminder,
    Session,
    Ticket,
    User,
)
from sessionfs.server.services import attention as svc

pytestmark = pytest.mark.asyncio

NOW = datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


async def _user(db: AsyncSession, name: str = "alice", tier: str = "team") -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier=tier,
        email_verified=True,
        created_at=NOW,
    )
    db.add(user)
    await db.commit()
    raw = generate_api_key()
    db.add(ApiKey(id=str(uuid.uuid4()), user_id=user.id, key_hash=hash_api_key(raw),
                  name=f"{name}-key", created_at=NOW))
    await db.commit()
    return user, raw


async def _service_key(db: AsyncSession, user: User, org: Organization, *,
                       scopes=("attention:read", "tickets:read", "reminders:read",
                               "agent_runs:read", "knowledge:read", "meetings:read"),
                       project_ids=None) -> str:
    raw = generate_api_key()
    db.add(ApiKey(
        id=str(uuid.uuid4()), user_id=user.id, key_hash=hash_api_key(raw), name="svc",
        is_active=True, key_kind="service", org_id=org.id, scopes=json.dumps(list(scopes)),
        created_by_user_id=user.id, service_key_name="svc",
        project_ids=json.dumps(project_ids) if project_ids is not None else None,
        created_at=NOW,
    ))
    await db.commit()
    return raw


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _org(db: AsyncSession, tier: str = "team") -> Organization:
    org = Organization(id=_id("org"), name="Org", slug=f"o-{uuid.uuid4().hex[:8]}",
                       tier=tier, seats_limit=20, settings="{}")
    db.add(org)
    await db.commit()
    return org


async def _member(db: AsyncSession, org: Organization, user: User, role: str = "member") -> None:
    db.add(OrgMember(org_id=org.id, user_id=user.id, role=role))
    await db.commit()


async def _project(db: AsyncSession, owner: User, org: Organization | None = None) -> Project:
    p = Project(id=_id("proj"), name=f"p-{uuid.uuid4().hex[:6]}",
                git_remote_normalized=f"acme/{uuid.uuid4().hex[:8]}", context_document="",
                owner_id=owner.id, org_id=org.id if org else None)
    db.add(p)
    await db.commit()
    return p


async def _ticket(db: AsyncSession, project: Project, creator: User, *, assigned_user_id=None,
                  assigned_to=None, status="open", priority="medium", due=None,
                  title="Fix the login flow") -> Ticket:
    t = Ticket(id=_id("tk"), project_id=project.id, title=title, kind="task", status=status,
               priority=priority, created_by_user_id=creator.id, assigned_user_id=assigned_user_id,
               assigned_to=assigned_to, due_date=due, created_at=NOW, updated_at=NOW)
    db.add(t)
    await db.commit()
    return t


async def _reminder(db: AsyncSession, owner: User, *, due: datetime, title="Call Bob",
                    source: tuple[str, str] | None = None, project_id=None) -> Reminder:
    r = Reminder(id=_id("rem"), owner_user_id=owner.id, created_by_user_id=owner.id,
                 title=title, due_at=due, original_due_at=due, status="open",
                 source_kind=source[0] if source else None, source_id=source[1] if source else None,
                 project_id=project_id, created_at=NOW, updated_at=NOW)
    db.add(r)
    await db.commit()
    return r


async def _meeting_action(db: AsyncSession, project: Project, owner: User, *,
                          description="Send the notes", due=None) -> MeetingActionItem:
    content = "Alice: I'll send the notes\nBob: ok"
    m = MeetingTranscript(id=_id("mtg"), org_id=project.org_id, project_id=project.id,
                          title="Weekly sync", source="manual_paste", content=content,
                          content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                          line_count=2, created_by_user_id=owner.id)
    db.add(m)
    await db.flush()
    part = MeetingParticipant(id=_id("mp"), transcript_id=m.id, name_raw="Alice",
                              user_id=owner.id, resolution_method="manual")
    db.add(part)
    await db.flush()
    item = MeetingActionItem(id=_id("mai"), transcript_id=m.id, kind="action",
                             description=description, owner_participant_id=part.id,
                             due_date=due, line_start=1, line_end=1, status="pending",
                             extracted_at=NOW)
    db.add(item)
    await db.commit()
    return item


async def _knowledge(db: AsyncSession, project: Project, author: User, entry_type="decision",
                     content="We use Postgres for the login store") -> KnowledgeEntry:
    k = KnowledgeEntry(project_id=project.id, session_id="manual", user_id=author.id,
                       entry_type=entry_type, content=content, created_at=NOW)
    db.add(k)
    await db.commit()
    return k


async def _caught_run(db: AsyncSession, project: Project, ticket: Ticket, *, tokens=1000,
                      cost=None) -> AgentRun:
    run = AgentRun(
        id=_id("run"), project_id=project.id, persona_name="atlas", role="reviewer",
        ticket_id=ticket.id, input_tokens=tokens, created_at=NOW,
        cost_usd=cost, cost_basis="reported" if cost is not None else None,
        findings=json.dumps([{"title": "SQL injection", "harm": "security", "status": "fixed"}]),
    )
    db.add(run)
    await db.commit()
    return run


async def _session_in(db: AsyncSession, user: User, project: Project, title="Login flow work",
                      ticket_id=None) -> Session:
    s = Session(id=_id("ses"), user_id=user.id, title=title, tags="[]", source_tool="claude-code",
                blob_key="x", blob_size_bytes=0, etag="x", project_id=project.id,
                git_remote_normalized=project.git_remote_normalized, ticket_id=ticket_id,
                created_at=NOW, updated_at=NOW, uploaded_at=NOW)
    db.add(s)
    await db.commit()
    return s


async def _feed(client: AsyncClient, key: str, **params) -> dict:
    resp = await client.get("/api/v1/me/for-you", headers=_h(key), params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _kinds(body: dict) -> list[str]:
    return [i["kind"] for i in body["items"]]


# ── ranking ──


def _item(**kw) -> svc.Item:
    base = dict(kind="ticket", source_type="ticket", source_id=uuid.uuid4().hex, band="your_turn",
                involvement="assignee", title="t", why="w", link="/", version_key="v")
    base.update(kw)
    return svc.Item(**base)


async def test_ranking_is_band_involvement_urgency_recency():
    later = NOW + timedelta(days=3)
    items = [
        _item(source_id="lately", band="lately", involvement="member"),
        _item(source_id="watcher", involvement="watcher"),
        _item(source_id="plain-old", occurred_at=NOW - timedelta(days=2)),
        _item(source_id="plain-new", occurred_at=NOW),
        _item(source_id="low", priority="low"),
        _item(source_id="high", priority="high"),
        _item(source_id="harm", harm=True),
        _item(source_id="due-later", due_at=later),
        _item(source_id="due-soon", due_at=NOW + timedelta(days=1)),
        _item(source_id="overdue", overdue=True, due_at=NOW - timedelta(days=1)),
        _item(source_id="now", band="now", involvement="owner"),
    ]
    order = [i.source_id for i in sorted(items, key=svc.Item.sort_key)]
    assert order[0] == "now"
    assert order[1] == "overdue"
    assert order[2:4] == ["due-soon", "due-later"]
    assert order[4] == "harm"
    assert order[5:7] == ["high", "low"]
    assert order[7:9] == ["plain-new", "plain-old"]
    assert order[-2:] == ["watcher", "lately"]


# ── providers end to end ──


async def test_feed_lists_every_source_and_ranks_bands(client: AsyncClient, db_session: AsyncSession):
    me, key = await _user(db_session)
    other, _ = await _user(db_session, "bob")
    org = await _org(db_session)
    await _member(db_session, org, me)
    await _member(db_session, org, other)
    p = await _project(db_session, me)
    op = await _project(db_session, other, org)
    db_session.add(AgentPersona(id=_id("per"), org_id=org.id, name="atlas", role="Backend",
                                created_by=other.id, represented_user_id=me.id, is_active=True))
    await db_session.commit()

    direct = await _ticket(db_session, p, me, assigned_user_id=me.id, priority="high",
                           due=NOW.date() - timedelta(days=1))
    via_persona = await _ticket(db_session, op, other, assigned_to="Atlas")
    await _ticket(db_session, p, me, assigned_user_id=other.id)  # not mine
    await _ticket(db_session, p, me, assigned_user_id=me.id, status="done")  # not actionable
    await _reminder(db_session, me, due=NOW - timedelta(hours=1))
    await _reminder(db_session, me, due=NOW + timedelta(days=5), title="later")  # coming up
    await _meeting_action(db_session, p, me)
    await _session_in(db_session, me, op)  # "works in" the org project
    await _knowledge(db_session, op, other)
    await _caught_run(db_session, p, direct)

    body = await _feed(client, key)
    kinds = _kinds(body)
    assert kinds[0] == "reminder"
    assert set(kinds) == {"reminder", "ticket", "meeting_action", "knowledge_changes", "proof"}
    tickets = [i for i in body["items"] if i["kind"] == "ticket"]
    assert {i["source"]["id"] for i in tickets} == {direct.id, via_persona.id}
    # The overdue high-priority direct ticket ranks before the persona one.
    assert tickets[0]["source"]["id"] == direct.id
    assert "Assigned to you" in tickets[0]["why"] and "overdue" in tickets[0]["why"]
    assert "(you)" in tickets[1]["why"]
    assert tickets[0]["link"].endswith(f"ticket={direct.id}")
    assert [i["band"] for i in body["items"]][-2:] == ["lately", "lately"]
    assert body["counts"] == {
        "now": 1, "your_turn": 3, "upcoming": 1, "lately": 2, "total": 7, "actionable": 3,
    }
    assert body["sections"]["tickets"] == {"count": 2, "error": None}
    assert body["sections"]["approvals"] == {"count": 0, "error": None}
    assert all(i["item_key"].startswith("fy_") and direct.id not in i["item_key"]
               for i in body["items"])

    only_now = await _feed(client, key, band="now")
    assert _kinds(only_now) == ["reminder"]
    assert only_now["counts"]["total"] == 7

    count = await client.get("/api/v1/me/for-you/count", headers=_h(key))
    assert count.json() == {
        "now": 1, "your_turn": 3, "upcoming": 1, "lately": 2, "total": 7, "actionable": 3,
    }


async def test_cards_disappear_when_access_is_lost(client: AsyncClient, db_session: AsyncSession):
    me, key = await _user(db_session)
    owner, _ = await _user(db_session, "olga")
    org = await _org(db_session)
    await _member(db_session, org, owner, "owner")
    await _member(db_session, org, me)
    p = await _project(db_session, owner, org)
    await _ticket(db_session, p, owner, assigned_user_id=me.id)
    await _meeting_action_for(db_session, p, owner, me)
    await _session_in(db_session, me, p)
    await _knowledge(db_session, p, owner)
    rem_ticket = await _ticket(db_session, p, owner, title="source")
    db_session.add(Reminder(id=_id("rem"), owner_user_id=me.id, created_by_user_id=owner.id,
                            title="from olga", due_at=NOW - timedelta(minutes=5),
                            original_due_at=NOW, status="open", source_kind="ticket",
                            source_id=rem_ticket.id, project_id=p.id))
    await db_session.commit()

    before = _kinds(await _feed(client, key))
    assert {"ticket", "meeting_action", "knowledge_changes", "reminder"} <= set(before)

    # Removed from the org: every card from that project goes. (The captured
    # session would still admit me as a reader, so drop it too.)
    await db_session.execute(
        OrgMember.__table__.delete().where(OrgMember.user_id == me.id)
    )
    await db_session.execute(Session.__table__.delete().where(Session.user_id == me.id))
    await db_session.commit()
    after = await _feed(client, key)
    assert after["items"] == []
    assert after["counts"]["total"] == 0


async def _meeting_action_for(db: AsyncSession, project: Project, creator: User, owner: User):
    item = await _meeting_action(db, project, creator)
    part = (await db.execute(select(MeetingParticipant).where(
        MeetingParticipant.id == item.owner_participant_id))).scalar_one()
    part.user_id = owner.id
    await db.commit()
    return item


async def test_ticket_plan_gate(client: AsyncClient, db_session: AsyncSession):
    """Tickets are on every plan: a Free user's own tickets surface."""
    me, key = await _user(db_session, tier="free")
    p = await _project(db_session, me)
    await _ticket(db_session, p, me, assigned_user_id=me.id)
    await _meeting_action(db_session, p, me)
    kinds = _kinds(await _feed(client, key))
    assert "ticket" in kinds and "meeting_action" in kinds


# ── hides ──


async def test_hide_until_change_and_no_oracle(client: AsyncClient, db_session: AsyncSession):
    me, key = await _user(db_session)
    stranger, skey = await _user(db_session, "eve")
    p = await _project(db_session, me)
    t = await _ticket(db_session, p, me, assigned_user_id=me.id)
    body = await _feed(client, key)
    card = next(i for i in body["items"] if i["kind"] == "ticket")

    # A stranger hiding my key, and anyone hiding a made-up key: same 204,
    # nothing written.
    for k, item_key in ((skey, card["item_key"]), (key, "fy_" + "0" * 40), (key, "nope")):
        resp = await client.post("/api/v1/me/for-you/hide", headers=_h(k),
                                 json={"item_key": item_key})
        assert resp.status_code == 204
    assert (await db_session.execute(select(func.count()).select_from(AttentionHide))).scalar() == 0

    resp = await client.post("/api/v1/me/for-you/hide", headers=_h(key),
                             json={"item_key": card["item_key"]})
    assert resp.status_code == 204
    resp = await client.post("/api/v1/me/for-you/hide", headers=_h(key),
                             json={"item_key": card["item_key"]})  # idempotent
    assert resp.status_code == 204
    hidden = await _feed(client, key)
    assert "ticket" not in _kinds(hidden)
    assert hidden["counts"]["total"] == 0

    # The source changes -> the card is back.
    t.status = "in_progress"
    t.updated_at = NOW + timedelta(minutes=1)
    await db_session.commit()
    assert "ticket" in _kinds(await _feed(client, key))


async def test_snooze_until_expires(client: AsyncClient, db_session: AsyncSession):
    me, key = await _user(db_session)
    p = await _project(db_session, me)
    await _ticket(db_session, p, me, assigned_user_id=me.id)
    card = (await _feed(client, key))["items"][0]
    resp = await client.post("/api/v1/me/for-you/hide", headers=_h(key), json={
        "item_key": card["item_key"], "until": (NOW - timedelta(hours=1)).isoformat()})
    assert resp.status_code == 422
    resp = await client.post("/api/v1/me/for-you/hide", headers=_h(key), json={
        "item_key": card["item_key"], "until": (NOW + timedelta(hours=1)).isoformat()})
    assert resp.status_code == 204
    assert (await _feed(client, key))["items"] == []
    row = (await db_session.execute(select(AttentionHide))).scalar_one()
    row.hidden_until = NOW - timedelta(seconds=1)
    await db_session.commit()
    assert len((await _feed(client, key))["items"]) == 1


async def test_hide_needs_a_user_key(client: AsyncClient, db_session: AsyncSession):
    me, _ = await _user(db_session)
    org = await _org(db_session)
    await _member(db_session, org, me)
    raw = await _service_key(db_session, me, org)
    resp = await client.post("/api/v1/me/for-you/hide", headers=_h(raw), json={"item_key": "fy_x"})
    assert resp.status_code == 403


async def test_sweep_removes_old_hides(db_session: AsyncSession):
    me, _ = await _user(db_session)
    db_session.add_all([
        AttentionHide(user_id=me.id, item_key="fy_old", version_key="v",
                      created_at=NOW - timedelta(days=91)),
        AttentionHide(user_id=me.id, item_key="fy_new", version_key="v", created_at=NOW),
    ])
    await db_session.commit()
    result = await svc.sweep_attention_hides(db_session, batch_size=1)
    assert result["deleted"] == 1
    keys = (await db_session.execute(select(AttentionHide.item_key))).scalars().all()
    assert keys == ["fy_new"]


# ── ETag ──


async def test_etag_304_until_the_feed_changes(client: AsyncClient, db_session: AsyncSession):
    me, key = await _user(db_session)
    p = await _project(db_session, me)
    await _ticket(db_session, p, me, assigned_user_id=me.id)
    first = await client.get("/api/v1/me/for-you", headers=_h(key))
    etag = first.headers["ETag"]
    again = await client.get("/api/v1/me/for-you", headers={**_h(key), "If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers["ETag"] == etag
    await _ticket(db_session, p, me, assigned_user_id=me.id, title="Another one")
    changed = await client.get("/api/v1/me/for-you", headers={**_h(key), "If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.headers["ETag"] != etag


# ── degraded providers ──


async def test_slow_or_broken_provider_is_a_section_error(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    me, key = await _user(db_session)
    p = await _project(db_session, me)
    await _ticket(db_session, p, me, assigned_user_id=me.id)

    async def slow(ctx):
        await asyncio.sleep(5)
        return [], 0

    async def broken(ctx):
        raise RuntimeError("boom")

    providers = [(n, f) for n, f in svc.PROVIDERS]
    providers = [(n, slow if n == "knowledge_changes" else broken if n == "proof" else f)
                 for n, f in providers]
    monkeypatch.setattr(svc, "PROVIDERS", providers)
    # Long enough that no real (in-memory SQLite) query is ever cancelled:
    # cancelling one mid-query would invalidate the shared test connection.
    monkeypatch.setattr(svc, "PROVIDER_TIMEOUT_S", 1.0)
    body = await _feed(client, key)
    assert body["sections"]["knowledge_changes"] == {"count": 0, "error": "couldn't load"}
    assert body["sections"]["proof"] == {"count": 0, "error": "couldn't load"}
    assert _kinds(body) == ["ticket"]


# ── service keys ──


async def test_service_key_gets_minter_feed_inside_org_and_allowlist(
    client: AsyncClient, db_session: AsyncSession
):
    me, key = await _user(db_session)
    org = await _org(db_session)
    other_org = await _org(db_session)
    await _member(db_session, org, me)
    await _member(db_session, other_org, me)
    personal = await _project(db_session, me)
    a = await _project(db_session, me, org)
    b = await _project(db_session, me, org)
    c = await _project(db_session, me, other_org)
    ids = {}
    for proj in (personal, a, b, c):
        ids[proj.id] = (await _ticket(db_session, proj, me, assigned_user_id=me.id)).id
    await _reminder(db_session, me, due=NOW - timedelta(minutes=1))  # no source
    await _reminder(db_session, me, due=NOW - timedelta(minutes=1), title="on a",
                    source=("ticket", ids[a.id]), project_id=a.id)

    user_ids = {i["source"]["id"] for i in (await _feed(client, key))["items"] if i["kind"] == "ticket"}
    assert user_ids == set(ids.values())

    svc_key = await _service_key(db_session, me, org)
    body = await _feed(client, svc_key)
    assert {i["source"]["id"] for i in body["items"] if i["kind"] == "ticket"} == {ids[a.id], ids[b.id]}
    reminders = [i for i in body["items"] if i["kind"] == "reminder"]
    assert [r["title"] for r in reminders] == ["on a"]

    allow = await _service_key(db_session, me, org, project_ids=[b.id])
    body = await _feed(client, allow)
    assert {i["source"]["id"] for i in body["items"]} == {ids[b.id]}

    no_scope = await _service_key(db_session, me, org, scopes=("reminders:read",))
    resp = await client.get("/api/v1/me/for-you", headers=_h(no_scope))
    assert resp.status_code == 403

    # attention:read alone: sections whose content needs another scope are skipped.
    bare = await _service_key(db_session, me, org, scopes=("attention:read",))
    body = await _feed(client, bare)
    assert body["items"] == []
    assert "tickets" not in body["sections"] and "reminders" not in body["sections"]


# ── proof dollars ──


async def test_proof_dollars_only_for_org_admins(client: AsyncClient, db_session: AsyncSession):
    admin, admin_key = await _user(db_session, "ada")
    member, member_key = await _user(db_session, "max")
    org = await _org(db_session)
    await _member(db_session, org, admin, "admin")
    await _member(db_session, org, member)
    p = await _project(db_session, admin, org)
    for user in (admin, member):
        t = await _ticket(db_session, p, admin, assigned_user_id=user.id, status="done")
        t.resolved_at = NOW
        await db_session.commit()
        await _caught_run(db_session, p, t, tokens=400_000, cost=2.5)

    def proof(body):
        return next(i for i in body["items"] if i["kind"] == "proof")

    a = proof(await _feed(client, admin_key))
    assert a["detail"]["caught"] == 1 and a["detail"]["accepted_changes"] == 1
    assert a["detail"]["cost_usd_per_accepted_change"] == 2.5
    assert "$2.50" in a["why"] and "0.4M tokens" in a["why"]

    m = proof(await _feed(client, member_key))
    assert "cost_usd_per_accepted_change" not in m["detail"]
    assert "$" not in m["why"]

    svc_key = await _service_key(db_session, admin, org)
    s = proof(await _feed(client, svc_key))
    assert "cost_usd_per_accepted_change" not in s["detail"] and "$" not in s["why"]


# ── context ──


async def test_context_for_ticket_card_rechecks_access(client: AsyncClient, db_session: AsyncSession):
    me, key = await _user(db_session)
    owner, _ = await _user(db_session, "olga")
    org = await _org(db_session)
    await _member(db_session, org, owner, "owner")
    await _member(db_session, org, me)
    p = await _project(db_session, owner, org)
    t = await _ticket(db_session, p, owner, assigned_user_id=me.id, title="Fix the login flow")
    await _knowledge(db_session, p, owner, content="The login flow uses PKCE")
    await _knowledge(db_session, p, owner, content="Unrelated billing note")
    s = await _session_in(db_session, me, p, title="Login flow spike")
    await _meeting_action(db_session, p, me)

    body = await _feed(client, key)
    tcard = next(i for i in body["items"] if i["kind"] == "ticket")
    mcard = next(i for i in body["items"] if i["kind"] == "meeting_action")
    resp = await client.get(f"/api/v1/me/for-you/items/{tcard['item_key']}/context", headers=_h(key))
    assert resp.status_code == 200, resp.text
    ctx = resp.json()
    assert ctx["ticket"]["id"] == t.id
    assert [k["content"] for k in ctx["knowledge"]] == ["The login flow uses PKCE"]
    assert [x["id"] for x in ctx["sessions"]] == [s.id]

    miss = await client.get(f"/api/v1/me/for-you/items/{mcard['item_key']}/context", headers=_h(key))
    unknown = await client.get("/api/v1/me/for-you/items/fy_nope/context", headers=_h(key))
    assert miss.status_code == unknown.status_code == 404
    assert miss.json() == unknown.json()

    await db_session.execute(OrgMember.__table__.delete().where(OrgMember.user_id == me.id))
    await db_session.execute(Session.__table__.delete().where(Session.user_id == me.id))
    await db_session.commit()
    gone = await client.get(f"/api/v1/me/for-you/items/{tcard['item_key']}/context", headers=_h(key))
    assert gone.status_code == 404 and gone.json() == unknown.json()


# ── performance ──


async def _seed_everything(db: AsyncSession, me: User, n: int) -> None:
    org = await _org(db)
    await _member(db, org, me, "admin")
    db.add(AgentPersona(id=_id("per"), org_id=org.id, name="atlas", role="Backend",
                        created_by=me.id, represented_user_id=me.id, is_active=True))
    await db.commit()
    other, _ = await _user(db, "other")
    for _ in range(n):
        p = await _project(db, me, org)
        await _session_in(db, me, p)
        t = await _ticket(db, p, me, assigned_user_id=me.id, due=date.today())
        await _ticket(db, p, me, assigned_to="atlas")
        done = await _ticket(db, p, me, assigned_user_id=me.id, status="done")
        done.resolved_at = NOW
        await db.commit()
        await _caught_run(db, p, done, cost=1.0)
        await _reminder(db, me, due=NOW - timedelta(minutes=1), source=("ticket", t.id),
                        project_id=p.id)
        await _meeting_action(db, p, me, due=date.today())
        await _knowledge(db, p, other)
        await _harmful_loop_child(db, p, me, other)
        db.add(LoopChecklistItem(id=_id("lci"), project_id=p.id, slug=f"s-{uuid.uuid4().hex[:6]}",
                                 title="A trap", check_text="Check it.", status="proposed",
                                 source="manual", times_found=0, created_at=NOW))
        await db.commit()


async def test_no_n_plus_one_with_every_provider_active(db_engine, db_session: AsyncSession):
    one, _ = await _user(db_session, "one")
    three, _ = await _user(db_session, "three")
    await _seed_everything(db_session, one, 1)
    await _seed_everything(db_session, three, 3)

    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", _capture)
    try:
        f1 = await svc.build_feed(db_session, svc.AttentionScope(user_id=one.id))
        n1 = len(captured)
        captured.clear()
        f3 = await svc.build_feed(db_session, svc.AttentionScope(user_id=three.id))
        n3 = len(captured)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _capture)
    kinds = {i.kind for i in f3.items}
    assert {"ticket", "reminder", "meeting_action", "knowledge_changes", "proof",
            "loop_harmful_findings", "loop_checklist"} <= kinds
    assert len(f3.items) > len(f1.items)
    # Every provider is grouped: 3x the projects and items, same statements.
    assert n1 == n3, (n1, n3)


async def test_feed_latency_on_a_seeded_dataset(db_session: AsyncSession):
    me, _ = await _user(db_session)
    await _seed_everything(db_session, me, 15)
    timings = []
    for _ in range(5):
        started = time.perf_counter()
        feed = await svc.build_feed(db_session, svc.AttentionScope(user_id=me.id))
        timings.append(time.perf_counter() - started)
    assert len(feed.items) >= 60
    # p95 target is 300 ms on PG; SQLite in CI gets a generous bound.
    assert sorted(timings)[-1] < 3.0, timings


# ── harmful open findings (Now) ──

HARMFUL = (
    "Codex R1 review on tk_x: VERIFIED-CLEAN\n\n"
    "Findings:\n\n • HIGH — crash risk in src/foo.py:10 harm: crash\n"
)


async def _harmful_loop_child(db: AsyncSession, p: Project, me: User, reviewer: User) -> Ticket:
    from sessionfs.server.db.models import TicketComment

    parent = Ticket(id=_id("tk"), project_id=p.id, title="loop", kind="issue", status="open",
                    created_by_user_id=me.id, loop_config=json.dumps({"reviewer_persona": "r"}),
                    created_at=NOW, updated_at=NOW)
    db.add(parent)
    await db.flush()
    child = Ticket(id=_id("tk"), project_id=p.id, title="Harden the parser", kind="task",
                   status="in_progress", parent_ticket_id=parent.id, created_by_user_id=me.id,
                   assigned_user_id=me.id, created_at=NOW, updated_at=NOW)
    db.add(child)
    await db.commit()
    db.add(TicketComment(id=_id("tc"), ticket_id=child.id, author_user_id=reviewer.id,
                         author_persona="codex-reviewer", content=HARMFUL, verdict_trusted=True,
                         reviewed_commit="a1b2c3d4e5f6a7b8", created_at=NOW))
    await db.commit()
    return child


async def test_harmful_open_findings_on_my_loop_child_are_now(
    client: AsyncClient, db_session: AsyncSession
):
    from sessionfs.server.db.models import TicketComment

    me, key = await _user(db_session)
    reviewer, _ = await _user(db_session, "rev")
    p = await _project(db_session, me)
    parent = Ticket(id=_id("tk"), project_id=p.id, title="loop", kind="issue", status="open",
                    created_by_user_id=me.id, loop_config=json.dumps({"reviewer_persona": "r"}),
                    created_at=NOW, updated_at=NOW)
    db_session.add(parent)
    await db_session.flush()
    child = Ticket(id=_id("tk"), project_id=p.id, title="Harden the parser", kind="task",
                   status="in_progress", parent_ticket_id=parent.id, created_by_user_id=me.id,
                   assigned_user_id=me.id, created_at=NOW, updated_at=NOW)
    db_session.add(child)
    await db_session.commit()
    db_session.add(TicketComment(id=_id("tc"), ticket_id=child.id, author_user_id=reviewer.id,
                                 author_persona="codex-reviewer", content=HARMFUL,
                                 verdict_trusted=True, reviewed_commit="a1b2c3d4e5f6a7b8",
                                 created_at=NOW))
    await db_session.commit()

    body = await _feed(client, key)
    harm = [i for i in body["items"] if i["kind"] == "loop_harmful_findings"]
    assert len(harm) == 1 and harm[0]["band"] == "now"
    assert harm[0]["source"]["id"] == child.id
    assert "1 harmful open finding" in harm[0]["why"]
    assert body["items"][0]["kind"] == "loop_harmful_findings"

    child.assigned_user_id = reviewer.id
    await db_session.commit()
    body = await _feed(client, key)
    assert not [i for i in body["items"] if i["kind"] == "loop_harmful_findings"]

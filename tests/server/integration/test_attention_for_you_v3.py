"""For You v3 (tk_dc118ffd6e804a9a, F2 backend) — the providers and routes the
dashboard home needs on top of F1:

- meeting suggestions: owned or unowned-but-attended pending items, with the
  cited quote, speaker, suggestion wording and destination;
- "Your team is asking": unanswered questions and @mentions on your tickets;
- handoffs (waiting, then worth a second look);
- coming-up reminders, display groups, rank explanations and the chyron;
- "Working with others": review and dependency flows, collaborators, and the
  remind route (recipient resolved server-side, both people must read the
  source ticket, DLP on the message, service keys refused).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentRun,
    Handoff,
    MeetingActionItem,
    MeetingParticipant,
    MeetingTranscript,
    Reminder,
    ReminderNote,
    Session,
    TicketComment,
    TicketDependency,
)
from tests.server.integration.test_attention_feed import (
    NOW,
    _feed,
    _h,
    _id,
    _member,
    _org,
    _project,
    _reminder,
    _service_key,
    _ticket,
    _user,
)

pytestmark = pytest.mark.asyncio


def _by_kind(body: dict, kind: str) -> list[dict]:
    return [i for i in body["items"] if i["kind"] == kind]


async def _meeting(db: AsyncSession, project, uploader, content: str, *, group_id=None):
    m = MeetingTranscript(
        id=_id("mtg"), org_id=project.org_id if project else None,
        project_id=project.id if project else None, group_id=group_id,
        title="Release sync", source="manual_paste", content=content,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        line_count=content.count("\n") + 1, created_by_user_id=uploader.id,
    )
    db.add(m)
    await db.flush()
    return m


async def _participant(db, meeting, user=None, name="Kemi"):
    p = MeetingParticipant(id=_id("mp"), transcript_id=meeting.id, name_raw=name,
                           user_id=user.id if user else None, resolution_method="manual")
    db.add(p)
    await db.flush()
    return p


async def _item(db, meeting, owner_part=None, *, line=1, due=None, description="Write the upgrade note"):
    item = MeetingActionItem(id=_id("mai"), transcript_id=meeting.id, kind="action",
                             description=description,
                             owner_participant_id=owner_part.id if owner_part else None,
                             due_date=due, line_start=line, line_end=line, status="pending",
                             extracted_at=NOW)
    db.add(item)
    await db.commit()
    return item


# ── meeting suggestions ──


async def test_meeting_suggestions_owned_and_attended(client: AsyncClient, db_session: AsyncSession):
    me, key = await _user(db_session)
    kemi, _ = await _user(db_session, "kemi")
    p = await _project(db_session, me)
    content = "Kemi: Ola, can you write the upgrade note before Monday?\nOla: yes\nDara: we should backfill the locale"
    m = await _meeting(db_session, p, me, content)
    mine = await _participant(db_session, m, me, "Ola")
    kp = await _participant(db_session, m, kemi, "Kemi")
    owned = await _item(db_session, m, mine, line=1, due=NOW.date() + timedelta(days=3))
    unowned = await _item(db_session, m, None, line=3, description="Backfill the locale")
    await _item(db_session, m, kp, line=2, description="Kemi's own task")  # someone else's

    body = await _feed(client, key)
    assert {i["source"]["id"] for i in _by_kind(body, "meeting_action")} == {owned.id}
    assert {i["source"]["id"] for i in _by_kind(body, "meeting_suggestion")} == {unowned.id}
    card = _by_kind(body, "meeting_action")[0]
    assert card["group"] == "meetings"
    assert card["actions"] == ["promote", "remind", "dismiss"]
    ctx = card["context"]
    assert ctx["speaker"] == "Kemi"
    assert "upgrade note" in ctx["quote"]["text"]
    assert ctx["quote"]["by"].endswith("Kemi")
    assert ctx["suggest"]["level"] == 3 and ctx["suggest"]["owner"] == "You"
    assert ctx["suggest"]["destination"] == {"kind": "project", "id": p.id, "name": p.name}
    assert ctx["meeting"]["id"] == m.id and ctx["meeting"]["item_id"] == owned.id
    sugg = _by_kind(body, "meeting_suggestion")[0]["context"]["suggest"]
    assert sugg["level"] == 1 and sugg["wording"] == "Might be an action for you"
    assert body["groups"]["meetings"] == 2


async def test_unowned_items_need_attendance(client: AsyncClient, db_session: AsyncSession):
    me, key = await _user(db_session)
    p = await _project(db_session, me)
    m = await _meeting(db_session, p, me, "Dara: someone should do this")
    await _participant(db_session, m, None, "Dara")
    await _item(db_session, m, None)
    body = await _feed(client, key)
    assert not _by_kind(body, "meeting_suggestion")


# ── your team is asking ──


async def _comment(db, ticket, author, content, *, at=None, persona=None, trusted=False):
    c = TicketComment(id=_id("cmt"), ticket_id=ticket.id, author_user_id=author.id,
                      author_persona=persona, content=content, created_at=at or NOW,
                      verdict_trusted=trusted)
    db.add(c)
    await db.commit()
    return c


async def test_questions_until_answered(client: AsyncClient, db_session: AsyncSession):
    me, key = await _user(db_session)
    tolu, _ = await _user(db_session, "tolu")
    org = await _org(db_session)
    await _member(db_session, org, me)
    await _member(db_session, org, tolu)
    p = await _project(db_session, me, org)
    t = await _ticket(db_session, p, me, assigned_user_id=me.id, title="Throttle rule")
    await _comment(db_session, t, tolu, "Should the delivery path share the tier?",
                   at=NOW - timedelta(hours=2))
    await _comment(db_session, t, tolu, "Codex R1 review on x: VERIFIED-CLEAN?", persona="codex-reviewer")

    body = await _feed(client, key)
    qs = _by_kind(body, "ticket_question")
    assert len(qs) == 1 and qs[0]["group"] == "asking"
    assert qs[0]["context"]["reply"] == {"project_id": p.id, "ticket_id": t.id, "to": "Tolu"}
    assert "share the tier" in qs[0]["context"]["quote"]["text"]
    assert body["counts"]["actionable"] >= 2  # the ticket + the question

    await _comment(db_session, t, me, "Its own tier, please.", at=NOW)
    body = await _feed(client, key)
    assert not _by_kind(body, "ticket_question")


async def test_mention_without_question_mark(client: AsyncClient, db_session: AsyncSession):
    me, key = await _user(db_session, "ola")
    kemi, _ = await _user(db_session, "kemi")
    p = await _project(db_session, me)
    t = await _ticket(db_session, p, me, assigned_user_id=me.id)
    await _comment(db_session, t, kemi, "@ola heads up, the cert renews on the 9th.")
    await _comment(db_session, t, kemi, "Just an update.", at=NOW - timedelta(minutes=5))
    body = await _feed(client, key)
    qs = _by_kind(body, "ticket_question")
    assert len(qs) == 1 and "mentioned you" in qs[0]["title"]


# ── handoffs ──


async def test_handoffs_waiting_then_second_look(client: AsyncClient, db_session: AsyncSession):
    me, key = await _user(db_session)
    dara, _ = await _user(db_session, "dara")
    p = await _project(db_session, dara)
    sess = Session(id=_id("ses"), user_id=dara.id, title="x", tags="[]", source_tool="claude-code",
                   blob_key="x", blob_size_bytes=0, etag="x", project_id=p.id,
                   git_remote_normalized=p.git_remote_normalized,
                   created_at=NOW, updated_at=NOW, uploaded_at=NOW)
    db_session.add(sess)
    await db_session.commit()
    fresh = Handoff(id=_id("h")[:20], session_id=sess.id, sender_id=dara.id, recipient_user_id=me.id,
                    message="The session has the repro.", status="pending", created_at=NOW,
                    expires_at=NOW + timedelta(days=5), snapshot_title="merge-check fallback")
    old = Handoff(id=_id("g")[:20], session_id=sess.id, sender_id=dara.id,
                  recipient_email=me.email, recipient_email_normalized=me.email.lower(),
                  status="pending", created_at=NOW - timedelta(days=4),
                  expires_at=NOW + timedelta(days=3))
    gone = Handoff(id=_id("e")[:20], session_id=sess.id, sender_id=dara.id, recipient_user_id=me.id,
                   status="pending", created_at=NOW - timedelta(days=9),
                   expires_at=NOW - timedelta(days=1))
    db_session.add_all([fresh, old, gone])
    await db_session.commit()
    body = await _feed(client, key)
    cards = {i["source"]["id"]: i for i in _by_kind(body, "handoff")}
    assert set(cards) == {fresh.id, old.id}
    assert cards[fresh.id]["group"] == "waiting"
    assert cards[old.id]["group"] == "second" and cards[old.id]["context"]["quiet_days"] == 4
    assert cards[fresh.id]["context"]["quote"]["text"] == "The session has the repro."


# ── reminders, groups, rank, chyron ──


async def test_reminder_groups_and_rank(client: AsyncClient, db_session: AsyncSession):
    me, key = await _user(db_session)
    kemi, _ = await _user(db_session, "kemi")
    await _reminder(db_session, me, due=NOW - timedelta(minutes=10), title="Book Tolu")
    await _reminder(db_session, me, due=NOW + timedelta(days=3), title="Renew the cert")
    await _reminder(db_session, me, due=NOW + timedelta(days=20), title="far away")
    nudge = Reminder(id=_id("rem"), owner_user_id=me.id, created_by_user_id=kemi.id, title="Review PR",
                     due_at=NOW - timedelta(minutes=1), original_due_at=NOW, status="open",
                     created_at=NOW, updated_at=NOW)
    db_session.add(nudge)
    await db_session.commit()
    body = await _feed(client, key)
    rem = {i["title"]: i for i in _by_kind(body, "reminder")}
    assert set(rem) == {"Book Tolu", "Renew the cert", "Review PR"}  # not the one 20 days out
    assert rem["Review PR"]["group"] == "asking"  # a teammate's nudge
    assert "from Kemi" in rem["Review PR"]["why"]
    assert rem["Book Tolu"]["group"] == "radar" and rem["Book Tolu"]["band"] == "now"
    assert rem["Renew the cert"]["group"] == "coming" and rem["Renew the cert"]["band"] == "upcoming"
    assert body["groups"]["coming"] == 1 and body["groups"]["radar"] == 1
    first = body["items"][0]
    assert [r["factor"] for r in first["rank"]] == ["band", "involvement", "urgency", "recency"]
    assert sum(1 for r in first["rank"] if r.get("decides")) == 1
    assert [c["item_key"] for c in body["chyron"]][:2] == [i["item_key"] for i in body["items"][:2]]
    assert body["chyron"][0]["category"] == "urgent"


# ── working with others ──


async def _team(db: AsyncSession):
    me, key = await _user(db, "ola")
    kemi, kemi_key = await _user(db, "kemi")
    dara, _ = await _user(db, "dara")
    org = await _org(db)
    for u in (me, kemi, dara):
        await _member(db, org, u)
    p = await _project(db, me, org)
    return me, key, kemi, kemi_key, dara, org, p


async def test_collaboration_flows(client: AsyncClient, db_session: AsyncSession):
    me, key, kemi, _, dara, org, p = await _team(db_session)
    mine = await _ticket(db_session, p, me, assigned_user_id=me.id, title="Rotated keys")
    blocker = await _ticket(db_session, p, kemi, assigned_user_id=kemi.id, status="in_progress",
                            title="Migration 118 backfill")
    deeper = await _ticket(db_session, p, dara, assigned_user_id=dara.id, title="Schema review")
    done_dep = await _ticket(db_session, p, dara, assigned_user_id=dara.id, status="done")
    db_session.add_all([
        TicketDependency(ticket_id=mine.id, depends_on_id=blocker.id),
        TicketDependency(ticket_id=mine.id, depends_on_id=done_dep.id),
        TicketDependency(ticket_id=blocker.id, depends_on_id=deeper.id),
    ])
    review = await _ticket(db_session, p, me, assigned_user_id=me.id, status="review", title="Docs guide")
    await _comment(db_session, review, dara,
                   f"Codex R1 review on {review.id}: CHANGES REQUESTED\n\nFindings:\n\n - LOW - typo\n",
                   persona="codex-reviewer", trusted=True, at=NOW - timedelta(hours=3))
    db_session.add(AgentRun(id=_id("run"), project_id=p.id, persona_name="codex-reviewer",
                            role="reviewer", ticket_id=review.id, created_at=NOW))
    await db_session.commit()

    body = await _feed(client, key)
    collab = body["collaboration"]
    assert collab["error"] is None and collab["waiting"] == 2
    proj = collab["projects"][0]
    assert proj["project_id"] == p.id
    flows = {f["state"]: f for f in proj["flows"]}
    dep = flows["dependency"]
    assert dep["ticket"]["id"] == mine.id and dep["blocking"]["id"] == blocker.id
    assert [n["kind"] for n in dep["chain"]] == ["mine", "dep", "dep2"]
    assert dep["chain"][1]["who"]["name"] == "Kemi" and dep["chain"][2]["ref"] == deeper.id
    assert dep["to"] == {"name": "Kemi", "first": "Kemi"} and dep["can_remind"] is True
    assert "Hi Kemi" in dep["default_message"]
    rev = flows["review"]
    assert rev["ticket"]["id"] == review.id and rev["round"] == 2
    assert rev["to"]["name"] == "Dara" and rev["can_remind"]
    names = {m["name"] for m in proj["people"]}
    assert {"Kemi", "codex-reviewer"} <= names
    assert all("@" not in m["name"] or "***" in m["name"] for m in proj["people"])
    raw = json.dumps(collab)
    assert kemi.id not in raw and dara.id not in raw  # no user ids leave the server


async def test_remind_creates_reminder_for_the_owner(client: AsyncClient, db_session: AsyncSession):
    me, key, kemi, kemi_key, _dara, _org_, p = await _team(db_session)
    mine = await _ticket(db_session, p, me, assigned_user_id=me.id)
    blocker = await _ticket(db_session, p, kemi, assigned_user_id=kemi.id)
    db_session.add(TicketDependency(ticket_id=mine.id, depends_on_id=blocker.id))
    await db_session.commit()
    flow = (await _feed(client, key))["collaboration"]["projects"][0]["flows"][0]

    resp = await client.post("/api/v1/me/for-you/collaboration/remind", headers=_h(key), json={
        "flow_key": flow["flow_key"], "message": "Is it on track?", "when": "now",
    })
    assert resp.status_code == 201, resp.text
    rid = resp.json()["reminder_id"]
    assert resp.json()["to"] == "Kemi"
    r = (await db_session.execute(select(Reminder).where(Reminder.id == rid))).scalar_one()
    assert r.owner_user_id == kemi.id and r.created_by_user_id == me.id
    assert (r.source_kind, r.source_id) == ("ticket", blocker.id)
    note = (await db_session.execute(select(ReminderNote).where(ReminderNote.reminder_id == rid))).scalar_one()
    assert note.body == "Is it on track?"
    # Kemi sees it in her own feed, under "Your team is asking".
    kemi_feed = await _feed(client, kemi_key)
    asked = [i for i in _by_kind(kemi_feed, "reminder") if i["source"]["id"] == rid]
    assert asked and asked[0]["group"] == "asking"

    r.status, r.created_at = "done", NOW - timedelta(hours=2)  # past the cooldown
    await db_session.commit()
    tomorrow = await client.post("/api/v1/me/for-you/collaboration/remind", headers=_h(key), json={
        "flow_key": flow["flow_key"], "message": "Tomorrow then", "when": "tomorrow",
        "timezone": "Europe/London",
    })
    assert tomorrow.status_code == 201
    due = datetime.fromisoformat(tomorrow.json()["due_at"])
    from zoneinfo import ZoneInfo
    local = due.astimezone(ZoneInfo("Europe/London"))
    assert (local.hour, local.minute) == (9, 0) and due > datetime.now(timezone.utc)


async def test_remind_unknown_flow_and_lost_access(client: AsyncClient, db_session: AsyncSession):
    me, key, kemi, _, _dara, org, p = await _team(db_session)
    bad = await client.post("/api/v1/me/for-you/collaboration/remind", headers=_h(key), json={
        "flow_key": "fyc_" + "0" * 40, "message": "hi",
    })
    assert bad.status_code == 404

    outsider, _ = await _user(db_session, "zed")  # owns a ticket but can't read the project
    mine = await _ticket(db_session, p, me, assigned_user_id=me.id)
    blocker = await _ticket(db_session, p, me, assigned_user_id=outsider.id)
    db_session.add(TicketDependency(ticket_id=mine.id, depends_on_id=blocker.id))
    await db_session.commit()
    flow = (await _feed(client, key))["collaboration"]["projects"][0]["flows"][0]
    assert flow["can_remind"] is True
    resp = await client.post("/api/v1/me/for-you/collaboration/remind", headers=_h(key), json={
        "flow_key": flow["flow_key"], "message": "hi",
    })
    assert resp.status_code == 422
    assert "reminder_owner_not_eligible" in resp.text

    svc_key = await _service_key(db_session, me, org, scopes=("reminders:write", "attention:read"))
    refused = await client.post("/api/v1/me/for-you/collaboration/remind", headers=_h(svc_key), json={
        "flow_key": flow["flow_key"], "message": "hi",
    })
    assert refused.status_code == 403


async def test_collaboration_respects_project_access(client: AsyncClient, db_session: AsyncSession):
    me, key, kemi, kemi_key, _dara, org, p = await _team(db_session)
    other_org = await _org(db_session)
    await _member(db_session, other_org, kemi)
    hidden = await _project(db_session, kemi, other_org)
    t = await _ticket(db_session, hidden, kemi, assigned_user_id=me.id)  # assigned, but unreadable
    blocker = await _ticket(db_session, hidden, kemi, assigned_user_id=kemi.id)
    db_session.add(TicketDependency(ticket_id=t.id, depends_on_id=blocker.id))
    await db_session.commit()
    body = await _feed(client, key)
    assert body["collaboration"]["projects"] == []
    assert hidden.id not in json.dumps(body)


# ── review fixes ──


async def test_unchanged_feed_answers_304(client: AsyncClient, db_session: AsyncSession):
    me, key = await _user(db_session)
    p = await _project(db_session, me)
    t = await _ticket(db_session, p, me, assigned_user_id=me.id)
    db_session.add(AgentRun(id=_id("run"), project_id=p.id, persona_name="atlas", role="reviewer",
                            ticket_id=t.id, created_at=NOW,
                            findings=json.dumps([{"title": "x", "harm": "security", "status": "fixed"}])))
    await db_session.commit()
    first = await client.get("/api/v1/me/for-you", headers=_h(key))
    assert first.status_code == 200 and _by_kind(first.json(), "proof")
    again = await client.get("/api/v1/me/for-you", headers={**_h(key), "If-None-Match": first.headers["ETag"]})
    assert again.status_code == 304


async def test_keys_name_their_provider_and_hide_round_trips(client: AsyncClient, db_session: AsyncSession):
    from sessionfs.server.services import attention as svc

    me, key = await _user(db_session)
    p = await _project(db_session, me)
    await _ticket(db_session, p, me, assigned_user_id=me.id)
    card = _by_kind(await _feed(client, key), "ticket")[0]
    assert card["item_key"].startswith("fy_tk_")
    assert svc.provider_for_key(card["item_key"]) == "tickets"
    for _ in range(2):  # the upsert is idempotent
        r = await client.post("/api/v1/me/for-you/hide", headers=_h(key), json={"item_key": card["item_key"]})
        assert r.status_code == 204
    assert not _by_kind(await _feed(client, key), "ticket")
    assert (await client.get("/api/v1/me/for-you/count", headers=_h(key))).json()["actionable"] == 0
    r = await client.post("/api/v1/me/for-you/unhide", headers=_h(key), json={"item_key": card["item_key"]})
    assert r.status_code == 204
    assert _by_kind(await _feed(client, key), "ticket")


async def test_count_reuses_the_last_feed_build(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    from sessionfs.server.services import attention as svc

    me, key = await _user(db_session)
    p = await _project(db_session, me)
    await _ticket(db_session, p, me, assigned_user_id=me.id)
    await _feed(client, key)
    calls = []
    real = svc.build_feed
    monkeypatch.setattr(svc, "build_feed", lambda *a, **k: calls.append(1) or real(*a, **k))
    counts = (await client.get("/api/v1/me/for-you/count", headers=_h(key))).json()
    assert counts["actionable"] == 1 and calls == []


async def test_unverified_email_does_not_claim_handoffs(client: AsyncClient, db_session: AsyncSession):
    me, key = await _user(db_session)
    me.email_verified = False
    dara, _ = await _user(db_session, "dara")
    p = await _project(db_session, dara)
    sess = Session(id=_id("ses"), user_id=dara.id, title="x", tags="[]", source_tool="claude-code",
                   blob_key="x", blob_size_bytes=0, etag="x", project_id=p.id,
                   git_remote_normalized=p.git_remote_normalized,
                   created_at=NOW, updated_at=NOW, uploaded_at=NOW)
    db_session.add(sess)
    db_session.add(Handoff(id=_id("h")[:20], session_id=sess.id, sender_id=dara.id,
                           recipient_email=me.email, recipient_email_normalized=me.email.lower(),
                           status="pending", created_at=NOW, expires_at=NOW + timedelta(days=3)))
    await db_session.commit()
    assert not _by_kind(await _feed(client, key), "handoff")


async def test_remind_refuses_duplicates_and_cools_down(client: AsyncClient, db_session: AsyncSession):
    me, key, kemi, _kk, _dara, _org_, p = await _team(db_session)
    mine = await _ticket(db_session, p, me, assigned_user_id=me.id)
    blocker = await _ticket(db_session, p, kemi, assigned_user_id=kemi.id)
    db_session.add(TicketDependency(ticket_id=mine.id, depends_on_id=blocker.id))
    await db_session.commit()
    flow = (await _feed(client, key))["collaboration"]["projects"][0]["flows"][0]
    body = {"flow_key": flow["flow_key"], "message": "hi"}
    first = await client.post("/api/v1/me/for-you/collaboration/remind", headers=_h(key), json=body)
    assert first.status_code == 201
    dup = await client.post("/api/v1/me/for-you/collaboration/remind", headers=_h(key), json=body)
    assert dup.status_code == 409 and "reminder_exists" in dup.text
    r = (await db_session.execute(select(Reminder).where(Reminder.id == first.json()["reminder_id"]))).scalar_one()
    r.status = "done"
    await db_session.commit()
    cool = await client.post("/api/v1/me/for-you/collaboration/remind", headers=_h(key), json=body)
    assert cool.status_code == 429 and "reminder_cooldown" in cool.text


async def test_reminder_reopen_is_the_undo_for_done(client: AsyncClient, db_session: AsyncSession):
    me, key = await _user(db_session)
    r = await _reminder(db_session, me, due=NOW - timedelta(minutes=5))
    done = await client.patch(f"/api/v1/me/reminders/{r.id}", headers=_h(key), json={"status": "done"})
    assert done.status_code == 200
    back = await client.patch(f"/api/v1/me/reminders/{r.id}", headers=_h(key), json={"status": "open"})
    assert back.status_code == 200 and back.json()["status"] == "open"
    again = await client.patch(f"/api/v1/me/reminders/{r.id}", headers=_h(key), json={"status": "open"})
    assert again.status_code == 409


async def test_reminder_reopen_respects_the_open_cap(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    from sessionfs.server.services import reminders as rsvc

    me, key = await _user(db_session)
    a = await _reminder(db_session, me, due=NOW - timedelta(minutes=5))
    await _reminder(db_session, me, due=NOW - timedelta(minutes=5))
    done = await client.patch(f"/api/v1/me/reminders/{a.id}", headers=_h(key), json={"status": "done"})
    assert done.status_code == 200
    monkeypatch.setattr(rsvc, "MAX_OPEN_PER_USER", 1)
    back = await client.patch(f"/api/v1/me/reminders/{a.id}", headers=_h(key), json={"status": "open"})
    assert back.status_code == 409 and "reminder_limit_reached" in back.text


# ── Codex review ──


async def test_service_key_sees_reminder_source_only_with_its_scope(client: AsyncClient, db_session: AsyncSession):
    me, _ = await _user(db_session)
    org = await _org(db_session)
    await _member(db_session, org, me)
    p = await _project(db_session, me, org)
    t = await _ticket(db_session, p, me, assigned_user_id=me.id, title="Secret ticket title")
    await _reminder(db_session, me, due=NOW - timedelta(minutes=1), title="Follow up",
                    source=("ticket", t.id), project_id=p.id)
    bare = await _service_key(db_session, me, org, scopes=("attention:read", "reminders:read"))
    item = _by_kind(await _feed(client, bare), "reminder")[0]
    assert "Secret ticket title" not in json.dumps(item)
    full = await _service_key(db_session, me, org, scopes=("attention:read", "reminders:read", "tickets:read"))
    item = _by_kind(await _feed(client, full), "reminder")[0]
    assert "Secret ticket title" in item["why"]


async def test_remind_title_never_copies_the_ticket(client: AsyncClient, db_session: AsyncSession):
    me, key, kemi, _kk, _dara, _org_, p = await _team(db_session)
    mine = await _ticket(db_session, p, me, assigned_user_id=me.id, title="Mine")
    blocker = await _ticket(db_session, p, kemi, assigned_user_id=kemi.id, title="Confidential blocker")
    db_session.add(TicketDependency(ticket_id=mine.id, depends_on_id=blocker.id))
    await db_session.commit()
    flow = (await _feed(client, key))["collaboration"]["projects"][0]["flows"][0]
    resp = await client.post("/api/v1/me/for-you/collaboration/remind", headers=_h(key),
                             json={"flow_key": flow["flow_key"], "message": "hi"})
    assert resp.status_code == 201
    r = (await db_session.execute(select(Reminder).where(Reminder.id == resp.json()["reminder_id"]))).scalar_one()
    assert r.title == "Reminder from Ola: waiting on a ticket"
    assert "Confidential" not in r.title


async def test_remind_refuses_when_the_flow_moves_under_the_lock(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    from sessionfs.server.services import attention_collab as collab

    me, key, kemi, _kk, dara, _org_, p = await _team(db_session)
    mine = await _ticket(db_session, p, me, assigned_user_id=me.id)
    blocker = await _ticket(db_session, p, kemi, assigned_user_id=kemi.id)
    db_session.add(TicketDependency(ticket_id=mine.id, depends_on_id=blocker.id))
    await db_session.commit()
    flow = (await _feed(client, key))["collaboration"]["projects"][0]["flows"][0]

    real = collab.collaboration_flows
    calls = {"n": 0}

    async def moving(ctx):
        flows = await real(ctx)
        calls["n"] += 1
        if calls["n"] == 2:  # the re-read under the lock: reassigned meanwhile
            for f in flows:
                f.recipient_user_id = dara.id
        return flows

    monkeypatch.setattr(collab, "collaboration_flows", moving)
    resp = await client.post("/api/v1/me/for-you/collaboration/remind", headers=_h(key),
                             json={"flow_key": flow["flow_key"], "message": "hi"})
    assert resp.status_code == 409 and "flow_changed" in resp.text
    count = (await db_session.execute(select(Reminder).where(Reminder.created_by_user_id == me.id))).all()
    assert count == []

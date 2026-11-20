"""For You scope (tk_caf9ae418d14491c): a card is yours only when it names
you or you started it.

These tests pin the membership rule end to end with at least two users:
a brand-new member sees none of another member's loops/tickets/checklists/
meetings/questions and a zero badge; a member sees loop cards only for loops
they started or children assigned to them (directly or via a persona that
represents them); proposed checklist items never appear; and every card in a
mixed feed carries four plain-language explanation sentences with no record
id, no ranking term, and no email.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentPersona,
    AgentRun,
    Handoff,
    LoopChecklistItem,
    Session,
    Ticket,
    TicketComment,
    User,
)
from tests.server.integration.test_attention_feed import (
    NOW,
    _caught_run,
    _feed,
    _id,
    _knowledge,
    _meeting_action,
    _member,
    _org,
    _project,
    _reminder,
    _service_key,
    _session_in,
    _ticket,
    _user,
)

pytestmark = pytest.mark.asyncio

HEAD = "a1b2c3d4e5f6a7b8"
CLEAN = "Codex R1 review on tk_x: VERIFIED-CLEAN\n\nFindings: none."


async def _loop_child(
    db: AsyncSession,
    project,
    starter: User,
    *,
    assigned_user_id=None,
    assigned_to=None,
    ready: bool = True,
) -> tuple[Ticket, Ticket]:
    """A review-loop parent + child, seeded so the child is merge-ready (a
    trusted VERIFIED-CLEAN bound to HEAD by a different identity) or, when
    ``ready`` is False, has hit a token budget (needs_human)."""
    parent = Ticket(
        id=_id("tk"),
        project_id=project.id,
        title=f"loop-{uuid.uuid4().hex[:6]}",
        kind="issue",
        status="open",
        created_by_user_id=starter.id,
        loop_config=json.dumps({"reviewer_persona": "reviewer", "token_budget": 10}),
    )
    db.add(parent)
    await db.flush()
    child = Ticket(
        id=_id("tk"),
        project_id=project.id,
        title=f"child-{uuid.uuid4().hex[:6]}",
        kind="task",
        status="open",
        parent_ticket_id=parent.id,
        created_by_user_id=starter.id,
        assigned_user_id=assigned_user_id,
        assigned_to=assigned_to,
    )
    db.add(child)
    await db.flush()
    db.add(
        AgentRun(
            id=_id("run"),
            project_id=project.id,
            persona_name="atlas",
            role="implementer",
            ticket_id=child.id,
            head_sha=HEAD,
            branch="feat/loop",
            input_tokens=0 if ready else 20,
            triggered_by_user_id=starter.id,
            created_at=NOW,
        )
    )
    await db.flush()
    if ready:
        reviewer = User(
            id=str(uuid.uuid4()),
            email=f"rev-{uuid.uuid4().hex[:6]}@example.com",
            display_name="Reviewer",
            tier="team",
            email_verified=True,
            created_at=NOW,
        )
        db.add(reviewer)
        await db.flush()
        db.add(
            TicketComment(
                id=_id("tc"),
                ticket_id=child.id,
                author_user_id=reviewer.id,
                author_persona="codex-reviewer",
                content=CLEAN,
                verdict_trusted=True,
                reviewed_commit=HEAD,
                created_at=NOW,
            )
        )
    await db.commit()
    return parent, child


async def _proposed_item(db: AsyncSession, project, *, slug: str) -> LoopChecklistItem:
    item = LoopChecklistItem(
        id=_id("lci"),
        project_id=project.id,
        slug=slug,
        title="A trap",
        check_text="Check it.",
        status="proposed",
        source="manual",
        times_found=0,
        created_at=NOW,
    )
    db.add(item)
    await db.commit()
    return item


async def _question(
    db: AsyncSession, ticket: Ticket, asker: User, *, content: str = "Can you check the login flow?"
) -> TicketComment:
    c = TicketComment(
        id=_id("tc"),
        ticket_id=ticket.id,
        author_user_id=asker.id,
        content=content,
        source="sessionfs",
        actor_type="user",
        verdict_trusted=False,
        created_at=NOW,
    )
    db.add(c)
    await db.commit()
    return c


async def _needs_human_child(
    db: AsyncSession, project, starter: User
) -> tuple[Ticket, Ticket]:
    """A loop child that hit BOTH the token budget and the round cap, so it
    emits two ``loop_needs_human`` cards. It is not assigned to anyone, so it
    never also surfaces as a plain ticket card."""
    parent = Ticket(
        id=_id("tk"),
        project_id=project.id,
        title=f"loop-{uuid.uuid4().hex[:6]}",
        kind="issue",
        status="open",
        created_by_user_id=starter.id,
        loop_config=json.dumps(
            {"reviewer_persona": "reviewer", "token_budget": 10, "max_review_rounds": 1}
        ),
    )
    db.add(parent)
    await db.flush()
    child = Ticket(
        id=_id("tk"),
        project_id=project.id,
        title=f"child-{uuid.uuid4().hex[:6]}",
        kind="task",
        status="open",
        parent_ticket_id=parent.id,
        created_by_user_id=starter.id,
    )
    db.add(child)
    await db.flush()
    db.add(
        AgentRun(
            id=_id("run"),
            project_id=project.id,
            persona_name="atlas",
            role="implementer",
            ticket_id=child.id,
            head_sha=HEAD,
            branch="feat/loop",
            input_tokens=20,
            triggered_by_user_id=starter.id,
            created_at=NOW,
        )
    )
    reviewer = User(
        id=str(uuid.uuid4()),
        email=f"rev-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Reviewer",
        tier="team",
        email_verified=True,
        created_at=NOW,
    )
    db.add(reviewer)
    await db.flush()
    db.add(
        TicketComment(
            id=_id("tc"),
            ticket_id=child.id,
            author_user_id=reviewer.id,
            author_persona="codex-reviewer",
            content=f"Codex R1 review on {child.id}: CHANGES-REQUESTED\n\nFindings: none.",
            verdict_trusted=True,
            reviewed_commit=HEAD,
            created_at=NOW,
        )
    )
    await db.commit()
    return parent, child


def _loop_ids(body: dict) -> set[str]:
    return {
        i["source"]["id"]
        for i in body["items"]
        if i["kind"] in ("loop_ready", "loop_needs_human")
    }


async def test_new_member_sees_nothing_and_a_zero_badge(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, alice_key = await _user(db_session, "alice")
    bob, bob_key = await _user(db_session, "bob")
    carol, _ = await _user(db_session, "carol")
    org = await _org(db_session)
    await _member(db_session, org, alice)
    await _member(db_session, org, bob)
    await _member(db_session, org, carol)
    p = await _project(db_session, alice, org)

    # Alice's personal sources, all in a project bob can read.
    await _ticket(db_session, p, alice, assigned_user_id=alice.id)
    await _loop_child(db_session, p, alice)
    await _proposed_item(db_session, p, slug="a-trap")
    await _meeting_action(db_session, p, alice)
    q_ticket = await _ticket(db_session, p, alice, assigned_user_id=alice.id)
    await _question(db_session, q_ticket, carol)

    # Sanity: the seed is live for alice, so bob's emptiness is the lens.
    alice_body = await _feed(client, alice_key)
    assert {
        "ticket", "loop_ready", "meeting_action", "ticket_question",
    } <= {i["kind"] for i in alice_body["items"]}

    bob_body = await _feed(client, bob_key)
    assert bob_body["items"] == []
    assert bob_body["counts"] == {
        "now": 0, "your_turn": 0, "upcoming": 0, "lately": 0,
        "total": 0, "actionable": 0,
    }


async def test_loop_cards_are_only_mine(client: AsyncClient, db_session: AsyncSession) -> None:
    alice, alice_key = await _user(db_session, "alice")
    carol, _ = await _user(db_session, "carol")
    dave, _ = await _user(db_session, "dave")
    org = await _org(db_session)
    for u in (alice, carol, dave):
        await _member(db_session, org, u)
    p = await _project(db_session, alice, org)

    _, started = await _loop_child(db_session, p, alice)
    _, assigned = await _loop_child(db_session, p, dave, assigned_user_id=alice.id)
    _, other = await _loop_child(db_session, p, carol, assigned_user_id=carol.id)

    body = await _feed(client, alice_key)
    # The starter's child and the child assigned to alice are hers; carol's
    # child (started by and assigned to carol) is not. The assigned child also
    # surfaces as a plain ticket, so assert on loop cards and on carol's
    # child never appearing anywhere, rather than on raw totals.
    assert _loop_ids(body) == {started.id, assigned.id}
    assert other.id not in {i["source"]["id"] for i in body["items"]}


async def test_proposed_checklist_items_never_reach_for_you(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, alice_key = await _user(db_session, "alice")
    org = await _org(db_session)
    await _member(db_session, org, alice)
    p = await _project(db_session, alice, org)
    await _loop_child(db_session, p, alice)
    await _proposed_item(db_session, p, slug="a-trap")

    body = await _feed(client, alice_key)
    assert "loop_checklist" not in {i["kind"] for i in body["items"]}
    assert _loop_ids(body)  # the ready child still shows for the starter


async def test_persona_representing_me_makes_the_loop_card_mine(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, alice_key = await _user(db_session, "alice")
    bob, bob_key = await _user(db_session, "bob")
    carol, _ = await _user(db_session, "carol")
    org = await _org(db_session)
    for u in (alice, bob, carol):
        await _member(db_session, org, u)
    p = await _project(db_session, alice, org)

    # "atlas" represents bob in this org; the child is assigned to it and
    # started by carol, so only bob (via the persona) should own the card.
    db_session.add(
        AgentPersona(
            id=_id("per"),
            org_id=org.id,
            name="atlas",
            role="Backend",
            created_by=carol.id,
            represented_user_id=bob.id,
            is_active=True,
        )
    )
    await db_session.commit()
    _, child = await _loop_child(db_session, p, carol, assigned_to="Atlas")

    bob_body = await _feed(client, bob_key)
    assert _loop_ids(bob_body) == {child.id}

    alice_body = await _feed(client, alice_key)
    assert child.id not in _loop_ids(alice_body)


async def test_two_orgs_do_not_leak_into_each_other(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, alice_key = await _user(db_session, "alice")
    bob, bob_key = await _user(db_session, "bob")
    org1 = await _org(db_session)
    org2 = await _org(db_session)
    await _member(db_session, org1, alice)
    await _member(db_session, org2, bob)
    p1 = await _project(db_session, alice, org1)
    p2 = await _project(db_session, bob, org2)

    _, alice_child = await _loop_child(db_session, p1, alice)
    await _ticket(db_session, p1, alice, assigned_user_id=alice.id)
    await _ticket(db_session, p2, bob, assigned_user_id=bob.id)

    alice_body = await _feed(client, alice_key)
    assert alice_child.id in _loop_ids(alice_body)
    assert {i["project_id"] for i in alice_body["items"]} == {p1.id}

    bob_body = await _feed(client, bob_key)
    assert {i["project_id"] for i in bob_body["items"]} == {p2.id}
    assert "loop_ready" not in {i["kind"] for i in bob_body["items"]}


async def test_every_card_carries_four_explanation_sentences(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    me, key = await _user(db_session, "alice")
    other, _ = await _user(db_session, "bob")
    dara, _ = await _user(db_session, "dara")
    org = await _org(db_session)
    await _member(db_session, org, me)
    await _member(db_session, org, other)
    await _member(db_session, org, dara)
    p = await _project(db_session, me, org)

    ticket = await _ticket(db_session, p, me, assigned_user_id=me.id)
    await _reminder(db_session, me, due=NOW - timedelta(hours=1))
    await _meeting_action(db_session, p, me)
    _, child = await _loop_child(db_session, p, me)
    await _session_in(db_session, me, p)
    await _knowledge(db_session, p, other)
    await _caught_run(db_session, p, ticket)
    q_ticket = await _ticket(db_session, p, me, assigned_user_id=me.id)
    await _question(db_session, q_ticket, other)
    sess = Session(
        id=_id("ses"), user_id=dara.id, title="x", tags="[]", source_tool="claude-code",
        blob_key="x", blob_size_bytes=0, etag="x", project_id=p.id,
        git_remote_normalized=p.git_remote_normalized,
        created_at=NOW, updated_at=NOW, uploaded_at=NOW,
    )
    db_session.add(sess)
    await db_session.commit()
    db_session.add(
        Handoff(
            id=_id("h")[:20], session_id=sess.id, sender_id=dara.id,
            recipient_user_id=me.id, status="pending", created_at=NOW,
            expires_at=NOW + timedelta(days=5),
        )
    )
    await db_session.commit()

    body = await _feed(client, key)
    assert body["items"], "expected a populated mixed feed"

    expected_kinds = {
        "ticket", "reminder", "meeting_action", "loop_ready",
        "knowledge_changes", "proof", "ticket_question", "handoff",
    }
    kinds = {i["kind"] for i in body["items"]}
    assert expected_kinds <= kinds, kinds

    for card in body["items"]:
        explain = card["context"].get("explain")
        assert isinstance(explain, dict), card["kind"]
        assert set(explain) == {"what", "why", "act", "from"}, card["kind"]
        for label, sentence in explain.items():
            assert isinstance(sentence, str) and sentence.strip(), (card["kind"], label)
            assert "tk_" not in sentence, (card["kind"], label, sentence)
            assert "proj_" not in sentence, (card["kind"], label, sentence)
            assert "@" not in sentence, (card["kind"], label, sentence)


async def test_callers_loop_card_survives_a_busy_org(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, alice_key = await _user(db_session, "alice")
    bob, _ = await _user(db_session, "bob")
    org = await _org(db_session)
    await _member(db_session, org, alice)
    await _member(db_session, org, bob)
    p = await _project(db_session, alice, org)

    # 120 ready loop children owned by bob would fill the org-wide inbox past
    # its cap; alice's own child must still surface because the provider
    # queries her children directly instead of narrowing the shared inbox.
    for _ in range(120):
        await _loop_child(db_session, p, bob, assigned_user_id=bob.id)
    _, mine = await _loop_child(db_session, p, alice)

    body = await _feed(client, alice_key)
    assert _loop_ids(body) == {mine.id}


async def test_loop_total_reflects_full_count_when_capped(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sessionfs.server.services import attention

    alice, alice_key = await _user(db_session, "alice")
    org = await _org(db_session)
    await _member(db_session, org, alice)
    p = await _project(db_session, alice, org)

    monkeypatch.setattr(attention, "LOOP_ROW_CAP", 2)
    for _ in range(3):
        await _loop_child(db_session, p, alice)

    body = await _feed(client, alice_key)
    loop_cards = [i for i in body["items"] if i["kind"] == "loop_ready"]
    assert len(loop_cards) == 2  # the cap limits how many are listed…
    # …and the section says it was truncated instead of inventing a count.
    assert body["sections"]["loop_needs_you"]["count"] == 2
    assert body["sections"]["loop_needs_you"]["truncated"] is True


async def test_other_issue_children_never_use_up_the_loop_cap(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Children of ordinary issues (no loop_config) that the caller created
    are filtered in SQL, so they can't crowd out a loop card or inflate the
    loop total."""
    from sessionfs.server.services import attention

    alice, alice_key = await _user(db_session, "alice")
    org = await _org(db_session)
    await _member(db_session, org, alice)
    p = await _project(db_session, alice, org)

    monkeypatch.setattr(attention, "LOOP_ROW_CAP", 2)
    issue = Ticket(
        id=_id("tk"), project_id=p.id, title="plain issue", kind="issue",
        status="open", created_by_user_id=alice.id,
    )
    db_session.add(issue)
    await db_session.flush()
    for n in range(3):
        db_session.add(
            Ticket(
                id=_id("tk"), project_id=p.id, title=f"plain child {n}", kind="task",
                status="open", parent_ticket_id=issue.id, created_by_user_id=alice.id,
            )
        )
    await db_session.commit()
    _, mine = await _loop_child(db_session, p, alice)

    body = await _feed(client, alice_key)
    assert _loop_ids(body) == {mine.id}
    assert body["sections"]["loop_needs_you"]["count"] == 1


async def test_loop_child_hitting_two_limits_emits_two_cards(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, alice_key = await _user(db_session, "alice")
    org = await _org(db_session)
    await _member(db_session, org, alice)
    p = await _project(db_session, alice, org)

    _, child = await _needs_human_child(db_session, p, alice)

    body = await _feed(client, alice_key)
    cards = [i for i in body["items"] if i["kind"] == "loop_needs_human"]
    assert len(cards) == 2  # one card per limit hit
    assert {c["source"]["id"] for c in cards} == {
        f"{child.id}#token budget hit (20 tokens spent)",
        f"{child.id}#round cap hit",
    }
    assert body["sections"]["loop_needs_you"]["count"] == 2
    assert body["sections"]["loop_needs_you"].get("truncated") is not True


async def test_non_actionable_child_emits_no_card(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, alice_key = await _user(db_session, "alice")
    org = await _org(db_session)
    await _member(db_session, org, alice)
    p = await _project(db_session, alice, org)

    # A loop child with no run and no verdict: neither merge-ready nor
    # needs-human, so it must not produce a card or count.
    parent = Ticket(
        id=_id("tk"), project_id=p.id, title="loop", kind="issue", status="open",
        created_by_user_id=alice.id,
        loop_config=json.dumps({"reviewer_persona": "reviewer"}),
    )
    db_session.add(parent)
    await db_session.flush()
    db_session.add(
        Ticket(
            id=_id("tk"), project_id=p.id, title="child", kind="task", status="open",
            parent_ticket_id=parent.id, created_by_user_id=alice.id,
        )
    )
    await db_session.commit()

    body = await _feed(client, alice_key)
    assert [i for i in body["items"] if i["kind"] in ("loop_ready", "loop_needs_human")] == []
    assert body["sections"]["loop_needs_you"]["count"] == 0


async def test_service_key_raise_needs_loops_write_scope(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, _ = await _user(db_session, "alice")
    org = await _org(db_session)
    await _member(db_session, org, alice)
    p = await _project(db_session, alice, org)
    _, child = await _needs_human_child(db_session, p, alice)

    def _needs_human_card(body: dict) -> dict:
        return next(
            i for i in body["items"]
            if i["kind"] == "loop_needs_human" and i["source"]["id"].startswith(f"{child.id}#")
        )

    # tickets:read shows the card, but without loops:write the minter's editor
    # standing alone must not offer "raise" (a write action).
    read_only = await _service_key(db_session, alice, org)
    card = _needs_human_card(await _feed(client, read_only))
    assert "raise" not in card["actions"]
    assert card["context"].get("loop") is None

    write = await _service_key(
        db_session, alice, org,
        scopes=("attention:read", "tickets:read", "loops:write"),
    )
    card = _needs_human_card(await _feed(client, write))
    assert "raise" in card["actions"]
    assert card["context"]["loop"]["ticket_id"] == child.id


async def test_viewer_created_ticket_question_is_only_assignee_or_mention(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, alice_key = await _user(db_session, "alice")
    bob, _ = await _user(db_session, "bob")
    carol, _ = await _user(db_session, "carol")
    org = await _org(db_session)
    for u in (alice, bob, carol):
        await _member(db_session, org, u)
    p = await _project(db_session, alice, org)

    # Three tickets alice created but assigned to bob. A bare "?" is only
    # alice's when bob (the assignee) asked it or it @mentions her.
    third_party = await _ticket(db_session, p, alice, assigned_user_id=bob.id)
    await _question(db_session, third_party, carol)

    assignee = await _ticket(db_session, p, alice, assigned_user_id=bob.id)
    await _question(db_session, assignee, bob)

    mention = await _ticket(db_session, p, alice, assigned_user_id=bob.id)
    await _question(db_session, mention, carol, content="@alice can you check this?")

    body = await _feed(client, alice_key)
    question_tickets = {
        i["context"]["ref"] for i in body["items"] if i["kind"] == "ticket_question"
    }
    assert question_tickets == {assignee.id, mention.id}
    assert third_party.id not in question_tickets


async def test_grandfathered_project_surfaces_loop_cards(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sessionfs.server.db.models import ProjectFeatureGrandfather
    from sessionfs.server.services import attention
    from sessionfs.server.tiers import Tier

    alice, alice_key = await _user(db_session, "alice", tier="free")
    p = await _project(db_session, alice)  # personal project on the free plan

    _, child = await _loop_child(db_session, p, alice)

    # Free plan, no grandfather: review loops are not available to alice.
    assert child.id not in _loop_ids(await _feed(client, alice_key))

    db_session.add(ProjectFeatureGrandfather(project_id=p.id, feature="review_loop"))
    await db_session.commit()

    async def _team(user, db, **kw):
        return Tier.TEAM

    # The loop feed follows the same rule as the routes: the project is
    # grandfathered, so a team-tier caller sees its loop cards.
    monkeypatch.setattr(attention, "get_effective_tier", _team)
    assert child.id in _loop_ids(await _feed(client, alice_key))


async def test_ticket_explanation_says_why_it_is_yours(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, alice_key = await _user(db_session, "alice")
    bob, _ = await _user(db_session, "bob")
    org = await _org(db_session)
    await _member(db_session, org, alice)
    await _member(db_session, org, bob)
    p = await _project(db_session, alice, org)

    direct = await _ticket(db_session, p, bob, assigned_user_id=alice.id)
    db_session.add(
        AgentPersona(
            id=_id("per"),
            org_id=org.id,
            name="atlas",
            role="Backend",
            created_by=bob.id,
            represented_user_id=alice.id,
            is_active=True,
        )
    )
    await db_session.commit()
    via_persona = await _ticket(db_session, p, bob, assigned_to="Atlas")
    # Created by alice but assigned to bob: it waits on someone else, so the
    # ticket provider never adds it to alice's feed.
    created = await _ticket(db_session, p, alice, assigned_user_id=bob.id)

    body = await _feed(client, alice_key)
    tickets = {i["source"]["id"]: i for i in body["items"] if i["kind"] == "ticket"}
    assert direct.id in tickets
    assert via_persona.id in tickets
    assert created.id not in tickets

    assert tickets[direct.id]["context"]["explain"]["why"] == "This ticket is assigned to you."
    assert tickets[via_persona.id]["context"]["explain"]["why"] == (
        "This ticket is assigned to Atlas, a persona that represents you."
    )


async def test_ticket_i_created_waiting_on_someone_else_is_a_second_look_card(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """CEO 2026-11-18: a ticket you created that waits on someone else is
    yours ("you started it"), shown in Worth a second look and never in the
    badge. The assignee still sees it as their own Waiting on you card."""
    alice, alice_key = await _user(db_session, "alice")
    bob, bob_key = await _user(db_session, "bob")
    org = await _org(db_session)
    await _member(db_session, org, alice)
    await _member(db_session, org, bob)
    p = await _project(db_session, alice, org)
    t = await _ticket(db_session, p, alice, assigned_user_id=bob.id, title="Rotate the signing key")

    body = await _feed(client, alice_key)
    mine = [i for i in body["items"] if i["source"]["id"] == t.id]
    assert len(mine) == 1
    card = mine[0]
    assert card["group"] == "second" and card["band"] == "upcoming"
    assert "waiting on" in card["why"]
    ex = card["context"]["explain"]
    assert ex["why"] == "You created this ticket and it is waiting on someone else."
    assert "Rotate the signing key" in ex["what"]

    bob_body = await _feed(client, bob_key)
    assert any(i["source"]["id"] == t.id and i["group"] == "waiting" for i in bob_body["items"])

    # An unassigned ticket you created is not waiting on anyone: no card.
    lone = await _ticket(db_session, p, alice, title="Unassigned idea")
    body = await _feed(client, alice_key)
    assert not any(i["source"]["id"] == lone.id for i in body["items"])

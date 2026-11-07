"""Approval policies (GitHub #123): redaction for non-member readers, scoped
service-key titles, deferred KB supersede, merge-check rechecks, batched
eligibility queries, re-raising terminal requests, decide re-checks under the
lock, generic readiness reasons, strict requirement counts and decisions that
outlive their approver."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentRun,
    ApprovalDecision,
    ApprovalRequest,
    KnowledgeEntry,
    OrgMember,
    Project,
    PullRequest,
    Session,
    Ticket,
    TicketNotification,
    TicketPrLink,
)
from sessionfs.server.services import approvals as svc
from tests.server.integration.approvals_helpers import (
    HEAD,
    add_member,
    hdrs,
    make_org,
    make_policy,
    make_service_key,
    make_ticket,
    make_user,
    make_world,
    seed_clean,
    seed_implementer,
)

CONTENT = (
    "Decision: PHI fields in src/sessionfs/server/models.py are encrypted with "
    "the per-org key before they are written."
)
KB_POLICY = {"entity_ref_prefix_any": ["docs/compliance/"]}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _readiness(client: AsyncClient, w, key: str, sha: str = HEAD) -> dict:
    resp = await client.get(
        f"/api/v1/projects/{w.project.id}/tickets/{w.ticket.id}/readiness",
        headers=hdrs(key), params={"sha": sha},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _captured_reader(db: AsyncSession, project: Project):
    """A user who reads the project only through a synced session."""
    user, raw = await make_user(db, "captured")
    db.add(
        Session(
            id=f"ses_{uuid.uuid4().hex[:16]}",
            user_id=user.id,
            title="synced from the same repo",
            tags="[]",
            source_tool="claude-code",
            blob_key="x",
            blob_size_bytes=0,
            etag="x",
            git_remote_normalized=project.git_remote_normalized,
            created_at=_now(),
            updated_at=_now(),
            uploaded_at=_now(),
        )
    )
    await db.commit()
    return user, raw


async def _fresh(db: AsyncSession, model, pk):
    return (
        await db.execute(
            select(model).where(model.id == pk).execution_options(populate_existing=True)
        )
    ).scalar_one()


class _Counter:
    """Counts SQL statements on an engine while active."""

    def __init__(self, engine) -> None:
        self.engine = engine.sync_engine
        self.n = 0

    def _on(self, *_a, **_k) -> None:
        self.n += 1

    def __enter__(self) -> "_Counter":
        event.listen(self.engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *_exc) -> None:
        event.remove(self.engine, "before_cursor_execute", self._on)


# ── 1. redacted request view for non-members ───────────────────────


@pytest.mark.asyncio
async def test_request_view_redacted_for_non_members(
    client: AsyncClient, db_session: AsyncSession
):
    w = await make_world(db_session)
    await make_policy(db_session, w.org, requirements=[
        {"who": "users", "user_ids": [w.admin.id, w.admin2.id], "count": 2}])
    rid = (await _readiness(client, w, w.owner_key))["approvals"][0]["request_id"]
    resp = await client.post(
        f"/api/v1/approval-requests/{rid}/decide", headers=hdrs(w.admin_key),
        json={"decision": "approve", "reason": "fine"},
    )
    assert resp.status_code == 200, resp.text
    url = f"/api/v1/approval-requests/{rid}"

    member = (await client.get(url, headers=hdrs(w.reviewer_key))).json()
    assert member["decisions"][0]["approver_name"] == "Admin"
    assert member["policy"]["requirements"][0]["user_ids"] == [w.admin.id, w.admin2.id]

    _, reader_key = await _captured_reader(db_session, w.project)
    _, svc_key = await make_service_key(db_session, w.owner, w.org, ["approvals:read"])
    for key in (reader_key, svc_key):
        resp = await client.get(url, headers=hdrs(key))
        assert resp.status_code == 200, resp.text
        view = resp.json()
        text = resp.text
        assert w.admin.id not in text and w.admin2.id not in text
        assert w.admin.email not in text and "Admin" not in text
        (d,) = view["decisions"]
        assert d["approver_user_id"] is None and d["approver_name"] is None
        assert view["policy"]["requirements"] == [{"who": "users", "count": 2}]
        assert view["approved_count"] == 1 and view["can_decide"] is False


# ── 2. service-key titles need the matching read scope ─────────────


@pytest.mark.asyncio
async def test_service_key_titles_need_read_scopes(
    client: AsyncClient, db_session: AsyncSession
):
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    await make_policy(db_session, w.org, name="Compliance", target_kind="kb_promote",
                      flt=KB_POLICY)
    await _readiness(client, w, w.owner_key)
    entry = KnowledgeEntry(
        project_id=w.project.id, session_id="manual", user_id=w.owner.id,
        entry_type="decision", content=CONTENT, confidence=0.9, claim_class="note",
        entity_ref="docs/compliance/phi.md", freshness_class="current",
    )
    db_session.add(entry)
    await db_session.commit()
    gate = await svc.kb_promotion_gate(db_session, entry, w.owner.id)
    await db_session.commit()
    assert gate.blocked

    async def _mine(scopes: list[str]) -> list[dict]:
        _, raw = await make_service_key(db_session, w.admin, w.org, scopes)
        resp = await client.get("/api/v1/me/approvals", headers=hdrs(raw))
        assert resp.status_code == 200, resp.text
        return resp.json()["requests"]

    bare = await _mine(["approvals:read"])
    assert {r["target_kind"] for r in bare} == {"merge", "kb_promote"}
    assert all("target_title" not in r for r in bare)
    tickets = {r["target_kind"]: r for r in await _mine(["approvals:read", "tickets:read"])}
    assert tickets["merge"]["target_title"] == "Auth change"
    assert "target_title" not in tickets["kb_promote"]
    kb = {r["target_kind"]: r for r in await _mine(["approvals:read", "knowledge:read"])}
    assert kb["kb_promote"]["target_title"].startswith("Decision: PHI")
    assert "target_title" not in kb["merge"]

    # Without an allowlist the key stays in its own org; a pending request
    # in another org the minter can decide is left out.
    other = await make_org(db_session)
    await add_member(db_session, other, w.admin, "admin")
    await add_member(db_session, other, w.implementer, "member")
    other_project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}", name="o", context_document="",
        git_remote_normalized=f"acme/o-{uuid.uuid4().hex[:6]}",
        owner_id=w.implementer.id, org_id=other.id,
    )
    db_session.add(other_project)
    await db_session.commit()
    other_ticket = await make_ticket(db_session, other_project, w.implementer)
    await seed_implementer(db_session, other_project, other_ticket, user_id=w.implementer.id)
    await seed_clean(db_session, other_ticket, w.reviewer, HEAD)
    await make_policy(db_session, other)
    await svc.ticket_readiness(db_session, other_ticket, HEAD)
    await db_session.commit()
    user_view = (await client.get("/api/v1/me/approvals", headers=hdrs(w.admin_key))).json()
    assert {r["org_id"] for r in user_view["requests"]} == {w.org.id, other.id}
    assert {r["org_id"] for r in bare} == {w.org.id}
    assert {r["org_id"] for r in await _mine(["approvals:read"])} == {w.org.id}


# ── 3. a held upsert supersedes only once promoted ─────────────────


@pytest.mark.asyncio
async def test_held_upsert_keeps_prior_claim_until_approved(
    client: AsyncClient, db_session: AsyncSession
):
    w = await make_world(db_session)
    prior = KnowledgeEntry(
        project_id=w.project.id, session_id="manual", user_id=w.owner.id,
        entry_type="decision", content=CONTENT + " (v1)", confidence=0.9,
        claim_class="claim", entity_ref="docs/compliance/phi.md", freshness_class="current",
    )
    db_session.add(prior)
    await db_session.commit()
    prior_id = prior.id
    await make_policy(db_session, w.org, name="Compliance", target_kind="kb_promote",
                      flt=KB_POLICY)

    resp = await client.post(
        f"/api/v1/projects/{w.project.id}/entries/add", headers=hdrs(w.owner_key),
        json={"content": CONTENT + " Keys rotate yearly via scripts/rotate.py.",
              "entry_type": "decision", "confidence": 0.95,
              "entity_ref": "docs/compliance/phi.md", "upsert": True},
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["claim_class"] == "note" and body["approval_pending"] is True
    assert body["upserted_from"] == []
    new_id = body["id"]

    old = await _fresh(db_session, KnowledgeEntry, prior_id)
    assert old.dismissed is False and old.superseded_by is None
    (req,) = (
        await db_session.execute(
            select(ApprovalRequest).where(ApprovalRequest.target_id == str(new_id))
        )
    ).scalars().all()

    await svc.decide(db_session, request_id=req.id, approver=w.admin,
                     decision="approve", reason="ok")
    await db_session.commit()
    new = await _fresh(db_session, KnowledgeEntry, new_id)
    old = await _fresh(db_session, KnowledgeEntry, prior_id)
    assert new.claim_class == "claim" and new.pending_supersede_ids is None
    assert old.dismissed is True and old.superseded_by == new_id


@pytest.mark.asyncio
async def test_unheld_upsert_still_supersedes_at_once(
    client: AsyncClient, db_session: AsyncSession
):
    w = await make_world(db_session)
    prior = KnowledgeEntry(
        project_id=w.project.id, session_id="manual", user_id=w.owner.id,
        entry_type="decision", content=CONTENT + " (v1)", confidence=0.9,
        claim_class="claim", entity_ref="docs/other.md", freshness_class="current",
    )
    db_session.add(prior)
    await db_session.commit()
    await make_policy(db_session, w.org, name="Compliance", target_kind="kb_promote",
                      flt=KB_POLICY)
    resp = await client.post(
        f"/api/v1/projects/{w.project.id}/entries/add", headers=hdrs(w.owner_key),
        json={"content": CONTENT + " Keys rotate yearly via scripts/rotate.py.",
              "entry_type": "decision", "confidence": 0.95,
              "entity_ref": "docs/other.md", "upsert": True},
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["upserted_from"] == [prior.id]


# ── 4. merge checks re-run after decisions and policy writes ───────


@pytest.mark.asyncio
async def test_merge_check_rechecked_after_decide_and_policy_writes(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    from sessionfs.server.services import merge_checks

    calls: list[str] = []
    monkeypatch.setattr(merge_checks, "schedule_recheck", lambda t: calls.append(f"t:{t}"))
    monkeypatch.setattr(
        merge_checks, "schedule_org_recheck", lambda o: calls.append(f"o:{o}")
    )
    w = await make_world(db_session)
    org, tk = f"o:{w.org.id}", f"t:{w.ticket.id}"
    pr = PullRequest(
        id=f"pr_{uuid.uuid4().hex[:16]}", project_id=w.project.id, provider="github",
        repo="acme/r", number=7, title="Auth", state="open", branch="feat", head_sha=HEAD,
    )
    db_session.add(pr)
    await db_session.flush()
    db_session.add(TicketPrLink(ticket_id=w.ticket.id, pr_id=pr.id, link_source="manual"))
    await db_session.commit()

    body = {"name": "Auth changes", "target_kind": "merge", "filter": {},
            "requirements": [{"who": "org_role", "role": "admin", "count": 1}]}
    url = f"/api/v1/orgs/{w.org.id}/approval-policies"
    created = await client.post(url, headers=hdrs(w.admin_key), json=body)
    assert created.status_code == 201, created.text
    assert calls == [org]
    edited = await client.put(f"{url}/{created.json()['id']}", headers=hdrs(w.admin_key),
                              json={**body, "name": "Auth"})
    assert edited.status_code == 200 and calls == [org, org]

    rid = (await _readiness(client, w, w.owner_key))["approvals"][0]["request_id"]
    resp = await client.post(
        f"/api/v1/approval-requests/{rid}/decide", headers=hdrs(w.admin_key),
        json={"decision": "approve", "reason": ""},
    )
    assert resp.status_code == 200 and calls == [org, org, tk]

    resp = await client.delete(f"{url}/{edited.json()['id']}", headers=hdrs(w.admin_key))
    assert resp.status_code == 200 and calls == [org, org, tk, org]
    # Idempotent second delete changes nothing: no recheck.
    await client.delete(f"{url}/{edited.json()['id']}", headers=hdrs(w.admin_key))
    assert calls == [org, org, tk, org]
    # A KB policy never touches merge checks.
    await client.post(url, headers=hdrs(w.admin_key), json={
        **body, "target_kind": "kb_promote", "filter": KB_POLICY})
    assert calls == [org, org, tk, org]


@pytest.mark.asyncio
async def test_org_recheck_pages_through_every_open_pr_ticket(
    db_engine, db_session: AsyncSession, monkeypatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sessionfs.server.services import merge_checks

    w = await make_world(db_session)
    tickets = [w.ticket] + [await make_ticket(db_session, w.project, w.owner) for _ in range(6)]
    for i, t in enumerate(tickets):
        pr = PullRequest(
            id=f"pr_{uuid.uuid4().hex[:16]}", project_id=w.project.id, provider="github",
            repo="acme/r", number=100 + i, title="x", branch=f"b{i}", head_sha=HEAD,
            state="open" if i != 3 else "merged",
        )
        db_session.add(pr)
        await db_session.flush()
        db_session.add(TicketPrLink(ticket_id=t.id, pr_id=pr.id, link_source="manual"))
    await db_session.commit()
    seen: list[str] = []

    async def fake(db, ticket_id):
        seen.append(ticket_id)

    monkeypatch.setattr(merge_checks, "on_verdict_recorded", fake)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    n = await merge_checks.recheck_org(w.org.id, session_factory=factory, page_size=2)
    expected = sorted(t.id for i, t in enumerate(tickets) if i != 3)
    assert n == 6 and seen == expected  # every open-PR ticket, none dropped


@pytest.mark.asyncio
async def test_gitlab_status_reposted_after_policy_and_approval(
    client: AsyncClient, db_engine, db_session: AsyncSession, monkeypatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sessionfs.server import gitlab_client as gc
    from sessionfs.server.db.models import GitLabSettings, ProjectRepo
    from sessionfs.server.llm_keys import encrypt_secret
    from sessionfs.server.services import merge_checks

    monkeypatch.setattr(merge_checks, "schedule_recheck", lambda t: None)
    monkeypatch.setattr(merge_checks, "schedule_org_recheck", lambda o: None)
    posts: list[str] = []

    async def fake_post(self, project_id, sha, state, *, name, description, target_url=None):
        posts.append(state)
        return {"id": 1}

    monkeypatch.setattr(gc.GitLabClient, "post_commit_status", fake_post)
    w = await make_world(db_session)
    remote = w.project.git_remote_normalized
    db_session.add(ProjectRepo(
        id=f"repo_{uuid.uuid4().hex[:16]}", project_id=w.project.id,
        git_remote_normalized=remote, provider="gitlab", provider_repo_id="4242",
        is_primary=True,
    ))
    db_session.add(GitLabSettings(
        user_id=w.owner.id, instance_url="https://gitlab.example.com",
        encrypted_access_token=encrypt_secret("glpat-test"), webhook_secret="wh",
    ))
    pr = PullRequest(
        id=f"pr_{uuid.uuid4().hex[:16]}", project_id=w.project.id, provider="gitlab",
        repo=remote, number=9, title="mr", state="open", branch="feat", head_sha=HEAD,
    )
    db_session.add(pr)
    await db_session.flush()
    db_session.add(TicketPrLink(ticket_id=w.ticket.id, pr_id=pr.id, link_source="manual"))
    await db_session.commit()
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Without a policy the trusted-clean ticket is green.
    await merge_checks.recheck_org(w.org.id, session_factory=factory)
    assert posts == ["success"]

    # A blocking policy created over HTTP: the org recheck re-posts pending.
    resp = await client.post(
        f"/api/v1/orgs/{w.org.id}/approval-policies", headers=hdrs(w.admin_key),
        json={"name": "Auth", "target_kind": "merge", "filter": {},
              "requirements": [{"who": "org_role", "role": "admin", "count": 1}]},
    )
    assert resp.status_code == 201, resp.text
    await merge_checks.recheck_org(w.org.id, session_factory=factory)
    assert posts == ["success", "pending"]

    # Approval: the per-ticket recheck (what schedule_recheck runs) re-posts
    # success to GitLab.
    async with factory() as s:
        rid = (await svc.ticket_readiness(s, await s.get(Ticket, w.ticket.id), HEAD))[
            "approvals"][0]["request_id"]
        await s.commit()
    resp = await client.post(
        f"/api/v1/approval-requests/{rid}/decide", headers=hdrs(w.admin_key),
        json={"decision": "approve", "reason": ""},
    )
    assert resp.status_code == 200, resp.text
    async with factory() as s:
        await merge_checks.on_verdict_recorded(s, w.ticket.id)
        await s.commit()
    assert posts[-1] == "success" and len(posts) == 3


# ── 5. eligibility checks don't grow with the input ────────────────


async def _pending_requests(db: AsyncSession, w, n: int) -> list[ApprovalRequest]:
    reqs = []
    for _ in range(n):
        t = await make_ticket(db, w.project, w.implementer)
        await seed_implementer(db, w.project, t, user_id=w.implementer.id)
        await seed_clean(db, t, w.reviewer, HEAD)
        r = await svc.ticket_readiness(db, t, HEAD)
        await db.commit()
        reqs.append(await db.get(ApprovalRequest, r["approvals"][0]["request_id"]))
    return reqs


@pytest.mark.asyncio
async def test_query_counts_do_not_grow_linearly(db_engine, db_session: AsyncSession):
    w = await make_world(db_session)
    policy = await make_policy(db_session, w.org)
    reqs = await _pending_requests(db_session, w, 8)

    def _count(fn) -> int:
        async def run():
            with _Counter(db_engine) as c:
                await fn()
            return c.n
        return run

    small = await _count(lambda: svc.decidable_by(db_session, w.admin, reqs[:2]))()
    large = await _count(lambda: svc.decidable_by(db_session, w.admin, reqs))()
    assert large == small
    assert len(await svc.decidable_by(db_session, w.admin, reqs)) == 8

    for i in range(6):
        u, _ = await make_user(db_session, f"extra{i}")
        await add_member(db_session, w.org, u, "admin")
    req = reqs[0]
    few = await _count(
        lambda: svc.eligible_approver_ids(db_session, req, policy, w.project)
    )()
    for i in range(10):
        u, _ = await make_user(db_session, f"more{i}")
        await add_member(db_session, w.org, u, "admin")
    many = await _count(
        lambda: svc.eligible_approver_ids(db_session, req, policy, w.project)
    )()
    assert many == few
    assert len(await svc.eligible_approver_ids(db_session, req, policy, w.project)) == 19

    notes = (
        await db_session.execute(
            select(TicketNotification).where(
                TicketNotification.user_id == w.admin.id,
                TicketNotification.subject_kind == "approval",
            )
        )
    ).scalars().all()
    assert len(notes) == 8
    two = await _count(
        lambda: svc.approval_notification_subjects(db_session, w.admin, list(notes[:2]))
    )()
    eight = await _count(
        lambda: svc.approval_notification_subjects(db_session, w.admin, list(notes))
    )()
    assert eight == two
    assert len(await svc.approval_notification_subjects(db_session, w.admin, list(notes))) == 8


@pytest.mark.asyncio
async def test_notification_subjects_are_capped(db_session: AsyncSession, monkeypatch):
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    await _pending_requests(db_session, w, 3)
    monkeypatch.setattr(svc, "MAX_NOTIFICATION_SUBJECTS", 2)
    notes = (
        await db_session.execute(
            select(TicketNotification).where(TicketNotification.user_id == w.admin.id)
        )
    ).scalars().all()
    out = await svc.approval_notification_subjects(db_session, w.admin, list(notes))
    newest = sorted(n.id for n in notes)[-2:]
    assert sorted(out) == newest


@pytest.mark.asyncio
async def test_load_loops_policy_queries_constant(db_engine, db_session: AsyncSession):
    from sessionfs.server.services.loops import load_loops

    w = await make_world(db_session)
    await make_policy(db_session, w.org, name="Elsewhere",
                      flt={"project_ids": [w.project.id], "ticket_kind": "issue"})
    loop = Ticket(id=f"tk_{uuid.uuid4().hex[:16]}", project_id=w.project.id,
                  title="loop", kind="issue", loop_config="{}",
                  created_by_user_id=w.owner.id)
    db_session.add(loop)
    await db_session.commit()

    async def _add_children(n: int) -> None:
        for _ in range(n):
            db_session.add(Ticket(
                id=f"tk_{uuid.uuid4().hex[:16]}", project_id=w.project.id, title="c",
                parent_ticket_id=loop.id, created_by_user_id=w.owner.id,
            ))
        await db_session.commit()

    async def _queries() -> int:
        with _Counter(db_engine) as c:
            await load_loops(db_session, loop_tickets=[loop], show_dollars=False)
        return c.n

    await _add_children(2)
    small = await _queries()
    await _add_children(6)
    assert await _queries() == small


# ── 7. terminal requests can be raised again ───────────────────────


@pytest.mark.asyncio
async def test_rejected_kb_request_reraised_on_explicit_promote(
    client: AsyncClient, db_session: AsyncSession
):
    w = await make_world(db_session)
    await make_policy(db_session, w.org, name="Compliance", target_kind="kb_promote",
                      flt=KB_POLICY)
    entry = KnowledgeEntry(
        project_id=w.project.id, session_id="manual", user_id=w.owner.id,
        entry_type="decision", content=CONTENT, confidence=0.9, claim_class="note",
        entity_ref="docs/compliance/phi.md", freshness_class="current",
    )
    db_session.add(entry)
    await db_session.commit()
    url = f"/api/v1/projects/{w.project.id}/entries/{entry.id}/promote"
    assert (await client.put(url, headers=hdrs(w.owner_key))).status_code == 202
    (req,) = (await db_session.execute(select(ApprovalRequest))).scalars().all()
    await svc.decide(db_session, request_id=req.id, approver=w.admin,
                     decision="reject", reason="no")
    await db_session.commit()

    # The automatic path (compile) leaves the rejection standing...
    gate = await svc.kb_promotion_gate(db_session, entry, None)
    await db_session.commit()
    assert gate.blocked and gate.approvals[0]["state"] == "rejected"
    # ...a person's explicit promote raises a fresh request beside it.
    resp = await client.put(url, headers=hdrs(w.owner_key))
    assert resp.status_code == 202
    assert resp.json()["approvals"][0]["state"] == "pending"
    rows = (
        await db_session.execute(
            select(ApprovalRequest.state).order_by(ApprovalRequest.requested_at)
        )
    ).scalars().all()
    assert rows == ["rejected", "pending"]


@pytest.mark.asyncio
async def test_expired_merge_request_reraised(db_session: AsyncSession):
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    r = await svc.ticket_readiness(db_session, w.ticket, HEAD)
    await db_session.commit()
    rid = r["approvals"][0]["request_id"]
    req = await db_session.get(ApprovalRequest, rid)
    req.expires_at = _now() - timedelta(minutes=1)
    await db_session.commit()
    r = await svc.ticket_readiness(db_session, w.ticket, HEAD)
    await db_session.commit()
    (appr,) = r["approvals"]
    assert appr["state"] == "pending" and appr["request_id"] != rid
    assert (await _fresh(db_session, ApprovalRequest, rid)).state == "expired"
    assert r["reasons"][-1] == "Needs approval (0 of 1)"


@pytest.mark.asyncio
async def test_superseded_reason_is_accurate(db_session: AsyncSession):
    w = await make_world(db_session)
    policy = await make_policy(db_session, w.org)
    r = await svc.ticket_readiness(db_session, w.ticket, HEAD)
    await db_session.commit()
    rid = r["approvals"][0]["request_id"]
    req = await db_session.get(ApprovalRequest, rid)
    req.state = "superseded"
    await db_session.commit()
    out = await svc.evaluate_approvals(db_session, w.ticket, HEAD, create=False)
    assert out["blocking_pending"] == [
        "Approval superseded (it is requested again once the review is clean)"
    ]
    db_session.add(AgentRun(
        id=f"run_{uuid.uuid4().hex[:12]}", project_id=w.project.id, ticket_id=w.ticket.id,
        persona_name="atlas", tool="codex", trigger_source="manual", status="passed",
        role="implementer",
        head_sha=HEAD, findings="[]",
    ))
    await db_session.commit()
    ok, reason = await svc.workflow_gate_passed(db_session, policy_id=policy.id, ticket=w.ticket)
    assert not ok and reason.startswith("Approval superseded: Auth changes")


# ── 8. decide re-checks the project under the lock ─────────────────


@pytest.mark.asyncio
async def test_decide_404_when_project_left_the_org(db_session: AsyncSession):
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    rid = (await svc.ticket_readiness(db_session, w.ticket, HEAD))["approvals"][0]["request_id"]
    await db_session.commit()
    other = await make_org(db_session)
    await db_session.execute(
        update(Project).where(Project.id == w.project.id).values(org_id=other.id)
    )
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await svc.decide(db_session, request_id=rid, approver=w.admin,
                         decision="approve", reason="")
    assert exc.value.status_code == 404 and exc.value.detail == svc.REQUEST_NOT_FOUND


@pytest.mark.asyncio
async def test_decide_404_when_approver_lost_read_access(db_session: AsyncSession):
    from sessionfs.server.db.models import Organization

    w = await make_world(db_session)
    await make_policy(db_session, w.org, requirements=[
        {"who": "users", "user_ids": [w.reviewer.id], "count": 1}])
    rid = (await svc.ticket_readiness(db_session, w.ticket, HEAD))["approvals"][0]["request_id"]
    await db_session.commit()
    # Restricted visibility: a plain member without a group grant can't read.
    org = await db_session.get(Organization, w.org.id)
    org.settings = json.dumps({"general": {"restrict_project_visibility": True}})
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await svc.decide(db_session, request_id=rid, approver=w.reviewer,
                         decision="approve", reason="")
    assert exc.value.status_code == 404


# ── 9. no policy names for the merge check or non-members ──────────


@pytest.mark.asyncio
async def test_readiness_reasons_generic_for_non_members(
    client: AsyncClient, db_session: AsyncSession
):
    w = await make_world(db_session)
    await make_policy(db_session, w.org, name="Secret auth rewrite")
    member = await _readiness(client, w, w.owner_key)
    assert "Waiting on approval: Secret auth rewrite (0 of 1)" in member["reasons"]
    assert member["approvals"][0]["policy_name"] == "Secret auth rewrite"

    _, reader_key = await _captured_reader(db_session, w.project)
    _, svc_key = await make_service_key(db_session, w.owner, w.org, ["tickets:read"])
    for key in (reader_key, svc_key):
        resp = await client.get(
            f"/api/v1/projects/{w.project.id}/tickets/{w.ticket.id}/readiness",
            headers=hdrs(key), params={"sha": HEAD},
        )
        assert resp.status_code == 200, resp.text
        assert "Secret auth rewrite" not in resp.text
        assert "Needs approval (0 of 1)" in resp.json()["reasons"]

    # The shared function the GitHub check and GitLab status use is generic.
    plain = await svc.ticket_readiness(db_session, w.ticket, HEAD)
    assert "Secret auth rewrite" not in json.dumps(plain)
    from sessionfs.server.services.merge_checks import _check_run_summary

    summary = _check_run_summary(plain["ready"], plain["reasons"])
    assert "Needs approval (0 of 1)" in summary and "Secret" not in summary


# ── 10. requirement counts are strict ints ─────────────────────────


@pytest.mark.parametrize("count", [True, "1", 1.0])
def test_requirement_count_is_strict(count) -> None:
    for req in (
        {"who": "org_role", "role": "admin", "count": count},
        {"who": "users", "user_ids": ["u_1"], "count": count},
    ):
        with pytest.raises(HTTPException) as exc:
            svc.validate_policy_body("merge", {}, [req])
        assert exc.value.status_code == 422


# ── 11. decisions outlive their approver ───────────────────────────


@pytest.mark.asyncio
async def test_former_member_decision_counts_toward_quorum(
    client: AsyncClient, db_session: AsyncSession
):
    w = await make_world(db_session)
    await make_policy(db_session, w.org, requirements=[
        {"who": "org_role", "role": "admin", "count": 2}])
    rid = (await _readiness(client, w, w.owner_key))["approvals"][0]["request_id"]
    await svc.decide(db_session, request_id=rid, approver=w.admin, decision="approve",
                     reason="first")
    await db_session.commit()
    # The approver's user row goes away (ON DELETE SET NULL on PostgreSQL).
    await db_session.execute(
        update(ApprovalDecision).where(ApprovalDecision.request_id == rid)
        .values(approver_user_id=None)
    )
    await db_session.execute(
        OrgMember.__table__.delete().where(OrgMember.user_id == w.admin.id)
    )
    await db_session.commit()

    view = (await client.get(f"/api/v1/approval-requests/{rid}",
                             headers=hdrs(w.owner_key))).json()
    (d,) = view["decisions"]
    assert d["approver_user_id"] is None and d["approver_name"] == "former member"
    assert view["approved_count"] == 1

    req = await svc.decide(db_session, request_id=rid, approver=w.admin2,
                           decision="approve", reason="second")
    await db_session.commit()
    assert req.state == "approved"
    log = (await client.get(f"/api/v1/orgs/{w.org.id}/approval-log",
                            headers=hdrs(w.owner_key))).json()["items"]
    assert {i["approver_name"] for i in log} == {"former member", "Admintwo"}

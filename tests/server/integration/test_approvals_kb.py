"""Approval policies A2: the KB promotion gate on every promote path
(manual, bulk, add-as-claim, auto-promotion at compile), approval release,
content-changed supersede and author self-approval (GitHub #123)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    ApprovalRequest,
    KnowledgeEntry,
    TicketNotification,
    User,
)
from sessionfs.server.services import approvals as svc
from sessionfs.server.services.bulk_promote import promote_eligible_notes
from sessionfs.server.services.compiler import compile_project_context
from tests.server.integration.approvals_helpers import (
    hdrs,
    make_policy,
    make_service_key,
    make_world,
)

CONTENT = (
    "Decision: PHI fields in src/sessionfs/server/models.py are encrypted with "
    "the per-org key before they are written."
)
KB_POLICY = {"entity_ref_prefix_any": ["docs/compliance/"]}


async def _entry(
    db: AsyncSession, w, *, author, claim_class: str = "note",
    entity_ref: str = "docs/compliance/phi.md", content: str = CONTENT,
    service_key_id: str | None = None, confidence: float = 0.9,
) -> KnowledgeEntry:
    entry = KnowledgeEntry(
        project_id=w.project.id,
        session_id="manual",
        user_id=author.id,
        entry_type="decision",
        content=content,
        confidence=confidence,
        claim_class=claim_class,
        entity_ref=entity_ref,
        freshness_class="current",
        actor_type="service_key" if service_key_id else "user",
        service_key_id=service_key_id,
    )
    db.add(entry)
    await db.commit()
    return entry


async def _kb_policy(db: AsyncSession, w, **kw):
    return await make_policy(
        db, w.org, name=kw.pop("name", "Compliance"), target_kind="kb_promote",
        flt=kw.pop("flt", KB_POLICY), **kw,
    )


async def _requests(db: AsyncSession, entry: KnowledgeEntry | int) -> list[ApprovalRequest]:
    eid = entry if isinstance(entry, int) else entry.id
    return list(
        (
            await db.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.target_kind == "kb_promote",
                    ApprovalRequest.target_id == str(eid),
                )
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )


async def _reload(db: AsyncSession, entry: KnowledgeEntry) -> KnowledgeEntry:
    return (
        await db.execute(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.id == entry.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


def _promote_url(w, entry) -> str:
    return f"/api/v1/projects/{w.project.id}/entries/{entry.id}/promote"


# ── manual promote ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_manual_promote_held_then_released_on_approval(
    client: AsyncClient, db_session: AsyncSession
):
    w = await make_world(db_session)
    await _kb_policy(db_session, w)
    entry = await _entry(db_session, w, author=w.owner)

    resp = await client.put(_promote_url(w, entry), headers=hdrs(w.owner_key))
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["claim_class"] == "note" and body["approval_pending"] is True
    assert body["approval_reasons"] == ["Waiting on approval: Compliance (0 of 1)"]
    (appr,) = body["approvals"]
    assert appr["state"] == "pending" and appr["required_count"] == 1

    (req,) = await _requests(db_session, entry)
    assert req.target_fingerprint == svc.entry_fingerprint(CONTENT)
    # A second attempt loads the same request instead of making another.
    resp = await client.put(_promote_url(w, entry), headers=hdrs(w.owner_key))
    assert resp.status_code == 202
    assert len(await _requests(db_session, entry)) == 1

    # Both admins (not the author) are notified; the author is not.
    rows = (
        await db_session.execute(
            select(TicketNotification).where(TicketNotification.subject_kind == "approval")
        )
    ).scalars().all()
    assert {r.user_id for r in rows} == {w.admin.id, w.admin2.id}
    assert {r.kind for r in rows} == {"approval_requested"}

    await svc.decide(db_session, request_id=req.id, approver=w.admin,
                     decision="approve", reason="Checked.")
    await db_session.commit()
    entry = await _reload(db_session, entry)
    assert entry.claim_class == "claim" and entry.approval_pending is False
    assert entry.promoted_by == w.admin.id
    decided = (
        await db_session.execute(
            select(TicketNotification).where(TicketNotification.kind == "approval_decided")
        )
    ).scalars().all()
    assert [d.user_id for d in decided] == [w.owner.id]


@pytest.mark.asyncio
async def test_content_change_supersedes_instead_of_promoting(db_session: AsyncSession):
    w = await make_world(db_session)
    await _kb_policy(db_session, w)
    entry = await _entry(db_session, w, author=w.owner)
    gate = await svc.kb_promotion_gate(db_session, entry, w.owner.id)
    await db_session.commit()
    assert gate.blocked
    (req,) = await _requests(db_session, entry)

    entry.content = CONTENT + " Rotated yearly."
    await db_session.commit()
    await svc.decide(db_session, request_id=req.id, approver=w.admin,
                     decision="approve", reason="ok")
    await db_session.commit()
    (req,) = await _requests(db_session, entry)
    assert req.state == "superseded"
    entry = await _reload(db_session, entry)
    assert entry.claim_class == "note" and entry.approval_pending is True

    # A fresh attempt on the new content raises a new request.
    gate = await svc.kb_promotion_gate(db_session, entry, w.owner.id)
    await db_session.commit()
    assert gate.blocked and gate.approvals[0]["state"] == "pending"
    assert len(await _requests(db_session, entry)) == 2


@pytest.mark.asyncio
async def test_author_cannot_approve(db_session: AsyncSession):
    w = await make_world(db_session)
    await _kb_policy(db_session, w)
    entry = await _entry(db_session, w, author=w.admin)
    await svc.kb_promotion_gate(db_session, entry, w.admin.id)
    await db_session.commit()
    (req,) = await _requests(db_session, entry)
    eid = entry.id
    ids = (w.admin.id, w.admin2.id)
    with pytest.raises(HTTPException) as exc:
        await svc.decide(db_session, request_id=req.id, approver=w.admin,
                         decision="approve", reason="mine")
    assert exc.value.detail["code"] == "self_approval_forbidden"
    await db_session.rollback()
    (req,) = await _requests(db_session, eid)
    admin, admin2 = [await db_session.get(User, i) for i in ids]
    assert not await svc.decidable_by(db_session, admin, [req])
    assert await svc.decidable_by(db_session, admin2, [req])


@pytest.mark.asyncio
async def test_author_cannot_approve_through_own_service_key(db_session: AsyncSession):
    w = await make_world(db_session)
    await _kb_policy(db_session, w)
    # The key belongs to a member but was minted by admin, so admin is part
    # of the author identity too.
    key, _ = await make_service_key(db_session, w.admin, w.org, ["knowledge:write"])
    entry = await _entry(db_session, w, author=w.admin, service_key_id=key.id)
    await svc.kb_promotion_gate(db_session, entry, None)
    await db_session.commit()
    (req,) = await _requests(db_session, entry)
    eid = entry.id
    admin2_id = w.admin2.id
    with pytest.raises(HTTPException) as exc:
        await svc.decide(db_session, request_id=req.id, approver=w.admin,
                         decision="approve", reason="mine")
    assert exc.value.detail["code"] == "self_approval_forbidden"
    await db_session.rollback()
    (req,) = await _requests(db_session, eid)
    admin2 = await db_session.get(User, admin2_id)
    assert await svc.decidable_by(db_session, admin2, [req])


@pytest.mark.asyncio
async def test_unresolvable_key_author_fails_closed(db_session: AsyncSession):
    w = await make_world(db_session)
    await _kb_policy(db_session, w)
    entry = await _entry(db_session, w, author=w.implementer, service_key_id="sk_gone")
    await svc.kb_promotion_gate(db_session, entry, None)
    await db_session.commit()
    (req,) = await _requests(db_session, entry)
    rid = req.id
    for approver_id in (w.admin.id, w.admin2.id):
        approver = await db_session.get(User, approver_id)
        with pytest.raises(HTTPException) as exc:
            await svc.decide(db_session, request_id=rid, approver=approver,
                             decision="approve", reason="?")
        assert exc.value.detail["code"] == "self_approval_forbidden"
        await db_session.rollback()
    # Nobody can decide, so nobody is notified.
    assert (
        await db_session.execute(
            select(TicketNotification).where(TicketNotification.subject_kind == "approval")
        )
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_advisory_kb_policy_only_warns(client: AsyncClient, db_session: AsyncSession):
    w = await make_world(db_session)
    await _kb_policy(db_session, w, name="FYI", blocking=False)
    entry = await _entry(db_session, w, author=w.owner)
    resp = await client.put(_promote_url(w, entry), headers=hdrs(w.owner_key))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["claim_class"] == "claim" and body["approval_pending"] is False
    assert body["warnings"] == ["Advisory approval policy: FYI"]
    assert await _requests(db_session, entry) == []


@pytest.mark.asyncio
async def test_non_matching_entry_is_not_held(client: AsyncClient, db_session: AsyncSession):
    w = await make_world(db_session)
    await _kb_policy(db_session, w)
    entry = await _entry(db_session, w, author=w.owner, entity_ref="src/app.py")
    resp = await client.put(_promote_url(w, entry), headers=hdrs(w.owner_key))
    assert resp.status_code == 200 and resp.json()["claim_class"] == "claim"


@pytest.mark.asyncio
async def test_add_entry_as_claim_is_held(client: AsyncClient, db_session: AsyncSession):
    w = await make_world(db_session)
    await _kb_policy(db_session, w)
    resp = await client.post(
        f"/api/v1/projects/{w.project.id}/entries/add",
        headers=hdrs(w.owner_key),
        json={"content": CONTENT, "entry_type": "decision", "confidence": 0.95,
              "entity_ref": "docs/compliance/phi.md"},
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["claim_class"] == "note" and body["approval_pending"] is True
    entry = (
        await db_session.execute(select(KnowledgeEntry).where(KnowledgeEntry.id == body["id"]))
    ).scalar_one()
    assert entry.approval_pending is True
    assert len(await _requests(db_session, entry)) == 1


# ── bulk promote ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_promote_gated(db_session: AsyncSession):
    w = await make_world(db_session)
    await _kb_policy(db_session, w)
    held = await _entry(db_session, w, author=w.owner)
    free = await _entry(
        db_session, w, author=w.owner, entity_ref="src/x.py",
        content="Pattern: every route in src/sessionfs/server/routes/x.py takes the scope lock.",
    )

    dry = await promote_eligible_notes(
        db_session, w.project.id, user_id=w.owner.id, dry_run=True, project=w.project
    )
    assert dry.approval_pending_ids == [held.id] and dry.promoted_ids == [free.id]
    assert dry.reasons.get("approval_pending") == 1
    assert await _requests(db_session, held) == []  # a dry run writes nothing

    real = await promote_eligible_notes(
        db_session, w.project.id, user_id=w.owner.id, dry_run=False, project=w.project
    )
    assert real.approval_pending_ids == [held.id] and real.promoted_ids == [free.id]
    held = await _reload(db_session, held)
    assert held.claim_class == "note" and held.approval_pending is True
    (req,) = await _requests(db_session, held)

    await svc.decide(db_session, request_id=req.id, approver=w.admin2,
                     decision="approve", reason="ok")
    await db_session.commit()
    assert (await _reload(db_session, held)).claim_class == "claim"


# ── auto-promotion at compile ──────────────────────────────────────


@pytest.mark.asyncio
async def test_compile_auto_promotion_respects_blocking_policy(db_session: AsyncSession):
    w = await make_world(db_session)
    await _kb_policy(db_session, w)
    held = await _entry(db_session, w, author=w.owner, claim_class="evidence", confidence=0.6)
    free = await _entry(
        db_session, w, author=w.owner, claim_class="evidence", confidence=0.6,
        entity_ref="src/y.py",
        content="Discovery: src/sessionfs/server/services/y.py retries the upload twice.",
    )

    await compile_project_context(w.project.id, w.owner.id, db_session)
    held = await _reload(db_session, held)
    free = await _reload(db_session, free)
    assert free.claim_class == "claim"
    assert held.claim_class == "evidence" and held.approval_pending is True
    assert held.compiled_at is None
    (req,) = await _requests(db_session, held)
    await db_session.refresh(w.project)
    assert "PHI fields" not in (w.project.context_document or "")

    # A second compile does not raise a second request.
    await compile_project_context(w.project.id, w.owner.id, db_session)
    assert len(await _requests(db_session, held)) == 1

    await svc.decide(db_session, request_id=req.id, approver=w.admin,
                     decision="approve", reason="ok")
    await db_session.commit()
    held = await _reload(db_session, held)
    assert held.claim_class == "claim" and held.approval_pending is False
    await compile_project_context(w.project.id, w.owner.id, db_session)
    await db_session.refresh(w.project)
    assert "PHI fields" in (w.project.context_document or "")


@pytest.mark.asyncio
async def test_personal_project_has_no_kb_policies(db_session: AsyncSession):
    from tests.server.integration.approvals_helpers import make_project

    w = await make_world(db_session)
    await _kb_policy(db_session, w)
    w.project = await make_project(db_session, w.owner, None)
    entry = await _entry(db_session, w, author=w.owner)
    gate = await svc.kb_promotion_gate(db_session, entry, w.owner.id)
    assert not gate.blocked and gate.warnings == []

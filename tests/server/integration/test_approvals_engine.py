"""Approval policies engine (GitHub #123, A1): validation, matching, requests
and decisions, exercised directly against services/approvals.py."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionfs.server.db.models import (
    ApprovalDecision,
    ApprovalRequest,
    Ticket,
    User,
)
from sessionfs.server.services import approvals as svc
from tests.server.integration.approvals_helpers import (
    raised_readiness,
    HEAD,
    NEXT,
    make_policy,
    make_service_key,
    make_ticket,
    make_world,
    add_member,
    seed_clean,
    seed_implementer,
)


# ── validation ─────────────────────────────────────────────────────


def test_validate_policy_body_accepts_and_normalises() -> None:
    flt, reqs = svc.validate_policy_body(
        "merge",
        {"finding_harm_any": ["data-exposed", "data-exposed"], "priority_at_least": "critical"},
        [{"who": "org_role", "role": "admin", "count": 1},
         {"who": "users", "user_ids": ["u1", "u2"], "count": 2}],
    )
    assert flt["priority_at_least"] == "critical"
    assert "project_ids" not in flt  # defaults are not stored
    assert reqs[1] == {"who": "users", "user_ids": ["u1", "u2"], "count": 2}


@pytest.mark.parametrize(
    ("kind", "flt", "reqs", "code"),
    [
        ("ticket_close", {}, [{"who": "org_role", "role": "admin", "count": 1}], "invalid_target_kind"),
        ("merge", {"nope": 1}, [{"who": "org_role", "role": "admin", "count": 1}], "invalid_filter"),
        ("merge", {"entry_type_any": ["bug"]}, [{"who": "org_role", "role": "admin", "count": 1}], "invalid_filter"),
        ("kb_promote", {"finding_harm_any": ["crash"]}, [{"who": "org_role", "role": "admin", "count": 1}], "invalid_filter"),
        ("merge", {"priority_at_least": "urgent"}, [{"who": "org_role", "role": "admin", "count": 1}], "invalid_filter"),
        ("merge", {"checklist_class_any": ["Not A Slug"]}, [{"who": "org_role", "role": "admin", "count": 1}], "invalid_filter"),
        ("merge", {"project_ids": ["x" * 3000, "y" * 3000, "z" * 3000]}, [{"who": "org_role", "role": "admin", "count": 1}], "filter_too_large"),
        ("merge", {}, [], "invalid_requirements"),
        ("merge", {}, [{"who": "org_role", "role": "member", "count": 1}], "invalid_requirements"),
        ("merge", {}, [{"who": "users", "user_ids": ["a"], "count": 2}], "invalid_requirements"),
        ("merge", {}, [{"who": "org_role", "role": "admin", "count": 0}], "invalid_requirements"),
        ("merge", {}, [{"who": "robots", "count": 1}], "invalid_requirements"),
    ],
)
def test_validate_policy_body_rejects(kind, flt, reqs, code) -> None:
    with pytest.raises(HTTPException) as exc:
        svc.validate_policy_body(kind, flt, reqs)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == code


# ── matching ───────────────────────────────────────────────────────


def test_merge_matching_scopes_and_triggers() -> None:
    target = {"project_id": "p1", "priority": "high", "kind": "task",
              "harms": {"data-exposed"}, "classes": set()}
    m = svc._matches_merge
    assert m({}, target)
    assert not m({"project_ids": ["p2"]}, target)
    assert not m({"ticket_kind": "issue"}, target)
    assert m({"priority_at_least": "high"}, target)
    assert not m({"priority_at_least": "critical"}, target)
    # Triggers are OR-ed: harm matches even though priority doesn't.
    assert m({"priority_at_least": "critical", "finding_harm_any": ["data-exposed"]}, target)
    assert not m({"checklist_class_any": ["sql-injection"]}, target)


def test_kb_matching_requires_every_key() -> None:
    target = {"project_id": "p1", "entry_type": "decision", "entity_ref": "docs/compliance/phi"}
    m = svc._matches_kb
    assert m({"entry_type_any": ["decision"], "entity_ref_prefix_any": ["docs/compliance/"]}, target)
    assert not m({"entry_type_any": ["bug"], "entity_ref_prefix_any": ["docs/compliance/"]}, target)
    assert not m({"entity_ref_prefix_any": ["src/"]}, target)


@pytest.mark.asyncio
async def test_matching_policies_uses_ticket_findings(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    harm_policy = await make_policy(db_session, w.org, flt={"finding_harm_any": ["data-exposed"]})
    await make_policy(db_session, w.org, name="Any", flt={})
    await seed_clean(
        db_session, w.ticket, w.reviewer, HEAD,
        content="Codex R2 review on tk_x: CHANGES_REQUESTED\n\nFindings:\n"
                "- HIGH — leaks token harm: data-exposed\n",
    )
    target = await svc.merge_target(db_session, w.ticket)
    assert "data-exposed" in target["harms"]
    names = [p.name for p in await svc.matching_policies(db_session, w.org.id, "merge", target)]
    assert harm_policy.name in names and "Any" in names


# ── evaluate ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_readiness_waits_then_turns_ready_after_quorum(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    r = await raised_readiness(db_session, w.ticket, HEAD, show_names=True)
    await db_session.commit()
    assert r["ready"] is False
    assert "Waiting on approval: Auth changes (0 of 1)" in r["reasons"]
    (appr,) = r["approvals"]
    assert appr["state"] == "pending" and appr["required_count"] == 1
    await svc.decide(db_session, request_id=appr["request_id"], approver=w.admin,
                     decision="approve", reason="ok")
    await db_session.commit()
    r = await raised_readiness(db_session, w.ticket, HEAD)
    assert r["ready"] is True
    assert r["approvals"][0]["state"] == "approved"


@pytest.mark.asyncio
async def test_no_request_before_the_review_is_clean(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    r = await raised_readiness(db_session, w.ticket, NEXT)  # clean is bound to HEAD
    assert r["ready"] is False
    assert r["approvals"][0]["state"] == "not_requested"
    assert r["approvals"][0]["request_id"] is None
    # The trigger raised the one request the work can need: at the commit the
    # clean verdict is bound to, never at the sha a reader asked about.
    rows = (await db_session.execute(select(ApprovalRequest))).scalars().all()
    assert [row.target_fingerprint for row in rows] == [HEAD]


@pytest.mark.asyncio
async def test_advisory_policy_only_warns(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    await make_policy(db_session, w.org, name="FYI", blocking=False)
    r = await raised_readiness(db_session, w.ticket, HEAD, show_names=True)
    assert r["ready"] is True
    assert r["warnings"] == ["Waiting on approval: FYI (0 of 1)"]
    # Without names (the merge check, non-member readers): generic wording.
    r = await raised_readiness(db_session, w.ticket, HEAD)
    assert r["warnings"] == ["Needs approval (0 of 1)"]
    assert r["approvals"][0]["policy_name"] is None


@pytest.mark.asyncio
async def test_personal_project_has_no_policies(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    from tests.server.integration.approvals_helpers import make_project

    personal = await make_project(db_session, w.owner, None)
    t = await make_ticket(db_session, personal, w.implementer)
    out = await svc.evaluate_approvals(db_session, t, HEAD)
    assert out == {"blocking_pending": [], "warnings": [], "approvals": []}


@pytest.mark.asyncio
async def test_new_sha_creates_new_request_and_supersedes_old(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    first = await raised_readiness(db_session, w.ticket, HEAD)
    await db_session.commit()
    await seed_clean(db_session, w.ticket, w.reviewer, NEXT)
    second = await raised_readiness(db_session, w.ticket, NEXT)
    await db_session.commit()
    assert first["approvals"][0]["request_id"] != second["approvals"][0]["request_id"]
    old = await db_session.get(ApprovalRequest, first["approvals"][0]["request_id"])
    await db_session.refresh(old)
    assert old.state == "superseded"
    with pytest.raises(HTTPException) as exc:
        await svc.decide(db_session, request_id=old.id, approver=w.admin,
                         decision="approve", reason="")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_concurrent_readiness_reads_create_one_request(db_engine, db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def read() -> str:
        async with factory() as s:
            t = await s.get(Ticket, w.ticket.id)
            r = await raised_readiness(s, t, HEAD)
            await s.commit()
            return r["approvals"][0]["request_id"]

    ids = await asyncio.gather(*(read() for _ in range(4)))
    assert len(set(ids)) == 1
    rows = (await db_session.execute(select(ApprovalRequest))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_pending_request_expires_lazily(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    r = await raised_readiness(db_session, w.ticket, HEAD)
    req = await db_session.get(ApprovalRequest, r["approvals"][0]["request_id"])
    req.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()
    old_id = req.id
    r = await raised_readiness(db_session, w.ticket, HEAD)
    await db_session.commit()
    # The expired request stays as history and a fresh one is raised.
    assert (await db_session.get(ApprovalRequest, old_id)).state == "expired"
    assert r["approvals"][0]["state"] == "pending"
    assert r["approvals"][0]["request_id"] != old_id
    assert r["ready"] is False
    # A read-only evaluation loads the fresh request without raising another.
    other = await svc.evaluate_approvals(db_session, w.ticket, HEAD, create=False)
    assert other["approvals"][0]["state"] == "pending"


@pytest.mark.asyncio
async def test_edited_policy_keeps_request_on_its_version(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    v1 = await make_policy(db_session, w.org)
    r = await raised_readiness(db_session, w.ticket, HEAD)
    await db_session.commit()
    v1.active = False
    from sessionfs.server.db.models import ApprovalPolicy

    v2 = ApprovalPolicy(
        id="ap_v2" + v1.id[5:], org_id=w.org.id, lineage_id=v1.lineage_id, name=v1.name,
        target_kind="merge", filter="{}", version=2, supersedes_id=v1.id,
        requirements=json.dumps([{"who": "org_role", "role": "admin", "count": 2}]),
    )
    db_session.add(v2)
    await db_session.commit()
    again = await raised_readiness(db_session, w.ticket, HEAD)
    assert again["approvals"][0]["request_id"] == r["approvals"][0]["request_id"]
    assert again["approvals"][0]["required_count"] == 1  # still v1's requirements


# ── decide ─────────────────────────────────────────────────────────


async def _pending(db: AsyncSession, w, **policy) -> str:
    await make_policy(db, w.org, **policy)
    r = await raised_readiness(db, w.ticket, HEAD)
    await db.commit()
    return r["approvals"][0]["request_id"]


@pytest.mark.asyncio
async def test_quorum_across_two_requirements(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    rid = await _pending(db_session, w, requirements=[
        {"who": "org_role", "role": "owner", "count": 1},
        {"who": "users", "user_ids": [w.admin.id, w.admin2.id], "count": 2},
    ])
    req = await svc.decide(db_session, request_id=rid, approver=w.admin,
                           decision="approve", reason="")
    assert req.state == "pending"
    req = await svc.decide(db_session, request_id=rid, approver=w.owner,
                           decision="approve", reason="")
    assert req.state == "pending"  # owner fills requirement 0; users still 1 of 2
    req = await svc.decide(db_session, request_id=rid, approver=w.admin2,
                           decision="approve", reason="")
    assert req.state == "approved"
    await db_session.commit()
    r = await raised_readiness(db_session, w.ticket, HEAD)
    assert r["ready"] is True and r["approvals"][0]["approved_count"] == 3


@pytest.mark.asyncio
async def test_one_reject_rejects(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    rid = await _pending(db_session, w, requirements=[{"who": "org_role", "role": "admin", "count": 2}])
    await svc.decide(db_session, request_id=rid, approver=w.admin, decision="approve", reason="")
    req = await svc.decide(db_session, request_id=rid, approver=w.admin2, decision="reject",
                           reason="no")
    assert req.state == "rejected"
    await db_session.commit()
    r = await raised_readiness(db_session, w.ticket, HEAD, show_names=True)
    assert r["ready"] is False and "Approval rejected: Auth changes" in r["reasons"]
    # A rejection stands for this exact commit: no new request is raised.
    assert r["approvals"][0]["request_id"] == rid


@pytest.mark.asyncio
async def test_double_vote_refused(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    rid = await _pending(db_session, w, requirements=[{"who": "org_role", "role": "admin", "count": 2}])
    await svc.decide(db_session, request_id=rid, approver=w.admin, decision="approve", reason="")
    with pytest.raises(HTTPException) as exc:
        await svc.decide(db_session, request_id=rid, approver=w.admin, decision="approve", reason="")
    assert exc.value.status_code == 409 and exc.value.detail["code"] == "already_decided"
    rows = (await db_session.execute(select(ApprovalDecision))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_member_without_fit_refused(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    rid = await _pending(db_session, w)
    with pytest.raises(HTTPException) as exc:
        await svc.decide(db_session, request_id=rid, approver=w.reviewer, decision="approve", reason="")
    assert exc.value.detail["code"] == "not_an_approver"


@pytest.mark.asyncio
async def test_implementer_refused(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    rid = await _pending(db_session, w, requirements=[
        {"who": "users", "user_ids": [w.implementer.id, w.admin.id], "count": 1}])
    with pytest.raises(HTTPException) as exc:
        await svc.decide(db_session, request_id=rid, approver=w.implementer, decision="approve", reason="")
    assert exc.value.detail["code"] == "self_approval_forbidden"


@pytest.mark.asyncio
async def test_implementer_via_minted_key_refused(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    key, _ = await make_service_key(db_session, w.admin, w.org, ["tickets:write"])
    await seed_implementer(db_session, w.project, w.ticket, user_id=None, service_key_id=key.id)
    rid = await _pending(db_session, w)
    with pytest.raises(HTTPException) as exc:
        await svc.decide(db_session, request_id=rid, approver=w.admin, decision="approve", reason="")
    assert exc.value.detail["code"] == "self_approval_forbidden"
    # Another admin is independent and may approve.
    req = await svc.decide(db_session, request_id=rid, approver=w.admin2, decision="approve", reason="")
    assert req.state == "approved"


@pytest.mark.asyncio
async def test_unresolvable_minter_fails_closed(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    rid = await _pending(db_session, w)
    # An implementer key that no longer resolves to anyone.
    await seed_implementer(db_session, w.project, w.ticket, user_id=None,
                           service_key_id="sk_gone_000000000000000000")
    with pytest.raises(HTTPException) as exc:
        await svc.decide(db_session, request_id=rid, approver=w.admin2, decision="approve", reason="")
    assert exc.value.detail["code"] == "self_approval_forbidden"


@pytest.mark.asyncio
async def test_approver_who_lost_standing_refused(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    rid = await _pending(db_session, w, requirements=[
        {"who": "users", "user_ids": [w.admin.id], "count": 1}])
    from sessionfs.server.db.models import OrgMember

    member = (await db_session.execute(
        select(OrgMember).where(OrgMember.org_id == w.org.id, OrgMember.user_id == w.admin.id)
    )).scalar_one()
    await db_session.delete(member)
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await svc.decide(db_session, request_id=rid, approver=w.admin, decision="approve", reason="")
    # No longer a member: no read access to the project either, so the same
    # 404 as an unknown request (re-checked under the row lock).
    assert exc.value.status_code == 404 and exc.value.detail == svc.REQUEST_NOT_FOUND
    # A deactivated user is refused too.
    await add_member(db_session, w.org, w.admin, "admin")
    user = await db_session.get(User, w.admin.id)
    user.is_active = False
    await db_session.commit()
    with pytest.raises(HTTPException):
        await svc.decide(db_session, request_id=rid, approver=user, decision="approve", reason="")


@pytest.mark.asyncio
async def test_check_policy_references(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    await svc.check_policy_references(
        db_session, w.org.id, {"project_ids": [w.project.id]},
        [{"who": "users", "user_ids": [w.admin.id], "count": 1}],
    )
    with pytest.raises(HTTPException) as exc:
        await svc.check_policy_references(
            db_session, w.org.id, {}, [{"who": "users", "user_ids": [w.outsider.id], "count": 1}])
    assert exc.value.detail["code"] == "approver_not_eligible"
    with pytest.raises(HTTPException) as exc:
        await svc.check_policy_references(db_session, w.org.id, {"project_ids": ["proj_nope"]}, [])
    assert exc.value.detail["code"] == "invalid_filter"


@pytest.mark.asyncio
async def test_loop_status_waits_on_blocking_approval(db_session: AsyncSession) -> None:
    import uuid

    from sessionfs.server.db.models import AgentRun
    from sessionfs.server.services.loops import load_loops

    w = await make_world(db_session)
    parent = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}", project_id=w.project.id, title="Loop",
        kind="issue", created_by_user_id=w.owner.id,
        loop_config=json.dumps({"queue_id": "wq_x"}),
    )
    db_session.add(parent)
    child = await db_session.get(Ticket, w.ticket.id)
    child.parent_ticket_id = parent.id
    db_session.add(AgentRun(
        id=f"run_{uuid.uuid4().hex[:16]}", project_id=w.project.id, persona_name="atlas",
        role="implementer", ticket_id=child.id, head_sha=HEAD,
        created_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    await make_policy(db_session, w.org)

    # Loop status is a read: it shows the policy but raises nothing.
    (loop,) = await load_loops(db_session, loop_tickets=[parent], show_dollars=False)
    (c,) = loop["children"]
    assert c["ready"] is False
    assert c["approvals"][0]["state"] == "not_requested"
    assert (await db_session.execute(select(ApprovalRequest))).scalars().all() == []

    await svc.raise_ticket_approvals(db_session, child)  # the trigger
    await db_session.commit()
    (loop,) = await load_loops(db_session, loop_tickets=[parent], show_dollars=False)
    (c,) = loop["children"]
    assert c["ready"] is False
    assert "Needs approval (0 of 1)" in c["not_ready_reasons"]
    assert c["approvals"][0]["state"] == "pending"
    await db_session.commit()
    await svc.decide(db_session, request_id=c["approvals"][0]["request_id"],
                     approver=w.admin, decision="approve", reason="")
    await db_session.commit()
    (loop,) = await load_loops(db_session, loop_tickets=[parent], show_dollars=False)
    assert loop["children"][0]["ready"] is True

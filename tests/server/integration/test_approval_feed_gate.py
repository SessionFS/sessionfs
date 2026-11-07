"""Approvals A4: the For You attention items and the workflow gate check."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import AgentRun
from sessionfs.server.services import approvals as svc
from tests.server.integration.approvals_helpers import (
    HEAD,
    NEXT,
    make_policy,
    make_world,
)

pytestmark = pytest.mark.asyncio


async def _run(db: AsyncSession, w, sha: str) -> None:
    db.add(AgentRun(
        id=f"run_{uuid.uuid4().hex[:16]}", project_id=w.project.id, persona_name="atlas",
        role="implementer", ticket_id=w.ticket.id, head_sha=sha,
        created_at=datetime.now(timezone.utc),
    ))
    await db.commit()


async def _raise_request(db: AsyncSession, w, **policy_kw):
    policy = await make_policy(db, w.org, **policy_kw)
    r = await svc.ticket_readiness(db, w.ticket, HEAD)
    await db.commit()
    return policy, r["approvals"][0]["request_id"]


async def test_attention_item_shape_and_eligibility(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    _, rid = await _raise_request(db_session, w)

    items = await svc.attention_items(db_session, user_id=w.admin.id, scope=None)
    assert len(items) == 1
    it = items[0]
    assert re.fullmatch(r"fy_[0-9a-f]{40}", it["item_key"])
    assert it["kind"] == "approval" and it["band"] == "now"
    assert it["involvement"] == "approver"
    assert it["title"] == "Auth changes (0/1)"
    assert it["why"] == "Waiting on your approval · blocks merge"
    assert it["link"] == f"/approvals?request={rid}"
    assert it["project_id"] == w.project.id
    assert it["actions"] == ["snooze", "hide"]
    assert it["source"] == {"type": "approval_request", "id": rid}
    assert it["occurred_at"] and it["due_at"]

    # The implementer, a plain reviewer member and an outsider cannot decide it.
    for u in (w.implementer, w.reviewer, w.outsider):
        assert await svc.attention_items(db_session, user_id=u.id, scope=None) == []

    # Once the admin has voted it leaves their feed.
    await svc.decide(db_session, request_id=rid, approver=w.admin, decision="approve", reason="ok")
    await db_session.commit()
    assert await svc.attention_items(db_session, user_id=w.admin.id, scope=None) == []


async def test_attention_item_advisory_and_scope(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    await _raise_request(db_session, w, blocking=False)

    (it,) = await svc.attention_items(db_session, user_id=w.admin.id, scope=None)
    assert it["why"].endswith("advisory")

    ok = SimpleNamespace(org_id=w.org.id, project_ids=[w.project.id])
    assert len(await svc.attention_items(db_session, user_id=w.admin.id, scope=ok)) == 1
    other_org = SimpleNamespace(org_id="org_other", project_ids=None)
    assert await svc.attention_items(db_session, user_id=w.admin.id, scope=other_org) == []
    other_proj = SimpleNamespace(org_id=w.org.id, project_ids=["proj_nope"])
    assert await svc.attention_items(db_session, user_id=w.admin.id, scope=other_proj) == []
    assert await svc.attention_items(
        db_session, user_id=w.admin.id, scope=None, allowed_project_ids=set()
    ) == []
    assert len(await svc.attention_items(
        db_session, user_id=w.admin.id, scope=None, allowed_project_ids={w.project.id}
    )) == 1


async def test_item_key_is_stable() -> None:
    assert svc.attention_item_key("apr_1") == svc.attention_item_key("apr_1")
    assert svc.attention_item_key("apr_1") != svc.attention_item_key("apr_2")


async def test_gate_flow(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    await _run(db_session, w, HEAD)

    ok, why = await svc.workflow_gate_passed(db_session, policy_id="ap_missing", ticket=w.ticket)
    assert (ok, why) == (False, "Approval policy not found")

    policy = await make_policy(db_session, w.org)
    # No request yet: the gate never creates one.
    ok, why = await svc.workflow_gate_passed(db_session, policy_id=policy.id, ticket=w.ticket)
    assert (ok, why) == (False, "Waiting on approval: Auth changes (0 of 1)")
    from sqlalchemy import func, select
    from sessionfs.server.db.models import ApprovalRequest
    n = (await db_session.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
    assert n == 0

    await svc.ticket_readiness(db_session, w.ticket, HEAD)
    await db_session.commit()
    ok, why = await svc.workflow_gate_passed(db_session, policy_id=policy.id, ticket=w.ticket)
    assert ok is False and "Waiting on approval: Auth changes" in why

    rid = (await db_session.execute(select(ApprovalRequest.id))).scalar_one()
    await svc.decide(db_session, request_id=rid, approver=w.admin, decision="approve", reason="ok")
    await db_session.commit()
    assert await svc.workflow_gate_passed(db_session, policy_id=policy.id, ticket=w.ticket) == (True, "Approved")

    # A new head sha needs its own approval.
    await _run(db_session, w, NEXT)
    ok, _ = await svc.workflow_gate_passed(db_session, policy_id=policy.id, ticket=w.ticket)
    assert ok is False


async def test_gate_without_commit_and_other_org(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    policy = await make_policy(db_session, w.org)
    ok, why = await svc.workflow_gate_passed(db_session, policy_id=policy.id, ticket=w.ticket)
    assert ok is False and why.startswith("Waiting on a commit")

    w2 = await make_world(db_session)
    other = await make_policy(db_session, w2.org)
    assert await svc.workflow_gate_passed(db_session, policy_id=other.id, ticket=w.ticket) == (
        False, "Approval policy not found")

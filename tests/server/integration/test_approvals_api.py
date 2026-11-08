"""Approval policies REST API (GitHub #123, A1)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import ApprovalDecision, ApprovalPolicy
from tests.server.integration.approvals_helpers import (
    trigger_merge_check,
    HEAD,
    hdrs,
    make_org,
    make_policy,
    make_project,
    make_service_key,
    make_user,
    make_world,
    add_member,
)

MERGE_BODY = {
    "name": "Auth changes",
    "target_kind": "merge",
    "filter": {"finding_harm_any": ["data-exposed"], "priority_at_least": "critical"},
    "requirements": [{"who": "org_role", "role": "admin", "count": 1}],
}


def _url(org_id: str, suffix: str = "") -> str:
    return f"/api/v1/orgs/{org_id}/approval-policies{suffix}"


async def _readiness(client: AsyncClient, w, key: str, sha: str = HEAD):
    await trigger_merge_check(client, w)  # the write path raises requests
    resp = await client.get(
        f"/api/v1/projects/{w.project.id}/tickets/{w.ticket.id}/readiness",
        headers=hdrs(key), params={"sha": sha},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── policy CRUD ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_policy_crud_and_versioning(client: AsyncClient, db_session: AsyncSession):
    w = await make_world(db_session)
    resp = await client.post(_url(w.org.id), headers=hdrs(w.admin_key), json=MERGE_BODY)
    assert resp.status_code == 201, resp.text
    v1 = resp.json()
    assert v1["version"] == 1 and v1["active"] and v1["lineage_id"] == v1["id"]
    assert v1["filter"]["priority_at_least"] == "critical"

    # A member sees names and requirements only.
    member = (await client.get(_url(w.org.id), headers=hdrs(w.reviewer_key))).json()
    (row,) = member["policies"]
    assert row["name"] == "Auth changes" and row["requirements"]
    assert "filter" not in row and "description" not in row
    assert member["can_manage"] is False

    body = {**MERGE_BODY, "requirements": [{"who": "org_role", "role": "owner", "count": 1}]}
    resp = await client.put(_url(w.org.id, f"/{v1['id']}"), headers=hdrs(w.admin_key), json=body)
    assert resp.status_code == 200, resp.text
    v2 = resp.json()
    assert v2["version"] == 2 and v2["supersedes_id"] == v1["id"]
    assert v2["lineage_id"] == v1["id"]
    old = await db_session.get(ApprovalPolicy, v1["id"])
    await db_session.refresh(old)
    assert old.active is False and old.deactivated_at is not None

    # Editing the old version again is refused.
    resp = await client.put(_url(w.org.id, f"/{v1['id']}"), headers=hdrs(w.admin_key), json=body)
    assert resp.status_code == 409

    listed = (await client.get(_url(w.org.id), headers=hdrs(w.admin_key))).json()["policies"]
    assert [p["id"] for p in listed] == [v2["id"]]
    every = (await client.get(_url(w.org.id), headers=hdrs(w.admin_key),
                              params={"include_inactive": "true"})).json()["policies"]
    assert {p["id"] for p in every} == {v1["id"], v2["id"]}

    resp = await client.delete(_url(w.org.id, f"/{v2['id']}"), headers=hdrs(w.admin_key))
    assert resp.status_code == 200 and resp.json()["active"] is False
    assert (await client.get(_url(w.org.id), headers=hdrs(w.admin_key))).json()["policies"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"filter": {"bogus": True}}, "invalid_filter"),
        ({"filter": {"project_ids": ["proj_" + "a" * 50] * 1 + [f"p{i:061d}" for i in range(140)]}},
         "filter_too_large"),
        ({"requirements": []}, "invalid_requirements"),
        ({"target_kind": "kb_promote"}, "invalid_filter"),  # merge keys on a kb policy
    ],
)
async def test_policy_validation(client: AsyncClient, db_session: AsyncSession, patch, code):
    w = await make_world(db_session)
    resp = await client.post(_url(w.org.id), headers=hdrs(w.admin_key), json={**MERGE_BODY, **patch})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == code or code in resp.text


@pytest.mark.asyncio
async def test_named_user_must_be_active_member(client: AsyncClient, db_session: AsyncSession):
    w = await make_world(db_session)
    body = {**MERGE_BODY, "requirements": [
        {"who": "users", "user_ids": [w.admin.id, w.outsider.id], "count": 1}]}
    resp = await client.post(_url(w.org.id), headers=hdrs(w.admin_key), json=body)
    assert resp.status_code == 422 and "approver_not_eligible" in resp.text
    assert w.outsider.id not in resp.text  # never names the miss


@pytest.mark.asyncio
async def test_filter_projects_must_be_in_org(client: AsyncClient, db_session: AsyncSession):
    w = await make_world(db_session)
    other = await make_project(db_session, w.outsider, None)
    body = {**MERGE_BODY, "filter": {"project_ids": [other.id]}}
    resp = await client.post(_url(w.org.id), headers=hdrs(w.admin_key), json=body)
    assert resp.status_code == 422 and "invalid_filter" in resp.text


@pytest.mark.asyncio
async def test_fifty_active_policy_cap(client: AsyncClient, db_session: AsyncSession):
    w = await make_world(db_session)
    for i in range(50):
        await make_policy(db_session, w.org, name=f"p{i}")
    resp = await client.post(_url(w.org.id), headers=hdrs(w.admin_key), json=MERGE_BODY)
    assert resp.status_code == 409 and "policy_limit" in resp.text


@pytest.mark.asyncio
async def test_roles_and_visibility(client: AsyncClient, db_session: AsyncSession):
    w = await make_world(db_session)
    # Member: visible org, wrong role.
    resp = await client.post(_url(w.org.id), headers=hdrs(w.reviewer_key), json=MERGE_BODY)
    assert resp.status_code == 403 and "insufficient_role" in resp.text
    # Outsider and unknown org: the same 404 body.
    a = await client.get(_url(w.org.id), headers=hdrs(w.outsider_key))
    b = await client.get(_url("org_does_not_exist"), headers=hdrs(w.outsider_key))
    c = await client.post(_url(w.org.id), headers=hdrs(w.outsider_key), json=MERGE_BODY)
    assert a.status_code == b.status_code == c.status_code == 404
    assert a.content == b.content == c.content
    policy = await make_policy(db_session, w.org)
    d = await client.put(_url(w.org.id, f"/{policy.id}"), headers=hdrs(w.outsider_key), json=MERGE_BODY)
    assert d.status_code == 404 and d.content == a.content


@pytest.mark.asyncio
async def test_plan_gate_after_visibility(client: AsyncClient, db_session: AsyncSession):
    w = await make_world(db_session, tier="team")
    resp = await client.post(_url(w.org.id), headers=hdrs(w.outsider_key), json=MERGE_BODY)
    assert resp.status_code == 404
    resp = await client.post(_url(w.org.id), headers=hdrs(w.admin_key), json=MERGE_BODY)
    assert resp.status_code == 403
    assert "upgrade_required" in resp.text and "approval_policies" in resp.text
    # Reading still works on any plan.
    resp = await client.get(_url(w.org.id), headers=hdrs(w.admin_key))
    assert resp.status_code == 200 and resp.json()["plan_includes_approvals"] is False


@pytest.mark.asyncio
async def test_service_keys_read_only_in_own_org(client: AsyncClient, db_session: AsyncSession):
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    _, raw = await make_service_key(db_session, w.admin, w.org, ["approvals:read"])
    resp = await client.get(_url(w.org.id), headers=hdrs(raw))
    assert resp.status_code == 200 and len(resp.json()["policies"]) == 1
    other = await make_org(db_session)
    await add_member(db_session, other, w.admin, "admin")
    resp = await client.get(_url(other.id), headers=hdrs(raw))
    assert resp.status_code == 404
    resp = await client.post(_url(w.org.id), headers=hdrs(raw), json=MERGE_BODY)
    assert resp.status_code == 403 and "service_key_not_allowed" in resp.text
    _, noscope = await make_service_key(db_session, w.admin, w.org, ["tickets:read"])
    resp = await client.get(_url(w.org.id), headers=hdrs(noscope))
    assert resp.status_code == 403 and "insufficient_scope" in resp.text


# ── readiness + decide over HTTP ───────────────────────────────────


@pytest.mark.asyncio
async def test_readiness_route_waits_then_ready_after_decide(
    client: AsyncClient, db_session: AsyncSession
):
    w = await make_world(db_session)
    await make_policy(db_session, w.org, name="Auth changes")
    await make_policy(db_session, w.org, name="FYI", blocking=False)
    body = await _readiness(client, w, w.owner_key)
    assert body["ready"] is False
    assert "Waiting on approval: Auth changes (0 of 1)" in body["reasons"]
    assert body["warnings"] == ["Waiting on approval: FYI (0 of 1)"]
    by_name = {a["policy_name"]: a for a in body["approvals"]}
    assert by_name["Auth changes"]["state"] == "pending"
    assert set(by_name["Auth changes"]) >= {
        "request_id", "policy_name", "state", "approved_count", "required_count"}
    rid = by_name["Auth changes"]["request_id"]

    mine = (await client.get("/api/v1/me/approvals", headers=hdrs(w.admin_key))).json()
    assert rid in [r["id"] for r in mine["requests"]]
    assert mine["requests"][0]["target_title"] == "Auth change"
    # The implementer can't decide, so it's not in their queue.
    impl = (await client.get("/api/v1/me/approvals", headers=hdrs(w.implementer_key))).json()
    assert impl["requests"] == []

    resp = await client.post(
        f"/api/v1/approval-requests/{rid}/decide", headers=hdrs(w.admin_key),
        json={"decision": "approve", "reason": "Reviewed the token handling."},
    )
    assert resp.status_code == 200, resp.text
    view = resp.json()
    assert view["state"] == "approved" and view["approved_count"] == 1
    assert view["decisions"][0]["reason"] == "Reviewed the token handling."

    body = await _readiness(client, w, w.owner_key)
    assert body["ready"] is True and body["warnings"] == ["Waiting on approval: FYI (0 of 1)"]
    mine = (await client.get("/api/v1/me/approvals", headers=hdrs(w.admin_key))).json()
    assert rid not in [r["id"] for r in mine["requests"]]


@pytest.mark.asyncio
async def test_decide_refusals(client: AsyncClient, db_session: AsyncSession):
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    rid = (await _readiness(client, w, w.owner_key))["approvals"][0]["request_id"]
    url = f"/api/v1/approval-requests/{rid}/decide"

    _, svc_raw = await make_service_key(db_session, w.admin, w.org, ["approvals:read"])
    resp = await client.post(url, headers=hdrs(svc_raw), json={"decision": "approve"})
    assert resp.status_code == 403 and "service_key_not_allowed" in resp.text

    resp = await client.post(url, headers=hdrs(w.implementer_key), json={"decision": "approve"})
    assert resp.status_code == 403  # a member who fits no requirement

    a = await client.post(url, headers=hdrs(w.outsider_key), json={"decision": "approve"})
    b = await client.post("/api/v1/approval-requests/ar_nope/decide",
                          headers=hdrs(w.outsider_key), json={"decision": "approve"})
    assert a.status_code == b.status_code == 404 and a.content == b.content

    resp = await client.post(url, headers=hdrs(w.admin_key),
                             json={"decision": "approve", "reason": "x" * 2001})
    assert resp.status_code == 422
    resp = await client.post(url, headers=hdrs(w.admin_key), json={"decision": "maybe"})
    assert resp.status_code == 422
    assert (await db_session.execute(select(ApprovalDecision))).scalars().all() == []


@pytest.mark.asyncio
async def test_get_request_visibility(client: AsyncClient, db_session: AsyncSession):
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    rid = (await _readiness(client, w, w.owner_key))["approvals"][0]["request_id"]
    url = f"/api/v1/approval-requests/{rid}"
    resp = await client.get(url, headers=hdrs(w.reviewer_key))  # project reader
    assert resp.status_code == 200
    view = resp.json()
    assert view["target_fingerprint"] == HEAD and view["can_decide"] is False
    assert view["policy"]["name"] == "Auth changes"
    assert (await client.get(url, headers=hdrs(w.admin_key))).json()["can_decide"] is True

    a = await client.get(url, headers=hdrs(w.outsider_key))
    b = await client.get("/api/v1/approval-requests/ar_nope", headers=hdrs(w.outsider_key))
    assert a.status_code == b.status_code == 404 and a.content == b.content

    _, svc_raw = await make_service_key(db_session, w.admin, w.org, ["approvals:read"])
    resp = await client.get(url, headers=hdrs(svc_raw))
    assert resp.status_code == 200 and resp.json()["can_decide"] is False
    other = await make_org(db_session)
    foreign, _ = await make_user(db_session, "foreign")
    _, foreign_raw = await make_service_key(db_session, foreign, other, ["approvals:read"])
    resp = await client.get(url, headers=hdrs(foreign_raw))
    assert resp.status_code == 404 and resp.content == a.content


@pytest.mark.asyncio
async def test_decision_reason_is_redacted(client: AsyncClient, db_session: AsyncSession):
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    rid = (await _readiness(client, w, w.owner_key))["approvals"][0]["request_id"]
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    resp = await client.post(
        f"/api/v1/approval-requests/{rid}/decide", headers=hdrs(w.admin_key),
        json={"decision": "approve", "reason": f"ok \x00 token {secret}"},
    )
    assert resp.status_code == 200, resp.text
    (stored,) = (await db_session.execute(select(ApprovalDecision))).scalars().all()
    assert secret not in stored.reason and "\x00" not in stored.reason
    assert stored.reason.startswith("ok")

"""For You x approvals: the approvals section reads the real
``services.approvals.attention_items`` shape (``source`` + ISO strings) and
only shows a request to someone who could decide it."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.services import approvals as asvc
from tests.server.integration.approvals_helpers import (
    raised_readiness,
    HEAD,
    hdrs,
    make_policy,
    make_service_key,
    make_world,
)

pytestmark = pytest.mark.asyncio


async def _raise(db: AsyncSession, w, **kw) -> str:
    await make_policy(db, w.org, **kw)
    r = await raised_readiness(db, w.ticket, HEAD)
    await db.commit()
    return r["approvals"][0]["request_id"]


async def _feed(client: AsyncClient, key: str) -> dict:
    resp = await client.get("/api/v1/me/for-you", headers=hdrs(key))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _approvals(body: dict) -> list[dict]:
    return [i for i in body["items"] if i["kind"] == "approval"]


async def test_eligible_approver_sees_the_request_with_its_facts(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    w = await make_world(db_session)
    rid = await _raise(db_session, w)

    body = await _feed(client, w.admin_key)
    assert body["sections"]["approvals"] == {"count": 1, "error": None}
    (it,) = _approvals(body)
    assert it["source"] == {"type": "approval_request", "id": rid}
    assert it["band"] == "now" and it["group"] == "radar"
    assert it["involvement"] == "approver"
    assert it["title"] == "Auth changes (0/1)"
    assert it["why"] == "Waiting on your approval · blocks merge"
    assert it["link"] == f"/approvals?request={rid}"
    assert it["project_id"] == w.project.id
    # ISO strings from the approvals service become real datetimes again.
    assert it["occurred_at"] and it["due_at"]
    assert it["due_at"] > it["occurred_at"]
    assert {"approve", "reject", "snooze", "hide"} <= set(it["actions"])
    ap = it["context"]["approval"]
    assert ap["request_id"] == rid and ap["approved_count"] == 0 and ap["required_count"] == 1
    assert ap["target_kind"] == "merge" and ap["target_label"] == f"commit {HEAD[:7]}"
    assert ap["target_title"] == "Auth change"
    assert it["context"]["progress"][0]["text"] == "0 of 1"
    # No approver names or emails anywhere on the card.
    assert w.admin.email not in str(it)


async def test_never_shown_to_the_maker_or_a_non_member(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    w = await make_world(db_session)
    # Members may approve, so the implementer would qualify if self-review
    # were not refused.
    await _raise(db_session, w, requirements=[{"who": "org_role", "role": "member", "count": 1}])
    for key in (w.implementer_key, w.outsider_key):
        body = await _feed(client, key)
        assert _approvals(body) == []
    assert len(_approvals(await _feed(client, w.reviewer_key))) == 1


async def test_leaves_the_feed_once_decided(client: AsyncClient, db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    rid = await _raise(db_session, w)
    resp = await client.post(
        f"/api/v1/approval-requests/{rid}/decide",
        headers=hdrs(w.admin_key),
        json={"decision": "approve", "reason": "Reviewed"},
    )
    assert resp.status_code == 200, resp.text
    assert _approvals(await _feed(client, w.admin_key)) == []


async def test_service_keys_never_get_approval_cards(client: AsyncClient, db_session: AsyncSession) -> None:
    """Deciding needs a person's user key, so a service key gets no approval
    cards it could not clear, whatever its scopes."""
    w = await make_world(db_session)
    await _raise(db_session, w)
    for scopes in (
        ["attention:read"],
        ["attention:read", "approvals:read"],
        ["attention:read", "approvals:read", "tickets:read"],
    ):
        _, key = await make_service_key(db_session, w.admin, w.org, scopes)
        assert _approvals(await _feed(client, key)) == []
    # The person still sees it.
    assert len(_approvals(await _feed(client, w.admin_key))) == 1


async def test_hidden_card_returns_when_another_approval_lands(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    w = await make_world(db_session)
    await _raise(db_session, w, requirements=[{"who": "org_role", "role": "admin", "count": 2}])
    (it,) = _approvals(await _feed(client, w.admin_key))
    resp = await client.post(
        "/api/v1/me/for-you/hide", headers=hdrs(w.admin_key), json={"item_key": it["item_key"]}
    )
    assert resp.status_code in (200, 204), resp.text
    assert _approvals(await _feed(client, w.admin_key)) == []
    other = await client.post(
        f"/api/v1/approval-requests/{it['source']['id']}/decide",
        headers=hdrs(w.admin2_key),
        json={"decision": "approve", "reason": "ok"},
    )
    assert other.status_code == 200, other.text
    (back,) = _approvals(await _feed(client, w.admin_key))
    assert back["context"]["approval"]["approved_count"] == 1

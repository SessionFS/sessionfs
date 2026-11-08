"""Approval policies A2: the org approval log (JSON keyset + CSV export) and
the helper's read-only list_my_approvals tool (GitHub #123)."""

from __future__ import annotations

import csv
import io

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.helper.tools import TOOLS
from sessionfs.server.routes.approvals import csv_safe
from sessionfs.server.services import approvals as svc
from tests.server.integration.approvals_helpers import (
    raised_readiness,
    HEAD,
    hdrs,
    make_policy,
    make_project,
    make_service_key,
    make_ticket,
    make_world,
    seed_clean,
    seed_implementer,
)
from tests.server.integration.test_helper_ticket_actions import _ctx

pytestmark = pytest.mark.asyncio


def _log_url(org_id: str) -> str:
    return f"/api/v1/orgs/{org_id}/approval-log"


async def _two_decisions(db: AsyncSession):
    """Two requests: one approved (admin), one rejected (admin2) with a
    formula-looking reason."""
    w = await make_world(db)
    policy = await make_policy(db, w.org)
    r = await raised_readiness(db, w.ticket, HEAD)
    await db.commit()
    first = r["approvals"][0]["request_id"]
    await svc.decide(db, request_id=first, approver=w.admin, decision="approve",
                     reason="Checked the token handling.")
    await db.commit()

    ticket2 = await make_ticket(db, w.project, w.implementer)
    await seed_implementer(db, w.project, ticket2, user_id=w.implementer.id)
    await seed_clean(db, ticket2, w.reviewer, HEAD)
    r2 = await raised_readiness(db, ticket2, HEAD)
    await db.commit()
    second = r2["approvals"][0]["request_id"]
    await svc.decide(db, request_id=second, approver=w.admin2, decision="reject",
                     reason='=HYPERLINK("http://evil.test","x")')
    await db.commit()
    return w, policy, first, second


async def test_csv_safe_defuses_formulas() -> None:
    for raw in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx", "|x", "%x"):
        assert csv_safe(raw) == "'" + raw
    assert csv_safe("plain") == "plain"
    assert csv_safe(None) == ""
    assert csv_safe(3) == "3"


async def test_log_lists_decisions_newest_first_with_keyset(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    w, policy, first, second = await _two_decisions(db_session)
    resp = await client.get(_log_url(w.org.id), headers=hdrs(w.owner_key))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["request_id"] for i in items] == [second, first]
    assert items[0]["decision"] == "reject" and items[0]["request_state"] == "rejected"
    assert items[1]["approver_user_id"] == w.admin.id
    assert items[1]["policy_name"] == "Auth changes" and items[1]["policy_version"] == 1
    assert items[1]["target_fingerprint"] == HEAD

    page1 = (await client.get(_log_url(w.org.id), headers=hdrs(w.admin_key),
                              params={"limit": 1})).json()
    assert [i["request_id"] for i in page1["items"]] == [second]
    assert page1["next_before"]
    page2 = (await client.get(_log_url(w.org.id), headers=hdrs(w.admin_key),
                              params={"limit": 1, "before": page1["next_before"]})).json()
    assert [i["request_id"] for i in page2["items"]] == [first]
    assert page2["next_before"] is None


async def test_log_filters(client: AsyncClient, db_session: AsyncSession) -> None:
    w, policy, first, second = await _two_decisions(db_session)

    async def ids(**params):
        resp = await client.get(_log_url(w.org.id), headers=hdrs(w.owner_key), params=params)
        assert resp.status_code == 200, resp.text
        return [i["request_id"] for i in resp.json()["items"]]

    assert await ids(decision="approve") == [first]
    assert await ids(approver=w.admin2.id) == [second]
    assert await ids(target_kind="kb_promote") == []
    assert await ids(project_id=w.project.id) == [second, first]
    other = await make_project(db_session, w.owner, w.org)
    assert await ids(project_id=other.id) == []
    assert await ids(policy_id=policy.id) == [second, first]
    assert await ids(policy_id="ap_unknown") == []
    assert await ids(since="2999-01-01T00:00:00Z") == []
    bad = await client.get(_log_url(w.org.id), headers=hdrs(w.owner_key),
                           params={"since": "yesterday"})
    assert bad.status_code == 422
    bad = await client.get(_log_url(w.org.id), headers=hdrs(w.owner_key),
                           params={"before": "nope"})
    assert bad.status_code == 422


async def test_log_is_admin_only(client: AsyncClient, db_session: AsyncSession) -> None:
    w, *_ = await _two_decisions(db_session)
    member = await client.get(_log_url(w.org.id), headers=hdrs(w.reviewer_key))
    assert member.status_code == 403
    outsider = await client.get(_log_url(w.org.id), headers=hdrs(w.outsider_key))
    unknown = await client.get(_log_url("org_nope"), headers=hdrs(w.outsider_key))
    assert outsider.status_code == unknown.status_code == 404
    assert outsider.content == unknown.content
    _, svc_raw = await make_service_key(db_session, w.admin, w.org, ["approvals:read"])
    service = await client.get(_log_url(w.org.id), headers=hdrs(svc_raw))
    assert service.status_code == 403


async def test_log_csv_export_is_injection_safe(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    w, _policy, first, second = await _two_decisions(db_session)
    resp = await client.get(_log_url(w.org.id), headers=hdrs(w.owner_key),
                            params={"format": "csv"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["cache-control"] == "no-store"
    rows = list(csv.reader(io.StringIO(resp.text)))
    header, body = rows[0], rows[1:]
    assert header[:3] == ["decided_at", "decision", "approver_user_id"]
    by_request = {r[header.index("request_id")]: r for r in body}
    assert set(by_request) == {first, second}
    assert by_request[second][header.index("reason")] == "'" + '=HYPERLINK("http://evil.test","x")'
    assert by_request[first][header.index("reason")] == "Checked the token handling."


async def test_helper_list_my_approvals(db_session: AsyncSession) -> None:
    w = await make_world(db_session)
    await make_policy(db_session, w.org)
    rid = (await raised_readiness(db_session, w.ticket, HEAD))["approvals"][0]["request_id"]
    await db_session.commit()
    tool = TOOLS["list_my_approvals"]

    out = await tool.handler(_ctx(db_session, w.admin), {})
    assert f"[{rid}]" in out.text and "Auth changes" in out.text
    assert "0 of 1 approvals" in out.text and "<approvals>" in out.text
    assert out.refs.get("ticket_ids") == [w.ticket.id]

    # The implementer can't decide their own work.
    out = await tool.handler(_ctx(db_session, w.implementer), {})
    assert "No approval requests" in out.text
    # A conversation scoped to another project leaves it out.
    other = await make_project(db_session, w.owner, w.org)
    out = await tool.handler(_ctx(db_session, w.admin, project_id=other.id), {})
    assert "No approval requests" in out.text
    out = await tool.handler(_ctx(db_session, w.admin, project_id=w.project.id), {})
    assert f"[{rid}]" in out.text

"""Pre-release security review fixes for approval policies (0.29.6)."""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import OrgAuditEvent
from sessionfs.server.services import approvals
from tests.server.integration.approvals_helpers import (
    HEAD,
    hdrs,
    make_policy,
    make_world,
)


@pytest.mark.asyncio
async def test_lowering_priority_does_not_drop_a_raised_policy(db_session: AsyncSession):
    """Once a policy raised a request for the ticket, editing the ticket so
    the filter no longer matches keeps the gate in place."""
    w = await make_world(db_session)
    w.ticket.priority = "critical"
    await db_session.commit()
    await make_policy(db_session, w.org, flt={"priority_at_least": "high"})

    first = await approvals.ticket_readiness(db_session, w.ticket, HEAD)
    await db_session.commit()
    assert first["ready"] is False
    assert first["approvals"][0]["state"] == "pending"

    w.ticket.priority = "low"
    await db_session.commit()
    again = await approvals.ticket_readiness(db_session, w.ticket, HEAD)
    assert again["ready"] is False
    assert again["approvals"] and again["approvals"][0]["state"] == "pending"


@pytest.mark.asyncio
async def test_policy_never_raised_still_honours_the_filter(db_session: AsyncSession):
    w = await make_world(db_session)
    w.ticket.priority = "low"
    await db_session.commit()
    await make_policy(db_session, w.org, flt={"priority_at_least": "high"})
    result = await approvals.ticket_readiness(db_session, w.ticket, HEAD)
    assert result["ready"] is True
    assert result["approvals"] == []


@pytest.mark.asyncio
async def test_policy_writes_are_audited(client: AsyncClient, db_session: AsyncSession):
    w = await make_world(db_session)
    body = {
        "name": "Auth changes",
        "target_kind": "merge",
        "requirements": [{"who": "org_role", "role": "admin", "count": 1}],
    }
    url = f"/api/v1/orgs/{w.org.id}/approval-policies"
    created = await client.post(url, headers=hdrs(w.admin_key), json=body)
    assert created.status_code == 201, created.text
    v1 = created.json()
    edited = await client.put(
        f"{url}/{v1['id']}", headers=hdrs(w.admin_key), json={**body, "name": "Auth v2"}
    )
    assert edited.status_code == 200, edited.text
    v2 = edited.json()
    gone = await client.delete(f"{url}/{v2['id']}", headers=hdrs(w.admin2_key))
    assert gone.status_code == 200, gone.text
    # Idempotent repeat writes no second event.
    again = await client.delete(f"{url}/{v2['id']}", headers=hdrs(w.admin2_key))
    assert again.status_code == 200

    events = (
        await db_session.execute(
            select(OrgAuditEvent)
            .where(OrgAuditEvent.target_type == "approval_policy")
            .order_by(OrgAuditEvent.created_at, OrgAuditEvent.id)
        )
    ).scalars().all()
    kinds = sorted(e.event_type for e in events)
    assert kinds == [
        "approval_policy_created",
        "approval_policy_deactivated",
        "approval_policy_updated",
    ]
    by_kind = {e.event_type: e for e in events}
    assert all(e.target_id == v1["lineage_id"] for e in events)
    assert by_kind["approval_policy_created"].actor_user_id == w.admin.id
    deact = by_kind["approval_policy_deactivated"]
    assert deact.actor_user_id == w.admin2.id
    assert json.loads(deact.before)["active"] is True
    assert json.loads(deact.after)["active"] is False
    upd = by_kind["approval_policy_updated"]
    assert json.loads(upd.before)["name"] == "Auth changes"
    assert json.loads(upd.after)["name"] == "Auth v2"

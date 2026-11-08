"""Delivery-webhook idempotency ledger retention (tk_86db8de1c3bf4bf0 #11).

``delivery_webhook_events`` had no retention, so it grew by one row per
delivery forever. The scheduled GitHub webhook-delivery sweep now also drops
ledger rows older than the replay-tombstone window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import DeliveryWebhookEvent
from tests.server.integration.test_platform_sweeps_scope import _sweep_key

pytestmark = pytest.mark.asyncio


async def test_sweep_prunes_old_delivery_events_only(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, org_id, _, headers = await _sweep_key(db_session)
    now = datetime.now(timezone.utc)
    db_session.add_all([
        DeliveryWebhookEvent(
            provider="github", org_id=org_id, idempotency_key="old",
            received_at=now - timedelta(days=401),
        ),
        DeliveryWebhookEvent(
            provider="gitlab", org_id=org_id, idempotency_key="recent",
            received_at=now - timedelta(days=30),
        ),
    ])
    await db_session.commit()

    resp = await client.post(
        "/api/v1/admin/github-webhook-deliveries/sweep", headers=headers, json={}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["delivery_events_purged"] == 1
    keys = (
        await db_session.execute(
            select(DeliveryWebhookEvent.idempotency_key)
            .where(DeliveryWebhookEvent.org_id == org_id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert keys == ["recent"]

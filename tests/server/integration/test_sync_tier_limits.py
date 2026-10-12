"""Per-tier session-sync limits (tk_59efad03840b4790).

Starter is a paid plan: it gets the paid per-session caps, and sync status
reports each paid plan's storage from tiers.py rather than the Free cap.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import User
from sessionfs.server.routes.sessions import (
    SFS_MAX_SYNC_BYTES_FREE,
    SFS_MAX_SYNC_BYTES_PAID,
    SFS_MAX_SYNC_MEMBER_BYTES_FREE,
    SFS_MAX_SYNC_MEMBER_BYTES_PAID,
    _member_size_limit_for_tier,
    _sync_limit_for_tier,
)
from sessionfs.server.tiers import Tier, get_storage_limit


@pytest.mark.parametrize(
    ("tier", "paid"),
    [
        (Tier.FREE, False),
        (Tier.STARTER, True),
        (Tier.PRO, True),
        (Tier.TEAM, True),
        (Tier.ENTERPRISE, True),
    ],
)
def test_per_session_caps_by_tier(tier: Tier, paid: bool):
    assert _sync_limit_for_tier(tier) == (SFS_MAX_SYNC_BYTES_PAID if paid else SFS_MAX_SYNC_BYTES_FREE)
    assert _member_size_limit_for_tier(tier) == (
        SFS_MAX_SYNC_MEMBER_BYTES_PAID if paid else SFS_MAX_SYNC_MEMBER_BYTES_FREE
    )


def test_plan_storage_matches_pricing():
    assert get_storage_limit(Tier.STARTER) == 500 * 1024 * 1024
    assert get_storage_limit(Tier.PRO) == 2 * 1024 * 1024 * 1024


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("free", 50 * 1024 * 1024),
        ("starter", 500 * 1024 * 1024),
        ("pro", 2 * 1024 * 1024 * 1024),
        ("team", 1024 * 1024 * 1024),
        ("enterprise", 0),
    ],
)
async def test_sync_status_reports_plan_storage(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tier: str,
    expected: int,
):
    monkeypatch.delenv("SFS_MAX_SYNC_BYTES_FREE", raising=False)
    test_user.tier = tier
    await db_session.commit()
    resp = await client.get("/api/v1/sync/status", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["storage_limit_bytes"] == expected

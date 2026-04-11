"""Integration tests for the GitHub App installation claim flow.

Covers PUT /api/v1/settings/github with the `installation_id` body field,
the 15-minute claim window, the atomic conditional UPDATE that prevents the
race condition flagged in the pre-release review, and the various error
codes (404 / 403 / 410).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import ApiKey, GitHubInstallation, User
from sessionfs.server.auth.keys import generate_api_key, hash_api_key


async def _create_installation(
    db: AsyncSession,
    *,
    installation_id: int,
    user_id: str | None = None,
    account_login: str = "acme",
    account_type: str = "Organization",
    created_offset_minutes: int = 0,
) -> GitHubInstallation:
    """Helper: create a github_installations row with a controllable age."""
    created = datetime.now(timezone.utc) - timedelta(minutes=created_offset_minutes)
    inst = GitHubInstallation(
        id=installation_id,
        user_id=user_id,
        account_login=account_login,
        account_type=account_type,
        auto_comment=True,
        include_trust_score=True,
        include_session_links=True,
        created_at=created,
    )
    db.add(inst)
    await db.commit()
    await db.refresh(inst)
    return inst


class TestGitHubClaim:
    """PUT /api/v1/settings/github installation claim flow."""

    @pytest.mark.asyncio
    async def test_claim_succeeds_for_fresh_unclaimed_installation(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
    ):
        await _create_installation(db_session, installation_id=100001)

        resp = await client.put(
            "/api/v1/settings/github",
            headers=auth_headers,
            json={"installation_id": 100001},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["account_login"] == "acme"
        assert data["account_type"] == "Organization"

    @pytest.mark.asyncio
    async def test_missing_installation_id_returns_defaults_not_error(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
    ):
        # Even with an unclaimed row sitting in the DB, an update without
        # installation_id must NOT silently claim it (that was the original
        # IDOR bug). It should return defaults instead.
        await _create_installation(db_session, installation_id=100002)

        resp = await client.put(
            "/api/v1/settings/github",
            headers=auth_headers,
            json={"auto_comment": False},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["account_login"] is None  # row not claimed
        assert data["auto_comment"] is False  # caller's preference echoed

    @pytest.mark.asyncio
    async def test_claim_unknown_installation_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ):
        resp = await client.put(
            "/api/v1/settings/github",
            headers=auth_headers,
            json={"installation_id": 999999999},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_claim_already_owned_by_other_user_returns_403(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
    ):
        other = User(
            id=str(uuid.uuid4()),
            email="other@example.com",
            tier="pro",
            email_verified=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other)
        await db_session.commit()

        await _create_installation(
            db_session, installation_id=100003, user_id=other.id
        )

        resp = await client.put(
            "/api/v1/settings/github",
            headers=auth_headers,
            json={"installation_id": 100003},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_claim_expired_window_returns_410(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
    ):
        # 20 minutes old — outside the 15-minute claim window.
        await _create_installation(
            db_session, installation_id=100004, created_offset_minutes=20
        )

        resp = await client.put(
            "/api/v1/settings/github",
            headers=auth_headers,
            json={"installation_id": 100004},
        )
        assert resp.status_code == 410

    @pytest.mark.asyncio
    async def test_idempotent_reclaim_of_own_installation(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        test_user: User,
        db_session: AsyncSession,
    ):
        # Already owned by the authenticated user — re-claim should succeed
        # and NOT fail the 15-minute window (idempotent behavior on re-read).
        await _create_installation(
            db_session,
            installation_id=100005,
            user_id=test_user.id,
            created_offset_minutes=90,  # well past the claim window
        )

        resp = await client.put(
            "/api/v1/settings/github",
            headers=auth_headers,
            json={"installation_id": 100005, "auto_comment": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["auto_comment"] is False

    @pytest.mark.asyncio
    async def test_concurrent_claims_only_one_wins(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        test_user: User,
    ):
        """Race: two users try to claim the same fresh installation at once.

        The atomic conditional UPDATE (UPDATE ... WHERE user_id IS NULL) must
        ensure exactly one request wins. The loser gets 403. Without the fix,
        both would succeed because the old code did SELECT, check, UPDATE
        without a lock or conditional predicate.
        """
        # Create a second user + API key so we have two distinct auth
        # identities pointing at the same installation_id.
        user_b = User(
            id=str(uuid.uuid4()),
            email="userb@example.com",
            tier="pro",
            email_verified=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user_b)
        await db_session.commit()

        raw_key_b = generate_api_key()
        key_b = ApiKey(
            id=str(uuid.uuid4()),
            user_id=user_b.id,
            key_hash=hash_api_key(raw_key_b),
            name="userb-key",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(key_b)
        await db_session.commit()

        await _create_installation(db_session, installation_id=100006)

        # Fire both PUTs concurrently
        # The `client` fixture's ApiKey only stores the hash, so we can't
        # recover the raw_key for user A. Issue a fresh one for the test.
        raw_key_a = generate_api_key()
        key_a = ApiKey(
            id=str(uuid.uuid4()),
            user_id=test_user.id,
            key_hash=hash_api_key(raw_key_a),
            name="usera-key",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(key_a)
        await db_session.commit()

        headers_a = {"Authorization": f"Bearer {raw_key_a}"}
        headers_b = {"Authorization": f"Bearer {raw_key_b}"}

        task_a = client.put(
            "/api/v1/settings/github",
            headers=headers_a,
            json={"installation_id": 100006},
        )
        task_b = client.put(
            "/api/v1/settings/github",
            headers=headers_b,
            json={"installation_id": 100006},
        )
        resp_a, resp_b = await asyncio.gather(task_a, task_b)

        statuses = sorted([resp_a.status_code, resp_b.status_code])
        # One winner (200), one loser (403 "already claimed"). Never both 200.
        assert statuses == [200, 403], (
            f"Expected exactly one winner. Got {statuses}. "
            f"A={resp_a.status_code} body={resp_a.text[:200]} "
            f"B={resp_b.status_code} body={resp_b.text[:200]}"
        )

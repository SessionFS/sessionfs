"""Regression tests for the fifth review round on GitHub claims/webhooks.

1. A repo renamed on GitHub: a holder with the same (provider, repo id)
   under the old remote goes through displacement (no 500, no block).
2. One linked installation per user is enforced by the database and by
   serialized binding, also under concurrency.
3. Comment publishing is serialized per comment: an older body can never
   land after a newer one.
4. A delivery outcome is recorded only for the current lease.
5. Failed webhook work is retried from the stored payload (opportunistic
   on deliveries + admin sweeper), with backoff and a max attempt count.
6. Helm refuses api.github.enabled without a dashboard URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import sessionfs.server.db.engine as engine_mod
from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    Base,
    GitHubInstallation,
    GitHubWebhookDelivery,
    PRComment,
    Project,
    ProjectRepo,
    User,
)
from sessionfs.server.routes import webhooks
from sessionfs.server.services import github_oauth

REPO_ROOT = Path(__file__).resolve().parents[3]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _user(db: AsyncSession, email: str) -> User:
    u = User(id=str(uuid.uuid4()), email=email, tier="pro", email_verified=True, created_at=_now())
    db.add(u)
    await db.commit()
    return u


async def _all(db: AsyncSession, model):
    return (
        await db.execute(select(model).execution_options(populate_existing=True))
    ).scalars().all()


@pytest.fixture
async def file_db(tmp_path, monkeypatch):
    """Separate connections per session, like separate workers."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'r5.db'}", connect_args={"timeout": 30}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(engine_mod, "_session_factory", factory)
    yield factory
    await engine.dispose()


# --- 1 ---------------------------------------------------------------------


class TestRenamedRepoProviderIdentity:
    async def _setup(self, db, test_user, *, holder_verified: bool):
        other = await _user(db, f"holder-{uuid.uuid4().hex[:6]}@example.com")
        holder_project = Project(
            id=f"proj_{uuid.uuid4().hex[:16]}", name="h",
            git_remote_normalized="github.com/acme/old-name", context_document="",
            owner_id=other.id,
        )
        claimant = Project(
            id=f"proj_{uuid.uuid4().hex[:16]}", name="c",
            git_remote_normalized="github.com/test/claimant", context_document="",
            owner_id=test_user.id,
        )
        db.add_all([holder_project, claimant])
        await db.commit()
        db.add_all([
            ProjectRepo(
                id=f"repo_{uuid.uuid4().hex[:16]}", project_id=holder_project.id,
                git_remote_normalized="github.com/acme/old-name", provider="github",
                provider_repo_id="555", is_primary=True, verified=holder_verified,
                verification_method="github_app" if holder_verified else "owner_attested",
            ),
            ProjectRepo(
                id=f"repo_{uuid.uuid4().hex[:16]}", project_id=claimant.id,
                git_remote_normalized="github.com/test/claimant", is_primary=True,
                verified=False, verification_method="legacy_backfill",
            ),
        ])
        await db.commit()
        return holder_project, claimant

    @pytest.mark.asyncio
    async def test_unverified_old_name_holder_displaced(
        self, client: AsyncClient, auth_headers, test_user, db_session
    ):
        holder_project, claimant = await self._setup(db_session, test_user, holder_verified=False)
        with patch(
            "sessionfs.server.github_app.verify_repo_ownership",
            new=AsyncMock(return_value=(True, "github_app", "github", "555")),
        ):
            resp = await client.post(
                f"/api/v1/projects/{claimant.id}/repos",
                json={"git_remote": "github.com/acme/new-name"},
                headers=auth_headers,
            )
        assert resp.status_code == 201, resp.text
        rows = await _all(db_session, ProjectRepo)
        by_remote = {r.git_remote_normalized: r for r in rows}
        assert "github.com/acme/old-name" not in by_remote
        assert by_remote["github.com/acme/new-name"].project_id == claimant.id
        assert by_remote["github.com/acme/new-name"].provider_repo_id == "555"

    @pytest.mark.asyncio
    async def test_verified_old_name_holder_conflicts(
        self, client: AsyncClient, auth_headers, test_user, db_session
    ):
        _, claimant = await self._setup(db_session, test_user, holder_verified=True)
        with patch(
            "sessionfs.server.github_app.verify_repo_ownership",
            new=AsyncMock(return_value=(True, "github_app", "github", "555")),
        ):
            resp = await client.post(
                f"/api/v1/projects/{claimant.id}/repos",
                json={"git_remote": "github.com/acme/new-name"},
                headers=auth_headers,
            )
        assert resp.status_code == 409


# --- 2 ---------------------------------------------------------------------


class TestOneInstallationPerUserEnforced:
    @pytest.mark.asyncio
    async def test_database_refuses_two_links_for_one_user(self, db_session, test_user):
        db_session.add(GitHubInstallation(id=910001, user_id=test_user.id))
        await db_session.commit()
        db_session.add(GitHubInstallation(id=910002, user_id=test_user.id))
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()
        # Unlinked rows are unconstrained.
        db_session.add_all([GitHubInstallation(id=910003), GitHubInstallation(id=910004)])
        await db_session.commit()

    @pytest.mark.asyncio
    async def test_concurrent_binds_leave_exactly_one_link(self, file_db, test_user):
        from sessionfs.server.routes.settings import _bind_installation

        async with file_db() as db:
            db.add(User(id=test_user.id, email="c@example.com", tier="pro",
                        email_verified=True, created_at=_now()))
            await db.commit()
        gh_user = github_oauth.GitHubUser(id=1001, login="alice")
        infos = [
            github_oauth.UserInstallation(
                id=920000 + i, account_id=i, account_login=f"org{i}", account_type="Organization"
            )
            for i in range(4)
        ]

        async def bind(info):
            async with file_db() as db:
                return await _bind_installation(
                    db, user_id=test_user.id, gh_user=gh_user, info=info, role="org_admin"
                )

        results = await asyncio.gather(*(bind(i) for i in infos))
        assert any(results)
        async with file_db() as db:
            linked = (
                await db.execute(
                    select(GitHubInstallation).where(GitHubInstallation.user_id == test_user.id)
                )
            ).scalars().all()
        assert len(linked) == 1


# --- 3 ---------------------------------------------------------------------


class TestPublishSerialized:
    @pytest.mark.asyncio
    async def test_slow_older_publish_cannot_land_after_newer(
        self, file_db, monkeypatch
    ):
        async with file_db() as db:
            db.add(GitHubInstallation(
                id=1, user_id="u-owner", account_login="acme",
                account_type="Organization", auto_comment=True,
            ))
            db.add(PRComment(
                id="pc1", installation_id=1, repo_full_name="acme/r", pr_number=1,
                comment_id=4242, session_ids="[]", desired_body="v2", body_version=2,
                published_version=1, created_at=_now(), updated_at=_now(),
                owner_user_id="u-owner",
            ))
            await db.commit()

        github_body: list[str] = []

        async def fake_post(token, repo, pr, body, existing_comment_id=None):
            if body == "v2":
                await asyncio.sleep(0.3)  # the older publish is slow
            github_body.append(body)
            return existing_comment_id

        monkeypatch.setattr(webhooks, "post_or_update_comment", fake_post)

        async def publisher():
            async with file_db() as db:
                await webhooks._publish_newest(db, "tok", "pc1", "acme/r", 1, 4242)

        async def newer_event_then_publish():
            await asyncio.sleep(0.05)  # publisher A is mid-PATCH with v2
            async with file_db() as db:
                await webhooks._record_desired_comment(
                    db, 1, "acme/r", 1, "v3", "[]", owner_user_id="u-owner"
                )
            await publisher()

        await asyncio.gather(publisher(), newer_event_then_publish())
        # Serialized: v2 completes before v3 is even read; no out-of-order
        # PATCH (which would show up as v3, v2, v3).
        assert github_body == ["v2", "v3"]
        async with file_db() as db:
            row = await db.get(PRComment, "pc1")
            assert row.published_version == row.body_version == 3


# --- 4 ---------------------------------------------------------------------


class TestDeliveryLease:
    @pytest.mark.asyncio
    async def test_stale_worker_cannot_overwrite_replacement_outcome(self, file_db):
        fp = "f" * 64
        outcome, lease1 = await webhooks._claim_delivery(fp, "d1", "ping", "{}")
        assert (outcome, lease1) == ("claimed", 1)
        # Worker 1 stalls; its claim goes stale and a redelivery takes over.
        async with file_db() as db:
            row = await db.get(GitHubWebhookDelivery, fp)
            row.claimed_at = _now() - timedelta(hours=1)
            await db.commit()
        outcome, lease2 = await webhooks._claim_delivery(fp, "d2", "ping", "{}")
        assert (outcome, lease2) == ("claimed", 2)
        # The stale worker finishes late with a failure: ignored.
        assert await webhooks._finish_delivery(fp, False, lease1) is False
        async with file_db() as db:
            assert (await db.get(GitHubWebhookDelivery, fp)).status == "processing"
        assert await webhooks._finish_delivery(fp, True, lease2) is True
        async with file_db() as db:
            row = await db.get(GitHubWebhookDelivery, fp)
            assert row.status == "completed" and row.payload is None


# --- 5 ---------------------------------------------------------------------


def _signed_headers(body: bytes, event: str) -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "Content-Type": "application/json",
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-Hub-Signature-256": "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest(),
    }


PR_BODY = json.dumps({"action": "opened", "pull_request": {"head": {"ref": "x"}, "number": 3}, "repository": {"full_name": "acme/repo", "clone_url": "https://github.com/acme/repo.git"}, "installation": {"id": 1}}).encode()


class TestRetries:
    @pytest.fixture(autouse=True)
    def _signed(self, monkeypatch):
        monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "whsec")
        monkeypatch.delenv("SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED", raising=False)

    async def _admin_headers(self, db: AsyncSession) -> dict[str, str]:
        admin = User(id=str(uuid.uuid4()), email=f"a{uuid.uuid4().hex[:6]}@sessionfs.dev",
                     tier="admin", email_verified=True, created_at=_now())
        db.add(admin)
        raw = generate_api_key()
        db.add(ApiKey(id=str(uuid.uuid4()), user_id=admin.id, key_hash=hash_api_key(raw),
                      name="k", created_at=_now()))
        await db.commit()
        return {"Authorization": f"Bearer {raw}"}

    @pytest.mark.asyncio
    async def test_failed_pr_work_retried_by_sweeper_from_stored_payload(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch
    ):
        calls: list[dict] = []
        outcome = {"ok": False}

        async def fake_handle(payload, fence=None):
            calls.append(payload)
            return outcome["ok"]

        monkeypatch.setattr(webhooks, "_handle_pr_event", fake_handle)
        await client.post("/webhooks/github", content=PR_BODY, headers=_signed_headers(PR_BODY, "pull_request"))
        await asyncio.sleep(0.05)
        row = (await _all(db_session, GitHubWebhookDelivery))[0]
        assert row.status == "failed" and row.payload == PR_BODY.decode()
        assert row.next_attempt_at is not None

        headers = await self._admin_headers(db_session)
        # Backoff not elapsed yet: nothing retried.
        resp = await client.post("/api/v1/admin/github-webhook-deliveries/sweep", headers=headers)
        assert resp.json()["deliveries_retried"] == 0
        row.next_attempt_at = _now() - timedelta(seconds=1)
        await db_session.commit()

        outcome["ok"] = True
        resp = await client.post("/api/v1/admin/github-webhook-deliveries/sweep", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["deliveries_retried"] == 1
        assert resp.json()["retries_succeeded"] == 1
        assert calls[-1] == json.loads(PR_BODY)
        row = (await _all(db_session, GitHubWebhookDelivery))[0]
        assert row.status == "completed" and row.attempts == 2 and row.payload is None

    @pytest.mark.asyncio
    async def test_max_attempts_stops_automatic_retry(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        db_session.add(GitHubWebhookDelivery(
            fingerprint="m" * 64, event="pull_request", status="failed",
            attempts=webhooks.WEBHOOK_MAX_ATTEMPTS, received_at=_now(), claimed_at=_now(),
            next_attempt_at=_now() - timedelta(minutes=1), payload=PR_BODY.decode(),
        ))
        await db_session.commit()
        stats = await webhooks.retry_due_deliveries(10)
        assert stats["retried"] == 0

    @pytest.mark.asyncio
    async def test_stale_processing_retried(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch
    ):
        calls: list = []

        async def fake_handle(payload, fence=None):
            calls.append(payload)
            return True

        monkeypatch.setattr(webhooks, "_handle_pr_event", fake_handle)
        db_session.add(GitHubWebhookDelivery(
            fingerprint="s" * 64, event="pull_request", status="processing", attempts=1,
            received_at=_now() - timedelta(hours=1), claimed_at=_now() - timedelta(hours=1),
            payload=PR_BODY.decode(),
        ))
        await db_session.commit()
        stats = await webhooks.retry_due_deliveries(10)
        assert stats == {"retried": 1, "succeeded": 1, "failed": 0}
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_incoming_delivery_retries_opportunistically(
        self, client: AsyncClient, file_db, monkeypatch
    ):
        monkeypatch.setattr(webhooks, "_OPPORTUNISTIC_RETRY_LIMIT", 2)
        calls: list = []

        async def fake_handle(payload, fence=None):
            calls.append(payload)
            return True

        monkeypatch.setattr(webhooks, "_handle_pr_event", fake_handle)
        async with file_db() as db:
            db.add(GitHubWebhookDelivery(
                fingerprint="o" * 64, event="pull_request", status="failed", attempts=1,
                received_at=_now(), claimed_at=_now(),
                next_attempt_at=_now() - timedelta(minutes=1), payload=PR_BODY.decode(),
            ))
            await db.commit()
        body = b'{"zen": "hi"}'
        resp = await client.post("/webhooks/github", content=body, headers=_signed_headers(body, "ping"))
        assert resp.status_code == 200
        status = None
        for _ in range(100):
            await asyncio.sleep(0.02)
            async with file_db() as db:
                status = (await db.get(GitHubWebhookDelivery, "o" * 64)).status
            if status == "completed":
                break
        assert status == "completed"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_old_payloads_purged_tombstone_kept(self, client: AsyncClient, db_session: AsyncSession):
        db_session.add(GitHubWebhookDelivery(
            fingerprint="p" * 64, event="pull_request", status="failed", attempts=5,
            received_at=_now() - timedelta(days=31), claimed_at=_now() - timedelta(days=31),
            payload=PR_BODY.decode(),
        ))
        await db_session.commit()
        headers = await self._admin_headers(db_session)
        resp = await client.post("/api/v1/admin/github-webhook-deliveries/sweep", headers=headers)
        assert resp.json()["payloads_purged"] == 1
        assert resp.json()["deliveries_purged"] == 0
        (row,) = await _all(db_session, GitHubWebhookDelivery)
        assert row.payload is None


# --- 6 ---------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
class TestHelmGithubDashboardUrl:
    BASE = [
        "--set", "api.github.enabled=true",
        "--set", "api.github.appId=12345",
        "--set", "api.github.clientId=Iv1.x",
        "--set", "api.github.existingSecret=gh",
        "--set", "api.github.callbackUrl=https://api.test/api/v1/settings/github/oauth/callback",
    ]

    def _render(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["helm", "template", "t", str(REPO_ROOT / "charts" / "sessionfs"), *self.BASE, *extra],
            capture_output=True, text=True, timeout=120,
        )

    def test_fails_without_dashboard_url(self):
        result = self._render()
        assert result.returncode != 0
        assert "requires a dashboard URL" in result.stderr

    def test_renders_with_github_dashboard_url(self):
        result = self._render("--set", "api.github.dashboardUrl=https://app.test")
        assert result.returncode == 0, result.stderr
        assert 'SFS_DASHBOARD_URL: "https://app.test"' in result.stdout

    def test_renders_reusing_sso_dashboard_url(self):
        result = self._render(
            "--set", "api.sso.enabled=true",
            "--set", "api.sso.dashboardUrl=https://sso.test",
            "--set", "api.sso.callbackUrl=https://api.test/cb",
        )
        assert result.returncode == 0, result.stderr
        assert 'SFS_DASHBOARD_URL: "https://sso.test"' in result.stdout

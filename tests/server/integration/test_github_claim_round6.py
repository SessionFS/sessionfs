"""Regression tests for the sixth review round.

1. Replay fingerprints outlive payloads (30-day payload, 400-day tombstone).
2. A GitHub redelivery cannot bypass backoff or the attempt limit; only an
   admin-authenticated retry can.
3. No database transaction is open while GitHub is called (publishing uses
   a committed lease, not a row lock).
4. Helm provisions the full GitHub integration (App ID, private key,
   webhook secret, client secret) or refuses to render.
5. Linking a renamed repository to the project that already holds it under
   the old name renames the row in place.
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
from sessionfs.server.db.models import Session as SessionRow
from sessionfs.server.routes import webhooks

REPO_ROOT = Path(__file__).resolve().parents[3]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _all(db: AsyncSession, model):
    return (
        await db.execute(select(model).execution_options(populate_existing=True))
    ).scalars().all()


def _signed(body: bytes, event: str) -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "Content-Type": "application/json",
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-Hub-Signature-256": "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest(),
    }


@pytest.fixture(autouse=True)
def _signed_mode(monkeypatch):
    monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "whsec")
    monkeypatch.delenv("SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED", raising=False)


async def _admin_headers(db: AsyncSession) -> dict[str, str]:
    admin = User(id=str(uuid.uuid4()), email=f"a{uuid.uuid4().hex[:6]}@sessionfs.dev",
                 tier="admin", email_verified=True, created_at=_now())
    db.add(admin)
    raw = generate_api_key()
    db.add(ApiKey(id=str(uuid.uuid4()), user_id=admin.id, key_hash=hash_api_key(raw),
                  name="k", created_at=_now()))
    await db.commit()
    return {"Authorization": f"Bearer {raw}"}


PR_BODY = json.dumps({"action": "opened", "pull_request": {"head": {"ref": "x"}, "number": 2}, "repository": {"full_name": "acme/repo", "clone_url": "https://github.com/acme/repo.git"}, "installation": {"id": 1}}).encode()


@pytest.fixture
def pr_handler(monkeypatch):
    state = {"ok": True, "calls": 0}

    async def fake_handle(payload, fence=None):
        state["calls"] += 1
        return state["ok"]

    monkeypatch.setattr(webhooks, "_handle_pr_event", fake_handle)
    return state


# --- 1 ---------------------------------------------------------------------


class TestTombstones:
    @pytest.mark.asyncio
    async def test_replay_after_payload_purge_is_still_a_duplicate(
        self, client: AsyncClient, db_session: AsyncSession, pr_handler
    ):
        """HIGH: purging must not forget the fingerprint."""
        fp = webhooks._delivery_fingerprint(PR_BODY)
        db_session.add(GitHubWebhookDelivery(
            fingerprint=fp, event="pull_request", status="completed", attempts=1,
            received_at=_now() - timedelta(days=200), claimed_at=_now() - timedelta(days=200),
        ))
        await db_session.commit()
        headers = await _admin_headers(db_session)
        await client.post("/api/v1/admin/github-webhook-deliveries/sweep", headers=headers)
        resp = await client.post("/webhooks/github", content=PR_BODY, headers=_signed(PR_BODY, "pull_request"))
        assert resp.json()["status"] == "duplicate"
        assert pr_handler["calls"] == 0

    @pytest.mark.asyncio
    async def test_tombstone_retention_is_long(self):
        assert webhooks.WEBHOOK_TOMBSTONE_RETENTION >= timedelta(days=365)
        assert webhooks.WEBHOOK_PAYLOAD_RETENTION <= timedelta(days=30)

    @pytest.mark.asyncio
    async def test_sweeper_cannot_set_tombstones_below_payload_window(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        db_session.add(GitHubWebhookDelivery(
            fingerprint="q" * 64, event="ping", status="completed", attempts=1,
            received_at=_now() - timedelta(days=10), claimed_at=_now() - timedelta(days=10),
        ))
        await db_session.commit()
        headers = await _admin_headers(db_session)
        resp = await client.post(
            "/api/v1/admin/github-webhook-deliveries/sweep",
            headers=headers,
            json={"retention_days": 30, "tombstone_retention_days": 1},
        )
        assert resp.json()["deliveries_purged"] == 0
        assert len(await _all(db_session, GitHubWebhookDelivery)) == 1

    def test_docs_say_rotate_webhook_secret(self):
        doc = (REPO_ROOT / "docs" / "github-app.md").read_text()
        assert "400 days" in doc
        assert "rotate the App's webhook secret at least once a year" in doc


# --- 2 ---------------------------------------------------------------------


class TestRedeliveryLimits:
    async def _failed(self, db, *, attempts: int, next_attempt_at, payload=True):
        fp = webhooks._delivery_fingerprint(PR_BODY)
        db.add(GitHubWebhookDelivery(
            fingerprint=fp, event="pull_request", status="failed", attempts=attempts,
            received_at=_now(), claimed_at=_now(), next_attempt_at=next_attempt_at,
            payload=PR_BODY.decode() if payload else None,
        ))
        await db.commit()
        return fp

    @pytest.mark.asyncio
    async def test_redelivery_during_backoff_deferred(
        self, client: AsyncClient, db_session: AsyncSession, pr_handler
    ):
        await self._failed(db_session, attempts=1, next_attempt_at=_now() + timedelta(minutes=5))
        resp = await client.post("/webhooks/github", content=PR_BODY, headers=_signed(PR_BODY, "pull_request"))
        assert resp.json()["status"] == "deferred"
        assert pr_handler["calls"] == 0

    @pytest.mark.asyncio
    async def test_redelivery_past_attempt_limit_deferred(
        self, client: AsyncClient, db_session: AsyncSession, pr_handler
    ):
        await self._failed(
            db_session, attempts=webhooks.WEBHOOK_MAX_ATTEMPTS,
            next_attempt_at=_now() - timedelta(minutes=5),
        )
        for _ in range(3):
            resp = await client.post("/webhooks/github", content=PR_BODY, headers=_signed(PR_BODY, "pull_request"))
            assert resp.json()["status"] == "deferred"
        assert pr_handler["calls"] == 0

    @pytest.mark.asyncio
    async def test_stale_processing_past_limit_not_taken_over(
        self, client: AsyncClient, db_session: AsyncSession, pr_handler
    ):
        fp = webhooks._delivery_fingerprint(PR_BODY)
        db_session.add(GitHubWebhookDelivery(
            fingerprint=fp, event="pull_request", status="processing",
            attempts=webhooks.WEBHOOK_MAX_ATTEMPTS, received_at=_now(),
            claimed_at=_now() - timedelta(hours=1),
        ))
        await db_session.commit()
        resp = await client.post("/webhooks/github", content=PR_BODY, headers=_signed(PR_BODY, "pull_request"))
        assert resp.json()["status"] == "in_progress"
        assert pr_handler["calls"] == 0

    @pytest.mark.asyncio
    async def test_admin_force_retry_overrides_limit(
        self, client: AsyncClient, db_session: AsyncSession, pr_handler
    ):
        fp = await self._failed(
            db_session, attempts=webhooks.WEBHOOK_MAX_ATTEMPTS,
            next_attempt_at=_now() + timedelta(hours=1),
        )
        headers = await _admin_headers(db_session)
        resp = await client.post(f"/api/v1/admin/github-webhook-deliveries/{fp}/retry", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["outcome"] == "succeeded"
        assert pr_handler["calls"] == 1
        (row,) = await _all(db_session, GitHubWebhookDelivery)
        assert row.status == "completed"

    @pytest.mark.asyncio
    async def test_admin_force_retry_without_payload_reenables_redelivery(
        self, client: AsyncClient, db_session: AsyncSession, pr_handler
    ):
        fp = await self._failed(
            db_session, attempts=webhooks.WEBHOOK_MAX_ATTEMPTS,
            next_attempt_at=None, payload=False,
        )
        headers = await _admin_headers(db_session)
        resp = await client.post(f"/api/v1/admin/github-webhook-deliveries/{fp}/retry", headers=headers)
        assert resp.json()["outcome"] == "no_payload"
        again = await client.post("/webhooks/github", content=PR_BODY, headers=_signed(PR_BODY, "pull_request"))
        assert again.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_force_retry_requires_admin(self, client: AsyncClient, auth_headers):
        resp = await client.post(
            f"/api/v1/admin/github-webhook-deliveries/{'a' * 64}/retry", headers=auth_headers
        )
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_force_retry_validates_fingerprint(self, client: AsyncClient, db_session):
        headers = await _admin_headers(db_session)
        resp = await client.post(
            "/api/v1/admin/github-webhook-deliveries/not-a-fingerprint/retry", headers=headers
        )
        assert resp.status_code == 400


# --- 3 ---------------------------------------------------------------------


@pytest.fixture
async def tracked_db(tmp_path, monkeypatch):
    """File-backed engine whose sessions are recorded, so fakes can assert
    that no transaction is open while 'GitHub' is being called."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'r6.db'}", connect_args={"timeout": 30}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sessions: list[AsyncSession] = []

    def tracked():
        s = factory()
        sessions.append(s)
        return s

    monkeypatch.setattr(engine_mod, "_session_factory", tracked)
    yield tracked, sessions
    await engine.dispose()


PR_PAYLOAD = {
    "pull_request": {"head": {"ref": "feat"}, "number": 9},
    "repository": {"full_name": "acme/repo", "clone_url": "https://github.com/acme/repo.git"},
    "installation": {"id": 77},
}


class TestNoTransactionAcrossGitHubCalls:
    @pytest.fixture(autouse=True)
    async def _installations(self, tracked_db, test_user):
        # The publish paths revalidate the installation before any GitHub
        # write (tk_b6c5e184ba8d4aa1), so these tests need live opted-in
        # installation rows (77 = PR_PAYLOAD, 1 = direct PRComment rows).
        factory, _ = tracked_db
        async with factory() as db:
            for installation_id, owner in ((1, "u-owner"), (77, test_user.id)):
                db.add(GitHubInstallation(
                    id=installation_id, user_id=owner, account_login="acme",
                    account_type="Organization", auto_comment=True,
                ))
            await db.commit()

    @pytest.mark.asyncio
    async def test_create_and_edit_paths(self, tracked_db, test_user, monkeypatch):
        factory, sessions = tracked_db
        async with factory() as db:
            db.add(User(id=test_user.id, email="t@example.com", tier="pro",
                        email_verified=True, created_at=_now()))
            db.add(SessionRow(
                id=f"ses_{uuid.uuid4().hex[:16]}", user_id=test_user.id, title="t",
                source_tool="claude-code", blob_key="k", etag="e",
                git_remote_normalized=webhooks.normalize_git_remote(
                    PR_PAYLOAD["repository"]["clone_url"]  # type: ignore[index]
                ),
                git_branch="feat", message_count=1,
            ))
            await db.commit()

        violations: list[str] = []

        def check(name: str) -> None:
            open_ = [s for s in sessions if s.in_transaction()]
            if open_:
                violations.append(name)

        comments: dict[int, str] = {}

        async def fake_token(installation_id):
            check("token")
            return "tok"

        async def fake_find(token, repo, pr, marker):
            check("find")
            return None

        async def fake_post(token, repo, pr, body, existing_comment_id=None):
            check("post")
            cid = existing_comment_id or 5000
            comments[cid] = body
            return cid

        monkeypatch.setattr(webhooks, "get_installation_token", fake_token)
        monkeypatch.setattr(webhooks, "find_marked_comment", fake_find)
        monkeypatch.setattr(webhooks, "post_or_update_comment", fake_post)

        assert await webhooks._handle_pr_event(PR_PAYLOAD) is True  # create
        async with factory() as db:
            db.add(SessionRow(
                id=f"ses_{uuid.uuid4().hex[:16]}", user_id=test_user.id, title="second",
                source_tool="claude-code", blob_key="k", etag="e2",
                git_remote_normalized=webhooks.normalize_git_remote(
                    PR_PAYLOAD["repository"]["clone_url"]  # type: ignore[index]
                ),
                git_branch="feat", message_count=1,
            ))
            await db.commit()
        assert await webhooks._handle_pr_event(PR_PAYLOAD) is True  # edit
        assert violations == []
        assert "second" in comments[5000]

    @pytest.mark.asyncio
    async def test_publish_lease_blocks_concurrent_publisher_without_db_lock(
        self, tracked_db, monkeypatch
    ):
        factory, sessions = tracked_db
        async with factory() as db:
            db.add(PRComment(
                id="pc", installation_id=1, repo_full_name="acme/r", pr_number=1,
                comment_id=4242, session_ids="[]", desired_body="v2", body_version=2,
                published_version=1, created_at=_now(), updated_at=_now(),
                owner_user_id="u-owner",
            ))
            await db.commit()
        bodies: list[str] = []
        lock_free_writes: list[bool] = []

        async def fake_post(token, repo, pr, body, existing_comment_id=None):
            if body == "v2":
                # While v2 is being published, another session can still
                # write the row (no lock is held across this call).
                async with factory() as db:
                    await webhooks._record_desired_comment(
                        db, 1, "acme/r", 1, "v3", "[]", owner_user_id="u-owner"
                    )
                    lock_free_writes.append(True)
                await asyncio.sleep(0.1)
            assert not any(s.in_transaction() for s in sessions)
            bodies.append(body)
            return existing_comment_id

        monkeypatch.setattr(webhooks, "post_or_update_comment", fake_post)

        async def publisher():
            async with factory() as db:
                await webhooks._publish_newest(db, "t", "pc", "acme/r", 1, 4242)

        async def second_publisher():
            await asyncio.sleep(0.05)
            await publisher()  # lease held by the first: backs off

        await asyncio.gather(publisher(), second_publisher())
        assert lock_free_writes == [True]
        assert bodies == ["v2", "v3"]
        async with factory() as db:
            row = await db.get(PRComment, "pc")
            assert row.published_version == row.body_version == 3
            assert row.publish_lease is None

    @pytest.mark.asyncio
    async def test_expired_lease_is_taken_over(self, tracked_db, monkeypatch):
        factory, _ = tracked_db
        async with factory() as db:
            db.add(PRComment(
                id="pc2", installation_id=1, repo_full_name="acme/r", pr_number=2,
                comment_id=9, session_ids="[]", desired_body="v5", body_version=5,
                published_version=4, publish_lease="dead-worker",
                publish_lease_until=_now() - timedelta(seconds=1),
                created_at=_now(), updated_at=_now(), owner_user_id="u-owner",
            ))
            await db.commit()
        bodies: list[str] = []

        async def fake_post(token, repo, pr, body, existing_comment_id=None):
            bodies.append(body)
            return existing_comment_id

        monkeypatch.setattr(webhooks, "post_or_update_comment", fake_post)
        async with factory() as db:
            await webhooks._publish_newest(db, "t", "pc2", "acme/r", 2, 9)
        assert bodies == ["v5"]


# --- 4 ---------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
class TestHelmFullGithubIntegration:
    FULL = [
        "--set", "api.github.enabled=true",
        "--set", "api.github.appId=12345",
        "--set", "api.github.clientId=Iv1.x",
        "--set", "api.github.existingSecret=gh",
        "--set", "api.github.callbackUrl=https://api.test/api/v1/settings/github/oauth/callback",
        "--set", "api.github.dashboardUrl=https://app.test",
    ]

    def _render(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["helm", "template", "t", str(REPO_ROOT / "charts" / "sessionfs"), *args],
            capture_output=True, text=True, timeout=120,
        )

    def test_full_render_wires_every_secret(self):
        result = self._render(*self.FULL)
        assert result.returncode == 0, result.stderr
        out = result.stdout
        assert 'SFS_GITHUB_APP_ID: "12345"' in out
        for env, key in (
            ("SFS_GITHUB_APP_CLIENT_SECRET", "github-client-secret"),
            ("SFS_GITHUB_APP_PRIVATE_KEY", "github-private-key"),
            ("SFS_GITHUB_WEBHOOK_SECRET", "github-webhook-secret"),
        ):
            i = out.index(f"name: {env}")
            assert f"key: {key}" in out[i:i + 200]
            assert "name: gh" in out[i:i + 200]

    @pytest.mark.parametrize(
        "override, message",
        [
            ("api.github.appId=", "api.github.appId is required"),
            ("api.github.existingSecret=", "api.github.existingSecret is required"),
            ("api.github.privateKeyKey=", "api.github.privateKeyKey is required"),
            ("api.github.webhookSecretKey=", "api.github.webhookSecretKey is required"),
            ("api.github.clientSecretKey=", "api.github.clientSecretKey is required"),
        ],
    )
    def test_missing_piece_fails(self, override, message):
        result = self._render(*self.FULL, "--set", override)
        assert result.returncode != 0
        assert message in result.stderr


# --- 5 ---------------------------------------------------------------------


class TestRenameInSameProject:
    @pytest.mark.asyncio
    async def test_rename_updates_existing_row_in_place(
        self, client: AsyncClient, auth_headers, test_user, db_session
    ):
        project = Project(
            id=f"proj_{uuid.uuid4().hex[:16]}", name="p",
            git_remote_normalized="github.com/acme/old-name", context_document="",
            owner_id=test_user.id,
        )
        db_session.add(project)
        await db_session.commit()
        row = ProjectRepo(
            id=f"repo_{uuid.uuid4().hex[:16]}", project_id=project.id,
            git_remote_normalized="github.com/acme/old-name", provider="github",
            provider_repo_id="777", is_primary=True, verified=True,
            verification_method="github_app",
        )
        db_session.add(row)
        await db_session.commit()
        row_id = row.id

        with patch(
            "sessionfs.server.github_app.verify_repo_ownership",
            new=AsyncMock(return_value=(True, "github_app", "github", "777")),
        ):
            resp = await client.post(
                f"/api/v1/projects/{project.id}/repos",
                json={"git_remote": "github.com/acme/new-name"},
                headers=auth_headers,
            )
        assert resp.status_code in (200, 201), resp.text
        assert resp.json()["id"] == row_id
        assert resp.json()["git_remote_normalized"] == "github.com/acme/new-name"
        rows = await _all(db_session, ProjectRepo)
        assert [(r.id, r.git_remote_normalized) for r in rows] == [
            (row_id, "github.com/acme/new-name")
        ]
        refreshed = (
            await db_session.execute(
                select(Project).where(Project.id == project.id).execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert refreshed.git_remote_normalized == "github.com/acme/new-name"

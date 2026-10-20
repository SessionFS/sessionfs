"""GitHub webhook replay protection, delivery lifecycle and PR comments.

- Replays are keyed on sha256(event + signed body), not the unsigned
  X-GitHub-Delivery header, so changing that header cannot mint new work.
- Deliveries move processing -> completed | failed. Only completed ones are
  permanent no-ops; failed and stale-processing deliveries are retried.
- Rows are pruned after 30 days (by the webhook and the admin sweeper,
  which also reaps dead GitHub claim attempts).
- PR comments carry a hidden marker: a comment created before a crash is
  adopted, not duplicated; events arriving while the comment is being
  created are published afterwards, not lost.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AdminAction,
    ApiKey,
    GitHubClaimAttempt,
    GitHubInstallation,
    GitHubWebhookDelivery,
    PRComment,
    User,
)
from sessionfs.server.db.models import Session as SessionRow
from sessionfs.server.routes import webhooks


@pytest.fixture(autouse=True)
def _signed_mode(monkeypatch):
    monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "whsec")
    monkeypatch.delenv("SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED", raising=False)


def _signed(body: bytes, event: str, delivery: str | None) -> dict[str, str]:
    headers = {
        "X-GitHub-Event": event,
        "Content-Type": "application/json",
        "X-Hub-Signature-256": "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest(),
    }
    if delivery is not None:
        headers["X-GitHub-Delivery"] = delivery
    return headers


async def _all(db: AsyncSession, model):
    return (
        await db.execute(select(model).execution_options(populate_existing=True))
    ).scalars().all()


def _delivery_row(fp: str, status: str, *, claimed_ago: timedelta, received_ago=None):
    now = datetime.now(timezone.utc)
    return GitHubWebhookDelivery(
        fingerprint=fp, delivery_id=None, event="ping", status=status, attempts=1,
        received_at=now - (received_ago or claimed_ago), claimed_at=now - claimed_ago,
    )


PR_BODY = json.dumps({"action": "opened", "pull_request": {"head": {"ref": "x"}, "number": 1}, "repository": {"full_name": "acme/repo", "clone_url": "https://github.com/acme/repo.git"}, "installation": {"id": 1}}).encode()


@pytest.fixture
def pr_handler(monkeypatch):
    handled: list[dict] = []
    outcome = {"ok": True}

    async def fake_handle(payload, fence=None):
        handled.append(payload)
        return outcome["ok"]

    monkeypatch.setattr(webhooks, "_handle_pr_event", fake_handle)
    return handled, outcome


class TestReplayFingerprint:
    @pytest.mark.asyncio
    async def test_replay_with_same_headers_runs_once(self, client: AsyncClient, pr_handler):
        handled, _ = pr_handler
        headers = _signed(PR_BODY, "pull_request", "d0a1b2c3-1")
        first = await client.post("/webhooks/github", content=PR_BODY, headers=headers)
        await asyncio.sleep(0.01)
        replay = await client.post("/webhooks/github", content=PR_BODY, headers=headers)
        assert first.json()["status"] == "ok"
        assert replay.json()["status"] == "duplicate"
        assert len(handled) == 1

    @pytest.mark.asyncio
    async def test_fresh_delivery_header_does_not_bypass_dedupe(
        self, client: AsyncClient, pr_handler
    ):
        """R4 finding 2: X-GitHub-Delivery is unsigned; rotating it must not
        create new work for the same signed payload."""
        handled, _ = pr_handler
        await client.post(
            "/webhooks/github", content=PR_BODY, headers=_signed(PR_BODY, "pull_request", "a-1")
        )
        await asyncio.sleep(0.01)
        for i in range(5):
            resp = await client.post(
                "/webhooks/github", content=PR_BODY,
                headers=_signed(PR_BODY, "pull_request", f"attacker-{i}"),
            )
            assert resp.json()["status"] == "duplicate"
        assert len(handled) == 1

    @pytest.mark.asyncio
    async def test_missing_delivery_header_is_fine(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        body = b'{"zen": "hi"}'
        resp = await client.post(
            "/webhooks/github", content=body, headers=_signed(body, "ping", None)
        )
        assert resp.status_code == 200
        rows = await _all(db_session, GitHubWebhookDelivery)
        assert len(rows) == 1 and rows[0].delivery_id is None and rows[0].status == "completed"
        assert rows[0].fingerprint == webhooks._delivery_fingerprint(body)

    @pytest.mark.asyncio
    async def test_replayed_installation_delete_does_nothing(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        body = json.dumps({
            "action": "deleted",
            "installation": {
                "id": 810101, "account": {"id": 1, "login": "v", "type": "User"},
                "app_id": 1, "target_type": "User",
            },
        }).encode()
        assert (await client.post(
            "/webhooks/github", content=body, headers=_signed(body, "installation", "del-1")
        )).status_code == 200
        db_session.add(GitHubInstallation(id=810101, account_login="v"))  # reinstalled
        await db_session.commit()
        replay = await client.post(
            "/webhooks/github", content=body, headers=_signed(body, "installation", "del-2")
        )
        assert replay.json()["status"] == "duplicate"
        assert [r.id for r in await _all(db_session, GitHubInstallation)] == [810101]


class TestDeliveryLifecycle:
    @pytest.mark.asyncio
    async def test_failed_pr_work_is_retried_by_redelivery(
        self, client: AsyncClient, db_session: AsyncSession, pr_handler
    ):
        """R4 finding 4: a delivery whose work failed is not a permanent
        no-op; a redelivery runs it again and then completes it."""
        handled, outcome = pr_handler
        outcome["ok"] = False
        headers = _signed(PR_BODY, "pull_request", "r-1")
        await client.post("/webhooks/github", content=PR_BODY, headers=headers)
        await asyncio.sleep(0.05)
        row = (await _all(db_session, GitHubWebhookDelivery))[0]
        assert row.status == "failed"
        # Within the backoff a redelivery is deferred ...
        early = await client.post("/webhooks/github", content=PR_BODY, headers=headers)
        assert early.json()["status"] == "deferred"
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db_session.commit()
        # ... after it, the redelivery runs the work again.
        outcome["ok"] = True
        again = await client.post("/webhooks/github", content=PR_BODY, headers=headers)
        await asyncio.sleep(0.05)
        assert again.json()["status"] == "ok"
        assert len(handled) == 2
        row = (await _all(db_session, GitHubWebhookDelivery))[0]
        assert row.status == "completed" and row.attempts == 2
        third = await client.post("/webhooks/github", content=PR_BODY, headers=headers)
        assert third.json()["status"] == "duplicate"

    @pytest.mark.asyncio
    async def test_fresh_processing_claim_is_in_progress(
        self, client: AsyncClient, db_session: AsyncSession, pr_handler
    ):
        handled, _ = pr_handler
        fp = webhooks._delivery_fingerprint(PR_BODY)
        db_session.add(_delivery_row(fp, "processing", claimed_ago=timedelta(minutes=1)))
        await db_session.commit()
        resp = await client.post(
            "/webhooks/github", content=PR_BODY, headers=_signed(PR_BODY, "pull_request", "x")
        )
        assert resp.json()["status"] == "in_progress"
        assert handled == []

    @pytest.mark.asyncio
    async def test_stale_processing_claim_is_taken_over(
        self, client: AsyncClient, db_session: AsyncSession, pr_handler
    ):
        handled, _ = pr_handler
        fp = webhooks._delivery_fingerprint(PR_BODY)
        db_session.add(_delivery_row(fp, "processing", claimed_ago=timedelta(minutes=30)))
        await db_session.commit()
        resp = await client.post(
            "/webhooks/github", content=PR_BODY, headers=_signed(PR_BODY, "pull_request", "x")
        )
        await asyncio.sleep(0.05)
        assert resp.json()["status"] == "ok"
        assert len(handled) == 1
        assert (await _all(db_session, GitHubWebhookDelivery))[0].status == "completed"

    @pytest.mark.asyncio
    async def test_failed_installation_event_returns_500_and_stays_retryable(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch
    ):
        async def boom(payload):
            raise RuntimeError("db down")

        monkeypatch.setattr(webhooks, "_apply_installation_event", boom)
        body = json.dumps({"action": "created", "installation": {
            "id": 5, "account": {"id": 1, "login": "o", "type": "Organization"},
            "app_id": 1, "target_type": "Organization",
        }}).encode()
        resp = await client.post(
            "/webhooks/github", content=body, headers=_signed(body, "installation", "i-1")
        )
        assert resp.status_code == 500
        assert (await _all(db_session, GitHubWebhookDelivery))[0].status == "failed"

    @pytest.mark.asyncio
    async def test_invalid_json_marks_failed(self, client: AsyncClient, db_session: AsyncSession):
        body = b"{not json"
        resp = await client.post(
            "/webhooks/github", content=body, headers=_signed(body, "ping", "j-1")
        )
        assert resp.status_code == 400
        assert (await _all(db_session, GitHubWebhookDelivery))[0].status == "failed"

    @pytest.mark.asyncio
    async def test_old_rows_pruned_on_delivery(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        old = _delivery_row("a" * 64, "failed", claimed_ago=timedelta(days=31))
        old.payload = "{}"
        db_session.add(old)
        db_session.add(_delivery_row("c" * 64, "completed", claimed_ago=timedelta(days=401)))
        db_session.add(_delivery_row("b" * 64, "completed", claimed_ago=timedelta(days=1)))
        await db_session.commit()
        body = b'{"zen": "hi"}'
        await client.post("/webhooks/github", content=body, headers=_signed(body, "ping", "f-1"))
        rows = {r.fingerprint: r for r in await _all(db_session, GitHubWebhookDelivery)}
        # Payload dropped after 30 days, tombstone kept until 400 days.
        assert set(rows) == {"a" * 64, "b" * 64, webhooks._delivery_fingerprint(body)}
        assert rows["a" * 64].payload is None


class TestAdminSweeper:
    @pytest.mark.asyncio
    async def test_purges_and_audit_fits_postgres_column(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        admin = User(
            id=str(uuid.uuid4()), email="sweep-admin@sessionfs.dev", tier="admin",
            email_verified=True, created_at=datetime.now(timezone.utc),
        )
        db_session.add(admin)
        raw = generate_api_key()
        db_session.add(ApiKey(
            id=str(uuid.uuid4()), user_id=admin.id, key_hash=hash_api_key(raw), name="k",
            created_at=datetime.now(timezone.utc),
        ))
        now = datetime.now(timezone.utc)
        old = _delivery_row("o" * 64, "completed", claimed_ago=timedelta(days=40))
        old.payload = "{}"
        db_session.add(old)
        db_session.add(_delivery_row("t" * 64, "completed", claimed_ago=timedelta(days=500)))
        db_session.add(_delivery_row("n" * 64, "completed", claimed_ago=timedelta(days=2)))
        for aid, status, expires in (
            ("gca_live", "pending", now + timedelta(minutes=5)),
            ("gca_used", "consumed", now + timedelta(minutes=5)),
            ("gca_dead", "pending", now - timedelta(minutes=5)),
        ):
            db_session.add(GitHubClaimAttempt(
                id=aid, state_hash=aid, user_id=admin.id, nonce_hash="n",
                pkce_verifier_hash="p", flow="authorize", status=status,
                created_at=now, expires_at=expires,
            ))
        await db_session.commit()
        resp = await client.post(
            "/api/v1/admin/github-webhook-deliveries/purge",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert (data["deliveries_purged"], data["claim_attempts_purged"]) == (1, 2)
        assert data["payloads_purged"] == 1
        left = {r.fingerprint for r in await _all(db_session, GitHubWebhookDelivery)}
        assert left == {"o" * 64, "n" * 64}  # 40-day tombstone kept, 500-day gone
        assert data["deliveries_retried"] == 0
        assert {r.id for r in await _all(db_session, GitHubClaimAttempt)} == {"gca_live"}
        audit = [
            a for a in await _all(db_session, AdminAction)
            if a.action == "purge_github_webhook_deliveries"
        ]
        assert len(audit) == 1
        # R4 finding 6: admin_actions.target_type is VARCHAR(20) on PostgreSQL.
        assert len(audit[0].target_type) <= 20
        assert len(audit[0].action) <= 50

    @pytest.mark.asyncio
    async def test_requires_admin(self, client: AsyncClient, auth_headers):
        resp = await client.post(
            "/api/v1/admin/github-webhook-deliveries/purge", headers=auth_headers
        )
        assert resp.status_code in (401, 403)


PR_PAYLOAD = {
    "pull_request": {"head": {"ref": "feat"}, "number": 7},
    "repository": {"full_name": "acme/repo", "clone_url": "https://github.com/acme/repo.git"},
    "installation": {"id": 4242},
}
MARKER = "<!-- sessionfs-pr:4242:acme/repo:7 -->"


def _add_pr_session(db, user_id: str, title: str = "t") -> SessionRow:
    row = SessionRow(
        id=f"ses_{uuid.uuid4().hex[:16]}", user_id=user_id, title=title,
        source_tool="claude-code", blob_key="k", etag="e",
        git_remote_normalized=webhooks.normalize_git_remote(
            PR_PAYLOAD["repository"]["clone_url"]  # type: ignore[index]
        ),
        git_branch="feat", message_count=3,
    )
    db.add(row)
    return row


@pytest.fixture
async def pr_session(db_session: AsyncSession, test_user):
    _add_pr_session(db_session, test_user.id)
    # The publish paths revalidate the installation before any GitHub
    # write (tk_b6c5e184ba8d4aa1), so these tests need a live opted-in
    # installation row for the payload's installation id.
    db_session.add(GitHubInstallation(
        id=4242, user_id=test_user.id, account_login="acme",
        account_type="Organization", auto_comment=True,
    ))
    await db_session.commit()


class FakeComments:
    """GitHub's issue-comment API for one PR."""

    def __init__(self, monkeypatch) -> None:
        self.comments: dict[int, str] = {}
        self.creates = 0
        self.fail_next_create_after_writing = False
        self.create_delay = 0.0
        self.next_id = 9000

        async def fake_token(installation_id):
            return "tok"

        async def fake_post(token, repo, pr_number, body, existing_comment_id=None):
            if existing_comment_id:
                self.comments[existing_comment_id] = body
                return existing_comment_id
            self.creates += 1
            await asyncio.sleep(self.create_delay)
            self.next_id += 1
            self.comments[self.next_id] = body
            if self.fail_next_create_after_writing:
                # GitHub created it, but the response never arrived.
                self.fail_next_create_after_writing = False
                raise RuntimeError("connection reset")
            return self.next_id

        async def fake_find(token, repo, pr_number, marker):
            for cid, body in self.comments.items():
                if marker in body:
                    return cid
            return None

        monkeypatch.setattr(webhooks, "get_installation_token", fake_token)
        monkeypatch.setattr(webhooks, "post_or_update_comment", fake_post)
        monkeypatch.setattr(webhooks, "find_marked_comment", fake_find)


class TestPrComments:
    @pytest.mark.asyncio
    async def test_comment_carries_hidden_marker(
        self, client: AsyncClient, db_session: AsyncSession, pr_session, monkeypatch
    ):
        gh = FakeComments(monkeypatch)
        assert await webhooks._handle_pr_event(PR_PAYLOAD) is True
        (body,) = gh.comments.values()
        assert body.rstrip().endswith(MARKER)

    @pytest.mark.asyncio
    async def test_lost_create_response_is_adopted_not_duplicated(
        self, client: AsyncClient, db_session: AsyncSession, pr_session, monkeypatch
    ):
        """R4 finding 5: GitHub created the comment but the response was
        lost. The next event finds it by marker and edits it."""
        gh = FakeComments(monkeypatch)
        gh.fail_next_create_after_writing = True
        assert await webhooks._handle_pr_event(PR_PAYLOAD) is False
        row = (await _all(db_session, PRComment))[0]
        assert row.comment_id == 0 and row.desired_body  # content kept, reclaimable
        assert await webhooks._handle_pr_event(PR_PAYLOAD) is True
        assert gh.creates == 1 and len(gh.comments) == 1
        row = (await _all(db_session, PRComment))[0]
        assert row.comment_id == next(iter(gh.comments))

    @pytest.mark.asyncio
    async def test_stale_placeholder_reclaimed_and_adopts_existing(
        self, client: AsyncClient, db_session: AsyncSession, pr_session, monkeypatch
    ):
        gh = FakeComments(monkeypatch)
        gh.comments[777] = f"old body\n\n{MARKER}"  # created by a worker that died
        old = datetime.now(timezone.utc) - timedelta(minutes=30)
        db_session.add(PRComment(
            id=str(uuid.uuid4()), installation_id=4242, repo_full_name="acme/repo",
            pr_number=7, comment_id=0, session_ids="[]", created_at=old, updated_at=old,
        ))
        await db_session.commit()
        assert await webhooks._handle_pr_event(PR_PAYLOAD) is True
        assert gh.creates == 0 and list(gh.comments) == [777]
        assert "old body" not in gh.comments[777]
        assert (await _all(db_session, PRComment))[0].comment_id == 777

    @pytest.mark.asyncio
    async def test_update_arriving_during_creation_is_published(
        self, tmp_path, test_user, monkeypatch
    ):
        """R4 finding 5: an event that lands while the comment is still being
        created is not skipped; its content ends up in the comment.

        Separate connections (file-backed SQLite) model separate workers; the
        shared in-memory test engine runs every session on one connection.
        """
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        import sessionfs.server.db.engine as engine_mod
        from sessionfs.server.db.models import Base

        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'race.db'}", connect_args={"timeout": 30}
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(engine_mod, "_session_factory", factory)
        async with factory() as db:
            db.add(User(
                id=test_user.id, email="race@example.com", tier="pro", email_verified=True,
                created_at=datetime.now(timezone.utc),
            ))
            _add_pr_session(db, test_user.id, title="first")
            db.add(GitHubInstallation(
                id=4242, user_id=test_user.id, account_login="acme",
                account_type="Organization", auto_comment=True,
            ))
            await db.commit()

        gh = FakeComments(monkeypatch)
        gh.create_delay = 0.3
        try:
            first = asyncio.create_task(webhooks._handle_pr_event(PR_PAYLOAD))
            await asyncio.sleep(0.1)  # creation in flight
            async with factory() as db:
                _add_pr_session(db, test_user.id, title="second-session-title")
                await db.commit()
            assert await webhooks._handle_pr_event(PR_PAYLOAD) is True  # mid-creation
            assert await first is True
            assert gh.creates == 1 and len(gh.comments) == 1
            (body,) = gh.comments.values()
            assert "second-session-title" in body
            async with factory() as db:
                row = (await _all(db, PRComment))[0]
            assert row.published_version == row.body_version == 2
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_concurrent_events_create_one_comment(self, tmp_path, test_user, monkeypatch):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        import sessionfs.server.db.engine as engine_mod
        from sessionfs.server.db.models import Base

        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'race.db'}", connect_args={"timeout": 30}
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(engine_mod, "_session_factory", factory)
        async with factory() as db:
            db.add(User(
                id=test_user.id, email="race@example.com", tier="pro", email_verified=True,
                created_at=datetime.now(timezone.utc),
            ))
            _add_pr_session(db, test_user.id)
            db.add(GitHubInstallation(
                id=4242, user_id=test_user.id, account_login="acme",
                account_type="Organization", auto_comment=True,
            ))
            await db.commit()
        gh = FakeComments(monkeypatch)
        gh.create_delay = 0.05
        try:
            results = await asyncio.gather(*(webhooks._handle_pr_event(PR_PAYLOAD) for _ in range(4)))
            assert all(results)
            assert gh.creates == 1 and len(gh.comments) == 1
        finally:
            await engine.dispose()

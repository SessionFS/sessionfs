"""Regression tests for the seventh review round.

1. The replay fingerprint covers the signed body only; dispatch requires
   the event's full payload shape (a repository.deleted body re-sent as an
   installation event deletes nothing).
2. The OAuth callback holds no transaction during GitHub calls, and the
   bind re-checks the user under the lock.
3. Repository verification fetches installation tokens with no
   transaction open and defers project mutations.
4. Comment publishing: one lease holder, bounded calls, release-if-clean,
   dirty marking on failure/lost lease, sweeper republish, no round cap.
5. Delivery fencing uses a monotonic generation; completed rows are never
   reopened.
6. Same-project rename displaces an unverified new-name holder.
7. Tombstone retention is never shorter than payload retention.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
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
from sessionfs.server.routes import settings as settings_routes
from sessionfs.server.services import github_oauth


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _all(db: AsyncSession, model):
    return (
        await db.execute(select(model).execution_options(populate_existing=True))
    ).scalars().all()


def _signed(body: bytes, event_name: str) -> dict[str, str]:
    return {
        "X-GitHub-Event": event_name,
        "Content-Type": "application/json",
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-Hub-Signature-256": "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest(),
    }


@pytest.fixture(autouse=True)
def _signed_mode(monkeypatch):
    monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "whsec")
    monkeypatch.delenv("SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED", raising=False)
    for limiter in (
        settings_routes._claim_start_limiter,
        settings_routes._claim_callback_limiter,
        settings_routes._claim_choose_limiter,
        settings_routes._legacy_claim_limiter,
    ):
        limiter._requests.clear()


@pytest.fixture
def open_txns(db_engine):
    """Counts transactions currently open on the test engine."""
    state = {"open": 0}

    def _begin(conn):
        state["open"] += 1

    def _end(conn):
        state["open"] = max(0, state["open"] - 1)

    sync_engine = db_engine.sync_engine
    event.listen(sync_engine, "begin", _begin)
    event.listen(sync_engine, "commit", _end)
    event.listen(sync_engine, "rollback", _end)
    yield state
    event.remove(sync_engine, "begin", _begin)
    event.remove(sync_engine, "commit", _end)
    event.remove(sync_engine, "rollback", _end)


# --- 1 ---------------------------------------------------------------------


REPO_DELETED = json.dumps({
    "action": "deleted",
    "repository": {"id": 1, "full_name": "victim/repo"},
    "installation": {"id": 950001, "node_id": "x"},
    "sender": {"id": 9},
}).encode()


class TestFingerprintAndShape:
    @pytest.mark.asyncio
    async def test_repository_deleted_body_resent_as_installation_does_nothing(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        db_session.add(GitHubInstallation(id=950001, account_login="victim"))
        await db_session.commit()
        resp = await client.post(
            "/webhooks/github", content=REPO_DELETED, headers=_signed(REPO_DELETED, "installation")
        )
        assert resp.status_code == 200
        assert [r.id for r in await _all(db_session, GitHubInstallation)] == [950001]

    @pytest.mark.asyncio
    async def test_event_header_does_not_change_the_fingerprint(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        first = await client.post(
            "/webhooks/github", content=REPO_DELETED, headers=_signed(REPO_DELETED, "repository")
        )
        assert first.json()["status"] == "ok"
        for name in ("installation", "ping", "x" * 40, "pull_request"):
            resp = await client.post(
                "/webhooks/github", content=REPO_DELETED, headers=_signed(REPO_DELETED, name)
            )
            assert resp.json()["status"] == "duplicate"
        rows = await _all(db_session, GitHubWebhookDelivery)
        assert len(rows) == 1  # no tombstone per invented event name
        assert rows[0].fingerprint == hashlib.sha256(REPO_DELETED).hexdigest()

    def test_installation_shape_rules(self, monkeypatch):
        good = {
            "action": "deleted",
            "installation": {
                "id": 5, "app_id": 10, "target_type": "Organization",
                "account": {"id": 1, "login": "o", "type": "Organization"},
            },
        }
        assert webhooks._valid_installation_event(good)
        assert not webhooks._valid_installation_event({**good, "repository": {"id": 1}})
        no_account = {**good, "installation": {k: v for k, v in good["installation"].items() if k != "account"}}
        assert not webhooks._valid_installation_event(no_account)
        no_app = {**good, "installation": {k: v for k, v in good["installation"].items() if k != "app_id"}}
        assert not webhooks._valid_installation_event(no_app)
        # suspend / unsuspend are handled (marketplace publisher badges).
        assert webhooks._valid_installation_event({**good, "action": "suspend"})
        assert webhooks._valid_installation_event({**good, "action": "unsuspend"})
        assert not webhooks._valid_installation_event({**good, "action": "new_permissions_accepted"})
        monkeypatch.setattr(webhooks, "GITHUB_APP_ID", "999")
        assert not webhooks._valid_installation_event(good)  # another App's event
        monkeypatch.setattr(webhooks, "GITHUB_APP_ID", "10")
        assert webhooks._valid_installation_event(good)

    def test_pr_shape_rules(self):
        good = {
            "action": "opened",
            "pull_request": {"head": {"ref": "b"}, "number": 3},
            "repository": {"full_name": "a/b", "clone_url": "https://github.com/a/b.git"},
            "installation": {"id": 1},
        }
        assert webhooks._valid_pr_event(good)
        assert not webhooks._valid_pr_event({**good, "action": "closed"})
        assert not webhooks._valid_pr_event({**good, "pull_request": {}})
        assert not webhooks._valid_pr_event({**good, "installation": {"id": True}})


# --- 2 ---------------------------------------------------------------------


class TestOAuthCallbackNoOpenTransaction:
    @pytest.mark.asyncio
    async def test_github_calls_run_with_no_transaction_open(
        self, client: AsyncClient, auth_headers, test_user, db_session, open_txns, monkeypatch
    ):
        monkeypatch.setenv("SFS_GITHUB_APP_CLIENT_ID", "Iv1.x")
        monkeypatch.setenv("SFS_GITHUB_APP_CLIENT_SECRET", "s")
        monkeypatch.setenv("SFS_DASHBOARD_URL", "https://app.test")
        monkeypatch.setenv(
            "SFS_GITHUB_OAUTH_CALLBACK_URL", "https://api.test" + github_oauth.PKCE_CALLBACK_PATH
        )
        inst = github_oauth.UserInstallation(
            id=960001, account_id=1, account_login="acme", account_type="Organization"
        )
        seen: list[tuple[str, int]] = []

        async def exchange(config, *, code, code_verifier):
            seen.append(("exchange", open_txns["open"]))
            return "tok"

        async def get_user(token):
            seen.append(("user", open_txns["open"]))
            return github_oauth.GitHubUser(id=7, login="alice")

        async def list_inst(token):
            seen.append(("list", open_txns["open"]))
            return [inst]

        async def is_admin(token, org):
            seen.append(("membership", open_txns["open"]))
            return True

        monkeypatch.setattr(github_oauth, "exchange_code", exchange)
        monkeypatch.setattr(github_oauth, "get_user", get_user)
        monkeypatch.setattr(github_oauth, "list_user_installations", list_inst)
        monkeypatch.setattr(github_oauth, "is_active_org_admin", is_admin)

        resp = await client.post(
            "/api/v1/settings/github/oauth/start", headers=auth_headers,
            json={"flow": "authorize", "installation_id": 960001},
        )
        url = resp.json()["redirect_url"]
        state = parse_qs(urlparse(url).query)["state"][0]
        from http.cookies import SimpleCookie

        jar = SimpleCookie()
        jar.load(resp.headers["set-cookie"])
        cookie = jar[settings_routes.GITHUB_CLAIM_COOKIE].value
        client.cookies.clear()
        await db_session.commit()  # the test's own session holds nothing open
        cb = await client.get(
            "/api/v1/settings/github/oauth/callback",
            params={"code": "c", "state": state},
            cookies={settings_routes.GITHUB_CLAIM_COOKIE: cookie},
            follow_redirects=False,
        )
        assert cb.headers["location"].endswith("github=connected")
        assert [name for name, _ in seen] == ["exchange", "user", "list", "membership"]
        assert all(count == 0 for _, count in seen), seen

    @pytest.mark.asyncio
    async def test_bind_rechecks_user_is_active_under_lock(self, db_session, test_user):
        test_user.is_active = False
        await db_session.commit()
        info = github_oauth.UserInstallation(
            id=960002, account_id=1, account_login="acme", account_type="Organization"
        )
        ok = await settings_routes._bind_installation(
            db_session, user_id=test_user.id,
            gh_user=github_oauth.GitHubUser(id=7, login="alice"), info=info, role="org_admin",
        )
        assert ok is False
        row = await db_session.get(GitHubInstallation, 960002)
        assert row is None or row.user_id is None


# --- 3 ---------------------------------------------------------------------


class TestRepoVerificationNoOpenTransaction:
    @pytest.mark.asyncio
    async def test_installation_token_fetched_with_no_transaction_open(
        self, client: AsyncClient, auth_headers, test_user, db_session, open_txns, monkeypatch
    ):
        from sessionfs.server import github_app

        monkeypatch.setattr(github_app, "GITHUB_APP_ID", "123")
        monkeypatch.setattr(github_app, "GITHUB_APP_PRIVATE_KEY", "pem")
        project = Project(
            id=f"proj_{uuid.uuid4().hex[:16]}", name="p",
            git_remote_normalized="github.com/acme/reclaimed", context_document="",
            owner_id=test_user.id, repo_reclaimed_at=_now(),
        )
        db_session.add(project)
        db_session.add(GitHubInstallation(
            id=961001, user_id=test_user.id, account_login="acme",
            account_type="Organization", claim_verified_at=_now(),
            claim_verified_github_user_id=7, claim_role="org_admin",
        ))
        await db_session.commit()
        project_id = project.id
        observed: dict = {}

        async def fake_token(installation_id):
            observed["open"] = open_txns["open"]
            # Project mutations are deferred until after verification.
            engine = db_session.bind
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as other:
                p = await other.get(Project, project_id)
                observed["reclaimed_still_set"] = p.repo_reclaimed_at is not None
                await other.rollback()
            raise RuntimeError("no network in tests")

        monkeypatch.setattr(github_app, "get_installation_token", fake_token)
        resp = await client.post(
            f"/api/v1/projects/{project_id}/repos",
            json={"git_remote": "github.com/acme/new"},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        assert observed["open"] == 0
        assert observed["reclaimed_still_set"] is True
        assert resp.json()["verification_method"] == "owner_attested"

    @pytest.mark.asyncio
    async def test_authorization_rechecked_after_verification(
        self, client: AsyncClient, auth_headers, test_user, db_session
    ):
        other = User(id=str(uuid.uuid4()), email="own@example.com", tier="pro",
                     email_verified=True, created_at=_now())
        db_session.add(other)
        project = Project(
            id=f"proj_{uuid.uuid4().hex[:16]}", name="p",
            git_remote_normalized="github.com/acme/x", context_document="", owner_id=test_user.id,
        )
        db_session.add(project)
        await db_session.commit()
        project_id = project.id

        async def lose_ownership(db, user_id, owner, repo):
            # Ownership moves away while GitHub is being called.
            factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
            async with factory() as s2:
                p = await s2.get(Project, project_id)
                p.owner_id = other.id
                await s2.commit()
            return (False, "owner_attested", None, None)

        with patch("sessionfs.server.github_app.verify_repo_ownership", new=lose_ownership):
            resp = await client.post(
                f"/api/v1/projects/{project_id}/repos",
                json={"git_remote": "github.com/acme/y"},
                headers=auth_headers,
            )
        assert resp.status_code == 403


# --- 4 ---------------------------------------------------------------------


@pytest.fixture
async def file_db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'r7.db'}", connect_args={"timeout": 30}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(engine_mod, "_session_factory", factory)
    yield factory
    await engine.dispose()


async def _comment(
    factory, row_id="pc", *, body_version=2, published=1, body="v2", pr_number=1, **extra
):
    async with factory() as db:
        # The publish paths revalidate the installation before any GitHub
        # write (tk_b6c5e184ba8d4aa1), so these tests need a live opted-in
        # installation row.
        if await db.get(GitHubInstallation, 1) is None:
            db.add(GitHubInstallation(
                id=1, user_id="u-owner", account_login="acme",
                account_type="Organization", auto_comment=True,
            ))
        extra.setdefault("owner_user_id", "u-owner")
        db.add(PRComment(
            id=row_id, installation_id=1, repo_full_name="acme/r", pr_number=pr_number,
            comment_id=4242, session_ids="[]", desired_body=body, body_version=body_version,
            published_version=published, created_at=_now(), updated_at=_now(), **extra,
        ))
        await db.commit()


async def _get(factory, row_id="pc") -> PRComment:
    async with factory() as db:
        row = await db.get(PRComment, row_id)
        db.expunge(row)
        return row


class TestPublishing:
    @pytest.mark.asyncio
    async def test_slow_call_is_bounded_and_row_left_dirty(self, file_db, monkeypatch):
        await _comment(file_db)
        monkeypatch.setattr(webhooks, "PUBLISH_CALL_TIMEOUT", 0.05)

        async def slow_post(*a, **k):
            await asyncio.sleep(1)
            return 4242

        monkeypatch.setattr(webhooks, "post_or_update_comment", slow_post)
        async with file_db() as db:
            with pytest.raises(asyncio.TimeoutError):
                await webhooks._publish_newest(db, "t", "pc", "acme/r", 1, 4242)
        row = await _get(file_db)
        assert row.publish_lease is None
        assert row.published_version < row.body_version  # dirty for the sweeper

    def test_call_timeout_well_under_lease(self):
        assert webhooks.PUBLISH_CALL_TIMEOUT * 2 <= webhooks.PUBLISH_LEASE_TTL.total_seconds()

    @pytest.mark.asyncio
    async def test_lost_lease_during_call_marks_row_dirty(self, file_db, monkeypatch):
        await _comment(file_db)

        async def stolen(token, repo, pr, body, existing_comment_id=None):
            async with file_db() as db:
                row = await db.get(PRComment, "pc")
                row.publish_lease = "someone-else"
                row.published_version = 2  # they published the newest
                await db.commit()
            return 4242

        monkeypatch.setattr(webhooks, "post_or_update_comment", stolen)
        async with file_db() as db:
            assert await webhooks._publish_newest(db, "t", "pc", "acme/r", 1, 4242) is False
        row = await _get(file_db)
        # Our call may have landed after theirs: force a republish of the newest.
        assert row.published_version == 0 and row.body_version == 2

    @pytest.mark.asyncio
    async def test_sweeper_republishes_dirty_rows_without_live_lease(self, file_db, monkeypatch):
        await _comment(file_db, "dirty-free", pr_number=1)
        await _comment(file_db, "dirty-expired", pr_number=2, publish_lease="dead",
                       publish_lease_until=_now() - timedelta(seconds=1))
        await _comment(file_db, "dirty-live", pr_number=3, publish_lease="alive",
                       publish_lease_until=_now() + timedelta(seconds=60))
        await _comment(file_db, "clean", pr_number=4, body_version=3, published=3, body="v3")
        published: list[str] = []

        async def fake_post(token, repo, pr, body, existing_comment_id=None):
            published.append(body)
            return existing_comment_id

        async def fake_token(installation_id):
            return "tok"

        monkeypatch.setattr(webhooks, "post_or_update_comment", fake_post)
        monkeypatch.setattr(webhooks, "get_installation_token", fake_token)
        stats = await webhooks.republish_dirty_comments(10)
        assert stats["comments_republished"] == 2
        assert (await _get(file_db, "dirty-free")).published_version == 2
        assert (await _get(file_db, "dirty-expired")).published_version == 2
        assert (await _get(file_db, "dirty-live")).published_version == 1
        assert (await _get(file_db, "dirty-free")).publish_lease is None

    @pytest.mark.asyncio
    async def test_no_round_cap_many_versions_during_publishing(self, file_db, monkeypatch):
        """Each publish is followed by a new event while the lease is held:
        the holder keeps going until clean, however many rounds that takes."""
        await _comment(file_db)
        bodies: list[str] = []
        state = {"next": 3}

        async def fake_post(token, repo, pr, body, existing_comment_id=None):
            bodies.append(body)
            if state["next"] <= 12:
                async with file_db() as db:
                    await webhooks._record_desired_comment(
                        db, 1, "acme/r", 1, f"v{state['next']}", "[]",
                        owner_user_id="u-owner",
                    )
                state["next"] += 1
            return existing_comment_id

        monkeypatch.setattr(webhooks, "post_or_update_comment", fake_post)
        async with file_db() as db:
            assert await webhooks._publish_newest(db, "t", "pc", "acme/r", 1, 4242) is True
        row = await _get(file_db)
        assert bodies[-1] == "v12"
        assert row.published_version == row.body_version == 12
        assert row.publish_lease is None

    @pytest.mark.asyncio
    async def test_concurrent_publisher_defers_to_holder(self, file_db, monkeypatch):
        await _comment(file_db)
        bodies: list[str] = []

        async def fake_post(token, repo, pr, body, existing_comment_id=None):
            if body == "v2":
                async with file_db() as db:
                    await webhooks._record_desired_comment(
                        db, 1, "acme/r", 1, "v3", "[]", owner_user_id="u-owner"
                    )
                    # A second publisher finds the lease taken and leaves.
                    assert await webhooks._publish_newest(db, "t", "pc", "acme/r", 1, 4242) is True
            bodies.append(body)
            return existing_comment_id

        monkeypatch.setattr(webhooks, "post_or_update_comment", fake_post)
        async with file_db() as db:
            await webhooks._publish_newest(db, "t", "pc", "acme/r", 1, 4242)
        assert bodies == ["v2", "v3"]


# --- 5 ---------------------------------------------------------------------


class TestGenerationFencing:
    @pytest.mark.asyncio
    async def test_force_retry_advances_generation_so_stale_worker_is_fenced(
        self, file_db, monkeypatch
    ):
        fp = "g" * 64
        outcome, gen1 = await webhooks._claim_delivery(fp, None, "ping", '{"zen": 1}')
        assert (outcome, gen1) == ("claimed", 1)
        async with file_db() as db:
            row = await db.get(GitHubWebhookDelivery, fp)
            row.claimed_at = _now() - timedelta(hours=1)  # worker 1 went stale
            row.attempts = webhooks.WEBHOOK_MAX_ATTEMPTS
            await db.commit()
        assert await webhooks.force_retry_delivery(fp) == "succeeded"
        async with file_db() as db:
            row = await db.get(GitHubWebhookDelivery, fp)
            assert row.status == "completed"
            assert row.attempts == 1 and row.lease_generation == 2
        # The stale worker reports late: ignored.
        assert await webhooks._finish_delivery(fp, False, gen1) is False

    @pytest.mark.asyncio
    async def test_completed_row_is_never_reopened(self, file_db):
        fp = "h" * 64
        _, gen = await webhooks._claim_delivery(fp, None, "ping", "{}")
        assert await webhooks._finish_delivery(fp, True, gen) is True
        async with file_db() as db:
            # A takeover computed from a stale read must not reopen it.
            assert await webhooks._take_over(db, fp, gen, "processing", _now()) is None
            assert await webhooks._take_over(db, fp, gen, "completed", _now()) is None
        assert await webhooks.force_retry_delivery(fp) == "completed"
        async with file_db() as db:
            assert (await db.get(GitHubWebhookDelivery, fp)).status == "completed"

    @pytest.mark.asyncio
    async def test_generation_never_decreases(self, file_db):
        fp = "i" * 64
        _, gen = await webhooks._claim_delivery(fp, None, "ping", "{}")
        generations = [gen]
        for _ in range(3):
            await webhooks._finish_delivery(fp, False, generations[-1])
            async with file_db() as db:
                row = await db.get(GitHubWebhookDelivery, fp)
                row.next_attempt_at = _now() - timedelta(seconds=1)
                await db.commit()
            outcome, gen = await webhooks._claim_delivery(fp, None, "ping", "{}")
            assert outcome == "claimed"
            generations.append(gen)
        assert generations == sorted(generations) and len(set(generations)) == 4


# --- 6 ---------------------------------------------------------------------


class TestSameProjectRenameWithUnverifiedNewNameHolder:
    @pytest.mark.asyncio
    async def test_displaces_unverified_holder_and_renames_in_place(
        self, client: AsyncClient, auth_headers, test_user, db_session
    ):
        squatter = User(id=str(uuid.uuid4()), email="sq@example.com", tier="pro",
                        email_verified=True, created_at=_now())
        db_session.add(squatter)
        mine = Project(
            id=f"proj_{uuid.uuid4().hex[:16]}", name="mine",
            git_remote_normalized="github.com/acme/old", context_document="", owner_id=test_user.id,
        )
        theirs = Project(
            id=f"proj_{uuid.uuid4().hex[:16]}", name="theirs",
            git_remote_normalized="github.com/acme/new", context_document="", owner_id=squatter.id,
        )
        db_session.add_all([mine, theirs])
        await db_session.commit()
        my_row = ProjectRepo(
            id=f"repo_{uuid.uuid4().hex[:16]}", project_id=mine.id,
            git_remote_normalized="github.com/acme/old", provider="github",
            provider_repo_id="888", is_primary=True, verified=True, verification_method="github_app",
        )
        db_session.add_all([
            my_row,
            ProjectRepo(
                id=f"repo_{uuid.uuid4().hex[:16]}", project_id=theirs.id,
                git_remote_normalized="github.com/acme/new", is_primary=True,
                verified=False, verification_method="owner_attested",
            ),
        ])
        await db_session.commit()
        my_row_id, mine_id = my_row.id, mine.id

        with patch(
            "sessionfs.server.github_app.verify_repo_ownership",
            new=AsyncMock(return_value=(True, "github_app", "github", "888")),
        ):
            resp = await client.post(
                f"/api/v1/projects/{mine_id}/repos",
                json={"git_remote": "github.com/acme/new"},
                headers=auth_headers,
            )
        assert resp.status_code in (200, 201), resp.text
        assert resp.json()["id"] == my_row_id
        rows = await _all(db_session, ProjectRepo)
        assert [(r.id, r.project_id, r.git_remote_normalized) for r in rows] == [
            (my_row_id, mine_id, "github.com/acme/new")
        ]


# --- 7 ---------------------------------------------------------------------


class TestRetentionFloor:
    @pytest.mark.asyncio
    async def test_custom_payload_window_extends_tombstones(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        db_session.add(GitHubWebhookDelivery(
            fingerprint="r" * 64, event="ping", status="completed", attempts=1,
            received_at=_now() - timedelta(days=450), claimed_at=_now() - timedelta(days=450),
            payload="{}",
        ))
        admin = User(id=str(uuid.uuid4()), email="adm7@sessionfs.dev", tier="admin",
                     email_verified=True, created_at=_now())
        db_session.add(admin)
        raw = generate_api_key()
        db_session.add(ApiKey(id=str(uuid.uuid4()), user_id=admin.id, key_hash=hash_api_key(raw),
                              name="k", created_at=_now()))
        await db_session.commit()
        resp = await client.post(
            "/api/v1/admin/github-webhook-deliveries/sweep",
            headers={"Authorization": f"Bearer {raw}"},
            json={"retention_days": 500},
        )
        assert resp.status_code == 200
        assert resp.json()["deliveries_purged"] == 0
        (row,) = await _all(db_session, GitHubWebhookDelivery)
        assert row.payload == "{}"  # still inside the 500-day payload window

    @pytest.mark.asyncio
    async def test_prune_helper_applies_floor(self, file_db):
        async with file_db() as db:
            db.add(GitHubWebhookDelivery(
                fingerprint="s" * 64, event="ping", status="completed", attempts=1,
                received_at=_now() - timedelta(days=50), claimed_at=_now() - timedelta(days=50),
            ))
            await db.commit()
            payloads, tombstones = await webhooks.prune_deliveries(
                db, payload_retention=timedelta(days=100), tombstone_retention=timedelta(days=10)
            )
        assert tombstones == 0

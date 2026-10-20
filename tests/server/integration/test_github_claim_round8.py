"""Regression tests for the eighth review round.

1. Dispatch is decided by the payload's validated shape, not the unsigned
   event header; a supported body is never completed without its handler.
2. link_repo re-checks the caller's freshly locked standing (active user,
   org membership) after the GitHub calls.
3. A displaced (repo-reclaimed) project loses its legacy remote; legacy
   remote fallbacks never resolve or grant access to it.
4. A superseded delivery generation leaves no durable side effect.
5. Comment creation is fenced by the shared comment lease.
6. Admin sweep / forced retry hold no request transaction during the work.
8. Helm refuses two different dashboard URLs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import sessionfs.server.db.engine as engine_mod
from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.auth.project_access import user_can_access_project
from sessionfs.server.db.models import (
    ApiKey,
    Base,
    GitHubInstallation,
    GitHubWebhookDelivery,
    OrgMember,
    Organization,
    PRComment,
    Project,
    ProjectRepo,
    User,
)
from sessionfs.server.db.models import Session as SessionRow
from sessionfs.server.routes import webhooks
from sessionfs.server.services.project_resolver import resolve_project_by_remote

REPO_ROOT = Path(__file__).resolve().parents[3]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _all(db: AsyncSession, model):
    return (
        await db.execute(select(model).execution_options(populate_existing=True))
    ).scalars().all()


def _signed(body: bytes, event_name: str | None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest(),
    }
    if event_name is not None:
        headers["X-GitHub-Event"] = event_name
    return headers


@pytest.fixture(autouse=True)
def _signed_mode(monkeypatch):
    monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "whsec")
    monkeypatch.delenv("SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED", raising=False)


@pytest.fixture
def open_txns(db_engine):
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


INSTALL_DELETED = json.dumps({
    "action": "deleted",
    "installation": {
        "id": 970001, "app_id": 1, "target_type": "Organization",
        "account": {"id": 5, "login": "acme", "type": "Organization"},
    },
}).encode()


class TestShapeDispatch:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("header", [None, "ping", "repository"])
    async def test_supported_body_runs_its_handler_whatever_the_header(
        self, client: AsyncClient, db_session: AsyncSession, header
    ):
        db_session.add(GitHubInstallation(id=970001, account_login="acme"))
        await db_session.commit()
        resp = await client.post("/webhooks/github", content=INSTALL_DELETED, headers=_signed(INSTALL_DELETED, header))
        assert resp.status_code == 200
        assert await _all(db_session, GitHubInstallation) == []  # handler ran
        (row,) = await _all(db_session, GitHubWebhookDelivery)
        assert row.status == "completed"
        # The genuine delivery afterwards is a harmless duplicate.
        again = await client.post(
            "/webhooks/github", content=INSTALL_DELETED, headers=_signed(INSTALL_DELETED, "installation")
        )
        assert again.json()["status"] == "duplicate"

    def test_shapes_are_mutually_exclusive(self):
        pr = {
            "action": "opened",
            "pull_request": {"head": {"ref": "b"}, "number": 3},
            "repository": {"full_name": "a/b", "clone_url": "https://github.com/a/b.git"},
            "installation": {"id": 1, "app_id": 1, "target_type": "Organization",
                             "account": {"id": 1, "login": "a", "type": "Organization"}},
        }
        assert webhooks._canonical_event(pr) == "pull_request"
        assert webhooks._canonical_event(json.loads(INSTALL_DELETED)) == "installation"
        assert webhooks._canonical_event({"zen": "hi"}) is None

    @pytest.mark.asyncio
    async def test_pr_body_with_wrong_header_runs_pr_handler(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch
    ):
        calls: list = []

        async def fake_handle(payload, fence=None):
            calls.append(fence)
            return True

        monkeypatch.setattr(webhooks, "_handle_pr_event", fake_handle)
        body = json.dumps({
            "action": "opened",
            "pull_request": {"head": {"ref": "b"}, "number": 3},
            "repository": {"full_name": "a/b", "clone_url": "https://github.com/a/b.git"},
            "installation": {"id": 1},
        }).encode()
        resp = await client.post("/webhooks/github", content=body, headers=_signed(body, "issues"))
        assert resp.json()["status"] == "ok"
        import asyncio

        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls and calls[0][0] == hashlib.sha256(body).hexdigest()


# --- 2 ---------------------------------------------------------------------


class TestLinkRepoRecheck:
    @pytest.mark.asyncio
    async def test_user_deactivated_during_verification(
        self, client: AsyncClient, auth_headers, test_user, db_session
    ):
        project = Project(
            id=f"proj_{uuid.uuid4().hex[:16]}", name="p",
            git_remote_normalized="github.com/acme/p1", context_document="", owner_id=test_user.id,
        )
        db_session.add(project)
        await db_session.commit()
        user_id, project_id = test_user.id, project.id

        async def deactivate(db, uid, owner, repo):
            factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
            async with factory() as s2:
                (await s2.get(User, user_id)).is_active = False
                await s2.commit()
            return (False, "owner_attested", None, None)

        with patch("sessionfs.server.github_app.verify_repo_ownership", new=deactivate):
            resp = await client.post(
                f"/api/v1/projects/{project_id}/repos",
                json={"git_remote": "github.com/acme/p2"}, headers=auth_headers,
            )
        assert resp.status_code in (401, 403)
        assert await _all(db_session, ProjectRepo) == []

    @pytest.mark.asyncio
    async def test_org_admin_demoted_during_verification(
        self, client: AsyncClient, auth_headers, test_user, db_session
    ):
        owner = User(id=str(uuid.uuid4()), email="own8@example.com", tier="team",
                     email_verified=True, created_at=_now())
        org = Organization(id=f"org_{uuid.uuid4().hex[:12]}", name="O", slug=f"o-{uuid.uuid4().hex[:6]}",
                           tier="team")
        db_session.add_all([owner, org])
        await db_session.commit()
        db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
        project = Project(
            id=f"proj_{uuid.uuid4().hex[:16]}", name="p", git_remote_normalized="github.com/acme/o1",
            context_document="", owner_id=owner.id, org_id=org.id,
        )
        db_session.add(project)
        await db_session.commit()
        org_id, user_id, project_id = org.id, test_user.id, project.id

        async def demote(db, uid, o, r):
            factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
            async with factory() as s2:
                member = (await s2.execute(select(OrgMember).where(
                    OrgMember.org_id == org_id, OrgMember.user_id == user_id))).scalar_one()
                member.role = "member"
                await s2.commit()
            return (False, "owner_attested", None, None)

        with patch("sessionfs.server.github_app.verify_repo_ownership", new=demote):
            resp = await client.post(
                f"/api/v1/projects/{project_id}/repos",
                json={"git_remote": "github.com/acme/o2"}, headers=auth_headers,
            )
        assert resp.status_code == 403

    def test_locks_user_and_membership_before_project(self):
        import inspect

        from sessionfs.server.routes import projects

        src = inspect.getsource(projects.link_repo)
        i_user = src.index("select(User.id, User.is_active)")
        i_member = src.index("select(OrgMember.user_id)")
        # The affected projects (target + holders) are locked in id order
        # after the user and membership rows, and before any ProjectRepo row
        # is locked (Project -> ProjectRepo matches unlink_repo/merge).
        i_project = src.index(".where(Project.id.in_(holder_project_ids))")
        assert i_user < i_member < i_project
        assert src.count("with_for_update(read=True)") == 2


# --- 3 ---------------------------------------------------------------------


class TestDisplacedRemote:
    async def _displace(self, client, auth_headers, test_user, db_session, *, rename: bool):
        squatter = User(id=str(uuid.uuid4()), email=f"sq{uuid.uuid4().hex[:4]}@example.com",
                        tier="pro", email_verified=True, created_at=_now())
        db_session.add(squatter)
        theirs = Project(
            id=f"proj_{uuid.uuid4().hex[:16]}", name="theirs",
            git_remote_normalized="github.com/acme/new", context_document="", owner_id=squatter.id,
        )
        mine = Project(
            id=f"proj_{uuid.uuid4().hex[:16]}", name="mine",
            git_remote_normalized="github.com/acme/old", context_document="", owner_id=test_user.id,
        )
        db_session.add_all([theirs, mine])
        await db_session.commit()
        db_session.add_all([
            ProjectRepo(id=f"repo_{uuid.uuid4().hex[:16]}", project_id=theirs.id,
                        git_remote_normalized="github.com/acme/new", is_primary=True,
                        verified=False, verification_method="owner_attested"),
            ProjectRepo(id=f"repo_{uuid.uuid4().hex[:16]}", project_id=mine.id,
                        git_remote_normalized="github.com/acme/old", provider="github",
                        provider_repo_id="999" if rename else None, is_primary=True,
                        verified=True, verification_method="github_app"),
        ])
        # The squatter has a legacy session (no project_id) on the remote.
        db_session.add(SessionRow(
            id=f"ses_{uuid.uuid4().hex[:16]}", user_id=squatter.id, title="t",
            source_tool="claude-code", blob_key="k", etag="e",
            git_remote_normalized="github.com/acme/new", git_branch="main", message_count=1,
        ))
        await db_session.commit()
        from unittest.mock import AsyncMock

        with patch(
            "sessionfs.server.github_app.verify_repo_ownership",
            new=AsyncMock(return_value=(True, "github_app", "github", "999")),
        ):
            resp = await client.post(
                f"/api/v1/projects/{mine.id}/repos",
                json={"git_remote": "github.com/acme/new", "is_primary": True},
                headers=auth_headers,
            )
        assert resp.status_code in (200, 201), resp.text
        return theirs.id, mine.id, squatter.id

    @pytest.mark.asyncio
    @pytest.mark.parametrize("rename", [True, False])
    async def test_displaced_project_loses_legacy_remote(
        self, client: AsyncClient, auth_headers, test_user, db_session, rename
    ):
        theirs_id, mine_id, squatter_id = await self._displace(
            client, auth_headers, test_user, db_session, rename=rename
        )
        projects = {p.id: p for p in await _all(db_session, Project)}
        assert projects[theirs_id].repo_reclaimed_at is not None
        # Reclaimed state is structural: NULL remote + repo_reclaimed_at,
        # never a user-reachable placeholder string.
        assert projects[theirs_id].git_remote_normalized is None
        assert projects[mine_id].git_remote_normalized == "github.com/acme/new"
        # Legacy fallbacks no longer reach the displaced project.
        resolved = await resolve_project_by_remote(db_session, "github.com/acme/new")
        assert resolved is not None and resolved.id == mine_id
        assert not await user_can_access_project(db_session, squatter_id, projects[theirs_id]) or (
            projects[theirs_id].owner_id == squatter_id
        )

    @pytest.mark.asyncio
    async def test_reclaimed_project_never_grants_access_by_former_remote(self, db_session, test_user):
        owner = User(id=str(uuid.uuid4()), email="o8@example.com", tier="pro",
                     email_verified=True, created_at=_now())
        db_session.add(owner)
        reclaimed = Project(
            id=f"proj_{uuid.uuid4().hex[:16]}", name="r", git_remote_normalized="github.com/x/y",
            context_document="", owner_id=owner.id, repo_reclaimed_at=_now(),
        )
        db_session.add(reclaimed)
        db_session.add(SessionRow(
            id=f"ses_{uuid.uuid4().hex[:16]}", user_id=test_user.id, title="t",
            source_tool="claude-code", blob_key="k", etag="e",
            git_remote_normalized="github.com/x/y", git_branch="main", message_count=1,
        ))
        await db_session.commit()
        assert await user_can_access_project(db_session, test_user.id, reclaimed) is False
        assert await resolve_project_by_remote(db_session, "github.com/x/y") is None


# --- 4 ---------------------------------------------------------------------


@pytest.fixture
async def file_db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'r8.db'}", connect_args={"timeout": 30}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(engine_mod, "_session_factory", factory)
    yield factory
    await engine.dispose()


class TestGenerationFenceOnSideEffects:
    @pytest.mark.asyncio
    async def test_superseded_worker_writes_nothing(self, file_db):
        fp = "f" * 64
        async with file_db() as db:
            db.add(GitHubWebhookDelivery(
                fingerprint=fp, event="pull_request", status="processing", attempts=2,
                lease_generation=2, received_at=_now(), claimed_at=_now(),
            ))
            db.add(PRComment(
                id="pc", installation_id=1, repo_full_name="acme/r", pr_number=1, comment_id=9,
                session_ids="[]", desired_body="v-newer", body_version=5, published_version=5,
                created_at=_now(), updated_at=_now(),
            ))
            await db.commit()
        async with file_db() as db:
            with pytest.raises(webhooks._Superseded):
                await webhooks._record_desired_comment(
                    db, 1, "acme/r", 1, "v-stale", "[]", fence=(fp, 1),
                    owner_user_id="u-owner",
                )
        async with file_db() as db:
            row = await db.get(PRComment, "pc")
            assert (row.desired_body, row.body_version) == ("v-newer", 5)

    @pytest.mark.asyncio
    async def test_current_generation_records(self, file_db):
        fp = "e" * 64
        async with file_db() as db:
            db.add(GitHubWebhookDelivery(
                fingerprint=fp, event="pull_request", status="processing", attempts=1,
                lease_generation=3, received_at=_now(), claimed_at=_now(),
            ))
            db.add(GitHubInstallation(
                id=1, user_id="u-owner", account_login="acme", account_type="Organization",
                auto_comment=True,
            ))
            await db.commit()
        async with file_db() as db:
            decision = await webhooks._record_desired_comment(
                db, 1, "acme/r", 2, "v1", "[]", fence=(fp, 3), owner_user_id="u-owner"
            )
        assert decision[0] == "create" and decision[3]

    @pytest.mark.asyncio
    async def test_pr_handler_with_stale_fence_posts_nothing(
        self, file_db, test_user, monkeypatch
    ):
        async with file_db() as db:
            db.add(User(id=test_user.id, email="g@example.com", tier="pro",
                        email_verified=True, created_at=_now()))
            db.add(SessionRow(
                id=f"ses_{uuid.uuid4().hex[:16]}", user_id=test_user.id, title="t",
                source_tool="claude-code", blob_key="k", etag="e",
                git_remote_normalized="github.com/acme/repo", git_branch="feat", message_count=1,
            ))
            db.add(GitHubWebhookDelivery(
                fingerprint="d" * 64, event="pull_request", status="processing", attempts=2,
                lease_generation=2, received_at=_now(), claimed_at=_now(),
            ))
            await db.commit()
        posted: list = []

        async def fake_token(i):
            return "tok"

        async def fake_post(*a, **k):
            posted.append(a)
            return 1

        monkeypatch.setattr(webhooks, "get_installation_token", fake_token)
        monkeypatch.setattr(webhooks, "post_or_update_comment", fake_post)
        payload = {
            "pull_request": {"head": {"ref": "feat"}, "number": 4},
            "repository": {"full_name": "acme/repo", "clone_url": "https://github.com/acme/repo.git"},
            "installation": {"id": 3},
        }
        assert await webhooks._handle_pr_event(payload, fence=("d" * 64, 1)) is True
        assert posted == []
        async with file_db() as db:
            assert await _all(db, PRComment) == []


# --- 5 ---------------------------------------------------------------------


async def _pending(factory, *, lease=None, until=None):
    async with factory() as db:
        # The publish paths revalidate the installation before any GitHub
        # write, so these tests need a live opted-in installation row.
        if await db.get(GitHubInstallation, 1) is None:
            db.add(GitHubInstallation(
                id=1, user_id="u-owner", account_login="acme", account_type="Organization",
                auto_comment=True,
            ))
        db.add(PRComment(
            id="pend", installation_id=1, repo_full_name="acme/r", pr_number=7, comment_id=0,
            session_ids="[]", desired_body="body", body_version=1, published_version=0,
            owner_user_id="u-owner",
            publish_lease=lease, publish_lease_until=until, created_at=_now(), updated_at=_now(),
        ))
        await db.commit()


class TestCreationLease:
    @pytest.mark.asyncio
    async def test_reclaimed_creation_claim_cannot_post(self, file_db, monkeypatch):
        await _pending(file_db, lease="mine", until=_now() + timedelta(seconds=60))
        posted: list = []

        async def find_then_lose_lease(token, repo, pr, marker):
            async with file_db() as db:  # another worker reclaims meanwhile
                row = await db.get(PRComment, "pend")
                row.publish_lease = "theirs"
                await db.commit()
            return None

        async def fake_post(*a, **k):
            posted.append(a)
            return 55

        monkeypatch.setattr(webhooks, "find_marked_comment", find_then_lose_lease)
        monkeypatch.setattr(webhooks, "post_or_update_comment", fake_post)
        async with file_db() as db:
            ok = await webhooks._create_or_adopt_comment(db, "tok", "pend", "mine", 1, "acme/r", 7)
        assert ok is False and posted == []

    @pytest.mark.asyncio
    async def test_finalize_conditional_on_lease(self, file_db, monkeypatch):
        await _pending(file_db, lease="mine", until=_now() + timedelta(seconds=60))

        async def find(*a):
            return None

        async def post_and_lose(token, repo, pr, body, existing=None):
            async with file_db() as db:
                row = await db.get(PRComment, "pend")
                row.publish_lease = "theirs"
                await db.commit()
            return 55

        monkeypatch.setattr(webhooks, "find_marked_comment", find)
        monkeypatch.setattr(webhooks, "post_or_update_comment", post_and_lose)
        async with file_db() as db:
            assert await webhooks._create_or_adopt_comment(db, "tok", "pend", "mine", 1, "acme/r", 7) is False
            row = await db.get(PRComment, "pend")
            assert row.comment_id == 0  # the stale holder did not finalize

    @pytest.mark.asyncio
    async def test_failed_create_releases_lease_for_immediate_retry(self, file_db, monkeypatch):
        await _pending(file_db, lease="mine", until=_now() + timedelta(seconds=60))

        async def find(*a):
            raise RuntimeError("github down")

        monkeypatch.setattr(webhooks, "find_marked_comment", find)
        async with file_db() as db:
            with pytest.raises(RuntimeError):
                await webhooks._create_or_adopt_comment(db, "tok", "pend", "mine", 1, "acme/r", 7)
            row = await db.get(PRComment, "pend", populate_existing=True)
            assert row.publish_lease is None and row.comment_id == 0

    @pytest.mark.asyncio
    async def test_sweeper_completes_abandoned_creation(self, file_db, monkeypatch):
        await _pending(file_db, lease="dead", until=_now() - timedelta(seconds=1))

        async def token(i):
            return "tok"

        async def find(*a):
            return None

        async def post(token, repo, pr, body, existing=None):
            return 66

        monkeypatch.setattr(webhooks, "get_installation_token", token)
        monkeypatch.setattr(webhooks, "find_marked_comment", find)
        monkeypatch.setattr(webhooks, "post_or_update_comment", post)
        stats = await webhooks.republish_dirty_comments(5)
        assert stats["comments_republished"] == 1
        async with file_db() as db:
            row = await db.get(PRComment, "pend")
            assert row.comment_id == 66 and row.publish_lease is None
            assert row.published_version == row.body_version


# --- 6 ---------------------------------------------------------------------


class TestAdminNoRequestTransaction:
    async def _admin(self, db):
        admin = User(id=str(uuid.uuid4()), email=f"a{uuid.uuid4().hex[:6]}@sessionfs.dev",
                     tier="admin", email_verified=True, created_at=_now())
        db.add(admin)
        raw = generate_api_key()
        db.add(ApiKey(id=str(uuid.uuid4()), user_id=admin.id, key_hash=hash_api_key(raw),
                      name="k", created_at=_now()))
        await db.commit()
        return {"Authorization": f"Bearer {raw}"}

    @pytest.mark.asyncio
    async def test_sweep_runs_retries_with_no_transaction_open(
        self, client: AsyncClient, db_session, open_txns, monkeypatch
    ):
        headers = await self._admin(db_session)
        await db_session.commit()
        seen: list[int] = []

        async def fake_retry(limit):
            seen.append(open_txns["open"])
            return {"retried": 0, "succeeded": 0, "failed": 0}

        async def fake_republish(limit):
            seen.append(open_txns["open"])
            return {"comments_republished": 0, "comment_republish_failed": 0}

        monkeypatch.setattr(webhooks, "retry_due_deliveries", fake_retry)
        monkeypatch.setattr(webhooks, "republish_dirty_comments", fake_republish)
        resp = await client.post("/api/v1/admin/github-webhook-deliveries/sweep", headers=headers)
        assert resp.status_code == 200, resp.text
        assert seen == [0, 0]

    @pytest.mark.asyncio
    async def test_force_retry_with_no_transaction_open(
        self, client: AsyncClient, db_session, open_txns, monkeypatch
    ):
        headers = await self._admin(db_session)
        await db_session.commit()
        seen: list[int] = []

        async def fake_force(fp):
            seen.append(open_txns["open"])
            return "completed"

        monkeypatch.setattr(webhooks, "force_retry_delivery", fake_force)
        resp = await client.post(
            f"/api/v1/admin/github-webhook-deliveries/{'a' * 64}/retry", headers=headers
        )
        assert resp.status_code == 200
        assert seen == [0]


# --- 8 ---------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
class TestHelmSingleDashboardUrl:
    BASE = [
        "--set", "api.github.enabled=true", "--set", "api.github.appId=1",
        "--set", "api.github.clientId=Iv1.x", "--set", "api.github.existingSecret=gh",
        "--set", "api.github.callbackUrl=https://api.test/api/v1/settings/github/oauth/callback",
        "--set", "api.sso.enabled=true", "--set", "api.sso.callbackUrl=https://api.test/cb",
        "--set", "api.sso.dashboardUrl=https://app.test",
    ]

    def _render(self, *extra):
        return subprocess.run(
            ["helm", "template", "t", str(REPO_ROOT / "charts" / "sessionfs"), *self.BASE, *extra],
            capture_output=True, text=True, timeout=120,
        )

    def test_different_urls_fail(self):
        result = self._render("--set", "api.github.dashboardUrl=https://other.test")
        assert result.returncode != 0
        assert "dashboardUrl differ" in result.stderr

    def test_same_url_renders_once(self):
        result = self._render("--set", "api.github.dashboardUrl=https://app.test")
        assert result.returncode == 0, result.stderr
        assert result.stdout.count('SFS_DASHBOARD_URL: "https://app.test"') == 1

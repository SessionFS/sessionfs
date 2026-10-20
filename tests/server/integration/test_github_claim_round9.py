"""Regression tests for the ninth review round (tk_b6c5e184ba8d4aa1).

1. Projects holding a remote only in the legacy
   projects.git_remote_normalized column (pre-backfill rows, or rows the
   backfill had to skip) are no installation-claim hijack path: every new
   project gets an unverified primary project_repos row atomically, and
   link_repo locks and displaces a matching legacy holder like any
   unverified holder (409 for unverified claimants).
2. Reclaimed projects are structural — NULL git_remote_normalized plus
   repo_reclaimed_at — and the 'reclaimed:' prefix is rejected on every
   input path; API responses expose an empty remote + repo_reclaimed.
3. Every affected holder project is locked FOR UPDATE in deterministic
   project-id order before any of its repos is deleted or counted.
4. Comment work revalidates the installation's opt-out/detached state
   immediately before leasing and before each external write; disabling,
   detaching or deleting an installation fences pending comment work.
5. Claim attempts have a distinct 'processing' state between consume and
   choose; cleanup never deletes a processing attempt inside its grace,
   and parking the chooser requires the row to still be processing.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AdminAction,
    ApiKey,
    GitHubClaimAttempt,
    GitHubInstallation,
    PRComment,
    Project,
    ProjectRepo,
    Session,
    User,
)
from sessionfs.server.routes import projects as projects_routes
from sessionfs.server.routes import settings as settings_routes
from sessionfs.server.routes import webhooks
from sessionfs.server.services import github_oauth
from tests.server.integration.test_github_claim import (  # noqa: F401 - fixtures
    ACME_INSTALL,
    ALICE_GH,
    _callback,
    _make_user,
    _outcome,
    _start,
    fake_github,
    oauth_env,
)

BETA_INSTALL = github_oauth.UserInstallation(
    id=500003, account_id=9004, account_login="beta", account_type="Organization"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _project_via_api(client: AsyncClient, auth_headers, remote: str) -> str:
    resp = await client.post(
        "/api/v1/projects/",
        headers=auth_headers,
        json={"name": f"p-{uuid.uuid4().hex[:6]}", "git_remote_normalized": remote},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _fresh_project(db: AsyncSession, project_id: str) -> Project:
    return (await db_session_fresh(db, Project, project_id))


async def db_session_fresh(db: AsyncSession, model, pk: str):
    return (
        await db.execute(
            select(model).where(model.id == pk).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _fresh_comment(db: AsyncSession, row_id: str) -> PRComment:
    return await db_session_fresh(db, PRComment, row_id)


def _add_legacy_holder(db: AsyncSession, owner_id: str, remote: str) -> Project:
    """A project holding `remote` ONLY in its legacy column (no repo rows)."""
    holder = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="legacy",
        git_remote_normalized=remote,
        context_document="",
        owner_id=owner_id,
    )
    db.add(holder)
    return holder


# --- 1: legacy holders + atomic primary row on creation ---------------------


class TestLegacyProjectHolders:
    @pytest.mark.asyncio
    async def test_create_project_writes_unverified_primary_repo(
        self, client: AsyncClient, auth_headers, db_session
    ):
        pid = await _project_via_api(client, auth_headers, "github.com/acme/new-proj")
        rows = (
            await db_session.execute(
                select(ProjectRepo).where(ProjectRepo.project_id == pid)
            )
        ).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.git_remote_normalized == "github.com/acme/new-proj"
        assert row.is_primary is True
        assert row.verified is False
        assert row.verification_method == "legacy_backfill"

    @pytest.mark.asyncio
    async def test_verified_link_displaces_legacy_only_holder(
        self, client: AsyncClient, auth_headers, test_user, db_session
    ):
        attacker, _ = await _make_user(db_session, f"att{uuid.uuid4().hex[:4]}@example.com")
        holder = _add_legacy_holder(db_session, attacker.id, "github.com/acme/victim-repo")
        await db_session.commit()
        holder_id = holder.id
        pid = await _project_via_api(client, auth_headers, "github.com/acme/mine")
        with patch(
            "sessionfs.server.github_app.verify_repo_ownership",
            new=AsyncMock(return_value=(True, "github_app", "github", "999")),
        ):
            resp = await client.post(
                f"/api/v1/projects/{pid}/repos",
                json={"git_remote": "github.com/acme/victim-repo"},
                headers=auth_headers,
            )
        assert resp.status_code == 201, resp.text
        assert resp.json()["verified"] is True
        holder = await _fresh_project(db_session, holder_id)
        # Zero repos left: structurally reclaimed, never a placeholder.
        assert holder.repo_reclaimed_at is not None
        assert holder.git_remote_normalized is None
        audits = (
            await db_session.execute(
                select(AdminAction).where(
                    AdminAction.action == "repo_displaced",
                    AdminAction.target_id == holder_id,
                )
            )
        ).scalars().all()
        assert audits
        details = json.loads(audits[-1].details)
        assert details["legacy_holder"] is True
        assert details["displaced_remote"] == "github.com/acme/victim-repo"

    @pytest.mark.asyncio
    async def test_verified_link_realigns_legacy_holder_with_other_repos(
        self, client: AsyncClient, auth_headers, test_user, db_session
    ):
        attacker, _ = await _make_user(db_session, f"att{uuid.uuid4().hex[:4]}@example.com")
        holder = _add_legacy_holder(db_session, attacker.id, "github.com/acme/victim-repo")
        db_session.add(ProjectRepo(
            id=f"repo_{uuid.uuid4().hex[:16]}", project_id=holder.id,
            git_remote_normalized="github.com/acme/att-main", is_primary=True,
            verified=False, verification_method="owner_attested",
        ))
        await db_session.commit()
        holder_id = holder.id
        pid = await _project_via_api(client, auth_headers, "github.com/acme/mine")
        with patch(
            "sessionfs.server.github_app.verify_repo_ownership",
            new=AsyncMock(return_value=(True, "github_app", "github", "999")),
        ):
            resp = await client.post(
                f"/api/v1/projects/{pid}/repos",
                json={"git_remote": "github.com/acme/victim-repo"},
                headers=auth_headers,
            )
        assert resp.status_code == 201, resp.text
        holder = await _fresh_project(db_session, holder_id)
        # The project keeps its real repository; only the squatted legacy
        # value is displaced, and the column is realigned to its primary.
        assert holder.repo_reclaimed_at is None
        assert holder.git_remote_normalized == "github.com/acme/att-main"

    @pytest.mark.asyncio
    async def test_unverified_link_against_legacy_holder_conflicts(
        self, client: AsyncClient, auth_headers, test_user, db_session
    ):
        attacker, _ = await _make_user(db_session, f"att{uuid.uuid4().hex[:4]}@example.com")
        holder = _add_legacy_holder(db_session, attacker.id, "github.com/acme/victim-repo")
        await db_session.commit()
        holder_id = holder.id
        pid = await _project_via_api(client, auth_headers, "github.com/acme/mine")
        with patch(
            "sessionfs.server.github_app.verify_repo_ownership",
            new=AsyncMock(return_value=(False, "owner_attested", None, None)),
        ):
            resp = await client.post(
                f"/api/v1/projects/{pid}/repos",
                json={"git_remote": "github.com/acme/victim-repo"},
                headers=auth_headers,
            )
        assert resp.status_code == 409, resp.text
        error = resp.json()["error"]
        assert error["code"] == "repo_already_linked"
        # F3: the attacker project is invisible to the claimant, so its id
        # is not disclosed.
        assert "existing_project_id" not in error["details"]
        holder = await _fresh_project(db_session, holder_id)
        assert holder.git_remote_normalized == "github.com/acme/victim-repo"
        assert holder.repo_reclaimed_at is None

    @pytest.mark.asyncio
    async def test_revival_link_displaces_legacy_holder_without_conflict(
        self, client: AsyncClient, auth_headers, test_user, db_session
    ):
        """A zero-repo (revival) claimant does not mirror the remote into
        its legacy column before the legacy holder is displaced — that
        order would hit the projects.git_remote_normalized unique
        constraint (IntegrityError 500) instead of displacing."""
        attacker, _ = await _make_user(db_session, f"att{uuid.uuid4().hex[:4]}@example.com")
        holder = _add_legacy_holder(db_session, attacker.id, "github.com/acme/victim-repo")
        # Zero-repo claimant project (revival path), owned by the caller.
        claimant = Project(
            id=f"proj_{uuid.uuid4().hex[:16]}", name="c",
            git_remote_normalized="github.com/acme/c-old", context_document="",
            owner_id=test_user.id, repo_reclaimed_at=_now(),
        )
        db_session.add(claimant)
        await db_session.commit()
        holder_id, claimant_id = holder.id, claimant.id
        with patch(
            "sessionfs.server.github_app.verify_repo_ownership",
            new=AsyncMock(return_value=(True, "github_app", "github", "999")),
        ):
            resp = await client.post(
                f"/api/v1/projects/{claimant_id}/repos",
                json={"git_remote": "github.com/acme/victim-repo"},
                headers=auth_headers,
            )
        assert resp.status_code == 201, resp.text
        holder = await _fresh_project(db_session, holder_id)
        assert holder.repo_reclaimed_at is not None
        assert holder.git_remote_normalized is None
        claimant = await _fresh_project(db_session, claimant_id)
        assert claimant.repo_reclaimed_at is None
        assert claimant.git_remote_normalized == "github.com/acme/victim-repo"


# --- 2: reclaimed is structural; the prefix is reserved ----------------------


class TestReclaimedIsStructural:
    @pytest.mark.asyncio
    async def test_create_project_rejects_reclaimed_prefix(
        self, client: AsyncClient, auth_headers
    ):
        resp = await client.post(
            "/api/v1/projects/",
            headers=auth_headers,
            json={"name": "x", "git_remote_normalized": "reclaimed:proj_1"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_link_repo_rejects_reclaimed_prefix(
        self, client: AsyncClient, auth_headers
    ):
        pid = await _project_via_api(client, auth_headers, "github.com/acme/mine")
        resp = await client.post(
            f"/api/v1/projects/{pid}/repos",
            json={"git_remote": "reclaimed:proj_x"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_reclaimed_project_response_uses_empty_remote_and_flag(
        self, client: AsyncClient, auth_headers, test_user, db_session
    ):
        project = Project(
            id=f"proj_{uuid.uuid4().hex[:16]}", name="r",
            git_remote_normalized=None, context_document="",
            owner_id=test_user.id, repo_reclaimed_at=_now(),
        )
        db_session.add(project)
        await db_session.commit()
        # GET /projects/{remote} resolves by remote: a reclaimed project is
        # unreachable there by design (its remote is NULL).
        resp = await client.get("/api/v1/projects/github.com/acme/gone", headers=auth_headers)
        assert resp.status_code == 404
        listed = (await client.get("/api/v1/projects/", headers=auth_headers)).json()
        row = next(p for p in listed if p["id"] == project.id)
        assert row["git_remote_normalized"] == ""
        assert row["repo_reclaimed"] is True


# --- 3: holder projects locked before delete/count ---------------------------


class TestHolderLocking:
    def test_holder_projects_locked_in_id_order_before_repo_locks(self):
        src = inspect.getsource(projects_routes.link_repo)
        # The ProjectRepo holder check is an UNLOCKED probe (scalar columns,
        # no FOR UPDATE). link_repo must lock the projects before any
        # ProjectRepo row, or it would invert the Project -> ProjectRepo
        # order unlink_repo/merge take and deadlock (tk_b6c5e184ba8d4aa1).
        assert "select(ProjectRepo.id, ProjectRepo.project_id, ProjectRepo.verified)" in src
        i_project_lock = src.index("holder_project_ids = sorted(")
        i_repo_lock = src.index(".where(ProjectRepo.id == remote_holder_probe.id)")
        assert i_project_lock < i_repo_lock
        # The single Project FOR UPDATE query locks the union of target +
        # probed holder projects and orders by Project.id, so concurrent
        # links acquire the same rows in the same order and cannot deadlock.
        project_lock_block = src[i_project_lock:i_repo_lock]
        assert project_lock_block.count(".with_for_update()") == 1
        assert "populate_existing=True" in project_lock_block
        assert "{project_id}" in project_lock_block
        assert ".order_by(Project.id)" in project_lock_block
        # The probed ProjectRepo rows are re-selected FOR UPDATE by id after
        # the projects are locked, and their classification re-validated.
        assert ".where(ProjectRepo.id == remote_holder_probe.id)" in src
        assert ".where(ProjectRepo.id == id_holder_probe.id)" in src
        # The target is re-validated (org + admin standing) under the lock.
        assert "user_is_project_admin(db, user.id, project)" in src
        # The user-reachable placeholder namespace is gone entirely.
        assert "reclaimed_remote_placeholder" not in src

    def test_unlink_locks_project_before_counting(self):
        src = inspect.getsource(projects_routes.unlink_repo)
        i_lock = src.index(".with_for_update()")
        i_count = src.index("select(func.count(ProjectRepo.id))")
        assert i_lock < i_count
        assert "populate_existing=True" in src[:i_count]


class TestInstallationBindLock:
    def test_bind_locks_installation_row_before_reading_holder(self):
        src = inspect.getsource(settings_routes._bind_installation)
        # The takeover side-effects (auto_comment flip + pending-comment
        # fence) are gated on previous_user_id, so that read must happen
        # under a FOR UPDATE lock on the installation row. An unlocked read
        # could observe a stale holder and skip the fence on a takeover that
        # the conditional UPDATE then wins against the live row (TOCTOU).
        i_user_lock = src.index(".where(User.id == user_id).with_for_update()")
        i_install_read = src.index("select(GitHubInstallation.user_id)")
        assert i_user_lock < i_install_read
        read_block = src[
            i_install_read:src.index("previous_user_id = previous.scalar_one_or_none()")
        ]
        assert ".with_for_update()" in read_block
        # The fence is still gated on the now-locked holder read.
        assert "previous_user_id is not None and previous_user_id != user_id" in src


# --- 4: comment work revalidates the installation ----------------------------


def _signed(body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest(),
        "X-GitHub-Event": "installation",
    }


@pytest.fixture(autouse=True)
def _signed_mode(monkeypatch):
    monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "whsec")
    monkeypatch.delenv("SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED", raising=False)


def _behind_comment(row_id: str, installation_id: int, *, lease: str | None = None) -> PRComment:
    return PRComment(
        id=row_id, installation_id=installation_id, repo_full_name="acme/r", pr_number=3,
        comment_id=11, session_ids="[]", desired_body="body", body_version=2,
        published_version=0,
        publish_lease=lease,
        publish_lease_until=(_now() + timedelta(seconds=60)) if lease else None,
        created_at=_now(), updated_at=_now(),
    )


@pytest.fixture
def no_github(monkeypatch):
    """Any GitHub call from the comment pipeline fails the test."""

    async def boom(*a, **k):
        raise AssertionError("no GitHub call expected")

    monkeypatch.setattr(webhooks, "get_installation_token", boom)
    monkeypatch.setattr(webhooks, "post_or_update_comment", boom)
    monkeypatch.setattr(webhooks, "find_marked_comment", boom)


class TestCommentWorkRevalidation:
    @pytest.mark.asyncio
    async def test_disabling_auto_comment_fences_pending_work(
        self, client: AsyncClient, auth_headers, test_user, db_session, no_github
    ):
        db_session.add(GitHubInstallation(
            id=777001, user_id=test_user.id, account_login="acme",
            account_type="Organization", auto_comment=True,
        ))
        db_session.add(_behind_comment("pc1", 777001, lease="L"))
        await db_session.commit()
        resp = await client.put(
            "/api/v1/settings/github", headers=auth_headers, json={"auto_comment": False}
        )
        assert resp.status_code == 200, resp.text
        row = await _fresh_comment(db_session, "pc1")
        assert row.published_version == row.body_version
        assert row.publish_lease is None and row.publish_lease_until is None

    @pytest.mark.asyncio
    async def test_sweeper_revalidates_and_fences_disabled_installation(
        self, client: AsyncClient, db_session, no_github
    ):
        # Legacy state: disabled without fencing (pre-fix rows, or a row
        # that became dirty again through an in-flight publisher).
        db_session.add(GitHubInstallation(
            id=777002, user_id=None, account_login="acme",
            account_type="Organization", auto_comment=False,
        ))
        db_session.add(_behind_comment("pc2", 777002))
        await db_session.commit()
        stats = await webhooks.republish_dirty_comments(10)
        assert stats == {"comments_republished": 0, "comment_republish_failed": 0}
        row = await _fresh_comment(db_session, "pc2")
        assert row.published_version == row.body_version

    @pytest.mark.asyncio
    async def test_sweeper_revalidates_detached_installation(
        self, client: AsyncClient, db_session, no_github
    ):
        # Detached installation: row gone entirely.
        db_session.add(_behind_comment("pc3", 777099))
        await db_session.commit()
        stats = await webhooks.republish_dirty_comments(10)
        assert stats == {"comments_republished": 0, "comment_republish_failed": 0}
        row = await _fresh_comment(db_session, "pc3")
        assert row.published_version == row.body_version

    @pytest.mark.asyncio
    async def test_publish_with_lease_revalidates_before_external_write(
        self, db_session, no_github
    ):
        db_session.add(GitHubInstallation(
            id=777003, user_id=None, account_login="acme",
            account_type="Organization", auto_comment=False,
        ))
        db_session.add(_behind_comment("pc4", 777003, lease="mine"))
        await db_session.commit()
        ok = await webhooks._publish_with_lease(
            db_session, "tok", "pc4", "mine", "acme/r", 3, 11
        )
        assert ok is True
        row = await _fresh_comment(db_session, "pc4")
        assert row.published_version == row.body_version
        assert row.publish_lease is None

    @pytest.mark.asyncio
    async def test_installation_deleted_fences_pending_work(
        self, client: AsyncClient, db_session, no_github
    ):
        db_session.add(GitHubInstallation(
            id=970002, user_id=None, account_login="acme", account_type="Organization",
        ))
        db_session.add(_behind_comment("pc5", 970002))
        await db_session.commit()
        body = json.dumps({
            "action": "deleted",
            "installation": {
                "id": 970002, "app_id": 1, "target_type": "Organization",
                "account": {"id": 5, "login": "acme", "type": "Organization"},
            },
        }).encode()
        resp = await client.post("/webhooks/github", content=body, headers=_signed(body))
        assert resp.status_code == 200, resp.text
        assert await db_session.get(GitHubInstallation, 970002) is None
        row = await _fresh_comment(db_session, "pc5")
        assert row.published_version == row.body_version

    @pytest.mark.asyncio
    async def test_rebind_detaches_and_fences_previous_installation(
        self, db_session, test_user, no_github
    ):
        db_session.add(GitHubInstallation(
            id=777004, user_id=test_user.id, account_login="old",
            account_type="Organization", auto_comment=True,
        ))
        db_session.add(_behind_comment("pc6", 777004))
        await db_session.commit()
        info = github_oauth.UserInstallation(
            id=777005, account_id=42, account_login="new", account_type="Organization"
        )
        gh_user = github_oauth.GitHubUser(id=1001, login="alice")
        ok = await settings_routes._bind_installation(
            db_session, user_id=test_user.id, gh_user=gh_user, info=info, role="org_admin"
        )
        assert ok is True
        old = await db_session.get(GitHubInstallation, 777004, populate_existing=True)
        assert old.user_id is None and old.auto_comment is False
        row = await _fresh_comment(db_session, "pc6")
        assert row.published_version == row.body_version
        assert row.publish_lease is None

    @pytest.mark.asyncio
    async def test_takeover_flips_auto_comment_off(
        self, db_session, test_user, no_github
    ):
        # A legacy (never-proven) claim held by another user is taken over
        # by a privileged claimant. Ownership moves but the previous holder's
        # sessions no longer scope the installation, so its PR comments must
        # stop auto-publishing on the new owner's behalf.
        other, _ = await _make_user(db_session, "takeover-other@example.com")
        db_session.add(GitHubInstallation(
            id=777007, user_id=other.id, account_login="acme",
            account_type="Organization", auto_comment=True,
        ))
        await db_session.commit()
        info = github_oauth.UserInstallation(
            id=777007, account_id=42, account_login="acme", account_type="Organization"
        )
        gh_user = github_oauth.GitHubUser(id=1001, login="alice")
        ok = await settings_routes._bind_installation(
            db_session, user_id=test_user.id, gh_user=gh_user, info=info, role="org_admin"
        )
        assert ok is True
        row = await db_session.get(GitHubInstallation, 777007, populate_existing=True)
        assert row.user_id == test_user.id
        assert row.auto_comment is False


# --- 5: the 'processing' claim-attempt state ---------------------------------


def _attempt(
    user_id: str, status: str, *, state: str | None = None,
    consumed_at: datetime | None = None, expires_at: datetime | None = None,
) -> GitHubClaimAttempt:
    now = _now()
    return GitHubClaimAttempt(
        id=f"gca_{uuid.uuid4().hex[:8]}",
        state_hash=hashlib.sha256((state or uuid.uuid4().hex).encode()).hexdigest(),
        user_id=user_id,
        nonce_hash="n",
        pkce_verifier_hash="p",
        flow="authorize",
        status=status,
        created_at=now,
        expires_at=expires_at or (now + timedelta(minutes=10)),
        consumed_at=consumed_at,
    )


class TestClaimProcessingState:
    @pytest.mark.asyncio
    async def test_consume_marks_attempt_processing(
        self, db_session, test_user
    ):
        attempt = _attempt(test_user.id, "pending", state="s1")
        db_session.add(attempt)
        await db_session.commit()
        consumed = await settings_routes._consume_claim_attempt(
            db_session, "s1", flow="authorize"
        )
        assert consumed is not None
        fresh = (
            await db_session.execute(
                select(GitHubClaimAttempt)
                .where(GitHubClaimAttempt.id == consumed.id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert fresh.status == "processing"
        assert fresh.consumed_at is not None

    @pytest.mark.asyncio
    async def test_create_attempt_housekeeping_keeps_fresh_processing(
        self, db_session, test_user
    ):
        fresh = _attempt(test_user.id, "processing", consumed_at=_now())
        old = _attempt(
            test_user.id, "processing",
            consumed_at=_now() - timedelta(minutes=30),
        )
        consumed = _attempt(test_user.id, "consumed", consumed_at=_now())
        pending = _attempt(test_user.id, "pending")
        db_session.add_all([fresh, old, consumed, pending])
        await db_session.commit()
        await settings_routes._create_attempt(
            db_session, user_id=test_user.id, flow="authorize", target=None
        )
        remaining = {
            a.id
            for a in (await db_session.execute(select(GitHubClaimAttempt))).scalars()
        }
        assert fresh.id in remaining
        assert pending.id in remaining
        assert old.id not in remaining
        assert consumed.id not in remaining

    @pytest.mark.asyncio
    async def test_admin_sweep_keeps_fresh_processing(
        self, client: AsyncClient, db_session, test_user
    ):
        admin = User(
            id=str(uuid.uuid4()), email="admin9@sessionfs.dev", tier="admin",
            email_verified=True, created_at=_now(),
        )
        raw_key = generate_api_key()
        db_session.add(admin)
        db_session.add(ApiKey(
            id=str(uuid.uuid4()), user_id=admin.id, key_hash=hash_api_key(raw_key),
            name="k", created_at=_now(),
        ))
        fresh = _attempt(test_user.id, "processing", consumed_at=_now())
        old = _attempt(
            test_user.id, "processing",
            consumed_at=_now() - timedelta(minutes=30),
        )
        consumed = _attempt(test_user.id, "consumed", consumed_at=_now())
        db_session.add_all([fresh, old, consumed])
        await db_session.commit()
        resp = await client.post(
            "/api/v1/admin/github-webhook-deliveries/sweep",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={"retry_limit": 0},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["claim_attempts_purged"] == 2
        remaining = {
            a.id
            for a in (await db_session.execute(select(GitHubClaimAttempt))).scalars()
        }
        assert remaining == {fresh.id}

    @pytest.mark.asyncio
    async def test_chooser_parks_from_processing(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        fake_github.add_code(
            "c1", ALICE_GH, [ACME_INSTALL, BETA_INSTALL], admin_orgs=["acme", "beta"]
        )
        state, cookie = await _start(client, auth_headers, flow="authorize")
        resp = await _callback(client, code="c1", state=state, cookie=cookie)
        assert _outcome(resp) == "choose"
        attempt = (
            await db_session.execute(
                select(GitHubClaimAttempt).where(GitHubClaimAttempt.status == "choosing")
            )
        ).scalars().one()
        assert attempt.choices_json

    @pytest.mark.asyncio
    async def test_chooser_parking_fails_when_attempt_moved_on(
        self, client: AsyncClient, auth_headers, db_session, fake_github, monkeypatch
    ):
        fake_github.add_code(
            "c1", ALICE_GH, [ACME_INSTALL, BETA_INSTALL], admin_orgs=["acme", "beta"]
        )
        real_list = github_oauth.list_user_installations

        async def flipping_list(token):
            # Cleanup raced the callback: the attempt left 'processing'
            # after it was consumed but before the chooser was parked.
            await db_session.execute(
                update(GitHubClaimAttempt).values(status="consumed")
            )
            await db_session.commit()
            return await real_list(token)

        monkeypatch.setattr(github_oauth, "list_user_installations", flipping_list)
        state, cookie = await _start(client, auth_headers, flow="authorize")
        resp = await _callback(client, code="c1", state=state, cookie=cookie)
        assert _outcome(resp) == "failed"
        # No chooser was parked.
        assert (
            await db_session.execute(
                select(GitHubClaimAttempt).where(GitHubClaimAttempt.status == "choosing")
            )
        ).scalars().first() is None


# --- 6: same-project re-verification upgrades in place -----------------------


class TestSameProjectReverification:
    @pytest.mark.asyncio
    async def test_reverify_own_unverified_repo_upgrades_in_place(
        self, client: AsyncClient, auth_headers, db_session
    ):
        pid = await _project_via_api(client, auth_headers, "github.com/acme/mine")
        before = (
            await db_session.execute(
                select(ProjectRepo).where(ProjectRepo.project_id == pid)
            )
        ).scalars().one()
        repo_id = before.id
        assert before.verified is False
        assert before.is_primary is True

        with patch(
            "sessionfs.server.github_app.verify_repo_ownership",
            new=AsyncMock(return_value=(True, "github_app", "github", "999")),
        ):
            resp = await client.post(
                f"/api/v1/projects/{pid}/repos",
                json={"git_remote": "github.com/acme/mine"},
                headers=auth_headers,
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["id"] == repo_id  # upgraded in place, not replaced
        assert body["verified"] is True
        assert body["is_primary"] is True  # primary status preserved
        assert body["verification_method"] == "github_app"
        assert body["provider"] == "github"
        assert body["provider_repo_id"] == "999"

        rows = (
            await db_session.execute(
                select(ProjectRepo).where(ProjectRepo.project_id == pid)
            )
        ).scalars().all()
        assert [r.id for r in rows] == [repo_id]
        project = await _fresh_project(db_session, pid)
        assert project.repo_reclaimed_at is None
        assert project.git_remote_normalized == "github.com/acme/mine"

    @pytest.mark.asyncio
    async def test_reverify_nonprimary_with_is_primary_promotes_in_place(
        self, client: AsyncClient, auth_headers, db_session
    ):
        """Re-verifying a non-primary repo with is_primary=true must promote it:
        the current primary is demoted (kept linked), the re-verified row is
        promoted, and the legacy column is mirrored."""
        pid = await _project_via_api(client, auth_headers, "github.com/acme/mine")
        # Link a second repo as non-primary, unverified (the state migration
        # 073 leaves a revoked github_app row in: owner_attested).
        resp = await client.post(
            f"/api/v1/projects/{pid}/repos",
            json={"git_remote": "github.com/acme/second", "is_primary": False},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        second_id = resp.json()["id"]
        assert resp.json()["is_primary"] is False

        with patch(
            "sessionfs.server.github_app.verify_repo_ownership",
            new=AsyncMock(return_value=(True, "github_app", "github", "999")),
        ):
            resp = await client.post(
                f"/api/v1/projects/{pid}/repos",
                json={"git_remote": "github.com/acme/second", "is_primary": True},
                headers=auth_headers,
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["id"] == second_id  # upgraded in place, not replaced
        assert body["is_primary"] is True
        assert body["verified"] is True
        assert body["verification_method"] == "github_app"

        primaries = (
            await db_session.execute(
                select(ProjectRepo).where(
                    ProjectRepo.project_id == pid,
                    ProjectRepo.is_primary == True,  # noqa: E712
                )
            )
        ).scalars().all()
        assert [r.id for r in primaries] == [second_id]
        # The original primary is demoted (kept linked), not unlinked.
        old = (
            await db_session.execute(
                select(ProjectRepo).where(
                    ProjectRepo.project_id == pid,
                    ProjectRepo.git_remote_normalized == "github.com/acme/mine",
                )
            )
        ).scalar_one_or_none()
        assert old is not None
        assert old.is_primary is False
        project = await _fresh_project(db_session, pid)
        assert project.git_remote_normalized == "github.com/acme/second"


# --- 7: PR comments require a linked owner and scope sessions to it ----------


def _pr_event(
    installation_id: int, *, repo: str = "acme/r", branch: str = "feature-x"
) -> dict:
    return {
        "pull_request": {"number": 3, "head": {"ref": branch}},
        "repository": {
            "full_name": repo,
            "clone_url": f"https://github.com/{repo}.git",
        },
        "installation": {"id": installation_id},
    }


def _session(
    user_id: str,
    *,
    remote: str = "acme/r",
    branch: str = "feature-x",
    title: str = "s",
) -> Session:
    now = _now()
    return Session(
        id=f"ses_{uuid.uuid4().hex[:12]}",
        user_id=user_id,
        title=title,
        tags="[]",
        source_tool="claude-code",
        blob_key="k",
        etag="e",
        git_remote_normalized=remote,
        git_branch=branch,
        created_at=now,
        updated_at=now,
        uploaded_at=now,
    )


class TestCommentOwnerScoping:
    @pytest.mark.asyncio
    async def test_unclaimed_installation_posts_nothing(
        self, client: AsyncClient, test_user, db_session, no_github
    ):
        db_session.add(GitHubInstallation(
            id=888001, user_id=None, account_login="acme",
            account_type="Organization", auto_comment=True,
        ))
        db_session.add(_session(test_user.id))
        await db_session.commit()
        assert await webhooks._handle_pr_event(_pr_event(888001)) is True
        comments = (
            await db_session.execute(
                select(PRComment).where(PRComment.installation_id == 888001)
            )
        ).scalars().all()
        assert comments == []

    @pytest.mark.asyncio
    async def test_auto_comment_off_posts_nothing(
        self, client: AsyncClient, test_user, db_session, no_github
    ):
        db_session.add(GitHubInstallation(
            id=888002, user_id=test_user.id, account_login="acme",
            account_type="Organization", auto_comment=False,
        ))
        db_session.add(_session(test_user.id))
        await db_session.commit()
        assert await webhooks._handle_pr_event(_pr_event(888002)) is True
        comments = (
            await db_session.execute(
                select(PRComment).where(PRComment.installation_id == 888002)
            )
        ).scalars().all()
        assert comments == []

    @pytest.mark.asyncio
    async def test_sessions_are_scoped_to_installation_owner(
        self, client: AsyncClient, test_user, db_session, no_github
    ):
        # The installation is linked to test_user; the only matching session
        # belongs to a DIFFERENT user, so it must not be published.
        db_session.add(GitHubInstallation(
            id=888003, user_id=test_user.id, account_login="acme",
            account_type="Organization", auto_comment=True,
        ))
        attacker, _ = await _make_user(db_session, f"att{uuid.uuid4().hex[:4]}@example.com")
        db_session.add(_session(attacker.id, title="attacker-title"))
        await db_session.commit()
        assert await webhooks._handle_pr_event(_pr_event(888003)) is True
        comments = (
            await db_session.execute(
                select(PRComment).where(PRComment.installation_id == 888003)
            )
        ).scalars().all()
        assert comments == []


# --- 8: the settings update cannot mutate a re-bound installation ------------


class TestInstallationUpdateTakeover:
    @pytest.mark.asyncio
    async def test_update_conflicts_when_installation_was_taken_over(
        self, client: AsyncClient, auth_headers, test_user, db_session, no_github, monkeypatch
    ):
        db_session.add(GitHubInstallation(
            id=999001, user_id=test_user.id, account_login="acme",
            account_type="Organization", auto_comment=True,
        ))
        await db_session.commit()

        # The caller's SELECT still sees the installation as theirs...
        stale = await settings_routes._current_installation(db_session, test_user.id)
        assert stale is not None

        async def _stale_current(_db, _user_id):
            return stale

        monkeypatch.setattr(settings_routes, "_current_installation", _stale_current)

        # ...but a privileged claimant has already re-bound the row before
        # the conditional UPDATE runs.
        await db_session.execute(
            update(GitHubInstallation)
            .where(GitHubInstallation.id == 999001)
            .values(user_id="someone-else")
            .execution_options(synchronize_session=False)
        )
        await db_session.commit()

        resp = await client.put(
            "/api/v1/settings/github", headers=auth_headers, json={"auto_comment": False}
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "installation_changed"
        # The new owner's settings are untouched.
        fresh = await db_session_fresh(db_session, GitHubInstallation, 999001)
        assert fresh.user_id == "someone-else"
        assert fresh.auto_comment is True


# --- 6: comment adoption is scoped to this App's id -------------------------


class TestOwnAppCommentAdoption:
    def test_matching_app_via_payload_is_adopted(self, monkeypatch):
        from sessionfs.server import github_app

        monkeypatch.setattr(github_app, "GITHUB_APP_ID", "123")
        assert github_app._is_own_app_comment(
            {"performed_via_github_app": {"id": 123}}
        ) is True

    def test_mismatched_app_via_payload_is_not_adopted(self, monkeypatch):
        from sessionfs.server import github_app

        monkeypatch.setattr(github_app, "GITHUB_APP_ID", "123")
        assert github_app._is_own_app_comment(
            {"performed_via_github_app": {"id": 999}}
        ) is False

    def test_foreign_bot_comment_not_adopted_when_app_id_known(self, monkeypatch):
        from sessionfs.server import github_app

        monkeypatch.setattr(github_app, "GITHUB_APP_ID", "123")
        # Another bot (e.g. Dependabot) quoting the marker must not be adopted
        # merely because its user.type is Bot.
        assert github_app._is_own_app_comment(
            {"user": {"type": "Bot", "id": 999}}
        ) is False

    def test_user_comment_not_adopted_when_app_id_known(self, monkeypatch):
        from sessionfs.server import github_app

        monkeypatch.setattr(github_app, "GITHUB_APP_ID", "123")
        assert github_app._is_own_app_comment(
            {"user": {"type": "User", "id": 1}}
        ) is False

    def test_bot_fallback_only_when_app_id_unset(self, monkeypatch):
        from sessionfs.server import github_app

        monkeypatch.setattr(github_app, "GITHUB_APP_ID", "")
        # Without a configured App id the pre-existing degraded fallback still
        # adopts any Bot comment.
        assert github_app._is_own_app_comment({"user": {"type": "Bot"}}) is True

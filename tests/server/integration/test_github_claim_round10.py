# ruff: noqa: F811 - fixtures are imported from test_github_claim and used as parameters
"""Regression tests for the round-10 review findings on GitHub installation claims.

1. Repository verification requires the repository to be in the
   installation's grant (GET /installation/repositories), not merely
   readable by the installation token (any public repository is), and is
   only attempted for a github.com remote with exactly owner/repo.
2. A PR comment body built for one installation owner is never recorded or
   published after the installation changed hands.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import sessionfs.server.db.engine as engine_mod
from sessionfs.server import github_app
from sessionfs.server.db.models import Base, GitHubInstallation, PRComment, Project, User
from sessionfs.server.db.models import Session as SessionRow
from sessionfs.server.routes import webhooks
from sessionfs.server.services import github_oauth
from tests.server.integration.test_github_claim import _create_installation


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- 1: host / path gating --------------------------------------------------


@pytest.mark.parametrize(
    "remote, expected",
    [
        ("github.com/acme/r", ("acme", "r")),
        ("https://github.com/Acme/R.git", ("Acme", "R")),
        ("git@github.com:acme/r.git", ("acme", "r")),
        ("ssh://git@github.com/acme/r.git", ("acme", "r")),
        ("https://gitlab.com/group/sub/repo", None),
        ("gitlab.com/group/sub/repo", None),
        ("https://gitlab.com/acme/r", None),
        ("git@gitlab.com:acme/r.git", None),
        ("https://github.com/group/sub/repo", None),
        ("acme/r", None),
        ("https://github.com:8443/acme/r", None),
        ("github.com/acme/..", None),
        ("", None),
    ],
)
def test_github_owner_repo_only_for_two_part_github_remotes(remote, expected):
    assert github_app.github_owner_repo_from_remote(remote) == expected


# --- 1: installation grant, not readability --------------------------------


class FakeInstallationApi:
    """GitHub API as seen by one installation token.

    ``readable`` repositories answer GET /repos/{o}/{r} with 200 (any public
    repository does); ``granted`` pages are what GET /installation/repositories
    lists for the installation.
    """

    def __init__(self, granted_pages: list[list[dict]], readable: dict[str, int]):
        self.granted_pages = granted_pages
        self.readable = readable
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if request.url.path == "/installation/repositories":
            page = int(request.url.params.get("page", "1"))
            repos = self.granted_pages[page - 1] if page <= len(self.granted_pages) else []
            headers = {}
            if page < len(self.granted_pages):
                headers["link"] = (
                    f'<https://api.github.com/installation/repositories?page={page + 1}>; '
                    'rel="next"'
                )
            total = sum(len(p) for p in self.granted_pages)
            return httpx.Response(
                200, json={"total_count": total, "repositories": repos}, headers=headers
            )
        if request.url.path.startswith("/repos/"):
            full = request.url.path[len("/repos/"):]
            if full.lower() in self.readable:
                return httpx.Response(200, json={"id": self.readable[full.lower()]})
            return httpx.Response(404, json={})
        return httpx.Response(404, json={})

    def install(self, monkeypatch) -> None:
        transport = httpx.MockTransport(self.handler)
        real_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(github_app.httpx, "AsyncClient", client_factory)

        async def fake_token(installation_id):
            return "tok"

        monkeypatch.setattr(github_app, "get_installation_token", fake_token)
        monkeypatch.setattr(github_app, "GITHUB_APP_ID", "123")
        monkeypatch.setattr(github_app, "GITHUB_APP_PRIVATE_KEY", "pem")


def _filler(n: int, offset: int = 0) -> list[dict]:
    return [
        {"id": 10_000 + offset + i, "full_name": f"attacker/repo-{offset + i}"}
        for i in range(n)
    ]


class TestVerificationNeedsInstallationGrant:
    @pytest.mark.asyncio
    async def test_public_repo_readable_but_not_granted_is_not_verified(
        self, db_session, test_user, monkeypatch
    ):
        # The attacker installed the App on their own account; their token
        # can read the victim's PUBLIC repository, but it is not granted.
        api = FakeInstallationApi(
            granted_pages=[_filler(3)], readable={"victim/public-repo": 777}
        )
        api.install(monkeypatch)
        await _create_installation(
            db_session, installation_id=710001, user_id=test_user.id,
            account_login="attacker", account_type="User", verified=True,
            role=github_oauth.ROLE_ACCOUNT_OWNER,
        )
        result = await github_app.verify_repo_ownership(
            db_session, test_user.id, "victim", "public-repo"
        )
        assert result == (False, "owner_attested", None, None)
        assert "/installation/repositories" in api.requests

    @pytest.mark.asyncio
    async def test_selected_repos_install_containing_repo_verifies(
        self, db_session, test_user, monkeypatch
    ):
        # Granted on the second page (Link: rel="next"), matched
        # case-insensitively; the id comes from the grant listing.
        api = FakeInstallationApi(
            granted_pages=[
                _filler(100),
                [{"id": 424242, "full_name": "Acme/Backend"}],
            ],
            readable={"acme/backend": 999},
        )
        api.install(monkeypatch)
        await _create_installation(
            db_session, installation_id=710002, user_id=test_user.id, verified=True,
            role="org_admin",
        )
        result = await github_app.verify_repo_ownership(
            db_session, test_user.id, "acme", "backend"
        )
        assert result == (True, "github_app", "github", "424242")
        assert not any(p.startswith("/repos/") for p in api.requests)

    @pytest.mark.asyncio
    async def test_listing_error_fails_closed(self, db_session, test_user, monkeypatch):
        api = FakeInstallationApi(granted_pages=[], readable={"acme/backend": 1})

        def broken(request):
            return httpx.Response(500, json={})

        api.handler = broken  # type: ignore[method-assign]
        api.install(monkeypatch)
        await _create_installation(
            db_session, installation_id=710003, user_id=test_user.id, verified=True,
            role="org_admin",
        )
        result = await github_app.verify_repo_ownership(
            db_session, test_user.id, "acme", "backend"
        )
        assert result == (False, "owner_attested", None, None)


class TestLinkRouteHostGate:
    async def _project(self, db: AsyncSession, owner: User) -> Project:
        p = Project(
            id=f"proj_{uuid.uuid4().hex[:16]}", name="p",
            git_remote_normalized=f"github.com/{uuid.uuid4().hex[:8]}/repo",
            context_document="", owner_id=owner.id,
        )
        db.add(p)
        await db.commit()
        return p

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "remote",
        [
            "https://gitlab.com/group/sub/repo",
            "https://gitlab.com/acme/repo",
            "https://github.com/group/sub/repo",
        ],
    )
    async def test_non_github_or_subgroup_remote_never_checked_against_github(
        self, client: AsyncClient, auth_headers, db_session, test_user, remote
    ):
        project = await self._project(db_session, test_user)
        verify = AsyncMock(return_value=(True, "github_app", "github", "1"))
        with patch("sessionfs.server.github_app.verify_repo_ownership", new=verify):
            resp = await client.post(
                f"/api/v1/projects/{project.id}/repos",
                json={"git_remote": remote}, headers=auth_headers,
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["verified"] is False
        assert body["verification_method"] == "owner_attested"
        verify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_github_remote_is_checked_with_raw_owner_repo(
        self, client: AsyncClient, auth_headers, db_session, test_user
    ):
        project = await self._project(db_session, test_user)
        verify = AsyncMock(return_value=(False, "owner_attested", None, None))
        with patch("sessionfs.server.github_app.verify_repo_ownership", new=verify):
            resp = await client.post(
                f"/api/v1/projects/{project.id}/repos",
                json={"git_remote": "https://github.com/Acme/Widget.git"},
                headers=auth_headers,
            )
        assert resp.status_code == 201, resp.text
        verify.assert_awaited_once()
        assert verify.await_args.args[2:] == ("Acme", "Widget")


# --- 2: owner change between body build and record --------------------------


@pytest.fixture
async def file_db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'r10.db'}", connect_args={"timeout": 30}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(engine_mod, "_session_factory", factory)
    yield factory
    await engine.dispose()


PAYLOAD = {
    "pull_request": {"head": {"ref": "feat"}, "number": 5},
    "repository": {"full_name": "acme/repo", "clone_url": "https://github.com/acme/repo.git"},
    "installation": {"id": 720001},
}


class TestOwnerChangeAbortsComment:
    async def _setup(self, factory, old_owner: str, new_owner: str) -> None:
        async with factory() as db:
            for uid in (old_owner, new_owner):
                db.add(User(id=uid, email=f"{uid}@example.com", tier="pro",
                            email_verified=True, created_at=_now()))
            db.add(SessionRow(
                id=f"ses_{uuid.uuid4().hex[:16]}", user_id=old_owner,
                title="old-owner-secret-session", source_tool="claude-code",
                blob_key="k", etag="e", git_remote_normalized="acme/repo",
                git_branch="feat", message_count=1,
            ))
            db.add(GitHubInstallation(
                id=720001, user_id=old_owner, account_login="acme",
                account_type="Organization", auto_comment=True,
            ))
            await db.commit()

    @pytest.mark.asyncio
    async def test_rebind_during_token_fetch_aborts_record(self, file_db, monkeypatch):
        old_owner, new_owner = str(uuid.uuid4()), str(uuid.uuid4())
        await self._setup(file_db, old_owner, new_owner)
        posted: list = []

        async def token_while_rebound(installation_id):
            # The installation is handed to a new owner (who re-enables
            # comments) after the body was built from the old owner's
            # sessions and before the desired comment is recorded.
            async with file_db() as db:
                inst = await db.get(GitHubInstallation, 720001)
                inst.user_id = new_owner
                inst.auto_comment = True
                await db.commit()
            return "tok"

        async def fake_post(*a, **k):
            posted.append(a)
            return 1

        async def fake_find(*a, **k):
            return None

        monkeypatch.setattr(webhooks, "get_installation_token", token_while_rebound)
        monkeypatch.setattr(webhooks, "post_or_update_comment", fake_post)
        monkeypatch.setattr(webhooks, "find_marked_comment", fake_find)
        assert await webhooks._handle_pr_event(PAYLOAD) is True
        assert posted == []
        async with file_db() as db:
            assert (await db.execute(select(PRComment))).scalars().all() == []

    @pytest.mark.asyncio
    async def test_same_owner_records_and_stamps_owner(self, file_db, monkeypatch):
        old_owner, new_owner = str(uuid.uuid4()), str(uuid.uuid4())
        await self._setup(file_db, old_owner, new_owner)
        posted: list[str] = []

        async def fake_token(installation_id):
            return "tok"

        async def fake_post(token, repo, pr, body, existing=None):
            posted.append(body)
            return 31

        async def fake_find(*a, **k):
            return None

        monkeypatch.setattr(webhooks, "get_installation_token", fake_token)
        monkeypatch.setattr(webhooks, "post_or_update_comment", fake_post)
        monkeypatch.setattr(webhooks, "find_marked_comment", fake_find)
        assert await webhooks._handle_pr_event(PAYLOAD) is True
        assert len(posted) == 1
        async with file_db() as db:
            (row,) = (await db.execute(select(PRComment))).scalars().all()
            assert row.owner_user_id == old_owner

    @pytest.mark.asyncio
    async def test_publish_refuses_body_of_previous_owner(self, file_db, monkeypatch):
        old_owner, new_owner = str(uuid.uuid4()), str(uuid.uuid4())
        await self._setup(file_db, old_owner, new_owner)
        async with file_db() as db:
            inst = await db.get(GitHubInstallation, 720001)
            inst.user_id = new_owner  # handed over; new owner re-enabled comments
            db.add(PRComment(
                id="stale", installation_id=720001, repo_full_name="acme/repo", pr_number=5,
                comment_id=4242, session_ids="[]", desired_body="old owner's sessions",
                body_version=2, published_version=1, owner_user_id=old_owner,
                created_at=_now(), updated_at=_now(),
            ))
            await db.commit()
        posted: list = []

        async def fake_post(*a, **k):
            posted.append(a)
            return 4242

        monkeypatch.setattr(webhooks, "post_or_update_comment", fake_post)
        async with file_db() as db:
            assert await webhooks._publish_newest(db, "t", "stale", "acme/repo", 5, 4242) is True
        assert posted == []
        async with file_db() as db:
            row = await db.get(PRComment, "stale")
            assert row.published_version == row.body_version
            assert row.publish_lease is None

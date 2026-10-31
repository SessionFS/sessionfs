"""GitLab commit status (track F2b) — readiness badge + SSRF guard.

The ingest service badges a merge request's head commit through the project
owner's GitLab integration: ``success`` only when every linked ticket is
merge-ready for that exact sha, otherwise ``pending``. These tests exercise
the readiness→state mapping with a mocked GitLab API and keep the SSRF guard
in the request path (a separate unit test proves ``post_commit_status``
re-checks the instance URL and never follows redirects).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server import gitlab_client as gc
from sessionfs.server.db.models import (
    GitLabSettings,
    Project,
    ProjectRepo,
    PullRequest,
    Ticket,
    TicketComment,
    TicketPrLink,
    User,
)
from sessionfs.server.llm_keys import encrypt_secret
from sessionfs.server.services import delivery_ingest


SHA = "a" * 40
CLEAN = "Codex R1 review on tk_x: VERIFIED-CLEAN\n\nFindings: none."


async def _make_user(db: AsyncSession, name: str) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_project(db: AsyncSession, owner: User, remote: str) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"gls-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=remote,
        context_document="",
        owner_id=owner.id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_repo(
    db: AsyncSession, project: Project, remote: str, provider_repo_id: str
) -> None:
    db.add(
        ProjectRepo(
            id=f"repo_{uuid.uuid4().hex[:16]}",
            project_id=project.id,
            git_remote_normalized=remote,
            provider="gitlab",
            provider_repo_id=provider_repo_id,
            is_primary=True,
        )
    )
    await db.commit()


async def _make_settings(db: AsyncSession, owner: User) -> None:
    db.add(
        GitLabSettings(
            user_id=owner.id,
            instance_url="https://gitlab.example.com",
            encrypted_access_token=encrypt_secret("glpat-test"),
            webhook_secret="wh-secret",
        )
    )
    await db.commit()


async def _make_ticket_and_pr(
    db: AsyncSession,
    project: Project,
    owner: User,
    remote: str,
    *,
    reviewer: User | None = None,
    reviewed_commit: str | None = None,
) -> None:
    """A ticket linked to a GitLab PR for ``remote``; optionally a trusted clean
    verdict. The owner is the implementer (via ``resolver_user_id``)."""
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title=f"gls ticket {uuid.uuid4().hex[:6]}",
        description="commit status coverage",
        created_by_user_id=owner.id,
        status="open",
        resolver_user_id=owner.id,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)

    pr = PullRequest(
        id=f"pr_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        provider="gitlab",
        repo=remote,
        number=9,
        title=f"ship {ticket.id}",
        state="open",
        branch="feat/y",
        head_sha=SHA,
    )
    db.add(pr)
    await db.commit()
    await db.refresh(pr)

    db.add(
        TicketPrLink(ticket_id=ticket.id, pr_id=pr.id, link_source="auto_parsed")
    )
    await db.commit()

    if reviewer is not None:
        db.add(
            TicketComment(
                id=f"tc_{uuid.uuid4().hex[:16]}",
                ticket_id=ticket.id,
                author_user_id=reviewer.id,
                author_persona="codex-reviewer",
                content=CLEAN,
                verdict_trusted=True,
                reviewed_commit=reviewed_commit,
                created_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()


async def _setup(
    db: AsyncSession,
    *,
    reviewer: User | None = None,
    reviewed_commit: str | None = None,
    with_settings: bool = True,
    with_repo: bool = True,
) -> tuple[Project, str]:
    owner = await _make_user(db, "owner")
    project = await _make_project(db, owner, "acme/widgets")
    if with_repo:
        await _make_repo(db, project, "acme/widgets", "12345")
    if with_settings:
        await _make_settings(db, owner)
    await _make_ticket_and_pr(
        db, project, owner, "acme/widgets", reviewer=reviewer, reviewed_commit=reviewed_commit
    )
    return project, "acme/widgets"


@pytest.mark.asyncio
async def test_post_gitlab_status_success_when_ready(
    db_session: AsyncSession, monkeypatch
):
    owner = await _make_user(db_session, "owner")
    reviewer = await _make_user(db_session, "reviewer")
    project = await _make_project(db_session, owner, "acme/widgets")
    await _make_repo(db_session, project, "acme/widgets", "12345")
    await _make_settings(db_session, owner)
    await _make_ticket_and_pr(
        db_session, project, owner, "acme/widgets", reviewer=reviewer, reviewed_commit=SHA
    )

    calls: list[dict] = []

    async def fake_post(self, project_id, sha, state, *, name, description, target_url=None):
        calls.append(
            {
                "project_id": project_id,
                "sha": sha,
                "state": state,
                "name": name,
                "description": description,
            }
        )
        return {"id": 1}

    monkeypatch.setattr(gc.GitLabClient, "post_commit_status", fake_post)

    await delivery_ingest.post_gitlab_status(
        db_session, project=project, repo="acme/widgets", sha=SHA
    )

    assert calls == [
        {
            "project_id": 12345,
            "sha": SHA,
            "state": "success",
            "name": "SessionFS review loop",
            "description": "All review checks passed",
        }
    ]


@pytest.mark.asyncio
async def test_post_gitlab_status_pending_when_not_ready(
    db_session: AsyncSession, monkeypatch
):
    # No trusted verdict → not ready → pending.
    project, remote = await _setup(db_session)

    calls: list[dict] = []

    async def fake_post(self, project_id, sha, state, *, name, description, target_url=None):
        calls.append({"state": state, "description": description})
        return {"id": 1}

    monkeypatch.setattr(gc.GitLabClient, "post_commit_status", fake_post)

    await delivery_ingest.post_gitlab_status(db_session, project=project, repo=remote, sha=SHA)

    assert calls == [{"state": "pending", "description": "Review in progress"}]


@pytest.mark.asyncio
async def test_post_gitlab_status_pending_when_wrong_sha(
    db_session: AsyncSession, monkeypatch
):
    reviewer = await _make_user(db_session, "reviewer")
    # Clean verdict bound to a *different* sha → not ready for the requested sha.
    project, remote = await _setup(db_session, reviewer=reviewer, reviewed_commit="b" * 40)

    calls: list[dict] = []

    async def fake_post(self, project_id, sha, state, *, name, description, target_url=None):
        calls.append({"state": state})
        return {"id": 1}

    monkeypatch.setattr(gc.GitLabClient, "post_commit_status", fake_post)

    await delivery_ingest.post_gitlab_status(db_session, project=project, repo=remote, sha=SHA)

    assert calls == [{"state": "pending"}]


@pytest.mark.asyncio
async def test_post_gitlab_status_noop_without_settings(
    db_session: AsyncSession, monkeypatch
):
    project, remote = await _setup(db_session, with_settings=False)

    calls: list[dict] = []

    async def fake_post(self, project_id, sha, state, *, name, description, target_url=None):
        calls.append({"state": state})
        return {"id": 1}

    monkeypatch.setattr(gc.GitLabClient, "post_commit_status", fake_post)

    await delivery_ingest.post_gitlab_status(db_session, project=project, repo=remote, sha=SHA)

    assert calls == []


# ── SSRF guard + no redirects (real post_commit_status) ───────────


@pytest.mark.asyncio
async def test_post_commit_status_rechecks_ssrf_and_no_redirects(monkeypatch):
    guard_calls: list[str] = []

    def fake_validate(url, *, allow_private=None):
        guard_calls.append(url)
        return url.rstrip("/")

    monkeypatch.setattr(gc, "validate_gitlab_instance_url", fake_validate)

    captured: dict = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["token"] = request.headers.get("PRIVATE-TOKEN")
        return httpx.Response(200, json={"id": 1})

    real_async_client = httpx.AsyncClient

    class RecordingClient(real_async_client):
        def __init__(self, *args, **kwargs):
            captured["client_kwargs"] = kwargs
            kwargs = dict(kwargs)
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(gc.httpx, "AsyncClient", RecordingClient)

    client = gc.GitLabClient("https://gitlab.example.com", "glpat-tok")
    await client.post_commit_status(
        42,
        "a" * 40,
        "pending",
        name="SessionFS review loop",
        description="Review in progress",
    )

    # The SSRF guard is re-checked on every request, with the stored URL.
    assert guard_calls == ["https://gitlab.example.com"]
    # Redirects are never followed.
    assert captured["client_kwargs"]["follow_redirects"] is False
    assert captured["url"] == f"https://gitlab.example.com/api/v4/projects/42/statuses/{'a' * 40}"
    assert captured["json"] == {
        "state": "pending",
        "name": "SessionFS review loop",
        "description": "Review in progress",
    }
    assert captured["token"] == "glpat-tok"

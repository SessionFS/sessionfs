"""GitHub webhook ingest: PR upsert + auto-link, release recording (track F2a).

The ``pull_request`` and ``release`` handlers map a repository to a project
through its normalized remote and record the event. These tests drive the
handlers directly (the HTTP endpoint's delivery/replay fence is covered
elsewhere) and only post through the router for the replay case.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    Project,
    ProjectRepo,
    PullRequest,
    Release,
    Ticket,
    TicketPrLink,
    User,
)
from sessionfs.server.routes import webhooks
from sessionfs.server.services import merge_checks

HEAD = "0123456789abcdef0123456789abcdef01234567"
REPO = "acme/repo"
CLONE_URL = "https://github.com/acme/repo.git"


@pytest.fixture(autouse=True)
def _signed_mode(monkeypatch):
    monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "whsec")
    monkeypatch.delenv("SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED", raising=False)


def _signed(body: bytes, event: str, delivery: str) -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "Content-Type": "application/json",
        "X-Hub-Signature-256": "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest(),
        "X-GitHub-Delivery": delivery,
    }


async def _make_user(db: AsyncSession, name: str = "owner") -> tuple[User, str]:
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
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{name}-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


async def _make_project(
    db: AsyncSession, owner: User, *, remote: str = REPO, verified: bool = True
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"ingest-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=remote,
        context_document="",
        owner_id=owner.id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    # The authoritative repo->project mapping (what _project_id_for_remote reads).
    db.add(
        ProjectRepo(
            id=f"prr_{uuid.uuid4().hex[:16]}",
            project_id=project.id,
            git_remote_normalized=remote,
            is_primary=True,
            verified=verified,
            verification_method="github_app" if verified else "owner_attested",
        )
    )
    await db.commit()
    return project


async def _make_ticket(db: AsyncSession, project: Project, owner: User) -> Ticket:
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title=f"ingest ticket {uuid.uuid4().hex[:6]}",
        description="webhook ingest coverage",
        created_by_user_id=owner.id,
        status="open",
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


def _pr_payload(ticket_id: str, *, action: str = "opened", head_sha: str = HEAD) -> dict:
    return {
        "action": action,
        "pull_request": {
            "head": {"ref": "feat/x", "sha": head_sha},
            "number": 7,
            "title": f"ship {ticket_id}",
            "body": f"closes {ticket_id}",
            "state": "open",
            "merged": False,
            "merged_at": None,
            "merge_commit_sha": None,
        },
        "repository": {"full_name": REPO, "clone_url": CLONE_URL},
        "installation": {"id": 424242},
    }


def _release_payload(tag: str, target: str) -> dict:
    return {
        "action": "published",
        "release": {
            "tag_name": tag,
            "target_commitish": target,
            "published_at": "2026-10-30T12:00:00Z",
        },
        "repository": {"full_name": REPO, "clone_url": CLONE_URL},
    }


@pytest.fixture
def pr_ingest_refresh(monkeypatch):
    """Capture merge-check refreshes so the ingest path needs no GitHub."""
    calls: list[dict] = []

    async def _fake(db, *, ticket_id=None, project_id=None, repo=None, head_sha=None):
        calls.append(dict(ticket_id=ticket_id, project_id=project_id, repo=repo, head_sha=head_sha))
        return {"repo": repo or "", "head_sha": head_sha or "", "ready": False, "posted": "skipped"}

    monkeypatch.setattr(merge_checks, "refresh_merge_check", _fake)
    return calls


# ── PR ingest ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pr_event_upserts_auto_links_and_refreshes(
    client: AsyncClient, db_session: AsyncSession, pr_ingest_refresh
):
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)

    ok = await webhooks._handle_pr_ingest(_pr_payload(ticket.id))
    assert ok is True

    pr = (
        await db_session.execute(
            select(PullRequest).where(
                PullRequest.project_id == project.id, PullRequest.repo == REPO
            )
        )
    ).scalar_one()
    assert pr.number == 7
    assert pr.head_sha == HEAD
    assert pr.branch == "feat/x"
    assert pr.state == "open"
    assert pr.title.startswith("ship ")

    links = (
        await db_session.execute(select(TicketPrLink).where(TicketPrLink.ticket_id == ticket.id))
    ).scalars().all()
    assert [link.pr_id for link in links] == [pr.id]
    assert links[0].link_source == "auto_parsed"

    # opened + linked -> a merge-check refresh was scheduled for the ticket.
    assert pr_ingest_refresh == [
        dict(ticket_id=None, project_id=project.id, repo=REPO, head_sha=HEAD)
    ]


@pytest.mark.asyncio
async def test_pr_ingest_ignores_unmapped_repo(
    client: AsyncClient, db_session: AsyncSession, pr_ingest_refresh
):
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner, remote="acme/other")
    ticket = await _make_ticket(db_session, project, owner)

    # clone_url is acme/repo, but the project maps acme/other -> ignored.
    ok = await webhooks._handle_pr_ingest(_pr_payload(ticket.id))
    assert ok is True
    rows = (await db_session.execute(select(PullRequest))).scalars().all()
    assert rows == []
    assert pr_ingest_refresh == []


@pytest.mark.asyncio
async def test_pr_ingest_ignores_unverified_repo_row(
    client: AsyncClient, db_session: AsyncSession, pr_ingest_refresh
):
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner, verified=False)
    ticket = await _make_ticket(db_session, project, owner)

    assert await webhooks._handle_pr_ingest(_pr_payload(ticket.id)) is True
    assert (await db_session.execute(select(PullRequest))).scalars().all() == []
    assert pr_ingest_refresh == []


@pytest.mark.asyncio
async def test_pr_ingest_closed_records_merged_state(
    client: AsyncClient, db_session: AsyncSession, pr_ingest_refresh
):
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)

    payload = _pr_payload(ticket.id, action="closed")
    payload["pull_request"]["merged"] = True
    payload["pull_request"]["state"] = "closed"
    payload["pull_request"]["merge_commit_sha"] = HEAD
    payload["pull_request"]["merged_at"] = "2026-10-30T11:00:00Z"

    ok = await webhooks._handle_pr_ingest(payload)
    assert ok is True
    pr = (
        await db_session.execute(
            select(PullRequest).where(
                PullRequest.project_id == project.id, PullRequest.repo == REPO
            )
        )
    ).scalar_one()
    assert pr.state == "merged"
    assert pr.merge_commit_sha == HEAD
    assert pr.merged_at is not None


@pytest.mark.asyncio
async def test_pr_ingest_stale_delivery_does_not_rewind(
    client: AsyncClient, db_session: AsyncSession, pr_ingest_refresh
):
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)

    fresh = _pr_payload(ticket.id, action="synchronize", head_sha="b" * 40)
    fresh["pull_request"]["updated_at"] = "2026-10-30T12:00:00Z"
    assert await webhooks._handle_pr_ingest(fresh) is True

    # An older delivery arriving late must not move head_sha backwards.
    stale = _pr_payload(ticket.id, action="opened", head_sha="a" * 40)
    stale["pull_request"]["updated_at"] = "2026-10-30T10:00:00Z"
    assert await webhooks._handle_pr_ingest(stale) is True

    pr = (
        await db_session.execute(
            select(PullRequest).where(
                PullRequest.project_id == project.id, PullRequest.repo == REPO.lower()
            )
        )
    ).scalar_one()
    await db_session.refresh(pr)
    assert pr.head_sha == "b" * 40


# ── release ingest ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_event_records_release_with_sha(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    ok = await webhooks._handle_release_event(_release_payload("v1.2.3", HEAD))
    assert ok is True
    release = (
        await db_session.execute(
            select(Release).where(Release.project_id == project.id, Release.version == "v1.2.3")
        )
    ).scalar_one()
    assert release.tag_sha == HEAD
    assert release.source == "github_release"
    assert release.released_at is not None


@pytest.mark.asyncio
async def test_release_event_target_branch_leaves_tag_sha_empty(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    ok = await webhooks._handle_release_event(_release_payload("v2.0.0", "main"))
    assert ok is True
    release = (
        await db_session.execute(
            select(Release).where(Release.project_id == project.id, Release.version == "v2.0.0")
        )
    ).scalar_one()
    assert release.tag_sha == ""  # branch target -> no resolvable tag sha
    assert release.source == "github_release"


@pytest.mark.asyncio
async def test_release_event_ignores_unmapped_repo(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session)
    await _make_project(db_session, owner, remote="acme/other")

    ok = await webhooks._handle_release_event(_release_payload("v9.9.9", HEAD))
    assert ok is True
    rows = (await db_session.execute(select(Release))).scalars().all()
    assert rows == []


# ── replay fence over the full HTTP path ──────────────────────────


@pytest.mark.asyncio
async def test_replayed_pr_delivery_processed_once(
    client: AsyncClient, db_session: AsyncSession, pr_ingest_refresh, monkeypatch
):
    # Keep the session-comment pipeline out of scope for this test.
    async def _no_op_pr(payload, fence=None):
        return True

    monkeypatch.setattr(webhooks, "_handle_pr_event", _no_op_pr)

    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)

    body = json.dumps(_pr_payload(ticket.id)).encode()
    first = await client.post(
        "/webhooks/github", content=body, headers=_signed(body, "pull_request", "d-1")
    )
    assert first.json()["status"] == "ok"
    # The pull_request path runs in a background task; let it finish.
    for _ in range(100):
        if pr_ingest_refresh:
            break
        await asyncio.sleep(0.01)

    replay = await client.post(
        "/webhooks/github", content=body, headers=_signed(body, "pull_request", "d-2")
    )
    assert replay.json()["status"] == "duplicate"

    assert len(pr_ingest_refresh) == 1
    prs = (
        await db_session.execute(
            select(PullRequest).where(PullRequest.project_id == project.id)
        )
    ).scalars().all()
    assert len(prs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action,comments", [
    ("opened", True), ("synchronize", True), ("reopened", True),
    ("edited", False), ("closed", False),
])
async def test_pr_dispatch_runs_comments_only_for_code_changes(
    monkeypatch, action, comments
):
    seen: dict[str, int] = {"comment": 0, "ingest": 0}

    async def _spy_comment(payload, fence=None):
        seen["comment"] += 1
        return True

    async def _spy_ingest(payload, fence=None):
        seen["ingest"] += 1
        return True

    async def _finish(*args, **kwargs):
        return True

    monkeypatch.setattr(webhooks, "_handle_pr_event", _spy_comment)
    monkeypatch.setattr(webhooks, "_handle_pr_ingest", _spy_ingest)
    monkeypatch.setattr(webhooks, "_finish_delivery", _finish)

    payload = _pr_payload("tk_x", action=action)
    await webhooks._process_delivery("pull_request", payload, "fp", 1)

    assert seen["ingest"] == 1
    assert seen["comment"] == (1 if comments else 0)

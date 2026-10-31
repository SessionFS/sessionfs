"""GitHub merge check service + trigger endpoint (Review Loop R3, track F2a).

The service reuses ``compute_merge_readiness`` (the same function the
``GET …/readiness`` route uses) and posts one "SessionFS review loop" check
run per ``(repo, head_sha)``. These tests drive the GitHub API through the
``merge_checks._http_transport`` seam (``httpx.MockTransport``) and stub
``get_installation_token`` / ``_find_installation_repo`` so no network is
touched. Coverage:

- success ONLY when ready for the exact head commit;
- a new head sha gets its own run and never flips an older one green;
- the not-ready summary carries no email or dollar figure;
- a 403 from the check-run API degrades to the PR-comment path;
- the trigger endpoint's authz matrix (editor / reader / outsider / scope).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ProjectRepo,
    ApiKey,
    GitHubInstallation,
    Organization,
    OrgMember,
    Project,
    PullRequest,
    Session,
    Ticket,
    TicketComment,
    TicketPrLink,
    User,
    WorkQueue,
    WorkQueueItem,
)
from sessionfs.server.services import merge_checks

HEAD = "0123456789abcdef0123456789abcdef01234567"
OTHER = "89abcdef0123456789abcdef0123456789abcdef"
REPO = "acme/repo"
CHECK_NAME = "SessionFS review loop"


# ── helpers ────────────────────────────────────────────────────────


async def _make_user(
    db: AsyncSession, name: str = "owner", tier: str = "team"
) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier=tier,
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


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_org(db: AsyncSession) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name=f"Org-{uuid.uuid4().hex[:6]}",
        slug=f"o-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


async def _add_org_member(db: AsyncSession, org: Organization, user: User, role: str) -> None:
    db.add(
        OrgMember(
            org_id=org.id, user_id=user.id, role=role,
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def _make_project(
    db: AsyncSession, owner: User, org: Organization | None = None
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"mc-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/mc-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_ticket(db: AsyncSession, project: Project, owner: User) -> Ticket:
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title=f"mc ticket {uuid.uuid4().hex[:6]}",
        description="merge check coverage",
        created_by_user_id=owner.id,
        status="open",
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


async def _seed_clean_verdict(
    db: AsyncSession,
    ticket_id: str,
    reviewer: User,
    *,
    reviewed_commit: str,
    content: str = "Codex R1 review on tk_x: VERIFIED-CLEAN\n\nFindings: none.",
) -> TicketComment:
    c = TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket_id,
        author_user_id=reviewer.id,
        author_persona="codex-reviewer",
        content=content,
        verdict_trusted=True,
        reviewed_commit=reviewed_commit,
        created_at=datetime.now(timezone.utc),
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


async def _seed_implementer(db: AsyncSession, project: Project, ticket_id: str, user_id: str) -> None:
    queue = WorkQueue(
        id=f"wq_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name=f"q-{uuid.uuid4().hex[:6]}",
        mode="implement_until_done",
        selector="{}",
        created_by_user_id=project.owner_id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(queue)
    await db.flush()
    db.add(
        WorkQueueItem(
            id=f"wqi_{uuid.uuid4().hex[:16]}",
            work_queue_id=queue.id,
            ticket_id=ticket_id,
            item_status="active",
            implementer_user_id=user_id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def _seed_pr(
    db: AsyncSession, project: Project, ticket: Ticket, *, head_sha: str, number: int = 1,
    verified_repo: bool = True,
) -> PullRequest:
    if verified_repo:
        existing = (
            await db.execute(
                select(ProjectRepo).where(ProjectRepo.project_id == project.id)
            )
        ).scalars().first()
        if existing is None:
            db.add(
                ProjectRepo(
                    id=f"prr_{uuid.uuid4().hex[:16]}",
                    project_id=project.id,
                    git_remote_normalized=REPO,
                    is_primary=True,
                    verified=True,
                    verification_method="github_app",
                )
            )
    pr = PullRequest(
        id=f"pr_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        provider="github",
        repo=REPO,
        number=number,
        title="ship it",
        branch="feat/x",
        head_sha=head_sha,
    )
    db.add(pr)
    await db.flush()
    db.add(TicketPrLink(ticket_id=ticket.id, pr_id=pr.id, link_source="auto_parsed"))
    await db.commit()
    await db.refresh(pr)
    return pr


async def _seed_installation(db: AsyncSession, owner: User, login: str = "acme") -> GitHubInstallation:
    inst = GitHubInstallation(
        id=424242,
        user_id=owner.id,
        account_login=login,
        account_type="Organization",
        auto_comment=True,
    )
    db.add(inst)
    await db.commit()
    await db.refresh(inst)
    return inst


class _CheckRuns:
    """Stateful in-memory GitHub check-runs API (list/create/update)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.runs_by_sha: dict[str, list[dict]] = {}
        self.next_id = 1000
        self.list_status = 200

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and "/commits/" in path and path.endswith("/check-runs"):
            if self.list_status != 200:
                return httpx.Response(self.list_status, json={})
            sha = path.split("/commits/")[1].split("/check-runs")[0]
            return httpx.Response(200, json={"check_runs": self.runs_by_sha.get(sha, [])})
        if request.method == "POST" and path.endswith("/check-runs"):
            body = json.loads(request.content)
            self.next_id += 1
            run = {
                "id": self.next_id,
                "name": body["name"],
                "head_sha": body["head_sha"],
                "conclusion": body["conclusion"],
                "output": body.get("output", {}),
            }
            self.runs_by_sha.setdefault(body["head_sha"], []).append(run)
            return httpx.Response(201, json={"id": self.next_id})
        if request.method == "PATCH" and "/check-runs/" in path:
            run_id = int(path.rstrip("/").split("/")[-1])
            body = json.loads(request.content)
            for runs in self.runs_by_sha.values():
                for run in runs:
                    if run["id"] == run_id:
                        run["conclusion"] = body.get("conclusion", run["conclusion"])
                        run["output"] = body.get("output", run["output"])
            return httpx.Response(200, json={"id": run_id})
        return httpx.Response(404, json={})


@pytest.fixture
def gh(monkeypatch) -> _CheckRuns:
    """Wire the check-runs API through a stateful mock; stub token + grant."""
    fake = _CheckRuns()

    async def _token(installation_id: int) -> str:
        return "tok"

    async def _grant(token: str, full_name: str) -> dict:
        return {"full_name": full_name}

    monkeypatch.setattr(merge_checks, "_http_transport", fake.transport())
    monkeypatch.setattr(merge_checks, "get_installation_token", _token)
    monkeypatch.setattr(merge_checks, "_find_installation_repo", _grant)
    return fake


async def _ready_ticket(
    db: AsyncSession, *, head_sha: str = HEAD
) -> tuple[User, Project, Ticket]:
    """A ticket whose trusted clean verdict (different reviewer) is ready."""
    owner, _ = await _make_user(db)
    project = await _make_project(db, owner)
    ticket = await _make_ticket(db, project, owner)
    reviewer, _ = await _make_user(db, name="reviewer")
    await _seed_clean_verdict(db, ticket.id, reviewer, reviewed_commit=head_sha)
    await _seed_implementer(db, project, ticket.id, owner.id)
    await _seed_installation(db, owner)
    await _seed_pr(db, project, ticket, head_sha=head_sha)
    return owner, project, ticket


# ── service: check-run outcome ─────────────────────────────────────


@pytest.mark.asyncio
async def test_success_only_when_ready_for_that_sha(
    client: AsyncClient, db_session: AsyncSession, gh: _CheckRuns
):
    owner, project, ticket = await _ready_ticket(db_session)

    result = await merge_checks.refresh_merge_check(
        db_session, ticket_id=ticket.id, project_id=project.id
    )
    assert result["ready"] is True
    assert result["posted"] == "check_run"
    # A create (POST) — no prior run for this commit — with conclusion success.
    posts = [r for r in gh.requests if r.method == "POST" and r.url.path.endswith("/check-runs")]
    assert len(posts) == 1
    body = json.loads(posts[0].content)
    assert body["conclusion"] == "success"
    assert body["head_sha"] == HEAD


@pytest.mark.asyncio
async def test_not_ready_when_verdict_bound_elsewhere(
    client: AsyncClient, db_session: AsyncSession, gh: _CheckRuns
):
    # The clean verdict is bound to a DIFFERENT commit than the PR head.
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    reviewer, _ = await _make_user(db_session, name="reviewer")
    await _seed_clean_verdict(db_session, ticket.id, reviewer, reviewed_commit=OTHER)
    await _seed_implementer(db_session, project, ticket.id, owner.id)
    await _seed_installation(db_session, owner)
    await _seed_pr(db_session, project, ticket, head_sha=HEAD)

    result = await merge_checks.refresh_merge_check(
        db_session, ticket_id=ticket.id, project_id=project.id
    )
    assert result["ready"] is False
    posts = [r for r in gh.requests if r.method == "POST" and r.url.path.endswith("/check-runs")]
    assert json.loads(posts[0].content)["conclusion"] == "action_required"


@pytest.mark.asyncio
async def test_no_verdict_is_neutral(
    client: AsyncClient, db_session: AsyncSession, gh: _CheckRuns
):
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    await _seed_installation(db_session, owner)
    await _seed_pr(db_session, project, ticket, head_sha=HEAD)

    result = await merge_checks.refresh_merge_check(
        db_session, ticket_id=ticket.id, project_id=project.id
    )
    assert result["ready"] is False
    posts = [r for r in gh.requests if r.method == "POST" and r.url.path.endswith("/check-runs")]
    assert json.loads(posts[0].content)["conclusion"] == "neutral"


@pytest.mark.asyncio
async def test_new_sha_gets_its_own_run_and_never_flips_old_one(
    client: AsyncClient, db_session: AsyncSession, gh: _CheckRuns
):
    owner, project, ticket = await _ready_ticket(db_session, head_sha=HEAD)

    await merge_checks.refresh_merge_check(db_session, ticket_id=ticket.id, project_id=project.id)
    first = gh.runs_by_sha[HEAD][0]
    assert first["conclusion"] == "success"

    # The head moves to a new commit the clean verdict is NOT bound to, so the
    # ticket is no longer ready. The old run must stay green and untouched.
    from sqlalchemy import select

    pr = (
        await db_session.execute(
            select(PullRequest).where(
                PullRequest.project_id == project.id, PullRequest.head_sha == HEAD
            )
        )
    ).scalar_one()
    pr.head_sha = OTHER
    await db_session.commit()

    result = await merge_checks.refresh_merge_check(
        db_session, ticket_id=ticket.id, project_id=project.id
    )
    assert result["ready"] is False
    # A new run is created for OTHER (list for OTHER found nothing) ...
    assert OTHER in gh.runs_by_sha
    # ... and the HEAD run was never PATCHed back to anything.
    assert first["conclusion"] == "success"
    patches = [r for r in gh.requests if r.method == "PATCH"]
    assert patches == []  # the old run id was never updated


@pytest.mark.asyncio
async def test_not_ready_summary_has_no_email_or_dollar(
    client: AsyncClient, db_session: AsyncSession, gh: _CheckRuns
):
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    reviewer, _ = await _make_user(db_session, name="reviewer")
    # The comment carries a real email + dollar figure and a HIGH finding —
    # none of it may leak into the public check-run summary.
    await _seed_clean_verdict(
        db_session,
        ticket.id,
        reviewer,
        reviewed_commit=HEAD,
        content=(
            "Codex R1 review on tk_x: VERIFIED-CLEAN\n\n"
            "Contact ops@example.com for budget questions.\n\n"
            "Findings:\n\n • HIGH — residual crash risk in src/foo.py:10\n"
        ),
    )
    await _seed_implementation_and_pr(db_session, project, ticket, owner)

    await merge_checks.refresh_merge_check(db_session, ticket_id=ticket.id, project_id=project.id)
    posts = [r for r in gh.requests if r.method == "POST" and r.url.path.endswith("/check-runs")]
    summary = json.loads(posts[0].content)["output"]["summary"]
    assert "@" not in summary
    assert "$" not in summary
    assert "Not ready to merge." in summary


async def _seed_implementation_and_pr(
    db: AsyncSession, project: Project, ticket: Ticket, owner: User
) -> None:
    await _seed_implementer(db, project, ticket.id, owner.id)
    await _seed_installation(db, owner)
    await _seed_pr(db, project, ticket, head_sha=HEAD)


@pytest.mark.asyncio
async def test_403_degrades_to_comment(
    client: AsyncClient, db_session: AsyncSession, gh: _CheckRuns, monkeypatch
):
    owner, project, ticket = await _ready_ticket(db_session)
    gh.list_status = 403  # checks:write not granted

    captured: dict = {}

    async def _fake_comment(db, token, installation_id, owner_user_id, repo, pr_number, summary):
        captured.update(
            dict(repo=repo, pr_number=pr_number, summary=summary, owner_user_id=owner_user_id)
        )
        return True

    monkeypatch.setattr(merge_checks, "_post_comment", _fake_comment)

    result = await merge_checks.refresh_merge_check(
        db_session, ticket_id=ticket.id, project_id=project.id
    )
    assert result["posted"] == "comment"
    assert captured["repo"] == REPO
    assert captured["pr_number"] == 1
    assert captured["owner_user_id"] == owner.id
    assert "Ready to merge." in captured["summary"]


# ── endpoint authz ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_endpoint_editor_succeeds(
    client: AsyncClient, db_session: AsyncSession, gh: _CheckRuns
):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/merge-check",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["posted"] == "skipped"  # no linked PR — nothing to post


@pytest.mark.asyncio
async def test_endpoint_captured_reader_forbidden(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)

    reader, reader_key = await _make_user(db_session, name="reader")
    # A captured session grants read access but not editor standing.
    db_session.add(
        Session(
            id=f"ses_{uuid.uuid4().hex[:16]}",
            user_id=reader.id,
            source_tool="claude-code",
            blob_key="k",
            etag="e",
            project_id=project.id,
            message_count=1,
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/merge-check",
        headers=_hdrs(reader_key),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_endpoint_outsider_404(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)

    outsider, outsider_key = await _make_user(db_session, name="outsider")
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/merge-check",
        headers=_hdrs(outsider_key),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_endpoint_service_key_scope(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session)
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, admin, "admin")
    project = await _make_project(db_session, admin, org=org)
    ticket = await _make_ticket(db_session, project, admin)

    # loops:write + org match → 200.
    write_key = await _make_service_key(db_session, org, admin, ["loops:write"])
    ok = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/merge-check",
        headers=_hdrs(write_key),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["posted"] == "skipped"

    # A different scope is refused before the project is even read.
    no_scope = await _make_service_key(db_session, org, admin, ["tickets:read"])
    denied = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/merge-check",
        headers=_hdrs(no_scope),
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["details"]["error"] == "insufficient_scope"


async def _make_service_key(
    db: AsyncSession, org: Organization, minter: User, scopes: list[str]
) -> str:
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=minter.id,
            key_hash=hash_api_key(raw),
            name=f"svc-{uuid.uuid4().hex[:6]}",
            is_active=True,
            key_kind="service",
            org_id=org.id,
            scopes=json.dumps(scopes),
            created_by_user_id=minter.id,
            service_key_name=f"svc-{uuid.uuid4().hex[:6]}",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return raw


@pytest.mark.asyncio
async def test_unverified_repo_row_skips_check_run(
    client: AsyncClient, db_session: AsyncSession, gh: _CheckRuns
):
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    ticket = await _make_ticket(db_session, project, owner)
    reviewer, _ = await _make_user(db_session, name="reviewer")
    await _seed_clean_verdict(db_session, ticket.id, reviewer, reviewed_commit=HEAD)
    await _seed_implementer(db_session, project, ticket.id, owner.id)
    await _seed_installation(db_session, owner)
    await _seed_pr(db_session, project, ticket, head_sha=HEAD, verified_repo=False)
    db_session.add(
        ProjectRepo(
            id=f"prr_{uuid.uuid4().hex[:16]}",
            project_id=project.id,
            git_remote_normalized=REPO,
            verified=False,
            verification_method="owner_attested",
        )
    )
    await db_session.commit()

    result = await merge_checks.refresh_merge_check(
        db_session, ticket_id=ticket.id, project_id=project.id
    )
    assert result["posted"] == "skipped"
    assert result["reason"] == "repo_not_verified"
    assert not [r for r in gh.requests if r.method == "POST"]


@pytest.mark.asyncio
async def test_shared_run_requires_every_linked_ticket_ready(
    client: AsyncClient, db_session: AsyncSession, gh: _CheckRuns
):
    owner, project, ready_ticket = await _ready_ticket(db_session)
    pr = (
        await db_session.execute(
            select(PullRequest).where(PullRequest.project_id == project.id)
        )
    ).scalar_one()
    other = await _make_ticket(db_session, project, owner)  # no verdict
    db_session.add(TicketPrLink(ticket_id=other.id, pr_id=pr.id, link_source="manual"))
    await db_session.commit()

    # Evaluating via the ready ticket must NOT turn the shared run green.
    result = await merge_checks.refresh_merge_check(
        db_session, ticket_id=ready_ticket.id, project_id=project.id
    )
    assert result["ready"] is False
    posts = [r for r in gh.requests if r.method == "POST" and r.url.path.endswith("/check-runs")]
    body = json.loads(posts[-1].content)
    assert body["conclusion"] != "success"
    assert other.id in body["output"]["summary"]
    assert ready_ticket.id not in body["output"]["summary"]


@pytest.mark.asyncio
async def test_second_pr_row_on_same_commit_cannot_hide_a_ticket(
    client: AsyncClient, db_session: AsyncSession, gh: _CheckRuns
):
    """A fork PR (or a manual upsert) on the same head commit that links only
    the ready ticket must not turn the commit's check green while another
    ticket linked to the real PR at that commit is not ready."""
    owner, project, ready_ticket = await _ready_ticket(db_session)
    real_pr = (
        await db_session.execute(
            select(PullRequest).where(PullRequest.project_id == project.id)
        )
    ).scalar_one()
    other = await _make_ticket(db_session, project, owner)  # no verdict
    db_session.add(TicketPrLink(ticket_id=other.id, pr_id=real_pr.id, link_source="manual"))
    await db_session.commit()
    # The attacker's PR row: same head sha, newer, links only the ready ticket.
    await _seed_pr(db_session, project, ready_ticket, head_sha=HEAD, number=99)

    # Webhook target path (repo + sha).
    result = await merge_checks.refresh_merge_check(
        db_session, project_id=project.id, repo=real_pr.repo, head_sha=HEAD
    )
    assert result["ready"] is False
    # Ticket path, via the ready ticket.
    result = await merge_checks.refresh_merge_check(
        db_session, ticket_id=ready_ticket.id, project_id=project.id
    )
    assert result["ready"] is False
    for runs in gh.runs_by_sha.values():
        for run in runs:
            assert run["conclusion"] != "success"


@pytest.mark.asyncio
async def test_non_sha_head_is_never_sent_to_github(
    client: AsyncClient, db_session: AsyncSession, gh: _CheckRuns
):
    owner, project, ticket = await _ready_ticket(db_session)
    result = await merge_checks.refresh_merge_check(
        db_session,
        project_id=project.id,
        repo="acme/widgets",
        head_sha="../../../../orgs/acme/members?x=",
    )
    assert result["posted"] == "skipped"
    assert gh.requests == []

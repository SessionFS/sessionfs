"""GitHub merge check + re-evaluation hook (Review Loop R3, track F2a).

``refresh_merge_check`` posts a single "SessionFS review loop" check run on the
exact head commit a ticket's trusted verdict is bound to. Readiness reuses
``approvals.ticket_readiness`` (the same function the ``GET …/readiness``
route uses: ``review_state.compute_merge_readiness`` plus approval policies),
so the two surfaces can never drift:

- a check run is looked up by ``(repo, head_sha, name)`` — a new head commit
  gets its own run, and an older run is never flipped green after the fact;
- ``success`` only when ready for that exact commit; a ticket with a trusted
  verdict that is not yet ready gets ``action_required``; one with no verdict
  yet gets ``neutral``;
- installations without ``checks:write`` degrade to the existing PR-comment
  machinery with the same summary.

All outbound calls are bounded (timeout, no redirects); errors are logged
without tokens and never raised to the caller.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional
from urllib.parse import quote

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    GitHubInstallation,
    Project,
    PullRequest,
    Ticket,
    TicketPrLink,
)
from sessionfs.server.github_app import (
    _find_installation_repo,
    get_installation_token,
)
from sessionfs.server.services.merge_check_order import (
    REPAIR_ROUNDS,
    PostGuard,
    take_eval_seq,
)

_FULL_SHA_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")

logger = logging.getLogger("sessionfs.merge_checks")

_CHECK_RUN_NAME = "SessionFS review loop"
_CHECK_RUN_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# Test seam: a httpx.MockTransport to route check-run API calls through.
_http_transport: Optional[httpx.BaseTransport] = None

def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_run_summary(ready: bool, reasons: list[str]) -> str:
    """The check-run/comment summary. Built only from fixed boilerplate and the
    readiness reasons (never emails or dollar figures)."""
    if ready:
        return "Ready to merge. A human must still approve the merge."
    lines = ["Not ready to merge.", ""]
    lines.extend(f"- {reason}" for reason in reasons)
    lines.append("")
    lines.append("A human must still approve the merge.")
    return "\n".join(lines)


def _check_run_conclusion(ready: bool, verdict: Optional[dict]) -> str:
    if ready:
        return "success"
    if verdict is not None:
        return "action_required"
    return "neutral"


# Bounded retry for check-run calls: up to 3 attempts with these waits
# (seconds) between them, only on a transport error or a 5xx response; a 4xx
# is final. The caller has already committed (no transaction is open while
# waiting). Tests shrink these.
_GITHUB_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.5)


async def _github_request(
    method: str, url: str, *, token: str, json_body: Optional[dict] = None
) -> Optional[httpx.Response]:
    """A bounded GitHub API call with a short retry on transport errors and
    5xx responses. Returns the last response, or None when every attempt hit
    a transport error."""
    attempts = len(_GITHUB_RETRY_DELAYS) + 1
    resp: Optional[httpx.Response] = None
    for attempt in range(attempts):
        resp = await _github_request_once(method, url, token=token, json_body=json_body)
        if resp is not None and resp.status_code < 500:
            return resp
        if attempt < attempts - 1:
            await asyncio.sleep(_GITHUB_RETRY_DELAYS[attempt])
    return resp


async def _github_request_once(
    method: str, url: str, *, token: str, json_body: Optional[dict] = None
) -> Optional[httpx.Response]:
    """One bounded GitHub API call. Returns None on any transport error."""
    client_kwargs: dict[str, Any] = {}
    if _http_transport is not None:
        client_kwargs["transport"] = _http_transport
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sessionfs",
    }
    try:
        async with httpx.AsyncClient(
            timeout=_CHECK_RUN_TIMEOUT, follow_redirects=False, **client_kwargs
        ) as client:
            return await client.request(method, url, headers=headers, json=json_body)
    except (httpx.HTTPError, ValueError):
        return None


def _check_run_status(resp: Optional[httpx.Response]) -> str:
    """Disposition of a check-run API response."""
    if resp is None:
        return "unavailable"
    if resp.status_code in (200, 201):
        return "check_run"
    if resp.status_code == 403:
        return "forbidden"
    if resp.status_code == 404:
        return "not_found"
    return "unavailable"


async def _post_or_update_check_run(
    token: str,
    repo: str,
    head_sha: str,
    conclusion: str,
    summary: str,
    *,
    guard: Optional[PostGuard] = None,
) -> str:
    """Create or update the check run for one ``(repo, head_sha, name)``.

    With a ``guard`` the post is ordered (services/merge_check_order.py): it
    is skipped (``superseded``) when a newer evaluation of the commit already
    claimed the post, and after a successful write the newest claimed state
    is re-sent if a newer evaluation claimed while this one was in flight.

    Returns ``check_run`` on success, or ``forbidden`` (no checks:write),
    ``not_found``, ``unavailable`` or ``superseded``.
    """
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name:
        return "unavailable"
    api = f"https://api.github.com/repos/{owner}/{name}"
    if guard is not None and not await guard.claim(conclusion, summary):
        return "superseded"
    status = await _write_check_run(token, api, head_sha, conclusion, summary)
    if status == "check_run" and guard is not None:
        written = guard.seq
        for _ in range(REPAIR_ROUNDS):
            newest = await guard.newest()
            if newest is None or newest[0] <= written:
                break
            written, newest_conclusion, newest_summary = newest
            if await _write_check_run(
                token, api, head_sha, newest_conclusion, newest_summary
            ) != "check_run":
                break
    return status


async def _write_check_run(
    token: str, api: str, head_sha: str, conclusion: str, summary: str
) -> str:
    """The unordered create-or-update of one check run (no DB access)."""
    # Look up an existing run for THIS commit only. A new head sha therefore
    # gets its own run and an older run is never touched.
    run_id: Optional[int] = None
    resp = await _github_request(
        "GET",
        f"{api}/commits/{head_sha}/check-runs?check_name={quote(_CHECK_RUN_NAME)}",
        token=token,
    )
    status = _check_run_status(resp)
    if status != "check_run":
        return status
    if resp is not None:
        try:
            data = resp.json()
            runs = data.get("check_runs") if isinstance(data, dict) else None
            if isinstance(runs, list):
                for run in runs:
                    if (
                        isinstance(run, dict)
                        and run.get("name") == _CHECK_RUN_NAME
                        and _is_int(run.get("id"))
                    ):
                        run_id = run["id"]
                        break
        except ValueError:
            pass

    body: dict[str, Any] = {
        "name": _CHECK_RUN_NAME,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": conclusion,
        "output": {"title": _CHECK_RUN_NAME, "summary": summary},
    }
    if run_id is None:
        return _check_run_status(
            await _github_request("POST", f"{api}/check-runs", token=token, json_body=body)
        )
    return _check_run_status(
        await _github_request(
            "PATCH",
            f"{api}/check-runs/{run_id}",
            token=token,
            json_body={"conclusion": conclusion, "output": {"title": _CHECK_RUN_NAME, "summary": summary}},
        )
    )


# ── Target resolution ──────────────────────────────────────────────


async def _linked_prs(
    db: AsyncSession, ticket_id: str, project_id: Optional[str] = None,
    repo: Optional[str] = None,
) -> list[PullRequest]:
    stmt = (
        select(PullRequest)
        .join(TicketPrLink, TicketPrLink.pr_id == PullRequest.id)
        .where(TicketPrLink.ticket_id == ticket_id)
        .order_by(PullRequest.updated_at.desc(), PullRequest.id.desc())
    )
    if project_id is not None:
        stmt = stmt.where(PullRequest.project_id == project_id)
    if repo is not None:
        stmt = stmt.where(PullRequest.repo == repo)
    return list((await db.execute(stmt)).scalars().all())


async def _pr_by_repo_sha(
    db: AsyncSession, project_id: str, repo: str, head_sha: str
) -> Optional[PullRequest]:
    return (
        await db.execute(
            select(PullRequest)
            .where(
                PullRequest.project_id == project_id,
                PullRequest.repo == repo,
                PullRequest.head_sha == head_sha,
            )
            .order_by(PullRequest.updated_at.desc(), PullRequest.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _tickets_for_commit(
    db: AsyncSession,
    project_id: str,
    repo: str,
    head_sha: str,
    extra: Optional[list[Ticket]] = None,
) -> list[Ticket]:
    """Every ticket linked to ANY PR row in the project at ``(repo, head_sha)``.

    The check run belongs to the commit, not to one PR row, so readiness is
    computed over the union: another PR row on the same head commit (a fork
    PR, a manual upsert) can only add tickets to the set, never hide one.
    """
    ids = set(
        (
            await db.execute(
                select(TicketPrLink.ticket_id)
                .join(PullRequest, PullRequest.id == TicketPrLink.pr_id)
                .where(
                    PullRequest.project_id == project_id,
                    PullRequest.repo == repo,
                    PullRequest.head_sha == head_sha,
                )
            )
        ).scalars().all()
    )
    ids.update(t.id for t in (extra or []))
    if not ids:
        return []
    rows = (
        await db.execute(
            select(Ticket)
            .where(Ticket.id.in_(sorted(ids)), Ticket.project_id == project_id)
            .order_by(Ticket.id)
        )
    ).scalars().all()
    return list(rows)


async def _repo_verified(db: AsyncSession, project_id: str, repo: str) -> bool:
    """True only when the project holds a GitHub-App-verified repo row for the
    remote. Unverified / legacy rows never qualify."""
    from sessionfs.git_remote import normalize_git_remote
    from sessionfs.server.db.models import ProjectRepo

    normalized = normalize_git_remote(f"https://github.com/{repo}.git")
    row = (
        await db.execute(
            select(ProjectRepo.id)
            .where(
                ProjectRepo.project_id == project_id,
                ProjectRepo.git_remote_normalized == normalized,
                ProjectRepo.verified.is_(True),
                ProjectRepo.verification_method == "github_app",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def _compute_readiness(
    db: AsyncSession, ticket: Ticket, head_sha: str
) -> dict[str, Any]:
    # The same function the readiness route uses: pure review readiness plus
    # approval policies (a pending blocking approval keeps the check not green).
    # A read: requests are raised by the trigger before the check is posted.
    from sessionfs.server.services.approvals import ticket_readiness

    return await ticket_readiness(db, ticket, head_sha)


# ── Approval requests (raised on triggers, never on reads) ─────────


async def raise_approvals_for_ticket(
    db: AsyncSession, ticket_id: str, *, project_id: Optional[str] = None
) -> None:
    """Raise the merge approval requests ``ticket_id`` needs (write path:
    a verdict, an approval decision, a policy recheck, the merge-check
    trigger). Runs before, and independently of, the GitHub/GitLab posting
    gates, so an unverified repo or a missing integration never stops a
    request from being raised. The caller commits."""
    from sessionfs.server.services.approvals import raise_ticket_approvals

    stmt = select(Ticket).where(Ticket.id == ticket_id)
    if project_id is not None:
        stmt = stmt.where(Ticket.project_id == project_id)
    ticket = (await db.execute(stmt)).scalar_one_or_none()
    if ticket is not None:
        await raise_ticket_approvals(db, ticket)


async def raise_approvals_for_commit(
    db: AsyncSession, project_id: str, repo: str, head_sha: Optional[str]
) -> None:
    """Raise approval requests for every ticket linked to a PR/MR row at
    ``(repo, head_sha)`` in the project (PR/MR webhook ingest). The caller
    commits."""
    if not head_sha:
        return
    from sessionfs.server.services.approvals import raise_ticket_approvals

    for ticket in await _tickets_for_commit(db, project_id, repo, head_sha):
        await raise_ticket_approvals(db, ticket)


def _skipped(repo: str, head_sha: str, ready: bool, reason: str) -> dict[str, Any]:
    return {
        "repo": repo or "",
        "head_sha": head_sha or "",
        "ready": ready,
        "posted": "skipped",
        "reason": reason,
    }


async def refresh_merge_check(
    db: AsyncSession,
    *,
    ticket_id: Optional[str] = None,
    project_id: Optional[str] = None,
    repo: Optional[str] = None,
    head_sha: Optional[str] = None,
) -> dict[str, Any]:
    """Compute readiness for a PR's head commit and post the check run.

    The check run is shared by every ticket linked to the PR, so readiness is
    evaluated for ALL of them: success only when each is ready. Accepts either
    ``ticket_id`` (resolving the PR(s) it is linked to) or an explicit
    ``(project_id, repo, head_sha)`` target. ``repo``/``head_sha`` override the
    linked-PR values when both are supplied (the webhook path). Never raises to
    the caller; a miss is reported as ``posted: "skipped"``. Returns the result
    for the most recently updated PR.
    """
    if ticket_id is not None:
        stmt = select(Ticket).where(Ticket.id == ticket_id)
        if project_id is not None:
            stmt = stmt.where(Ticket.project_id == project_id)
        ticket = (await db.execute(stmt)).scalar_one_or_none()
        if ticket is None:
            return _skipped(repo or "", head_sha or "", False, "ticket not found")
        project_id = ticket.project_id
        prs = await _linked_prs(db, ticket.id, project_id, repo)
        if not prs:
            # No linked PR row: evaluate this ticket alone against the target.
            if not (repo and head_sha):
                return _skipped(repo or "", head_sha or "", False, "no head commit available")
            tickets = await _tickets_for_commit(
                db, project_id, repo, head_sha, extra=[ticket]
            )
            return await _refresh_for_target(db, project_id, repo, head_sha, None, tickets)
        first: Optional[dict[str, Any]] = None
        for pr in prs:
            sha = head_sha or pr.head_sha
            if not sha:
                result = _skipped(pr.repo, "", False, "no head commit available")
            else:
                tickets = await _tickets_for_commit(
                    db, project_id, pr.repo, sha, extra=[ticket]
                )
                result = await _refresh_for_target(
                    db, project_id, pr.repo, sha, pr, tickets
                )
            if first is None:
                first = result
        assert first is not None
        return first

    if not (project_id and repo and head_sha):
        return _skipped(repo or "", head_sha or "", False, "no commit target")
    pr = await _pr_by_repo_sha(db, project_id, repo, head_sha)
    if pr is None:
        return _skipped(repo, head_sha, False, "pull request not found")
    tickets = await _tickets_for_commit(db, project_id, repo, head_sha)
    if not tickets:
        return _skipped(repo, head_sha, False, "no ticket linked to this commit")
    return await _refresh_for_target(db, project_id, repo, head_sha, pr, tickets)


async def _refresh_for_target(
    db: AsyncSession,
    project_id: str,
    repo: str,
    head_sha: str,
    pr: Optional[PullRequest],
    tickets: list[Ticket],
) -> dict[str, Any]:
    if not _FULL_SHA_RE.fullmatch(head_sha or ""):
        # Only a full commit id is ever interpolated into the GitHub URL.
        return _skipped(repo, head_sha or "", False, "head commit is not a full sha")
    if not await _repo_verified(db, project_id, repo):
        return _skipped(repo, head_sha, False, "repo_not_verified")
    # Merge checks are a Team feature: a downgraded project stops posting
    # check runs and comments (the explicit trigger route is gated too).
    from sessionfs.server.tier_gate import project_plan_has_feature

    project = await db.get(Project, project_id)
    if not await project_plan_has_feature(project, "review_loop", db):
        return _skipped(repo, head_sha, False, "plan")

    # Ordering: the evaluation sequence is taken before readiness is read
    # (row locked until the commit below, before any outbound call), so a
    # stale evaluation can never post over a newer one.
    seq = await take_eval_seq(
        db, project_id=project_id, provider="github", repo=repo, head_sha=head_sha
    )

    all_ready = True
    any_verdict = False
    reasons: list[str] = []
    for t in tickets:
        readiness = await _compute_readiness(db, t, head_sha)
        if readiness.get("verdict") is not None:
            any_verdict = True
        if not readiness["ready"]:
            all_ready = False
            t_reasons = readiness["reasons"] or ["not ready"]
            reasons.extend(f"{t.id}: {r}" for r in t_reasons)
    ready = all_ready
    summary = _check_run_summary(ready, reasons)
    conclusion = _check_run_conclusion(ready, {"v": 1} if any_verdict else None)

    owner = (repo or "").partition("/")[0].lower()
    installation = (
        await db.execute(
            select(GitHubInstallation)
            .where(func.lower(GitHubInstallation.account_login) == owner)
            .limit(1)
        )
    ).scalar_one_or_none()

    # Plain values only past this point: commit ends the read transaction
    # before any external call (review checklist #12).
    installation_id = installation.id if installation is not None else None
    owner_user_id = installation.user_id if installation is not None else None
    pr_number = pr.number if pr is not None else None
    await db.commit()

    if installation_id is None:
        return _skipped(
            repo, head_sha, ready, "no GitHub App installation for the repository owner"
        )

    try:
        token = await get_installation_token(installation_id)
    except Exception as exc:
        logger.warning(
            "merge check: failed to obtain installation token for installation %s: %s",
            installation_id,
            exc,
        )
        return _skipped(repo, head_sha, ready, "installation token unavailable")

    # Confused-deputy defense: only a repo actually granted to the
    # installation may receive a check run.
    try:
        grant = await _find_installation_repo(token, repo)
    except Exception as exc:
        logger.warning("merge check: failed to verify installation access to %s: %s", repo, exc)
        return _skipped(repo, head_sha, ready, "could not verify installation access")
    if grant is None:
        return _skipped(repo, head_sha, ready, "repository not granted to the installation")

    guard = PostGuard(
        db=db,
        project_id=project_id,
        provider="github",
        repo=repo,
        head_sha=head_sha,
        seq=seq,
    )
    posted = await _post_or_update_check_run(
        token, repo, head_sha, conclusion, summary, guard=guard
    )
    if posted == "superseded":
        return _skipped(
            repo, head_sha, ready, "a newer evaluation of this commit already posted"
        )
    if posted == "check_run":
        return {"repo": repo, "head_sha": head_sha, "ready": ready, "posted": "check_run"}

    if posted == "forbidden":
        # Installation lacks checks:write — degrade to a PR comment with the
        # same summary.
        if pr_number is None:
            return _skipped(
                repo, head_sha, ready,
                "checks:write not granted and no pull request to comment on",
            )
        if await _post_comment(
            db, token, installation_id, owner_user_id, repo, pr_number, summary
        ):
            return {
                "repo": repo,
                "head_sha": head_sha,
                "ready": ready,
                "posted": "comment",
                "reason": "checks:write not granted; posted a comment instead",
            }
        return _skipped(repo, head_sha, ready, "checks:write not granted and comment failed")

    reason = {
        "not_found": "repository or commit not found on GitHub",
        "unavailable": "GitHub check-run API unavailable",
    }.get(posted, "check run could not be posted")
    return _skipped(repo, head_sha, ready, reason)


async def _post_comment(
    db: AsyncSession,
    token: str,
    installation_id: int,
    owner_user_id: Optional[str],
    repo: str,
    pr_number: int,
    summary: str,
) -> bool:
    """Degrade to the existing PR-comment machinery with the same summary."""
    if owner_user_id is None:
        return False
    from sessionfs.server.routes.webhooks import (
        _Superseded,
        _create_or_adopt_comment,
        _publish_newest,
        _record_desired_comment,
    )

    try:
        decision = await _record_desired_comment(
            db, installation_id, repo, pr_number, summary, "[]",
            owner_user_id=owner_user_id,
        )
    except _Superseded:
        return False
    except Exception:
        logger.warning(
            "merge check: failed to record comment on %s#%d", repo, pr_number, exc_info=True
        )
        return False
    if decision is None:
        return True  # another worker will publish this version
    mode, row_id, comment_id, lease = decision
    try:
        if mode == "create":
            assert lease is not None
            await _create_or_adopt_comment(
                db, token, row_id, lease, installation_id, repo, pr_number
            )
        else:
            assert comment_id is not None
            await _publish_newest(db, token, row_id, repo, pr_number, comment_id)
        return True
    except Exception:
        logger.warning(
            "merge check: failed to publish comment on %s#%d", repo, pr_number, exc_info=True
        )
        return False


# ── Re-evaluation hook ─────────────────────────────────────────────


async def on_verdict_recorded(db: AsyncSession, ticket_id: str) -> None:
    """Re-evaluate a ticket's merge check after a verdict, an approval
    decision or a policy change.

    Provider-aware: the GitHub check run (``refresh_merge_check``) and the
    GitLab commit status of every open GitLab merge request the ticket is
    linked to (``delivery_ingest.post_gitlab_status``). Best-effort and never
    raises (a failing check must not fail the settle request).

    Raises the ticket's approval requests first (a write path), then posts
    the read-only check/status; ``refresh_merge_check`` commits before any
    outbound call, which persists the new requests.
    """
    try:
        # A savepoint: a failure here never drops what the caller already
        # holds in the transaction (the org recheck pages many tickets).
        async with db.begin_nested():
            await raise_approvals_for_ticket(db, ticket_id)
    except Exception:
        logger.warning(
            "approval requests could not be raised for ticket %s", ticket_id, exc_info=True
        )
    try:
        await refresh_merge_check(db, ticket_id=ticket_id)
    except Exception:
        logger.warning(
            "merge check re-evaluation failed for ticket %s", ticket_id, exc_info=True
        )
    try:
        await refresh_gitlab_statuses(db, ticket_id)
    except Exception:
        logger.warning(
            "GitLab status re-evaluation failed for ticket %s", ticket_id, exc_info=True
        )


async def refresh_gitlab_statuses(db: AsyncSession, ticket_id: str) -> int:
    """Re-post the GitLab commit status for each open GitLab merge request
    linked to ``ticket_id`` (one post per distinct (repo, head sha)). Returns
    the number of posts attempted.

    Every status is computed first; the session's transaction is then
    committed (keeping any approval request raised while computing
    readiness) so no transaction is open while the posts are sent and
    retried."""
    from sessionfs.server.services.delivery_ingest import (
        plan_gitlab_status,
        send_gitlab_statuses,
    )

    rows = (
        await db.execute(
            select(PullRequest.project_id, PullRequest.repo, PullRequest.head_sha)
            .join(TicketPrLink, TicketPrLink.pr_id == PullRequest.id)
            .join(Ticket, Ticket.id == TicketPrLink.ticket_id)
            .where(
                TicketPrLink.ticket_id == ticket_id,
                PullRequest.provider == "gitlab",
                PullRequest.state == "open",
                PullRequest.head_sha.is_not(None),
                PullRequest.project_id == Ticket.project_id,
            )
            .distinct()
        )
    ).all()
    posted = 0
    planned = []
    for project_id, repo, sha in sorted(rows):
        if not _FULL_SHA_RE.fullmatch(sha or ""):
            continue
        project = await db.get(Project, project_id)
        if project is None:
            continue
        posted += 1
        status = await plan_gitlab_status(db, project=project, repo=repo, sha=sha)
        if status is not None:
            planned.append(status)
    if planned:
        if db.in_transaction():
            await db.commit()
        await send_gitlab_statuses(planned, db)
    return posted


_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _recheck_with_fresh_session(ticket_id: str) -> None:
    from sessionfs.server.db.engine import _session_factory

    if _session_factory is None:
        return
    try:
        async with _session_factory() as db:
            await on_verdict_recorded(db, ticket_id)
            # Keep any approval request raised for the ticket.
            await db.commit()
    except Exception:
        logger.warning(
            "merge check re-evaluation failed for ticket %s", ticket_id, exc_info=True
        )


def schedule_recheck(ticket_id: str) -> None:
    """Spawn a detached re-evaluation (the work-queue settle path calls this
    after its commit so a merge check never delays the request). Refreshes
    both the GitHub check and the GitLab commit status."""
    _spawn(_recheck_with_fresh_session(ticket_id))


# ── Org-wide recheck (approval policy writes) ──────────────────────

ORG_RECHECK_PAGE = 200


async def org_open_pr_ticket_page(
    db: AsyncSession, org_id: str, *, after: Optional[str] = None,
    limit: int = ORG_RECHECK_PAGE,
) -> list[str]:
    """One keyset page (ordered by ticket id) of the tickets linked to an
    open PR or MR in ``org_id``'s projects."""
    stmt = (
        select(TicketPrLink.ticket_id)
        .join(PullRequest, PullRequest.id == TicketPrLink.pr_id)
        .join(Project, Project.id == PullRequest.project_id)
        .where(Project.org_id == org_id, PullRequest.state == "open")
        .distinct()
        .order_by(TicketPrLink.ticket_id)
        .limit(limit)
    )
    if after is not None:
        stmt = stmt.where(TicketPrLink.ticket_id > after)
    return list((await db.execute(stmt)).scalars().all())


async def recheck_org(
    org_id: str, *, session_factory: Any, page_size: int = ORG_RECHECK_PAGE
) -> int:
    """Re-evaluate every open-PR ticket of ``org_id`` (GitHub + GitLab),
    page by page: each page runs in its own short session and is committed
    before the next, so the work per transaction stays bounded while no
    ticket is dropped. Returns the number of tickets rechecked."""
    after: Optional[str] = None
    done = 0
    while True:
        async with session_factory() as db:
            page = await org_open_pr_ticket_page(db, org_id, after=after, limit=page_size)
            if not page:
                return done
            for ticket_id in page:
                await on_verdict_recorded(db, ticket_id)
            await db.commit()
        done += len(page)
        after = page[-1]
        if len(page) < page_size:
            return done


async def _recheck_org_with_fresh_session(org_id: str) -> None:
    from sessionfs.server.db.engine import _session_factory

    if _session_factory is None:
        return
    try:
        await recheck_org(org_id, session_factory=_session_factory)
    except Exception:
        logger.warning("org merge check re-evaluation failed for %s", org_id, exc_info=True)


def schedule_org_recheck(org_id: str) -> None:
    """Spawn ONE detached task that rechecks every open-PR ticket in the org
    (an approval policy was created, edited or deactivated)."""
    _spawn(_recheck_org_with_fresh_session(org_id))

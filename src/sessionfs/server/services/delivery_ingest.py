"""Delivery-webhook ingest (Review Loop R3, track F2 — GitHub #119).

Pure-DB ingest for the per-org GitHub/GitLab delivery webhooks: maps a repo
remote to the project that owns it (scoped to the webhook's org — a repo owned
by a *different* org is ignored), then dispatches ``pull_request`` / ``release``
/ ``deployment`` events to the provider-neutral writes in ``services.releases``.

The route layer (``routes.delivery_webhooks``) handles the signature/token
auth, the 1 MiB body cap and the idempotency ledger; this module never touches
HTTP. Every write reuses the idempotent upserts in ``services.releases`` so a
re-delivery that does get through is still a no-op at the data layer.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.git_remote import normalize_git_remote
from sessionfs.server.db.models import (
    DeliveryWebhookEvent,
    GitLabSettings,
    Project,
    ProjectRepo,
    PullRequest,
    Release,
    Ticket,
    TicketComment,
    TicketPrLink,
)
from sessionfs.server.sanitize import sanitize_archive_string
from sessionfs.server.services import releases
from sessionfs.server.tier_gate import project_plan_has_feature
from sessionfs.server.services.project_resolver import (
    ProjectResolutionLoopError,
    resolve_project_by_remote,
)

logger = logging.getLogger("sessionfs.delivery_ingest")

_MAX_SHA = 64

_GITHUB_RELEASE_ACTIONS = {"published", "released"}

# GitLab merge-request ``state`` → PullRequest.state (pull_requests CHECK).
_GITLAB_MR_STATES = {"opened": "open", "closed": "closed", "merged": "merged"}

# Merge-request ``action`` values that (re)trigger a commit-status push.
_GITLAB_MR_STATUS_ACTIONS = {"open", "update", "reopen"}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str | None:
    if isinstance(value, (str, int)):
        text = str(value).strip()
        return text or None
    return None


def _sha(value: Any) -> str | None:
    """A commit id clamped to the column width, or None when absent."""
    text = _str(value)
    if not text:
        return None
    return text[:_MAX_SHA]


_HEX_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _full_sha(value: Any) -> str | None:
    """A full 40/64-char lowercase hex commit sha, else None (never a branch
    name or tag, which can never match a deployment's sha)."""
    text = _str(value)
    if text and _HEX_SHA_RE.match(text.lower()):
        return text.lower()
    return None


def _parse_dt(value: Any) -> datetime | None:
    """Parse a provider ISO-8601 timestamp (``Z`` or ``+00:00``) to aware UTC.

    Python 3.10's ``fromisoformat`` rejects a trailing ``Z``, so normalize it
    to ``+00:00`` first. Unparseable values return None (the columns are
    optional), never raise.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    elif text.endswith(" UTC"):  # GitLab: "2026-01-01 12:00:00 UTC"
        text = text[:-4] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _sanitize_text(value: Any) -> str:
    """Collapse whitespace, strip NULs/lone surrogates, then redact secrets.

    Mirrors ``loop_checklist._sanitize_text`` (review checklist #7): the
    whitespace collapse runs first so provider markdown can't be restructured
    by multi-line input; the NUL/surrogate strip is mandatory because those
    values 500 on PostgreSQL. DLP redaction is best-effort.
    """
    if not isinstance(value, str):
        value = ""
    value = " ".join(value.split())
    value, _ = sanitize_archive_string(value)
    try:
        from sessionfs.server.dlp import redact_text, scan_dlp

        findings = scan_dlp(value, categories=["secrets"])
        if findings:
            value = redact_text(value, findings)
    except Exception:
        pass
    return value


# ── Idempotency ledger ──────────────────────────────────────────────


async def _dialect_insert(db: AsyncSession, model: Any) -> Any:
    """A dialect-correct INSERT builder (PG vs SQLite differ on ON CONFLICT)."""
    conn = await db.connection()
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert  # type: ignore[assignment]
    return dialect_insert(model)


async def record_delivery_event(
    db: AsyncSession,
    *,
    provider: str,
    org_id: str,
    idempotency_key: str,
) -> bool:
    """Claim a delivery in the idempotency ledger. False on a re-delivery.

    The row is inserted (not committed) so the caller can commit it atomically
    with the event's own writes — if ingest fails, the claim rolls back and the
    provider's retry is re-processed rather than silently dropped.
    """
    result = await db.execute(
        (await _dialect_insert(db, DeliveryWebhookEvent))
        .values(
            provider=provider,
            org_id=org_id,
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing()
    )
    return result.rowcount > 0


# ── Repo → project mapping ─────────────────────────────────────────


def _repo_urls(provider: str, payload: dict) -> list[str]:
    """Candidate repo remote URLs from the payload, in preference order."""
    if provider == "github":
        repo = _dict(payload.get("repository"))
        return [
            u
            for u in (
                repo.get("clone_url"),
                repo.get("ssh_url"),
                repo.get("git_url"),
                repo.get("html_url"),
            )
            if isinstance(u, str) and u.strip()
        ]
    project = _dict(payload.get("project"))
    return [
        u
        for u in (
            project.get("git_http_url"),
            project.get("git_ssh_url"),
            project.get("http_url"),
        )
        if isinstance(u, str) and u.strip()
    ]


async def _resolve_project(
    db: AsyncSession, *, org_id: str, provider: str, payload: dict
) -> tuple[Project | None, str]:
    """Resolve the payload's repo to an in-org project, returning (project,
    normalized_remote). A repo owned by another org (or no project at all)
    resolves to (None, "") — the webhook is ignored, never mapped cross-org."""
    for url in _repo_urls(provider, payload):
        normalized = normalize_git_remote(url)
        if not normalized:
            continue
        try:
            project = await resolve_project_by_remote(db, normalized)
        except ProjectResolutionLoopError:
            logger.warning("delivery webhook: resolution loop for remote %s", normalized)
            continue
        if project is not None and project.org_id == org_id:
            return project, normalized
    return None, ""


# ── Event dispatch ──────────────────────────────────────────────────


async def ingest_event(
    db: AsyncSession,
    *,
    provider: str,
    org_id: str,
    event: str,
    payload: dict,
) -> dict:
    """Ingest one verified delivery event. Returns a small status dict.

    ``event`` is GitHub's ``X-GitHub-Event`` header value or GitLab's
    ``object_kind``. The caller owns auth and the idempotency ledger; this
    function only maps + writes and never raises for a merely unsupported or
    unmapped event.
    """
    project, repo = await _resolve_project(
        db, org_id=org_id, provider=provider, payload=payload
    )
    if project is None:
        return {"status": "ignored", "reason": "unmapped"}
    if not await project_plan_has_feature(project, "agent_tickets", db):
        return {"status": "ignored", "reason": "plan"}

    try:
        if provider == "github":
            return await _ingest_github(db, project, repo, event, payload)
        return await _ingest_gitlab(db, project, repo, event, payload)
    except (ValueError, TypeError, DBAPIError):
        # A bad provider field must not 500 (the provider would retry forever).
        await db.rollback()
        logger.warning("delivery webhook: dropped %s event with invalid fields", event)
        return {"status": "ignored", "reason": "invalid_fields"}


# ── GitHub ──────────────────────────────────────────────────────────


async def _ingest_github(
    db: AsyncSession, project: Project, repo: str, event: str, payload: dict
) -> dict:
    if event == "pull_request":
        return await _ingest_github_pr(db, project, repo, payload)
    if event == "release":
        return await _ingest_github_release(db, project, payload)
    if event == "deployment_status":
        return await _ingest_github_deployment(db, project, payload)
    return {"status": "ignored", "reason": "unsupported_event"}


async def _ingest_github_pr(
    db: AsyncSession, project: Project, repo: str, payload: dict
) -> dict:
    pr = _dict(payload.get("pull_request"))
    number = pr.get("number")
    if not releases.valid_pr_number(number):
        return {"status": "ignored", "reason": "missing_fields"}

    head = _dict(pr.get("head"))
    state = "merged" if pr.get("merged") else ("open" if pr.get("state") == "open" else "closed")
    await releases.upsert_pull_request(
        db,
        project_id=project.id,
        provider="github",
        repo=repo,
        number=number,
        title=_sanitize_text(pr.get("title")),
        external_id=_str(pr.get("id")),
        state=state,
        branch=_str(head.get("ref")) or "",
        head_sha=_sha(head.get("sha")),
        merged_at=_parse_dt(pr.get("merged_at")),
        merge_commit_sha=_sha(pr.get("merge_commit_sha")),
        external_url=releases.safe_url(pr.get("html_url")),
        body=_sanitize_text(pr.get("body")),
        provider_updated_at=_parse_dt(pr.get("updated_at")),
    )
    return {"status": "ok"}


async def _ingest_github_release(
    db: AsyncSession, project: Project, payload: dict
) -> dict:
    # Only a published/released release counts; drafts, deletions and edits
    # of drafts are not ships.
    if payload.get("action") not in _GITHUB_RELEASE_ACTIONS:
        return {"status": "ignored", "reason": "release_action"}
    release = _dict(payload.get("release"))
    version = _str(release.get("tag_name"))
    if not version or release.get("draft"):
        return {"status": "ignored", "reason": "missing_fields"}
    await releases.record_release(
        db,
        project_id=project.id,
        version=version,
        # Only a real commit sha; target_commitish is usually a branch name.
        tag_sha=_full_sha(release.get("target_commitish")) or "",
        released_at=_parse_dt(release.get("published_at")),
        source="github_release",
    )
    return {"status": "ok"}


async def _ingest_github_deployment(
    db: AsyncSession, project: Project, payload: dict
) -> dict:
    status = _dict(payload.get("deployment_status"))
    if status.get("state") != "success":
        return {"status": "ignored", "reason": "not_success"}
    deployment = _dict(payload.get("deployment"))
    environment = _str(deployment.get("environment")) or _str(status.get("environment"))
    sha = _full_sha(deployment.get("sha"))
    if not environment or not sha:
        return {"status": "ignored", "reason": "missing_fields"}
    release = (
        await db.execute(
            select(Release).where(
                Release.project_id == project.id,
                Release.tag_sha == sha,
            )
        )
    ).scalars().first()
    if release is None:
        return {"status": "ignored", "reason": "no_matching_release"}
    await releases.record_deployment(
        db,
        release_id=release.id,
        environment=environment,
        deployed_at=_parse_dt(status.get("updated_at") or status.get("created_at")),
        source="manual",
    )
    return {"status": "ok"}


# ── GitLab ──────────────────────────────────────────────────────────


async def _ingest_gitlab(
    db: AsyncSession, project: Project, repo: str, event: str, payload: dict
) -> dict:
    if event == "merge_request":
        return await _ingest_gitlab_mr(db, project, repo, payload)
    if event == "release":
        return await _ingest_gitlab_release(db, project, payload)
    if event == "deployment":
        return await _ingest_gitlab_deployment(db, project, payload)
    return {"status": "ignored", "reason": "unsupported_event"}


async def _ingest_gitlab_mr(
    db: AsyncSession, project: Project, repo: str, payload: dict
) -> dict:
    mr = _dict(payload.get("object_attributes"))
    number = mr.get("iid")
    if not releases.valid_pr_number(number):
        return {"status": "ignored", "reason": "missing_fields"}

    last_commit = _dict(mr.get("last_commit"))
    head_sha = (
        _sha(last_commit.get("id"))
        or _sha(mr.get("last_commit_id"))
        or _sha(mr.get("sha"))
    )
    state = _GITLAB_MR_STATES.get(mr.get("state"), "open")
    result = await releases.upsert_pull_request(
        db,
        project_id=project.id,
        provider="gitlab",
        repo=repo,
        number=number,
        title=_sanitize_text(mr.get("title")),
        external_id=_str(mr.get("id")),
        state=state,
        branch=_str(mr.get("source_branch")) or "",
        head_sha=head_sha,
        merged_at=_parse_dt(mr.get("merged_at")),
        merge_commit_sha=_sha(mr.get("merge_commit_sha")),
        external_url=releases.safe_url(mr.get("url")),
        body=_sanitize_text(mr.get("description")),
        provider_updated_at=_parse_dt(mr.get("updated_at")),
    )

    # Push the commit status on open/update (the fallback covers a payload
    # with no ``action`` field). Only when the MR actually linked a ticket.
    action = mr.get("action") or ""
    if action in _GITLAB_MR_STATUS_ACTIONS or (not action and state == "open"):
        if head_sha and result.get("linked_ticket_ids"):
            await post_gitlab_status(db, project=project, repo=repo, sha=head_sha)
    return {"status": "ok"}


async def _ingest_gitlab_release(
    db: AsyncSession, project: Project, payload: dict
) -> dict:
    version = _str(payload.get("tag"))
    if not version:
        return {"status": "ignored", "reason": "missing_fields"}
    commit = _dict(payload.get("commit"))
    await releases.record_release(
        db,
        project_id=project.id,
        version=version,
        tag_sha=_full_sha(commit.get("id")) or "",
        released_at=_parse_dt(payload.get("released_at") or payload.get("created_at")),
        source="gitlab_release",
    )
    return {"status": "ok"}


async def _ingest_gitlab_deployment(
    db: AsyncSession, project: Project, payload: dict
) -> dict:
    if payload.get("status") != "success":
        return {"status": "ignored", "reason": "not_success"}
    environment = _str(payload.get("environment"))
    sha = _full_sha(payload.get("sha"))
    if not environment or not sha:
        return {"status": "ignored", "reason": "missing_fields"}
    release = (
        await db.execute(
            select(Release).where(
                Release.project_id == project.id,
                Release.tag_sha == sha,
            )
        )
    ).scalars().first()
    if release is None:
        return {"status": "ignored", "reason": "no_matching_release"}
    await releases.record_deployment(
        db,
        release_id=release.id,
        environment=environment,
        deployed_at=_parse_dt(payload.get("created_at")),
        source="manual",
    )
    return {"status": "ok"}


# ── GitLab commit status ──────────────────────────────────────────


async def _ticket_merge_readiness(db: AsyncSession, ticket: Ticket, sha: str) -> dict:
    """The shared readiness computation, mirrored from
    ``routes.tickets.get_ticket_merge_readiness`` so the badge can't drift."""
    from sessionfs.server.services.review_state import (
        compute_merge_readiness,
        load_implementer_identities,
        load_key_minters,
    )

    rows = (
        await db.execute(
            select(TicketComment)
            .where(TicketComment.ticket_id == ticket.id)
            .order_by(TicketComment.created_at, TicketComment.id)
            .limit(500)
        )
    ).scalars().all()
    comments = [
        {
            "id": r.id,
            "author_persona": r.author_persona,
            "content": r.content,
            "created_at": r.created_at,
            "verdict_trusted": r.verdict_trusted,
            "service_key_id": r.service_key_id,
            "author_user_id": r.author_user_id,
            "reviewed_commit": r.reviewed_commit,
        }
        for r in rows
    ]
    identities = (await load_implementer_identities(db, [ticket]))[ticket.id]
    key_minters = await load_key_minters(db, comments, identities)
    return compute_merge_readiness(
        comments=comments,
        sha=sha,
        implementer_identities=identities,
        key_minters=key_minters,
    )


async def post_gitlab_status(
    db: AsyncSession, *, project: Project, repo: str, sha: str
) -> None:
    """Best-effort GitLab commit status for a merge request's head sha.

    Badges the head commit through the project owner's GitLab integration
    (the client re-checks the instance URL's SSRF guard before the request
    and never follows redirects). State is ``success`` only when every ticket
    linked to the PR is merge-ready for that exact sha; otherwise ``pending``.
    Never raises — a missing integration or a failed API call must not fail
    the webhook.
    """
    if not sha or _full_sha(sha) is None:
        return
    try:
        await _post_gitlab_status(db, project=project, repo=repo, sha=sha)
    except Exception:
        logger.exception("GitLab commit status post failed for %s @ %s", project.id, sha)


async def _post_gitlab_status(
    db: AsyncSession, *, project: Project, repo: str, sha: str
) -> None:
    # The GitLab numeric project id lives on the repo row and is best-effort
    # (frequently NULL) — without it there is no remote to badge.
    repo_row = (
        await db.execute(
            select(ProjectRepo).where(
                ProjectRepo.project_id == project.id,
                ProjectRepo.git_remote_normalized == repo,
            )
        )
    ).scalars().first()
    if repo_row is None or not repo_row.provider_repo_id:
        return
    try:
        gitlab_project_id = int(repo_row.provider_repo_id)
    except (TypeError, ValueError):
        return

    settings = (
        await db.execute(
            select(GitLabSettings).where(GitLabSettings.user_id == project.owner_id)
        )
    ).scalars().first()
    if settings is None:
        return

    # The status belongs to the commit, so evaluate the tickets linked to
    # EVERY merge request row at this head sha: a second MR on the same
    # commit (e.g. from a fork) can only add tickets, never hide one.
    ticket_ids = sorted(
        set(
            (
                await db.execute(
                    select(TicketPrLink.ticket_id)
                    .join(PullRequest, PullRequest.id == TicketPrLink.pr_id)
                    .where(
                        PullRequest.project_id == project.id,
                        PullRequest.provider == "gitlab",
                        PullRequest.repo == repo,
                        PullRequest.head_sha == sha,
                    )
                )
            ).scalars().all()
        )
    )
    if not ticket_ids:
        return

    ready = True
    for ticket_id in ticket_ids:
        ticket = await db.get(Ticket, ticket_id)
        if ticket is None:
            ready = False
            break
        if not (await _ticket_merge_readiness(db, ticket, sha))["ready"]:
            ready = False
            break

    from sessionfs.server.gitlab_client import GitLabClient
    from sessionfs.server.llm_keys import decrypt_secret

    access_token = decrypt_secret(settings.encrypted_access_token)
    client = GitLabClient(settings.instance_url, access_token)
    await client.post_commit_status(
        gitlab_project_id,
        sha,
        "success" if ready else "pending",
        name="SessionFS review loop",
        description="All review checks passed" if ready else "Review in progress",
    )

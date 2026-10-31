"""Pull requests, releases, gates, deployments and the Ship Report.

Review Loop R3, track F1 (docs/design/review-loop-r3.md §4, §6.4). Pure-DB,
provider-neutral writes plus the per-release / per-month Ship Report read.

Writes are idempotent upserts keyed on the natural uniqueness of each row:

- a PR upserts on ``(project_id, provider, repo, number)``;
- a release upserts on ``(project_id, version)``;
- a gate upserts on ``(release_id, kind)``;
- a deployment is append-only (a release can deploy to the same environment
  repeatedly).

``upsert_pull_request`` also auto-links tickets: ``tk_…`` ids found in the PR
title/body/branch link with ``link_source='auto_parsed'`` (only ids that are
real tickets in the *same* project — a cross-project ``tk_…`` is ignored), and
agent runs whose ``branch`` matches the PR branch link their ticket with
``link_source='loop'``.

The Ship Report reuses the review-loop rollup helpers (``review_loop``) rather
than duplicating finding/token/round logic.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentRun,
    Deployment,
    LoopChecklistItem,
    PullRequest,
    Release,
    ReleaseGate,
    ReleasePr,
    Ticket,
    TicketComment,
    TicketPrLink,
)
from sessionfs.server.sanitize import sanitize_archive_string
from sessionfs.server.services import review_loop
from sessionfs.server.services.review_state import compute_review_state

# ── Enums (mirror the migration CHECK constraints) ──

_PR_PROVIDERS: tuple[str, ...] = ("github", "gitlab")
_PR_STATES: tuple[str, ...] = ("open", "closed", "merged")
_LINK_SOURCES: tuple[str, ...] = ("auto_parsed", "manual", "commit_message", "loop")
_RELEASE_SOURCES: tuple[str, ...] = (
    "github_release",
    "gitlab_release",
    "helm_sync",
    "manual",
)
_GATE_STATUSES: tuple[str, ...] = ("passed", "failed", "skipped")
_DEPLOY_SOURCES: tuple[str, ...] = ("argocd", "helm", "manual")

# Ticket ids are ``tk_<16 hex>`` (uuid4().hex[:16]); allow longer hex tails so
# a future longer id still links. The ``\\b`` anchors stop a longer ``tk_…``
# slug (e.g. ``tk_abc…extra``) from matching a shorter id inside it.
_TK_ID_RE = re.compile(r"\btk_[0-9a-fA-F]{16,}\b")

# Harm tags that count as "harmful" in a report (mirror review_loop).
_HARMFUL_EXCLUDED: frozenset[str] = frozenset({None, "", "none"})

_MAX_TITLE = 500
_MAX_REPO = 200  # column is String(200)
_MAX_BRANCH = 200  # column is String(200)
_MAX_EXTERNAL_ID = 200
_MAX_VERSION = 100
_MAX_SHA = 64
_MAX_ENV = 64
_MAX_KIND = 64
_MAX_URL = 2048
MAX_PR_NUMBER = 2_147_483_647
MAX_PR_NUMBERS = 200


def clean_str(value: Any, cap: int) -> str:
    """Strip NULs/lone surrogates (they 500 on PostgreSQL), trim, cap."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value, _ = sanitize_archive_string(value)
    return value.strip()[:cap]


def safe_url(value: Any) -> str | None:
    """An http(s) URL up to 2048 chars with no control/space characters, else
    None. Anything else (``javascript:``, ``data:``, relative) is dropped."""
    text = clean_str(value, _MAX_URL + 1)
    if not text or len(text) > _MAX_URL:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        return None
    if not re.match(r"^https?://[^/\s]+", text, re.IGNORECASE):
        return None
    return text


def valid_pr_number(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= MAX_PR_NUMBER
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _dialect_insert(db: AsyncSession, model: Any) -> Any:
    """A dialect-correct INSERT builder (PG vs SQLite differ on ON CONFLICT)."""
    conn = await db.connection()
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert  # type: ignore[assignment]
    return dialect_insert(model)


def _extract_ticket_ids(*texts: str | None) -> set[str]:
    ids: set[str] = set()
    for value in texts:
        if value:
            ids.update(_TK_ID_RE.findall(value))
    return ids


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── Pull requests ──────────────────────────────────────────────────


async def _link_tickets(
    db: AsyncSession,
    *,
    project_id: str,
    pr_id: str,
    branch: str,
    body: str,
    title: str,
) -> list[str]:
    """Auto-link tickets to a PR, returning the linked ticket ids.

    Two sources, both restricted to the *same* project:

    - ``auto_parsed`` — ``tk_…`` ids in the PR title/body/branch;
    - ``loop`` — agent runs whose ``branch`` equals the PR branch.

    Loop links are inserted first so a ticket found by both keeps the more
    specific ``loop`` provenance (a second insert no-ops on the unique pair).
    """
    branch_ids: set[str] = set()
    if branch:
        branch_ids = set(
            (
                await db.execute(
                    select(AgentRun.ticket_id)
                    .where(
                        AgentRun.project_id == project_id,
                        AgentRun.branch.is_not(None),
                        AgentRun.branch == branch,
                        AgentRun.ticket_id.is_not(None),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
    parsed_ids = _extract_ticket_ids(title, body, branch)
    # Bound the IN list: a PR mentioning thousands of ids links none past 200.
    candidates = set(sorted(branch_ids | parsed_ids)[:200])
    if not candidates:
        return []

    # Only ids that are real tickets in THIS project (cross-project ignored).
    existing = set(
        (
            await db.execute(
                select(Ticket.id).where(
                    Ticket.project_id == project_id,
                    Ticket.id.in_(candidates),
                )
            )
        )
        .scalars()
        .all()
    )

    for ticket_id in branch_ids & existing:
        await db.execute(
            (await _dialect_insert(db, TicketPrLink))
            .values(ticket_id=ticket_id, pr_id=pr_id, link_source="loop")
            .on_conflict_do_nothing()
        )
    for ticket_id in (parsed_ids & existing) - (branch_ids & existing):
        await db.execute(
            (await _dialect_insert(db, TicketPrLink))
            .values(
                ticket_id=ticket_id, pr_id=pr_id, link_source="auto_parsed"
            )
            .on_conflict_do_nothing()
        )
    return sorted(existing)


async def upsert_pull_request(
    db: AsyncSession,
    *,
    project_id: str,
    provider: str,
    repo: str,
    number: int,
    title: str,
    external_id: str | None = None,
    state: str = "open",
    author_user_id: str | None = None,
    branch: str | None = None,
    head_sha: str | None = None,
    merged_at: datetime | None = None,
    merge_commit_sha: str | None = None,
    external_url: str | None = None,
    body: str = "",
    provider_updated_at: datetime | None = None,
) -> dict[str, Any]:
    """Insert or update a PR, then auto-link its tickets. Idempotent.

    Out-of-order safe: a ``merged`` PR never reverts to open/closed, and when
    the provider's ``updated_at`` is supplied an older event is skipped. The
    repo is stored lowercase so the App and delivery paths share one row.
    """
    if not valid_pr_number(number):
        raise ValueError("number must be between 1 and 2147483647")
    provider = (provider or "").strip().lower()
    state = (state or "").strip().lower()
    repo = clean_str(repo, _MAX_REPO).lower()
    branch = clean_str(branch, _MAX_BRANCH)
    title = clean_str(title, _MAX_TITLE)
    if provider_updated_at is not None and provider_updated_at.tzinfo is None:
        provider_updated_at = provider_updated_at.replace(tzinfo=timezone.utc)

    values: dict[str, Any] = {
        "id": f"pr_{uuid.uuid4().hex[:16]}",
        "project_id": project_id,
        "provider": provider,
        "external_id": clean_str(external_id, _MAX_EXTERNAL_ID) or None,
        "repo": repo,
        "number": number,
        "title": title,
        "state": state,
        "author_user_id": author_user_id,
        "branch": branch,
        "head_sha": clean_str(head_sha, _MAX_SHA) or None,
        "merged_at": merged_at,
        "merge_commit_sha": clean_str(merge_commit_sha, _MAX_SHA) or None,
        "external_url": safe_url(external_url),
    }
    if provider_updated_at is not None:
        values["updated_at"] = provider_updated_at
    stmt = (await _dialect_insert(db, PullRequest)).values(**values)
    guard: list[Any] = [
        or_(PullRequest.state != "merged", stmt.excluded.state == "merged")
    ]
    if provider_updated_at is not None:
        guard.append(PullRequest.updated_at <= stmt.excluded.updated_at)
    stmt = stmt.on_conflict_do_update(
        index_elements=["project_id", "provider", "repo", "number"],
        where=and_(*guard),
        set_={
            "external_id": stmt.excluded.external_id,
            "title": stmt.excluded.title,
            "state": stmt.excluded.state,
            "author_user_id": stmt.excluded.author_user_id,
            "branch": stmt.excluded.branch,
            "head_sha": stmt.excluded.head_sha,
            "merged_at": stmt.excluded.merged_at,
            "merge_commit_sha": stmt.excluded.merge_commit_sha,
            "external_url": stmt.excluded.external_url,
            "updated_at": (
                stmt.excluded.updated_at
                if provider_updated_at is not None
                else func.now()
            ),
        },
    )
    await db.execute(stmt)

    pr = (
        await db.execute(
            select(PullRequest).where(
                PullRequest.project_id == project_id,
                PullRequest.provider == provider,
                PullRequest.repo == repo,
                PullRequest.number == number,
            )
        )
    ).scalar_one()

    linked = await _link_tickets(
        db,
        project_id=project_id,
        pr_id=pr.id,
        branch=pr.branch,
        body=body,
        title=pr.title,
    )
    await db.commit()
    return _pr_to_dict(pr, linked_ticket_ids=linked)


# ── Releases / gates / deployments ─────────────────────────────────


async def record_release(
    db: AsyncSession,
    *,
    project_id: str,
    version: str,
    tag_sha: str,
    previous_tag_sha: str | None = None,
    released_at: datetime | None = None,
    source: str = "manual",
    pr_numbers: list[int] | None = None,
    commit_shas: list[str] | None = None,
    created_by_user_id: str | None = None,
    service_key_id: str | None = None,
) -> dict[str, Any]:
    """Insert or update a release, then link its PRs. Idempotent.

    PRs are matched by ``pr_numbers`` (PR number within the project) and/or
    ``commit_shas`` (``head_sha`` or ``merge_commit_sha``). Re-recording the
    same version updates the tag/date/source without duplicating links.
    """
    version = clean_str(version, _MAX_VERSION)
    source = (source or "").strip().lower()
    if pr_numbers is not None:
        pr_numbers = [n for n in pr_numbers if valid_pr_number(n)][:MAX_PR_NUMBERS]
    if commit_shas is not None:
        commit_shas = [clean_str(c, _MAX_SHA) for c in commit_shas[:MAX_PR_NUMBERS]]

    values: dict[str, Any] = {
        "id": f"rel_{uuid.uuid4().hex[:16]}",
        "project_id": project_id,
        "version": version,
        "tag_sha": clean_str(tag_sha, _MAX_SHA),
        "previous_tag_sha": clean_str(previous_tag_sha, _MAX_SHA) or None,
        "released_at": released_at,
        "source": source,
        "created_by_user_id": created_by_user_id,
        "service_key_id": service_key_id,
    }
    stmt = (await _dialect_insert(db, Release)).values(**values)
    # A re-delivery never wipes a recorded value with null/"" and a webhook
    # never takes over a row that was recorded manually.
    stmt = stmt.on_conflict_do_update(
        index_elements=["project_id", "version"],
        set_={
            "tag_sha": func.coalesce(
                func.nullif(stmt.excluded.tag_sha, ""), Release.tag_sha
            ),
            "previous_tag_sha": func.coalesce(
                stmt.excluded.previous_tag_sha, Release.previous_tag_sha
            ),
            "released_at": func.coalesce(
                stmt.excluded.released_at, Release.released_at
            ),
            "source": case(
                (Release.source == "manual", Release.source),
                else_=stmt.excluded.source,
            ),
        },
    )
    await db.execute(stmt)

    release = (
        await db.execute(
            select(Release).where(
                Release.project_id == project_id,
                Release.version == version,
            )
        )
    ).scalar_one()

    pr_ids = await _resolve_pr_ids(
        db, project_id=project_id, pr_numbers=pr_numbers, commit_shas=commit_shas
    )
    for pr_id in pr_ids:
        await db.execute(
            (await _dialect_insert(db, ReleasePr))
            .values(release_id=release.id, pr_id=pr_id)
            .on_conflict_do_nothing()
        )
    await db.commit()
    return _release_to_dict(release, pr_ids=sorted(pr_ids))


async def _resolve_pr_ids(
    db: AsyncSession,
    *,
    project_id: str,
    pr_numbers: list[int] | None,
    commit_shas: list[str] | None,
) -> set[str]:
    conditions: list[Any] = [PullRequest.project_id == project_id]
    if pr_numbers:
        conditions.append(PullRequest.number.in_([int(n) for n in pr_numbers]))
    if commit_shas:
        shas = [s.strip() for s in commit_shas if s and s.strip()]
        if shas:
            conditions.append(
                or_(
                    PullRequest.head_sha.in_(shas),
                    PullRequest.merge_commit_sha.in_(shas),
                )
            )
    if len(conditions) == 1:
        return set()
    rows = (
        await db.execute(
            select(PullRequest.id).where(and_(*conditions))
        )
    ).scalars().all()
    return set(rows)


async def record_gate(
    db: AsyncSession,
    *,
    release_id: str,
    kind: str,
    status: str = "passed",
    run_id: str | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    """Insert or update a release gate (unique per release + kind)."""
    kind = clean_str(kind, _MAX_KIND)
    status = (status or "").strip().lower()
    values: dict[str, Any] = {
        "id": f"rg_{uuid.uuid4().hex[:16]}",
        "release_id": release_id,
        "kind": kind,
        "status": status,
        "run_id": clean_str(run_id, _MAX_SHA) or None,
        "url": safe_url(url),
    }
    stmt = (await _dialect_insert(db, ReleaseGate)).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["release_id", "kind"],
        set_={
            "status": stmt.excluded.status,
            "run_id": stmt.excluded.run_id,
            "url": stmt.excluded.url,
        },
    )
    await db.execute(stmt)
    gate = (
        await db.execute(
            select(ReleaseGate).where(
                ReleaseGate.release_id == release_id,
                ReleaseGate.kind == kind,
            )
        )
    ).scalar_one()
    await db.commit()
    return _gate_to_dict(gate)


async def record_deployment(
    db: AsyncSession,
    *,
    release_id: str,
    environment: str,
    deployed_at: datetime | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    """Append a deployment record for a release (not idempotent — a release
    may deploy to the same environment more than once)."""
    environment = clean_str(environment, _MAX_ENV)
    source = (source or "").strip().lower()
    deployment = Deployment(
        id=f"dep_{uuid.uuid4().hex[:16]}",
        release_id=release_id,
        environment=environment,
        deployed_at=deployed_at or _now(),
        source=source,
    )
    db.add(deployment)
    await db.commit()
    await db.refresh(deployment)
    return _deployment_to_dict(deployment)


# ── Serialization ──────────────────────────────────────────────────


def _pr_to_dict(
    pr: PullRequest, linked_ticket_ids: list[str] | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": pr.id,
        "project_id": pr.project_id,
        "provider": pr.provider,
        "external_id": pr.external_id,
        "repo": pr.repo,
        "number": pr.number,
        "title": pr.title,
        "state": pr.state,
        "author_user_id": pr.author_user_id,
        "branch": pr.branch,
        "head_sha": pr.head_sha,
        "merged_at": review_loop._iso(pr.merged_at),
        "merge_commit_sha": pr.merge_commit_sha,
        "external_url": pr.external_url,
        "created_at": review_loop._iso(pr.created_at),
        "updated_at": review_loop._iso(pr.updated_at),
    }
    if linked_ticket_ids is not None:
        out["linked_ticket_ids"] = linked_ticket_ids
    return out


def _release_to_dict(
    release: Release, pr_ids: list[str] | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": release.id,
        "project_id": release.project_id,
        "version": release.version,
        "tag_sha": release.tag_sha,
        "previous_tag_sha": release.previous_tag_sha,
        "released_at": review_loop._iso(release.released_at),
        "source": release.source,
        "created_by_user_id": release.created_by_user_id,
        "service_key_id": release.service_key_id,
        "created_at": review_loop._iso(release.created_at),
    }
    if pr_ids is not None:
        out["pr_ids"] = pr_ids
    return out


def _gate_to_dict(gate: ReleaseGate) -> dict[str, Any]:
    return {
        "id": gate.id,
        "release_id": gate.release_id,
        "kind": gate.kind,
        "status": gate.status,
        "run_id": gate.run_id,
        "url": gate.url,
        "recorded_at": review_loop._iso(gate.recorded_at),
    }


def _deployment_to_dict(deployment: Deployment) -> dict[str, Any]:
    return {
        "id": deployment.id,
        "release_id": deployment.release_id,
        "environment": deployment.environment,
        "deployed_at": review_loop._iso(deployment.deployed_at),
        "source": deployment.source,
    }


# ── Ship Report ────────────────────────────────────────────────────


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    """Parse ``YYYY-MM`` into a half-open [start, start+1month) UTC window."""
    year, sep, mm = month.partition("-")
    start = datetime(int(year), int(mm), 1, tzinfo=timezone.utc)
    if start.month == 12:
        end = datetime(start.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(start.year, start.month + 1, 1, tzinfo=timezone.utc)
    return start, end


async def _load_releases(
    db: AsyncSession,
    *,
    project_id: str,
    version: str | None,
    month: str | None,
) -> list[Release]:
    if version is not None:
        release = (
            await db.execute(
                select(Release).where(
                    Release.project_id == project_id,
                    Release.version == version,
                )
            )
        ).scalar_one_or_none()
        return [release] if release is not None else []
    start, end = _month_bounds(month or "")
    rows = (
        await db.execute(
            select(Release)
            .where(
                Release.project_id == project_id,
                Release.released_at.is_not(None),
                Release.released_at >= start,
                Release.released_at < end,
            )
            .order_by(Release.released_at, Release.id)
        )
    ).scalars().all()
    return list(rows)


async def _pr_ids_for_releases(
    db: AsyncSession, release_ids: list[str]
) -> set[str]:
    if not release_ids:
        return set()
    return set(
        (
            await db.execute(
                select(ReleasePr.pr_id).where(ReleasePr.release_id.in_(release_ids))
            )
        )
        .scalars()
        .all()
    )


async def _ticket_ids_for_prs(
    db: AsyncSession, pr_ids: set[str]
) -> set[str]:
    if not pr_ids:
        return set()
    return set(
        (
            await db.execute(
                select(TicketPrLink.ticket_id).where(
                    TicketPrLink.pr_id.in_(pr_ids)
                )
            )
        )
        .scalars()
        .all()
    )


async def _checklist_learned(
    db: AsyncSession,
    *,
    project_id: str,
    lo: datetime | None,
    hi: datetime,
) -> list[dict[str, Any]]:
    """Active checklist classes whose ``accepted_at`` falls in ``[lo, hi)``.

    ``lo=None`` means "no lower bound". Uses the ORM entity (the
    ``loop_checklist_items`` table ships with the same track-F migration set).
    """
    stmt = select(LoopChecklistItem).where(
        LoopChecklistItem.project_id == project_id,
        LoopChecklistItem.status == "active",
        LoopChecklistItem.accepted_at.is_not(None),
        LoopChecklistItem.accepted_at < hi,
    )
    if lo is not None:
        stmt = stmt.where(LoopChecklistItem.accepted_at >= lo)
    rows = (
        (await db.execute(stmt.order_by(LoopChecklistItem.accepted_at)))
        .scalars()
        .all()
    )
    return [
        {
            "class": item.slug,
            "title": item.title,
            "joined_at": review_loop._iso(item.accepted_at),
        }
        for item in rows
    ]


async def ship_report(
    db: AsyncSession,
    *,
    project_id: str,
    show_dollars: bool,
    version: str | None = None,
    month: str | None = None,
) -> dict[str, Any]:
    """The Ship Report for one release (``version``) or one ``month``.

    Numbers are counts of stored records only (design §1 "evidence, never
    estimates"). ``all_fixed_before_merge`` is True only when no harmful
    finding is still open. ``cost_per_accepted_change`` mirrors
    ``review_loop``: tokens (and dollars, for dollar-visible callers) of runs
    on done tickets, divided by the number of done tickets.
    """
    releases = await _load_releases(
        db, project_id=project_id, version=version, month=month
    )
    if version is not None and not releases:
        return None  # caller turns this into a 404

    release_ids = [r.id for r in releases]
    pr_ids = await _pr_ids_for_releases(db, release_ids)
    ticket_ids = await _ticket_ids_for_prs(db, pr_ids)

    # Loop parents hold design-stage findings; load them alongside the shipped
    # tickets so the report sees the whole loop, not just the child tasks.
    tickets: list[Ticket] = []
    if ticket_ids:
        tickets = list(
            (await db.execute(select(Ticket).where(Ticket.id.in_(ticket_ids))))
            .scalars()
            .all()
        )
    parent_ids = {t.parent_ticket_id for t in tickets if t.parent_ticket_id}
    parents: list[Ticket] = []
    if parent_ids:
        parents = list(
            (
                await db.execute(
                    select(Ticket).where(
                        Ticket.project_id == project_id,
                        Ticket.id.in_(parent_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
    involved = list(tickets) + [p for p in parents if p.id not in {t.id for t in tickets}]

    loop_tickets = [t for t in involved if t.loop_config is not None]
    done_tickets = [t for t in tickets if t.status == "done"]

    runs: list[AgentRun] = []
    comments: list[TicketComment] = []
    involved_ids = [t.id for t in involved]
    if involved_ids:
        runs = list(
            (
                await db.execute(
                    select(AgentRun)
                    .where(
                        AgentRun.project_id == project_id,
                        AgentRun.ticket_id.in_(involved_ids),
                    )
                    .order_by(AgentRun.created_at, AgentRun.id)
                )
            )
            .scalars()
            .all()
        )
        comments = list(
            (
                await db.execute(
                    select(TicketComment)
                    .where(TicketComment.ticket_id.in_(involved_ids))
                    .order_by(TicketComment.created_at, TicketComment.id)
                    .limit(5000)
                )
            )
            .scalars()
            .all()
        )

    # Findings + harm (one pass over already-loaded runs — no N+1).
    total_findings = 0
    harmful = 0
    harmful_by_harm: dict[str, int] = {}
    all_fixed = True
    for run in runs:
        for finding in review_loop._decode_findings(run.findings):
            total_findings += 1
            harm = finding.get("harm")
            if harm in _HARMFUL_EXCLUDED:
                continue
            harmful += 1
            key = str(harm)
            harmful_by_harm[key] = harmful_by_harm.get(key, 0) + 1
            if not (
                finding.get("status") == "fixed"
                or finding.get("fixed_in_sha") is not None
            ):
                all_fixed = False

    # Review rounds (trusted verdicts) across the involved tickets.
    comments_by_ticket: dict[str, list[TicketComment]] = {}
    for c in comments:
        comments_by_ticket.setdefault(c.ticket_id, []).append(c)
    review_rounds = 0
    for cid, crows in comments_by_ticket.items():
        state = compute_review_state(
            [
                {
                    "id": c.id,
                    "author_persona": c.author_persona,
                    "content": c.content,
                    "created_at": c.created_at,
                    "verdict_trusted": c.verdict_trusted,
                }
                for c in crows
            ]
        )
        if state is not None:
            review_rounds += len(state.rounds)

    # Cost per accepted change over the done tickets (mirror review_loop).
    done_ids = {t.id for t in done_tickets}
    done_runs = [r for r in runs if r.ticket_id in done_ids]
    tokens = sum(review_loop._total_tokens(r) for r in done_runs)
    dollars = sum(review_loop._dollar_cost(r) for r in done_runs)
    accepted = len(done_tickets)
    cost: dict[str, Any] = {
        "tokens": round(tokens / accepted, 2) if accepted else 0.0,
    }
    if show_dollars:
        cost["cost_usd"] = (
            round(float(dollars) / accepted, review_loop._COST_USD_SCALE)
            if accepted
            else 0.0
        )

    # Classes learned in this window: since the previous release (or time
    # immemorial) for a single release, or within the month bucket.
    if version is not None:
        this = releases[0]
        lo = (
            await db.execute(
                select(Release.released_at)
                .where(
                    Release.project_id == project_id,
                    Release.released_at.is_not(None),
                    Release.released_at < (this.released_at or _now()),
                )
                .order_by(Release.released_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        hi = this.released_at or _now()
    else:
        lo, hi = _month_bounds(month or "")
    classes_learned = await _checklist_learned(
        db, project_id=project_id, lo=lo, hi=hi
    )

    # Gates / deployments / PRs for the report body.
    gates: list[dict[str, Any]] = []
    deployments: list[dict[str, Any]] = []
    prs: list[dict[str, Any]] = []
    if release_ids:
        gate_rows = list(
            (
                await db.execute(
                    select(ReleaseGate)
                    .where(ReleaseGate.release_id.in_(release_ids))
                    .order_by(ReleaseGate.kind)
                )
            )
            .scalars()
            .all()
        )
        gates = [_gate_to_dict(g) for g in gate_rows]
        deploy_rows = list(
            (
                await db.execute(
                    select(Deployment)
                    .where(Deployment.release_id.in_(release_ids))
                    .order_by(Deployment.deployed_at, Deployment.id)
                )
            )
            .scalars()
            .all()
        )
        deployments = [_deployment_to_dict(d) for d in deploy_rows]
    if pr_ids:
        pr_rows = list(
            (
                await db.execute(
                    select(PullRequest).where(PullRequest.id.in_(pr_ids))
                )
            )
            .scalars()
            .all()
        )
        prs = [_pr_to_dict(p) for p in pr_rows]

    report: dict[str, Any] = {
        "version": version if version is not None else (month or ""),
        "released_at": review_loop._iso(releases[-1].released_at) if releases else None,
        "loops": len(loop_tickets),
        "tickets": len(tickets),
        "review_rounds": review_rounds,
        "findings": total_findings,
        "harmful": harmful,
        "harmful_by_harm": harmful_by_harm,
        "all_fixed_before_merge": all_fixed,
        "cost_per_accepted_change": cost,
        "checklist_classes_learned": classes_learned,
        "gates": gates,
        "deployments": deployments,
        "prs": prs,
    }
    report["markdown"] = _render_ship_report_markdown(report, show_dollars=show_dollars)
    return report


def _render_ship_report_markdown(
    report: dict[str, Any], *, show_dollars: bool
) -> str:
    """Render the report as a release-note markdown snippet (dollars only for
    dollar-visible callers — design §6.4)."""
    lines: list[str] = []
    heading = report.get("version") or ""
    if heading:
        lines.append(f"# Ship Report — {heading}")
    else:
        lines.append("# Ship Report")
    lines.append("")
    released = report.get("released_at")
    if released:
        lines.append(f"Released: {released}")
    lines.append("")
    lines.append(
        f"- Loops: {report.get('loops', 0)}\n"
        f"- Tickets: {report.get('tickets', 0)}\n"
        f"- Review rounds: {report.get('review_rounds', 0)}\n"
        f"- Findings: {report.get('findings', 0)}\n"
        f"- Harmful: {report.get('harmful', 0)}"
    )
    if report.get("harmful_by_harm"):
        by_harm = ", ".join(
            f"{k}={v}" for k, v in sorted(report["harmful_by_harm"].items())
        )
        lines.append(f"- Harmful by harm: {by_harm}")
    lines.append(
        f"- All fixed before merge: "
        f"{'yes' if report.get('all_fixed_before_merge') else 'no'}"
    )
    cost = report.get("cost_per_accepted_change") or {}
    cost_bits = [f"tokens {cost.get('tokens', 0):g}"]
    if show_dollars and "cost_usd" in cost:
        cost_bits.append(f"${cost['cost_usd']:.2f}")
    lines.append(f"- Cost per accepted change: {', '.join(cost_bits)}")
    learned = report.get("checklist_classes_learned") or []
    if learned:
        names = ", ".join(str(c.get("title") or c.get("class")) for c in learned)
        lines.append(f"- Checklist classes learned: {names}")
    gates = report.get("gates") or []
    if gates:
        gate_bits = ", ".join(
            f"{g.get('kind')}={g.get('status')}" for g in gates
        )
        lines.append(f"- Gates: {gate_bits}")
    deployments = report.get("deployments") or []
    if deployments:
        deploy_bits = ", ".join(
            f"{d.get('environment')} ({d.get('source')})" for d in deployments
        )
        lines.append(f"- Deployments: {deploy_bits}")
    lines.append("")
    return "\n".join(lines)


# ── Release listing (keyset) ───────────────────────────────────────


async def list_releases(
    db: AsyncSession,
    *,
    project_id: str,
    limit: int,
    before: datetime | None,
    before_id: str | None,
) -> list[dict[str, Any]]:
    """Releases newest-first with a ``(created_at, id)`` keyset cursor.

    ``before`` / ``before_id`` are the ``created_at`` and ``id`` of the last
    row the caller already saw; ``before_id`` is the tiebreaker for
    same-timestamp ties. ``before`` filters to strictly older rows.
    """
    stmt = select(Release).where(Release.project_id == project_id)
    if before is not None:
        before = _to_utc(before)
        if before_id:
            cursor_ts = (
                await db.execute(
                    select(Release.created_at).where(
                        Release.id == before_id,
                        Release.project_id == project_id,
                    )
                )
            ).scalar_one_or_none()
            if cursor_ts is not None:
                subq = (
                    select(Release.created_at)
                    .where(Release.id == before_id)
                    .scalar_subquery()
                )
                stmt = stmt.where(
                    or_(
                        Release.created_at < subq,
                        and_(
                            Release.created_at == subq,
                            Release.id < before_id,
                        ),
                    )
                )
            else:
                stmt = stmt.where(Release.created_at < before)
        else:
            stmt = stmt.where(Release.created_at < before)
    stmt = stmt.order_by(Release.created_at.desc(), Release.id.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [_release_to_dict(r) for r in rows]

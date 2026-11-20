"""Auto-reviewer P1a — review request lifecycle (ticket tk_9078a0e3feaa4e8a).

A review request is a stored "review this target for me" record. P1 is
personal: a request belongs to its requester and is served only by that
owner's own runner (``mode=draft_for_me``). Every endpoint here authenticates
with a user key (``get_current_user`` rejects service keys with 403
``service_key_not_allowed``).

Endpoints:
- ``POST   /api/v1/projects/{project_id}/review-requests``  create (idempotent) + supersede
- ``GET    /api/v1/me/review-requests``                     list mine (keyset paged)
- ``GET    /api/v1/review-requests/{id}``                   one request
- ``GET    /api/v1/review-requests/{id}/packet``            the review packet (context)
- ``POST   /api/v1/review-requests/{id}/claim``             atomic claim by a runner
- ``POST   /api/v1/review-requests/{id}/heartbeat``         extend the lease
- ``POST   /api/v1/review-requests/{id}/finish``            fenced finish + findings

Code bytes never reach SessionFS: findings are metadata + DLP-scanned text
only. Finding ``message`` is scanned under the project org's strictest-union
DLP policy — BLOCK refuses with 422, REDACT stores redacted text.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.security.secrets import scan_dlp as scan_dlp_full
from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    LoopChecklistItem,
    Project,
    ReviewRequest,
    ReviewRunner,
    Ticket,
    User,
)
from sessionfs.server.dlp import redact_text
from sessionfs.server.helper.redaction import org_dlp_policy, strictest_dlp_policy
from sessionfs.server.routes.knowledge import _get_project_or_404
from sessionfs.server.routes.tickets import _get_ticket_or_404
from sessionfs.server.sanitize import sanitize_archive_string
from sessionfs.server.services.merge_checks import _linked_prs
from sessionfs.server.services.personas import (
    resolve_persona,
    visible_persona_org_id,
)
from sessionfs.server.services.review_state import (
    compute_review_state,
    load_review_comments,
)
from sessionfs.server.services.rules import (
    DEFAULT_CONTEXT_SECTIONS,
    DEFAULT_KNOWLEDGE_TYPES,
    _collect_active_claims,
    get_or_create_rules,
    split_context_sections,
)
from sessionfs.server.tier_gate import build_user_context, check_project_feature

router = APIRouter(tags=["review-requests"])

# ── Constants ──────────────────────────────────────────────────────────────
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

SEVERITIES = ("critical", "high", "medium", "low", "info")
VERDICTS = ("clean", "findings", "incomplete")

_MAX_FINDINGS = 200
_MAX_FINDING_MESSAGE = 2000
_MAX_FINDING_PATH = 512
_MAX_FINDING_CLASS = 64
_MAX_REPO = 200
_MAX_PROFILE = 50
_MAX_PACKET_PATHS = 500
_MAX_PACKET_PATH = 512
# Raw ``paths`` query bound: 500 paths of 512 chars plus a separator each.
_MAX_PACKET_PATHS_RAW = _MAX_PACKET_PATHS * (_MAX_PACKET_PATH + 1)
_MAX_COVERAGE_FILES = 2000
_MAX_COVERAGE_PATH = 512
_MAX_COVERAGE_REASON = 500
_MAX_SKIP_REASON = 200
_MAX_FINISH_BODY_BYTES = 1024 * 1024
# Usage bounds on finish: BigInteger token columns, Numeric(12, 6) cost.
_MAX_USAGE_TOKENS = 10**12
_MAX_REVIEW_COST_USD = 999_999
# Pinned reviewer instructions and the packet manifest are both stored as
# JSON-as-Text; cap each at 64 KiB so a huge persona or manifest can't be
# persisted.
_MAX_INSTRUCTIONS_SNAPSHOT_BYTES = 64 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024

_DEFAULT_BUDGET_TOKENS = 200_000
_MAX_BUDGET_TOKENS = 2_000_000
_DEFAULT_BUDGET_SECONDS = 1800
_MAX_BUDGET_SECONDS = 86_400

# Lease grace: how long after ``budget_seconds`` a claim stays valid.
_LEASE_GRACE_SECONDS = 120
# A claimed request that expires this many attempts goes failed, not queued.
_MAX_ATTEMPTS = 3

# Default character budget for the ranked context in a review packet.
_DEFAULT_CONTEXT_BUDGET_CHARS = 24_000
_CONTEXT_BUDGET_FLOOR_CHARS = 2_000

# The fallback reviewer instruction when ``profile`` is ``default``.
_DEFAULT_REVIEWER_INSTRUCTION = (
    "Review the changes for correctness, security and maintainability. "
    "Report concrete, actionable findings with file paths and line numbers, "
    "and skip generated or vendored files."
)

# Statuses that are still "pending" and may be superseded by a newer head sha.
_PENDING = ("queued", "claimed")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_loads(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            422,
            {
                "code": "invalid_cursor",
                "error": "invalid_cursor",
                "message": "before is not an ISO-8601 datetime",
            },
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── DLP ────────────────────────────────────────────────────────────────────


# Findings are written by a model that read the code, so they could quote a
# secret. With no org policy at all (personal projects, keyless Community)
# secrets are still redacted.
_DEFAULT_FINDINGS_POLICY: dict = {"enabled": True, "mode": "redact", "categories": ["secrets"]}


async def _findings_dlp_policy(db: AsyncSession, user_id: str, project) -> dict:
    """Strictest union of every org the requester belongs to plus the
    project's org (block beats redact beats warn), never weaker than
    redacting secrets."""
    from sessionfs.server.db.models import OrgMember

    org_ids = set(
        (await db.execute(select(OrgMember.org_id).where(OrgMember.user_id == user_id)))
        .scalars()
        .all()
    )
    if project.org_id:
        org_ids.add(project.org_id)
    policies = [await org_dlp_policy(db, oid) for oid in sorted(org_ids)]
    policy = strictest_dlp_policy([p for p in policies if p] + [_DEFAULT_FINDINGS_POLICY])
    return policy or dict(_DEFAULT_FINDINGS_POLICY)


def _dlp_scan_text(text: str, policy: dict | None) -> str:
    """Scan a finding message under the project org's DLP policy.

    ``None`` (no enabled org policy) returns the text unchanged. BLOCK
    refuses with 422 ``dlp_blocked``; REDACT returns the redacted text; WARN
    (the default) stores the text as-is, matching the session DLP contract."""
    if policy is None:
        return text
    mode = policy.get("mode", "warn")
    categories = policy.get("categories") or ["secrets"]
    custom_patterns = policy.get("custom_patterns")
    if not isinstance(custom_patterns, list):
        custom_patterns = None
    allowlist = policy.get("allowlist")
    if not isinstance(allowlist, list):
        allowlist = None

    findings = scan_dlp_full(
        text,
        categories=categories,
        custom_patterns=custom_patterns,
        allowlist=allowlist,
    )
    findings = [f for f in findings if getattr(f, "match_text", None)]
    if not findings:
        return text
    if mode == "block":
        counts: dict[str, int] = {}
        for f in findings:
            counts[f.pattern_name] = counts.get(f.pattern_name, 0) + 1
        raise HTTPException(
            status_code=422,
            detail={
                "code": "dlp_blocked",
                "error": "dlp_blocked",
                "message": f"DLP policy blocked review: {len(findings)} finding(s)",
                "findings": counts,
            },
        )
    if mode == "redact":
        return redact_text(text, findings, custom_patterns)
    return text


def _dlp_scan_value(value: Any, policy: dict | None) -> Any:
    """DLP-scan every string nested inside ``value`` (dict/list/scalar).

    Used for the packet manifest, whose slugs/entity refs/paths are untrusted
    project text and must not bypass the requester's DLP policy."""
    if isinstance(value, str):
        return _dlp_scan_text(value, policy)
    if isinstance(value, dict):
        return {k: _dlp_scan_value(v, policy) for k, v in value.items()}
    if isinstance(value, list):
        return [_dlp_scan_value(v, policy) for v in value]
    return value


def _scan_coverage(coverage: ReviewCoverage, policy: dict | None) -> dict:
    """DLP-scan every persisted string inside a coverage block."""
    return {
        "reviewed_files": [
            _dlp_scan_text(path, policy) for path in coverage.reviewed_files
        ],
        "skipped_files": [
            {
                "path": _dlp_scan_text(sf.path, policy),
                "reason": _dlp_scan_text(sf.reason, policy),
            }
            for sf in coverage.skipped_files
        ],
        "incomplete": coverage.incomplete,
        "reason": (
            _dlp_scan_text(coverage.reason, policy)
            if coverage.reason is not None
            else None
        ),
    }


# ── Expiry sweep ───────────────────────────────────────────────────────────


async def sweep_review_requests(db: AsyncSession) -> dict[str, int]:
    """Expire claimed requests whose lease has lapsed (admin sweep + on-poll).

    A claimed row past ``lease_expires_at`` goes back to ``queued`` so a
    crashed runner's work becomes visible again, unless it already used its
    ``_MAX_ATTEMPTS`` attempts (tracked by ``lease_epoch``), in which case it
    is marked ``failed`` with ``error='expired'``. Two guarded UPDATEs; the
    commit is the caller's responsibility only in the standalone route.
    """
    now = _utcnow()
    failed = await db.execute(
        update(ReviewRequest)
        .where(
            ReviewRequest.status == "claimed",
            ReviewRequest.lease_expires_at <= now,
            ReviewRequest.lease_epoch >= _MAX_ATTEMPTS,
        )
        .values(status="failed", error="expired", completed_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    requeued = await db.execute(
        update(ReviewRequest)
        .where(
            ReviewRequest.status == "claimed",
            ReviewRequest.lease_expires_at <= now,
            or_(
                ReviewRequest.lease_epoch.is_(None),
                ReviewRequest.lease_epoch < _MAX_ATTEMPTS,
            ),
        )
        .values(
            status="queued",
            lease_runner_id=None,
            lease_expires_at=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return {"expired_failed": failed.rowcount, "requeued": requeued.rowcount}


# ── Request bodies ─────────────────────────────────────────────────────────


class ReviewFindingIn(BaseModel):
    """One finding. ``class`` is an optional slug; ``message`` is DLP-scanned."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=_MAX_FINDING_PATH)
    line: int = Field(ge=1)
    severity: Literal["critical", "high", "medium", "low", "info"]
    finding_class: str | None = Field(default=None, alias="class", max_length=_MAX_FINDING_CLASS)
    message: str = Field(min_length=1, max_length=_MAX_FINDING_MESSAGE)


class CreateReviewRequest(BaseModel):
    """Create a draft review. Either a target (repo/pr/head) or a ticket that
    has a linked PR supplies the target."""

    model_config = ConfigDict(extra="forbid")

    profile: str = Field(min_length=1, max_length=_MAX_PROFILE)
    ticket_id: str | None = None
    provider: Literal["github", "gitlab"] = "github"
    repo: str | None = Field(default=None, max_length=_MAX_REPO)
    pr_number: int | None = Field(default=None, ge=1)
    head_sha: str | None = None
    base_sha: str | None = None
    mode: Literal["draft_for_me"] = "draft_for_me"
    budget_tokens: int | None = Field(default=None, ge=1, le=_MAX_BUDGET_TOKENS)
    budget_seconds: int | None = Field(default=None, ge=1, le=_MAX_BUDGET_SECONDS)


class ClaimReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runner_id: str


class HeartbeatReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runner_id: str
    lease_epoch: int


class SkippedFile(BaseModel):
    """A file the reviewer chose to skip, with a bounded reason."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=_MAX_COVERAGE_PATH)
    reason: str = Field(min_length=1, max_length=_MAX_SKIP_REASON)


class ReviewCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewed_files: list[
        Annotated[str, Field(min_length=1, max_length=_MAX_COVERAGE_PATH)]
    ] = Field(default_factory=list, max_length=_MAX_COVERAGE_FILES)
    skipped_files: list[SkippedFile] = Field(
        default_factory=list, max_length=_MAX_COVERAGE_FILES
    )
    incomplete: bool = False
    reason: str | None = Field(default=None, max_length=_MAX_COVERAGE_REASON)


class FinishReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runner_id: str
    lease_epoch: int
    head_sha: str
    verdict: Literal["clean", "findings", "incomplete"]
    findings: list[ReviewFindingIn] = Field(default_factory=list)
    coverage: ReviewCoverage | None = None
    # Bounded so an out-of-range value is a 422, never a DB overflow 500
    # (BigInteger token columns; cost_usd is Numeric(12, 6)).
    input_tokens: int | None = Field(default=None, ge=0, le=_MAX_USAGE_TOKENS)
    output_tokens: int | None = Field(default=None, ge=0, le=_MAX_USAGE_TOKENS)
    cache_read_tokens: int | None = Field(default=None, ge=0, le=_MAX_USAGE_TOKENS)
    cache_write_tokens: int | None = Field(default=None, ge=0, le=_MAX_USAGE_TOKENS)
    cost_usd: float | None = Field(
        default=None, ge=0, le=_MAX_REVIEW_COST_USD, allow_inf_nan=False
    )
    cost_basis: Literal["reported", "estimated", "plan", "unknown"] | None = None
    error: str | None = Field(default=None, max_length=200)


# ── Serialization ──────────────────────────────────────────────────────────


def _req_summary(r: ReviewRequest) -> dict:
    return {
        "id": r.id,
        "project_id": r.project_id,
        "ticket_id": r.ticket_id,
        "provider": r.provider,
        "repo": r.repo,
        "pr_number": r.pr_number,
        "head_sha": r.head_sha,
        "base_sha": r.base_sha,
        "profile": r.profile,
        "instructions_version": r.instructions_version,
        "mode": r.mode,
        "status": r.status,
        "verdict": r.verdict,
        "findings_count": r.findings_count,
        "error": r.error,
        "created_at": _iso(r.created_at),
        "updated_at": _iso(r.updated_at),
        "completed_at": _iso(r.completed_at),
    }


def _req_dict(r: ReviewRequest) -> dict:
    return {
        **_req_summary(r),
        "requester_user_id": r.requester_user_id,
        "budget_tokens": r.budget_tokens,
        "budget_seconds": r.budget_seconds,
        "lease_epoch": r.lease_epoch,
        "lease_runner_id": r.lease_runner_id,
        "lease_expires_at": _iso(r.lease_expires_at),
        "superseded_by_id": r.superseded_by_id,
        "findings": _json_loads(r.findings, []),
        "coverage": _json_loads(r.coverage, None),
        "packet_manifest": _json_loads(r.packet_manifest, None),
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "cache_read_tokens": r.cache_read_tokens,
        "cache_write_tokens": r.cache_write_tokens,
        "cost_usd": str(r.cost_usd) if r.cost_usd is not None else None,
        "cost_basis": r.cost_basis,
    }


# ── Loading ────────────────────────────────────────────────────────────────


async def _readable_project_ids(
    db: AsyncSession, user_id: str, project_ids: set[str]
) -> set[str]:
    """The subset of ``project_ids`` the user can still read (recomputed on
    every read, so losing project access revokes access to stored drafts)."""
    from sessionfs.server.auth.project_access import user_can_access_project

    if not project_ids:
        return set()
    projects = (
        await db.execute(select(Project).where(Project.id.in_(list(project_ids))))
    ).scalars().all()
    return {p.id for p in projects if await user_can_access_project(db, user_id, p)}


async def _load_own_request(
    db: AsyncSession, request_id: str, user_id: str
) -> ReviewRequest:
    """Load a request by id; anything not owned by ``user_id`` is a 404.

    Project access is re-checked on every read: a former member can no longer
    read findings, coverage, manifests, repo or SHAs from a stored draft."""
    r = (
        await db.execute(
            select(ReviewRequest)
            .where(ReviewRequest.id == request_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if r is None or r.requester_user_id != user_id:
        raise HTTPException(404, "Review request not found")
    await _get_project_or_404(r.project_id, db, user_id)
    return r


def _path_words(paths: list[str]) -> set[str]:
    words: set[str] = set()
    for path in paths:
        for token in re.findall(r"[a-z0-9_]+", path.lower()):
            if len(token) >= 2:
                words.add(token)
    return words


def _overlap_score(text: str, words: set[str]) -> int:
    if not words:
        return 0
    lowered = text.lower()
    return sum(1 for w in words if w in lowered)


def _rank_context(
    sections: dict[str, str],
    claims: list[dict],
    path_words: set[str],
    budget_chars: int,
) -> tuple[dict[str, str], list[dict], int]:
    """Rank context sections and KB claims by path-word overlap, within a
    character budget. Returns the included (sections, claims, chars used)."""
    ranked_sections = sorted(
        sections.items(),
        key=lambda kv: (-_overlap_score(kv[0] + "\n" + kv[1], path_words), kv[0]),
    )
    ranked_claims = sorted(
        claims,
        key=lambda c: (
            -_overlap_score(
                f"{c.get('entity_ref') or ''}\n{c.get('content', '')}", path_words
            ),
            c.get("entity_ref") or "",
        ),
    )
    used = 0
    out_sections: dict[str, str] = {}
    for slug, body in ranked_sections:
        if used + len(body) > budget_chars:
            continue
        out_sections[slug] = body
        used += len(body)
    out_claims: list[dict] = []
    for claim in ranked_claims:
        size = len(claim.get("content", "")) + len(claim.get("entity_ref") or "")
        if used + size > budget_chars:
            continue
        out_claims.append(claim)
        used += size
    return out_sections, out_claims, used


# ── Packet assembly ────────────────────────────────────────────────────────


def _block(source: str, trust: str, content) -> dict[str, Any]:
    """A packet section labelled with its provenance and trust level."""
    return {"source": source, "trust": trust, "content": content}


async def _build_packet(
    db: AsyncSession,
    project,
    request: ReviewRequest,
    paths: list[str],
    policy: dict | None,
) -> tuple[dict, dict]:
    """Assemble the review packet and its manifest.

    The packet is the context the runner's reviewer needs; the manifest is a
    compact JSON description of what went into it (stored on the row so the
    requester can see what a draft was produced against). Every manifest
    string is DLP-scanned under ``policy`` and the serialized manifest is
    capped at ``_MAX_MANIFEST_BYTES``."""
    rules = await get_or_create_rules(db, project, request.requester_user_id)

    checklist_rows = (
        await db.execute(
            select(LoopChecklistItem)
            .where(
                LoopChecklistItem.project_id == project.id,
                LoopChecklistItem.status == "active",
            )
            .order_by(LoopChecklistItem.slug)
            .limit(_MAX_CHECKLIST_ITEMS)
        )
    ).scalars().all()
    # Bounded like every other variable section: at most
    # _MAX_CHECKLIST_ITEMS rows and _MAX_CHECKLIST_CHARS of text in total.
    checklist: list[dict] = []
    checklist_chars = 0
    for c in checklist_rows:
        text_ = (c.check_text or "")[:_MAX_CHECKLIST_ITEM_CHARS]
        if checklist_chars + len(text_) > _MAX_CHECKLIST_CHARS:
            break
        checklist_chars += len(text_)
        checklist.append({"slug": c.slug, "title": (c.title or "")[:200], "check_text": text_})

    knowledge_claims: list[dict] = []
    if rules.include_knowledge:
        wanted_types = _json_loads(
            rules.knowledge_types, list(DEFAULT_KNOWLEDGE_TYPES)
        )
        knowledge_claims = [
            {"entry_type": c.entry_type, "content": c.content, "entity_ref": c.entity_ref}
            for c in await _collect_active_claims(db, project.id, wanted_types)
        ]

    context_sections: dict[str, str] = {}
    if rules.include_context:
        wanted_sections = _json_loads(
            rules.context_sections, list(DEFAULT_CONTEXT_SECTIONS)
        )
        all_sections = split_context_sections(project.context_document or "")
        context_sections = {
            slug: body for slug, body in all_sections.items() if slug in wanted_sections
        }

    budget_chars = _DEFAULT_CONTEXT_BUDGET_CHARS
    if request.budget_tokens is not None:
        budget_chars = min(
            _DEFAULT_CONTEXT_BUDGET_CHARS,
            max(_CONTEXT_BUDGET_FLOOR_CHARS, request.budget_tokens * 4),
        )
    path_words = _path_words(paths)
    ranked_sections, ranked_claims, chars_used = _rank_context(
        context_sections, knowledge_claims, path_words, budget_chars
    )

    # Ticket metadata + open findings from the linked ticket's review thread.
    # The ticket is bound to THIS request's project: a stored ticket_id from a
    # different project must never be loaded into the packet.
    ticket_block: dict[str, Any] | None = None
    review_state: dict[str, Any] | None = None
    if request.ticket_id:
        ticket = (
            await db.execute(
                select(Ticket).where(
                    Ticket.id == request.ticket_id,
                    Ticket.project_id == project.id,
                )
            )
        ).scalar_one_or_none()
        if ticket is not None:
            ticket_block = {
                "id": ticket.id,
                "title": ticket.title,
                "status": ticket.status,
                "description": ticket.description,
                "acceptance_criteria": _json_loads(ticket.acceptance_criteria, []),
            }
            comments, _truncated = await load_review_comments(db, ticket.id)
            state = compute_review_state(comments)
            if state is not None:
                review_state = {
                    "open_findings": [f.to_dict() for f in state.open_findings],
                    "last_verdict": state.last_verdict,
                }

    # Reviewer persona text, pinned at create time in ``instructions_snapshot``
    # (DLP-scanned, sha256-summed). The packet serves the pinned snapshot, so a
    # persona edit after the request never changes what the runner reviews
    # against. Rows from before the snapshot column existed (only direct test
    # inserts; migration 139 is unreleased) fall back to resolving the persona
    # now.
    persona_block = _json_loads(request.instructions_snapshot, None)
    if not (isinstance(persona_block, dict) and persona_block.get("name")):
        persona_block = await _resolve_instructions(
            db, project, request.profile, user_id=request.requester_user_id
        )

    # Every packet section is labelled with its source and trust level so the
    # runner can neutralize untrusted text before it reaches the model. Only
    # the pinned reviewer instructions and fixed SessionFS text are trusted.
    packet = {
        "request": _block("request", "untrusted", _req_dict(request)),
        "project": _block(
            "project",
            "untrusted",
            {
                "id": project.id,
                "name": getattr(project, "name", None),
                "git_remote": getattr(project, "git_remote", None),
            },
        ),
        "ticket": _block("ticket", "untrusted", ticket_block),
        "review_state": _block("review_comments", "untrusted", review_state),
        "static_rules": _block("project_rules", "untrusted", rules.static_rules or ""),
        "checklist": _block("checklist", "untrusted", checklist),
        "context_sections": _block("context_document", "untrusted", ranked_sections),
        "knowledge_claims": _block("knowledge_base", "untrusted", ranked_claims),
        "persona": _block("persona", "trusted", persona_block),
        "instructions_version": _block(
            "sessionfs", "trusted", request.instructions_version
        ),
        "changed_paths": _block("client", "untrusted", paths),
    }
    manifest = {
        "ticket": ticket_block["id"] if ticket_block else None,
        "static_rules": bool(rules.static_rules),
        "checklist": [c["slug"] for c in checklist],
        "context_sections": sorted(ranked_sections),
        "knowledge_claims": [c.get("entity_ref") for c in ranked_claims],
        "open_findings": review_state is not None,
        "persona": persona_block["name"],
        "persona_version": persona_block["version"],
        "changed_paths": paths,
        "context_budget_chars": budget_chars,
        "context_chars_used": chars_used,
    }
    # Every manifest string is untrusted project text and must not bypass the
    # requester's DLP policy (a secret in a checklist slug, a context heading
    # or a KB entity ref would otherwise be stored in the clear).
    manifest = _dlp_scan_value(manifest, policy)
    manifest = _cap_manifest(manifest)
    return packet, manifest


# ── Endpoints ──────────────────────────────────────────────────────────────


async def _resolve_instructions(
    db, project, profile: str, *, user_id: str
) -> dict[str, Any]:
    """Resolve ``profile`` (a persona name or ``default``) to the pinned persona
    block ``{name, role, content, version}``. ``default`` needs no persona row."""
    if profile == "default":
        return {
            "name": "default",
            "role": None,
            "content": _DEFAULT_REVIEWER_INSTRUCTION,
            "version": 1,
        }
    org_id = await visible_persona_org_id(
        db, project, user_id=user_id, key_kind="user"
    )
    persona = await resolve_persona(db, project.id, profile, org_id=org_id)
    if persona is None:
        raise HTTPException(
            404,
            {
                "code": "persona_not_found",
                "error": "persona_not_found",
                "message": f"No active reviewer persona named {profile!r}",
            },
        )
    return {
        "name": persona.name,
        "role": persona.role,
        "content": persona.content,
        "version": persona.version,
    }


_MAX_CHECKLIST_ITEMS = 100
_MAX_CHECKLIST_ITEM_CHARS = 1000
_MAX_CHECKLIST_CHARS = 20_000


def _cap_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Cap the serialized manifest at ``_MAX_MANIFEST_BYTES``.

    ``changed_paths`` is the only unbounded field (each path ≤ 512 chars), so
    trailing paths are dropped until the JSON fits. Other fields are small and
    individually bounded, so a manifest without paths always fits."""
    if len(json.dumps(manifest).encode("utf-8")) <= _MAX_MANIFEST_BYTES:
        return manifest
    # Trim every list in the manifest from the end, longest first, until the
    # JSON fits; a manifest that still doesn't fit keeps only its scalars.
    while len(json.dumps(manifest).encode("utf-8")) > _MAX_MANIFEST_BYTES:
        lists = [(k, v) for k, v in manifest.items() if isinstance(v, list) and v]
        if not lists:
            break
        key, longest = max(lists, key=lambda kv: len(json.dumps(kv[1])))
        longest.pop()
    if len(json.dumps(manifest).encode("utf-8")) > _MAX_MANIFEST_BYTES:
        manifest = {k: v for k, v in manifest.items() if not isinstance(v, (list, dict))}
        manifest["truncated"] = True
    return manifest


@router.post("/api/v1/projects/{project_id}/review-requests")
async def create_review_request(
    project_id: str,
    body: CreateReviewRequest,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create (or reuse) a personal review request.

    The idempotency key is ``(project_id, requester_user_id, provider, repo,
    head_sha, profile, instructions_version, mode, COALESCE(pr_number, 0))``:
    requesting the same target twice returns the same live row, and two
    distinct PRs sharing a head sha are distinct targets. A new head for the
    same (provider, repo, pr, profile) supersedes older ``queued``/``claimed``
    rows atomically."""
    ctx = await build_user_context(user, db)
    project = await _get_project_or_404(project_id, db, user.id)
    await check_project_feature(ctx, "review_requests", project, db)

    provider, repo, pr_number, head_sha, base_sha = (
        body.provider,
        body.repo,
        body.pr_number,
        body.head_sha,
        body.base_sha,
    )

    # Target: explicit target, else the ticket's newest linked PR. A mix of
    # both is ambiguous and refused — the ticket must be validated against
    # THIS project, never stored blind alongside an explicit target.
    explicit = head_sha is not None and repo is not None and pr_number is not None
    if explicit and body.ticket_id:
        raise HTTPException(
            422,
            {
                "code": "ambiguous_target",
                "error": "ambiguous_target",
                "message": "Provide either repo/pr_number/head_sha or a ticket_id, not both",
            },
        )

    ticket_id: str | None = None
    if explicit:
        repo, _ = sanitize_archive_string(repo)
    elif body.ticket_id:
        ticket_id = body.ticket_id
        ticket = await _get_ticket_or_404(project.id, ticket_id, db)
        prs = await _linked_prs(db, ticket.id, project.id)
        if not prs or not prs[0].head_sha:
            raise HTTPException(
                422,
                {
                    "code": "no_linked_pr",
                    "error": "no_linked_pr",
                    "message": "This ticket has no linked pull request with a head sha",
                },
            )
        pr = prs[0]
        provider = pr.provider
        repo = pr.repo
        pr_number = pr.number
        head_sha = pr.head_sha
        base_sha = base_sha or None
    else:
        raise HTTPException(
            422,
            {
                "code": "missing_target",
                "error": "missing_target",
                "message": "Provide repo, pr_number and head_sha, or a ticket_id with a linked PR",
            },
        )

    if not _SHA_RE.match(head_sha):
        raise HTTPException(
            422,
            {
                "code": "invalid_head_sha",
                "error": "invalid_head_sha",
                "message": "head_sha must be a full 40- or 64-char SHA",
            },
        )
    if base_sha is not None and not _SHA_RE.match(base_sha):
        raise HTTPException(
            422,
            {
                "code": "invalid_base_sha",
                "error": "invalid_base_sha",
                "message": "base_sha must be a full 40- or 64-char SHA",
            },
        )

    # Serialize by project scope, then re-read and insert, so concurrent
    # creates of the same target resolve to one row and a new head reliably
    # supersedes the old pending row instead of racing.
    from sessionfs.server.services.scope_locks import lock_project_scope_authorized

    await lock_project_scope_authorized(
        db, project, user_id=user.id, ctx=ctx, feature="review_requests"
    )

    instructions = await _resolve_instructions(
        db, project, body.profile, user_id=user.id
    )
    profile = instructions["name"]
    instructions_version = instructions["version"]

    # Pin the reviewer instructions now: DLP-scan the persona content under
    # the requester's policy, cap it, and store the block + its sha256 so the
    # packet serves the exact text the request was created against.
    policy = await _findings_dlp_policy(db, user.id, project)
    instructions = {
        k: (_dlp_scan_text(v, policy) if isinstance(v, str) else v)
        for k, v in instructions.items()
    }
    snapshot = json.dumps(instructions)
    if len(snapshot.encode("utf-8")) > _MAX_INSTRUCTIONS_SNAPSHOT_BYTES:
        raise HTTPException(
            422,
            {
                "code": "instructions_too_large",
                "error": "instructions_too_large",
                "message": "Reviewer instructions are too large (64 KiB max)",
            },
        )
    snapshot_sha = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()

    existing = (
        await db.execute(
            select(ReviewRequest).where(
                ReviewRequest.project_id == project.id,
                ReviewRequest.requester_user_id == user.id,
                ReviewRequest.provider == provider,
                ReviewRequest.repo == repo,
                ReviewRequest.pr_number == pr_number,
                ReviewRequest.head_sha == head_sha,
                ReviewRequest.profile == profile,
                ReviewRequest.instructions_version == instructions_version,
                ReviewRequest.mode == body.mode,
                ReviewRequest.status != "superseded",
            )
        )
    ).scalar_one_or_none()

    if existing is not None and existing.status != "superseded":
        await _supersede_others(
            db,
            project.id,
            provider,
            repo,
            pr_number,
            profile,
            requester_user_id=user.id,
            exclude=existing.id,
        )
        await db.commit()
        await db.refresh(existing)
        response.status_code = 200
        return _req_dict(existing)

    request = ReviewRequest(
        id=f"rvq_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        requester_user_id=user.id,
        ticket_id=ticket_id,
        provider=provider,
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        base_sha=base_sha,
        profile=profile,
        instructions_version=instructions_version,
        instructions_snapshot=snapshot,
        instructions_sha256=snapshot_sha,
        mode="draft_for_me",
        budget_tokens=body.budget_tokens or _DEFAULT_BUDGET_TOKENS,
        budget_seconds=body.budget_seconds or _DEFAULT_BUDGET_SECONDS,
        status="queued",
    )
    db.add(request)
    await _supersede_others(
        db,
        project.id,
        provider,
        repo,
        pr_number,
        profile,
        requester_user_id=user.id,
        exclude=request.id,
    )
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent create with the same idempotency key won the race
        # (defensive: the project scope lock normally serializes this). Return
        # the winning live row.
        await db.rollback()
        winner = (
            await db.execute(
                select(ReviewRequest).where(
                    ReviewRequest.project_id == project.id,
                    ReviewRequest.requester_user_id == user.id,
                    ReviewRequest.provider == provider,
                    ReviewRequest.repo == repo,
                    ReviewRequest.pr_number == pr_number,
                    ReviewRequest.head_sha == head_sha,
                    ReviewRequest.profile == profile,
                    ReviewRequest.instructions_version == instructions_version,
                    ReviewRequest.mode == body.mode,
                    ReviewRequest.status != "superseded",
                )
            )
        ).scalar_one_or_none()
        if winner is None:
            raise HTTPException(409, "Review request already exists")
        response.status_code = 200
        return _req_dict(winner)
    await db.refresh(request)
    response.status_code = 201
    return _req_dict(request)


async def _supersede_others(
    db: AsyncSession,
    project_id: str,
    provider: str,
    repo: str,
    pr_number: int | None,
    profile: str,
    *,
    requester_user_id: str,
    exclude: str | None,
) -> None:
    """Mark older pending rows for the same (requester, provider, repo, pr,
    profile) superseded and point them at the new row. The requester is part
    of the guarded update: one user can never supersede another user's
    request. Pr-less rows (ticket-only) supersede by provider, repo and
    profile only."""
    stmt = (
        update(ReviewRequest)
        .where(
            ReviewRequest.project_id == project_id,
            ReviewRequest.requester_user_id == requester_user_id,
            ReviewRequest.provider == provider,
            ReviewRequest.repo == repo,
            ReviewRequest.profile == profile,
            ReviewRequest.mode == "draft_for_me",
            ReviewRequest.status.in_(_PENDING),
        )
        .values(status="superseded", superseded_by_id=exclude, updated_at=_utcnow())
    )
    if pr_number is not None:
        stmt = stmt.where(ReviewRequest.pr_number == pr_number)
    else:
        stmt = stmt.where(ReviewRequest.pr_number.is_(None))
    if exclude is not None:
        stmt = stmt.where(ReviewRequest.id != exclude)
    await db.execute(stmt.execution_options(synchronize_session=False))


@router.get("/api/v1/me/review-requests")
async def list_review_requests(
    limit: int = Query(50, ge=1, le=100),
    before: str | None = Query(None),
    before_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """My review requests, newest first, keyset-paged on (created_at, id).

    Rows in projects the requester can no longer read are hidden — losing
    project access revokes access to stored drafts."""
    candidate_ids = set(
        (
            await db.execute(
                select(ReviewRequest.project_id).where(
                    ReviewRequest.requester_user_id == user.id
                )
            )
        ).scalars().all()
    )
    readable = await _readable_project_ids(db, user.id, candidate_ids)
    stmt = select(ReviewRequest).where(
        ReviewRequest.requester_user_id == user.id,
        ReviewRequest.project_id.in_(readable),
    )
    dt = _parse_dt(before)
    if dt is not None and before_id:
        stmt = stmt.where(
            or_(
                ReviewRequest.created_at < dt,
                (ReviewRequest.created_at == dt) & (ReviewRequest.id < before_id),
            )
        )
    stmt = stmt.order_by(
        ReviewRequest.created_at.desc(), ReviewRequest.id.desc()
    ).limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [_req_summary(r) for r in rows],
        "has_more": has_more,
        "next_before": _iso(rows[-1].created_at) if rows else None,
        "next_before_id": rows[-1].id if rows else None,
    }


@router.get("/api/v1/review-requests/{request_id}")
async def get_review_request(
    request_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    r = await _load_own_request(db, request_id, user.id)
    return _req_dict(r)


@router.get("/api/v1/review-requests/{request_id}/packet")
async def get_review_packet(
    request_id: str,
    runner_id: str = Query(..., min_length=1),
    lease_epoch: int = Query(..., ge=1),
    paths: str = Query(""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The review packet, fenced on the caller's runner and lease.

    ``paths`` is a comma-separated list of changed paths (capped at 500);
    context sections and KB claims are ranked by overlap with those paths."""
    r = await _load_own_request(db, request_id, user.id)

    # The packet is only served to the active runner that currently holds the
    # lease, and only while that runner is enrolled for the request's project.
    runner_row = (
        await db.execute(
            select(ReviewRunner).where(
                ReviewRunner.id == runner_id,
                ReviewRunner.owner_user_id == user.id,
                ReviewRunner.status == "active",
            )
        )
    ).scalar_one_or_none()
    if runner_row is None:
        raise HTTPException(404, "Runner not found")
    if r.project_id not in _json_loads(runner_row.project_ids, []):
        raise HTTPException(
            403,
            {
                "code": "runner_project_not_allowed",
                "error": "runner_project_not_allowed",
                "message": "This runner is not enrolled for the request's project",
            },
        )

    now = _utcnow()
    lease_expires = r.lease_expires_at
    # SQLite returns a naive datetime for a timezone-aware column; normalise it
    # before the Python-side comparison against the aware ``now``.
    if lease_expires is not None and lease_expires.tzinfo is None:
        lease_expires = lease_expires.replace(tzinfo=timezone.utc)
    if (
        r.status != "claimed"
        or r.lease_runner_id != runner_id
        or r.lease_epoch != lease_epoch
        or lease_expires is None
        or lease_expires <= now
    ):
        raise HTTPException(
            409,
            {
                "code": "lease_superseded",
                "error": "lease_superseded",
                "message": "This claim is stale: claim the request first",
            },
        )

    # Refuse an over-long raw ``paths`` query before splitting it, so a client
    # can't make the server split an unbounded string.
    if len(paths) > _MAX_PACKET_PATHS_RAW:
        raise HTTPException(
            422,
            {
                "code": "paths_too_long",
                "error": "paths_too_long",
                "message": f"paths must be at most {_MAX_PACKET_PATHS_RAW} characters",
            },
        )

    changed_paths = [
        p.strip()[:_MAX_PACKET_PATH] for p in paths.split(",") if p.strip()
    ]
    changed_paths = changed_paths[:_MAX_PACKET_PATHS]

    project = await _get_project_or_404(r.project_id, db, user.id)
    policy = await _findings_dlp_policy(db, user.id, project)
    changed_paths = [_dlp_scan_text(p, policy) for p in changed_paths]
    packet, manifest = await _build_packet(db, project, r, changed_paths, policy)

    # Record the manifest on the row (guarded: only while the runner still
    # holds an unexpired lease). A lost race means the lease moved while the
    # packet was being built — answer 409 and store nothing.
    result = await db.execute(
        update(ReviewRequest)
        .where(
            ReviewRequest.id == r.id,
            ReviewRequest.status == "claimed",
            ReviewRequest.lease_runner_id == runner_id,
            ReviewRequest.lease_epoch == lease_epoch,
            ReviewRequest.lease_expires_at > now,
        )
        .values(packet_manifest=json.dumps(manifest), updated_at=_utcnow())
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if result.rowcount != 1:
        raise HTTPException(
            409,
            {
                "code": "lease_superseded",
                "error": "lease_superseded",
                "message": "This claim is stale: the request was re-claimed or the lease expired",
            },
        )
    return {"request_id": r.id, "packet": packet, "manifest": manifest}


@router.post("/api/v1/review-requests/{request_id}/claim")
async def claim_review_request(
    request_id: str,
    body: ClaimReviewRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Atomically claim a queued (or expired-lease) request for a runner.

    The guarded UPDATE returns rowcount 1 only when the row is still pending
    and unleased; a concurrent claim (or a finish) leaves rowcount 0 → 409."""
    r = await _load_own_request(db, request_id, user.id)
    # Re-check the requester can still read the project.
    await _get_project_or_404(r.project_id, db, user.id)
    runner_row = (
        await db.execute(
            select(ReviewRunner).where(
                ReviewRunner.id == body.runner_id,
                ReviewRunner.owner_user_id == user.id,
                ReviewRunner.status == "active",
            )
        )
    ).scalar_one_or_none()
    if runner_row is None:
        raise HTTPException(404, "Runner not found")
    if r.project_id not in _json_loads(runner_row.project_ids, []):
        raise HTTPException(
            403,
            {
                "code": "runner_project_not_allowed",
                "error": "runner_project_not_allowed",
                "message": "This runner is not enrolled for the request's project",
            },
        )

    await sweep_review_requests(db)

    now = _utcnow()
    lease_seconds = (r.budget_seconds or _DEFAULT_BUDGET_SECONDS) + _LEASE_GRACE_SECONDS
    result = await db.execute(
        update(ReviewRequest)
        .where(
            ReviewRequest.id == request_id,
            ReviewRequest.requester_user_id == user.id,
            ReviewRequest.status.in_(["queued", "claimed"]),
            or_(
                ReviewRequest.lease_expires_at.is_(None),
                ReviewRequest.lease_expires_at <= now,
            ),
        )
        .values(
            status="claimed",
            lease_runner_id=body.runner_id,
            lease_epoch=func.coalesce(ReviewRequest.lease_epoch, 0) + 1,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if result.rowcount != 1:
        raise HTTPException(
            409,
            {
                "code": "lease_conflict",
                "error": "lease_conflict",
                "message": "Request is no longer claimable (already claimed, finished or superseded)",
            },
        )
    r = await _load_own_request(db, request_id, user.id)
    return _req_dict(r)


@router.post("/api/v1/review-requests/{request_id}/heartbeat")
async def heartbeat_review_request(
    request_id: str,
    body: HeartbeatReviewRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Extend the lease of a claim the caller's runner holds."""
    r = await _load_own_request(db, request_id, user.id)
    now = _utcnow()
    lease_seconds = (r.budget_seconds or _DEFAULT_BUDGET_SECONDS) + _LEASE_GRACE_SECONDS
    result = await db.execute(
        update(ReviewRequest)
        .where(
            ReviewRequest.id == request_id,
            ReviewRequest.requester_user_id == user.id,
            ReviewRequest.status == "claimed",
            ReviewRequest.lease_runner_id == body.runner_id,
            ReviewRequest.lease_epoch == body.lease_epoch,
            ReviewRequest.lease_expires_at > now,
        )
        .values(
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if result.rowcount != 1:
        raise HTTPException(
            409,
            {
                "code": "lease_superseded",
                "error": "lease_superseded",
                "message": "This claim is stale: the request was re-claimed or finished",
            },
        )
    r = await _load_own_request(db, request_id, user.id)
    return _req_dict(r)


def _inline_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve ``$defs``/``$ref`` into a self-contained schema.

    ``raw_json_body_openapi`` rejects models with nested models (their
    ``$defs`` can't be inlined), but ``FinishReviewRequest`` has nested models
    and the finish body is now read manually. Inlining here keeps the
    request-body schema self-contained so the OpenAPI snapshot has no dangling
    ``$ref`` after the typed ``body`` parameter is gone."""
    defs = schema.get("$defs") or {}

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = defs.get(ref[len("#/$defs/"):], {})
                resolved = resolve(target) if isinstance(target, dict) else {}
                siblings = {k: resolve(v) for k, v in node.items() if k != "$ref"}
                return {**resolved, **siblings}
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    out = resolve(schema)
    out.pop("$defs", None)
    return out


async def _read_finish_body(request: Request) -> FinishReviewRequest:
    """Stream the finish body, refusing (413) past 1 MiB, then validate it.

    The body is read directly from ``request.stream()`` so a chunked body is
    never buffered beyond the cap before pydantic sees it (the meetings/chat
    upload pattern)."""
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > _MAX_FINISH_BODY_BYTES:
                raise HTTPException(
                    413,
                    {
                        "code": "finish_too_large",
                        "error": "finish_too_large",
                        "message": "finish body must be at most 1 MiB",
                    },
                )
        except ValueError:
            pass
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_FINISH_BODY_BYTES:
            raise HTTPException(
                413,
                {
                    "code": "finish_too_large",
                    "error": "finish_too_large",
                    "message": "finish body must be at most 1 MiB",
                },
            )
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        raise HTTPException(422, "Request body must be JSON")
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(422, "Request body must be JSON") from None
    if not isinstance(data, dict):
        raise HTTPException(422, "Request body must be a JSON object")
    try:
        return FinishReviewRequest.model_validate(data)
    except ValidationError as exc:
        # Drop ``input``/``ctx`` so a rejected body never echoes submitted
        # values back to the caller.
        sanitized = [
            {k: v for k, v in err.items() if k not in ("input", "ctx")}
            for err in exc.errors()
        ]
        raise HTTPException(
            422,
            {
                "code": "validation_error",
                "error": "validation_error",
                "message": "Invalid finish body",
                "errors": sanitized,
            },
        ) from exc


@router.post(
    "/api/v1/review-requests/{request_id}/finish",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": _inline_schema(FinishReviewRequest.model_json_schema()),
                }
            },
        },
        "responses": {
            "422": {
                "description": "Validation Error",
                "content": {
                    "application/json": {
                        "schema": {
                            "$ref": "#/components/schemas/HTTPValidationError"
                        }
                    }
                },
            },
        },
    },
)
async def finish_review_request(
    request_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Finish a claimed request with findings and usage.

    Fenced on (runner, lease_epoch, head_sha, expiry): a wrong head is a 409
    ``head_mismatch``; a stale epoch, wrong runner or expired lease is a 409
    ``lease_superseded``. Either way nothing is stored."""
    body = await _read_finish_body(request)
    if not _SHA_RE.match(body.head_sha):
        raise HTTPException(
            422,
            {
                "code": "invalid_head_sha",
                "error": "invalid_head_sha",
                "message": "head_sha must be a full 40- or 64-char SHA",
            },
        )
    if len(body.findings) > _MAX_FINDINGS:
        raise HTTPException(
            422,
            {
                "code": "too_many_findings",
                "error": "too_many_findings",
                "message": f"At most {_MAX_FINDINGS} findings per review",
            },
        )

    r = await _load_own_request(db, request_id, user.id)
    if r.head_sha != body.head_sha:
        raise HTTPException(
            409,
            {
                "code": "head_mismatch",
                "error": "head_mismatch",
                "message": "head_sha does not match this review request",
            },
        )

    project = await _get_project_or_404(r.project_id, db, user.id)
    policy = await _findings_dlp_policy(db, user.id, project)

    stored_findings: list[dict] = []
    for f in body.findings:
        if f.finding_class is not None and not _SLUG_RE.match(f.finding_class):
            raise HTTPException(
                422,
                {
                    "code": "invalid_finding_class",
                    "error": "invalid_finding_class",
                    "message": f"Bad finding class {f.finding_class!r}",
                },
            )
        message = _dlp_scan_text(f.message, policy)
        path = _dlp_scan_text(f.path, policy)
        stored_findings.append(
            {
                "path": path,
                "line": f.line,
                "severity": f.severity,
                "class": _dlp_scan_text(f.finding_class, policy) if f.finding_class else None,
                "message": message,
            }
        )

    status = "done" if body.verdict != "incomplete" else "failed"
    error = body.error or ("incomplete" if body.verdict == "incomplete" else None)
    if error is not None:
        error, _ = sanitize_archive_string(error)
        error = _dlp_scan_text(error, policy)
    coverage = (
        _scan_coverage(body.coverage, policy)
        if body.coverage is not None
        else None
    )
    cost_usd = None
    if body.cost_usd is not None:
        cost_usd = Decimal(str(body.cost_usd)).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )

    now = _utcnow()
    result = await db.execute(
        update(ReviewRequest)
        .where(
            ReviewRequest.id == request_id,
            ReviewRequest.requester_user_id == user.id,
            ReviewRequest.status == "claimed",
            ReviewRequest.lease_runner_id == body.runner_id,
            ReviewRequest.lease_epoch == body.lease_epoch,
            ReviewRequest.lease_expires_at > now,
            ReviewRequest.head_sha == body.head_sha,
        )
        .values(
            status=status,
            verdict=body.verdict,
            findings=json.dumps(stored_findings),
            findings_count=len(stored_findings),
            coverage=json.dumps(coverage) if coverage is not None else None,
            completed_at=now,
            input_tokens=body.input_tokens,
            output_tokens=body.output_tokens,
            cache_read_tokens=body.cache_read_tokens,
            cache_write_tokens=body.cache_write_tokens,
            cost_usd=cost_usd,
            cost_basis=body.cost_basis,
            error=error,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if result.rowcount != 1:
        raise HTTPException(
            409,
            {
                "code": "lease_superseded",
                "error": "lease_superseded",
                "message": "This claim is stale: the request was re-claimed or the lease expired",
            },
        )
    r = await _load_own_request(db, request_id, user.id)
    return _req_dict(r)

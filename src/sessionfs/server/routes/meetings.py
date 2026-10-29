"""Meeting transcripts (GitHub #116, M1a): upload, list, get, delete,
participant resolution and manual override.

Only the stored-text surface lives here — extraction (§5) and promote (§7)
are M1b. Access follows §3:

- project meetings — the project predicates (read / edit / admin), with
  service keys admitted via ``meetings:read`` / ``meetings:write`` and the
  usual project allowlist;
- group meetings — user keys only, gated on the caller's group grant
  (reader = visible, editor/admin = writable), and the ``project_groups``
  feature (Team+).

The transcript body is streamed with a hard cap, normalised, then DLP
redacted under the strictest union of the caller's orgs and the scope org
before it is stored — the only stored copy is the redacted text.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.security.secrets import scan_dlp as scan_dlp_full
from sessionfs.server.auth.dependencies import (
    AuthContext,
    assert_service_key_can_access_project,
    get_current_user,
    require_scope,
)
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    MeetingActionItem,
    MeetingParticipant,
    MeetingTranscript,
    OrgAuditEvent,
    Organization,
    OrgMember,
    Project,
    ProjectGroup,
    User,
)
from sessionfs.server.dlp import (
    DEFAULT_DLP_POLICY,
    get_org_dlp_policy,
    redact_text,
    well_formed_custom_patterns,
)
from sessionfs.server.helper.redaction import union_dlp_policies
from sessionfs.server.sanitize import sanitize_archive_string
from sessionfs.server.services.personas import (
    find_org_persona,
    resolve_persona,
    visible_persona_org_id_for_auth,
)
from sessionfs.server.services.ticket_writes import resolve_assigned_user_id

router = APIRouter(prefix="/api/v1", tags=["meetings"])
project_router = APIRouter(prefix="/api/v1/projects", tags=["meetings"])
group_router = APIRouter(prefix="/api/v1/groups", tags=["meetings"])


# Streamed body cap (1 MiB), normalised content cap (512 KiB) and line cap.
_MAX_BODY_BYTES = 1024 * 1024
_MAX_CONTENT_BYTES = 512 * 1024
_MAX_LINES = 10_000

_PARTICIPANT_NOT_ELIGIBLE = {
    "error": "participant_not_eligible",
    "message": "Participant must be an active org member or an org persona in the meeting's scope.",
}


class MeetingCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str
    meeting_at: datetime | None = None
    duration_minutes: int | None = Field(None, ge=0)
    source: Literal["manual_paste", "file_upload"] = "manual_paste"
    extract: bool = False


class ParticipantOverride(BaseModel):
    user_id: str | None = None
    persona_name: str | None = None


class MeetingParticipantResponse(BaseModel):
    id: str
    name_raw: str
    email_raw: str | None
    user_id: str | None
    persona_name: str | None
    resolution_method: str
    resolved_by_user_id: str | None


class MeetingActionItemResponse(BaseModel):
    id: str
    kind: str
    description: str
    owner_participant_id: str | None
    due_date: date | None
    line_start: int
    line_end: int
    status: str
    promoted_ticket_id: str | None
    extracted_at: datetime | None


class MeetingSummaryResponse(BaseModel):
    id: str
    org_id: str | None
    project_id: str | None
    group_id: str | None
    title: str
    meeting_at: datetime | None
    duration_minutes: int | None
    source: str
    line_count: int
    dlp_findings: int
    extraction_status: str
    # The classified failure code of the last extraction (never provider text).
    extraction_error: str | None = None
    # List responses only: per-meeting counts from one grouped query.
    pending_action_count: int | None = None
    decision_count: int | None = None
    created_by_user_id: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class MeetingDetailResponse(MeetingSummaryResponse):
    content: str
    content_sha256: str
    participants: list[MeetingParticipantResponse] = []
    action_items: list[MeetingActionItemResponse] = []
    warnings: list[str] = []


class MeetingListResponse(BaseModel):
    meetings: list[MeetingSummaryResponse]
    next_cursor: str | None = None


# ── Normalisation + DLP ──


# A VTT/SRT cue timing line: ``[hh:]mm:ss.ttt --> [hh:]mm:ss.ttt`` (SRT uses a
# comma before the milliseconds), optionally followed by VTT cue settings. Any
# other line with an arrow in it is ordinary transcript text.
_CUE_TIMESTAMP = r"(?:\d{1,3}:)?\d{1,2}:\d{2}[.,]\d{1,3}"
_CUE_TIMING_RE = re.compile(rf"^{_CUE_TIMESTAMP}\s+-->\s+{_CUE_TIMESTAMP}(?:\s.*)?$")
# Opening voice span: ``<v Speaker>`` or ``<v.class Speaker>``; the closing
# ``</v>`` is optional in WebVTT.
_CUE_VOICE_RE = re.compile(r"^<v(?:\.[^\s>]*)?\s+([^>]*)>(.*)$")
_CUE_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")
# The WebVTT signature: ``WEBVTT`` alone or followed by a space or tab.
_VTT_SIGNATURE_RE = re.compile(r"^WEBVTT(?:[ \t].*)?$")


def _is_cue_timing(line: str) -> bool:
    return _CUE_TIMING_RE.match(line.strip()) is not None


def _cue_voice_line(line: str) -> str:
    """Convert a ``<v Speaker>text`` VTT cue line to ``Speaker: text``.

    Any other cue markup (classes, italics, nested voice spans) is removed so
    only the spoken text is stored.
    """
    stripped = line.strip()
    match = _CUE_VOICE_RE.match(stripped)
    if match:
        speaker = _CUE_TAG_RE.sub("", match.group(1)).strip()
        text = _CUE_TAG_RE.sub("", match.group(2)).strip()
        return f"{speaker}: {text}" if speaker else text
    return _CUE_TAG_RE.sub("", stripped).strip()


def _merge_consecutive(lines: list[str]) -> list[str]:
    """Collapse consecutive identical cue text into one line."""
    merged: list[str] = []
    for line in lines:
        if merged and merged[-1] == line:
            continue
        merged.append(line)
    return merged


def _strip_vtt(lines: list[str]) -> str:
    """VTT → ``Speaker: text`` lines: drop the header, NOTE/STYLE blocks, cue
    identifiers and timing lines, keep voice-tagged cue text."""
    out: list[str] = []
    i = 0
    n = len(lines)
    # Header: the WEBVTT line plus any metadata, through the first blank line.
    # A file missing that blank line must not lose its cues, so the header
    # also ends at the first timing line or cue identifier.
    if i < n:
        i += 1
    while (
        i < n
        and lines[i].strip() != ""
        and not _is_cue_timing(lines[i])
        and not (i + 1 < n and _is_cue_timing(lines[i + 1]))
    ):
        i += 1
    while i < n and lines[i].strip() == "":
        i += 1
    while i < n:
        stripped = lines[i].strip()
        if stripped == "":
            i += 1
            continue
        upper = stripped.upper()
        if upper.startswith("NOTE") or upper.startswith("STYLE"):
            i += 1
            while i < n and lines[i].strip() != "":
                i += 1
            continue
        if _is_cue_timing(stripped):
            # Skip the timing line, then emit the cue text until blank.
            i += 1
            while i < n and lines[i].strip() != "":
                out.append(_cue_voice_line(lines[i]))
                i += 1
            continue
        # A cue identifier line (or a stray non-cue line). If the next
        # non-empty line is a timing line this is an identifier → drop it.
        j = i + 1
        while j < n and lines[j].strip() == "":
            j += 1
        if j < n and _is_cue_timing(lines[j].strip()):
            i += 1
            continue
        out.append(_cue_voice_line(lines[i]))
        i += 1
    return "\n".join(_merge_consecutive(out))


def _looks_like_srt(lines: list[str]) -> bool:
    """An SRT starts with an integer cue index followed by a timing line."""
    nonempty = [ln.strip() for ln in lines if ln.strip()]
    if len(nonempty) < 2:
        return False
    return nonempty[0].isdigit() and _is_cue_timing(nonempty[1])


def _strip_srt(lines: list[str]) -> str:
    """SRT → cue text lines: drop index and timing lines, merge duplicates."""
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if stripped == "":
            i += 1
            continue
        if stripped.isdigit():
            # A cue index only when a timing line follows; otherwise it is
            # spoken text that happens to be a number ("2026").
            j = i + 1
            while j < n and lines[j].strip() == "":
                j += 1
            if j < n and _is_cue_timing(lines[j]):
                i = j + 1
                continue
        if _is_cue_timing(stripped):
            i += 1
            continue
        out.append(stripped)
        i += 1
    return "\n".join(_merge_consecutive(out))


def _strip_cue_timing(raw: str) -> str:
    """Detect WEBVTT/SRT and reduce to cue text; plain text is unchanged."""
    # A WebVTT file starts with its signature, after an optional BOM only.
    lines = raw.removeprefix("\ufeff").split("\n")
    if _VTT_SIGNATURE_RE.match(lines[0]):
        return _strip_vtt(lines)
    if _looks_like_srt(lines):
        return _strip_srt(lines)
    return raw


def _normalise_content(raw: str, *, source: str = "manual_paste") -> tuple[str, int]:
    """CRLF→LF, strip NULs/lone surrogates, trim trailing whitespace per line.

    For ``source=file_upload``, VTT/SRT cue timing is stripped to
    ``Speaker: text`` lines (spec §4) before DLP and line numbering. Returns
    ``(normalised_text, line_count)``; line numbers in citations and action
    items refer to this stored text.
    """
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw, _ = sanitize_archive_string(raw)
    if source == "file_upload":
        raw = _strip_cue_timing(raw)
    lines = [line.rstrip() for line in raw.split("\n")]
    return "\n".join(lines), len(lines)


async def _read_meeting_body(request: Request) -> dict:
    """Stream the request body, refusing (413) past 1 MiB — a chunked body is
    never buffered beyond the cap (the Jira inbound pattern)."""
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > _MAX_BODY_BYTES:
                raise HTTPException(
                    413,
                    {"error": "meeting_too_large", "message": "Transcript is too large"},
                )
        except ValueError:
            pass
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_BODY_BYTES:
            raise HTTPException(
                413,
                {"error": "meeting_too_large", "message": "Transcript is too large"},
            )
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        raise HTTPException(422, "Request body must be JSON")
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise HTTPException(422, "Request body must be JSON") from None
    if not isinstance(parsed, dict):
        raise HTTPException(422, "Request body must be a JSON object")
    return parsed


async def _parse_meeting_body(request: Request) -> MeetingCreate:
    """Read + validate the JSON body (the body is streamed, not FastAPI-parsed,
    so validation errors are raised as 422 here rather than bubbling up)."""
    from pydantic import ValidationError

    body = await _read_meeting_body(request)
    # Normalise the title string before pydantic validation: the constrained
    # title field rejects lone surrogates outright, so strip NULs and replace
    # lone surrogates here rather than surfacing a raw validation error.
    if isinstance(body.get("title"), str):
        body["title"], _ = sanitize_archive_string(body["title"])
    try:
        return MeetingCreate.model_validate(body)
    except ValidationError as exc:
        # Drop ``input``/``ctx`` so a rejected body never echoes the submitted
        # values (e.g. the title text) back to the caller.
        sanitized = [
            {k: v for k, v in err.items() if k not in ("input", "ctx")}
            for err in exc.errors()
        ]
        raise HTTPException(
            422,
            {
                "code": "validation_error",
                "message": "Invalid meeting body",
                "errors": sanitized,
            },
        ) from exc


async def _upload_dlp_policy(
    db: AsyncSession, auth: AuthContext, scope_org_id: str | None
) -> dict | None:
    """Strictest union of every org the caller is an active member of, plus the
    scope org's policy. Service keys contribute only their own org."""
    if auth.key_kind == "service":
        org_ids: set[str] = {auth.org_id} if auth.org_id else set()
    else:
        org_ids = set(
            (
                await db.execute(
                    select(OrgMember.org_id).where(OrgMember.user_id == auth.user.id)
                )
            )
            .scalars()
            .all()
        )
    if scope_org_id:
        org_ids.add(scope_org_id)
    if not org_ids:
        return None
    orgs = (
        await db.execute(select(Organization).where(Organization.id.in_(org_ids)))
    ).scalars().all()
    policies = [get_org_dlp_policy(org) for org in orgs]
    scope_pattern_names: set[str] = set()
    if scope_org_id:
        for org in orgs:
            if org.id != scope_org_id:
                continue
            for pattern in well_formed_custom_patterns(get_org_dlp_policy(org)):
                scope_pattern_names.add(pattern["name"])
    union = union_dlp_policies(policies, scope_pattern_names=scope_pattern_names)
    if union is None:
        return None
    # The strictest union also takes the strictest *mode*: block beats redact
    # beats warn. ``union_dlp_policies`` only carries categories/patterns/
    # allowlist, so recompute mode from the contributing (enabled) policies.
    modes = {
        p.get("mode", "warn")
        for p in policies
        if isinstance(p, dict) and p.get("mode") in ("warn", "redact", "block")
    }
    if "block" in modes:
        union["mode"] = "block"
    elif "redact" in modes:
        union["mode"] = "redact"
    else:
        union["mode"] = "warn"
    return union


def _dlp_scan_content(content: str, policy: dict | None) -> tuple[str, int]:
    """Scan ``content`` under ``policy``; BLOCK-mode findings refuse with a
    422 carrying counts by type. Returns ``(stored_content, dlp_findings)``
    where ``dlp_findings`` is the number of redactions applied."""
    policy = policy or DEFAULT_DLP_POLICY
    mode = policy.get("mode", "warn")
    categories = policy.get("categories")
    if not isinstance(categories, list) or not categories:
        categories = []
    categories = ["secrets", *[c for c in categories if c != "secrets"]]
    custom_patterns = policy.get("custom_patterns")
    if not isinstance(custom_patterns, list):
        custom_patterns = None
    allowlist = policy.get("allowlist")
    if not isinstance(allowlist, list):
        allowlist = None

    findings = scan_dlp_full(
        content,
        categories=categories,
        custom_patterns=custom_patterns,
        allowlist=allowlist,
    )
    findings = [f for f in findings if getattr(f, "match_text", None)]
    if not findings:
        return content, 0
    if mode == "block":
        counts: dict[str, int] = {}
        for f in findings:
            counts[f.pattern_name] = counts.get(f.pattern_name, 0) + 1
        raise HTTPException(
            status_code=422,
            detail={
                "error": "dlp_blocked",
                "message": f"DLP policy blocked upload: {len(findings)} finding(s)",
                "findings": counts,
            },
        )
    # Every non-block mode (redact, warn, or the default secrets-only policy)
    # redacts findings before storage — the only stored copy is redacted text.
    return redact_text(content, findings, custom_patterns), len(findings)


# ── Scope resolution / authorization ──


async def _get_meeting(db: AsyncSession, meeting_id: str) -> MeetingTranscript:
    meeting = (
        await db.execute(
            select(MeetingTranscript).where(MeetingTranscript.id == meeting_id)
        )
    ).scalar_one_or_none()
    if meeting is None:
        raise HTTPException(404, "Meeting not found")
    return meeting


def _merged_project_error() -> HTTPException:
    """410 for a write against a merge tombstone (see ``project_groups``)."""
    return HTTPException(
        410,
        {
            "code": "project_merged",
            "error": "project_merged",
            "message": "This project was merged into another project.",
        },
    )


async def _load_project_for_meetings(
    db: AsyncSession,
    project_id: str,
    auth: AuthContext,
    *,
    write: bool,
    not_found_detail: str = "Project not found",
) -> Project:
    """Authorize a caller against a project meeting scope.

    Service keys go through the usual org + allowlist check (403 cross-org or
    not-in-list). User keys use the read predicate; a caller who cannot even
    read the project gets ``not_found_detail`` (existence stays hidden), and a
    reader who is not an editor gets 403 ``project_edit_forbidden`` only when a
    write is needed. Meeting-level routes pass ``not_found_detail="Meeting not
    found"`` so every miss on a meeting id is byte-identical.
    """
    from sessionfs.server.auth.project_access import (
        load_project_for_user,
        require_project_editor,
    )

    if auth.key_kind == "service":
        project = (
            await db.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None:
            raise HTTPException(404, not_found_detail)
        await assert_service_key_can_access_project(db, auth, project)
        if write and project.merged_into_project_id is not None:
            raise _merged_project_error()
        return project

    try:
        project = await load_project_for_user(project_id, db, auth.user.id)
    except HTTPException as exc:
        if exc.status_code in (403, 404):
            raise HTTPException(404, not_found_detail) from None
        raise
    if write:
        if project.merged_into_project_id is not None:
            raise _merged_project_error()
        await require_project_editor(db, auth.user.id, project)
    return project


async def _project_for_meeting(
    db: AsyncSession, meeting: MeetingTranscript, auth: AuthContext, *, write: bool
) -> Project:
    try:
        return await _load_project_for_meetings(
            db, meeting.project_id, auth, write=write, not_found_detail="Meeting not found"
        )
    except HTTPException as exc:
        # Meeting ids must not leak a service key's cross-org / allowlist
        # verdict: every denial becomes the same "Meeting not found" 404.
        if auth.key_kind == "service" and exc.status_code == 403:
            raise HTTPException(404, "Meeting not found") from None
        raise


async def _group_member_roles(
    db: AsyncSession, user: User, group: ProjectGroup, *, not_found_detail: str
) -> tuple[str, str]:
    """Resolve the caller's standing on ``group`` with visibility first.

    Membership and the grant check come before any plan feature gate, so a
    non-member (or a member with no grant) always gets ``not_found_detail`` —
    never a tier upgrade hint. Returns ``(org_role, my_role)``.
    """
    from sessionfs.server.routes.project_groups import (
        _caller_group_role,
        _org_for_member,
    )

    try:
        _, org_role = await _org_for_member(db, user, group.org_id)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(404, not_found_detail) from None
        raise
    my_role = await _caller_group_role(db, user, group, org_role)
    if my_role is None:
        raise HTTPException(404, not_found_detail)
    return org_role, my_role


async def _group_for_meeting(
    db: AsyncSession, meeting: MeetingTranscript, auth: AuthContext, *, write: bool
) -> ProjectGroup:
    from sessionfs.server.auth.project_access import GROUP_EDIT_ROLES
    from sessionfs.server.routes.project_groups import _require_group_feature

    if auth.key_kind == "service":
        # Group meetings aren't reachable by service keys, and the existence
        # of a group meeting must not leak through a distinct error.
        raise HTTPException(404, "Meeting not found")
    group = (
        await db.execute(
            select(ProjectGroup).where(
                ProjectGroup.id == meeting.group_id,
                ProjectGroup.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(404, "Meeting not found")
    _, my_role = await _group_member_roles(
        db, auth.user, group, not_found_detail="Meeting not found"
    )
    await _require_group_feature(db, group.org_id)
    if write and my_role not in GROUP_EDIT_ROLES:
        raise HTTPException(
            403,
            {
                "code": "group_edit_forbidden",
                "message": "You cannot change this group's meetings.",
            },
        )
    return group


def _meeting_scope(
    db: AsyncSession, meeting: MeetingTranscript, auth: AuthContext, *, write: bool
):
    """Authorize the caller on ``meeting`` and return its scope object
    (``Project`` or ``ProjectGroup``)."""
    if meeting.project_id is not None:
        return _project_for_meeting(db, meeting, auth, write=write)
    return _group_for_meeting(db, meeting, auth, write=write)


async def _meeting_scope_org_id(
    db: AsyncSession, meeting: MeetingTranscript
) -> str | None:
    """The meeting's live scope org (``project.org_id`` or ``group.org_id``).

    ``meeting.org_id`` is denormalised at upload and goes stale when a project
    is transferred between orgs, so resolution / audit / retention derive the
    org from the live scope rather than the stored hint.
    """
    if meeting.project_id is not None:
        return (
            await db.execute(
                select(Project.org_id).where(Project.id == meeting.project_id)
            )
        ).scalar_one_or_none()
    if meeting.group_id is not None:
        return (
            await db.execute(
                select(ProjectGroup.org_id).where(ProjectGroup.id == meeting.group_id)
            )
        ).scalar_one_or_none()
    return None


# ── Audit ──


async def _user_org_role(
    db: AsyncSession, user_id: str, org_id: str | None
) -> str | None:
    if not org_id:
        return None
    return (
        await db.execute(
            select(OrgMember.role).where(
                OrgMember.org_id == org_id, OrgMember.user_id == user_id
            )
        )
    ).scalar_one_or_none()


def _meeting_audit_event(
    *,
    meeting: MeetingTranscript,
    org: Organization | None,
    event_type: str,
    actor_user_id: str | None,
    actor_email: str | None,
    actor_role: str | None,
    before: dict | None = None,
    after: dict | None = None,
) -> OrgAuditEvent:
    """An OrgAuditEvent row with ids/counts only — never transcript text.

    A personal-scope meeting (no org) still gets a row, with ``org_id`` NULL
    (``OrgAuditEvent.org_id`` is nullable) and an empty org-name snapshot, so
    personal-project upload / delete / override are audited too.
    """
    return OrgAuditEvent(
        id=f"oae_{uuid.uuid4().hex[:24]}",
        org_id=org.id if org is not None else None,
        org_name_snapshot=org.name if org is not None else "",
        event_type=event_type,
        actor_user_id=actor_user_id,
        actor_email_snapshot=actor_email,
        actor_role_at_time=actor_role,
        target_type="meeting_transcript",
        target_id=meeting.id,
        before=json.dumps(before, default=str) if before is not None else None,
        after=json.dumps(after, default=str) if after is not None else None,
    )


def _meeting_summary(
    meeting: MeetingTranscript, org_id: str | None
) -> MeetingSummaryResponse:
    # ``org_id`` is the live scope org; the stored column goes stale when a
    # project moves between orgs.
    return MeetingSummaryResponse(
        id=meeting.id,
        org_id=org_id,
        project_id=meeting.project_id,
        group_id=meeting.group_id,
        title=meeting.title,
        meeting_at=meeting.meeting_at,
        duration_minutes=meeting.duration_minutes,
        source=meeting.source,
        line_count=meeting.line_count,
        dlp_findings=meeting.dlp_findings,
        extraction_status=meeting.extraction_status,
        extraction_error=meeting.extraction_error,
        created_by_user_id=meeting.created_by_user_id,
        created_at=meeting.created_at,
        updated_at=meeting.updated_at,
        deleted_at=meeting.deleted_at,
    )


async def _action_counts(
    db: AsyncSession, meeting_ids: list[str]
) -> dict[str, tuple[int, int]]:
    """``{meeting_id: (pending actions, decisions)}`` in one grouped query."""
    if not meeting_ids:
        return {}
    rows = (
        await db.execute(
            select(
                MeetingActionItem.transcript_id,
                func.sum(
                    case(
                        (
                            (MeetingActionItem.kind == "action")
                            & (MeetingActionItem.status == "pending"),
                            1,
                        ),
                        else_=0,
                    )
                ),
                func.sum(case((MeetingActionItem.kind == "decision", 1), else_=0)),
            )
            .where(MeetingActionItem.transcript_id.in_(meeting_ids))
            .group_by(MeetingActionItem.transcript_id)
        )
    ).all()
    return {r[0]: (int(r[1] or 0), int(r[2] or 0)) for r in rows}


def _summaries_with_counts(
    meetings: list[MeetingTranscript],
    org_id: str | None,
    counts: dict[str, tuple[int, int]],
) -> list[MeetingSummaryResponse]:
    out = []
    for m in meetings:
        summary = _meeting_summary(m, org_id)
        pending, decisions = counts.get(m.id, (0, 0))
        summary.pending_action_count = pending
        summary.decision_count = decisions
        out.append(summary)
    return out


def _participant_response(p: MeetingParticipant) -> MeetingParticipantResponse:
    return MeetingParticipantResponse(
        id=p.id,
        name_raw=p.name_raw,
        email_raw=p.email_raw,
        user_id=p.user_id,
        persona_name=p.persona_name,
        resolution_method=p.resolution_method,
        resolved_by_user_id=p.resolved_by_user_id,
    )


def _action_item_response(item: MeetingActionItem) -> MeetingActionItemResponse:
    return MeetingActionItemResponse(
        id=item.id,
        kind=item.kind,
        description=item.description,
        owner_participant_id=item.owner_participant_id,
        due_date=item.due_date,
        line_start=item.line_start,
        line_end=item.line_end,
        status=item.status,
        promoted_ticket_id=item.promoted_ticket_id,
        extracted_at=item.extracted_at,
    )


async def _meeting_detail(
    db: AsyncSession, meeting: MeetingTranscript, warnings: list[str] | None = None
) -> MeetingDetailResponse:
    participants = (
        (
            await db.execute(
                select(MeetingParticipant)
                .where(MeetingParticipant.transcript_id == meeting.id)
                .order_by(MeetingParticipant.id.asc())
            )
        )
        .scalars()
        .all()
    )
    action_items = (
        (
            await db.execute(
                select(MeetingActionItem)
                .where(MeetingActionItem.transcript_id == meeting.id)
                .order_by(MeetingActionItem.id.asc())
            )
        )
        .scalars()
        .all()
    )
    base = _meeting_summary(meeting, await _meeting_scope_org_id(db, meeting))
    return MeetingDetailResponse(
        **base.model_dump(),
        content=meeting.content,
        content_sha256=meeting.content_sha256,
        participants=[_participant_response(p) for p in participants],
        action_items=[_action_item_response(i) for i in action_items],
        warnings=list(warnings or []),
    )


async def _store_meeting(
    db: AsyncSession,
    auth: AuthContext,
    *,
    project: Project | None,
    group: ProjectGroup | None,
    payload: MeetingCreate,
) -> MeetingDetailResponse:
    """Normalise, DLP-scan, then persist a meeting (project OR group scope)."""
    title = payload.title.strip()
    title, _ = sanitize_archive_string(title)
    title = title.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    title = title.strip()
    if not title:
        raise HTTPException(422, "title must not be blank")
    if not payload.content:
        raise HTTPException(422, "content must not be empty")

    if project is not None:
        scope_org_id = project.org_id
    else:
        assert group is not None  # a meeting is always project- OR group-scoped
        scope_org_id = group.org_id
    policy = await _upload_dlp_policy(db, auth, scope_org_id)

    content, line_count = _normalise_content(payload.content, source=payload.source)
    if not content.strip():
        raise HTTPException(422, "content must not be empty")
    if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
        raise HTTPException(413, {"error": "meeting_too_large", "message": "Transcript is too large"})
    if line_count > _MAX_LINES:
        raise HTTPException(413, {"error": "meeting_too_large", "message": "Transcript has too many lines"})

    # DLP-scan title and content under the same policy: a block finding in
    # either refuses the upload, otherwise both are redacted before storage.
    title, title_findings = _dlp_scan_content(title, policy)
    title = title[:200]
    content, content_findings = _dlp_scan_content(content, policy)
    dlp_findings = title_findings + content_findings
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()

    now = datetime.now(timezone.utc)
    meeting = MeetingTranscript(
        id=f"meet_{uuid.uuid4().hex[:16]}",
        org_id=scope_org_id,
        project_id=project.id if project is not None else None,
        group_id=group.id if group is not None else None,
        title=title,
        meeting_at=payload.meeting_at,
        duration_minutes=payload.duration_minutes,
        source=payload.source,
        content=content,
        content_sha256=content_sha256,
        line_count=line_count,
        dlp_findings=dlp_findings,
        extraction_status="none",
        created_by_user_id=auth.user.id,
        created_at=now,
        updated_at=now,
    )
    db.add(meeting)

    warnings: list[str] = []
    if project is not None:
        duplicate = (
            await db.execute(
                select(MeetingTranscript.id)
                .where(
                    MeetingTranscript.project_id == project.id,
                    MeetingTranscript.content_sha256 == content_sha256,
                    MeetingTranscript.deleted_at.is_(None),
                )
                .order_by(MeetingTranscript.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if duplicate is not None and duplicate != meeting.id:
            warnings.append(f"duplicate_of: {duplicate}")
    elif group is not None:
        duplicate = (
            await db.execute(
                select(MeetingTranscript.id)
                .where(
                    MeetingTranscript.group_id == group.id,
                    MeetingTranscript.content_sha256 == content_sha256,
                    MeetingTranscript.deleted_at.is_(None),
                )
                .order_by(MeetingTranscript.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if duplicate is not None and duplicate != meeting.id:
            warnings.append(f"duplicate_of: {duplicate}")

    org = await db.get(Organization, scope_org_id) if scope_org_id else None
    if auth.key_kind == "service":
        actor_role = "service_key"
    elif scope_org_id:
        actor_role = await _user_org_role(db, auth.user.id, scope_org_id)
    else:
        actor_role = None
    db.add(
        _meeting_audit_event(
            meeting=meeting,
            org=org,
            event_type="meeting_uploaded",
            actor_user_id=auth.user.id,
            actor_email=auth.user.email if auth.key_kind == "user" else None,
            actor_role=actor_role,
            after={
                "meeting_id": meeting.id,
                "source": meeting.source,
                "line_count": meeting.line_count,
                "dlp_findings": meeting.dlp_findings,
                "participant_count": 0,
                "action_item_count": 0,
            },
        )
    )

    await db.commit()
    await db.refresh(meeting)

    if payload.extract:
        # Lazy import: meeting_extraction imports from this module, so importing
        # at call time avoids a cycle at app startup.
        from sessionfs.server.services.meeting_extraction import (
            run_meeting_extraction,
        )

        try:
            await run_meeting_extraction(db, meeting=meeting, auth=auth)
        except HTTPException as exc:
            # The meeting is already stored: report why extraction was skipped
            # instead of failing the upload with an error that hides its id.
            detail = exc.detail
            code = (
                detail.get("code") or detail.get("error")
                if isinstance(detail, dict)
                else None
            ) or f"http_{exc.status_code}"
            warnings.append(f"extraction_skipped: {code}")
            await db.refresh(meeting)

    return await _meeting_detail(db, meeting, warnings=warnings)


# ── Service: participant resolution (§6) ──


async def _active_org_member_by_email(
    db: AsyncSession, org_id: str, email: str
) -> User | None:
    return (
        await db.execute(
            select(User)
            .join(OrgMember, OrgMember.user_id == User.id)
            .where(
                OrgMember.org_id == org_id,
                func.lower(User.email) == email.lower(),
                User.is_active.is_(True),
            )
            .limit(1)
        )
    ).scalars().first()


async def _org_members_by_display_name(
    db: AsyncSession, org_id: str, name: str
) -> list[User]:
    return list(
        (
            await db.execute(
                select(User)
                .join(OrgMember, OrgMember.user_id == User.id)
                .where(
                    OrgMember.org_id == org_id,
                    func.lower(User.display_name) == name.lower(),
                    User.is_active.is_(True),
                )
                .limit(20)
            )
        ).scalars().all()
    )


async def resolve_participants(
    db: AsyncSession,
    transcript: MeetingTranscript,
    names: list[dict],
    *,
    auth: AuthContext | None,
) -> list[dict]:
    """Resolve participant names to org members/personas over the meeting's
    scope (§6), in order: exact email of an active org member → exact persona
    name → a display name matching exactly one member → unresolved. A persona
    that represents a member fills both ``persona_name`` and ``user_id``.

    Persona resolution for a project meeting follows the visibility rule
    (``resolve_persona`` + ``visible_persona_org_id_for_auth``): project
    personas always resolve, the org fallback only for a caller who is a
    current member of the project's live org. Group meetings resolve org
    personas directly. Callers must pass the requesting ``auth``; ``None`` is
    only for trusted internal labeling and falls back to the project's live
    org.

    ``names`` is a list of dicts with ``name`` (str) and optional ``email``
    (str). Returns one dict per input with the resolved
    ``user_id`` / ``persona_name`` / ``resolution_method`` (never writes).
    """
    org_id = await _meeting_scope_org_id(db, transcript)
    project: Project | None = None
    persona_org_id: str | None = org_id
    if transcript.project_id is not None:
        project = await db.get(Project, transcript.project_id)
        if project is not None:
            persona_org_id = (
                await visible_persona_org_id_for_auth(db, project, auth)
                if auth is not None
                else project.org_id
            )
    resolved: list[dict] = []
    for entry in names:
        if not isinstance(entry, dict):
            resolved.append(
                {"name": "", "email": None, "user_id": None, "persona_name": None, "resolution_method": "unresolved"}
            )
            continue
        name = (entry.get("name") or "").strip()
        email = (entry.get("email") or "").strip() or None
        result: dict[str, Any] = {
            "name": name,
            "email": email,
            "user_id": None,
            "persona_name": None,
            "resolution_method": "unresolved",
        }
        if org_id and email:
            user = await _active_org_member_by_email(db, org_id, email)
            if user is not None:
                result["user_id"] = user.id
                result["resolution_method"] = "exact_email"
                resolved.append(result)
                continue
        if name and (project is not None or org_id):
            if project is not None:
                persona = await resolve_persona(
                    db,
                    project.id,
                    name,
                    org_id=persona_org_id,
                    active_only=True,
                    case_insensitive=True,
                )
            else:
                persona = await find_org_persona(
                    db, persona_org_id, name, active_only=True, case_insensitive=True
                )
            if persona is not None:
                result["persona_name"] = persona.name
                result["resolution_method"] = "persona"
                if persona.represented_user_id:
                    result["user_id"] = persona.represented_user_id
                resolved.append(result)
                continue
            if org_id:
                members = await _org_members_by_display_name(db, org_id, name)
                if len(members) == 1:
                    result["user_id"] = members[0].id
                    result["resolution_method"] = "display_name"
        resolved.append(result)
    return resolved


# ── List query (search + keyset paging) ──


def _escape_like(q: str) -> str:
    """Escape ``%`` / ``_`` / ``\\`` so a user query searches for them literally."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _paginate_meetings(
    db: AsyncSession,
    scope_column: Any,
    scope_id: str,
    *,
    q: str | None,
    before: str | None,
    limit: int,
) -> tuple[list[MeetingTranscript], str | None]:
    """Search + keyset page over a project's or group's live meetings.

    Ordering is newest-first on ``(created_at, id)``; ``before`` is the id of
    the last meeting seen on the previous page (an opaque cursor). The cursor's
    stored timestamp is compared via a scalar subquery so SQLite's second
    precision and PostgreSQL's microsecond precision both page identically.
    """
    stmt = select(MeetingTranscript).where(
        scope_column == scope_id,
        MeetingTranscript.deleted_at.is_(None),
    )
    if q:
        pattern = f"%{_escape_like(q)}%"
        stmt = stmt.where(
            or_(
                MeetingTranscript.title.ilike(pattern, escape="\\"),
                MeetingTranscript.content.ilike(pattern, escape="\\"),
            )
        )
    if before:
        cursor_subquery = (
            select(MeetingTranscript.created_at)
            .where(
                scope_column == scope_id,
                MeetingTranscript.id == before,
                MeetingTranscript.deleted_at.is_(None),
            )
            .scalar_subquery()
        )
        stmt = stmt.where(
            or_(
                MeetingTranscript.created_at < cursor_subquery,
                and_(
                    MeetingTranscript.created_at == cursor_subquery,
                    MeetingTranscript.id < before,
                ),
            )
        )
    stmt = stmt.order_by(
        MeetingTranscript.created_at.desc(),
        MeetingTranscript.id.desc(),
    ).limit(limit + 1)
    meetings = list((await db.execute(stmt)).scalars().all())
    has_more = len(meetings) > limit
    meetings = meetings[:limit]
    next_cursor = meetings[-1].id if has_more and meetings else None
    return meetings, next_cursor


# ── Project routes ──


@project_router.post("/{project_id}/meetings", response_model=MeetingDetailResponse)
async def upload_project_meeting(
    project_id: str,
    request: Request,
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    project = await _load_project_for_meetings(db, project_id, auth, write=True)

    payload = await _parse_meeting_body(request)
    return await _store_meeting(db, auth, project=project, group=None, payload=payload)


@project_router.get("/{project_id}/meetings", response_model=MeetingListResponse)
async def list_project_meetings(
    project_id: str,
    q: str | None = Query(default=None),
    before: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=200),
    auth: AuthContext = Depends(require_scope("meetings:read")),
    db: AsyncSession = Depends(get_db),
):
    project = await _load_project_for_meetings(db, project_id, auth, write=False)

    meetings, next_cursor = await _paginate_meetings(
        db,
        MeetingTranscript.project_id,
        project_id,
        q=q,
        before=before,
        limit=limit,
    )
    return MeetingListResponse(
        meetings=_summaries_with_counts(
            meetings, project.org_id, await _action_counts(db, [m.id for m in meetings])
        ),
        next_cursor=next_cursor,
    )


# ── Group routes ──


@group_router.post("/{group_id}/meetings", response_model=MeetingDetailResponse)
async def upload_group_meeting(
    group_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sessionfs.server.auth.project_access import GROUP_EDIT_ROLES
    from sessionfs.server.routes.project_groups import _require_group_feature

    group = (
        await db.execute(
            select(ProjectGroup).where(
                ProjectGroup.id == group_id, ProjectGroup.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(404, "Project group not found")
    # Membership + grant first so a non-member never leaks the plan tier;
    # every miss is the same "Project group not found" 404.
    _, my_role = await _group_member_roles(
        db, user, group, not_found_detail="Project group not found"
    )
    await _require_group_feature(db, group.org_id)
    if my_role not in GROUP_EDIT_ROLES:
        raise HTTPException(
            403,
            {
                "code": "group_edit_forbidden",
                "message": "You cannot upload meetings to this group.",
            },
        )

    payload = await _parse_meeting_body(request)

    # Group routes run under get_current_user (user keys only); build a
    # user-key context so _store_meeting can read actor + org membership.
    ctx = AuthContext(
        user=user,
        api_key_id="",
        key_kind="user",
    )
    return await _store_meeting(db, ctx, project=None, group=group, payload=payload)


@group_router.get("/{group_id}/meetings", response_model=MeetingListResponse)
async def list_group_meetings(
    group_id: str,
    q: str | None = Query(default=None),
    before: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sessionfs.server.routes.project_groups import _require_group_feature

    group = (
        await db.execute(
            select(ProjectGroup).where(
                ProjectGroup.id == group_id, ProjectGroup.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(404, "Project group not found")
    # Membership + grant first so a non-member never leaks the plan tier.
    await _group_member_roles(
        db, user, group, not_found_detail="Project group not found"
    )
    await _require_group_feature(db, group.org_id)

    meetings, next_cursor = await _paginate_meetings(
        db,
        MeetingTranscript.group_id,
        group_id,
        q=q,
        before=before,
        limit=limit,
    )
    return MeetingListResponse(
        meetings=_summaries_with_counts(
            meetings, group.org_id, await _action_counts(db, [m.id for m in meetings])
        ),
        next_cursor=next_cursor,
    )


# ── Meeting-level routes ──


@router.get("/meetings/{meeting_id}", response_model=MeetingDetailResponse)
async def get_meeting(
    meeting_id: str,
    auth: AuthContext = Depends(require_scope("meetings:read")),
    db: AsyncSession = Depends(get_db),
):
    meeting = await _get_meeting(db, meeting_id)
    if meeting.deleted_at is not None:
        raise HTTPException(404, "Meeting not found")
    await _meeting_scope(db, meeting, auth, write=False)
    return await _meeting_detail(db, meeting)


@router.delete("/meetings/{meeting_id}", response_model=MeetingDetailResponse)
async def delete_meeting(
    meeting_id: str,
    auth: AuthContext = Depends(require_scope("meetings:write")),
    db: AsyncSession = Depends(get_db),
):
    from sessionfs.server.auth.project_access import (
        GROUP_ROLE_ADMIN,
        user_is_project_admin,
    )
    from sessionfs.server.routes.project_groups import (
        _caller_group_role,
        _org_for_member,
    )
    from sessionfs.server.roles import ADMIN_ROLES

    meeting = await _get_meeting(db, meeting_id)
    if meeting.deleted_at is not None:
        raise HTTPException(404, "Meeting not found")

    if meeting.project_id is not None:
        project = await _project_for_meeting(db, meeting, auth, write=True)
        scope_org_id = project.org_id
        # Same delete authorization for user and service keys: the service
        # key's backing user must be the uploader or a project admin.
        if auth.user.id != meeting.created_by_user_id and not await user_is_project_admin(
            db, auth.user.id, project
        ):
            raise HTTPException(
                403, "Only the uploader or a project admin can delete this meeting"
            )
    else:
        group = await _group_for_meeting(db, meeting, auth, write=True)
        scope_org_id = group.org_id
        if auth.user.id != meeting.created_by_user_id:
            _, org_role = await _org_for_member(db, auth.user, group.org_id)
            my_role = await _caller_group_role(db, auth.user, group, org_role)
            if org_role not in ADMIN_ROLES and my_role != GROUP_ROLE_ADMIN:
                raise HTTPException(
                    403, "Only the uploader or a group admin can delete this meeting"
                )

    now = datetime.now(timezone.utc)
    meeting.deleted_at = now
    meeting.updated_at = now

    # Derive the org from the live scope (not meeting.org_id, which goes stale
    # after a project transfer) and count the real related rows for the audit.
    # Personal meetings (no org) still get an audit row with org_id NULL.
    org = await db.get(Organization, scope_org_id) if scope_org_id else None
    actor_role = (
        await _user_org_role(db, auth.user.id, scope_org_id)
        if auth.key_kind == "user"
        else "service_key"
    )
    participant_count = (
        await db.execute(
            select(func.count())
            .select_from(MeetingParticipant)
            .where(MeetingParticipant.transcript_id == meeting.id)
        )
    ).scalar_one()
    action_item_count = (
        await db.execute(
            select(func.count())
            .select_from(MeetingActionItem)
            .where(MeetingActionItem.transcript_id == meeting.id)
        )
    ).scalar_one()
    db.add(
        _meeting_audit_event(
            meeting=meeting,
            org=org,
            event_type="meeting_deleted",
            actor_user_id=auth.user.id,
            actor_email=auth.user.email if auth.key_kind == "user" else None,
            actor_role=actor_role,
            before={
                "meeting_id": meeting.id,
                "line_count": meeting.line_count,
                "participant_count": participant_count,
                "action_item_count": action_item_count,
            },
        )
    )

    await db.commit()
    await db.refresh(meeting)
    return await _meeting_detail(db, meeting)


@router.put(
    "/meetings/{meeting_id}/participants/{participant_id}",
    response_model=MeetingParticipantResponse,
)
async def override_meeting_participant(
    meeting_id: str,
    participant_id: str,
    body: ParticipantOverride,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    meeting = await _get_meeting(db, meeting_id)
    if meeting.deleted_at is not None:
        raise HTTPException(404, "Meeting not found")

    # Authorize the write (project edit or group editor/admin), user key only.
    # Meeting-level misses are a uniform "Meeting not found" (no oracle).
    auth = AuthContext(user=user, api_key_id="", key_kind="user")
    if meeting.project_id is not None:
        project = await _project_for_meeting(db, meeting, auth, write=True)
    else:
        await _group_for_meeting(db, meeting, auth, write=True)

    scope_org_id = await _meeting_scope_org_id(db, meeting)

    participant = (
        await db.execute(
            select(MeetingParticipant).where(
                MeetingParticipant.id == participant_id,
                MeetingParticipant.transcript_id == meeting.id,
            )
        )
    ).scalar_one_or_none()
    if participant is None:
        raise HTTPException(404, "Participant not found")

    has_user = body.user_id is not None
    has_persona = body.persona_name is not None
    if has_user == has_persona:
        raise HTTPException(422, "Provide exactly one of user_id or persona_name")

    if has_user:
        if meeting.project_id is not None:
            try:
                target = await resolve_assigned_user_id(
                    db, project=project, value=body.user_id
                )
            except HTTPException as exc:
                if exc.status_code == 422:
                    # Map the generic "assignee not eligible" miss to the
                    # meeting override's own uniform 422 so the response never
                    # leaks whether a user exists or belongs to the org.
                    raise HTTPException(422, _PARTICIPANT_NOT_ELIGIBLE) from None
                raise
        else:
            target = await _resolve_group_org_member(db, scope_org_id, body.user_id)
        participant.user_id = target.id
        participant.persona_name = None
    else:
        if meeting.project_id is not None:
            # Project personas resolve first, then the caller's visible org
            # personas (same visibility rule as participant resolution). A
            # retained owner who is no longer a member of the project's live
            # org sees only project personas.
            visible_org_id = await visible_persona_org_id_for_auth(db, project, auth)
            persona = await resolve_persona(
                db,
                project.id,
                body.persona_name,
                org_id=visible_org_id,
                active_only=True,
                case_insensitive=True,
            )
        else:
            if not scope_org_id:
                raise HTTPException(422, _PARTICIPANT_NOT_ELIGIBLE)
            persona = await find_org_persona(
                db, scope_org_id, body.persona_name, active_only=True, case_insensitive=True
            )
        if persona is None:
            raise HTTPException(422, _PARTICIPANT_NOT_ELIGIBLE)
        participant.persona_name = persona.name
        # Same as automatic resolution: a persona that represents a member
        # labels the participant with both.
        participant.user_id = persona.represented_user_id

    participant.resolution_method = "manual"
    participant.resolved_by_user_id = user.id

    # Personal meetings (no org) still get an audit row with org_id NULL.
    org = await db.get(Organization, scope_org_id) if scope_org_id else None
    actor_role = await _user_org_role(db, user.id, scope_org_id)
    db.add(
        _meeting_audit_event(
            meeting=meeting,
            org=org,
            event_type="meeting_participant_overridden",
            actor_user_id=user.id,
            actor_email=user.email,
            actor_role=actor_role,
            after={
                "meeting_id": meeting.id,
                "participant_id": participant.id,
                "user_id": participant.user_id,
            },
        )
    )

    await db.commit()
    await db.refresh(participant)
    return _participant_response(participant)


async def _resolve_group_org_member(
    db: AsyncSession, org_id: str | None, value: str
) -> User:
    if not org_id:
        raise HTTPException(422, _PARTICIPANT_NOT_ELIGIBLE)
    value = value.strip()
    stmt = select(User).join(OrgMember, OrgMember.user_id == User.id).where(
        OrgMember.org_id == org_id, User.is_active.is_(True)
    )
    if "@" in value:
        stmt = stmt.where(func.lower(User.email) == value.lower())
    else:
        stmt = stmt.where(User.id == value)
    rows = (await db.execute(stmt.limit(2))).scalars().all()
    if len(rows) != 1:
        raise HTTPException(422, _PARTICIPANT_NOT_ELIGIBLE)
    return rows[0]

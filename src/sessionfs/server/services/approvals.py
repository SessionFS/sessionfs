"""Approval policies: validation, matching, requests and decisions (GitHub #123, A1).

An org admin writes a policy (``approval_policies``, immutable versions). When
a ticket's work is otherwise merge-ready at a commit, ``evaluate_approvals``
creates or loads one request per matching active policy for that exact sha
(``approval_requests``, at most one PENDING per policy version + target +
fingerprint), and readiness waits until every BLOCKING request is approved.
Advisory policies only add warnings. A new sha means a new request; older
pending ones become ``superseded``. Pending requests expire 14 days after they
were raised (applied lazily, on read); an expired or superseded request is
raised again for the same key (a rejected KB request only on a person's
explicit promote; a merge rejection stands for that commit).

``decide`` locks the request, re-checks the approver (active org member who
fits a requirement, not an implementer of the ticket — a service key counts as
the people who minted it, and an unresolvable minter fails closed), records
one decision per person, then moves ``pending → approved`` (every requirement
met) or ``→ rejected`` (any reject) with a rowcount guard.

Matching (merge): ``project_ids`` and ``ticket_kind`` scope the policy; the
triggers ``priority_at_least``, ``finding_harm_any`` and
``checklist_class_any`` are OR-ed (any one set trigger that holds matches);
with no trigger set, every ticket in scope matches. Matching (kb): every set
key must hold (A2 calls ``matching_policies`` from the promotion gate).

Plan: creating and editing policies needs an Enterprise org. Policies that
already exist keep being enforced (and can be decided and deactivated) after a
downgrade, so a gate never silently disappears — the same rule as the org
skills policy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Iterable, Literal, Optional, Union

from fastapi import HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
)
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentRun,
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
    KnowledgeEntry,
    OrgMember,
    Project,
    Ticket,
    TicketNotification,
    User,
)
from sessionfs.server.roles import has_minimum_role

logger = logging.getLogger("sessionfs.approvals")

FEATURE = "approval_policies"
REQUEST_TTL = timedelta(days=14)
MAX_FILTER_BYTES = 8 * 1024
MAX_ACTIVE_POLICIES = 50
MAX_PENDING_PER_TARGET = 20
MAX_REASON_CHARS = 2000
MAX_REQUIREMENTS = 10
MAX_REQUIREMENT_COUNT = 20

REQUEST_NOT_FOUND = "Approval request not found"
POLICY_NOT_FOUND = "Approval policy not found"

TARGET_KINDS = ("merge", "kb_promote")
_PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_HARMS = ("data-exposed", "money-spent", "wrong-actor", "crash", "other")
_ENTRY_TYPES = ("decision", "pattern", "discovery", "convention", "bug", "dependency")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _err(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status, {"error": code, "code": code, "message": message, **extra})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ── Schemas ────────────────────────────────────────────────────────


def _ids(values: list[str], what: str) -> list[str]:
    out: list[str] = []
    for v in values:
        if not _ID_RE.fullmatch(v):
            raise ValueError(f"invalid {what}")
        if v not in out:
            out.append(v)
    return out


class MergeFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_ids: list[str] = Field(default_factory=list, max_length=200)
    priority_at_least: Optional[Literal["low", "medium", "high", "critical"]] = None
    ticket_kind: Optional[Literal["task", "issue"]] = None
    finding_harm_any: list[
        Literal["data-exposed", "money-spent", "wrong-actor", "crash", "other"]
    ] = Field(default_factory=list, max_length=len(_HARMS))
    checklist_class_any: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("project_ids")
    @classmethod
    def _project_ids(cls, v: list[str]) -> list[str]:
        return _ids(v, "project id")

    @field_validator("checklist_class_any")
    @classmethod
    def _classes(cls, v: list[str]) -> list[str]:
        for s in v:
            if not _SLUG_RE.fullmatch(s):
                raise ValueError("checklist classes are lowercase slugs")
        return list(dict.fromkeys(v))


class KbFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_ids: list[str] = Field(default_factory=list, max_length=200)
    entry_type_any: list[
        Literal["decision", "pattern", "discovery", "convention", "bug", "dependency"]
    ] = Field(default_factory=list, max_length=len(_ENTRY_TYPES))
    entity_ref_prefix_any: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("project_ids")
    @classmethod
    def _project_ids(cls, v: list[str]) -> list[str]:
        return _ids(v, "project id")

    @field_validator("entity_ref_prefix_any")
    @classmethod
    def _prefixes(cls, v: list[str]) -> list[str]:
        for p in v:
            if not p or len(p) > 200 or any(ord(ch) < 32 or ord(ch) == 127 for ch in p):
                raise ValueError("entity_ref prefixes are 1-200 printable characters")
        return list(dict.fromkeys(v))


class OrgRoleRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    who: Literal["org_role"]
    role: Literal["owner", "admin"]
    count: StrictInt = Field(ge=1, le=MAX_REQUIREMENT_COUNT)


class UsersRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    who: Literal["users"]
    user_ids: list[str] = Field(min_length=1, max_length=50)
    count: StrictInt = Field(ge=1, le=MAX_REQUIREMENT_COUNT)

    @field_validator("user_ids")
    @classmethod
    def _user_ids(cls, v: list[str]) -> list[str]:
        return _ids(v, "user id")


Requirement = Annotated[
    Union[OrgRoleRequirement, UsersRequirement], Field(discriminator="who")
]


class _Requirements(BaseModel):
    items: list[Requirement] = Field(min_length=1, max_length=MAX_REQUIREMENTS)


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()) if p != "items")
    msg = str(first.get("msg", "invalid value"))
    return f"{loc}: {msg}" if loc else msg


def validate_policy_body(
    target_kind: str, filter_raw: Any, requirements_raw: Any
) -> tuple[dict, list[dict]]:
    """Validate a policy's filter and requirements; returns normalised JSON
    values. Raises 422 with a stable code on any problem."""
    if target_kind not in TARGET_KINDS:
        raise _err(422, "invalid_target_kind", "target_kind must be merge or kb_promote.")
    if filter_raw is None:
        filter_raw = {}
    if not isinstance(filter_raw, dict):
        raise _err(422, "invalid_filter", "filter must be an object.")
    try:
        raw_size = len(json.dumps(filter_raw, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        raise _err(422, "invalid_filter", "filter must be JSON.") from None
    if raw_size > MAX_FILTER_BYTES:
        raise _err(422, "filter_too_large", "filter must be at most 8 KiB.")
    model = MergeFilter if target_kind == "merge" else KbFilter
    try:
        flt = model.model_validate(filter_raw).model_dump(exclude_defaults=True)
    except ValidationError as exc:
        raise _err(422, "invalid_filter", _validation_message(exc)) from None
    if not isinstance(requirements_raw, list):
        raise _err(422, "invalid_requirements", "requirements must be a list.")
    try:
        reqs = _Requirements.model_validate({"items": requirements_raw})
    except ValidationError as exc:
        raise _err(422, "invalid_requirements", _validation_message(exc)) from None
    out: list[dict] = []
    for r in reqs.items:
        if isinstance(r, UsersRequirement) and r.count > len(r.user_ids):
            raise _err(
                422, "invalid_requirements", "count cannot exceed the number of named users."
            )
        out.append(r.model_dump())
    return flt, out


async def check_policy_references(
    db: AsyncSession, org_id: str, flt: dict, requirements: list[dict]
) -> None:
    """Named users must be active members of the org; filter projects must be
    projects of the org. One 422 per kind, never naming which id missed."""
    user_ids = {u for r in requirements if r["who"] == "users" for u in r["user_ids"]}
    if user_ids:
        found = set(
            (
                await db.execute(
                    select(OrgMember.user_id)
                    .join(User, User.id == OrgMember.user_id)
                    .where(
                        OrgMember.org_id == org_id,
                        OrgMember.user_id.in_(user_ids),
                        User.is_active.is_(True),
                    )
                )
            ).scalars().all()
        )
        if found != user_ids:
            raise _err(
                422, "approver_not_eligible",
                "Every named approver must be an active member of the organization.",
            )
    project_ids = set(flt.get("project_ids") or [])
    if project_ids:
        found_p = set(
            (
                await db.execute(
                    select(Project.id).where(
                        Project.id.in_(project_ids), Project.org_id == org_id
                    )
                )
            ).scalars().all()
        )
        if found_p != project_ids:
            raise _err(
                422, "invalid_filter", "filter.project_ids must name projects in this organization."
            )


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def policy_requirements(policy: ApprovalPolicy) -> list[dict]:
    try:
        reqs = json.loads(policy.requirements)
    except (TypeError, ValueError):
        return []
    return reqs if isinstance(reqs, list) else []


def policy_filter(policy: ApprovalPolicy) -> dict:
    try:
        flt = json.loads(policy.filter or "{}")
    except (TypeError, ValueError):
        return {}
    return flt if isinstance(flt, dict) else {}


def required_count(requirements: list[dict]) -> int:
    return sum(int(r.get("count") or 0) for r in requirements)


# ── Matching ───────────────────────────────────────────────────────


def _matches_merge(flt: dict, target: dict) -> bool:
    project_ids = flt.get("project_ids") or []
    if project_ids and target.get("project_id") not in project_ids:
        return False
    kind = flt.get("ticket_kind")
    if kind and target.get("kind") != kind:
        return False
    triggers: list[bool] = []
    floor = flt.get("priority_at_least")
    if floor:
        rank = _PRIORITY_RANK.get(str(target.get("priority") or ""), -1)
        triggers.append(rank >= _PRIORITY_RANK[floor])
    harms = flt.get("finding_harm_any") or []
    if harms:
        triggers.append(bool(set(harms) & set(target.get("harms") or ())))
    classes = flt.get("checklist_class_any") or []
    if classes:
        triggers.append(bool(set(classes) & set(target.get("classes") or ())))
    return any(triggers) if triggers else True


def _matches_kb(flt: dict, target: dict) -> bool:
    project_ids = flt.get("project_ids") or []
    if project_ids and target.get("project_id") not in project_ids:
        return False
    types = flt.get("entry_type_any") or []
    if types and target.get("entry_type") not in types:
        return False
    prefixes = flt.get("entity_ref_prefix_any") or []
    if prefixes:
        ref = target.get("entity_ref") or ""
        if not any(ref.startswith(p) for p in prefixes):
            return False
    return True


async def matching_policies(
    db: AsyncSession, org_id: str, target_kind: str, target: dict
) -> list[ApprovalPolicy]:
    """Active policies of ``org_id`` for ``target_kind`` whose filter matches.

    ``target`` for merge: ``project_id``, ``priority``, ``kind``, ``harms``,
    ``classes``. For kb_promote: ``project_id``, ``entry_type``,
    ``entity_ref``. Oldest policy first.
    """
    policies = (
        await db.execute(
            select(ApprovalPolicy)
            .where(
                ApprovalPolicy.org_id == org_id,
                ApprovalPolicy.active.is_(True),
                ApprovalPolicy.target_kind == target_kind,
            )
            .order_by(ApprovalPolicy.created_at, ApprovalPolicy.id)
        )
    ).scalars().all()
    match = _matches_merge if target_kind == "merge" else _matches_kb
    return [p for p in policies if match(policy_filter(p), target)]


async def load_ticket_comments(db: AsyncSession, ticket_id: str) -> list[dict]:
    """The comment dicts ``compute_merge_readiness`` consumes: the oldest
    ``REVIEW_HISTORY_CAP`` comments (``review_state.load_review_comments``).
    Decision callers that need the truncation flag use
    ``load_review_comments`` directly (``ticket_readiness``)."""
    from sessionfs.server.services.review_state import load_review_comments

    comments, _truncated = await load_review_comments(db, ticket_id)
    return comments


async def merge_target(
    db: AsyncSession, ticket: Ticket, comments: Optional[list[dict]] = None
) -> dict:
    """The facts a merge policy filter matches on. Harms come from every
    finding (open or fixed) in trusted review comments and in the ticket's
    recorded agent-run findings; checklist classes from agent-run findings."""
    from sessionfs.server.services.review_state import compute_review_state

    if comments is None:
        comments = await load_ticket_comments(db, ticket.id)
    harms: set[str] = set()
    classes: set[str] = set()
    state = compute_review_state(comments)
    if state is not None:
        for f in state.open_findings + state.closed_findings:
            if f.harm and f.harm != "none":
                harms.add(f.harm)
    for (raw,) in (
        await db.execute(
            select(AgentRun.findings).where(
                AgentRun.ticket_id == ticket.id, AgentRun.project_id == ticket.project_id
            )
        )
    ).all():
        try:
            findings = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            continue
        if not isinstance(findings, list):
            continue
        for f in findings:
            if not isinstance(f, dict):
                continue
            harm = f.get("harm")
            if isinstance(harm, str) and harm in _HARMS:
                harms.add(harm)
            cls = f.get("class")
            if isinstance(cls, str) and cls != "new" and _SLUG_RE.fullmatch(cls):
                classes.add(cls)
    return {
        "project_id": ticket.project_id,
        "priority": ticket.priority,
        "kind": ticket.kind,
        "harms": harms,
        "classes": classes,
    }


# ── Requests ───────────────────────────────────────────────────────


async def _dialect_insert(db: AsyncSession):
    conn = await db.connection()
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert  # type: ignore[assignment]
    return insert(ApprovalRequest)


async def _expire_if_due(db: AsyncSession, req: ApprovalRequest, now: datetime) -> None:
    """Lazy expiry: a pending request past ``expires_at`` becomes expired
    (guarded UPDATE so a concurrent decision wins cleanly)."""
    expires = _aware(req.expires_at)
    if req.state != "pending" or expires is None or expires > now:
        return
    result = await db.execute(
        update(ApprovalRequest)
        .where(ApprovalRequest.id == req.id, ApprovalRequest.state == "pending")
        .values(state="expired", decided_at=now)
    )
    if result.rowcount == 1:  # type: ignore[attr-defined]
        req.state = "expired"
        req.decided_at = now
    else:
        await db.refresh(req)


async def approval_counts(
    db: AsyncSession, requests: Iterable[ApprovalRequest]
) -> dict[str, dict[int, int]]:
    """Approve decisions per request, by requirement index (one query)."""
    ids = [r.id for r in requests]
    out: dict[str, dict[int, int]] = {rid: {} for rid in ids}
    if not ids:
        return out
    for rid, idx, n in (
        await db.execute(
            select(
                ApprovalDecision.request_id,
                ApprovalDecision.requirement_index,
                func.count(),
            )
            .where(
                ApprovalDecision.request_id.in_(ids),
                ApprovalDecision.decision == "approve",
            )
            .group_by(ApprovalDecision.request_id, ApprovalDecision.requirement_index)
        )
    ).all():
        out[rid][int(idx)] = int(n)
    return out


def approved_count(requirements: list[dict], by_index: dict[int, int]) -> int:
    """Approvals that count toward quorum (each requirement capped at its count)."""
    return sum(
        min(int(r.get("count") or 0), by_index.get(i, 0)) for i, r in enumerate(requirements)
    )


def quorum_met(requirements: list[dict], by_index: dict[int, int]) -> bool:
    return bool(requirements) and all(
        by_index.get(i, 0) >= int(r.get("count") or 0) for i, r in enumerate(requirements)
    )


def _reason(state: str, name: Optional[str], got: int, need: int) -> str:
    """A readiness reason. ``name`` is the policy name for org members; the
    merge check and non-member readers get ``None`` (generic wording)."""
    label = f": {name}" if name else ""
    if state == "pending":
        if name:
            return f"Waiting on approval: {name} ({got} of {need})"
        return f"Needs approval ({got} of {need})"
    if state == "rejected":
        return f"Approval rejected{label}"
    if state == "expired":
        return f"Approval expired{label} (it is requested again once the review is clean)"
    if state == "superseded":
        return f"Approval superseded{label} (it is requested again once the review is clean)"
    if state == "limit":
        return f"Approval can't be requested{label} (too many open approval requests)"
    return f"Needs approval once the review is clean{label} (0 of {need})"


# A request in one of these states no longer holds its key: a fresh pending
# request can be raised beside it (the old row stays as history). A merge
# rejection stands for that exact commit (a new commit raises a new one).
_MERGE_REOPENABLE = frozenset({"expired", "superseded"})
_KB_REOPENABLE = frozenset({"expired", "superseded"})
_PENDING_WHERE = "state = 'pending'"
_REQUEST_KEY = ["policy_id", "target_kind", "target_id", "target_fingerprint"]


async def active_merge_policies(db: AsyncSession, org_id: str) -> list[ApprovalPolicy]:
    """Active merge policies of ``org_id``, oldest first."""
    return list(
        (
            await db.execute(
                select(ApprovalPolicy)
                .where(
                    ApprovalPolicy.org_id == org_id,
                    ApprovalPolicy.active.is_(True),
                    ApprovalPolicy.target_kind == "merge",
                )
                .order_by(ApprovalPolicy.created_at, ApprovalPolicy.id)
            )
        ).scalars().all()
    )


async def evaluate_approvals(
    db: AsyncSession,
    ticket: Ticket,
    sha: str,
    *,
    create: bool = True,
    comments: Optional[list[dict]] = None,
    show_names: bool = False,
    project: Optional[Project] = None,
    policies: Optional[list[ApprovalPolicy]] = None,
) -> dict:
    """Approval state of ``ticket`` at the exact commit ``sha``.

    With ``create`` (callers pass it when the work is otherwise ready), one
    request per matching active policy is inserted or loaded for this sha
    (``ON CONFLICT DO NOTHING`` on the pending-only unique key, then a read,
    so concurrent readiness reads converge on one row); an expired or
    superseded request for this sha is raised again. Otherwise existing
    requests are only read and a missing one reports ``not_requested``. A
    request raised under an older version of the same policy (same lineage)
    for this sha stays on that version. The caller commits.

    ``show_names`` puts policy names in the reasons and ``approvals`` (org
    members only; the merge check and other readers get generic wording).
    ``project`` / ``policies`` (the org's active merge policies) let a caller
    that evaluates many tickets load them once.

    Returns ``{"blocking_pending": [reason], "warnings": [reason],
    "approvals": [{request_id, policy_id, policy_name, state, blocking,
    approved_count, required_count}]}``. Personal projects have no policies.
    """
    empty: dict = {"blocking_pending": [], "warnings": [], "approvals": []}
    if project is None or project.id != ticket.project_id:
        project = await db.get(Project, ticket.project_id)
    if project is None or not project.org_id:
        return empty
    org_id = project.org_id
    if policies is None:
        policies = await active_merge_policies(db, org_id)
    if not policies:
        return empty
    target = await merge_target(db, ticket, comments)
    matched = [p for p in policies if _matches_merge(policy_filter(p), target)]
    unmatched = [p for p in policies if p not in matched]
    if unmatched:
        # Sticky match: once this exact policy version raised a request for
        # the ticket (any commit), editing the ticket (lowering its priority,
        # say) can't make the policy stop applying. Only a policy edit (a new
        # version) or deactivation re-evaluates the filter.
        raised = set(
            (
                await db.execute(
                    select(ApprovalRequest.policy_id)
                    .where(
                        ApprovalRequest.target_kind == "merge",
                        ApprovalRequest.target_id == ticket.id,
                        ApprovalRequest.policy_id.in_([p.id for p in unmatched]),
                    )
                    .distinct()
                )
            ).scalars().all()
        )
        if raised:
            matched = [p for p in policies if p in matched or p.id in raised]
    if not matched:
        return empty

    async def _existing() -> list[tuple[ApprovalRequest, str]]:
        return [
            (req, lineage)
            for req, lineage in (
                await db.execute(
                    select(ApprovalRequest, ApprovalPolicy.lineage_id)
                    .join(ApprovalPolicy, ApprovalPolicy.id == ApprovalRequest.policy_id)
                    .where(
                        ApprovalRequest.target_kind == "merge",
                        ApprovalRequest.target_id == ticket.id,
                        ApprovalRequest.target_fingerprint == sha,
                    )
                    .order_by(ApprovalRequest.requested_at, ApprovalRequest.id)
                )
            ).all()
        ]

    now = _now()
    rows = await _existing()
    for req, _lin in rows:
        await _expire_if_due(db, req, now)
    missing = [
        p for p in matched
        if (picked := _pick_request(rows, p)) is None or picked.state in _MERGE_REOPENABLE
    ]
    limited: set[str] = set()
    if create and missing:
        # Older shas' pending requests are superseded below, so the cap
        # counts the open requests on this sha only.
        room = MAX_PENDING_PER_TARGET - sum(1 for r, _ in rows if r.state == "pending")
        inserted_ids: list[str] = []
        for policy in missing:
            if room <= 0:
                limited.add(policy.id)
                continue
            rid = new_id("ar")
            stmt = (await _dialect_insert(db)).values(
                id=rid,
                policy_id=policy.id,
                org_id=org_id,
                project_id=ticket.project_id,
                target_kind="merge",
                target_id=ticket.id,
                target_fingerprint=sha,
                state="pending",
                requested_at=now,
                expires_at=now + REQUEST_TTL,
            ).on_conflict_do_nothing(
                index_elements=_REQUEST_KEY, index_where=text(_PENDING_WHERE)
            )
            result = await db.execute(stmt)
            if result.rowcount == 1:  # type: ignore[attr-defined]
                inserted_ids.append(rid)
                room -= 1
        if inserted_ids:
            # A new commit supersedes every request still pending on an older one.
            await db.execute(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.target_kind == "merge",
                    ApprovalRequest.target_id == ticket.id,
                    ApprovalRequest.target_fingerprint != sha,
                    ApprovalRequest.state == "pending",
                )
                .values(state="superseded", decided_at=now)
                .execution_options(synchronize_session=False)
            )
            await notify_approval_requested(db, inserted_ids, project=project)
        rows = await _existing()

    picked = {p.id: _pick_request(rows, p) for p in matched}
    reqs = [r for r in picked.values() if r is not None]
    # Requirements come from the version the request was raised under.
    version_ids = {r.policy_id for r in reqs} - {p.id for p in matched}
    versions = {p.id: p for p in matched}
    if version_ids:
        for p in (
            await db.execute(select(ApprovalPolicy).where(ApprovalPolicy.id.in_(version_ids)))
        ).scalars().all():
            versions[p.id] = p
    counts = await approval_counts(db, reqs)

    out: dict = {"blocking_pending": [], "warnings": [], "approvals": []}
    for policy in matched:
        req = picked[policy.id]
        if req is not None:
            reqs_def = policy_requirements(versions[req.policy_id])
            state = req.state
            got = approved_count(reqs_def, counts.get(req.id, {}))
        else:
            reqs_def = policy_requirements(policy)
            state = "limit" if policy.id in limited else "not_requested"
            got = 0
        if policy.id in limited and req is not None and req.state in _MERGE_REOPENABLE:
            state = "limit"
        need = required_count(reqs_def)
        if state != "approved":
            bucket = "blocking_pending" if policy.blocking else "warnings"
            out[bucket].append(_reason(state, policy.name if show_names else None, got, need))
        out["approvals"].append(
            {
                "request_id": req.id if req is not None else None,
                "policy_id": policy.id,
                "policy_name": policy.name if show_names else None,
                "state": "not_requested" if state == "limit" else state,
                "blocking": bool(policy.blocking),
                "approved_count": got,
                "required_count": need,
            }
        )
    return out


def _pick_request(rows: list[tuple[ApprovalRequest, str]], policy: ApprovalPolicy):
    """The newest request for ``policy`` (this exact version first, else any
    version of its lineage)."""
    exact = [r for r, _ in rows if r.policy_id == policy.id]
    if exact:
        return exact[-1]
    same_lineage = [r for r, lin in rows if lin == policy.lineage_id]
    return same_lineage[-1] if same_lineage else None


def apply_approvals(readiness: dict, approvals: dict) -> dict:
    """Fold an ``evaluate_approvals`` result into a readiness dict (additive:
    ``warnings`` and ``approvals`` keys; blocking reasons join ``reasons``)."""
    reasons = list(readiness.get("reasons") or [])
    reasons.extend(approvals["blocking_pending"])
    return {
        **readiness,
        "ready": bool(readiness.get("ready")) and not approvals["blocking_pending"],
        "reasons": reasons,
        "warnings": list(approvals["warnings"]),
        "approvals": list(approvals["approvals"]),
    }


async def ticket_readiness(
    db: AsyncSession, ticket: Ticket, sha: str, *, show_names: bool = False
) -> dict:
    """Merge readiness with approvals — the one function the readiness route,
    the GitHub check and the GitLab status share. ``compute_merge_readiness``
    stays pure; approval requests are created only once the work is otherwise
    ready. ``show_names`` (org members reading the API only) puts policy
    names in the reasons; the check summary always stays generic. The caller
    commits (new requests are pending rows)."""
    from sessionfs.server.services.review_state import (
        compute_merge_readiness,
        load_implementer_identities,
        load_key_minters,
        load_review_comments,
    )

    # A thread longer than the cap fails closed (never ready, so no approval
    # request is raised on a partial history either).
    comments, truncated = await load_review_comments(db, ticket.id)
    identities = (await load_implementer_identities(db, [ticket]))[ticket.id]
    key_minters = await load_key_minters(db, comments, identities)
    readiness = compute_merge_readiness(
        comments=comments,
        sha=sha,
        implementer_identities=identities,
        key_minters=key_minters,
        history_truncated=truncated,
    )
    approvals = await evaluate_approvals(
        db, ticket, sha, create=bool(readiness["ready"]), comments=comments,
        show_names=show_names,
    )
    return apply_approvals(readiness, approvals)


# ── Notifications (A2) ─────────────────────────────────────────────

APPROVAL_REQUESTED_KIND = "approval_requested"
APPROVAL_DECIDED_KIND = "approval_decided"
MAX_NOTIFY_RECIPIENTS = 200
_DECIDED_SUMMARY = {
    "approved": "Approval granted",
    "rejected": "Approval rejected",
}


async def eligible_approver_ids(
    db: AsyncSession,
    req: ApprovalRequest,
    policy: ApprovalPolicy,
    project: Optional[Project],
    *,
    self_ids: Optional[set[str]] = None,
    self_known: bool = False,
    members: Optional[list[tuple[str, str]]] = None,
    readers: Optional[set[str]] = None,
) -> list[str]:
    """People who could decide ``req`` right now: active org members who fit
    a requirement of the request's policy version, are not the target's
    maker, and can read the target's project. Nobody when the maker is
    unknown (fail closed) or the project left the request's org.

    A fixed number of queries whatever the org size; ``members`` (active
    ``(user_id, role)`` rows of the org) and ``readers``
    (``org_member_reader_ids``) let a caller notifying many requests load
    them once."""
    from sessionfs.server.auth.project_access import org_member_reader_ids

    if project is None or project.org_id != req.org_id:
        return []
    if not self_known:
        self_ids = await self_identities(db, req)
    if self_ids is None:
        return []
    requirements = policy_requirements(policy)
    if members is None:
        members = await active_members(db, req.org_id)
    if readers is None:
        readers = await org_member_reader_ids(db, project)
    out: list[str] = []
    for uid, role in members:
        if uid in self_ids or uid not in readers:
            continue
        if not fitting_requirements(requirements, uid, role):
            continue
        out.append(uid)
        if len(out) >= MAX_NOTIFY_RECIPIENTS:
            break
    return out


async def active_members(db: AsyncSession, org_id: str) -> list[tuple[str, str]]:
    """``(user_id, role)`` of every active member of ``org_id`` (one query)."""
    return [
        (uid, role)
        for uid, role in (
            await db.execute(
                select(OrgMember.user_id, OrgMember.role)
                .join(User, User.id == OrgMember.user_id)
                .where(OrgMember.org_id == org_id, User.is_active.is_(True))
                .order_by(OrgMember.user_id)
            )
        ).all()
    ]


def _approval_notification(
    *, user_id: str, req: ApprovalRequest, kind: str, summary: str,
    actor_user_id: Optional[str], now: datetime,
) -> TicketNotification:
    # Summaries are fixed server strings; titles are derived at read time
    # after the reader's access is re-checked, so nothing about the target
    # is stored in the row.
    return TicketNotification(
        user_id=user_id,
        subject_kind="approval",
        subject_id=req.id,
        ticket_id=None,
        project_id=req.project_id,
        kind=kind,
        summary=summary,
        actor_type="user",
        actor_user_id=actor_user_id,
        service_key_name=None,
        event_count=1,
        created_at=now,
        updated_at=now,
    )


async def notify_approval_requested(
    db: AsyncSession,
    request_ids: list[str],
    *,
    actor_user_id: Optional[str] = None,
    project: Optional[Project] = None,
) -> int:
    """One ``approval_requested`` notification per eligible approver for
    each newly created request. Returns the number of rows written.

    Best effort, like ticket notifications: it runs in a SAVEPOINT and a
    failure is logged and swallowed, never failing the write that created
    the request."""
    if not request_ids:
        return 0
    try:
        async with db.begin_nested():
            return await _notify_approval_requested(
                db, request_ids, actor_user_id=actor_user_id, project=project
            )
    except Exception as exc:  # pragma: no cover - defensive side channel
        logger.warning("approval_requested notification failed: %s", type(exc).__name__)
        return 0


async def _notify_approval_requested(
    db: AsyncSession,
    request_ids: list[str],
    *,
    actor_user_id: Optional[str],
    project: Optional[Project],
) -> int:
    reqs = list(
        (
            await db.execute(select(ApprovalRequest).where(ApprovalRequest.id.in_(request_ids)))
        ).scalars().all()
    )
    policies = {
        p.id: p
        for p in (
            await db.execute(
                select(ApprovalPolicy).where(ApprovalPolicy.id.in_({r.policy_id for r in reqs}))
            )
        ).scalars().all()
    }
    from sessionfs.server.auth.project_access import org_member_reader_ids

    now = _now()
    written = 0
    projects = {
        p.id: p
        for p in (
            await db.execute(select(Project).where(Project.id.in_({r.project_id for r in reqs})))
        ).scalars().all()
    }
    if project is not None:
        projects[project.id] = project
    self_by_req = await self_identities_many(db, reqs)
    members_by_org: dict[str, list[tuple[str, str]]] = {}
    readers_by_project: dict[str, set[str]] = {}
    for req in reqs:
        policy = policies.get(req.policy_id)
        proj = projects.get(req.project_id)
        if policy is None or proj is None or proj.org_id != req.org_id:
            continue
        if req.org_id not in members_by_org:
            members_by_org[req.org_id] = await active_members(db, req.org_id)
        if proj.id not in readers_by_project:
            readers_by_project[proj.id] = await org_member_reader_ids(db, proj)
        for uid in await eligible_approver_ids(
            db, req, policy, proj, self_ids=self_by_req.get(req.id), self_known=True,
            members=members_by_org[req.org_id], readers=readers_by_project[proj.id],
        ):
            db.add(
                _approval_notification(
                    user_id=uid, req=req, kind=APPROVAL_REQUESTED_KIND,
                    summary="Approval requested", actor_user_id=actor_user_id, now=now,
                )
            )
            written += 1
    return written


async def requester_ids(db: AsyncSession, req: ApprovalRequest) -> list[str]:
    """The requester side of a request: the ticket's creator and assignee
    (merge) or the entry's author (kb_promote). Active users only."""
    ids: list[str] = []
    if req.target_kind == "merge":
        ticket = await db.get(Ticket, req.target_id)
        if ticket is not None:
            ids = [i for i in (ticket.created_by_user_id, ticket.assigned_user_id) if i]
    elif req.target_kind == "kb_promote":
        entry = await load_kb_entry(db, req)
        if entry is not None and entry.user_id:
            ids = [entry.user_id]
    if not ids:
        return []
    active = set(
        (
            await db.execute(
                select(User.id).where(User.id.in_(ids), User.is_active.is_(True))
            )
        ).scalars().all()
    )
    return [i for i in dict.fromkeys(ids) if i in active]


async def notify_approval_decided(
    db: AsyncSession, req: ApprovalRequest, *, decider_id: str
) -> int:
    """An ``approval_decided`` notification to the requester side once a
    request settles as approved or rejected (never to the decider). Best
    effort (SAVEPOINT, failures logged and swallowed)."""
    summary = _DECIDED_SUMMARY.get(req.state)
    if summary is None:
        return 0
    try:
        async with db.begin_nested():
            return await _notify_approval_decided(db, req, summary, decider_id)
    except Exception as exc:  # pragma: no cover - defensive side channel
        logger.warning("approval_decided notification failed: %s", type(exc).__name__)
        return 0


async def _notify_approval_decided(
    db: AsyncSession, req: ApprovalRequest, summary: str, decider_id: str
) -> int:
    now = _now()
    written = 0
    for uid in await requester_ids(db, req):
        if uid == decider_id:
            continue
        db.add(
            _approval_notification(
                user_id=uid, req=req, kind=APPROVAL_DECIDED_KIND, summary=summary,
                actor_user_id=decider_id, now=now,
            )
        )
        written += 1
    return written


MAX_NOTIFICATION_SUBJECTS = 100


async def approval_notification_subjects(
    db: AsyncSession, user: User, notifications: list[TicketNotification]
) -> dict[int, dict]:
    """Re-check, at read time, which approval notifications ``user`` may
    still see, and build their subject. ``approval_requested`` rows need the
    reader to still be able to decide the request (pending, eligible, not
    the maker, can read the target's project); ``approval_decided`` rows
    need read access to the target's live project. Everything else is left
    out, so no request detail reaches someone who lost access.

    A fixed number of queries per call; at most ``MAX_NOTIFICATION_SUBJECTS``
    distinct requests are resolved (the newest notifications first), the
    rest are left out."""
    from sessionfs.server.auth.project_access import accessible_project_ids_subquery

    rows = [n for n in notifications if n.subject_kind == "approval" and n.subject_id]
    if not rows or not user.is_active:
        return {}
    wanted: list[str] = []
    for n in sorted(rows, key=lambda n: n.id or 0, reverse=True):
        if n.subject_id not in wanted:
            wanted.append(n.subject_id)  # type: ignore[arg-type]
        if len(wanted) >= MAX_NOTIFICATION_SUBJECTS:
            break
    reqs = {
        r.id: r
        for r in (
            await db.execute(select(ApprovalRequest).where(ApprovalRequest.id.in_(wanted)))
        ).scalars().all()
    }
    project_orgs = dict(
        (
            await db.execute(
                select(Project.id, Project.org_id).where(
                    Project.id.in_({r.project_id for r in reqs.values()}),
                    Project.id.in_(await accessible_project_ids_subquery(db, user.id)),
                )
            )
        ).all()
    ) if reqs else {}
    visible_reqs = [
        r for r in reqs.values()
        if r.project_id in project_orgs and project_orgs[r.project_id] == r.org_id
    ]
    visible_ids = {r.id for r in visible_reqs}
    decidable = {r.id for r in await decidable_by(db, user, visible_reqs)}
    policies = {
        p.id: p
        for p in (
            await db.execute(
                select(ApprovalPolicy).where(
                    ApprovalPolicy.id.in_({r.policy_id for r in visible_reqs})
                )
            )
        ).scalars().all()
    } if visible_reqs else {}
    member_orgs = set(
        (
            await db.execute(
                select(OrgMember.org_id).where(
                    OrgMember.user_id == user.id,
                    OrgMember.org_id.in_({r.org_id for r in visible_reqs}),
                )
            )
        ).scalars().all()
    ) if visible_reqs else set()
    titles = await target_titles(db, visible_reqs)
    out: dict[int, dict] = {}
    for n in rows:
        req = reqs.get(n.subject_id or "")
        if req is None or req.id not in visible_ids:
            continue
        if n.kind == APPROVAL_REQUESTED_KIND and req.id not in decidable:
            continue
        policy = policies.get(req.policy_id)
        # Policy names are for org members; anyone else sees "Approval".
        name = policy.name if policy is not None and req.org_id in member_orgs else "Approval"
        target_title = titles.get(req.id)
        out[n.id] = {
            "id": req.id,
            "title": f"{name}: {target_title}" if target_title else name,
            "project_id": req.project_id,
            "target_kind": req.target_kind,
            "target_id": req.target_id,
            "state": req.state,
        }
    return out


async def target_titles(db: AsyncSession, reqs: list[ApprovalRequest]) -> dict[str, str]:
    """A short display title per request: the ticket title (merge) or the
    first 80 characters of the entry (kb_promote). Callers re-check access
    before calling."""
    out: dict[str, str] = {}
    ticket_ids = [r.target_id for r in reqs if r.target_kind == "merge"]
    if ticket_ids:
        tmap = dict(
            (await db.execute(select(Ticket.id, Ticket.title).where(Ticket.id.in_(ticket_ids)))).all()
        )
        for r in reqs:
            if r.target_kind == "merge" and tmap.get(r.target_id):
                out[r.id] = tmap[r.target_id]
    entry_ids: list[int] = []
    for r in reqs:
        if r.target_kind == "kb_promote":
            try:
                entry_ids.append(int(r.target_id))
            except (TypeError, ValueError):
                continue
    if entry_ids:
        emap = {
            (str(i), pid): c
            for i, pid, c in (
                await db.execute(
                    select(KnowledgeEntry.id, KnowledgeEntry.project_id, KnowledgeEntry.content)
                    .where(KnowledgeEntry.id.in_(entry_ids))
                )
            ).all()
        }
        for r in reqs:
            content = emap.get((r.target_id, r.project_id))
            if r.target_kind == "kb_promote" and content:
                text = " ".join(content.split())
                out[r.id] = text if len(text) <= 80 else text[:79] + "…"
    return out


# ── KB promotion gate (A2) ─────────────────────────────────────────


def entry_fingerprint(content: Optional[str]) -> str:
    """The KB request fingerprint: sha256 of the entry content."""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _kb_target(entry: KnowledgeEntry) -> dict:
    return {
        "project_id": entry.project_id,
        "entry_type": entry.entry_type,
        "entity_ref": entry.entity_ref,
    }


async def load_kb_entry(
    db: AsyncSession, request: ApprovalRequest, *, for_update: bool = False
) -> Optional[KnowledgeEntry]:
    """The knowledge entry a kb_promote request points at (same project)."""
    try:
        entry_id = int(request.target_id)
    except (TypeError, ValueError):
        return None
    stmt = select(KnowledgeEntry).where(
        KnowledgeEntry.id == entry_id, KnowledgeEntry.project_id == request.project_id
    )
    if for_update:
        # Fresh column values: a stale identity-map copy would make the
        # settle step compare against old state and skip its writes.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one_or_none()


async def active_kb_policies(db: AsyncSession, org_id: str) -> list[ApprovalPolicy]:
    return list(
        (
            await db.execute(
                select(ApprovalPolicy)
                .where(
                    ApprovalPolicy.org_id == org_id,
                    ApprovalPolicy.active.is_(True),
                    ApprovalPolicy.target_kind == "kb_promote",
                )
                .order_by(ApprovalPolicy.created_at, ApprovalPolicy.id)
            )
        ).scalars().all()
    )


@dataclass
class KbGate:
    """Outcome of ``kb_promotion_gate``. ``blocked`` means a blocking policy
    is not yet approved for this exact content; ``warnings`` carry advisory
    policies (and never block)."""

    blocked: bool = False
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    approvals: list[dict] = field(default_factory=list)
    created_request_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "approval_pending": self.blocked,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "approvals": list(self.approvals),
        }


def _kb_reason(state: str, name: str, got: int, need: int) -> str:
    if state == "pending":
        return f"Waiting on approval: {name} ({got} of {need})"
    if state == "rejected":
        return f"Approval rejected: {name}"
    if state == "expired":
        return f"Approval expired: {name}"
    if state == "superseded":
        return f"Approval superseded: {name} (promote the entry again to request it)"
    return f"Needs approval: {name} (0 of {need})"


async def kb_promotion_gate(
    db: AsyncSession,
    entry: KnowledgeEntry,
    actor: Optional[str] = None,
    *,
    project: Optional[Project] = None,
    policies: Optional[list[ApprovalPolicy]] = None,
    create: bool = True,
    reraise_rejected: bool = False,
) -> KbGate:
    """May ``entry`` become a claim now? Called by every promote path
    (manual, bulk, auto-promotion at compile, and the classify-as-claim step
    of add) under the project scope lock.

    Matching active BLOCKING kb_promote policies of the project's org each
    need an approved request for this entry's current content (fingerprint =
    sha256 of the content). With ``create`` a missing request is inserted or
    loaded (same unique key as merge requests), pending requests for older
    content are superseded, every eligible approver is notified, and a
    blocked entry is marked ``approval_pending``. Without ``create`` (dry
    runs) nothing is written. Advisory policies only add ``warnings``.
    ``actor`` is the user id behind the promote attempt (recorded on the
    approvers' notifications). An expired or superseded request for this
    content is raised again; a rejected one only with ``reraise_rejected``
    (a person's explicit promote attempt, never the automatic compile
    pass). Personal projects have no policies. The caller commits.
    """
    gate = KbGate()
    if project is None or project.id != entry.project_id:
        project = await db.get(Project, entry.project_id)
    if project is None or not project.org_id:
        return gate
    org_id = project.org_id
    if policies is None:
        policies = await active_kb_policies(db, org_id)
    if not policies:
        return gate
    target = _kb_target(entry)
    matched = [p for p in policies if _matches_kb(policy_filter(p), target)]
    if not matched:
        return gate
    gate.warnings = [f"Advisory approval policy: {p.name}" for p in matched if not p.blocking]
    blocking = [p for p in matched if p.blocking]
    if not blocking:
        return gate

    fp = entry_fingerprint(entry.content)
    target_id = str(entry.id)

    async def _existing() -> list[tuple[ApprovalRequest, str]]:
        return [
            (req, lineage)
            for req, lineage in (
                await db.execute(
                    select(ApprovalRequest, ApprovalPolicy.lineage_id)
                    .join(ApprovalPolicy, ApprovalPolicy.id == ApprovalRequest.policy_id)
                    .where(
                        ApprovalRequest.target_kind == "kb_promote",
                        ApprovalRequest.target_id == target_id,
                        ApprovalRequest.target_fingerprint == fp,
                        ApprovalRequest.project_id == entry.project_id,
                    )
                    .order_by(ApprovalRequest.requested_at, ApprovalRequest.id)
                )
            ).all()
        ]

    reopenable = _KB_REOPENABLE | ({"rejected"} if reraise_rejected else set())
    now = _now()
    rows = await _existing()
    if create:
        for req, _lin in rows:
            await _expire_if_due(db, req, now)
    missing = [
        p for p in blocking
        if (picked := _pick_request(rows, p)) is None or picked.state in reopenable
    ]
    if create and missing:
        room = MAX_PENDING_PER_TARGET - sum(1 for r, _ in rows if r.state == "pending")
        for policy in missing:
            if room <= 0:
                break
            rid = new_id("ar")
            stmt = (await _dialect_insert(db)).values(
                id=rid,
                policy_id=policy.id,
                org_id=org_id,
                project_id=entry.project_id,
                target_kind="kb_promote",
                target_id=target_id,
                target_fingerprint=fp,
                state="pending",
                requested_at=now,
                expires_at=now + REQUEST_TTL,
            ).on_conflict_do_nothing(
                index_elements=_REQUEST_KEY, index_where=text(_PENDING_WHERE)
            )
            result = await db.execute(stmt)
            if result.rowcount == 1:  # type: ignore[attr-defined]
                gate.created_request_ids.append(rid)
                room -= 1
        if gate.created_request_ids:
            # The content changed since an older request: that one is moot.
            await db.execute(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.target_kind == "kb_promote",
                    ApprovalRequest.target_id == target_id,
                    ApprovalRequest.project_id == entry.project_id,
                    ApprovalRequest.target_fingerprint != fp,
                    ApprovalRequest.state == "pending",
                )
                .values(state="superseded", decided_at=now)
                .execution_options(synchronize_session=False)
            )
        rows = await _existing()

    picked = {p.id: _pick_request(rows, p) for p in blocking}
    reqs = [r for r in picked.values() if r is not None]
    versions = {p.id: p for p in blocking}
    version_ids = {r.policy_id for r in reqs} - set(versions)
    if version_ids:
        for p in (
            await db.execute(select(ApprovalPolicy).where(ApprovalPolicy.id.in_(version_ids)))
        ).scalars().all():
            versions[p.id] = p
    counts = await approval_counts(db, reqs)
    for policy in blocking:
        req = picked[policy.id]
        if req is not None:
            reqs_def = policy_requirements(versions.get(req.policy_id, policy))
            state = req.state
            got = approved_count(reqs_def, counts.get(req.id, {}))
        else:
            reqs_def = policy_requirements(policy)
            state, got = "not_requested", 0
        need = required_count(reqs_def)
        if state != "approved":
            gate.blocked = True
            gate.reasons.append(_kb_reason(state, policy.name, got, need))
        gate.approvals.append(
            {
                "request_id": req.id if req is not None else None,
                "policy_id": policy.id,
                "policy_name": policy.name,
                "state": state,
                "blocking": True,
                "approved_count": got,
                "required_count": need,
            }
        )
    if create:
        if gate.blocked:
            entry.approval_pending = True
        if gate.created_request_ids:
            await notify_approval_requested(
                db, gate.created_request_ids, actor_user_id=actor, project=project
            )
    return gate


async def settle_kb_request(
    db: AsyncSession, req: ApprovalRequest, *, approver_id: str
) -> None:
    """Called once a kb_promote request reached ``approved`` (inside the
    deciding transaction, under the project scope lock). Promotes the entry
    only when its content still hashes to the request's fingerprint and no
    other blocking policy still holds it; when the content changed (or the
    entry is gone, dismissed or superseded) the request becomes
    ``superseded`` instead."""
    entry = await load_kb_entry(db, req, for_update=True)
    now = _now()
    if (
        entry is None
        or entry.dismissed
        or entry.superseded_by is not None
        or entry_fingerprint(entry.content) != req.target_fingerprint
    ):
        result = await db.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == req.id, ApprovalRequest.state == "approved")
            .values(state="superseded", decided_at=now)
        )
        if result.rowcount == 1:  # type: ignore[attr-defined]
            req.state = "superseded"
            req.decided_at = now
        return
    if entry.claim_class == "claim":
        entry.approval_pending = False
        await apply_pending_supersede(db, entry, actor_id=approver_id)
        return
    gate = await kb_promotion_gate(db, entry, approver_id)
    if gate.blocked:
        return
    entry.claim_class = "claim"
    entry.promoted_at = now
    entry.promoted_by = approver_id
    entry.approval_pending = False
    await apply_pending_supersede(db, entry, actor_id=approver_id)


async def apply_pending_supersede(
    db: AsyncSession, entry: KnowledgeEntry, *, actor_id: Optional[str]
) -> list[int]:
    """Finish an entity_ref upsert that was held for approval: now that
    ``entry`` really is a claim, supersede the entries it was written to
    replace (only those still active — a guarded UPDATE, like the upsert
    itself). Returns the ids that transitioned. No-op without a pending
    list."""
    raw = getattr(entry, "pending_supersede_ids", None)
    if not raw:
        return []
    entry.pending_supersede_ids = None
    try:
        ids = [int(i) for i in json.loads(raw) if int(i) != entry.id]
    except (TypeError, ValueError):
        return []
    if not ids:
        return []
    now = _now()
    result = await db.execute(
        update(KnowledgeEntry)
        .where(
            KnowledgeEntry.id.in_(ids),
            KnowledgeEntry.project_id == entry.project_id,
            KnowledgeEntry.dismissed == False,  # noqa: E712
            KnowledgeEntry.superseded_by.is_(None),
        )
        .values(
            dismissed=True,
            dismissed_at=now,
            dismissed_by=actor_id,
            dismissed_reason=f"entity_ref upsert: superseded by entry {entry.id}",
            superseded_by=entry.id,
            supersession_reason="entity_ref upsert",
        )
        .execution_options(synchronize_session=False)
    )
    n = int(result.rowcount or 0)  # type: ignore[attr-defined]
    return ids[:n] if n > 0 else []


async def _lock_request_project(db: AsyncSession, request_id: str) -> None:
    """kb_promote decisions take the project scope lock BEFORE the request
    row lock — the same order every promote path uses (scope lock, then
    the request rows the gate touches)."""
    from sessionfs.server.services.scope_locks import lock_project_scope

    peek = (
        await db.execute(
            select(ApprovalRequest.target_kind, ApprovalRequest.project_id).where(
                ApprovalRequest.id == request_id
            )
        )
    ).first()
    if peek is None or peek[0] != "kb_promote":
        return
    project = await db.get(Project, peek[1])
    if project is not None:
        await lock_project_scope(db, project, require_live=False)


# ── Decisions ──────────────────────────────────────────────────────


async def active_member_role(db: AsyncSession, org_id: str, user_id: str) -> Optional[str]:
    """The user's role in ``org_id`` when they are an active member, else None."""
    return (
        await db.execute(
            select(OrgMember.role)
            .join(User, User.id == OrgMember.user_id)
            .where(
                OrgMember.org_id == org_id,
                OrgMember.user_id == user_id,
                User.is_active.is_(True),
            )
        )
    ).scalars().first()


async def is_active_org_member(
    db: AsyncSession, org_id: Optional[str], user_id: Optional[str]
) -> bool:
    """True when ``user_id`` is an active member of ``org_id``."""
    if not org_id or not user_id:
        return False
    return await active_member_role(db, org_id, user_id) is not None


def fitting_requirements(requirements: list[dict], user_id: str, role: Optional[str]) -> list[int]:
    """Indices of the requirements this person can satisfy."""
    if role is None:
        return []
    out: list[int] = []
    for i, r in enumerate(requirements):
        if r.get("who") == "org_role" and has_minimum_role(role, str(r.get("role"))):
            out.append(i)
        elif r.get("who") == "users" and user_id in (r.get("user_ids") or []):
            out.append(i)
    return out


async def self_identities_many(
    db: AsyncSession, requests: Iterable[ApprovalRequest]
) -> dict[str, Optional[set[str]]]:
    """``self_identities`` for many requests, by request id, in a fixed
    number of queries (tickets, entries, implementers and key minters are
    each loaded once for all of them)."""
    from sessionfs.server.services.review_state import (
        load_implementer_identities,
        load_key_minters,
    )

    reqs = list(requests)
    out: dict[str, Optional[set[str]]] = {r.id: None for r in reqs}
    merge = [r for r in reqs if r.target_kind == "merge"]
    kb = [r for r in reqs if r.target_kind == "kb_promote"]

    tickets: dict[str, Ticket] = {}
    if merge:
        tickets = {
            t.id: t
            for t in (
                await db.execute(
                    select(Ticket).where(Ticket.id.in_({r.target_id for r in merge}))
                )
            ).scalars().all()
        }
    implementers = await load_implementer_identities(db, tickets.values()) if tickets else {}
    entries: dict[tuple[str, str], KnowledgeEntry] = {}
    entry_ids: set[int] = set()
    for r in kb:
        try:
            entry_ids.add(int(r.target_id))
        except (TypeError, ValueError):
            continue
    if entry_ids:
        for e in (
            await db.execute(select(KnowledgeEntry).where(KnowledgeEntry.id.in_(entry_ids)))
        ).scalars().all():
            entries[(str(e.id), e.project_id)] = e

    key_ids: list[tuple[Optional[str], Optional[str]]] = [
        (svc or None, usr or None)
        for ids in implementers.values()
        for svc, usr in ids
        if svc or usr
    ]
    key_ids.extend((e.service_key_id, None) for e in entries.values() if e.service_key_id)
    minters = await load_key_minters(db, [], key_ids) if key_ids else {}

    for r in merge:
        ticket = tickets.get(r.target_id)
        if ticket is None:
            continue
        identities = [
            (svc or None, usr or None) for svc, usr in implementers.get(ticket.id, []) if svc or usr
        ]
        if not identities:
            continue
        ids: Optional[set[str]] = set()
        for svc, usr in identities:
            if usr:
                ids.add(usr)  # type: ignore[union-attr]
            if svc:
                minted_by = minters.get(svc)
                if minted_by is None:
                    ids = None
                    break
                ids.update(minted_by)  # type: ignore[union-attr]
        out[r.id] = ids
    for r in kb:
        entry = entries.get((r.target_id, r.project_id))
        if entry is None:
            continue
        kb_ids: set[str] = {entry.user_id} if entry.user_id else set()
        if entry.actor_type == "service_key" or entry.service_key_id:
            minted = minters.get(entry.service_key_id) if entry.service_key_id else None
            if minted is None:
                continue
            kb_ids.update(minted)
        out[r.id] = kb_ids or None
    return out


async def self_identities(db: AsyncSession, request: ApprovalRequest) -> Optional[set[str]]:
    """The user ids that count as the target's own maker: the ticket's
    implementers (merge) or the KB entry's author (kb_promote), with service
    keys resolved to everyone who minted them. ``None`` means unknown — no
    recorded implementer, a missing entry, or an unresolvable minter — and
    every caller treats that as "nobody may approve" (fail closed)."""
    return (await self_identities_many(db, [request]))[request.id]


async def is_implementer(db: AsyncSession, request: ApprovalRequest, user_id: str) -> bool:
    """True when ``user_id`` is (or may be) the target's implementer or
    author. Fails closed (see ``self_identities``)."""
    ids = await self_identities(db, request)
    return ids is None or user_id in ids


async def decide(
    db: AsyncSession,
    *,
    request_id: str,
    approver: User,
    decision: str,
    reason: str,
) -> ApprovalRequest:
    """Record ``approver``'s decision and settle the request when it can.

    The caller has already checked visibility and sanitised + DLP-scanned
    ``reason``. The caller commits."""
    if decision not in ("approve", "reject"):
        raise _err(422, "invalid_decision", "decision must be approve or reject.")
    await _lock_request_project(db, request_id)
    req = (
        await db.execute(
            select(ApprovalRequest)
            .where(ApprovalRequest.id == request_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if req is None:
        raise HTTPException(404, REQUEST_NOT_FOUND)
    # Re-checked under the row lock: the project may have moved out of the
    # request's org, or the approver lost read access, since the caller's
    # visibility check. Same 404 as an unknown request.
    from sessionfs.server.auth.project_access import user_can_access_project

    project = await db.get(Project, req.project_id)
    if (
        project is None
        or project.org_id != req.org_id
        or not await user_can_access_project(db, approver.id, project)
    ):
        raise HTTPException(404, REQUEST_NOT_FOUND)
    now = _now()
    await _expire_if_due(db, req, now)
    if req.state != "pending":
        raise _err(409, "request_not_pending", "This approval request is no longer open.",
                   state=req.state)

    policy = await db.get(ApprovalPolicy, req.policy_id)
    requirements = policy_requirements(policy) if policy is not None else []
    role = await active_member_role(db, req.org_id, approver.id)
    fits = fitting_requirements(requirements, approver.id, role) if approver.is_active else []
    if not fits:
        raise _err(403, "not_an_approver", "You are not an approver for this request.")
    if await is_implementer(db, req, approver.id):
        raise _err(
            403, "self_approval_forbidden",
            "You can't approve work you implemented (or whose implementer is unknown).",
        )
    already = (
        await db.execute(
            select(ApprovalDecision.id).where(
                ApprovalDecision.request_id == req.id,
                ApprovalDecision.approver_user_id == approver.id,
            )
        )
    ).scalar_one_or_none()
    if already is not None:
        raise _err(409, "already_decided", "You have already decided on this request.")

    by_index = (await approval_counts(db, [req]))[req.id]
    index = next(
        (i for i in fits if by_index.get(i, 0) < int(requirements[i].get("count") or 0)),
        fits[0],
    )
    try:
        async with db.begin_nested():
            db.add(
                ApprovalDecision(
                    id=new_id("ad"),
                    request_id=req.id,
                    approver_user_id=approver.id,
                    requirement_index=index,
                    decision=decision,
                    reason=reason,
                    decided_at=now,
                )
            )
    except IntegrityError:
        raise _err(409, "already_decided", "You have already decided on this request.") from None

    new_state: Optional[str] = None
    if decision == "reject":
        new_state = "rejected"
    else:
        by_index[index] = by_index.get(index, 0) + 1
        if quorum_met(requirements, by_index):
            new_state = "approved"
    if new_state is not None:
        result = await db.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == req.id, ApprovalRequest.state == "pending")
            .values(state=new_state, decided_at=now)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise _err(409, "request_not_pending", "This approval request is no longer open.")
        req.state = new_state
        req.decided_at = now
        if new_state == "approved" and req.target_kind == "kb_promote":
            await settle_kb_request(db, req, approver_id=approver.id)
        await notify_approval_decided(db, req, decider_id=approver.id)
    return req


async def decidable_by(
    db: AsyncSession, user: User, requests: list[ApprovalRequest]
) -> list[ApprovalRequest]:
    """Filter ``requests`` to the pending ones ``user`` could decide now.

    A fixed number of queries whatever the number of requests (policies,
    votes, roles and maker identities are each loaded once)."""
    if not requests or not user.is_active:
        return []
    now = _now()
    open_reqs = []
    for req in requests:
        expires = _aware(req.expires_at)
        if req.state == "pending" and (expires is None or expires > now):
            open_reqs.append(req)
    if not open_reqs:
        return []
    policies = {
        p.id: p
        for p in (
            await db.execute(
                select(ApprovalPolicy).where(
                    ApprovalPolicy.id.in_({r.policy_id for r in open_reqs})
                )
            )
        ).scalars().all()
    }
    voted = set(
        (
            await db.execute(
                select(ApprovalDecision.request_id).where(
                    ApprovalDecision.request_id.in_([r.id for r in open_reqs]),
                    ApprovalDecision.approver_user_id == user.id,
                )
            )
        ).scalars().all()
    )
    roles: dict[str, str] = dict(
        (
            await db.execute(
                select(OrgMember.org_id, OrgMember.role)
                .join(User, User.id == OrgMember.user_id)
                .where(
                    OrgMember.user_id == user.id,
                    OrgMember.org_id.in_({r.org_id for r in open_reqs}),
                    User.is_active.is_(True),
                )
            )
        ).all()
    )
    candidates = []
    for req in open_reqs:
        policy = policies.get(req.policy_id)
        if req.id in voted or policy is None:
            continue
        if not fitting_requirements(policy_requirements(policy), user.id, roles.get(req.org_id)):
            continue
        candidates.append(req)
    if not candidates:
        return []
    makers = await self_identities_many(db, candidates)
    return [
        r for r in candidates
        if makers.get(r.id) is not None and user.id not in makers[r.id]  # type: ignore[operator]
    ]


async def pending_for(
    db: AsyncSession,
    user: User,
    *,
    org_id: Optional[str] = None,
    project_ids: Optional[Iterable[str]] = None,
    limit: int = 100,
    scan: int = 500,
) -> list[ApprovalRequest]:
    """Pending requests ``user`` could decide now, newest first: in orgs they
    belong to, on projects they can read (optionally narrowed to one org and
    a project allowlist), filtered through ``decidable_by``."""
    from sessionfs.server.auth.project_access import accessible_project_ids_subquery

    stmt = (
        select(ApprovalRequest)
        .where(
            ApprovalRequest.state == "pending",
            ApprovalRequest.org_id.in_(
                select(OrgMember.org_id).where(OrgMember.user_id == user.id)
            ),
            ApprovalRequest.project_id.in_(await accessible_project_ids_subquery(db, user.id)),
        )
        .order_by(ApprovalRequest.requested_at.desc(), ApprovalRequest.id.desc())
        .limit(scan)
    )
    if org_id is not None:
        stmt = stmt.where(ApprovalRequest.org_id == org_id)
    if project_ids is not None:
        stmt = stmt.where(ApprovalRequest.project_id.in_(list(project_ids)))
    candidates = list((await db.execute(stmt)).scalars().all())
    return (await decidable_by(db, user, candidates))[:limit]


FORMER_MEMBER = "former member"


def redacted_requirements(requirements: list[dict]) -> list[dict]:
    """Requirements without named user ids (the view for readers who are not
    members of the org): who/role/count only."""
    return [{k: v for k, v in r.items() if k != "user_ids"} for r in requirements]


async def request_view(
    db: AsyncSession, req: ApprovalRequest, *, viewer: User, redact: bool = False
) -> dict:
    """The REST shape of one request, with its decisions.

    ``redact`` (readers who are not active members of the request's org —
    captured-session readers and service keys): no approver ids, names or
    emails and no named user ids in the requirements. A decision whose
    approver was deleted still counts and renders as "former member"."""
    policy = await db.get(ApprovalPolicy, req.policy_id)
    requirements = policy_requirements(policy) if policy is not None else []
    decisions = (
        await db.execute(
            select(ApprovalDecision, User.display_name, User.email)
            .join(User, User.id == ApprovalDecision.approver_user_id, isouter=True)
            .where(ApprovalDecision.request_id == req.id)
            .order_by(ApprovalDecision.decided_at, ApprovalDecision.id)
        )
    ).all()
    by_index: dict[int, int] = {}
    for d, _n, _e in decisions:
        if d.decision == "approve":
            by_index[d.requirement_index] = by_index.get(d.requirement_index, 0) + 1
    can_decide = False if redact else bool(await decidable_by(db, viewer, [req]))

    def _approver(d: ApprovalDecision, name: Optional[str], email: Optional[str]) -> dict:
        if redact:
            return {"approver_user_id": None, "approver_name": None}
        if d.approver_user_id is None:
            return {"approver_user_id": None, "approver_name": FORMER_MEMBER}
        return {"approver_user_id": d.approver_user_id, "approver_name": name or email}

    return {
        "id": req.id,
        "policy": {
            "id": policy.id if policy else req.policy_id,
            "name": policy.name if policy else None,
            "version": policy.version if policy else None,
            "blocking": bool(policy.blocking) if policy else True,
            "requirements": redacted_requirements(requirements) if redact else requirements,
        },
        "org_id": req.org_id,
        "project_id": req.project_id,
        "target_kind": req.target_kind,
        "target_id": req.target_id,
        "target_fingerprint": req.target_fingerprint,
        "state": req.state,
        "requested_at": _iso(req.requested_at),
        "expires_at": _iso(req.expires_at),
        "decided_at": _iso(req.decided_at),
        "approved_count": approved_count(requirements, by_index),
        "required_count": required_count(requirements),
        "can_decide": can_decide,
        "decisions": [
            {
                "id": d.id,
                **_approver(d, name, email),
                "requirement_index": d.requirement_index,
                "decision": d.decision,
                "reason": d.reason,
                "decided_at": _iso(d.decided_at),
            }
            for d, name, email in decisions
        ],
    }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    dt = _aware(dt)
    return dt.isoformat() if dt is not None else None


# ── For You feed + workflow gates (A4) ─────────────────────────────

ATTENTION_LIMIT = 100


def attention_item_key(request_id: str) -> str:
    """Stable feed key: ``fy_`` + 40 hex chars of sha1("approval:<id>")."""
    return "fy_" + hashlib.sha1(f"approval:{request_id}".encode()).hexdigest()


async def attention_items(
    db: AsyncSession,
    *,
    user_id: str,
    scope: Any = None,
    allowed_project_ids: Optional[Iterable[str]] = None,
    limit: int = ATTENTION_LIMIT,
) -> list[dict]:
    """One For You item per pending approval ``user_id`` could decide now.

    ``scope`` is ``None`` for a user key, or an object with ``org_id`` and
    ``project_ids`` (an AuthContext for a service key) which narrows the
    list to that org and project allowlist. ``allowed_project_ids`` narrows
    further. The eligibility rules are ``pending_for`` / ``decidable_by``."""
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        return []
    org_id = getattr(scope, "org_id", None) if scope is not None else None
    allow: Optional[set[str]] = None
    scope_projects = getattr(scope, "project_ids", None) if scope is not None else None
    if scope_projects is not None:
        allow = set(scope_projects)
    if allowed_project_ids is not None:
        extra = set(allowed_project_ids)
        allow = extra if allow is None else allow & extra
    if allow is not None and not allow:
        return []
    mine = await pending_for(
        db, user, org_id=org_id, project_ids=allow, limit=limit
    )
    if not mine:
        return []
    policies = {
        p.id: p
        for p in (
            await db.execute(
                select(ApprovalPolicy).where(ApprovalPolicy.id.in_({r.policy_id for r in mine}))
            )
        ).scalars().all()
    }
    counts = await approval_counts(db, mine)
    items: list[dict] = []
    for r in mine:
        p = policies.get(r.policy_id)
        reqs = policy_requirements(p) if p is not None else []
        blocking = bool(p.blocking) if p is not None else True
        name = p.name if p is not None else "Approval"
        got = approved_count(reqs, counts.get(r.id, {}))
        need = required_count(reqs)
        items.append(
            {
                "item_key": attention_item_key(r.id),
                "kind": "approval",
                "band": "now",
                "involvement": "approver",
                "occurred_at": _iso(r.requested_at),
                "due_at": _iso(r.expires_at),
                "title": f"{name} ({got}/{need})",
                "why": "Waiting on your approval · "
                + ("blocks merge" if blocking else "advisory"),
                "link": f"/approvals?request={r.id}",
                "project_id": r.project_id,
                "actions": ["snooze", "hide"],
                "source": {"type": "approval_request", "id": r.id},
                # The decision panel's facts (additive; no names or emails).
                "approval": {
                    "request_id": r.id,
                    "policy_name": name,
                    "policy_version": p.version if p is not None else None,
                    "blocking": blocking,
                    "approved_count": got,
                    "required_count": need,
                    "target_kind": r.target_kind,
                    "target_id": r.target_id,
                    "target_fingerprint": r.target_fingerprint,
                    "expires_at": _iso(r.expires_at),
                },
            }
        )
    return items


async def _current_head_sha(db: AsyncSession, ticket: Ticket) -> Optional[str]:
    """The ticket's current merge head: the newest agent run that recorded
    one (implementer roles first, as the loop readiness uses)."""
    order = (
        func.coalesce(AgentRun.occurred_at, AgentRun.created_at).desc(),
        AgentRun.id.desc(),
    )
    base = select(AgentRun.head_sha).where(
        AgentRun.ticket_id == ticket.id,
        AgentRun.project_id == ticket.project_id,
        AgentRun.head_sha.is_not(None),
        AgentRun.head_sha != "",
    )
    for stmt in (
        base.where(AgentRun.role.in_(("implementer", "ui-implementer"))),
        base,
    ):
        sha = (await db.execute(stmt.order_by(*order).limit(1))).scalar_one_or_none()
        if sha:
            return sha
    return None


async def workflow_gate_passed(
    db: AsyncSession, *, policy_id: str, ticket: Ticket
) -> tuple[bool, str]:
    """True only when the policy (any version of its lineage) has an approved
    request for this ticket at its current head sha. Never creates requests."""
    policy = await db.get(ApprovalPolicy, policy_id)
    if policy is None:
        return False, POLICY_NOT_FOUND
    project = await db.get(Project, ticket.project_id)
    if project is None or not project.org_id or project.org_id != policy.org_id:
        return False, POLICY_NOT_FOUND
    sha = await _current_head_sha(db, ticket)
    if not sha:
        return False, f"Waiting on a commit to approve: {policy.name}"
    rows = (
        await db.execute(
            select(ApprovalRequest)
            .join(ApprovalPolicy, ApprovalPolicy.id == ApprovalRequest.policy_id)
            .where(
                ApprovalPolicy.lineage_id == policy.lineage_id,
                ApprovalPolicy.org_id == policy.org_id,
                ApprovalRequest.target_kind == "merge",
                ApprovalRequest.target_id == ticket.id,
                ApprovalRequest.target_fingerprint == sha,
            )
            .order_by(ApprovalRequest.requested_at.desc(), ApprovalRequest.id.desc())
        )
    ).scalars().all()
    now = _now()
    for r in rows:
        if r.state == "approved":
            return True, "Approved"
    pending = next((r for r in rows if r.state == "pending"), None)
    if pending is not None:
        expires = _aware(pending.expires_at)
        if expires is None or expires > now:
            got = approved_count(
                policy_requirements(policy),
                (await approval_counts(db, [pending])).get(pending.id, {}),
            )
            need = required_count(policy_requirements(policy))
            return False, f"Waiting on approval: {policy.name} ({got} of {need})"
    latest = rows[0] if rows else None
    if any(r.state == "rejected" for r in rows):
        return False, f"Approval rejected: {policy.name}"
    if latest is not None and latest.state in ("expired", "superseded"):
        return False, (
            f"Approval {latest.state}: {policy.name} "
            "(it is requested again once the review is clean)"
        )
    if pending is not None:  # pending but past its expiry
        return False, (
            f"Approval expired: {policy.name} (it is requested again once the review is clean)"
        )
    need = required_count(policy_requirements(policy))
    return False, f"Waiting on approval: {policy.name} (0 of {need})"

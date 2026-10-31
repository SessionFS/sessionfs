"""Agent run CRUD + lifecycle routes — v0.10.2.

AgentRun is the execution record for ephemeral agent work. Six routes
under `/api/v1/projects/{project_id}/agent-runs`:

    POST   /                          — create queued
    GET    /                          — list with filters
    GET    /{run_id}                  — detail
    POST   /{run_id}/start            — queued → running, return compiled context
    POST   /{run_id}/complete         — running/queued → passed/failed/errored
    POST   /{run_id}/cancel           — queued/running → cancelled

Status FSM (enforced via atomic `UPDATE ... WHERE status=X` rowcount
guards, same pattern as v0.10.1 tickets):

    queued → running → passed | failed | errored
    queued → cancelled
    running → cancelled

Cross-project safety: when a run references a ticket or persona, those
references must belong to the same project as the run. Lookups filter
by project_id so a guessed run_id from another project surfaces as 404,
not as cross-project data leak.

Policy evaluation runs at complete time. Severity hierarchy:
    none < low < medium < high < critical
A run fails when severity meets or exceeds `fail_on`; `severity=none`
never trips a threshold. The stored `policy_result` is `"pass"` or
`"fail"`; `exit_code` is 0 (pass) or 1 (fail). The CLI exits with
`exit_code` when `--fail-on` is set so CI builds can gate on it.

Tier-gated via `agent_runs` (Team+ — same tier as tickets since runs
are typically execution records tied to ticket work).
"""

from __future__ import annotations

import json
import math
import re
import shlex
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, PrivateAttr, field_validator, model_serializer
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    get_current_user,
    require_scope,
)
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    WorkQueue,
    WorkQueueItem,
    AgentPersona,
    AgentRun,
    OrgMember,
    Project,
    Ticket,
    User,
)
from sessionfs.server.roles import has_minimum_role
from sessionfs.server.services.personas import (
    resolve_persona,
    visible_persona_org_id,
    visible_persona_org_id_for_auth,
)
from sessionfs.server.routes.knowledge import _get_project_for_auth
from sessionfs.server.routes.tickets import _compile_persona_context
from sessionfs.server.routes.wiki import _get_project_or_404
from sessionfs.server.tier_gate import UserContext, check_project_feature, get_user_context

router = APIRouter(prefix="/api/v1/projects", tags=["agent-runs"])


# ── Enums (validated client-side; server is the source of truth) ──

_VALID_TRIGGER_SOURCES: set[str] = {
    "manual", "ci", "webhook", "scheduled", "mcp", "api",
}
_VALID_STATUSES: set[str] = {
    "queued", "running", "passed", "failed", "errored", "cancelled",
}
_TERMINAL_STATUSES: set[str] = {"passed", "failed", "errored", "cancelled"}
_VALID_SEVERITIES: tuple[str, ...] = ("none", "low", "medium", "high", "critical")
_VALID_FAIL_ON: tuple[str, ...] = ("none", "low", "medium", "high", "critical")
_SEVERITY_ORDER: dict[str, int] = {
    "none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4,
}
_VALID_CI_PROVIDERS: set[str] = {
    "github", "gitlab", "bitbucket", "circleci", "jenkins", "buildkite",
    "azure", "drone", "other",
}

# Persona names must stay CLI/MCP-friendly ASCII identifiers (same regex as
# personas.py). Anchored start-to-end so empty + over-long inputs fail.
_PERSONA_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")

# GitHub Actions expression injection marker. Replayed/prompted user values
# must never be pasted into a `run:` step, so any value carrying `${{` is
# refused outright as defense-in-depth (even though the replay command no
# longer embeds user values — it only references the server-owned run id).
_UNSAFE_CI_MARKER = "${{"

# Field limits for run/replay inputs (match the DB column widths + a sane
# prompt ceiling so oversized prompts fail fast instead of round-tripping).
_MAX_PROMPT_CHARS = 20_000
_MAX_CONTEXT_REFS = 50
_MAX_CONTEXT_REF_CHARS = 500
_MAX_TRIGGER_REF_CHARS = 200
_MAX_TICKET_ID_CHARS = 64
# Compare caps: no more than this many findings per side enter the diff,
# and no more than this many per output list (added/removed/unchanged).
_MAX_FINDINGS_PER_SIDE = 2_000
_MAX_DIFF_ITEMS_PER_LIST = 500

# Review Loop R3 track A — spend / role / loop-field vocabularies (mirror the
# migration 106 CHECK constraints; server-side validation gives a 422 naming
# the field before the DB CHECK would raise a bare IntegrityError).
_VALID_COST_BASIS: tuple[str, ...] = ("reported", "estimated", "plan", "unknown")
_VALID_ROLES: tuple[str, ...] = (
    "implementer", "ui-implementer", "reviewer",
    "design-reviewer", "security-reviewer", "release",
)
_VALID_EXIT_REASONS: tuple[str, ...] = ("finished", "out_of_turns", "crashed")
_VALID_FINDING_STAGES: tuple[str, ...] = ("design", "code")
_VALID_FINDING_HARMS: tuple[str, ...] = (
    "data-exposed", "money-spent", "wrong-actor", "crash", "other", "none",
)
_VALID_FINDING_STATUSES: tuple[str, ...] = ("open", "fixed", "accepted", "ticketed")

# Token counters are cumulative per run; cap each at 10**12 so a bad client
# can't store an absurd BigInt. Cost is capped below 1,000,000 because
# Numeric(12,6) holds at most 999,999.999999; the stored running total is
# held to the same cap (``_usage_within_caps``).
_MAX_TOKEN_COUNT = 10**12
_MAX_COST_USD = 999_999

# A finding class is a URL-safe slug ("new" reserved for an unclassified
# finding); a git SHA is 7-64 lowercase hex (short SHAs accepted, full 40s fit).
_FINDING_CLASS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


def _reject_unsafe_ci(value: str, field: str) -> str:
    if _UNSAFE_CI_MARKER in value:
        raise ValueError(
            f"unsafe_ci_value: {field} must not contain GitHub Actions "
            "expression syntax ('${{...}}')."
        )
    return value


def _validate_finding(finding: dict[str, Any]) -> None:
    """Validate the Review Loop R3 finding keys (spend design §4b).

    Only the keys this track owns are validated; every other key the caller
    sends is preserved verbatim (findings are stored as opaque JSON). A bad
    value raises ``ValueError`` naming the offending key so the route turns
    it into a 422 with a field name instead of a bare DB error.
    """
    round_ = finding.get("round")
    if round_ is not None:
        if isinstance(round_, bool) or not isinstance(round_, int):
            raise ValueError("finding 'round' must be an integer")
        if round_ < 1:
            raise ValueError("finding 'round' must be >= 1")

    stage = finding.get("stage")
    if stage is not None and stage not in _VALID_FINDING_STAGES:
        raise ValueError(
            f"finding 'stage' must be one of: {list(_VALID_FINDING_STAGES)}"
        )

    # Older clients send free-text finding severities; only Review Loop
    # findings (those carrying harm, class or round) are held to the enum.
    is_loop_finding = any(k in finding for k in ("harm", "class", "round"))
    severity = finding.get("severity")
    if is_loop_finding and severity is not None and str(severity).lower() not in _VALID_SEVERITIES:
        raise ValueError(
            f"finding 'severity' must be one of: {list(_VALID_SEVERITIES)}"
        )

    class_ = finding.get("class")
    if class_ is not None:
        if class_ == "new":
            pass
        elif not isinstance(class_, str) or not _FINDING_CLASS_RE.match(class_):
            raise ValueError(
                "finding 'class' must be a lowercase slug "
                "(letters, digits, hyphens) or 'new'"
            )

    harm = finding.get("harm")
    if harm is not None and harm not in _VALID_FINDING_HARMS:
        raise ValueError(
            f"finding 'harm' must be one of: {list(_VALID_FINDING_HARMS)}"
        )

    status = finding.get("status")
    if is_loop_finding and status is not None and status not in _VALID_FINDING_STATUSES:
        raise ValueError(
            f"finding 'status' must be one of: {list(_VALID_FINDING_STATUSES)}"
        )

    fixed_in_sha = finding.get("fixed_in_sha")
    if fixed_in_sha is not None:
        if not isinstance(fixed_in_sha, str) or not _SHA_RE.match(
            fixed_in_sha.strip().lower()
        ):
            raise ValueError(
                "finding 'fixed_in_sha' must be 7-64 lowercase hex characters"
            )


# ── Pydantic models ──


class _AgentRunUsage(BaseModel):
    """Token/cost fields shared by the usage endpoint and run completion.

    Token counts are cumulative integers (booleans and non-integer numbers
    are rejected — bool is an int subclass in Python, so a bare `int` type
    would silently accept `true`). ``cost_usd`` is a reported dollar figure.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None

    @field_validator(
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
        mode="before",
    )
    @classmethod
    def _token_int(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, bool):
            raise ValueError("token counts must be integers, not booleans")
        if not isinstance(v, int):
            raise ValueError("token counts must be integers")
        return v

    @field_validator(
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    )
    @classmethod
    def _token_range(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if v < 0 or v > _MAX_TOKEN_COUNT:
            raise ValueError(
                f"token counts must be between 0 and {_MAX_TOKEN_COUNT}"
            )
        return v

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _cost_number(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, bool):
            raise ValueError("cost_usd must be a number, not a boolean")
        if not isinstance(v, (int, float)):
            raise ValueError("cost_usd must be a number")
        return v

    @field_validator("cost_usd")
    @classmethod
    def _cost_range(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if not math.isfinite(v):
            raise ValueError("cost_usd must be a finite number")
        if v < 0 or v > _MAX_COST_USD:
            raise ValueError(f"cost_usd must be between 0 and {_MAX_COST_USD}")
        return v


class AgentRunCreate(BaseModel):
    persona_name: str
    tool: str = "generic"
    trigger_source: str = "manual"
    ticket_id: str | None = None
    trigger_ref: str | None = None
    ci_provider: str | None = None
    ci_run_url: str | None = None
    fail_on: str | None = None
    triggered_by_persona: str | None = None
    # GitHub #95 — an edited prompt and context refs for the run.
    prompt: str | None = None
    context_refs: list[str] | None = None
    # Review Loop R3 track A — model/role and loop-linkage fields.
    model: str | None = None
    provider: str | None = None
    role: str | None = None
    branch: str | None = None
    base_sha: str | None = None
    parent_run_id: str | None = None
    continues_run_id: str | None = None
    work_queue_id: str | None = None
    work_queue_item_id: str | None = None

    @field_validator("persona_name")
    @classmethod
    def _persona_shape(cls, v: str) -> str:
        v = v.strip()
        if not _PERSONA_NAME_RE.match(v):
            raise ValueError(
                "persona_name must be 1-50 ASCII letters, digits, "
                "underscores or hyphens"
            )
        return v

    @field_validator("tool")
    @classmethod
    def _tool_shape(cls, v: str) -> str:
        v = v.strip() or "generic"
        if len(v) > 50:
            raise ValueError("tool must be 50 characters or fewer")
        return v

    @field_validator("trigger_source")
    @classmethod
    def _trigger_source_shape(cls, v: str) -> str:
        v = (v or "manual").strip().lower()
        if v not in _VALID_TRIGGER_SOURCES:
            raise ValueError(
                f"trigger_source must be one of: {sorted(_VALID_TRIGGER_SOURCES)}"
            )
        return v

    @field_validator("fail_on")
    @classmethod
    def _fail_on_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in _VALID_FAIL_ON:
            raise ValueError(
                f"fail_on must be one of: {list(_VALID_FAIL_ON)}"
            )
        return v

    @field_validator("ci_provider")
    @classmethod
    def _ci_provider_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in _VALID_CI_PROVIDERS:
            raise ValueError(
                f"ci_provider must be one of: {sorted(_VALID_CI_PROVIDERS)}"
            )
        return v

    @field_validator("ticket_id")
    @classmethod
    def _ticket_id_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > _MAX_TICKET_ID_CHARS:
            raise ValueError(
                f"ticket_id must be {_MAX_TICKET_ID_CHARS} characters or fewer"
            )
        return v

    @field_validator("trigger_ref")
    @classmethod
    def _trigger_ref_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > _MAX_TRIGGER_REF_CHARS:
            raise ValueError(
                f"trigger_ref must be {_MAX_TRIGGER_REF_CHARS} characters or fewer"
            )
        return _reject_unsafe_ci(v, "trigger_ref")

    @field_validator("prompt")
    @classmethod
    def _prompt_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if len(v) > _MAX_PROMPT_CHARS:
            raise ValueError(
                f"prompt must be {_MAX_PROMPT_CHARS} characters or fewer"
            )
        return _reject_unsafe_ci(v, "prompt")

    @field_validator("context_refs")
    @classmethod
    def _context_refs_shape(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        cleaned: list[str] = []
        for ref in v:
            ref = str(ref).strip()
            if not ref:
                continue
            if len(ref) > _MAX_CONTEXT_REF_CHARS:
                raise ValueError(
                    f"each context_ref must be {_MAX_CONTEXT_REF_CHARS} "
                    "characters or fewer"
                )
            cleaned.append(_reject_unsafe_ci(ref, "context_refs"))
        if len(cleaned) > _MAX_CONTEXT_REFS:
            raise ValueError(
                f"context_refs must be {_MAX_CONTEXT_REFS} items or fewer"
            )
        return cleaned

    @field_validator("model")
    @classmethod
    def _model_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > 120:
            raise ValueError("model must be 120 characters or fewer")
        return v

    @field_validator("provider")
    @classmethod
    def _provider_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > 40:
            raise ValueError("provider must be 40 characters or fewer")
        return v

    @field_validator("role")
    @classmethod
    def _role_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not v:
            return None
        if v not in _VALID_ROLES:
            raise ValueError(f"role must be one of: {list(_VALID_ROLES)}")
        return v

    @field_validator("branch")
    @classmethod
    def _branch_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > 200:
            raise ValueError("branch must be 200 characters or fewer")
        return v

    @field_validator("base_sha")
    @classmethod
    def _base_sha_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not v:
            return None
        if not _SHA_RE.match(v):
            raise ValueError("base_sha must be 7-64 lowercase hex characters")
        return v

    @field_validator("parent_run_id", "continues_run_id", "work_queue_id", "work_queue_item_id")
    @classmethod
    def _run_id_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > 64:
            raise ValueError("run ids must be 64 characters or fewer")
        return v


class AgentRunComplete(_AgentRunUsage):
    """Final-state submission at run completion."""

    status: str = "passed"  # passed | failed | errored — never queued/running/cancelled here
    result_summary: str | None = None
    severity: str = "none"
    findings: list[dict[str, Any]] = []
    session_id: str | None = None
    # Review Loop R3 track A — completion adds usage (inherited above) plus
    # the cost basis, the head commit the run produced, why it exited and the
    # wall-clock time it actually ran (bounded so a bad clock can't write a
    # run into the far past or future).
    cost_basis: str | None = None
    head_sha: str | None = None
    exit_reason: str | None = None
    occurred_at: datetime | None = None

    @field_validator("status")
    @classmethod
    def _status_shape(cls, v: str) -> str:
        v = (v or "passed").strip().lower()
        if v not in {"passed", "failed", "errored"}:
            raise ValueError(
                "status at completion must be one of: passed, failed, errored"
            )
        return v

    @field_validator("severity")
    @classmethod
    def _severity_shape(cls, v: str) -> str:
        v = (v or "none").strip().lower()
        if v not in _VALID_SEVERITIES:
            raise ValueError(
                f"severity must be one of: {list(_VALID_SEVERITIES)}"
            )
        return v

    @field_validator("cost_basis")
    @classmethod
    def _cost_basis_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not v:
            return None
        if v not in _VALID_COST_BASIS:
            raise ValueError(
                f"cost_basis must be one of: {list(_VALID_COST_BASIS)}"
            )
        return v

    @field_validator("head_sha")
    @classmethod
    def _head_sha_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not v:
            return None
        if not _SHA_RE.match(v):
            raise ValueError("head_sha must be 7-64 lowercase hex characters")
        return v

    @field_validator("exit_reason")
    @classmethod
    def _exit_reason_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not v:
            return None
        if v not in _VALID_EXIT_REASONS:
            raise ValueError(
                f"exit_reason must be one of: {list(_VALID_EXIT_REASONS)}"
            )
        return v

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_shape(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        if v.tzinfo is None:
            # Naive datetimes are treated as UTC so a naive client isn't
            # silently shifted by the server's local zone.
            v = v.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if v > now + timedelta(minutes=5):
            raise ValueError("occurred_at must not be more than 5 minutes in the future")
        if v < now - timedelta(days=30):
            raise ValueError("occurred_at must not be more than 30 days in the past")
        return v

    @field_validator("findings")
    @classmethod
    def _findings_shape(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for finding in v:
            if not isinstance(finding, dict):
                continue
            _validate_finding(finding)
        return v


class AgentRunResponse(BaseModel):
    id: str
    project_id: str
    persona_name: str
    tool: str
    trigger_source: str
    status: str
    ticket_id: str | None
    trigger_ref: str | None
    ci_provider: str | None
    ci_run_url: str | None
    result_summary: str | None
    severity: str | None
    findings_count: int
    findings: list[dict[str, Any]]
    fail_on: str | None
    policy_result: str | None
    exit_code: int | None
    session_id: str | None
    triggered_by_user_id: str | None
    triggered_by_persona: str | None
    # GitHub #95 — replay provenance + edited prompt/context.
    replayed_from: str | None = None
    prompt: str | None = None
    context_refs: list[str] = []
    # v0.10.21 (tk_a77b671fd86a42fb) — service-key provenance triple,
    # populated at create time (v0.10.10 migration 042 columns). Reads
    # expose them additively so dashboards and the Scout v4 n8n smoke
    # test can verify which key minted/completed a run without DB
    # access. actor_type is "service_key" for service-key-minted runs
    # and "user" for legacy user-key runs.
    actor_type: str | None = None
    service_key_id: str | None = None
    service_key_name: str | None = None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    duration_seconds: int | None
    # Review Loop R3 track A — model, spend and loop-linkage fields. Token
    # fields are visible to every reader; cost_usd is omitted entirely (not
    # nulled) for callers who are not org owners/admins or the personal
    # project's owner. `_include_cost` controls that omission.
    model: str | None = None
    provider: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    cost_basis: str | None = None
    role: str | None = None
    branch: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    parent_run_id: str | None = None
    continues_run_id: str | None = None
    work_queue_id: str | None = None
    work_queue_item_id: str | None = None
    review_round: int | None = None
    exit_reason: str | None = None
    occurred_at: datetime | None = None

    _include_cost: bool = PrivateAttr(default=True)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if not self._include_cost:
            data.pop("cost_usd", None)
        return data


class StartAgentRunResponse(BaseModel):
    run: AgentRunResponse
    compiled_context: str


class AgentRunReplayCreate(BaseModel):
    """Editable fields for a replay (GitHub #95). None = copy from parent."""

    persona_name: str | None = None
    ticket_id: str | None = None
    prompt: str | None = None
    context_refs: list[str] | None = None
    fail_on: str | None = None
    trigger_ref: str | None = None

    @field_validator("persona_name")
    @classmethod
    def _persona_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not _PERSONA_NAME_RE.match(v):
            raise ValueError(
                "persona_name must be 1-50 ASCII letters, digits, "
                "underscores or hyphens"
            )
        return v

    @field_validator("fail_on")
    @classmethod
    def _fail_on_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in _VALID_FAIL_ON:
            raise ValueError(
                f"fail_on must be one of: {list(_VALID_FAIL_ON)}"
            )
        return v

    @field_validator("ticket_id")
    @classmethod
    def _ticket_id_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > _MAX_TICKET_ID_CHARS:
            raise ValueError(
                f"ticket_id must be {_MAX_TICKET_ID_CHARS} characters or fewer"
            )
        return v

    @field_validator("trigger_ref")
    @classmethod
    def _trigger_ref_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > _MAX_TRIGGER_REF_CHARS:
            raise ValueError(
                f"trigger_ref must be {_MAX_TRIGGER_REF_CHARS} characters or fewer"
            )
        return _reject_unsafe_ci(v, "trigger_ref")

    @field_validator("prompt")
    @classmethod
    def _prompt_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if len(v) > _MAX_PROMPT_CHARS:
            raise ValueError(
                f"prompt must be {_MAX_PROMPT_CHARS} characters or fewer"
            )
        return _reject_unsafe_ci(v, "prompt")

    @field_validator("context_refs")
    @classmethod
    def _context_refs_shape(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        cleaned: list[str] = []
        for ref in v:
            ref = str(ref).strip()
            if not ref:
                continue
            if len(ref) > _MAX_CONTEXT_REF_CHARS:
                raise ValueError(
                    f"each context_ref must be {_MAX_CONTEXT_REF_CHARS} "
                    "characters or fewer"
                )
            cleaned.append(_reject_unsafe_ci(ref, "context_refs"))
        if len(cleaned) > _MAX_CONTEXT_REFS:
            raise ValueError(
                f"context_refs must be {_MAX_CONTEXT_REFS} items or fewer"
            )
        return cleaned


class AgentRunReplayResponse(BaseModel):
    run: AgentRunResponse
    command: str
    ci_hint: str


class AgentRunCompareResponse(BaseModel):
    """Findings diff between two runs in the same project (GitHub #95)."""

    a_id: str
    b_id: str
    a_findings_count: int
    b_findings_count: int
    a_severity_counts: dict[str, int]
    b_severity_counts: dict[str, int]
    added: list[dict[str, Any]]
    removed: list[dict[str, Any]]
    unchanged: list[dict[str, Any]]
    # True when any of added/removed/unchanged was capped to fit the
    # response (input cap or per-list cap).
    truncated: bool = False


# ── Helpers ──


def _decode_context_refs(value: str | None) -> list[str]:
    """Decode the JSON-encoded context_refs Text column to a list[str]."""
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError):
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded]


def _decode_findings(value: str | None) -> list[dict[str, Any]]:
    try:
        findings = json.loads(value) if value else []
        if not isinstance(findings, list):
            findings = []
    except (ValueError, TypeError):
        findings = []
    return [f for f in findings if isinstance(f, dict)]


def _finding_line_key(value: Any) -> str:
    """Normalise a finding's `line` to a hashable string.

    Findings are dicts under caller control, so `line` can be an int, a
    string, or an unhashable list/dict (e.g. a SAST finding encoding the
    location as an object). Hashable scalars become `str(...)`; anything
    else is JSON-encoded with a stable key order so the same location
    always matches the same key.
    """
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, sort_keys=True, default=str)


def _finding_key(finding: dict[str, Any]) -> tuple[str, str, str]:
    """Match key for a finding: rule/title + file + line (when present)."""
    rule = finding.get("rule") or finding.get("title") or ""
    return (
        str(rule),
        str(finding.get("file") or ""),
        _finding_line_key(finding.get("line")),
    )


def _severity_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        sev = str(f.get("severity") or "none")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _diff_findings(
    a: list[dict[str, Any]], b: list[dict[str, Any]]
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    bool,
]:
    """Split `b` vs `a` into added / removed / unchanged by match key.

    Inputs are capped at `_MAX_FINDINGS_PER_SIDE` and each output list at
    `_MAX_DIFF_ITEMS_PER_LIST`; the trailing bool signals truncation so a
    pathological run can't bloat the compare response.
    """
    a = a[:_MAX_FINDINGS_PER_SIDE]
    b = b[:_MAX_FINDINGS_PER_SIDE]
    b_remaining = Counter(_finding_key(f) for f in b)
    removed: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    for f in a:
        key = _finding_key(f)
        if b_remaining.get(key, 0) > 0:
            unchanged.append(f)
            b_remaining[key] -= 1
        else:
            removed.append(f)
    added: list[dict[str, Any]] = []
    for f in b:
        key = _finding_key(f)
        if b_remaining.get(key, 0) > 0:
            added.append(f)
            b_remaining[key] -= 1
    truncated = (
        len(added) > _MAX_DIFF_ITEMS_PER_LIST
        or len(removed) > _MAX_DIFF_ITEMS_PER_LIST
        or len(unchanged) > _MAX_DIFF_ITEMS_PER_LIST
    )
    return (
        added[:_MAX_DIFF_ITEMS_PER_LIST],
        removed[:_MAX_DIFF_ITEMS_PER_LIST],
        unchanged[:_MAX_DIFF_ITEMS_PER_LIST],
        truncated,
    )


def _build_replay_command(run_id: str) -> str:
    """The `sfs agent run` invocation that resumes the queued replay.

    The new run's fields live on the server row, so the command only
    references the run id — no user-controlled prompt/ticket/ref values
    are pasted into a shell or CI `run:` step. `--run-id` tells the CLI
    to start the existing queued run instead of creating a fresh one.
    """
    return f"sfs agent run --run-id {shlex.quote(run_id)}"


def _build_ci_hint(command: str) -> str:
    """A YAML-safe CI hint block.

    The `run:` value is the server-owned run id only, so there is no
    user-controlled text to inject. The block is a real YAML mapping
    (not commented-out lines), so tooling can `yaml.safe_load` it and
    read `github_actions[0].run` / `gitlab_ci.replay.script[0]` directly.
    """
    return (
        "# Replay this agent run in CI.\n"
        "github_actions:\n"
        "  - name: Replay agent run\n"
        "    run: " + command + "\n"
        "gitlab_ci:\n"
        "  replay:\n"
        "    script:\n"
        "      - " + command + "\n"
    )


async def _cost_visible(
    db: AsyncSession,
    project: Project,
    *,
    auth: AuthContext | None = None,
    user: User | None = None,
) -> bool:
    """Dollar visibility (spend design §9 / review-loop-r3 §8).

    Only org owners/admins and personal-project owners see ``cost_usd``.
    Service keys never see dollars — they hold project data but not budget
    authority. Everyone else with read access sees token totals only.
    """
    if auth is not None and auth.key_kind == "service":
        return False
    if user is None:
        user = auth.user if auth is not None else None
    if user is None:
        return False
    if project.org_id is None:
        return project.owner_id == user.id
    role = (
        await db.execute(
            select(OrgMember.role).where(
                OrgMember.org_id == project.org_id,
                OrgMember.user_id == user.id,
            )
        )
    ).scalars().first()
    return role is not None and has_minimum_role(role, "admin")


def _row_to_response(
    row: AgentRun, *, include_cost: bool = True
) -> AgentRunResponse:
    findings = _decode_findings(row.findings)
    response = AgentRunResponse(
        id=row.id,
        project_id=row.project_id,
        persona_name=row.persona_name,
        tool=row.tool,
        trigger_source=row.trigger_source,
        status=row.status,
        ticket_id=row.ticket_id,
        trigger_ref=row.trigger_ref,
        ci_provider=row.ci_provider,
        ci_run_url=row.ci_run_url,
        result_summary=row.result_summary,
        severity=row.severity,
        findings_count=row.findings_count,
        findings=findings,
        fail_on=row.fail_on,
        policy_result=row.policy_result,
        exit_code=row.exit_code,
        session_id=row.session_id,
        triggered_by_user_id=row.triggered_by_user_id,
        triggered_by_persona=row.triggered_by_persona,
        actor_type=row.actor_type,
        service_key_id=row.service_key_id,
        service_key_name=row.service_key_name,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        duration_seconds=row.duration_seconds,
        replayed_from=row.replayed_from,
        prompt=row.prompt,
        context_refs=_decode_context_refs(row.context_refs),
        model=row.model,
        provider=row.provider,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cache_read_tokens=row.cache_read_tokens,
        cache_write_tokens=row.cache_write_tokens,
        cost_usd=float(row.cost_usd) if row.cost_usd is not None else None,
        cost_basis=row.cost_basis,
        role=row.role,
        branch=row.branch,
        base_sha=row.base_sha,
        head_sha=row.head_sha,
        parent_run_id=row.parent_run_id,
        continues_run_id=row.continues_run_id,
        work_queue_id=row.work_queue_id,
        work_queue_item_id=row.work_queue_item_id,
        review_round=row.review_round,
        exit_reason=row.exit_reason,
        occurred_at=row.occurred_at,
    )
    response._include_cost = include_cost
    return response


async def _get_run_or_404(
    project_id: str, run_id: str, db: AsyncSession
) -> AgentRun:
    """Project-scoped fetch. Cross-project run_id surfaces as 404."""
    row = (
        await db.execute(
            select(AgentRun).where(
                AgentRun.id == run_id,
                AgentRun.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Agent run not found")
    return row


async def _require_run_actor(
    db: AsyncSession,
    project: Project,
    run: AgentRun,
    user_id: str,
    *,
    key_kind: str = "user",
) -> None:
    """Gate run transitions on top of project access.

    Recording a run is an append-only audit action open to anyone who can
    read the project, but a reader whose only access is a captured session
    may move only the runs they triggered; other runs need editor standing
    (project owner or org member) — 403 ``project_edit_forbidden``. Service
    keys keep their own org / allowlist boundary.
    """
    if key_kind == "service" or run.triggered_by_user_id == user_id:
        return
    from sessionfs.server.auth.project_access import require_project_editor

    await require_project_editor(db, user_id, project)


async def _validate_same_project_ticket(
    project_id: str, ticket_id: str, db: AsyncSession
) -> Ticket:
    """Ticket must exist and belong to this project (no cross-project leak)."""
    ticket = (
        await db.execute(
            select(Ticket).where(
                Ticket.id == ticket_id,
                Ticket.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(
            422,
            f"Ticket {ticket_id!r} not found in this project (cross-project "
            "ticket references are rejected).",
        )
    return ticket


async def _validate_active_persona(
    project_id: str, persona_name: str, db: AsyncSession, *, org_id: str | None
) -> AgentPersona:
    """Persona must exist and be active in this project or, when the caller
    may see it (`org_id` from visible_persona_org_id), the project's org
    (GH #70 — project persona first, then org)."""
    persona = await resolve_persona(
        db, project_id, persona_name, org_id=org_id, active_only=True
    )
    if persona is None:
        raise HTTPException(
            422,
            f"Persona {persona_name!r} not found or inactive in this project.",
        )
    return persona


def _evaluate_policy(
    severity: str, fail_on: str | None
) -> tuple[str, int]:
    """Return (policy_result, exit_code) for a completed run.

    Logic:
    - If `fail_on` is None or "none": always pass (exit 0).
    - If `severity` is "none": always pass regardless of fail_on threshold
      (the run found nothing actionable; no threshold can trip).
    - Otherwise fail iff `_SEVERITY_ORDER[severity] >= _SEVERITY_ORDER[fail_on]`.

    Returns ("pass", 0) or ("fail", 1).
    """
    if not fail_on or fail_on == "none":
        return ("pass", 0)
    if severity == "none":
        return ("pass", 0)
    sev_order = _SEVERITY_ORDER.get(severity, 0)
    threshold = _SEVERITY_ORDER.get(fail_on, 0)
    if sev_order >= threshold:
        return ("fail", 1)
    return ("pass", 0)


def _new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:16]}"


def _additive_usage(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
    cost_usd: float | None,
    cost_basis: str | None = None,
) -> dict[str, Any]:
    """UPDATE values that add reported usage to a run's stored totals.

    The addition happens in SQL (``coalesce(col, 0) + delta``) so two
    concurrent reports can't overwrite each other. A field reported as None
    is left out and keeps its stored value. ``cost_usd`` is added as a
    ``Decimal`` so Numeric(12,6) never accumulates float rounding error. A
    cost sent without a basis is recorded as ``reported``.
    """
    values: dict[str, Any] = {}
    for name, delta in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("cache_read_tokens", cache_read_tokens),
        ("cache_write_tokens", cache_write_tokens),
    ):
        if delta is not None:
            column = getattr(AgentRun, name)
            values[name] = func.coalesce(column, 0) + delta
    if cost_usd is not None:
        values["cost_usd"] = func.coalesce(AgentRun.cost_usd, 0) + Decimal(str(cost_usd))
    if cost_basis is not None:
        values["cost_basis"] = cost_basis
    elif cost_usd is not None:
        values["cost_basis"] = func.coalesce(AgentRun.cost_basis, "reported")
    return values


def _usage_within_caps(*, cost_usd: float | None) -> list[Any]:
    """Extra UPDATE conditions keeping the stored running cost total within
    ``_MAX_COST_USD``: a report that would push it past the column's range
    matches no row (and is answered with 422) instead of failing on
    PostgreSQL with a numeric overflow."""
    if cost_usd is None:
        return []
    return [
        func.coalesce(AgentRun.cost_usd, 0) + Decimal(str(cost_usd))
        <= _MAX_COST_USD
    ]


async def _raise_usage_conflict(
    db: AsyncSession, project_id: str, run_id: str, *, what: str
) -> None:
    """After a guarded usage UPDATE matched no row: 422 when the run is still
    open (so only the cost cap can have refused it), else 409."""
    await db.rollback()
    current = await _get_run_or_404(project_id, run_id, db)
    if current.status in ("queued", "running"):
        raise HTTPException(
            422, f"cost_usd total for a run must stay at or below {_MAX_COST_USD}"
        )
    raise HTTPException(409, f"{what}: current status is {current.status!r}.")


# ── Routes ──


@router.post(
    "/{project_id}/agent-runs",
    response_model=AgentRunResponse,
    status_code=201,
)
async def create_agent_run(
    project_id: str,
    body: AgentRunCreate,
    auth: AuthContext = Depends(require_scope("agent_runs:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> AgentRunResponse:
    """Create a queued AgentRun. Validates same-project ticket + active persona.

    v0.10.10 — accepts service keys with `agent_runs:write` scope. The
    row's actor_type=='service_key' when a service key triggered the
    run, separate from triggered_by_user_id (the human who minted the key).
    """
    user = auth.user
    project = await _get_project_or_404(project_id, db, user.id)
    # Codex R5 HIGH 1 — service-key org/project boundary BEFORE side
    # effects. A key minted for org_A cannot create runs in org_B even
    # if the backing user happens to have access to both.
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_runs", project, db)
    # GH #70 — serialize with merge / transfer / persona-delete guards.
    from sessionfs.server.services.scope_locks import (
        lock_project_scope_authorized,
    )

    # GH #70 — lock, then re-authorize against the re-read project.
    await lock_project_scope_authorized(db, project, auth=auth, ctx=ctx, feature="agent_runs")

    # Validate persona exists + is active in this project.
    persona = await _validate_active_persona(
        project_id,
        body.persona_name,
        db,
        org_id=await visible_persona_org_id_for_auth(db, project, auth),
    )

    # Validate ticket (if provided) is in this project.
    if body.ticket_id:
        await _validate_same_project_ticket(project_id, body.ticket_id, db)
    # Review Loop R3 — loop-linkage ids must name a run in this project
    # (uniform 404, never a cross-project leak).
    if body.parent_run_id:
        await _get_run_or_404(project_id, body.parent_run_id, db)
    if body.continues_run_id:
        await _get_run_or_404(project_id, body.continues_run_id, db)
    # A run started from a queue directive is linked to that queue (and
    # item) so queue spend and budgets see it; both must be in this project.
    if body.work_queue_item_id and not body.work_queue_id:
        raise HTTPException(422, "work_queue_item_id needs work_queue_id")
    if body.work_queue_id:
        queue = await db.get(WorkQueue, body.work_queue_id)
        if queue is None or queue.project_id != project_id:
            raise HTTPException(404, "Work queue not found")
        if body.work_queue_item_id:
            item = await db.get(WorkQueueItem, body.work_queue_item_id)
            if item is None or item.work_queue_id != queue.id:
                raise HTTPException(404, "Work queue item not found")

    now = datetime.now(timezone.utc)
    row = AgentRun(
        id=_new_run_id(),
        project_id=project_id,
        # GH #70 — canonical stored spelling, so the run re-resolves to
        # the same persona at start time.
        persona_name=persona.name,
        tool=body.tool,
        trigger_source=body.trigger_source,
        status="queued",
        ticket_id=body.ticket_id,
        trigger_ref=body.trigger_ref,
        ci_provider=body.ci_provider,
        ci_run_url=body.ci_run_url,
        fail_on=body.fail_on,
        prompt=body.prompt,
        context_refs=json.dumps(body.context_refs) if body.context_refs else None,
        triggered_by_user_id=user.id,
        triggered_by_persona=body.triggered_by_persona,
        # v0.10.10 — service-key provenance (Codex R1 HIGH 2).
        actor_type=auth.actor_type,
        service_key_id=auth.service_key_id,
        service_key_name=auth.service_key_name,
        created_at=now,
        model=body.model,
        provider=body.provider,
        role=body.role,
        branch=body.branch,
        base_sha=body.base_sha,
        parent_run_id=body.parent_run_id,
        continues_run_id=body.continues_run_id,
        work_queue_id=body.work_queue_id,
        work_queue_item_id=body.work_queue_item_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    include_cost = await _cost_visible(db, project, auth=auth, user=user)
    return _row_to_response(row, include_cost=include_cost)


@router.get(
    "/{project_id}/agent-runs",
    response_model=list[AgentRunResponse],
)
async def list_agent_runs(
    project_id: str,
    persona_name: str | None = Query(None),
    status: str | None = Query(None),
    trigger_source: str | None = Query(None),
    ticket_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    auth: AuthContext = Depends(require_scope("agent_runs:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[AgentRunResponse]:
    """v0.10.21 (tk_31b87575d5534d00) — opted into agent_runs:read for
    service keys. Lets continuous agents (n8n Scout, future
    Sentinel-watch / Ledger-monitor, dashboard surfaces) inspect run
    state without using a human user key.
    """
    project = await _get_project_for_auth(project_id, db, auth)
    # Cross-org / cross-project boundary BEFORE returning any rows.
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_runs", project, db)

    stmt = select(AgentRun).where(AgentRun.project_id == project_id)
    if persona_name:
        stmt = stmt.where(AgentRun.persona_name == persona_name)
    if status:
        stmt = stmt.where(AgentRun.status == status)
    if trigger_source:
        stmt = stmt.where(AgentRun.trigger_source == trigger_source)
    if ticket_id:
        stmt = stmt.where(AgentRun.ticket_id == ticket_id)
    stmt = stmt.order_by(AgentRun.created_at.desc()).limit(limit)

    rows = (await db.execute(stmt)).scalars().all()
    include_cost = await _cost_visible(db, project, auth=auth)
    return [_row_to_response(r, include_cost=include_cost) for r in rows]


@router.get(
    "/{project_id}/agent-runs/{run_id}",
    response_model=AgentRunResponse,
)
async def get_agent_run(
    project_id: str,
    run_id: str,
    auth: AuthContext = Depends(require_scope("agent_runs:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> AgentRunResponse:
    """v0.10.21 (tk_31b87575d5534d00) — opted into agent_runs:read."""
    project = await _get_project_for_auth(project_id, db, auth)
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_runs", project, db)
    include_cost = await _cost_visible(db, project, auth=auth)
    return _row_to_response(
        await _get_run_or_404(project_id, run_id, db), include_cost=include_cost
    )


@router.post(
    "/{project_id}/agent-runs/{run_id}/start",
    response_model=StartAgentRunResponse,
)
async def start_agent_run(
    project_id: str,
    run_id: str,
    tool: str | None = Query(
        None,
        description=(
            "Token-budget hint for compiled_context. Same vocabulary as "
            "ticket start: claude-code/bedrock (16k), codex/gemini/vertex/"
            "copilot/amp/antigravity/generic (8k), "
            "cursor/windsurf/cline/roo-code/kilo-code (4k)."
        ),
    ),
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> StartAgentRunResponse:
    """Atomic queued → running transition. Returns compiled persona +
    ticket context the caller should feed into its own model runtime.

    This does NOT mutate the ticket's FSM state (the run's lifecycle is
    separate from the ticket's). If the caller wants the ticket moved to
    in_progress, they call `start_ticket` separately — keeping the
    operations explicit avoids hiding 409 conflicts behind a single
    run-start call.
    """
    project = await _get_project_or_404(project_id, db, user.id)
    await check_project_feature(ctx, "agent_runs", project, db)

    # GH #70 — resolve the persona and compile the context BEFORE the
    # queued → running transition: a persona that no longer resolves
    # (e.g. an org persona stranded by a transfer) must fail the start
    # with the run still 'queued', not leave it stuck in 'running'.
    # GH #70 — project scope lock before reading the project's org, the
    # run and the persona; held through the queued → running transition.
    from sessionfs.server.services.scope_locks import (
        lock_project_scope_authorized,
    )

    # GH #70 — lock, then re-authorize against the re-read project.
    await lock_project_scope_authorized(db, project, user_id=user.id, ctx=ctx, feature="agent_runs")
    pre = await _get_run_or_404(project_id, run_id, db)
    await _require_run_actor(db, project, pre, user.id)
    if pre.status != "queued":
        raise HTTPException(
            409,
            f"Cannot start agent run: current status is {pre.status!r}, "
            "expected 'queued'.",
        )
    persona = await _validate_active_persona(
        project_id,
        pre.persona_name,
        db,
        org_id=await visible_persona_org_id(db, project, user_id=user.id),
    )
    ticket = None
    if pre.ticket_id:
        # Best-effort — if the ticket was deleted since run creation,
        # we don't fail the start; the persona context alone is still
        # useful. The same-project guard at creation time + the FK
        # CASCADE on tickets means the ticket either exists in this
        # project or it doesn't exist at all.
        ticket = (
            await db.execute(
                select(Ticket).where(
                    Ticket.id == pre.ticket_id,
                    Ticket.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
    compiled = await _compile_persona_context(
        db=db, persona=persona, ticket=ticket, tool=tool or pre.tool,
    )

    now = datetime.now(timezone.utc)
    # Atomic queued → running with rowcount-1 guard.
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.project_id == project_id,
            AgentRun.status == "queued",
        )
        .values(status="running", started_at=now)
    )
    if result.rowcount != 1:
        # Either the run doesn't exist in this project, or it wasn't queued.
        await db.rollback()
        existing = await _get_run_or_404(project_id, run_id, db)
        raise HTTPException(
            409,
            f"Cannot start agent run: current status is {existing.status!r}, "
            "expected 'queued'.",
        )
    # Build the response from the run we already hold, re-read INSIDE the
    # locked transaction (GH #70): after commit a concurrent merge may move
    # the run to another project, and a re-query filtered by this project
    # would then 404 a start that succeeded.
    await db.refresh(pre)
    include_cost = await _cost_visible(db, project, user=user)
    response = StartAgentRunResponse(
        run=_row_to_response(pre, include_cost=include_cost),
        compiled_context=compiled,
    )
    # Commit the transition (and release the scope lock). A concurrent
    # start waited on the scope lock and now sees status='running', so its
    # rowcount guard trips.
    await db.commit()
    return response


@router.post(
    "/{project_id}/agent-runs/{run_id}/complete",
    response_model=AgentRunResponse,
)
async def complete_agent_run(
    project_id: str,
    run_id: str,
    body: AgentRunComplete,
    auth: AuthContext = Depends(require_scope("agent_runs:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> AgentRunResponse:
    """Atomic running/queued → passed/failed/errored. Runs policy evaluation.

    v0.10.10 — accepts service keys with `agent_runs:write` scope.
    Codex R5 HIGH 1 — also enforces service-key org/project boundary.
    """
    user = auth.user
    project = await _get_project_or_404(project_id, db, user.id)
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_runs", project, db)

    now = datetime.now(timezone.utc)
    # Read the row first so we can compute duration_seconds and evaluate policy.
    existing = await _get_run_or_404(project_id, run_id, db)
    await _require_run_actor(db, project, existing, user.id, key_kind=auth.key_kind)
    if existing.status in _TERMINAL_STATUSES:
        raise HTTPException(
            409,
            f"Cannot complete agent run: current status is "
            f"{existing.status!r} (already terminal).",
        )

    findings_str = json.dumps(body.findings)
    findings_count = len(body.findings)
    duration = None
    if existing.started_at is not None:
        # SQLite returns naive datetimes from DateTime(timezone=True)
        # columns; normalize to UTC-aware so the subtraction works on
        # both backends. (PostgreSQL hands back tz-aware automatically.)
        started = existing.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        duration = int((now - started).total_seconds())

    policy_result, exit_code = _evaluate_policy(body.severity, existing.fail_on)
    # If the caller explicitly says the run failed (status='failed' or
    # 'errored'), respect that — the policy only adds enforcement on top
    # of the caller's status, it doesn't override 'failed' to 'pass'.
    final_status = body.status
    if final_status == "passed" and policy_result == "fail":
        final_status = "failed"

    # KB review HIGH (post-Round 1): an explicit `failed` or `errored`
    # status from the caller MUST force exit_code=1 + policy_result='fail'
    # for CI enforcement. Without this, a crashed review tool that
    # submits status='errored' + severity='none' would store exit_code=0
    # and `sfs agent complete --enforce` would let CI pass despite the
    # terminal run being errored. Severity/fail_on policy is layered on
    # top of caller-submitted status, not the other way around.
    if final_status in {"failed", "errored"}:
        policy_result = "fail"
        exit_code = 1

    # Review Loop R3 — completion reports usage additively on top of any
    # usage reported earlier via POST …/usage, then fixes it (no further
    # usage reports after a terminal state).
    usage = _additive_usage(
        input_tokens=body.input_tokens,
        output_tokens=body.output_tokens,
        cache_read_tokens=body.cache_read_tokens,
        cache_write_tokens=body.cache_write_tokens,
        cost_usd=body.cost_usd,
        cost_basis=body.cost_basis,
    )
    # Optional completion fields only overwrite what is already stored when
    # the caller actually sent them.
    for name in ("head_sha", "exit_reason", "occurred_at"):
        value = getattr(body, name)
        if value is not None:
            usage[name] = value

    # Atomic write — must not be in a terminal state, must currently be
    # queued or running. The pre-check above already raised if terminal,
    # but the atomic UPDATE is the actual guard against concurrent
    # completion races (KB 326 pattern).
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.project_id == project_id,
            AgentRun.status.in_(("queued", "running")),
            *_usage_within_caps(cost_usd=body.cost_usd),
        )
        .values(
            status=final_status,
            completed_at=now,
            duration_seconds=duration,
            result_summary=body.result_summary,
            severity=body.severity,
            findings=findings_str,
            findings_count=findings_count,
            session_id=body.session_id,
            policy_result=policy_result,
            exit_code=exit_code,
            **usage,
        )
    )
    if result.rowcount != 1:
        await _raise_usage_conflict(
            db, project_id, run_id, what="Concurrent completion detected"
        )

    await db.commit()
    # Review Loop R3 track E1 — findings with class="new" propose checklist
    # items; classes matching an existing item's slug bump its times_found.
    # Best-effort and strictly after the run is durable: a proposal failure
    # must never fail (or roll back) the completion itself.
    try:
        from sessionfs.server.routes.loop_checklist import propose_from_findings

        await propose_from_findings(
            db,
            project_id,
            run_id,
            body.findings,
            # A service-key completion leaves the creator user id NULL and
            # records the key's provenance on the actor columns instead
            # (review checklist #3); a user completion keeps the run's
            # triggerer as the proposer.
            None
            if auth.key_kind == "service"
            else (existing.triggered_by_user_id or user.id),
            created_actor_type=auth.actor_type,
            created_service_key_id=auth.service_key_id,
            created_service_key_name=auth.service_key_name,
        )
    except Exception:
        await db.rollback()
    include_cost = await _cost_visible(db, project, auth=auth, user=user)
    return _row_to_response(
        await _get_run_or_404(project_id, run_id, db), include_cost=include_cost
    )


@router.post(
    "/{project_id}/agent-runs/{run_id}/usage",
    response_model=AgentRunResponse,
)
async def report_agent_run_usage(
    project_id: str,
    run_id: str,
    body: _AgentRunUsage,
    auth: AuthContext = Depends(require_scope("agent_runs:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> AgentRunResponse:
    """Add token/cost usage to a queued or running run (additive).

    Same auth as complete. Usage is additive while the run is active (long
    runs and continuations report more than once); once the run reaches a
    terminal state the totals are fixed and any further report is a 409.
    """
    if all(
        getattr(body, f) is None
        for f in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd")
    ):
        raise HTTPException(422, "Report at least one usage field")
    user = auth.user
    project = await _get_project_or_404(project_id, db, user.id)
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_runs", project, db)

    existing = await _get_run_or_404(project_id, run_id, db)
    await _require_run_actor(db, project, existing, user.id, key_kind=auth.key_kind)
    if existing.status in _TERMINAL_STATUSES:
        raise HTTPException(
            409,
            f"Cannot report usage: current status is {existing.status!r} "
            "(already terminal).",
        )

    usage = _additive_usage(
        input_tokens=body.input_tokens,
        output_tokens=body.output_tokens,
        cache_read_tokens=body.cache_read_tokens,
        cache_write_tokens=body.cache_write_tokens,
        cost_usd=body.cost_usd,
    )

    # Atomic write — the run must still be queued/running; the rowcount-1
    # guard also closes the race with a concurrent complete.
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.project_id == project_id,
            AgentRun.status.in_(("queued", "running")),
            *_usage_within_caps(cost_usd=body.cost_usd),
        )
        .values(**usage)
    )
    if result.rowcount != 1:
        await _raise_usage_conflict(
            db, project_id, run_id, what="Cannot report usage"
        )

    await db.commit()
    include_cost = await _cost_visible(db, project, auth=auth, user=user)
    return _row_to_response(
        await _get_run_or_404(project_id, run_id, db), include_cost=include_cost
    )


@router.post(
    "/{project_id}/agent-runs/{run_id}/cancel",
    response_model=AgentRunResponse,
)
async def cancel_agent_run(
    project_id: str,
    run_id: str,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> AgentRunResponse:
    """Atomic queued/running → cancelled."""
    project = await _get_project_or_404(project_id, db, user.id)
    await check_project_feature(ctx, "agent_runs", project, db)
    target = (
        await db.execute(
            select(AgentRun).where(
                AgentRun.id == run_id, AgentRun.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if target is not None:
        await _require_run_actor(db, project, target, user.id)

    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.project_id == project_id,
            AgentRun.status.in_(("queued", "running")),
        )
        .values(
            status="cancelled",
            completed_at=now,
            policy_result="pass",
            exit_code=0,
        )
    )
    if result.rowcount != 1:
        await db.rollback()
        existing = await _get_run_or_404(project_id, run_id, db)
        raise HTTPException(
            409,
            f"Cannot cancel agent run: current status is {existing.status!r} "
            "(already terminal).",
        )
    await db.commit()
    include_cost = await _cost_visible(db, project, user=user)
    return _row_to_response(
        await _get_run_or_404(project_id, run_id, db), include_cost=include_cost
    )


@router.post(
    "/{project_id}/agent-runs/{run_id}/replay",
    response_model=AgentRunReplayResponse,
    status_code=201,
)
async def replay_agent_run(
    project_id: str,
    run_id: str,
    body: AgentRunReplayCreate,
    auth: AuthContext = Depends(require_scope("agent_runs:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> AgentRunReplayResponse:
    """Create a queued replay of an existing run (GitHub #95).

    Unspecified fields copy from the parent; the body overrides persona,
    ticket, prompt, context_refs, fail_on and trigger_ref. Same auth,
    tier and same-project ticket validation as creating a run. Returns
    the new run plus an `sfs agent run --run-id <id>` command and a
    YAML CI hint block (GitHub Actions / GitLab).

    A replay is a fresh, manual re-invocation: it does NOT copy the
    parent's CI provenance (`ci_provider`, `ci_run_url`), trigger
    source, or triggering persona. The new run records `trigger_source`
    as `"manual"` and its `triggered_by_user_id` as the replaying actor.
    An explicit empty prompt (`""`) or empty `context_refs` (`[]`)
    clears the inherited value.
    """
    user = auth.user
    project = await _get_project_or_404(project_id, db, user.id)
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_runs", project, db)
    from sessionfs.server.services.scope_locks import (
        lock_project_scope_authorized,
    )
    await lock_project_scope_authorized(db, project, auth=auth, ctx=ctx, feature="agent_runs")

    parent = await _get_run_or_404(project_id, run_id, db)

    # Body wins when set; otherwise inherit the parent's value. An empty
    # prompt / empty context_refs clears rather than inherits.
    persona_name = body.persona_name if body.persona_name is not None else parent.persona_name
    ticket_id = body.ticket_id if body.ticket_id is not None else parent.ticket_id
    prompt = body.prompt if body.prompt is not None else parent.prompt
    if prompt == "":
        prompt = None
    context_refs = (
        list(body.context_refs)
        if body.context_refs is not None
        else _decode_context_refs(parent.context_refs)
    )
    fail_on = body.fail_on if body.fail_on is not None else parent.fail_on
    trigger_ref = body.trigger_ref if body.trigger_ref is not None else parent.trigger_ref

    # Validate the (possibly overridden) persona + ticket like create does.
    persona = await _validate_active_persona(
        project_id,
        persona_name,
        db,
        org_id=await visible_persona_org_id_for_auth(db, project, auth),
    )
    if ticket_id:
        await _validate_same_project_ticket(project_id, ticket_id, db)

    now = datetime.now(timezone.utc)
    row = AgentRun(
        id=_new_run_id(),
        project_id=project_id,
        persona_name=persona.name,
        tool=parent.tool,
        trigger_source="manual",
        status="queued",
        ticket_id=ticket_id,
        trigger_ref=trigger_ref,
        ci_provider=None,
        ci_run_url=None,
        fail_on=fail_on,
        prompt=prompt,
        context_refs=json.dumps(context_refs) if context_refs else None,
        replayed_from=parent.id,
        triggered_by_user_id=user.id,
        triggered_by_persona=None,
        actor_type=auth.actor_type,
        service_key_id=auth.service_key_id,
        service_key_name=auth.service_key_name,
        created_at=now,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    run_response = _row_to_response(row, include_cost=await _cost_visible(db, project, auth=auth, user=user))
    command = _build_replay_command(run_response.id)
    return AgentRunReplayResponse(
        run=run_response,
        command=command,
        ci_hint=_build_ci_hint(command),
    )


@router.get(
    "/{project_id}/agent-runs/{run_id}/compare/{other_id}",
    response_model=AgentRunCompareResponse,
)
async def compare_agent_runs(
    project_id: str,
    run_id: str,
    other_id: str,
    auth: AuthContext = Depends(require_scope("agent_runs:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> AgentRunCompareResponse:
    """Diff the findings of two runs in this project (GitHub #95).

    Findings are matched by rule/title + file + line (when present) and
    split into added (only in `other`), removed (only in `run`) and
    unchanged, with severity counts per side. Both runs must belong to
    the project — a cross-project other_id surfaces as 404.
    """
    project = await _get_project_for_auth(project_id, db, auth)
    from sessionfs.server.auth.dependencies import (
        assert_service_key_can_access_project,
    )
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "agent_runs", project, db)

    a = await _get_run_or_404(project_id, run_id, db)
    b = await _get_run_or_404(project_id, other_id, db)

    a_findings = _decode_findings(a.findings)
    b_findings = _decode_findings(b.findings)
    added, removed, unchanged, truncated = _diff_findings(a_findings, b_findings)

    return AgentRunCompareResponse(
        a_id=a.id,
        b_id=b.id,
        a_findings_count=len(a_findings),
        b_findings_count=len(b_findings),
        a_severity_counts=_severity_counts(a_findings),
        b_severity_counts=_severity_counts(b_findings),
        added=added,
        removed=removed,
        unchanged=unchanged,
        truncated=truncated,
    )

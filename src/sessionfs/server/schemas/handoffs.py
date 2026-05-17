"""Handoff request/response schemas.

v0.10.9 — comprehensive redesign per parent ticket tk_89e90060e6314311
with Codex R1 scope-review corrections applied. Exactly-one-recipient
invariant (email XOR user_id XOR team_id) validated on create; new
fields for provenance carry-through, revoke metadata, attachments, and
the audit-event surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


ATTACHMENT_KINDS = {"kb_entry", "wiki_page", "ticket"}


class HandoffAttachmentInput(BaseModel):
    kind: Literal["kb_entry", "wiki_page", "ticket"]
    ref_id: str = Field(..., max_length=255)

    @field_validator("ref_id")
    @classmethod
    def strip_ref_id(cls, v: str) -> str:
        """Codex R2 LOW #4 — normalize whitespace; reject blank refs."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("ref_id cannot be blank")
        return stripped


class HandoffAttachmentResponse(BaseModel):
    """v0.10.9 — attachment as returned in handoff detail.

    R3 MEDIUM 4 — `project_id` is included so a recipient with access to
    multiple projects can disambiguate a `wiki_page:auth-flow` ref:
    slugs are project-local, and without the explicit project_id the
    client would have to guess which project the page belongs to."""

    kind: str
    ref_id: str
    project_id: str | None = None
    created_at: datetime


class HandoffCommentResponse(BaseModel):
    id: str
    handoff_id: str
    author_user_id: str | None
    content: str
    created_at: datetime


class HandoffEventResponse(BaseModel):
    id: int
    handoff_id: str
    event_type: str
    actor_user_id: str | None
    payload: dict | None
    created_at: datetime


class CreateHandoffRequest(BaseModel):
    session_id: str
    # v0.10.9 — exactly one of these three must be set. Server validates
    # via @model_validator below; surface a 422 if zero or 2+ are set.
    recipient_email: str | None = Field(None, max_length=255)
    recipient_user_id: str | None = Field(None, max_length=36)
    recipient_team_id: str | None = Field(None, max_length=64)
    message: str | None = Field(None, max_length=2000)
    # v0.10.9 — provenance carry-through. Both optional; recipient claim
    # validates each against the recipient's accessible projects before
    # populating the active-ticket bundle.
    ticket_id: str | None = Field(None, max_length=64)
    persona_name: str | None = Field(None, max_length=50)
    # v0.10.9 — configurable expiry. Clamped server-side to [1, 720]
    # (1 hour to 30 days). Enterprise tier can go up to 90 days via
    # tier check. Default 168 = 7d (matches prior HANDOFF_EXPIRY_DAYS).
    expires_in_hours: int | None = Field(None, ge=1, le=2160)
    # v0.10.9 — sender curates additional context for the recipient.
    # KB entries, wiki pages, tickets. v0.10.9 scope explicitly does
    # not include file attachments.
    attachments: list[HandoffAttachmentInput] = Field(default_factory=list, max_length=50)

    @field_validator("recipient_email")
    @classmethod
    def normalize_email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        normalized = v.strip().lower()
        return normalized or None

    @field_validator(
        "recipient_user_id",
        "recipient_team_id",
        "ticket_id",
        "persona_name",
    )
    @classmethod
    def strip_blank(cls, v: str | None) -> str | None:
        """Codex R2 LOW #4 — whitespace-only IDs were passing the
        exactly-one-recipient invariant and failing later as opaque
        not-found errors. Strip + collapse blanks to None so the model
        validator sees actual content."""
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @model_validator(mode="after")
    def exactly_one_recipient(self):
        """Server enforces email XOR user_id XOR team_id (Codex A.1)."""
        provided = sum(
            1
            for field in (self.recipient_email, self.recipient_user_id, self.recipient_team_id)
            if field
        )
        if provided != 1:
            raise ValueError(
                "Exactly one of recipient_email, recipient_user_id, recipient_team_id "
                f"must be set (got {provided})"
            )
        return self


class RevokeHandoffRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class DeclineHandoffRequest(BaseModel):
    reason: str | None = Field(None, max_length=500)


class HandoffCommentCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=10_000)


class DroppedAttachment(BaseModel):
    """Returned on claim when an attachment fails recipient-project scope
    validation (Codex I.7). Audit event still records the drop."""

    kind: str
    ref_id: str
    reason: str


class ActiveTicketPayload(BaseModel):
    """v0.10.9 — claim response carries this so direct API/cloud agents
    get the same active-ticket context CLI/MCP get when they write
    ~/.sessionfs/active_ticket.json (Codex I.2)."""

    ticket_id: str | None = None
    persona_name: str | None = None
    project_id: str | None = None
    lease_epoch: int | None = None


class HandoffResponse(BaseModel):
    id: str
    session_id: str
    recipient_session_id: str | None = None
    sender_email: str
    # v0.10.9 — any of the three recipient_* may be present depending on
    # handoff_kind. Email may be NULL on team handoffs.
    recipient_email: str | None
    recipient_user_id: str | None = None
    recipient_team_id: str | None = None
    message: str | None
    status: str
    session_title: str | None
    session_tool: str | None
    session_model_id: str | None = None
    session_message_count: int | None = None
    session_total_tokens: int | None = None
    created_at: datetime
    claimed_at: datetime | None = None
    expires_at: datetime
    # v0.10.9 — new fields on detail responses
    ticket_id: str | None = None
    persona_name: str | None = None
    revoked_at: datetime | None = None
    revoked_by_user_id: str | None = None
    revoke_reason: str | None = None
    handoff_kind: str = "individual"
    viewed_at: datetime | None = None
    snapshot_persona_name: str | None = None
    snapshot_ticket_title: str | None = None
    sender_tier_snapshot: str | None = None
    # v0.10.9 — populated on GET /handoffs/{id} detail; omitted on list
    # endpoints to keep payload size sane.
    attachments: list[HandoffAttachmentResponse] = []
    comments: list[HandoffCommentResponse] = []
    events: list[HandoffEventResponse] = []
    # v0.10.9 — populated on claim response only (Codex I.2)
    active_ticket_payload: ActiveTicketPayload | None = None
    dropped_attachments: list[DroppedAttachment] = []


class HandoffListResponse(BaseModel):
    handoffs: list[HandoffResponse]
    total: int


class HandoffSummaryResponse(BaseModel):
    """Compact deterministic summary for a handoff's session."""

    session_id: str
    title: str
    tool: str
    model: str | None = None
    message_count: int = 0
    files_modified: list[str] = []
    commands_executed: int = 0
    tests_run: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    errors_encountered: list[str] = []
    last_assistant_messages: list[str] = []


# Team management schemas (Codex I.1 — minimal CRUD)


class CreateTeamRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    slug: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-z0-9-]+$")


class TeamResponse(BaseModel):
    id: str
    org_id: str
    name: str
    slug: str
    created_by: str | None
    created_at: datetime
    member_count: int = 0


class TeamMemberAddRequest(BaseModel):
    user_id: str = Field(..., max_length=36)


class TeamMemberResponse(BaseModel):
    user_id: str
    user_email: str | None
    added_by: str | None
    added_at: datetime

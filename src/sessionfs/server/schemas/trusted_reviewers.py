"""tk_f503ce5c24c54040 — schemas for the trusted-reviewer admin surface.

Routes live at /api/v1/orgs/{org_id}/trusted-reviewers (org-admin gated).
The trusted_reviewers registry decides whose review verdicts the work-queue
stop oracle trusts (docs/security/review-verdict-provenance.md), so the
register payload is validated tightly: an identity (user_id and/or
service_key_id) MUST be present, and a scope (project_id within the org, OR
org-wide = project_id omitted) MUST be present. The DB CHECK mirrors these
invariants, but routes pre-validate so a missing field is a clean 422, never
a 500 surfaced from the constraint.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class TrustedReviewerCreateRequest(BaseModel):
    """Register an identity as a trusted reviewer under an org.

    `org_id` comes from the URL path, never the body.

    Identity (at least one required):
      - user_id: an org member (a personal user-key identity).
      - service_key_id: a service key belonging to this org.

    Scope (exactly the registry's two shapes):
      - project_id set → scoped to that one project (must be in the org).
      - project_id omitted → org-wide (every project in the org).
    """

    user_id: str | None = Field(None, max_length=64)
    service_key_id: str | None = Field(None, max_length=36)
    project_id: str | None = Field(None, max_length=64)
    reviewer_persona: str = Field("codex-reviewer", min_length=1, max_length=50)
    is_active: bool = True

    @field_validator("user_id", "service_key_id", "project_id", "reviewer_persona")
    @classmethod
    def _strip_blank_to_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @model_validator(mode="after")
    def _require_identity(self):
        # Identity-present invariant (mirrors ck_trusted_reviewer_identity_present).
        # Pre-validated here so a missing identity is a clean 422, not a DB 500.
        if not self.user_id and not self.service_key_id:
            raise ValueError(
                "An identity is required: provide user_id and/or service_key_id."
            )
        # reviewer_persona is required and non-blank (the strip validator may
        # have nulled an all-whitespace value).
        if not self.reviewer_persona:
            raise ValueError("reviewer_persona cannot be blank.")
        return self


class TrustedReviewerResponse(BaseModel):
    """List/detail shape for a trusted-reviewer registration."""

    id: str
    org_id: str | None
    project_id: str | None
    user_id: str | None
    service_key_id: str | None
    reviewer_persona: str
    is_active: bool
    created_by_user_id: str
    created_at: datetime
    revoked_at: datetime | None


class TrustedReviewerRevokeRequest(BaseModel):
    """Optional revoke reason (audit only — soft delete is unconditional)."""

    reason: str | None = Field(None, max_length=500)

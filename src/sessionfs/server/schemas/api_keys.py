"""v0.10.10 — schemas for scoped service API keys + personal user keys.

Two route groups serve two key kinds:
  /api/v1/orgs/{org_id}/service-keys/...  → ServiceKeyCreateRequest etc.
  /api/v1/auth/me/api-keys/...            → PersonalKeyCreateRequest etc.

CreateResponse subclasses include the raw `key` field — returned ONCE
on create + rotate. List/detail responses omit it (Codex R1 MEDIUM 3).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


# Capability:verb vocabulary documented in docs/api-keys.md (v0.10.10).
VALID_SCOPES = frozenset(
    {
        "sessions:read",
        "handoffs:read",
        "handoffs:write",
        "tickets:read",
        "tickets:write",
        "personas:read",
        "personas:write",
        "knowledge:read",
        "knowledge:write",
        "rules:read",
        "rules:write",
        "agent_runs:read",
        "agent_runs:write",
        # v0.17.0 — structured agent-feedback records. read lists/summarizes;
        # write creates. Kept narrow so a reviewer principal can post/consume
        # feedback without the broader agent_runs write surface.
        "agent_feedback:read",
        "agent_feedback:write",
        # WQ-P2 (tk_3481237f3b0847d6) — agent work-queue management surface.
        # work_queues:read inspects (list/get); work_queues:write acts
        # (create/set-status). Same shape as tickets:read/write. User
        # wildcard '*' keys satisfy both (existing behavior).
        "work_queues:read",
        "work_queues:write",
        # Resident R0 (migration 057) — durable mind for reviewer/implementer
        # residents. resident_memory:read hydrates; resident_memory:write
        # writes + compacts + quarantines.
        "resident_memory:read",
        "resident_memory:write",
        "retrieval_audit:read",
        # v0.16 portable skills/plugins marketplace. Discovery is separate
        # from activation; approval and org administration are separately
        # mintable so service principals receive least privilege.
        "marketplace:read",
        "marketplace:write",
        "skills:read",
        "skills:request",
        "skills:approve",
        "skills:admin",
        # Marketplace v2 §7.4: the works-in harness job's credential. Only
        # the claim / projection / result endpoints accept it, and only when
        # the key was minted by a platform admin.
        "skills_verify:run",
        # GitHub #69: the registry poller scheduler's credential (Cloud
        # Scheduler / Helm CronJob). Platform-admin mint only; accepted only by
        # POST /api/v1/admin/skills/registries/poll.
        "skills_registry:poll",
        # Scheduled platform sweeps (Cloud Run jobs / Helm CronJobs): catalog
        # regenerate, verify sweep, freshness sweep, rescan, publisher
        # recheck, GitHub webhook-delivery sweep. Platform-admin mint only;
        # accepted only by those endpoints, and only while the minter is
        # still a platform admin.
        "platform_sweeps:run",
        # GitHub #90: operator metrics scrape (GET /api/v1/admin/metrics).
        # Platform-admin mint only; the key works only while its owner and
        # minter are still active platform admins.
        "admin_metrics:read",
        # Marketplace v2 §8.6: devices post advisory skills-policy
        # compliance reports (CI runners included).
        "devices:report",
        "admin:*",
    }
)


class ServiceKeyCreateRequest(BaseModel):
    """Create a service key under an org. Scopes required + non-wildcard.
    `org_id` comes from the URL path, not the body."""

    name: str = Field(..., min_length=1, max_length=100)
    scopes: list[str] = Field(..., min_length=1)
    expires_in_days: int | None = Field(None, ge=1, le=730)
    project_ids: list[str] | None = Field(None)  # None/empty = all-in-org

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("name cannot be blank")
        return stripped

    @model_validator(mode="after")
    def _validate_scopes(self):
        # Codex R1 finding C — service keys cannot have the '*' wildcard.
        # Reserved for user/admin keys only.
        if "*" in self.scopes:
            raise ValueError(
                "Service keys cannot use the '*' wildcard scope — "
                "enumerate specific scopes explicitly"
            )
        unknown = set(self.scopes) - VALID_SCOPES
        if unknown:
            raise ValueError(
                f"Unknown scopes: {sorted(unknown)}. "
                f"Valid scopes: {sorted(VALID_SCOPES)}"
            )
        # De-dupe preserving order
        seen: set[str] = set()
        deduped = []
        for s in self.scopes:
            if s not in seen:
                seen.add(s)
                deduped.append(s)
        self.scopes = deduped
        if self.project_ids is not None:
            # Strip+dedupe
            cleaned = []
            seen_p: set[str] = set()
            for p in self.project_ids:
                if not isinstance(p, str):
                    continue
                ps = p.strip()
                if not ps or ps in seen_p:
                    continue
                seen_p.add(ps)
                cleaned.append(ps)
            self.project_ids = cleaned or None
        return self


class ServiceKeyResponse(BaseModel):
    """Service key detail/list shape. Raw key NEVER returned here
    (Codex R1 MEDIUM 3) — only on the create/rotate response."""

    id: str
    name: str
    org_id: str
    service_key_name: str
    scopes: list[str]
    project_ids: list[str] | None
    key_prefix: str  # first 12 chars (safe to display, e.g. "sk_sfs_abc12...")
    created_at: datetime
    created_by_user_id: str | None
    expires_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: str | None
    last_used_at: datetime | None
    last_used_ip: str | None
    is_active: bool
    # Tier-coupling status for platform-admin-minted scopes (the key stops
    # working when its owner/minter is no longer an active platform admin).
    # None when the key has no tier-coupled scope; True/False otherwise.
    coupling_ok: bool | None = None
    coupling_reason: str | None = None


class ServiceKeyWarning(BaseModel):
    """A non-fatal warning attached to a create/rotate response."""

    code: str
    message: str


class ServiceKeyCreateResponse(ServiceKeyResponse):
    """Returned ONCE on POST /service-keys. Includes the raw key.
    Save it — subsequent reads only return key_prefix."""

    key: str  # raw sk_sfs_... — returned exactly once
    warnings: list[ServiceKeyWarning] = Field(default_factory=list)


class RevokeKeyRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class PersonalKeyCreateRequest(BaseModel):
    """Mint a personal user-scoped key. Caps to caller's tier."""

    name: str = Field(..., min_length=1, max_length=255)
    expires_in_days: int | None = Field(None, ge=1, le=730)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("name cannot be blank")
        return stripped


class PersonalKeyResponse(BaseModel):
    id: str
    name: str | None
    key_prefix: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    last_used_ip: str | None
    is_active: bool


class PersonalKeyCreateResponse(PersonalKeyResponse):
    """Returned ONCE on POST /auth/me/api-keys."""

    key: str

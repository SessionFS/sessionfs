"""SQLAlchemy 2.0 ORM models."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Float, Index, Integer, String, Text, ForeignKey, UniqueConstraint, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    tier: Mapped[str] = mapped_column(String(20), default="free")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tier_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    storage_used_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    beta_pro_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_mode: Mapped[str] = mapped_column(String(20), default="off", server_default="off")
    sync_debounce: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    audit_trigger: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual")
    summarize_trigger: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual")
    last_client_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_client_platform: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_client_device: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # v0.10.0: per-user default org for multi-org membership. `sfs
    # project init` reads this via /api/v1/auth/me when the user passes
    # neither --org nor --personal and uses it as the new project's
    # scope. Session sync routing does NOT consume this today (uses
    # git remote → Project lookup; see
    # routes/sessions.py:_resolve_project_id_for_session); a v0.10.x
    # follow-up may add a default-org fallback for unmatched remotes.
    # Nullable so single-org / no-org users keep the existing
    # personal-scope default. ON DELETE SET NULL — if the org is
    # deleted or the user is removed from it (the latter is enforced
    # application-side in the member-removal endpoint), this falls
    # back to None.
    default_org_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
    )
    # v0.11.0 P1 — denormalized pointer to the active entitlement for
    # fast resolution. Nullable during migration, populated by the
    # entitlements backfill. The AUTHORITATIVE entitlement is ALWAYS
    # resolved via SELECT FROM entitlements WHERE owner_type='user'
    # AND owner_id=:id AND status='active'. This column is a hint only
    # — never read as an authz shortcut (Sentinel MEDIUM-2).
    entitlement_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("entitlements.id", ondelete="SET NULL"),
        nullable=True,
    )


class ApiKey(Base):
    """API key for cloud-sync auth.

    v0.10.10 (tk_2e030a85253143df) extends with scoped-service-key
    fields. `key_kind='user'` rows behave exactly as v0.10.9 did
    (scopes='["*"]' grants everything); `key_kind='service'` rows
    require explicit scope enumeration, an `org_id`, and a
    `service_key_name`, and are only admitted by `require_scope(...)`
    dependencies (never by plain `get_current_user`). Codex R1+R2.
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        Index(
            "idx_api_keys_kind_active_expires",
            "key_kind", "is_active", "expires_at",
        ),
        Index("idx_api_keys_org_id", "org_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # v0.10.10 — scoped service key fields. Existing rows backfill to
    # key_kind='user' + scopes='["*"]' per migration 042.
    key_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user", server_default="user"
    )
    org_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Default '["*"]' matches the migration 042 backfill for existing
    # rows: new user keys minted without explicit scopes inherit the
    # legacy wildcard. Service keys always set scopes explicitly via
    # the routes layer so the default never applies to them.
    scopes: Mapped[str] = mapped_column(
        Text, nullable=False, default='["*"]', server_default='["*"]'
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_used_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    service_key_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # JSON list of project_ids; null/empty = all projects in org.
    project_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Codex R3 MEDIUM 2 — real raw-key prefix (sk_sfs_<6 hex>) captured
    # at create time. List/get responses display this so ops can match
    # against deployed keys during incident response and rotation.
    # Existing rows back-fill to NULL since we don't have the raw key.
    key_prefix: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # SSO-P1: marks keys minted via the OIDC SSO callback (§3.5).
    # Under enforcement, a human's legacy (non-SSO-minted) keys are gated
    # at auth time until they re-login via SSO.  Service keys are
    # categorically exempt from SSO enforcement (§4.2).
    sso_minted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("idx_sessions_user_id", "user_id"),
        Index("idx_sessions_source_tool", "source_tool"),
        Index("idx_sessions_created_at", "created_at"),
        Index("idx_sessions_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    tags: Mapped[str] = mapped_column(Text, default="[]")  # JSON array as TEXT
    source_tool: Mapped[str] = mapped_column(String(50), nullable=False)
    source_tool_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    original_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_use_count: Mapped[int] = mapped_column(Integer, default=0)
    total_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    total_output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    blob_key: Mapped[str] = mapped_column(String(500), nullable=False)
    blob_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    etag: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    messages_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    alias: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    delete_scope: Mapped[str | None] = mapped_column(String(16), nullable=True)
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    git_remote_normalized: Mapped[str | None] = mapped_column(String(255), nullable=True)
    git_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    git_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    dlp_scan_results: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Rules portability (migration 028) — instruction provenance
    rules_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rules_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    rules_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none", server_default="none"
    )
    instruction_artifacts: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    # v0.10.0 Phase 5 — multi-org daemon routing. Links a captured
    # session to its project. The daemon resolves project membership
    # at capture time from the workspace's git remote; the sync upload
    # carries the project_id forward to the server, which validates
    # the caller has access (owner or org member of project.org_id).
    # Nullable for sessions captured before Phase 5 OR for workspaces
    # that aren't linked to a project (untracked git repos / non-repo
    # workspaces). ON DELETE SET NULL preserves the session even if
    # the project is hard-deleted — same durability shape as the
    # ProjectTransfer.project_id column from Phase 1.
    project_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # v0.10.1 Phase 1 — agent personas + ticketing provenance. Set by
    # the daemon at capture time from ~/.sessionfs/active_ticket.json
    # if a developer (human or AI) was working under a named persona
    # on a specific ticket. Plain String, no FK — personas and tickets
    # are project-scoped and a session may reference rows in the
    # target project before/after this session row is created. The
    # capture pipeline doesn't enforce existence; the read paths
    # (dashboard, /api/v1/sessions/{id}) join cooperatively.
    persona_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ticket_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    retrieval_audit_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class Handoff(Base):
    __tablename__ = "handoffs"
    __table_args__ = (
        Index("idx_handoffs_session_id", "session_id"),
        Index("idx_handoffs_sender_id", "sender_id"),
        Index("idx_handoffs_recipient_email", "recipient_email"),
        # Inbox lookups filter by lower(recipient_email); the raw column
        # index above won't be used by that predicate. The normalized
        # column is populated at write time (route layer) and indexed so
        # inbox queries hit it directly. See migration 032.
        Index("idx_handoffs_recipient_email_normalized", "recipient_email_normalized"),
        Index("idx_handoffs_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id"), nullable=False
    )
    sender_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    # v0.10.9 — recipient_email is now NULLABLE. Server enforces
    # exactly-one-recipient invariant (email XOR user_id XOR team_id)
    # at create time.
    recipient_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Lowercased copy of recipient_email used for case-insensitive lookups.
    # Backfilled from existing rows by migration 032 and kept in sync at
    # write time by the handoff create route. Nullable on legacy rows
    # only; new rows always populate it.
    recipient_email_normalized: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    recipient_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    # v0.10.9 — direct account / team targeting. Recipient is determined
    # by exactly one of recipient_email / recipient_user_id / recipient_team_id.
    recipient_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Codex R2 MEDIUM #1 — real FK to teams.id with SET NULL so deleting
    # a team strands the handoff to status only, not orphan ID.
    recipient_team_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recipient_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Snapshot of session metadata at creation time — immune to session-ID reuse
    snapshot_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    snapshot_tool: Mapped[str | None] = mapped_column(String(100), nullable=True)
    snapshot_model_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    snapshot_message_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # v0.10.9 — provenance carried through to recipient's claim. Plain
    # strings (not FKs) for audit-row survival per the v0.10.2 agent_runs
    # pattern. Claim validates these against the recipient's accessible
    # projects before populating active-ticket bundle.
    ticket_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    persona_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # v0.10.9 — revoke metadata. Sender (or org admin) can revoke a
    # pending handoff with a reason that surfaces in the revoke email.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # v0.10.9 — 'individual' | 'team'. Determines which recipient_* field
    # is authoritative for inbox lookups + claim eligibility.
    handoff_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="individual"
    )
    # v0.10.9 — recipient peeked at the handoff metadata via GET before
    # claiming. Populated on the first GET by a recipient-context user.
    viewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # v0.10.9 — display-time snapshots for when persona/ticket later
    # rename or delete (mirrors snapshot_title pattern).
    snapshot_persona_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    snapshot_ticket_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # v0.10.9 — tier at send time. Claim does NOT re-check sender's
    # current tier (per Codex I.4); only recipient access matters at
    # claim. Audit can compare snapshot vs current for forensic purposes.
    sender_tier_snapshot: Mapped[str | None] = mapped_column(String(20), nullable=True)


class Team(Base):
    """v0.10.9 — org sub-group used for team handoffs."""

    __tablename__ = "teams"
    __table_args__ = (
        Index("idx_teams_org_id", "org_id"),
        UniqueConstraint("org_id", "slug", name="uq_teams_org_slug"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(50), nullable=False)
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TeamMember(Base):
    """v0.10.9 — user ↔ team membership. Users belong to multiple teams
    (per Codex A.2 — single team_id on org_members is insufficient)."""

    __tablename__ = "team_members"
    __table_args__ = (
        Index("idx_team_members_team_id", "team_id"),
        Index("idx_team_members_user_id", "user_id"),
        UniqueConstraint("team_id", "user_id", name="uq_team_members_team_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    added_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HandoffComment(Base):
    """v0.10.9 — sender/recipient comment thread on a handoff."""

    __tablename__ = "handoff_comments"
    __table_args__ = (
        Index("idx_handoff_comments_handoff_id", "handoff_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    handoff_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("handoffs.id", ondelete="CASCADE"), nullable=False
    )
    author_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HandoffEvent(Base):
    """v0.10.9 — durable audit log replacing the derived `_effective_status`
    quirk. Event types: created, emailed, viewed, claimed, revoked,
    expired, declined, commented, claim_failed_stale, email_delivery_failed,
    attachment_dropped, source_session_deleted."""

    __tablename__ = "handoff_events"
    __table_args__ = (
        Index("idx_handoff_events_handoff_id", "handoff_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    handoff_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("handoffs.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # v0.10.10 — service-key provenance (Codex R1 HIGH 2 + R5 MEDIUM).
    # Distinct from actor_user_id (which may still be set since the
    # service key 'runs as' a user context). actor_type='service_key'
    # tells the audit viewer the key was the actual caller, not the
    # human directly. service_key_id is the durable identifier for
    # incident response — names are mutable and reusable.
    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user", server_default="user"
    )
    service_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    service_key_name: Mapped[str | None] = mapped_column(String(100), nullable=True)


class HandoffAttachment(Base):
    """v0.10.9 — sender curates additional context for the recipient:
    KB entries, wiki pages, and tickets they should also read. Validated
    against sender's accessible projects on create and against recipient's
    accessible projects on claim (stale refs silently dropped with an
    `attachment_dropped` event and surfaced as `dropped_attachments` on
    the claim response per Codex I.7).

    Codex R2 MEDIUM #3 — `project_id` stored on each attachment row so
    wiki_page refs validate unambiguously (slugs are project-local; without
    this two projects with the same `auth-flow` slug would collide).
    """

    __tablename__ = "handoff_attachments"
    __table_args__ = (
        Index("idx_handoff_attachments_handoff_id", "handoff_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    handoff_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("handoffs.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # 'kb_entry'|'wiki_page'|'ticket'
    ref_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserJudgeSettings(Base):
    __tablename__ = "user_judge_settings"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AdminAction(Base):
    __tablename__ = "admin_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    admin_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BookmarkFolder(Base):
    __tablename__ = "bookmark_folders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Bookmark(Base):
    __tablename__ = "bookmarks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    folder_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("bookmark_folders.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ShareLink(Base):
    __tablename__ = "share_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class GitHubInstallation(Base):
    __tablename__ = "github_installations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # GitHub's installation ID
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    account_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    auto_comment: Mapped[bool] = mapped_column(Boolean, default=True)
    include_trust_score: Mapped[bool] = mapped_column(Boolean, default=True)
    include_session_links: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SessionSummaryRecord(Base):
    __tablename__ = "session_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    files_modified: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    files_read: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    commands_executed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tests_run: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tests_passed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tests_failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    packages_installed: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    errors_encountered: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    what_happened: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_decisions: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    open_issues: Mapped[str | None] = mapped_column(Text, nullable=True)
    narrative_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    personas_active: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditReport(Base):
    __tablename__ = "audit_reports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    judge_model: Mapped[str] = mapped_column(String(100), nullable=False)
    judge_provider: Mapped[str] = mapped_column(String(50), nullable=False)
    judge_base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    trust_score: Mapped[int] = mapped_column(Integer, nullable=False)
    total_claims: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    verified_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    unverified_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    contradiction_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    findings: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    execution_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GitLabSettings(Base):
    __tablename__ = "gitlab_settings"

    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), primary_key=True)
    instance_url: Mapped[str] = mapped_column(String(500), nullable=False, server_default="https://gitlab.com")
    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    webhook_secret: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SyncWatchlist(Base):
    __tablename__ = "sync_watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        Index("idx_projects_org_id", "org_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    git_remote_normalized: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    context_document: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id"), nullable=False
    )
    # v0.10.0: org-scoped projects. NULL = personal project (the
    # pre-v0.10.0 state — preserved for every existing row by the
    # migration). NON-NULL = team project, gated by org-admin role
    # in the routes layer. ON DELETE SET NULL: deleting the org
    # demotes its projects to personal-scope rather than destroying
    # them (data-stays-access-revoked invariant, KB entry 230 #3).
    org_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
    )
    auto_narrative: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    kb_retention_days: Mapped[int] = mapped_column(Integer, default=180, server_default="180")
    kb_max_context_words: Mapped[int] = mapped_column(Integer, default=2000, server_default="2000")
    kb_section_page_limit: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # v0.11.0 — multi-repo projects tombstone + orphaned state (§3.4).
    # merged_into_project_id: set when this project is merged into another.
    # merged_at: when the merge occurred.
    # repo_reclaimed_at: set when ALL repos were reclaimed by verified owners
    #   via displacement. The project keeps its own KB/personas/tickets/rules
    #   (NEVER auto-imported into the claimant). Distinct from merge tombstone.
    merged_into_project_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    )
    merged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    repo_reclaimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PRComment(Base):
    __tablename__ = "pr_comments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repo_full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    comment_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    session_ids: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    tier: Mapped[str] = mapped_column(String(20), nullable=False, server_default="team")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_limit_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    storage_used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    seats_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    settings: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # v0.11.0 P1 — denormalized pointer to the active entitlement for
    # fast resolution. Nullable during migration, populated by the
    # entitlements backfill. Same hint-only semantics as User.entitlement_id.
    entitlement_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("entitlements.id", ondelete="SET NULL"),
        nullable=True,
    )


class OrgMember(Base):
    __tablename__ = "org_members"
    __table_args__ = (
        Index("idx_org_members_org", "org_id"),
        Index("idx_org_members_user", "user_id"),
        # v0.11.0 P1 — structural invariant: at most one owner per org.
        # Created in migration 050 AFTER the deterministic single-owner
        # backfill. Also defined here so ORM-level create_all enforces it.
        Index(
            "uq_org_members_one_owner_per_org",
            "org_id",
            unique=True,
            postgresql_where=text("role = 'owner'"),
            sqlite_where=text("role = 'owner'"),
        ),
        # P4 follow-up — structural vocabulary guard (added on PG via
        # migration 052; present here so create_all/SQLite enforces it too).
        CheckConstraint(
            "role IN ('owner', 'admin', 'member')",
            name="ck_org_members_role",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, server_default="member")
    invited_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OrgOwnerTransfer(Base):
    """Two-step ownership transfer with single-pending invariant.

    P4 of the licensing + org-management redesign (§2.4.3).
    At most one pending transfer per org — enforced by the partial
    unique index uq_org_owner_transfer_one_pending (migration 051).
    """

    __tablename__ = "org_owner_transfer"
    __table_args__ = (
        Index("idx_org_owner_transfer_org", "org_id"),
        Index(
            "uq_org_owner_transfer_one_pending",
            "org_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        # P4 follow-up — vocabulary guard (PG via migration 052; here for
        # create_all/SQLite).
        CheckConstraint(
            "status IN ('pending', 'accepted', 'cancelled', 'expired')",
            name="ck_org_owner_transfer_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    from_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    to_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending",
        comment="pending | accepted | cancelled | expired"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OrgInvite(Base):
    __tablename__ = "org_invites"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, server_default="member")
    invited_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # v0.10.22 tk_6afbcfefe5804c1d — decline + resend lifecycle.
    # declined_at + accepted_at are mutually exclusive (enforced at the
    # route layer, not via CHECK constraint, to keep SQLite local-mode
    # working). last_emailed_at tracks the most recent send so the
    # dashboard / resend endpoint can show recipients the last nudge.
    declined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decline_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectTransfer(Base):
    """v0.10.0 — durable state machine + audit row for project
    ownership transfers (personal ↔ org, org ↔ org).

    State machine: ``pending`` → ``accepted`` | ``rejected`` | ``cancelled``.
    Rows are NEVER deleted post-resolution — the audit trail is the
    compliance artifact (CEO directive, KB entry 230 #1).

    ``from_scope`` / ``to_scope`` hold either the literal string
    ``"personal"`` or an ``organizations.id``. Stored as plain TEXT
    (not FK) so the historical record survives an org deletion.

    ``target_user_id`` identifies the user who must accept WHILE the
    row is pending. The dashboard inbox query is
    ``WHERE state='pending' AND target_user_id=:user``. The composite
    index ``idx_project_transfers_inbox`` matches that shape. For an
    auto-accept shape (initiator == target — e.g. a user transferring
    their own personal project into an org they belong to) the route
    layer sets target_user_id = initiated_by and flips state to
    ``accepted`` at create time.

    ``accepted_by`` is the user at the moment of acceptance, frozen
    for audit even if ``target_user_id`` is later nulled by a user
    delete.
    """

    __tablename__ = "project_transfers"
    __table_args__ = (
        Index("idx_project_transfers_project", "project_id"),
        Index("idx_project_transfers_state", "state"),
        # Composite index for the dashboard inbox query:
        # "incoming pending transfers for user X".
        Index("idx_project_transfers_inbox", "state", "target_user_id"),
        # Concurrency-safe duplicate guard: at most one pending row
        # per project. The route does a SELECT precheck for the
        # friendly error message, but this DB-level constraint is the
        # backstop two concurrent initiates collide on. Codex Phase-2
        # round-2 catch (KB entry 248).
        Index(
            "idx_project_transfers_pending_unique",
            "project_id",
            unique=True,
            postgresql_where=text("state = 'pending'"),
            sqlite_where=text("state = 'pending'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # project_id is FK ON DELETE SET NULL (nullable). The audit row
    # MUST survive a hard project delete (CEO durability invariant,
    # KB entry 230 #1; Codex Phase-1 round-2 catch).
    #
    # Two snapshot columns keep the row self-describing after
    # project_id goes NULL:
    #   - project_git_remote_snapshot: the STABLE unique identifier.
    #     `projects.git_remote_normalized` is `unique=True`, so a
    #     snapshot disambiguates audit rows even if two deleted
    #     projects shared a display name (Codex Phase-1 round-3
    #     catch, KB entry 238).
    #   - project_name_snapshot: human-readable label for display.
    #     Not load-bearing for identity — use git_remote_snapshot
    #     for any "which project was this?" lookups.
    project_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    )
    project_git_remote_snapshot: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    project_name_snapshot: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    initiated_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    target_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    from_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    to_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending"
    )
    accepted_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StripeEvent(Base):
    __tablename__ = "stripe_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HelmLicense(Base):
    __tablename__ = "helm_licenses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_email: Mapped[str] = mapped_column(String(255), nullable=False)
    license_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="paid")
    tier: Mapped[str] = mapped_column(String(20), nullable=False, server_default="enterprise")
    seats_limit: Mapped[int | None] = mapped_column(Integer, server_default="25")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    cluster_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    validation_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    license_metadata: Mapped[str] = mapped_column("metadata", Text, nullable=False, server_default="{}")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # v0.11.0 P1 — FK to the org this license is bound to.
    # Nullable (unbound licenses exist). UNIQUE (one org per license).
    # Set atomically in the activation Phase B transaction (org-before-FK-bind
    # ordering, rowcount-1 guard — see design §2.2).
    org_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )


class LicenseValidation(Base):
    __tablename__ = "license_validations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    license_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("helm_licenses.id", ondelete="CASCADE"), nullable=False
    )
    cluster_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class KnowledgeEntry(Base):
    __tablename__ = "knowledge_entries"
    __table_args__ = (
        Index("idx_ke_project", "project_id"),
        Index("idx_ke_session", "session_id"),
        Index("idx_ke_type", "project_id", "entry_type"),
        # v0.9.9.7 perf-3: list_entries filters and sorts on these columns
        # but only project_id was indexed. Composites added by migration
        # 033 cover the default listing path, the pending-compile path,
        # and the keyset cursor scan order.
        Index(
            "idx_ke_listing",
            "project_id",
            "dismissed",
            "claim_class",
            "freshness_class",
        ),
        Index(
            "idx_ke_pending",
            "project_id",
            "compiled_at",
            "dismissed",
        ),
        # Cursor pagination path: WHERE project_id=? AND id < cursor ORDER BY
        # created_at DESC, id DESC LIMIT N. The id-DESC tail is intrinsic
        # since id is the primary key — leading with (project_id, created_at)
        # is what the planner needs to skip the heap scan.
        Index(
            "idx_ke_cursor",
            "project_id",
            "created_at",
        ),
        Index(
            "idx_knowledge_persona_recent",
            "project_id",
            "persona_name",
            text("created_at DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0")
    source_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    persona_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    author_class: Mapped[str] = mapped_column(
        String(16), nullable=False, default="human", server_default="human"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    compiled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Audit fields for dismissals (migration 031). Populated when an entry
    # is dismissed via the dismiss endpoint or MCP tool. NULL on legacy
    # rows that were dismissed before the audit fields existed.
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dismissed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_relevant_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reference_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Knowledge Base v2 fields
    claim_class: Mapped[str] = mapped_column(String(20), nullable=False, default="claim", server_default="claim")
    entity_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    freshness_class: Mapped[str] = mapped_column(String(20), nullable=False, default="current", server_default="current")
    supersession_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_by: Mapped[str | None] = mapped_column(String(50), nullable=True)
    retrieved_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    used_in_answer_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    compiled_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # v0.10.10 — service-key provenance (Codex R1 HIGH 2).
    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user", server_default="user"
    )
    service_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    service_key_name: Mapped[str | None] = mapped_column(String(100), nullable=True)


class ContextCompilation(Base):
    __tablename__ = "context_compilations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entries_compiled: Mapped[int] = mapped_column(Integer, nullable=False)
    context_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_after: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_manifest: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    compiled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TelemetryEvent(Base):
    __tablename__ = "telemetry_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    install_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    os: Mapped[str] = mapped_column(String(50), nullable=False)
    tools_active: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    sessions_captured_24h: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    avg_session_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    features_used: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    errors_24h: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tier: Mapped[str] = mapped_column(String(20), nullable=False, server_default="free")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class KnowledgePage(Base):
    __tablename__ = "knowledge_pages"
    __table_args__ = (
        Index("idx_kp_project", "project_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    page_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    word_count: Mapped[int] = mapped_column(Integer, server_default="0")
    entry_count: Mapped[int] = mapped_column(Integer, server_default="0")
    parent_slug: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    auto_generated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class WikiPageRevision(Base):
    """v0.10.7 — append-only per-revision history for wiki pages.

    Inserted from routes/wiki.py:create_or_update_page on every page
    write. Full content snapshot per revision (pages are bounded in
    size — snapshots are simpler than diffs and cheaper to render).
    """

    __tablename__ = "wiki_page_revisions"
    __table_args__ = (
        Index(
            "idx_wiki_revisions_history",
            "project_id",
            "page_slug",
            "revised_at",
            "id",
        ),
        UniqueConstraint(
            "project_id",
            "page_slug",
            "revision_number",
            name="uq_wiki_revisions_number",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    page_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content_snapshot: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    persona_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ticket_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revised_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProjectRules(Base):
    """Canonical project rules (one per project)."""

    __tablename__ = "project_rules"
    __table_args__ = (
        Index("idx_project_rules_project", "project_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    static_rules: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    include_knowledge: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    knowledge_types: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default='["convention", "decision"]',
        server_default='["convention", "decision"]',
    )
    knowledge_max_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1500, server_default="1500"
    )
    include_context: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    context_sections: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default='["overview", "architecture"]',
        server_default='["overview", "architecture"]',
    )
    context_max_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1500, server_default="1500"
    )
    tool_overrides: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    enabled_tools: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    created_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RulesVersion(Base):
    """Immutable snapshot of compiled rules outputs."""

    __tablename__ = "rules_versions"
    __table_args__ = (
        Index("idx_rules_versions_rules_id", "rules_id", "version"),
        UniqueConstraint("rules_id", "version", name="uq_rules_versions_rid_ver"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    rules_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("project_rules.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    static_rules: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    compiled_outputs: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    knowledge_snapshot: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    context_snapshot: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    compiled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    compiled_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class KnowledgeLink(Base):
    __tablename__ = "knowledge_links"
    __table_args__ = (
        Index("idx_kl_source", "project_id", "source_type", "source_id"),
        Index("idx_kl_target", "project_id", "target_type", "target_id"),
        # uq_kl_link is declared in migration 019 (`019_wiki_pages.py`).
        # Mirror it on the ORM model so SQLite test schemas built via
        # `Base.metadata.create_all` enforce the same uniqueness as
        # production PostgreSQL. tk_09d8bdf4f6374a13 — without this,
        # idempotency regression tests for _auto_supersede are weaker
        # than prod and can claim "no IntegrityError" while the migrated
        # schema would still raise.
        UniqueConstraint(
            "project_id", "source_type", "source_id", "target_type", "target_id",
            name="uq_kl_link",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    link_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="related")
    confidence: Mapped[float] = mapped_column(Float, server_default="1.0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())



# v0.10.1 Phase 1 — Agent Personas + Ticketing (migration 037).
#
# Personas are portable AI roles scoped to a project. Tickets are
# self-contained task units assigned to a persona, tracked through a
# status FSM, and linked to sessions via the local provenance bundle
# pattern (see daemon capture path). Persona/ticket linkage on a
# session row uses plain String columns (Session.persona_name +
# Session.ticket_id, above) — no FK because sessions can carry the
# tag forward even if the persona/ticket row is hard-deleted.


class AgentPersona(Base):
    """A portable AI role scoped to one project.

    Multiple personas per project; the same persona name is unique
    within a project (uq_persona_project_name). `content` is opaque
    markdown injected into the context window as-is. `specializations`
    is a JSON array of domain keywords used for routing suggestions in
    a future version — v0.10.1 does NOT auto-filter the KB by them.
    Soft-delete via `is_active = false`.
    """

    __tablename__ = "agent_personas"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_persona_project_name"),
        Index("idx_persona_project_active", "project_id", "is_active"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    specializations: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    # v0.10.19 Phase 3.6 — service-key provenance for persona writes.
    # Nullable for pre-migration rows; persona create/update/delete routes
    # populate this from AuthContext for both user keys and service keys.
    actor_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    service_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    service_key_name: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Ticket(Base):
    """A self-contained task unit assigned to a persona.

    Status FSM enforced server-side (routes layer):
      suggested → open → in_progress → blocked → review → done → cancelled
    Agent-created tickets default to 'suggested' (quality gate at
    creation time requires acceptance criteria + 20+ char description
    + ≤3 per session). Reporter provenance is structured into three
    fields: user_id (always set, from auth), session_id (optional —
    set if created during a captured session), persona (optional —
    set if created by an agent working under a persona).

    `assigned_to` is the persona name (NOT FK) — tickets may be
    created before the persona row exists; start_ticket() validates
    at execution time. ON DELETE CASCADE on project_id means deleting
    a project takes its tickets with it; sessions stay (Session.
    ticket_id is a plain String, not an FK).
    """

    __tablename__ = "tickets"
    __table_args__ = (
        Index("idx_ticket_project_status", "project_id", "status"),
        Index("idx_ticket_assigned", "project_id", "assigned_to", "status"),
        Index("idx_ticket_project_parent", "project_id", "parent_ticket_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    # v0.10.24 tk_dbccde26ed604b3c — Issue/Task rollup (Option A). `kind`
    # distinguishes PM-triaged Issues from executor-owned Tasks; existing
    # rows default to 'task' so behavior is unchanged. `parent_ticket_id`
    # links a Task to its containing Issue (single-level nesting only in
    # v1 — Issues cannot be nested under other Issues; enforced at the
    # route layer). NOT a reuse of TicketDependency, which is for DAG
    # ordering ("A blocks B"), not container rollup ("A rolls up B").
    kind: Mapped[str] = mapped_column(
        String(10), nullable=False, default="task", server_default="task"
    )
    parent_ticket_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tickets.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Task
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    priority: Mapped[str] = mapped_column(
        String(20), nullable=False, default="medium", server_default="medium"
    )

    # Assignment — must match an existing AgentPersona.name in the
    # same project when start_ticket() runs. Nullable for unassigned
    # tickets that humans triage.
    assigned_to: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Reporter provenance (structured, not free string).
    created_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_persona: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # v0.10.18 Phase 3.5 — service-key provenance for ticket creation.
    # Nullable for pre-migration rows; create_ticket populates this from
    # AuthContext for both user keys and service keys.
    actor_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    service_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    service_key_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Status FSM.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="open", server_default="open"
    )
    lease_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Context — explicit references chosen by the reporter. NO
    # automatic KB injection in v1; compile_persona_context only
    # uses these claim IDs verbatim.
    context_refs: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    file_refs: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    related_sessions: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    acceptance_criteria: Mapped[str] = mapped_column(
        Text, default="[]", server_default="[]"
    )

    # Resolution.
    resolver_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolver_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completion_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_files: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    knowledge_entry_ids: Mapped[str] = mapped_column(
        Text, default="[]", server_default="[]"
    )

    # Timestamps.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TicketDependency(Base):
    """Join table for ticket-to-ticket dependencies.

    Both columns part of the composite PK; ON DELETE CASCADE on each
    side so deleting a ticket cleans up both directions. CHECK
    constraint prevents a ticket from depending on itself. Cycle
    detection is deliberately application-layer (DAG enforcement on
    insert) — too expensive to compute in SQL on every insert.
    """

    __tablename__ = "ticket_dependencies"
    __table_args__ = (
        CheckConstraint("ticket_id != depends_on_id", name="ck_no_self_dep"),
        Index("idx_ticket_deps_depends_on", "depends_on_id"),
    )

    ticket_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    depends_on_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TicketComment(Base):
    """Append-only comment thread on a ticket.

    Authors are humans or AI agents; `author_persona` distinguishes.
    `session_id` links the comment to the capture that produced it
    (plain String, not FK — sessions may be deleted independently and
    the comment should survive).
    """

    __tablename__ = "ticket_comments"
    __table_args__ = (
        Index("idx_comment_ticket", "ticket_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    author_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    author_persona: Mapped[str | None] = mapped_column(String(50), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # v0.10.10 — service-key provenance (Codex R1 HIGH 2). actor_type
    # defaults 'user' so back-compat is automatic; routes that write
    # this row populate service_key_id+_name from the AuthContext when
    # key_kind='service'.
    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user", server_default="user"
    )
    service_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    service_key_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # tk_d42170b4670f4448 — server-stamped trust decision at write time
    # (docs/security/review-verdict-provenance.md). Recorded from
    # AuthContext against the trusted_reviewers registry — NEVER from the
    # request body. compute_review_state counts a comment as an
    # authoritative review verdict only when this is true; author_persona
    # is display-only and no longer carries authority. server_default
    # 'false' makes every existing/forged row non-authoritative
    # (fail-closed). Migration 053 backfills the known operator's
    # historical codex-reviewer comments.
    verdict_trusted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class TrustedReviewer(Base):
    """Registry of identities authorized to post counted review verdicts.

    tk_d42170b4670f4448 (docs/security/review-verdict-provenance.md). Binds
    an authenticated identity (user_id and/or service_key_id) to the reviewer
    persona it may speak as, scoped to a project OR org-wide:
      - project-scoped: project_id set, org_id NULL → one project.
      - org-wide:       org_id set, project_id NULL → every project in the org.

    `create_ticket_comment` consults this registry from AuthContext to stamp
    `TicketComment.verdict_trusted`. Registration is admin-gated; revocation
    (is_active=false / revoked_at set) stops FUTURE verdicts but never
    rewrites settled verdict_trusted rows. App-assigned PK ('tr_<hex>').
    The inline CheckConstraints mirror migration 053 so create_all / SQLite
    enforce identity-present + scope-present.
    """

    __tablename__ = "trusted_reviewers"
    __table_args__ = (
        Index("idx_trusted_reviewer_project", "project_id"),
        Index("idx_trusted_reviewer_org", "org_id"),
        CheckConstraint(
            "(user_id IS NOT NULL) OR (service_key_id IS NOT NULL)",
            name="ck_trusted_reviewer_identity_present",
        ),
        CheckConstraint(
            "(project_id IS NOT NULL) OR (org_id IS NOT NULL)",
            name="ck_trusted_reviewer_scope_present",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
    )
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    service_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reviewer_persona: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="codex-reviewer"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TicketEdit(Base):
    """Per-field audit row for ticket field mutations via update_ticket.

    tk_835a876529de4551 — every successful call to the PATCH-shaped
    update verb writes one row per mutated field so the historical
    record of what the field looked like before the change is
    preserved server-side. Pairs with the auto-posted diff comment
    on TicketComment (human-readable summary) — this table is the
    structured per-field history.

    `edited_by_user_id` and `edited_by_persona` are plain Strings, not
    FKs, so future user-row deletes can't cascade away the audit trail.
    Matches the AdminAction / KnowledgeEntry audit-triple convention.
    `lease_epoch` captures the ticket's epoch at edit time for
    concurrent-edit ordering reconstruction.
    """

    __tablename__ = "ticket_edits"
    __table_args__ = (
        Index("idx_ticket_edits_ticket_at", "ticket_id", "edited_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    edited_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    edited_by_persona: Mapped[str | None] = mapped_column(String(50), nullable=True)
    field_name: Mapped[str] = mapped_column(String(50), nullable=False)
    # JSON-encoded prior + new values. Text (not native JSONB) keeps
    # the column cross-DB compatible for SQLite local-mode deployments;
    # PG queries can json_extract via the operator on the typed cast.
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class AgentRun(Base):
    """One execution of one persona, optionally against one ticket.

    v0.10.2 — AgentRun is the audit/enforcement record for ephemeral
    agent work. The server tracks identity (which persona ran), trigger
    (manual/CI/webhook), result severity, findings, and policy outcome.
    It does NOT spawn the model — `start_agent_run` returns compiled
    persona+ticket context that the caller feeds into its own runtime.

    Status FSM (enforced at routes layer):
        queued → running → passed | failed | errored | cancelled
        queued → cancelled
        running → cancelled

    `persona_name`, `ticket_id`, `session_id` are plain Strings, not
    FKs — same pattern as `sessions.persona_name` / `sessions.ticket_id`
    from migration 037. Personas may be soft-deleted; tickets and
    sessions may be hard-deleted; the AgentRun audit row survives.
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("idx_agent_run_project_status", "project_id", "status"),
        Index("idx_agent_run_ticket", "ticket_id"),
        Index("idx_agent_run_project_persona", "project_id", "persona_name"),
        Index(
            "idx_agent_run_project_trigger",
            "project_id",
            "trigger_source",
            "trigger_ref",
        ),
        Index("idx_agent_run_project_created", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # run_<hex>
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Required execution metadata.
    persona_name: Mapped[str] = mapped_column(String(50), nullable=False)
    tool: Mapped[str] = mapped_column(
        String(50), nullable=False, default="generic", server_default="generic"
    )
    trigger_source: Mapped[str] = mapped_column(
        String(30), nullable=False, default="manual", server_default="manual"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="queued", server_default="queued"
    )

    # Optional ticket linkage.
    ticket_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # CI / trigger context.
    trigger_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ci_provider: Mapped[str | None] = mapped_column(String(30), nullable=True)
    ci_run_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Result fields (set on complete).
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    findings_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    findings: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )

    # Policy fields.
    fail_on: Mapped[str | None] = mapped_column(String(20), nullable=True)
    policy_result: Mapped[str | None] = mapped_column(String(10), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Session linkage (.sfs session that captured this run).
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Triggerer provenance.
    triggered_by_user_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    triggered_by_persona: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Timestamps.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # v0.10.10 — service-key provenance (Codex R1 HIGH 2). When an
    # agent run is triggered by a service key, actor_type='service_key'
    # + service_key_id/_name surface the actual minted key in audit
    # trails, separate from the human triggered_by_user_id (which may
    # be the human who minted the key).
    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user", server_default="user"
    )
    service_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    service_key_name: Mapped[str | None] = mapped_column(String(100), nullable=True)


class WorkQueue(Base):
    """A durable, project-scoped plan for an agent to service tickets.

    tk_529a64620db846f5 (WQ-P1) — the durable definition of an autonomous
    ticket-closing loop (design tk_c2ed6093acde4d55,
    docs/design/agent-work-queues.md §3.1). It owns selection (a filter or
    explicit ticket-id list, in `selector`), a `mode`, a stop condition +
    budget, an advisory cadence, and a queue-level `lease_epoch` fencing
    concurrent queue mutation (same pattern as Ticket.lease_epoch).

    A WorkQueue is distinct from Ticket (a single unit of work), AgentPersona
    (who acts), and AgentRun (one execution): it is the standing loop that
    emits many AgentRuns over time and answers "where is the cursor, what's
    the stop condition, when is the next wake?". App-assigned PK ('wq_<hex>').
    Inline CheckConstraints mirror migration 054 so create_all / SQLite
    enforce the mode + status enums.
    """

    __tablename__ = "work_queues"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "name", name="uq_work_queue_project_name"
        ),
        Index("idx_work_queue_project_status", "project_id", "status"),
        CheckConstraint(
            "mode IN ('review_until_clean', 'implement_until_done', 'triage')",
            name="ck_work_queue_mode",
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'completed', 'cancelled')",
            name="ck_work_queue_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # wq_<hex>
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    mode: Mapped[str] = mapped_column(String(30), nullable=False)
    assigned_persona: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # JSON-as-Text (not native JSONB) for cross-DB SQLite/PG compatibility.
    selector: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    auto_adopt: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    max_adopt_per_wake: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    stop_condition: Mapped[str] = mapped_column(
        String(30), nullable=False, default="queue_empty",
        server_default="queue_empty",
    )
    cadence_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=300, server_default="300"
    )
    max_tickets_per_run: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    max_attempts_per_item: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    lease_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Provenance triple.
    created_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_session_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    created_by_persona: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkQueueItem(Base):
    """The per-(queue, ticket) cursor — the durable resumable loop state.

    tk_529a64620db846f5 (WQ-P1, design §3.2). No chat memory is required to
    resume a loop: everything the wake mechanism needs lives here.

    Cursor split (the headline crash-safety design): `last_seen_comment_*` is
    advanced when a directive is EMITTED (the `since` floor for the next
    delta); `last_acked_comment_*` is advanced ONLY by complete_work_queue_step
    after the writeback is validated/committed. The stop oracle and the
    reviewer-turn check read the ACKED cursor. A crash between directive and
    writeback leaves SEEN ahead of ACKED with an open directive lease
    (`open_directive_id` / `open_directive_run_id`) → the same directive
    re-emits (no review lost, none replayed).

    `item_status` is the queue's view of the loop and is DISTINCT from
    Ticket.status. `ticket_id` is a plain string (the ticket may be hard
    deleted; the cursor row survives). App-assigned PK ('wqi_<hex>').
    """

    __tablename__ = "work_queue_items"
    __table_args__ = (
        UniqueConstraint(
            "work_queue_id", "ticket_id", name="uq_work_queue_item"
        ),
        # Covers the atomic-claim predicate (services/work_queues.py).
        Index(
            "idx_wqi_claim",
            "work_queue_id",
            "item_status",
            "next_eligible_at",
        ),
        Index("idx_wqi_ticket", "ticket_id"),
        CheckConstraint(
            "item_status IN "
            "('pending', 'active', 'waiting', 'done', 'failed')",
            name="ck_work_queue_item_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # wqi_<hex>
    work_queue_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("work_queues.id", ondelete="CASCADE"),
        nullable=False,
    )
    ticket_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    # SEEN cursor (server-shown floor).
    last_seen_comment_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_comment_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    # ACKED cursor (durably reviewed) — advanced only by complete_work_queue_step.
    last_acked_comment_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_acked_comment_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    # Directive lease.
    open_directive_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    open_directive_run_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    last_agent_run_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    last_verdict: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Count of EMITTED directives / action attempts (runaway guard); passive
    # waits do NOT increment it.
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_eligible_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkQueueRun(Base):
    """Append-only per-wake audit for a work queue.

    tk_529a64620db846f5 (WQ-P1, design §3.3). One row per wake (per call to
    the run-step tool), INCLUDING poll-only / no-op wakes that service no
    item. Kept a SEPARATE table from AgentRun (Atlas R2): a wake may produce
    no AgentRun at all; when it does, the row links it via the nullable
    `agent_run_id`. `work_queue_item_id` is nullable (no-op wake). App-assigned
    PK ('wqr_<hex>'). This id IS the work_queue_run_id returned to the caller
    (the directive-lease handle in later phases).
    """

    __tablename__ = "work_queue_runs"
    __table_args__ = (
        Index("idx_wqr_queue_created", "work_queue_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # wqr_<hex>
    work_queue_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("work_queues.id", ondelete="CASCADE"),
        nullable=False,
    )
    work_queue_item_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("work_queue_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    agent_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    directive_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RetrievalAuditContext(Base):
    """Server-side audit context for MCP retrievals that shaped a run."""

    __tablename__ = "retrieval_audit_contexts"
    __table_args__ = (
        Index("idx_retrieval_ctx_project", "project_id"),
        Index("idx_retrieval_ctx_ticket", "ticket_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    ticket_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    persona_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    lease_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RetrievalAuditEvent(Base):
    """Append-only server-side record of one context-shaping retrieval."""

    __tablename__ = "retrieval_audit_events"
    __table_args__ = (
        Index("idx_retrieval_event_context", "context_id", "created_at"),
        Index("idx_retrieval_event_session", "session_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    context_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("retrieval_audit_contexts.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    arguments: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    returned_refs: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    source: Mapped[str] = mapped_column(String(20), nullable=False, server_default="mcp")
    caller_user_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # v0.10.10 — service-key provenance (Codex R1 HIGH 2). source
    # already distinguishes mcp/cli/local; actor_type distinguishes
    # user-key vs service-key calls within those.
    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user", server_default="user"
    )
    service_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    service_key_name: Mapped[str | None] = mapped_column(String(100), nullable=True)


# v0.11.0 — Multi-repo projects (§3.1).
#
# A project owns N repos via the project_repos join table. Each repo belongs
# to exactly ONE project (global UNIQUE on git_remote_normalized). This
# breaks the pre-v0.11 1:1 repo↔project hard constraint.
#
# Ownership verification (Sentinel F1): verified + verification_method
# implement an anti-hijack model. github_app installation proof is the
# authoritative path (verified=true). owner_attested is the fallback for
# non-GitHub/self-hosted/app-not-installed (verified=false). legacy_backfill
# grandfathered existing rows at migration 049 (verified=false).
# Verified claims can displace unverified holders atomically (§6.2).


class ProjectRepo(Base):
    __tablename__ = "project_repos"
    __table_args__ = (
        UniqueConstraint("git_remote_normalized", name="uq_project_repos_remote"),
        Index("idx_project_repos_project", "project_id"),
        # Partial unique: at most one primary repo per project. Declared
        # here so Base.metadata.create_all (used by the test engines)
        # matches migration 049's DB-level index — without it the demote-
        # before-promote ordering in link_repo / merge._step_repos was
        # only exercised against an unconstrained table and a production
        # flush-order regression could slip through (tk_b3fc4a81446544ff).
        Index(
            "uq_project_repos_primary",
            "project_id",
            unique=True,
            postgresql_where=text("is_primary IS TRUE"),
            sqlite_where=text("is_primary IS TRUE"),
        ),
        # Partial unique: one project per provider+repo_id (rename
        # survival). Mirrors migration 049. Only fires when
        # provider_repo_id IS NOT NULL.
        Index(
            "uq_project_repos_provider_repo",
            "provider",
            "provider_repo_id",
            unique=True,
            postgresql_where=text("provider_repo_id IS NOT NULL"),
            sqlite_where=text("provider_repo_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    git_remote_normalized: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    # Provider identity for rename survival (v1). Best-effort only —
    # frequently NULL. The load-bearing anti-hijack control is
    # verified + verification_method (Sentinel F1/F2).
    provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    provider_repo_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    # Sentinel F1: verified=true ONLY for github_app (Sentinel S2 MED-3).
    # owner_attested and legacy_backfill are ALWAYS verified=false.
    verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    verification_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    added_by_user_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# v0.11.0 — Merge audit trail (§5.10).
#
# Durable record of every validated merge-EXECUTE attempt (dry-run writes
# nothing). The audit row is written in a SEPARATE transaction BEFORE
# mutation begins (status='started'), then outcome-updated to 'completed'
# or 'failed' via a fresh session that survives rollback of the merge
# transaction. Precondition/authz rejections (404, cross-org, already-merged)
# are refused BEFORE any audit row exists and are covered by standard
# request/access logging.
#
# House convention: Text columns with NOT NULL DEFAULT '{}'/'[]' for JSON
# payloads, matching agent_runs.findings + ticket_edits.old_value/new_value.


class ProjectMergeAudit(Base):
    __tablename__ = "project_merge_audit"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    initiated_by_user_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    dry_run: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default="completed", server_default=text("'completed'"),
        nullable=False
    )
    persona_policy: Mapped[str] = mapped_column(
        String(20), nullable=False
    )
    # JSON payloads as Text (cross-DB; matches agent_runs.findings pattern).
    stats: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'"), nullable=False
    )
    persona_renames: Mapped[str] = mapped_column(
        Text, default="[]", server_default=text("'[]'"), nullable=False
    )
    slug_renames: Mapped[str] = mapped_column(
        Text, default="[]", server_default=text("'[]'"), nullable=False
    )
    skipped_ke_ids: Mapped[str] = mapped_column(
        Text, default="[]", server_default=text("'[]'"), nullable=False
    )
    skipped_link_ids: Mapped[str] = mapped_column(
        Text, default="[]", server_default=text("'[]'"), nullable=False
    )
    rules_action: Mapped[str | None] = mapped_column(String(20), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ────────────────────────────────────────────────────────────────
# v0.11.0 P1 — Licensing Redesign: entitlements data-model foundation
# ────────────────────────────────────────────────────────────────


class Entitlement(Base):
    """Single source of truth for tier, seats, storage, and expiry.

    Unifies Stripe subscriptions, Helm licenses, and admin-provisioned
    orgs into one table.  At most one active entitlement per owner
    (enforced by partial unique index).  The old ``User.tier`` /
    ``Organization.tier`` / ``HelmLicense.tier`` columns remain as
    denormalized caches — the authoritative tier is resolved from
    this table.  See docs/design/licensing-org-redesign.md §2.1.
    """

    __tablename__ = "entitlements"
    __table_args__ = (
        # Partial unique index — at most one active entitlement per owner.
        # Runtime resolution hits exactly one row; the ORDER BY
        # current_period_end DESC NULLS FIRST tiebreak is for
        # historical rows / defensive fallback only.
        Index(
            "uq_entitlements_one_active_per_owner",
            "owner_type",
            "owner_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        # Unique external binding — each Stripe subscription or Helm
        # license maps to exactly one entitlement row.
        Index(
            "uq_entitlements_source_ref",
            "source",
            "source_ref",
            unique=True,
            postgresql_where=text("source_ref IS NOT NULL"),
            sqlite_where=text("source_ref IS NOT NULL"),
        ),
        # CHECK: 'admin' is NOT a valid entitlement tier (Sentinel MEDIUM-3).
        CheckConstraint(
            "tier IN ('free', 'starter', 'pro', 'team', 'enterprise')",
            name="ck_entitlements_tier",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_type: Mapped[str] = mapped_column(
        String(10), nullable=False, comment="'user' | 'org'"
    )
    owner_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="users.id | organizations.id"
    )
    source: Mapped[str] = mapped_column(
        String(20), nullable=False,
        comment="'stripe' | 'helm_license' | 'manual' | 'admin_provisioned'"
    )
    source_ref: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="stripe_subscription_id | helm_licenses.id | NULL (manual)"
    )
    tier: Mapped[str] = mapped_column(
        String(20), nullable=False, default="free", server_default="free"
    )
    seats_limit: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="NULL = unlimited/default"
    )
    storage_limit_bytes: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="NULL = tier default"
    )
    # Lifecycle status: active → canceled|expired|revoked.
    # Terminal statuses are never reactivated — a new row is inserted instead.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active",
        comment="active | canceled | expired | revoked"
    )
    # Separate billing-health flag — does NOT change lifecycle status.
    # An entitlement with status='active' + billing_status='past_due'
    # resolves normally for tier/feature gating (R3 amendment).
    billing_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="current", server_default="current",
        comment="current | past_due"
    )
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="Stripe=renewal date, HelmLicense=expiry date, NULL=perpetual"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OrgAuditEvent(Base):
    """Append-only audit trail for org-level mutations.

    Modeled on ProjectMergeAudit (models.py).  No UPDATE or DELETE
    routes exist — all writes are INSERTs via a shared helper.
    org_id ON DELETE SET NULL so audit rows survive org deletion.
    See design §2.7.
    """

    __tablename__ = "org_audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    org_name_snapshot: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    event_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )
    actor_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_email_snapshot: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    actor_role_at_time: Mapped[str | None] = mapped_column(
        String(20), nullable=True,
        comment="OrgMember.role at event time, or 'platform_admin'"
    )
    target_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True,
        comment="'user' | 'license' | 'entitlement' | 'invite' | 'settings' | 'organization'"
    )
    target_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    target_email_snapshot: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    # JSON payloads as Text (cross-DB; matches ProjectMergeAudit pattern).
    before: Mapped[str | None] = mapped_column(Text, nullable=True)
    after: Mapped[str | None] = mapped_column(Text, nullable=True)
    entitlement_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("entitlements.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ActivationAttempt(Base):
    """Durable single-use token store for license activation.

    Raw token is NEVER stored — only token_hash.  The activation_attempt
    row is committed BEFORE the email leaves the server, so the token
    survives email failure.  See design §2.2 Phase A/B.
    """

    __tablename__ = "activation_attempt"
    __table_args__ = (
        # Composite index for Phase B verify lookup:
        # find pending token by hash, check expiry, consume with rowcount-1 guard.
        Index(
            "idx_activation_attempt_lookup",
            "token_hash",
            "status",
            "expires_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    helm_license_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("helm_licenses.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="Hash of single-use token — raw token NEVER stored"
    )
    contact_email_snapshot: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    requested_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending",
        comment="pending | verified | consumed | expired"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PendingLicenseClaim(Base):
    """Lightweight migration-era table for unmatched HelmLicenses.

    Created during the migration 050 backfill for active HelmLicenses
    that have no matching Organization.  New licenses post-migration
    go through the standard activation path (ActivationAttempt).
    See design §3.1 step 5c.
    """

    __tablename__ = "pending_license_claim"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    helm_license_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("helm_licenses.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    org_name: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    contact_email: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    tier: Mapped[str] = mapped_column(
        String(20), nullable=False
    )
    seats_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ── SSO-P1 models (tk_c2cdbe7114804403, docs/design/sso-oidc.md §2) ──


class OrgIdentityProvider(Base):
    """Per-org OIDC/SAML identity provider configuration.

    Protocol-tagged so SAML rows (v2) can coexist without a schema change.
    At most one enabled IdP per org — enforced by the partial-unique index
    uq_org_idp_one_enabled_per_org (same shape as
    uq_entitlements_one_active_per_owner). `client_secret_ref` is a GCP
    Secret Manager resource name (or K8s secret URI on self-hosted); the
    raw secret is NEVER persisted to the DB.
    """

    __tablename__ = "org_identity_providers"
    __table_args__ = (
        Index("idx_org_idp_org", "org_id"),
        Index(
            "uq_org_idp_one_enabled_per_org",
            "org_id",
            unique=True,
            postgresql_where=text("enabled = true"),
            sqlite_where=text("enabled = true"),
        ),
        CheckConstraint(
            "protocol IN ('oidc', 'saml')",
            name="ck_org_idp_protocol",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    protocol: Mapped[str] = mapped_column(
        String(20), nullable=False, default="oidc", server_default="oidc"
    )
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    client_secret_ref: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="GCP Secret Manager / K8s secret ref — NEVER the plaintext secret",
    )
    allowed_scopes: Mapped[str] = mapped_column(
        Text, nullable=False, default='["openid","email","profile"]',
        server_default='["openid","email","profile"]',
    )
    discovery_cache: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovery_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    jwks_cache: Mapped[str | None] = mapped_column(Text, nullable=True)
    jwks_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    enforced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OrgDomainVerification(Base):
    """Proof that an org owns an email domain.

    JIT provisioning and enforcement key off verified domains only. A
    verified domain is claimable by AT MOST ONE org — the partial-unique
    index uq_org_domain_global_verified enforces this (anti-cross-tenant-
    hijack control). v1 proof mechanism is DNS TXT.
    """

    __tablename__ = "org_domain_verifications"
    __table_args__ = (
        Index("idx_org_domain_verification_org", "org_id"),
        Index(
            "uq_org_domain_global_verified",
            "domain",
            unique=True,
            postgresql_where=text("status = 'verified'"),
            sqlite_where=text("status = 'verified'"),
        ),
        CheckConstraint(
            "status IN ('pending', 'verified', 'failed')",
            name="ck_org_domain_verification_status",
        ),
        CheckConstraint(
            "method IN ('dns_txt', 'meta_tag')",
            name="ck_org_domain_verification_method",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    domain: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Normalized lowercase, e.g. acme.com",
    )
    method: Mapped[str] = mapped_column(
        String(20), nullable=False, default="dns_txt", server_default="dns_txt"
    )
    verification_token: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified_by_user_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExternalIdentity(Base):
    """Links an IdP subject to a SessionFS User.

    The identity key is (provider_issuer, subject) — NOT email. Email is
    mutable at the IdP and must never be the join key for an existing
    link. A single User may hold multiple ExternalIdentity rows (e.g. a
    consultant in two customer orgs' IdPs).
    """

    __tablename__ = "external_identities"
    __table_args__ = (
        Index("idx_external_identity_user", "user_id"),
        Index("idx_external_identity_org_idp", "org_idp_id"),
        UniqueConstraint(
            "provider_issuer", "subject",
            name="uq_external_identity_issuer_sub",
        ),
        CheckConstraint(
            "link_method IN "
            "('verified_email_match', 'jit_provision', 'explicit_confirm')",
            name="ck_external_identity_link_method",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_idp_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("org_identity_providers.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_issuer: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Snapshotted issuer — survives IdP config edits",
    )
    subject: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="OIDC sub claim — stable, opaque, IdP-assigned",
    )
    email_at_link: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Email asserted by IdP at link time (audit)",
    )
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    link_method: Mapped[str] = mapped_column(
        String(30), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OidcLoginAttempt(Base):
    """Durable single-use state token for OIDC authorization-code + PKCE.

    Mirrors the ActivationAttempt proven shape: row committed BEFORE the
    external redirect, token hash stored (raw verifier NEVER persisted),
    consumed with an atomic UPDATE WHERE status='pending' rowcount-1 guard
    for CSRF/replay defense. Short TTL (10 min).
    """

    __tablename__ = "oidc_login_attempts"
    __table_args__ = (
        Index(
            "idx_oidc_login_attempt_state",
            "state", "status", "expires_at",
        ),
        CheckConstraint(
            "status IN ('pending', 'consumed', 'expired')",
            name="ck_oidc_login_attempt_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_idp_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("org_identity_providers.id", ondelete="CASCADE"),
        nullable=True,
        comment="Nullable — resolved at start time; CASCADE so deleting an "
                "IdP invalidates in-flight attempts",
    )
    state: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True,
        comment="Random; returned in callback, matched exactly",
    )
    nonce: Mapped[str] = mapped_column(
        String(128), nullable=False,
        comment="Echoed in id_token nonce claim, matched (replay defense)",
    )
    pkce_verifier_hash: Mapped[str] = mapped_column(
        String(128), nullable=False,
        comment="Hash of the PKCE code verifier; raw verifier NEVER stored",
    )
    org_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    redirect_after: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        comment="Short TTL (10 min) — mirror of ActivationAttempt.expires_at",
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

"""SQLAlchemy 2.0 ORM models."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, Float, Index, Integer, SmallInteger, String, Text, ForeignKey, UniqueConstraint, func, text
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
    # A Stripe subscription created but not yet paid ('incomplete'): it
    # routes later events and blocks a second checkout, but grants nothing
    # and never revokes anything (migration 075).
    stripe_pending_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    # v0.15 P1 — daemon uploads per-watcher capture health on each
    # sync-settings poll (~60s).  JSON-encoded list of watcher status
    # objects: [{name, health, degraded_since, last_error, ...}].
    # Nullable — users who haven't run the daemon since this field was
    # added stay NULL.  The dashboard renders a degraded badge only when
    # at least one watcher reports health='degraded'.
    capture_health: Mapped[str | None] = mapped_column(
        Text, nullable=True,
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


class SessionUpload(Base):
    """A chunked, resumable session upload (GitHub #91).

    The single-shot ``PUT /sessions/{id}/sync`` route has to receive the whole
    archive in one request; large transcripts run into proxy and read-timeout
    ceilings. A chunked upload instead: (1) initiates an upload row, (2) PUTs
    each part into blob storage, (3) completes, which assembles the parts and
    runs them through the exact same validation/quote/DLP/project-routing path
    as the single-shot push.

    Parts live in the ``session_upload_parts`` child table and under blob key
    ``uploads/{upload_id}/part-{n:05d}``. If-Match is NOT stored here: complete
    takes it from the complete request so a retried complete never replays the
    (possibly stale) ETag captured at initiate. After a successful complete the
    push result is stored on the row (``result_etag``, ``result_size``,
    ``result_synced_at``, ``result_created``) so a retried complete returns the
    same 200/201 without re-pushing.

    The ``completion_token`` fences a slow, live complete against the stale-
    completing reclaim: the complete that claims the row writes a fresh random
    token, and finalize/revert only apply when that token is still on the row.
    A reclaim that has since reverted (or re-claimed) the row clears the token,
    so the original complete's finalize matches zero rows and never overwrites
    the newer claimant's stored result.
    """

    __tablename__ = "session_uploads"
    __table_args__ = (
        # The "max 3 pending per user" gate and the owner-scoped reads.
        Index("idx_session_uploads_user_status", "user_id", "status"),
        # The retention sweeper aborts expired pending uploads.
        Index("idx_session_uploads_status_expires", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    total_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    part_size: Mapped[int] = mapped_column(Integer, nullable=False)
    part_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    completing_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completion_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_etag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    result_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result_created: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SessionUploadPart(Base):
    """One received part of a chunked session upload."""

    __tablename__ = "session_upload_parts"

    upload_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("session_uploads.id", ondelete="CASCADE"),
        primary_key=True,
    )
    part_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)


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
        # GH #70 — pending-handoff attribution by durable project anchor
        # (stranding guards, merge rewrite, claim resolution). Name must
        # match migration 072's create_index.
        Index("idx_handoffs_source_project_id", "source_project_id"),
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
    # GH #70 — durable project anchor for the handoff's provenance
    # (ticket/persona). Set at create time from the source session's
    # project (session.project_id, else git-remote resolution) and
    # backfilled by migration 072, so the stranding guards and the merge
    # persona-reference rewrite can attribute PENDING handoffs to a
    # project without re-resolving the session's remote — including
    # legacy sessions whose project_id is NULL. ON DELETE SET NULL
    # mirrors sessions.project_id.
    source_project_id: Mapped[str | None] = mapped_column(
        String(64),
        # Name must match migration 072's create_foreign_key — the
        # downgrade drops it by name and the migration tests build the
        # pre/post schema from this metadata.
        ForeignKey(
            "projects.id",
            ondelete="SET NULL",
            name="fk_handoffs_source_project_id",
        ),
        nullable=True,
    )
    # v0.15.0 — sha256-at-rest token for the P1 pre-signup landing page.
    # Raw token goes ONLY into the recipient email link (generated at
    # handoff creation); the hash gates the unauthenticated /preview
    # endpoint. Nullable — existing rows constant-404.
    preview_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_snapshot: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Precomputed bounded preview JSON (M2 — the unauthenticated "
                "preview endpoint never touches the blob store)",
    )


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
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OrgLlmSettings(Base):
    """Org-wide LLM key for the session helper (migration 084).

    One row per org (``org_id`` is the primary key). An org admin sets a
    provider / model / encrypted key that any active member can use when
    they have no personal judge settings. The key is Fernet-encrypted with
    the same derivation as ``UserJudgeSettings`` (``server/llm_keys.py``)
    and is never returned by the API — ``has_api_key`` plus a last-4 hint
    stand in for it.
    """

    __tablename__ = "org_llm_settings"

    org_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    updated_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AdminAction(Base):
    """Platform-admin audit log (migration 007; operator context 081, GH #84).

    ``source`` is 'api' when the server performed the action and wrote the
    row, 'operator_record' when an operator recorded an action they took
    outside the API (raw SQL in a pinch) — the server did not verify those.
    ``before_state`` / ``after_state`` are JSON text, like ``details``.
    """

    __tablename__ = "admin_actions"
    __table_args__ = (
        Index("idx_admin_actions_created_at", "created_at"),
        Index("idx_admin_actions_target", "target_type", "target_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    admin_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="api", server_default="api"
    )
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
    # v0.14.0: DLP gate for public share pages (migration 060).
    # NULL = not yet checked; True = scanned, clean; False = scanned, blocked.
    dlp_checked: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    dlp_checked_etag: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="session.etag the DLP verdict was computed against (H2 — "
                "blob re-push invalidates the cached verdict)",
    )
    dlp_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GitHubInstallation(Base):
    __tablename__ = "github_installations"
    __table_args__ = (
        # One linked installation per user (migration 073).
        Index(
            "uq_github_installations_user",
            "user_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
            sqlite_where=text("user_id IS NOT NULL"),
        ),
    )

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
    # GitHub numeric id of the installation's account (user or org). Logins
    # can be renamed; the id cannot.
    account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # GitHub user id of the `sender` on the signed installation.created
    # webhook, i.e. who installed the App. Recorded only when webhook
    # signatures are verified. For an organization installation the installer
    # is an org owner (or app manager) at install time; used as the admin
    # signal when the App lacks the Members permission.
    installer_github_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Ownership proof (migration 073). Set ONLY by the OAuth claim callback
    # after GitHub's GET /user/installations listed this installation for the
    # claiming GitHub user. NULL = a legacy claim made through the old
    # unauthenticated installation_id path; such claims are never trusted for
    # repo verification or publisher verification until re-verified.
    claim_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_verified_github_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    claim_verified_github_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 'account_owner' (user installation and the claimant IS that account),
    # 'org_admin' (claimant is an active admin of the installation's org), or
    # 'member' (GitHub lists the installation for the claimant, but admin
    # rights could not be confirmed).
    claim_role: Mapped[str | None] = mapped_column(String(20), nullable=True)


class GitHubClaimAttempt(Base):
    """Single-use state for the GitHub App OAuth claim flow (migration 073).

    Committed before the redirect to GitHub and consumed with an atomic
    UPDATE ... WHERE status='pending' (rowcount-1). Only hashes are stored:
    the raw state, browser nonce and PKCE verifier live in the browser
    (state in the URL, nonce + verifier in an HttpOnly cookie).

    Lifecycle: pending -> processing (consumed by a callback; the callback
    may still be talking to GitHub, so cleanup keeps processing rows for a
    grace period) -> choosing (multi-installation chooser parked) ->
    consumed (terminal; chooser closed or flow otherwise finished).
    """

    __tablename__ = "github_claim_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'consumed', 'choosing')",
            name="ck_github_claim_attempt_status",
        ),
        CheckConstraint(
            "flow IN ('install', 'authorize')",
            name="ck_github_claim_attempt_flow",
        ),
        Index("idx_github_claim_attempt_user", "user_id"),
        Index("idx_github_claim_attempt_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    nonce_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pkce_verifier_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    flow: Mapped[str] = mapped_column(String(20), nullable=False)
    target_installation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # status='choosing': GitHub proved the user can link several
    # installations; this holds the proven GitHub user and the eligible
    # installations (with their live roles) until the user picks one or
    # expires_at passes. Never holds a GitHub token.
    choices_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class GitHubWebhookDelivery(Base):
    """One row per distinct signed GitHub webhook payload (migration 073).

    Retention: the stored payload is dropped after 30 days, but the row
    itself (a compact fingerprint tombstone) is kept for 400 days so a
    captured, validly signed body cannot be replayed once its payload is
    gone. Rotating the webhook secret invalidates every older signature.

    Keyed by sha256(event header + raw body). The body is what the HMAC
    signature authenticates, so an attacker cannot mint a fresh key by
    changing unsigned headers (X-GitHub-Delivery is kept for logs only). A
    legitimate GitHub redelivery carries the same body and maps to the same
    row. Lifecycle: processing -> completed | failed. Only completed rows
    are permanent no-ops; failed rows, and processing rows whose worker went
    stale, are retried by a redelivery. Pruned after the retention window.
    """

    __tablename__ = "github_webhook_deliveries"
    __table_args__ = (
        Index("idx_github_webhook_delivery_received", "received_at"),
        CheckConstraint(
            "status IN ('processing', 'completed', 'failed')",
            name="ck_github_webhook_delivery_status",
        ),
    )

    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    delivery_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="processing")
    # Retry counter (backoff, attempt limit); an admin may reset it.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Fencing token: increases on EVERY claim/takeover and is never reset,
    # so a stale worker can never record an outcome for a newer attempt.
    lease_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Earliest automatic retry of a failed delivery (backoff).
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The verified raw body, kept so failed work can be retried without
    # GitHub; cleared on completion and purged with the row after 30 days.
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)


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
    # Nullable (migration 073): a repo-reclaimed project is represented by
    # NULL plus repo_reclaimed_at, NOT by a synthetic "reclaimed:<id>"
    # remote string — a user-reachable placeholder made the namespace
    # attacker-claimable. NULL never matches a remote equality lookup, so
    # reclaimed projects are unreachable by any remote. Multiple NULLs are
    # allowed by the unique constraint on both PostgreSQL and SQLite.
    git_remote_normalized: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True, index=True
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
    # Scheduled KB compile (GitHub #93). compile_schedule is 'off', 'nightly'
    # or 'weekly'; compile_after_claims compiles once that many claims are
    # pending regardless of schedule. last_scheduled_compile_at is set by the
    # sweep only (never by a manual compile). The server default is 'off' so
    # existing rows (and any raw insert that omits the column) opt out of the
    # sweep; new projects are created with an explicit 'nightly' (the Python
    # default below) at the single creation path in routes/projects.py.
    compile_schedule: Mapped[str] = mapped_column(
        String(16), default="nightly", server_default="off"
    )
    compile_after_claims: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_scheduled_compile_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When confidence decay last ran for this project. Decay is time-based —
    # at most one 0.8 factor per compile pass, once per elapsed 30-day period
    # since this timestamp — rather than applied every compile, so a long gap
    # never retroactively decays long-lived claims in a single pass.
    last_decay_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    __table_args__ = (
        # Created by migration 026; declared here so create_all-built schemas
        # (tests, fresh installs) enforce it too. Webhook PR-comment creation
        # relies on it to serialize per (installation, repo, PR).
        Index(
            "idx_pr_comments_installation_repo_pr",
            "installation_id", "repo_full_name", "pr_number",
            unique=True,
        ),
    )

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
    # Latest desired comment content (migration 073). Every event writes it
    # and bumps body_version; whoever creates/edits the comment publishes
    # the newest version, so an event arriving mid-creation is not lost.
    desired_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    published_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Publishing lease: one publisher at a time edits the comment, without
    # holding a database lock across the GitHub call (claim, commit, call,
    # conditionally finalize). Expired leases can be taken over.
    publish_lease: Mapped[str | None] = mapped_column(String(64), nullable=True)
    publish_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Installation owner desired_body was built for (migration 073). A
    # publisher refuses to post it once the installation has a new owner.
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    tier: Mapped[str] = mapped_column(String(20), nullable=False, server_default="team")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # See User.stripe_pending_subscription_id (migration 075).
    stripe_pending_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
        # v0.12.2 SSO-P1-fix — one membership per (org, user).
        UniqueConstraint(
            "org_id", "user_id",
            name="uq_org_members_org_user",
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
    # GH #97 — per-member opt-out for the weekly org activity digest.
    # ON by default; owners/admins can disable it for themselves via
    # PUT /api/v1/orgs/{org_id}/members/me/digest.
    digest_opt_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class OrgDigestRun(Base):
    """Idempotency ledger for the weekly org activity digest (GH #97).

    One row per (org, week_start) once a digest has been sent for that
    week. The sweep inserts the row BEFORE sending so a concurrent
    re-run (or a crash mid-send) cannot double-send; ``week_start`` is
    the Monday 00:00 UTC that opens the 7-day window.
    """

    __tablename__ = "org_digest_runs"

    org_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    week_start: Mapped[date] = mapped_column(Date, primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recipient_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Number of recipients actually delivered to. Updated AFTER sending so
    # a fully-failed send records zero and the sweep releases the (org, week)
    # reservation for a later retry (see routes/admin.py).
    sent_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
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

    State machine: ``pending`` → ``accepted`` | ``rejected`` | ``cancelled``
    (and ``expired`` for admin-proposed transfers past their consent window,
    GH #96). Rows are NEVER deleted post-resolution — the audit trail is the
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
        # Admin-proposed transfers (GH #96): the proposer is also
        # `initiated_by`, but this index keeps "which proposals did this
        # admin make" lookups cheap when `initiated_by` is not the
        # proposal flag (e.g. after a future initiator/on-behalf split).
        Index("idx_project_transfers_proposed_by", "proposed_by_user_id"),
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
    # GH #96 — admin-proposed transfers. `proposed_by_user_id` marks the
    # proposal shape (non-NULL = the destination-org admin who proposed the
    # move); `proposal_reason` is the justification shown to the owner and
    # quoted in decline/expiry emails; `expires_at` bounds the consent
    # window (lazily flipped pending → expired, like owner transfers).
    proposed_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
            name="fk_project_transfers_proposed_by_user_id",
        ),
        nullable=True,
    )
    proposal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
    # 'processed' — the handler applied (or deliberately ignored) the event.
    # 'dead_letter' — the event is permanently unusable; a matching
    # billing_reconciliations row carries the reason (migration 075).
    outcome: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="processed"
    )


class BillingCheckoutIntent(Base):
    """A Stripe Checkout session started for a billing owner (migration 075).

    At most one open intent (pending or unresolved) per billing owner and per
    Stripe customer: a second checkout request while one is live reuses it
    (same plan) or is refused, so concurrent clicks can't open two
    subscriptions. 'unresolved' marks a checkout Stripe completed that we
    could not apply (dead letter, duplicate, or a session that completed
    without its webhook); it blocks new checkouts until ops resolves it. The Stripe session is created with
    an idempotency key derived from the owner, scope, plan and a short time
    window, and expires with the intent.
    """

    __tablename__ = "billing_checkout_intents"
    __table_args__ = (
        Index(
            "uq_billing_checkout_intents_one_pending",
            "owner_type",
            "owner_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'unresolved')"),
            sqlite_where=text("status IN ('pending', 'unresolved')"),
        ),
        Index(
            "uq_billing_checkout_intents_one_pending_per_customer",
            "stripe_customer_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending', 'unresolved') AND stripe_customer_id IS NOT NULL"
            ),
            sqlite_where=text(
                "status IN ('pending', 'unresolved') AND stripe_customer_id IS NOT NULL"
            ),
        ),
        Index("idx_billing_checkout_intents_session", "stripe_session_id"),
        CheckConstraint(
            "status IN ('pending', 'completed', 'expired', 'unresolved')",
            name="ck_billing_checkout_intents_status",
        ),
        CheckConstraint(
            "owner_type IN ('user', 'org')",
            name="ck_billing_checkout_intents_owner_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_type: Mapped[str] = mapped_column(String(10), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    tier: Mapped[str] = mapped_column(String(20), nullable=False)
    seats: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stripe_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checkout_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class StripeFetchGeneration(Base):
    """Monotonic, DB-issued ordering for Stripe fetches (migration 075).

    A row is inserted (and committed) BEFORE every Stripe retrieve/list, and
    its id is that fetch's generation. Generations order fetches by when they
    STARTED, using the database as the clock — webhooks fetch outside any
    transaction, so comparing wall-clock timestamps across machines cannot
    order them. A fetch with a lower generation than the state already
    applied (or than a terminal observation) is stale and is not applied.
    """

    __tablename__ = "stripe_fetch_generations"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StripeSubscriptionObservation(Base):
    """Latest canonical Stripe state seen for a subscription (migration 075).

    Keyed by subscription id, independent of who (if anyone) owns its
    entitlement, so an observation recorded while the subscription was not
    yet routable — e.g. a deletion delivered before its checkout — still
    governs later events. Written under the Stripe customer lock whenever a
    webhook applies or records a fetched state; a grant whose fetch
    generation is older than the latest terminal observation's generation is
    refused.
    """

    __tablename__ = "stripe_subscription_observations"

    subscription_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # DB-issued fetch generations (stripe_fetch_generations.id) backing
    # observed_at / terminal_at — the authoritative ordering, not the
    # wall-clock timestamps they sit beside.
    generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    terminal_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BillingReconciliation(Base):
    """Durable record of a billing event ops must resolve (migration 075).

    Written instead of guessing: a duplicate subscription for an owner that
    already has one (never auto-canceled, never rebound), a subscription
    event that doesn't match the owner's current subscription, an event no
    owner claims, or a permanently malformed event (dead letter).
    """

    __tablename__ = "billing_reconciliations"
    __table_args__ = (
        Index("idx_billing_reconciliations_status", "status", "created_at"),
        Index("idx_billing_reconciliations_event", "stripe_event_id"),
        Index("idx_billing_reconciliations_subscription", "stripe_subscription_id"),
        CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_billing_reconciliations_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment="duplicate_subscription | stale_subscription_event | "
        "unroutable_subscription_event | malformed_event | plan_owner_mismatch | "
        "subscription_binding_conflict | ambiguous_subscription_pointer",
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="open"
    )
    stripe_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stripe_event_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
    # 'manual' (a user clicked Compile) or 'scheduled' (the KB compile sweep).
    trigger: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default="manual"
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
    event: Mapped[str | None] = mapped_column(String(40), nullable=True)
    event_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tool: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class KnowledgePage(Base):
    __tablename__ = "knowledge_pages"
    __table_args__ = (
        Index("idx_kp_project", "project_id"),
        # Created by migration 019 but never declared here, so create_all
        # schemas (tests, fresh SQLite) lacked it. GH #70: declared so every
        # schema enforces one page per (project_id, slug).
        UniqueConstraint("project_id", "slug", name="uq_kp_project_slug"),
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
    """A portable AI role scoped to one project OR to one organization.

    Exactly one of `project_id` / `org_id` is set (ck_agent_personas_scope,
    migration 072). Project personas: the same name is unique within a
    project (uq_persona_project_name; case-insensitive uniqueness is
    enforced at create time). Org personas: case-insensitive unique per
    org (uq_persona_org_lower_name) and usable from every project in the
    org; a project persona with the same name overrides the org one (see
    services/personas.resolve_persona). `content` is opaque markdown
    injected into the context window as-is. `specializations` is a JSON
    array of domain keywords used for routing suggestions in a future
    version — v0.10.1 does NOT auto-filter the KB by them.
    Soft-delete via `is_active = false`.
    """

    __tablename__ = "agent_personas"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_persona_project_name"),
        Index("idx_persona_project_active", "project_id", "is_active"),
        Index("idx_persona_org_active", "org_id", "is_active"),
        Index(
            "uq_persona_org_lower_name",
            "org_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("org_id IS NOT NULL"),
            sqlite_where=text("org_id IS NOT NULL"),
        ),
        CheckConstraint(
            "(project_id IS NOT NULL AND org_id IS NULL) "
            "OR (project_id IS NULL AND org_id IS NOT NULL)",
            name="ck_agent_personas_scope",
        ),
        CheckConstraint(
            "represented_user_id IS NULL OR org_id IS NOT NULL",
            name="ck_agent_personas_represented_user_org",
        ),
        Index("idx_persona_represented_user", "represented_user_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
    )
    # v0.17.x (GH #70) — org-level persona scope. NULL for project personas.
    org_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "organizations.id",
            ondelete="CASCADE",
            name="fk_agent_personas_org_id",
        ),
        nullable=True,
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
    # GitHub #81 (migration 080) — org personas only: the org member this
    # persona represents. They may update its role, content and
    # specializations without admin standing while they stay a member.
    represented_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_agent_personas_represented_user"),
        nullable=True,
    )
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


class TicketWatcher(Base):
    """A user following a ticket so they (and their agents) learn when it
    changes hands or state.

    One row per (ticket, user). ``source`` records how the row came to be:
    ``creator`` / ``assignee`` / ``commenter`` / ``reviewer`` are written
    automatically when the corresponding event fires; ``explicit`` is a
    user following via the watch API. ``muted`` suppresses notifications
    without dropping the follow. ``persona_name`` is the persona that
    caused an auto-subscribe (e.g. the assigned persona whose human owner
    this user is); it is informational, not a second key.
    """

    __tablename__ = "ticket_watchers"
    __table_args__ = (
        Index("idx_ticket_watchers_ticket", "ticket_id"),
        Index("idx_ticket_watchers_user", "user_id"),
    )

    ticket_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    persona_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="explicit", server_default="explicit"
    )
    muted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TicketNotification(Base):
    """A server-generated notification about a watched ticket.

    One row per event (or coalesced burst — see services/ticket_watchers).
    ``id`` is a monotonic BigInteger cursor used by the agent delta API.
    ``summary`` is written by the server and NEVER copies a comment body.
    ``actor_type`` is ``user`` or ``service_key``; ``actor_user_id`` is
    the human identity behind the action and ``service_key_name`` records
    the service-key credential when a resident acted. ``project_id`` is
    denormalized so every read/delivery can re-check tenant isolation
    without joining back through the ticket.
    """

    __tablename__ = "ticket_notifications"
    __table_args__ = (
        Index("idx_ticket_notifications_user_id", "user_id", "id"),
        Index("idx_ticket_notifications_user_read", "user_id", "read_at"),
        Index("idx_ticket_notifications_ticket", "ticket_id"),
        Index(
            "idx_ticket_notifications_sweep",
            "emailed_at",
            "read_at",
            "updated_at",
        ),
        Index(
            "idx_ticket_notifications_read_updated",
            "read_at",
            "updated_at",
        ),
        Index(
            "idx_ticket_notifications_emailed_updated",
            "emailed_at",
            "updated_at",
        ),
        # SQLite would otherwise reuse a deleted max id, and coalescing deletes
        # and re-inserts rows: the agent delta (id > cursor) needs ids that
        # never repeat.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    ticket_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user", server_default="user"
    )
    actor_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    service_key_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    event_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    emailed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TicketWatchCursor(Base):
    """Last-seen notification cursor for a (user, persona) pair.

    ``persona_name`` is the empty string for the human's own cursor and a
    persona name for an agent polling on that persona's behalf, so the
    human inbox and each resident keep independent read positions.
    """

    __tablename__ = "ticket_watch_cursors"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    persona_name: Mapped[str] = mapped_column(
        String(50), primary_key=True, server_default=""
    )
    last_seen_notification_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserNotificationSettings(Base):
    """Per-user notification preferences.

    ``ticket_email`` is ``off``, ``your_turn`` (default) or ``all`` and
    gates the ticket-notification email sweep.
    """

    __tablename__ = "user_notification_settings"

    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ticket_email: Mapped[str] = mapped_column(
        String(20), nullable=False, default="your_turn", server_default="your_turn"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Resident(Base):
    """Durable resident identity — reviewer or implementer (R0, migration 057).

    A resident is a persistent agent runtime bound to a single org, project,
    and service key. It drives a work queue (nullable — assigned after
    registration) and owns a private durable mind in resident_memory_entries.
    One service key may drive at most one resident (uq_resident_service_key).
    App-assigned PK ('res_<hex>').

    F4 SoD mutual-exclusion: a service_key_id bound to a TrustedReviewer row
    (for the same project/org) cannot also be an implementer resident, and
    vice-versa. Enforced at registration in routes/residents.py.
    """

    __tablename__ = "residents"
    __table_args__ = (
        Index("uq_resident_service_key", "service_key_id", unique=True),
        Index("idx_resident_org_project", "org_id", "project_id"),
        CheckConstraint(
            "kind IN ('reviewer', 'implementer')",
            name="ck_resident_kind",
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'retired')",
            name="ck_resident_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # res_<hex>
    org_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    persona_name: Mapped[str] = mapped_column(String(50), nullable=False)
    service_key_id: Mapped[str] = mapped_column(String(36), nullable=False)
    work_queue_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("work_queues.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    mind_token_budget: Mapped[int] = mapped_column(
        Integer, nullable=False, default=8000, server_default="8000"
    )
    max_uncompacted_entries: Mapped[int] = mapped_column(
        Integer, nullable=False, default=500, server_default="500"
    )
    created_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    service_key_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ResidentMemoryEntry(Base):
    """Append-only durable mind entry for a resident (R0, migration 057).

    Each entry is owned by one resident and carries a denormalized org_id for
    server-enforced C5 isolation (belt-and-suspenders over the resident FK).
    Entries are monotonic per resident (seq) and can be superseded by a
    compacting digest. App-assigned PK ('rme_<hex>').
    """

    __tablename__ = "resident_memory_entries"
    __table_args__ = (
        Index("idx_rme_resident_kind_seq", "resident_id", "kind", "seq"),
        Index("idx_rme_resident_created", "resident_id", "created_at"),
        Index("idx_rme_org", "org_id"),
        Index("uq_resident_memory_seq", "resident_id", "seq", unique=True),
        CheckConstraint(
            "kind IN ('reasoning', 'digest', 'observation')",
            name="ck_rme_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # rme_<hex>
    resident_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("residents.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_estimate: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    compacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    quarantined: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
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
        Index("idx_agent_run_replayed_from", "replayed_from"),
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

    # Replay provenance + edited prompt/context (GitHub #95, migration 085).
    # `replayed_from` is a plain string (no FK) so the audit row survives
    # even if the parent run is later hard-deleted, same as persona_name /
    # ticket_id / session_id. `context_refs` is JSON-encoded list[str] as
    # Text (cross-DB, matching `findings`).
    replayed_from: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_refs: Mapped[str | None] = mapped_column(Text, nullable=True)

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


class AgentFeedback(Base):
    """Structured outcome/feedback record on agent work (v0.17.0).

    Append-only. A record points at at least one target — an agent run,
    a ticket, or a ticket comment — all of which must belong to the
    same `project_id` (enforced at the route layer so a guessed
    cross-project id surfaces as 404, never as a leak). Targets are FKs
    with SET NULL so the feedback row survives target deletion while the
    audit triple + `final_owner`/`notes` retain the human-readable trail.
    "At least one target" is therefore enforced on create only, not by a
    DB CHECK (which would block deleting the last referenced target).

    `feedback_type` / `verdict` / `source` are enum-as-strings with
    inline CHECK constraints (mirroring migration 064). `label` is a
    fixed vocabulary validated per feedback_type at the route layer.
    """

    __tablename__ = "agent_feedback"
    __table_args__ = (
        CheckConstraint(
            "feedback_type IN ('route','summary','actionability',"
            "'duplicate','resolution')",
            name="ck_agent_feedback_type",
        ),
        CheckConstraint(
            "verdict IN ('positive','negative','neutral')",
            name="ck_agent_feedback_verdict",
        ),
        CheckConstraint(
            "source IN ('human','auto')",
            name="ck_agent_feedback_source",
        ),
        Index("idx_agent_feedback_project_created", "project_id", "created_at"),
        Index("idx_agent_feedback_run", "agent_run_id"),
        Index("idx_agent_feedback_ticket", "ticket_id"),
        Index("idx_agent_feedback_comment", "comment_id"),
        Index(
            "uq_agent_feedback_auto_route",
            "ticket_id",
            "final_owner",
            unique=True,
            sqlite_where=text("source = 'auto' AND label = 'wrong_route'"),
            postgresql_where=text("source = 'auto' AND label = 'wrong_route'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # afb_<hex>
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Targets — at least one required (see CHECK above).
    agent_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    ticket_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tickets.id", ondelete="SET NULL"),
        nullable=True,
    )
    comment_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("ticket_comments.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Classification.
    feedback_type: Mapped[str] = mapped_column(String(20), nullable=False)
    verdict: Mapped[str] = mapped_column(String(10), nullable=False)
    label: Mapped[str | None] = mapped_column(String(50), nullable=True)
    final_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(
        String(10), nullable=False, default="human", server_default="human"
    )

    # Audit triple (v0.10.10 service-key provenance pattern).
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user", server_default="user"
    )
    service_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    service_key_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


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
        CheckConstraint(
            "auto_close_review_kind IS NULL OR "
            "auto_close_review_kind IN ('resident_trusted', 'human')",
            name="ck_wqi_auto_close_review_kind",
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
    # F1 implementer-identity + auto-close provenance (R0, migration 057).
    # Recorded at implement-side claim/settle; compared at close to enforce
    # the self-review prohibition. auto_close_review_kind stamps the CEO
    # condition-(a) marker ("resident-reviewed, NOT human-reviewed").
    implementer_service_key_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    implementer_user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    closed_by_service_key_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    closed_by_user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    auto_close_review_kind: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
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
    # Nullable (090): a helper turn scoped to sessions without a single
    # project still writes audit events; API-created contexts always set it.
    project_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
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
    project_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
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
    # Fetch generation (stripe_fetch_generations.id) of the Stripe state last
    # applied to this row (migration 075). Webhooks fetch outside any
    # transaction, so a fetch with a lower generation is stale and is not
    # applied — the DB-issued generation orders fetches, not wall clocks.
    stripe_synced_generation: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
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


# v0.23 — Jira integration (migration 091).


class OrgTrackerConnection(Base):
    """An org's connection to an external tracker (Jira Cloud only in the MVP).

    One row per org (org_id is the PK). The API token is Fernet-encrypted at
    rest with the same derivation the judge settings use; it is never
    returned by any route. ``default_template`` is the template slug the org
    admin picks when connecting — the mapping UI defaults new mappings to it.
    """

    __tablename__ = "org_tracker_connections"

    org_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(
        String(20), nullable=False, default="jira", server_default="jira"
    )
    site_url: Mapped[str] = mapped_column(String(255), nullable=False)
    account_email: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_api_token: Mapped[str] = mapped_column(Text, nullable=False)
    default_template: Mapped[str] = mapped_column(
        String(50), nullable=False, default="agile", server_default="agile"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    connected_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TrackerProjectMapping(Base):
    """Maps a SessionFS project to a Jira project for ticket promotion.

    One mapping per SessionFS project (project_id unique). ``template`` NULL
    means "use the org's default template". ``issue_type`` defaults to
    "Story" but may name any issue type the Jira project exposes.
    """

    __tablename__ = "tracker_project_mappings"
    __table_args__ = (
        Index("idx_tracker_mapping_org", "org_id"),
        UniqueConstraint("project_id", name="uq_tracker_mapping_project"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    jira_project_key: Mapped[str] = mapped_column(String(50), nullable=False)
    issue_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="Story", server_default="Story"
    )
    template: Mapped[str | None] = mapped_column(String(50), nullable=True)
    done_status_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TicketTrackerLink(Base):
    """Strict 1:1 link from a SessionFS ticket to a promoted Jira issue.

    ``ticket_id`` is the PK and FK (CASCADE) — deleting a ticket removes its
    link, and the PK guarantees at most one link per ticket. ``org_id`` is a
    denormalized copy for org-scoped queries (the ticket's org is reachable
    through its project, but health checks scan links directly).

    ``status`` is ``pending`` while a publish is claiming the ticket (a second
    concurrent publish hits the unique ticket PK and gets 409
    ``publish_in_progress``) and flips to ``linked`` only after the remote
    issue exists. ``site_url``/``issue_id`` snapshot the connection at publish
    time; a link whose org/site no longer matches the current connection is
    stale and never updated.
    """

    __tablename__ = "ticket_tracker_links"
    __table_args__ = (
        Index("idx_ticket_tracker_link_org", "org_id"),
        Index("idx_ticket_tracker_link_status_checked", "last_status_checked_at"),
    )

    ticket_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tickets.id", ondelete="CASCADE"), primary_key=True
    )
    org_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(
        String(20), nullable=False, default="jira", server_default="jira"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="linked", server_default="linked"
    )
    # Fences the pending publish claim (see the migration 091 comment): set to a
    # fresh ``secrets.token_hex(16)`` on claim/reclaim and cleared once linked.
    claim_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    site_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issue_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    issue_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    issue_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_pushed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_pushed_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_known_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_status_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_published_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_push_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_push_failure: Mapped[str | None] = mapped_column(String(300), nullable=True)


# v0.16 — portable skills/plugins marketplace (migration 063).


class SkillPackage(Base):
    """Logical package identity; bytes live in immutable version rows."""

    __tablename__ = "skill_packages"
    __table_args__ = (
        UniqueConstraint(
            "owner_type",
            "owner_id",
            "namespace",
            "source_type",
            "source_locator",
            "source_subdir",
            "name",
            name="uq_skill_package_source_identity",
        ),
        Index("idx_skill_packages_org_name", "org_id", "name"),
        Index("idx_skill_packages_created_by", "created_by_user_id"),
        CheckConstraint(
            "owner_type IN ('system', 'org', 'user')",
            name="ck_skill_package_owner_type",
        ),
        CheckConstraint(
            "(owner_type = 'system' AND org_id IS NULL) OR "
            "(owner_type = 'org' AND org_id IS NOT NULL AND owner_id = org_id) OR "
            "(owner_type = 'user' AND org_id IS NULL)",
            name="ck_skill_package_owner_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_type: Mapped[str] = mapped_column(String(20), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    org_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True
    )
    namespace: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_locator: Mapped[str] = mapped_column(String(500), nullable=False)
    source_subdir: Mapped[str] = mapped_column(String(500), nullable=False, server_default="")
    current_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_yanked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Owner (org / personal) moderation of the whole package (migration 066):
    # NULL | 'yanked' | 'blocked'. Hides only the package's NON-PUBLIC
    # listings, including ones published later; is_yanked is the platform flag.
    owner_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Platform moderation of the whole package (migration 066): NULL |
    # 'yanked' | 'blocked'. is_yanked mirrors it (True for both) and is what
    # delivery filters on; 'blocked' also refuses publishes and deletions.
    platform_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Publisher that owns this package's namespace (migration 071, §4.2).
    # NULL for legacy per-owner namespaces nobody has claimed.
    publisher_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("skill_publishers.id", ondelete="SET NULL"), nullable=True
    )
    # Discovery (migration 076, marketplace v2 §9): publisher-set deprecation
    # with an optional successor package (same publisher), and an optional
    # README path inside the package (default: README.md at the root, else
    # the first SKILL.md body).
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deprecation_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    successor_package_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("skill_packages.id", ondelete="SET NULL"), nullable=True
    )
    readme_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SkillPackageVersion(Base):
    """Immutable package bytes and hash-bound inspection metadata."""

    __tablename__ = "skill_package_versions"
    __table_args__ = (
        UniqueConstraint("package_id", "content_hash", name="uq_skill_version_hash"),
        Index("idx_skill_versions_package_created", "package_id", "created_at"),
        Index("idx_skill_versions_publication_seq", "package_id", "publication_seq"),
        Index("idx_skill_versions_content_hash", "content_hash"),
        Index("idx_skill_versions_created_by", "created_by_user_id"),
        Index("idx_skill_versions_capability_digest", "package_id", "capability_digest"),
        CheckConstraint(
            "dlp_status IN ('passed', 'blocked')",
            name="ck_skill_version_dlp_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    package_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_packages.id", ondelete="CASCADE"), nullable=False
    )
    version_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    source_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    agent_plugin_schema: Mapped[str] = mapped_column(String(200), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    risk_summary: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    compatibility: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    instruction_bundle: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    license_metadata: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    signature_metadata: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    dlp_status: Mapped[str] = mapped_column(String(20), nullable=False)
    blob_key: Mapped[str] = mapped_column(String(700), nullable=False, unique=True)
    blob_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Monotonic, package-scoped publication order (migration 070). Allocated
    # under the publisher lock so two publishes in the same second never tie
    # on created_at + random id; NULL only on rows a failed migration left
    # behind (treated as "never newer", fail closed).
    publication_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Instruction scanning (migration 069): bounded summary of the LATEST scan
    # (ruleset, max severity, counts, value-free findings), and the automated
    # quarantine a rescan sets on a new critical finding. Quarantine stops new
    # delivery only; it never revokes grants (a human block does that).
    scan_summary: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Install disclosure (migration 070, marketplace v2 §3.6): the byte-derived
    # capability manifest (skills/disclosure.py), the digest of its access
    # part, and its derivation version (0 = not computed yet; computed lazily
    # from the blob, and until then an update from it is treated as widening).
    capability_manifest: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    capability_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    manifest_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    # Search facets (migration 076, §9.2), derived from the JSON columns
    # above on every ORM flush (see _sync_version_facets) so the catalog
    # filters run in SQL: normalized declared license, worst OPEN scan
    # severity rank, and ",scripts,mcp,"-style capability classes.
    license_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    scan_severity_rank: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    capability_flags: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=""
    )


class SkillVersionTool(Base):
    """One row per (version, supported tool): the SQL-side compatibility filter."""

    __tablename__ = "skill_version_tools"
    __table_args__ = (Index("idx_skill_version_tools_tool", "tool", "version_id"),)

    version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("skill_package_versions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tool: Mapped[str] = mapped_column(String(50), primary_key=True)


class SkillMarketplaceListing(Base):
    __tablename__ = "skill_marketplace_listings"
    __table_args__ = (
        UniqueConstraint("collection", "version_id", name="uq_skill_listing_collection_version"),
        Index(
            "idx_skill_listing_visibility_state",
            "visibility",
            "org_id",
            "is_published",
            "is_yanked",
        ),
        Index("idx_skill_listing_search_name", "name"),
        Index("idx_skill_listing_package", "package_id"),
        Index("idx_skill_listing_owner_user", "owner_user_id"),
        Index("idx_skill_listing_version", "version_id"),
        Index("idx_skill_listing_org", "org_id"),
        Index("idx_skill_listing_published_by", "published_by_user_id"),
        CheckConstraint(
            "visibility IN ('public', 'user', 'org')",
            name="ck_skill_listing_visibility",
        ),
        CheckConstraint(
            "(visibility = 'public' AND org_id IS NULL AND owner_user_id IS NULL) OR "
            "(visibility = 'user' AND org_id IS NULL AND owner_user_id IS NOT NULL) OR "
            "(visibility = 'org' AND org_id IS NOT NULL AND owner_user_id IS NULL)",
            name="ck_skill_listing_visibility_owner",
        ),
        CheckConstraint(
            "verification_state IN ('verified', 'unverified', 'blocked')",
            name="ck_skill_listing_verification_state",
        ),
        CheckConstraint(
            "moderation_state IN ('approved', 'pending_review', 'quarantined', 'blocked', 'rejected')",
            name="ck_skill_listing_moderation_state",
        ),
        Index("idx_skill_listing_moderation", "moderation_state", "visibility"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    collection: Mapped[str] = mapped_column(
        String(100), nullable=False, default="sessionfs", server_default="sessionfs"
    )
    package_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_packages.id", ondelete="CASCADE"), nullable=False
    )
    version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_package_versions.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True
    )
    owner_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    visibility: Mapped[str] = mapped_column(String(20), nullable=False)
    is_published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    is_yanked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Owner (org / personal) moderation of THIS listing (migration 066):
    # NULL | 'yanked' | 'blocked'. Never set on a public listing; the
    # version-wide is_yanked / verification_state flags are platform state.
    owner_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    verification_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="verified", server_default="verified"
    )
    # Listing moderation, split from publisher identity (migration 071, §4.3):
    # 'approved' is deliverable; 'pending_review' (a verified publisher's first
    # public version, or an update that widens access) and 'rejected' are not.
    # verification_state is kept read-only-ish for one release (blocks still
    # write it); delivery requires BOTH to allow the listing.
    moderation_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="approved", server_default="approved"
    )
    moderation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    published_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Publication provenance for THIS listing event (JSON object). Unlike the
    # deduplicated version's signature_metadata — which records the provenance
    # of the run that FIRST minted the immutable version — this is the
    # provenance of the publish that created the listing, so a reused version
    # re-published via a different path keeps its own (e.g. trusted-publishing)
    # provenance for responses and moderation comparisons.
    signature_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Search facet (migration 076): this listing's publication provenance is
    # verified (trusted publishing over OIDC, or a verified attestation).
    # Derived from signature_metadata on every ORM flush.
    provenance_attested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class SkillConsentReceipt(Base):
    __tablename__ = "skill_consent_receipts"
    __table_args__ = (
        Index("idx_skill_consent_actor_hash", "actor_user_id", "content_hash"),
        Index("idx_skill_consent_version", "version_id"),
        Index("idx_skill_consent_request", "install_request_id"),
        CheckConstraint(
            "actor_type IN ('user', 'service_key')",
            name="ck_skill_consent_actor_type",
        ),
        CheckConstraint(
            "scope IN ('project', 'user', 'org', 'device')",
            name="ck_skill_consent_scope",
        ),
        CheckConstraint(
            "capability IN ('instructions', 'assets', 'scripts', 'mcp', 'hooks')",
            name="ck_skill_consent_capability",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Plain string, deliberately NOT a ForeignKey: a consent receipt is an
    # AUDIT ROW and must outlive the version it consented to (the same
    # audit-row-survival convention AgentRun uses for persona/ticket/session).
    # An ON DELETE CASCADE here destroyed the consent evidence at the exact
    # moment a version was deleted; `deleted_at` archives it instead.
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    capability: Mapped[str] = mapped_column(String(50), nullable=False)
    target_tool: Mapped[str | None] = mapped_column(String(50), nullable=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    managed_policy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    install_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Archived (soft-deleted) when the consented version was deleted. The row
    # is retained as durable consent evidence; every read filters it out.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # What was consented to (migration 070): the version's capability digest
    # and access manifest at consent time; the receipt a non-widening update
    # inherited consent from (plain string: audit references survive
    # archival); the re-consent rule in force (1 = v0.16 §7.3, 2 = D2).
    capability_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    consented_manifest: Mapped[str | None] = mapped_column(Text, nullable=True)
    carried_forward_from_receipt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consent_rule_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1"
    )


class SkillMarketplaceRegistry(Base):
    __tablename__ = "skill_marketplace_registries"
    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", "alias", name="uq_skill_registry_alias"),
        Index("idx_skill_registry_org", "org_id", "policy_state"),
        Index("idx_skill_registry_created_by", "created_by_user_id"),
        CheckConstraint(
            "owner_type IN ('user', 'project', 'org')",
            name="ck_skill_registry_owner_type",
        ),
        CheckConstraint(
            "policy_state IN ('approved', 'review', 'blocked')",
            name="ck_skill_registry_policy_state",
        ),
        CheckConstraint(
            "(owner_type = 'org' AND org_id IS NOT NULL AND owner_id = org_id) OR "
            "(owner_type = 'user' AND org_id IS NULL) OR "
            "owner_type = 'project'",
            name="ck_skill_registry_owner_scope",
        ),
        # Server-side polling (migration 079, GitHub #69).
        CheckConstraint(
            "poll_interval_seconds = 0 OR poll_interval_seconds >= 300",
            name="ck_skill_registry_poll_interval",
        ),
        CheckConstraint(
            "last_poll_status IS NULL OR last_poll_status IN ('ok', 'partial', 'error')",
            name="ck_skill_registry_poll_status",
        ),
        Index("idx_skill_registry_next_poll", "next_poll_at"),
        Index("idx_skill_registry_poll_actor", "poll_actor_user_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_type: Mapped[str] = mapped_column(String(20), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    org_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True
    )
    alias: Mapped[str] = mapped_column(String(50), nullable=False)
    source_url: Mapped[str] = mapped_column(String(500), nullable=False)
    refresh_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    policy_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="approved", server_default="approved"
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    # --- Server-side polling (migration 079). 0 = metadata-only (no fetch). ---
    poll_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Restricted, linear-time tag syntax (services/registry_polling.py); NULL
    # = the semver default.
    tag_pattern: Mapped[str | None] = mapped_column(String(200), nullable=True)
    branch_only_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # A REFERENCE (env:NAME / k8s:NAME), never a credential.
    credential_secret_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    publish_namespace: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The org owner/admin the poller publishes as (the last admin to change
    # the polling settings); re-checked as owner/admin on every publish.
    poll_actor_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_skill_registry_poll_actor"),
        nullable=True,
    )
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_poll_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_poll_error_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    poll_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    poll_lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    poll_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SkillRegistryPollRef(Base):
    """One processed (ref, commit) of a polled registry (migration 079).

    The idempotency record: a ref already seen at the same commit is never
    fetched again, whatever the outcome was (a refused tag stays refused
    until the tag moves). A force-moved tag is a NEW row (new commit)."""

    __tablename__ = "skill_registry_poll_refs"
    __table_args__ = (
        UniqueConstraint("registry_id", "ref_name", "commit_sha", name="uq_skill_registry_poll_ref"),
        Index("idx_skill_registry_poll_ref_created", "registry_id", "created_at"),
        CheckConstraint(
            "outcome IN ('published', 'partial', 'rejected', 'skipped')",
            name="ck_skill_registry_poll_ref_outcome",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    registry_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("skill_marketplace_registries.id", ondelete="CASCADE"),
        nullable=False,
    )
    ref_name: Mapped[str] = mapped_column(String(255), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    # JSON list of version ids published (or reused) from this ref.
    version_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    # Sanitized, bounded per-package results ({name, outcome, code}).
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SkillInstallRequest(Base):
    __tablename__ = "skill_install_requests"
    __table_args__ = (
        Index("idx_skill_request_project_status", "project_id", "status", "created_at"),
        Index("idx_skill_request_user_status", "requested_by_user_id", "status"),
        Index("idx_skill_request_version", "version_id"),
        Index("idx_skill_request_target_user", "target_user_id"),
        Index("idx_skill_request_approved_by", "approved_by_user_id"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled', 'applied', 'revoked')",
            name="ck_skill_request_status",
        ),
        CheckConstraint(
            "target_scope IN ('project', 'user', 'device')",
            name="ck_skill_request_target_scope",
        ),
        CheckConstraint(
            "target_scope != 'device' OR target_device_id IS NOT NULL",
            name="ck_skill_request_device_target",
        ),
        CheckConstraint(
            "requested_via IN ('api', 'mcp')",
            name="ck_skill_request_requested_via",
        ),
        # One ACTIVE approved version per (project, package, grant scope)
        # (migration 070): approving a replacement supersedes the old one.
        Index(
            "uq_skill_request_active_grant",
            "project_id",
            "package_id",
            "grant_scope_key",
            unique=True,
            postgresql_where=text(
                "status IN ('approved', 'applied') AND package_id IS NOT NULL "
                "AND grant_scope_key IS NOT NULL AND deleted_at IS NULL"
            ),
            sqlite_where=text(
                "status IN ('approved', 'applied') AND package_id IS NOT NULL "
                "AND grant_scope_key IS NOT NULL AND deleted_at IS NULL"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # Plain string, deliberately NOT a ForeignKey: a decided request is the
    # ATTRIBUTION record for "who asked for this package" and must outlive the
    # version. An ON DELETE CASCADE erased that trail (and dangled the consent
    # receipt's install_request_id) the moment a version was deleted.
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    requested_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    requested_by_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user", server_default="user"
    )
    requested_by_service_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    requested_by_service_key_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    target_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    target_device_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    target_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    target_tools: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    requested_capabilities: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    # 'api' | 'mcp' — request origin, for DISPLAY only. Caller-supplied under
    # bearer auth, so it is never a security input; same-principal approval is
    # gated by the emailed confirmation code below instead.
    requested_via: Mapped[str] = mapped_column(
        String(10), nullable=False, default="api", server_default="api"
    )
    approval_challenge_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Issuance rate limit anchor (a new code may be issued at most every
    # APPROVAL_CHALLENGE_MIN_INTERVAL); cleared when a delivery fails.
    approval_challenge_issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approval_challenge_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approval_challenge_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    approved_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Archived (soft-deleted) when the requested version was deleted. Only
    # TERMINAL requests are archived; pending ones are hard-deleted because
    # they can never be acted on again. Every read path filters this out.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # The capability digest the approver confirmed, bound immutably at
    # approval (migration 070) under that consent rule version. NULL for
    # approvals made before 070: devices never treat them as approving a
    # widening update (fail closed).
    approved_capability_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    approved_consent_rule_version: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # The requested version's package and the normalized grant scope
    # ('project' | 'user:<id>' | 'device:<id>:<device>') - the key of the
    # one-active-approval invariant - and the approval that superseded this
    # one (migration 070; plain strings, audit rows outlive their targets).
    package_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    grant_scope_key: Mapped[str | None] = mapped_column(String(250), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class OrgSkillAssignment(Base):
    __tablename__ = "org_skill_assignments"
    __table_args__ = (
        Index(
            "uq_org_skill_assignment_org_user",
            "org_id",
            "version_id",
            "target_type",
            "target_id",
            unique=True,
            # Revoked rows (migration 066) never block a fresh assignment.
            postgresql_where=text("target_type != 'device' AND revoked_at IS NULL"),
            sqlite_where=text("target_type != 'device' AND revoked_at IS NULL"),
        ),
        Index(
            "uq_org_skill_assignment_device",
            "org_id",
            "version_id",
            "target_user_id",
            "target_id",
            unique=True,
            postgresql_where=text("target_type = 'device' AND revoked_at IS NULL"),
            sqlite_where=text("target_type = 'device' AND revoked_at IS NULL"),
        ),
        # One ACTIVE assignment per (org, package, target) (migration 070):
        # moving a target to another version goes through /advance, which
        # supersedes (revokes) the old row first.
        Index(
            "uq_org_skill_assignment_package_target",
            "org_id",
            "package_id",
            "target_type",
            "target_id",
            unique=True,
            postgresql_where=text(
                "target_type != 'device' AND revoked_at IS NULL AND package_id IS NOT NULL"
            ),
            sqlite_where=text(
                "target_type != 'device' AND revoked_at IS NULL AND package_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_org_skill_assignment_package_device",
            "org_id",
            "package_id",
            "target_user_id",
            "target_id",
            unique=True,
            postgresql_where=text(
                "target_type = 'device' AND revoked_at IS NULL AND package_id IS NOT NULL"
            ),
            sqlite_where=text(
                "target_type = 'device' AND revoked_at IS NULL AND package_id IS NOT NULL"
            ),
        ),
        Index("idx_org_skill_assignment_target", "org_id", "target_type", "target_id"),
        Index("idx_org_skill_assignment_version", "version_id"),
        Index("idx_org_skill_assignment_target_user", "target_user_id"),
        Index("idx_org_skill_assignment_created_by", "created_by_user_id"),
        CheckConstraint(
            "target_type IN ('org', 'user', 'device')",
            name="ck_org_skill_assignment_target_type",
        ),
        CheckConstraint(
            "(target_type = 'org' AND target_id = '*' AND target_user_id IS NULL) OR "
            "(target_type = 'user' AND target_user_id IS NOT NULL AND "
            "target_id = target_user_id) OR "
            "(target_type = 'device' AND target_user_id IS NOT NULL AND target_id != '*')",
            name="ck_org_skill_assignment_target_binding",
        ),
        CheckConstraint(
            "mode IN ('required', 'optional')",
            name="ck_org_skill_assignment_mode",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # Plain string, deliberately NOT a ForeignKey (migration 066): a revoked
    # assignment is audit history and must outlive its version, like install
    # requests and consent receipts. Active assignments still block version
    # deletion (routes/marketplace.py _delete_version).
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str] = mapped_column(String(100), nullable=False, server_default="*")
    target_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    mode: Mapped[str] = mapped_column(String(20), nullable=False, server_default="optional")
    target_tools: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    policy_snapshot: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Set when the assigned version is blocked (migration 066). A revoked
    # assignment never grants again, even after the version is restored.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The capability digest the admin approved for this exact version
    # (migration 070), bound at creation under consent_rule_version (NULL on
    # assignments made before 070: never treated as a digest approval). An
    # assignment never authorizes another version: moving it to an update
    # goes through /advance, which re-checks D2 and SUPERSEDES this row
    # (kept as history; revoked_at is set too, so it never grants again).
    capability_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    consent_rule_version: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # The assigned version's package (migration 070, backfilled): the key of
    # the one-active-assignment-per-target invariant. Plain string like
    # version_id (audit rows outlive their package).
    package_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_assignment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


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

    The identity key is (org_idp_id, subject) — NOT email, and NOT the
    global (provider_issuer, subject) which breaks shared-issuer IdPs
    like Google Workspace (one issuer serving many orgs). Email is
    mutable at the IdP and must never be the join key for an existing
    link. A single User may hold multiple ExternalIdentity rows (e.g. a
    consultant in two customer orgs' IdPs).
    """

    __tablename__ = "external_identities"
    __table_args__ = (
        Index("idx_external_identity_user", "user_id"),
        Index("idx_external_identity_org_idp", "org_idp_id"),
        UniqueConstraint(
            "org_idp_id", "subject",
            name="uq_external_identity_idp_sub",
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


class SsoBreakGlassGrant(Base):
    """Durable, server-consulted admin break-glass grant for SSO enforcement.

    Under SSO enforcement, admins are normally gated like all other humans.
    The owner can issue a time-boxed break-glass grant to a specific admin
    so they can authenticate with an API key while the IdP is misconfigured.
    The grant is consulted at auth time (§4.2 enforcement check) with
    server-side expiry; a stale grant never admits.

    Single active grant per admin enforced by partial-unique index
    uq_sbg_one_active_per_admin (mirrors the org_owner_transfer one-pending
    pattern). The owner is always exempt by construction and never needs a
    grant.
    """

    __tablename__ = "sso_break_glass_grants"
    __table_args__ = (
        Index("idx_sbg_org", "org_id"),
        Index(
            "uq_sbg_one_active_per_admin",
            "org_id", "admin_user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sbg_<hex>
    org_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    admin_user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    issued_by_user_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
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
    # 'cli' (default — the existing JSON-response contract) or 'browser' (the
    # dashboard flow: /callback redirects with a one-time code instead of
    # returning the key). Server-authoritative; set at /start, bound to `state`.
    client_flow: Mapped[str] = mapped_column(
        String(20), nullable=False, default="cli", server_default="cli"
    )


class SsoExchangeCode(Base):
    """One-time authorization code for the BROWSER SSO login handoff.

    Issued by /callback for a browser flow (client_flow='browser') INSTEAD of
    minting a key inline; traded at POST /auth/sso/exchange for a freshly-minted
    api_key. Carries ONLY identity (never a secret / never a raw key). Single-use
    via an atomic rowcount-1 consume, 120s TTL, sha256-at-rest. Bound to the
    authenticating browser via `binding_hash` (a cookie-nonce hash) to defeat
    login-CSRF / session-fixation (Sentinel F1).
    """

    __tablename__ = "sso_exchange_codes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'consumed')",
            name="ck_sso_exchange_code_status",
        ),
        Index("ix_sso_exchange_codes_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code_hash: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True,
        comment="sha256 of the raw one-time code; raw code NEVER stored",
    )
    binding_hash: Mapped[str] = mapped_column(
        String(128), nullable=False,
        comment="sha256 of the browser-binding cookie nonce (login-CSRF defense)",
    )
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    link_method: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class ProjectFeatureGrandfather(Base):
    """A project that keeps a feature through the caller's own plan.

    Project-scoped features are gated on the plan of the project's owner
    (the owning org, or the owner's own plan for personal projects).
    Before v0.17.2 they were gated on the CALLER's plan, so members on a
    paid plan could use paid features inside a free org's project. To
    avoid taking that away, migration 065 recorded every project that
    already used each feature; for those projects a caller whose own plan
    includes the feature keeps access. The set is written once by the
    migration (and carried along by project merges), never grown at
    request time.
    """

    __tablename__ = "project_feature_grandfathers"

    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True,
    )
    feature: Mapped[str] = mapped_column(String(64), primary_key=True)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


# Marketplace takedown (migration 066).

SKILL_REPORT_REASONS = (
    "malicious",
    "secrets",
    "impersonation",
    "broken",
    "license",
    "spam",
    "other",
)


class SkillPackageReport(Base):
    """A signed-in user's report against a marketplace package or version.

    package_id / version_id are plain strings (no FK) so the moderation record
    survives package deletion, like the other marketplace audit rows.
    """

    __tablename__ = "skill_package_reports"
    __table_args__ = (
        Index(
            "uq_skill_report_open_reporter_package",
            "reporter_user_id",
            "package_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
        Index("idx_skill_report_package_status", "package_id", "status"),
        Index("idx_skill_report_status_created", "status", "created_at"),
        Index("idx_skill_report_reporter_created", "reporter_user_id", "created_at"),
        Index("idx_skill_report_resolved_by", "resolved_by_user_id"),
        Index("idx_skill_report_scope", "scope_kind", "scope_org_id", "status", "created_at"),
        CheckConstraint(
            "(scope_kind = 'platform' AND scope_org_id IS NULL) OR "
            "(scope_kind = 'org' AND scope_org_id IS NOT NULL)",
            name="ck_skill_report_scope",
        ),
        CheckConstraint(
            "reason IN ('malicious', 'secrets', 'impersonation', 'broken', "
            "'license', 'spam', 'other')",
            name="ck_skill_report_reason",
        ),
        CheckConstraint(
            "status IN ('open', 'actioned', 'dismissed')", name="ck_skill_report_status"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reporter_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="open", server_default="open"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Bound at creation to the listings the reporter could see: 'platform'
    # (a public or personal listing) or 'org' (only that org's listings).
    scope_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SkillRevocationEvent(Base):
    """Append-only yank / block / restore history feeding the revocations feed.

    `id` is monotonic and `changed_at` is strictly increasing (written under a
    writer lock; the unique index is the backstop), so the feed pages on
    changed_at alone. kind 'version' / 'package' target one version or a whole
    package; kind 'content' carries only a blocked (or unblocked) content
    hash. The visibility / org_id / owner_user_id snapshot scopes who may
    read the row.
    """

    __tablename__ = "skill_revocation_events"
    __table_args__ = (
        Index("uq_skill_revocation_changed_at", "changed_at", unique=True),
        Index("idx_skill_revocation_package", "package_id", "version_id", "id"),
        Index("idx_skill_revocation_org", "org_id", "changed_at"),
        Index("idx_skill_revocation_owner", "owner_user_id", "changed_at"),
        Index("idx_skill_revocation_actor", "actor_user_id"),
        CheckConstraint(
            "state IN ('yanked', 'blocked', 'restored', 'quarantined')",
            name="ck_skill_revocation_state"
        ),
        CheckConstraint(
            "kind IN ('version', 'package', 'content')", name="ck_skill_revocation_kind"
        ),
        CheckConstraint(
            "block_scope IS NULL OR block_scope IN ('global', 'org', 'owner')",
            name="ck_skill_revocation_block_scope",
        ),
        CheckConstraint(
            "(COALESCE(block_scope, '') = 'org' AND scope_org_id IS NOT NULL) OR "
            "(COALESCE(block_scope, '') = 'owner') OR "
            "(COALESCE(block_scope, '') NOT IN ('org', 'owner') AND scope_org_id IS NULL)",
            name="ck_skill_revocation_block_scope_org",
        ),
        CheckConstraint(
            "(kind = 'version' AND package_id IS NOT NULL AND version_id IS NOT NULL) OR "
            "(kind = 'package' AND package_id IS NOT NULL AND version_id IS NULL) OR "
            "(kind = 'content' AND package_id IS NULL AND version_id IS NULL "
            "AND content_hash IS NOT NULL)",
            name="ck_skill_revocation_kind_target",
        ),
        CheckConstraint(
            "visibility IN ('public', 'user', 'org')", name="ck_skill_revocation_visibility"
        ),
        CheckConstraint(
            "(visibility = 'public') OR "
            "(visibility = 'org' AND org_id IS NOT NULL) OR "
            "(visibility = 'user' AND owner_user_id IS NOT NULL)",
            name="ck_skill_revocation_scope",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    package_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    block_scope: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Set exactly when block_scope == 'org': the org the block is confined to.
    scope_org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Internal provenance of 'content' events; never served by the feed.
    source_package_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_role: Mapped[str] = mapped_column(String(20), nullable=False)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    visibility: Mapped[str] = mapped_column(String(20), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillBlockedContentHash(Base):
    """Durable content-hash blocklist (migration 066).

    Independent of package / version / listing rows: deleting a blocked
    version and re-publishing identical bytes stays blocked. scope_org_id
    NULL = global block (platform admin); otherwise it applies only to
    packages owned by that org. A block is active while lifted_at IS NULL;
    rows are lifted on restore, never deleted.
    """

    __tablename__ = "skill_blocked_content_hashes"
    __table_args__ = (
        Index("idx_skill_blocked_hash_active", "content_hash", "lifted_at"),
        Index("idx_skill_blocked_hash_source", "source_package_id", "source_version_id"),
        Index("idx_skill_blocked_hash_actor", "actor_user_id"),
        Index("idx_skill_blocked_hash_lifted_by", "lifted_by_user_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    scope_org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_package_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_role: Mapped[str] = mapped_column(String(20), nullable=False)
    blocked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifted_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class SkillGrantGeneration(Base):
    """Per-collection change counter for the paginated grant lists
    (migration 066): scope_type 'project' (its install requests) or 'org'
    (its assignments). Bumped in the same transaction as any write to the
    collection, so a cursor issued before a change is refused (409
    listing_changed) instead of silently missing a row. A dedicated table, so
    bumping it never contends with the locks publishes take on project/org
    rows."""

    __tablename__ = "skill_grant_generations"

    scope_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )


class SkillScanRuleset(Base):
    """One row per instruction-scanner ruleset version that has been used."""

    __tablename__ = "skill_scan_rulesets"

    version: Mapped[str] = mapped_column(String(20), primary_key=True)
    released_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    rule_count: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")


SKILL_SCAN_SEVERITIES = ("none", "low", "medium", "high", "critical")


class SkillVersionScan(Base):
    """One instruction scan of one package version under one ruleset.

    Re-running the same ruleset (weekly cadence) updates the row in place;
    a new ruleset adds a row. Findings are diffed against the previous scan
    so only NEW findings trigger action (quarantine)."""

    __tablename__ = "skill_version_scans"
    __table_args__ = (
        UniqueConstraint("version_id", "ruleset_version", name="uq_skill_version_scan_ruleset"),
        Index("idx_skill_version_scans_version_scanned", "version_id", "scanned_at"),
        Index("idx_skill_version_scans_ruleset_scanned", "ruleset_version", "scanned_at"),
        CheckConstraint(
            "max_severity IN ('none', 'low', 'medium', 'high', 'critical')",
            name="ck_skill_version_scan_severity",
        ),
        CheckConstraint(
            "trigger IN ('publish', 'rescan', 'manual')", name="ck_skill_version_scan_trigger"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_package_versions.id", ondelete="CASCADE"), nullable=False
    )
    ruleset_version: Mapped[str] = mapped_column(
        String(20), ForeignKey("skill_scan_rulesets.version"), nullable=False
    )
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    max_severity: Mapped[str] = mapped_column(String(10), nullable=False)
    finding_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    new_finding_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    trigger: Mapped[str] = mapped_column(String(10), nullable=False)


class SkillScanFinding(Base):
    """One finding of one scan. Never stores matched text: the rule id, the
    location, the rule's fixed explanation and a hash of the flagged line."""

    __tablename__ = "skill_scan_findings"
    __table_args__ = (
        Index("idx_skill_scan_findings_scan", "scan_id"),
        CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="ck_skill_scan_finding_severity",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    scan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_version_scans.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[str] = mapped_column(String(80), nullable=False)
    family: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    line: Mapped[int] = mapped_column(Integer, nullable=False)
    col: Mapped[int] = mapped_column(Integer, nullable=False)
    message: Mapped[str] = mapped_column(String(300), nullable=False)
    line_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    suppressed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    suppression_justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_scan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ---------------------------------------------------------------------------
# Marketplace v2 §4: verified publishers, namespaces, trusted publishing
# (migration 071)
# ---------------------------------------------------------------------------


class SkillPublisher(Base):
    """A first-class owner of a globally unique public namespace.

    Namespaces are never released: deleting a publisher tombstones the row so
    the namespace (and its confusable skeleton) cannot be re-registered by
    someone else (resurrection defence). ``org_id`` / ``user_id`` are plain
    columns, deliberately without a foreign key, so deleting the owning org
    or user cannot cascade the row away and free the namespace."""

    __tablename__ = "skill_publishers"
    __table_args__ = (
        UniqueConstraint("namespace", name="uq_skill_publishers_namespace"),
        UniqueConstraint("namespace_normalized", name="uq_skill_publishers_namespace_normalized"),
        Index("idx_skill_publishers_org", "org_id"),
        Index("idx_skill_publishers_user", "user_id"),
        CheckConstraint(
            "owner_type IN ('org', 'user', 'system')", name="ck_skill_publisher_owner_type"
        ),
        CheckConstraint(
            "(owner_type = 'system' AND org_id IS NULL AND user_id IS NULL) OR "
            "(owner_type = 'org' AND org_id IS NOT NULL AND user_id IS NULL) OR "
            "(owner_type = 'user' AND user_id IS NOT NULL AND org_id IS NULL)",
            name="ck_skill_publisher_owner_scope",
        ),
        CheckConstraint(
            "status IN ('active', 'suspended', 'tombstoned')", name="ck_skill_publisher_status"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_type: Mapped[str] = mapped_column(String(20), nullable=False)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    namespace: Mapped[str] = mapped_column(String(39), nullable=False)
    namespace_normalized: Mapped[str] = mapped_column(String(39), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    suspended_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Public listings a suspension moved into review (restored on un-suspend).
    suspended_listing_ids: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    # D1 invited cohort: a platform admin opts a verified publisher into
    # public publishing. Verification alone is necessary, not sufficient.
    public_publishing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Distance-2 lookalike flags raised at claim time, for moderators.
    review_flags: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    # Domains whose proof unlocked a reserved (or affixed reserved) namespace
    # at claim time (JSON list). Public publishing requires a CURRENT DNS
    # verification of each one on this publisher; other verifications do not
    # substitute.
    required_domains: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    # Authorization generation: bumped on suspension, tombstoning and trust
    # revocation. Publish tokens carry the generation they were minted under
    # and are refused once it moves on (a restore never revives them).
    auth_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now(),
        onupdate=_utcnow,
    )


class SkillPublisherVerification(Base):
    """Proof of WHO a publisher is: a DNS TXT domain or a GitHub account
    (numeric id). Separate from listing moderation. At most one publisher
    holds a verified (method, subject) at a time."""

    __tablename__ = "skill_publisher_verifications"
    __table_args__ = (
        Index("idx_skill_publisher_verifications_publisher", "publisher_id"),
        Index("idx_skill_publisher_verifications_installation", "github_installation_id"),
        Index(
            "uq_skill_publisher_verification_subject",
            "method",
            "subject",
            unique=True,
            postgresql_where=text("status = 'verified'"),
            sqlite_where=text("status = 'verified'"),
        ),
        CheckConstraint(
            "method IN ('dns_txt', 'github_org', 'github_user')",
            name="ck_skill_publisher_verification_method",
        ),
        CheckConstraint(
            "status IN ('pending', 'verified', 'lapsed', 'revoked')",
            name="ck_skill_publisher_verification_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    publisher_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_publishers.id", ondelete="CASCADE"), nullable=False
    )
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    # Domain (lowercase) or GitHub numeric account id as text.
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_display: Mapped[str] = mapped_column(String(255), nullable=False)
    verification_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    org_domain_verification_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("org_domain_verifications.id", ondelete="SET NULL"), nullable=True
    )
    github_installation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    status_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class SkillReservedName(Base):
    """Admin-managed reserved namespace skeletons (brand, popular, tombstone),
    on top of the vendored BRAND_RESERVED list in skills/confusables.py."""

    __tablename__ = "skill_reserved_names"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('brand', 'popular', 'tombstone')", name="ck_skill_reserved_name_reason"
        ),
    )

    normalized: Mapped[str] = mapped_column(String(39), primary_key=True)
    name: Mapped[str] = mapped_column(String(39), nullable=False)
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    # Domain whose verification unlocks the claim; NULL = platform admin only.
    required_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class SkillTrustedPublisher(Base):
    """A CI identity allowed to publish for a publisher.

    provider 'github': a GitHub Actions workflow, bound to the NUMERIC
    repository id and owner id (immutable across renames and never reused by
    a re-registered name) and the workflow file.

    provider 'gitlab' (migration 082): a GitLab CI project on one allowlisted
    GitLab instance (``issuer``), bound to the NUMERIC project id and,
    optionally, the namespace id.

    Either may add a deployment environment and a ref pattern."""

    __tablename__ = "skill_trusted_publishers"
    __table_args__ = (
        Index("idx_skill_trusted_publishers_repo", "provider", "repository_id"),
        Index("idx_skill_trusted_publishers_publisher", "publisher_id"),
        Index("idx_skill_trusted_publishers_gitlab", "provider", "gitlab_project_id"),
        CheckConstraint(
            "provider IN ('github', 'gitlab')", name="ck_skill_trusted_publisher_provider"
        ),
        CheckConstraint(
            "(provider = 'github' AND repository_id IS NOT NULL AND repository_owner_id IS NOT NULL "
            "AND workflow_path IS NOT NULL AND issuer IS NULL AND gitlab_project_id IS NULL "
            "AND gitlab_namespace_id IS NULL AND require_protected_environment = false) OR "
            "(provider = 'gitlab' AND issuer IS NOT NULL AND gitlab_project_id IS NOT NULL "
            "AND repository_id IS NULL AND repository_owner_id IS NULL AND workflow_path IS NULL "
            "AND reusable_workflow IS NULL AND allow_workflow_run = false "
            "AND (require_protected_environment = false OR environment IS NOT NULL))",
            name="ck_skill_trusted_publisher_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    publisher_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_publishers.id", ondelete="CASCADE"), nullable=False
    )
    # NULL = any package in the publisher's namespace.
    package_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False, server_default="github")
    # GitHub only (NULL on GitLab rows).
    repository_owner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    repository_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Display only ("owner/repo", or the GitLab project path); matching uses the ids.
    repository: Mapped[str] = mapped_column(String(200), nullable=False)
    # GitHub only: the CALLER workflow file (matched against workflow_ref).
    workflow_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # GitLab only: the instance origin (the token's exact ``iss``; on the
    # SFS_GITLAB_OIDC_ISSUERS allowlist), and the numeric project id (the
    # immutable pin) and optional namespace id.
    issuer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gitlab_project_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gitlab_namespace_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # GitLab only: also require GitLab to report the job's environment as
    # protected. A job names its own environment, so the name alone binds
    # nothing unless the environment is protected in GitLab.
    require_protected_environment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Optional reusable (callee) workflow, "owner/repo/.github/workflows/x.yml",
    # matched independently against job_workflow_ref.
    reusable_workflow: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # workflow_run-triggered publishes are refused unless explicitly allowed.
    allow_workflow_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    environment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ref_pattern: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class SkillPublishToken(Base):
    """Short-lived, single-use publish token minted from a verified GitHub
    Actions or GitLab CI OIDC token. Only the SHA-256 of the raw token is stored. The
    (issuer, jti) unique constraint is the OIDC replay guard."""

    __tablename__ = "skill_publish_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_skill_publish_tokens_hash"),
        UniqueConstraint("oidc_issuer", "oidc_jti", name="uq_skill_publish_tokens_jti"),
        Index("idx_skill_publish_tokens_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    trusted_publisher_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_trusted_publishers.id", ondelete="CASCADE"), nullable=False
    )
    publisher_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_publishers.id", ondelete="CASCADE"), nullable=False
    )
    package_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    oidc_issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    oidc_jti: Mapped[str] = mapped_column(String(255), nullable=False)
    # Bounded, allow-listed subset of the verified claims (JSON).
    claims: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    # SkillPublisher.auth_generation when minted; must still match to publish.
    auth_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_for_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class SkillGithubInstallationState(Base):
    """Durable GitHub App installation lifecycle seen by signed webhooks
    (suspend / unsuspend / delete), for marketplace publisher verification.

    Kept here (migration 071) rather than on github_installations, whose
    claim flow is being reworked separately (tk_b6c5e184ba8d4aa1, migration
    073); verification creation re-checks it under the installation lock."""

    __tablename__ = "skill_github_installation_states"

    installation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class SkillFeedCatalog(Base):
    """One generated native catalog repository per scope (migration 077,
    marketplace v2 §7.5). ``scope_key`` is 'public', 'org:<id>' or
    'user:<id>' so the public catalog (scope_id NULL) is unique too.

    The repository files live in blob storage under ``blob_prefix``; the row
    records the CURRENT commit and the digest of the approved input set it
    was built from, so a fetch can tell in one cheap query whether the
    catalog is stale (a block / yank / new approval changes the digest)."""

    __tablename__ = "skill_feed_catalogs"
    __table_args__ = (
        UniqueConstraint("scope_key", name="uq_skill_feed_catalogs_scope_key"),
        CheckConstraint(
            "scope_type IN ('public', 'org', 'user')", name="ck_skill_feed_catalog_scope_type"
        ),
        CheckConstraint(
            "(scope_type = 'public' AND scope_id IS NULL) OR "
            "(scope_type != 'public' AND scope_id IS NOT NULL)",
            name="ck_skill_feed_catalog_scope_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(100), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plugin_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    blob_prefix: Mapped[str] = mapped_column(String(500), nullable=False)
    # Optional customer-owned mirror (D4b); exports are pushed by the client.
    export_remote: Mapped[str | None] = mapped_column(String(500), nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class SkillFeedEntry(Base):
    """One (catalog, version, tool) entry of a catalog's current commit."""

    __tablename__ = "skill_feed_entries"
    __table_args__ = (Index("idx_skill_feed_entries_version", "version_id"),)

    catalog_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_feed_catalogs.id", ondelete="CASCADE"), primary_key=True
    )
    version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_package_versions.id", ondelete="CASCADE"), primary_key=True
    )
    tool: Mapped[str] = mapped_column(String(50), primary_key=True)
    plugin_name: Mapped[str] = mapped_column(String(100), nullable=False)
    projection_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    plugin_path: Mapped[str] = mapped_column(String(300), nullable=False)


class SkillToolVerification(Base):
    """Evidence that a version works in a tool (§7.4). L0 = the projection
    validates against the tool's documented format (in-process, at catalog
    generation); L1 = the tool's own installer accepted it; L2 = the tool
    listed its components. L1/L2 come only from the sandboxed harness."""

    __tablename__ = "skill_tool_verifications"
    __table_args__ = (
        Index("idx_skill_tool_verifications_lookup", "version_id", "tool", "run_at"),
        CheckConstraint("level IN ('L0', 'L1', 'L2')", name="ck_skill_tool_verification_level"),
        CheckConstraint(
            "status IN ('pass', 'fail', 'regressed', 'skipped')",
            name="ck_skill_tool_verification_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_package_versions.id", ondelete="CASCADE"), nullable=False
    )
    tool: Mapped[str] = mapped_column(String(50), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(50), nullable=False)
    level: Mapped[str] = mapped_column(String(2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # Bounded JSON object (installer exit code, listed components, digests).
    evidence: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    projection_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    harness_image_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class SkillVerificationJob(Base):
    """Queued harness work (L1/L2). The harness is a sandboxed job outside
    the API request path; it claims jobs and posts results back."""

    __tablename__ = "skill_verification_jobs"
    __table_args__ = (
        Index(
            "uq_skill_verification_jobs_open",
            "version_id",
            "tool",
            "level",
            unique=True,
            postgresql_where=text("status IN ('queued', 'claimed')"),
            sqlite_where=text("status IN ('queued', 'claimed')"),
        ),
        Index("idx_skill_verification_jobs_status", "status", "tool", "created_at"),
        Index("idx_skill_verification_jobs_org", "owner_org_id", "created_at"),
        CheckConstraint("level IN ('L1', 'L2')", name="ck_skill_verification_job_level"),
        CheckConstraint(
            "status IN ('queued', 'claimed', 'done')", name="ck_skill_verification_job_status"
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('pass', 'fail', 'regressed', 'skipped', 'errored')",
            name="ck_skill_verification_job_outcome",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_package_versions.id", ondelete="CASCADE"), nullable=False
    )
    tool: Mapped[str] = mapped_column(String(50), nullable=False)
    level: Mapped[str] = mapped_column(String(2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="queued")
    trigger: Mapped[str] = mapped_column(String(30), nullable=False, server_default="manual")
    # The org whose per-org verification quota this job counts against
    # (NULL for public packages).
    owner_org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    claim_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # How a done job ended: a verdict (pass/fail/regressed/skipped) or
    # 'errored' (the harness could not run it within the attempt cap). A job
    # whose package became undeliverable ends 'skipped' with no evidence row.
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    outcome_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class OrgSkillPolicy(Base):
    """One immutable revision of an org's skills policy (migration 078,
    marketplace v2 §8.5). A PUT creates a new revision; rows are never
    updated in place."""

    __tablename__ = "org_skill_policies"
    __table_args__ = (
        UniqueConstraint("org_id", "revision", name="uq_org_skill_policy_revision"),
        CheckConstraint("revision >= 1", name="ck_org_skill_policy_revision"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_json: Mapped[str] = mapped_column(Text, nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class OrgPolicyCompilation(Base):
    """Compiled per-tool output of a policy revision (migration 078)."""

    __tablename__ = "org_policy_compilations"
    __table_args__ = (
        UniqueConstraint("policy_id", "tool", "compiler_version", name="uq_org_policy_compilation"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("org_skill_policies.id", ondelete="CASCADE"), nullable=False
    )
    tool: Mapped[str] = mapped_column(String(50), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(20), nullable=False)
    artifact_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    enforceable: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    report_only: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    unsupported: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class SkillDevice(Base):
    """A user's device as seen by the advisory policy reconcile path
    (migration 078). device_key is the client-reported device id."""

    __tablename__ = "skill_devices"
    __table_args__ = (UniqueConstraint("user_id", "device_key", name="uq_skill_device_user_key"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_key: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    platform: Mapped[str] = mapped_column(String(30), nullable=False, server_default="")
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class DevicePolicyReport(Base):
    """A device's LATEST compliance report for one org's policy (migration
    078): one row per (device, org), replaced on every report. History is
    kept in DevicePolicyReportHistory. Advisory telemetry: never an
    access-control input."""

    __tablename__ = "device_policy_reports"
    __table_args__ = (
        UniqueConstraint("device_id", "org_id", name="uq_device_policy_report_device_org"),
        Index("idx_device_policy_reports_org_time", "org_id", "reported_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_devices.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    policy_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("org_skill_policies.id", ondelete="CASCADE"), nullable=True
    )
    policy_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    report_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    compliant: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class DevicePolicyReportHistory(Base):
    """At most one compliance report per (device, org, UTC day): the last
    report of that day (migration 078). Retained 90 days."""

    __tablename__ = "device_policy_report_history"
    __table_args__ = (
        UniqueConstraint(
            "device_id", "org_id", "report_day", name="uq_device_policy_report_history_day"
        ),
        Index("idx_device_policy_report_history_time", "reported_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_devices.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    policy_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # UTC calendar day of the report, "YYYY-MM-DD".
    report_day: Mapped[str] = mapped_column(String(10), nullable=False)
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    report_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    compliant: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


# ---------------------------------------------------------------------------
# Marketplace discovery and freshness (migration 076, marketplace v2 §9)
# ---------------------------------------------------------------------------


class SkillCategory(Base):
    """The fixed, curated category taxonomy (seeded by migration 076 from
    skills/discovery.py CATEGORIES)."""

    __tablename__ = "skill_categories"

    slug: Mapped[str] = mapped_column(String(40), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class SkillPackageCategory(Base):
    """Up to three categories per package (publisher-set; moderators can
    change them)."""

    __tablename__ = "skill_package_categories"
    __table_args__ = (Index("idx_skill_package_categories_slug", "category_slug", "package_id"),)

    package_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_packages.id", ondelete="CASCADE"), primary_key=True
    )
    category_slug: Mapped[str] = mapped_column(
        String(40), ForeignKey("skill_categories.slug", ondelete="CASCADE"), primary_key=True
    )


class SkillInstallDaily(Base):
    """Daily install-event aggregates. No per-user data: one row per
    (version, tool, source, event, actor type, org, day). ``org_id`` is the
    consuming org ('' for none) so an org admin can see their own members'
    usage; it is never exposed to a publisher. ``version_id`` carries no
    foreign key so a deleted version's counts still roll up to its package."""

    __tablename__ = "skill_install_daily"
    __table_args__ = (
        Index("idx_skill_install_daily_package_day", "package_id", "day"),
        Index("idx_skill_install_daily_org_day", "org_id", "day"),
        Index("idx_skill_install_daily_day", "day"),
        CheckConstraint(
            "event IN ('install', 'uninstall', 'active', 'translation_failure')",
            name="ck_skill_install_daily_event",
        ),
        CheckConstraint(
            "source IN ('download', 'reconcile', 'native')", name="ck_skill_install_daily_source"
        ),
        CheckConstraint("actor_type IN ('user', 'service')", name="ck_skill_install_daily_actor"),
    )

    version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tool: Mapped[str] = mapped_column(String(50), primary_key=True)
    source: Mapped[str] = mapped_column(String(20), primary_key=True)
    event: Mapped[str] = mapped_column(String(20), primary_key=True)
    actor_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), primary_key=True, default="", server_default="")
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    package_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_packages.id", ondelete="CASCADE"), nullable=False
    )
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class SkillInstallDedupe(Base):
    """One row per pseudonymous principal per (version, tool, event, day):
    an event counts at most once a day per principal. Pruned after 30 days
    by the freshness sweeper. ``principal_hash`` is a SHA-256 of the actor
    identity (never stored in clear)."""

    __tablename__ = "skill_install_dedupe"
    __table_args__ = (
        Index("idx_skill_install_dedupe_package_day", "package_id", "day"),
        Index("idx_skill_install_dedupe_day", "day"),
    )

    principal_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tool: Mapped[str] = mapped_column(String(50), primary_key=True)
    event: Mapped[str] = mapped_column(String(20), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    package_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_packages.id", ondelete="CASCADE"), nullable=False
    )
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False, default="user", server_default="user")


class SkillPackageFreshness(Base):
    """Package-level freshness and ranking inputs, recomputed by the daily
    sweeper (POST /api/v1/admin/marketplace/freshness/sweep)."""

    __tablename__ = "skill_package_freshness"
    __table_args__ = (Index("idx_skill_package_freshness_stale", "stale"),)

    package_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_packages.id", ondelete="CASCADE"), primary_key=True
    )
    last_version_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    works_in_regressed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    source_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    unmaintained: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    installs_30d: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    service_installs_30d: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    popularity_rank: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class SkillPackageToolFreshness(Base):
    """Per (package, tool): when the package last shipped a version for the
    tool, and when it was last verified to work there. Verification results
    come from the works-in harness (POST /api/v1/admin/marketplace/
    works-in-results); 'regressed' = a version that passed, then failed."""

    __tablename__ = "skill_package_tool_freshness"
    __table_args__ = (
        Index("idx_skill_package_tool_freshness_tool", "tool", "verify_status"),
        CheckConstraint(
            "verify_status IN ('unknown', 'passing', 'failing', 'regressed')",
            name="ck_skill_package_tool_freshness_status",
        ),
    )

    package_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("skill_packages.id", ondelete="CASCADE"), primary_key=True
    )
    tool: Mapped[str] = mapped_column(String(50), primary_key=True)
    last_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verified_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The version verify_status describes. A status only counts while this is
    # the package's CURRENT version; a new release reads as "not verified".
    last_checked_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verify_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown", server_default="unknown"
    )
    verify_detail: Mapped[str | None] = mapped_column(String(300), nullable=True)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class HelperConversation(Base):
    """A session-helper thread (migration 092; Enterprise feature).

    ``org_id`` names the org that owns the conversation's scope (the
    project's org, when the turn is project-scoped) and is NULL for
    personal scope. ON DELETE SET NULL keeps the thread if the org is
    deleted, matching the project/session data-stays-access-revoked
    invariant. Threads are private to their user; the v1 DELETE endpoint
    and the admin retention sweep hard-delete rows (``deleted_at`` is
    reserved for a future soft-delete).
    """

    __tablename__ = "helper_conversations"
    __table_args__ = (
        Index("idx_helper_conversations_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # JSON-as-Text (mirrors helper_messages.citations for cross-DB
    # compatibility): the project/session scope this conversation was created
    # with, restored on resume so an org-bound thread can continue without the
    # caller re-supplying its scope.
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HelperMessage(Base):
    """One message in a session-helper conversation (migration 092).

    ``content`` is the model-visible text after DLP redaction. Tool messages
    carry the sanitized call identity (``tool_name`` / ``tool_call_id``) and
    sanitized ``tool_args``; assistant messages carry the answer text and a
    JSON ``citations`` list of ``{session_id, message_index?}``. ``key_source``
    records ``user`` or ``org`` for the turn — never the key itself.
    ``stop_reason`` records how the turn ended (``stop``, ``max_tokens``,
    ``max_steps``, ``deadline``, …) and ``client_disconnected`` when the
    caller dropped the stream mid-answer.
    """

    __tablename__ = "helper_messages"
    __table_args__ = (
        Index("idx_helper_messages_conversation", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("helper_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tool_args: Mapped[str | None] = mapped_column(Text, nullable=True)
    citations: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    stop_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        # Python-side default so messages flushed in one turn get distinct
        # sub-second timestamps; the server_default is the raw-SQL fallback.
        default=_utcnow,
        server_default=func.now(),
    )


class HelperAction(Base):
    """A proposed, human-confirmed write action from the session helper
    (migration 094).

    The helper can only PROPOSE: a row starts ``pending`` and nothing changes
    until the caller confirms it in the dashboard. ``kind`` is
    ``set_auto_narrative`` or ``trash_sessions``. ``params`` holds the exact
    target ids + desired value that confirm applies (the targets that would
    actually change); ``preview`` holds the per-target before/after + display
    names computed at proposal time (including skipped/unchanged targets);
    ``result`` holds the per-target ok/error once applied. JSON-as-Text for
    cross-DB compatibility, mirroring ``helper_messages``. ``expires_at`` is
    proposal time + 15 minutes; ``decided_at`` records confirm/cancel/expiry.
    ``message_id`` is the id of the ``helper_messages`` tool row that carried
    the proposal (written even though that row lands at the end of the turn).
    ``created_at`` is set at tool-call time and strictly increases within a
    process, so the pending cap can order proposals deterministically.
    """

    __tablename__ = "helper_actions"
    __table_args__ = (
        Index("idx_helper_actions_user_status", "user_id", "status"),
        Index("idx_helper_actions_conversation", "conversation_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("helper_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    params: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HelperConfig(Base):
    """Session-helper configuration for one org or one user (migration 095).

    Kept apart from the key rows (``org_llm_settings`` /
    ``user_judge_settings``) so deleting or rotating a key never touches the
    configuration, and saving a configuration never creates a key row.
    ``scope`` is ``org`` (``org_id`` set) or ``user`` (``user_id`` set) —
    exactly one, enforced by a CHECK. ``settings`` is JSON-as-Text holding only
    the keys the owner set explicitly (unset keys use the defaults);
    ``instructions`` is stored DLP-redacted; ``monthly_token_budget`` is
    org-only.
    """

    __tablename__ = "helper_configs"
    __table_args__ = (
        CheckConstraint(
            "(scope = 'org' AND org_id IS NOT NULL AND user_id IS NULL) OR "
            "(scope = 'user' AND user_id IS NOT NULL AND org_id IS NULL)",
            name="ck_helper_configs_scope_owner",
        ),
        CheckConstraint(
            "monthly_token_budget IS NULL OR "
            "(scope = 'org' AND monthly_token_budget >= 0)",
            name="ck_helper_configs_budget_org_only",
        ),
        Index(
            "uq_helper_configs_org",
            "org_id",
            unique=True,
            postgresql_where=text("org_id IS NOT NULL"),
            sqlite_where=text("org_id IS NOT NULL"),
        ),
        Index(
            "uq_helper_configs_user",
            "user_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
            sqlite_where=text("user_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    org_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    settings: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    monthly_token_budget: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        onupdate=_utcnow,
    )
    updated_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class HelperUsage(Base):
    """Append-only session-helper token ledger (migration 095).

    One row per turn that reported any tokens — including turns that ended
    with a client disconnect, a provider error, the deadline or max_steps.
    ``key_source`` is ``user`` or ``org``; ``org_id`` is the org whose key the
    turn used (NULL for a user-key turn). ``conversation_id`` is a plain column
    with no foreign key, so deleting a conversation (or the helper-retention
    sweep purging it) keeps the ledger row: budgets and usage summaries read
    only this table. Retention: 13 months — the helper-retention sweep deletes
    rows older than 400 days.
    """

    __tablename__ = "helper_usage"
    __table_args__ = (
        CheckConstraint(
            "key_source IN ('user', 'org')", name="ck_helper_usage_key_source"
        ),
        Index("idx_helper_usage_org_created", "org_id", "created_at"),
        Index("idx_helper_usage_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # SET NULL: deleting an org keeps its members' rows in their own totals.
    org_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    key_source: Mapped[str] = mapped_column(String(20), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    stop_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )


def _changed(target, attribute: str, inserting: bool) -> bool:
    """On insert every facet is derived; on update only when its source
    column changed in this flush (never loads an unloaded attribute)."""
    if inserting:
        return True
    from sqlalchemy import inspect as sa_inspect

    return bool(sa_inspect(target).attrs[attribute].history.has_changes())


def _sync_version_facets(target: SkillPackageVersion, inserting: bool) -> None:
    from sessionfs.skills import discovery

    if _changed(target, "license_metadata", inserting):
        target.license_key = discovery.license_key(target.license_metadata)
    if _changed(target, "scan_summary", inserting):
        target.scan_severity_rank = discovery.scan_severity_rank(target.scan_summary)
    if _changed(target, "risk_summary", inserting):
        target.capability_flags = discovery.capability_flags(target.risk_summary)


def _sync_listing_facets(target: SkillMarketplaceListing, inserting: bool) -> None:
    from sessionfs.skills import discovery

    if _changed(target, "signature_metadata", inserting):
        target.provenance_attested = discovery.provenance_attested(target.signature_metadata)


def _register_facet_listeners() -> None:
    from sqlalchemy import event

    for name, inserting in (("before_insert", True), ("before_update", False)):
        event.listen(
            SkillPackageVersion, name,
            lambda _m, _c, target, _i=inserting: _sync_version_facets(target, _i),
        )
        event.listen(
            SkillMarketplaceListing, name,
            lambda _m, _c, target, _i=inserting: _sync_listing_facets(target, _i),
        )


_register_facet_listeners()

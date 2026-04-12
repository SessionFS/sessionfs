"""SQLAlchemy 2.0 ORM models."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Index, Integer, String, Text, ForeignKey, func
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


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


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
    git_remote_normalized: Mapped[str | None] = mapped_column(String(255), nullable=True)
    git_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    git_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    dlp_scan_results: Mapped[str | None] = mapped_column(Text, nullable=True)


class Handoff(Base):
    __tablename__ = "handoffs"
    __table_args__ = (
        Index("idx_handoffs_session_id", "session_id"),
        Index("idx_handoffs_sender_id", "sender_id"),
        Index("idx_handoffs_recipient_email", "recipient_email"),
        Index("idx_handoffs_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id"), nullable=False
    )
    sender_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    recipient_email: Mapped[str] = mapped_column(String(255), nullable=False)
    recipient_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
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

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    git_remote_normalized: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    context_document: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    owner_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id"), nullable=False
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


class OrgMember(Base):
    __tablename__ = "org_members"
    __table_args__ = (
        Index("idx_org_members_org", "org_id"),
        Index("idx_org_members_user", "user_id"),
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
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0")
    source_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    compiled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
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


class ContextCompilation(Base):
    __tablename__ = "context_compilations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entries_compiled: Mapped[int] = mapped_column(Integer, nullable=False)
    context_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_after: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class KnowledgeLink(Base):
    __tablename__ = "knowledge_links"
    __table_args__ = (
        Index("idx_kl_source", "project_id", "source_type", "source_id"),
        Index("idx_kl_target", "project_id", "target_type", "target_id"),
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

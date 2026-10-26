"""Session request/response schemas."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class SessionSummary(BaseModel):
    id: str
    title: str | None = None
    alias: str | None = None
    tags: list[str] = []
    source_tool: str
    model_id: str | None = None
    message_count: int = 0
    turn_count: int = 0
    tool_use_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    blob_size_bytes: int = 0
    etag: str
    parent_session_id: str | None = None
    created_at: datetime
    updated_at: datetime
    is_deleted: bool = False
    deleted_at: datetime | None = None
    deleted_by: str | None = None
    delete_scope: str | None = None
    purge_after: datetime | None = None


class SessionDetail(SessionSummary):
    original_session_id: str | None = None
    source_tool_version: str | None = None
    model_provider: str | None = None
    duration_ms: int | None = None
    parent_session_id: str | None = None
    uploaded_at: datetime
    dlp_scan_results: str | None = None
    retrieval_audit_id: str | None = None


class RestoreResponse(SessionDetail):
    """Response for session restore, includes prior delete scope guidance."""

    restored_from_scope: str | None = None
    local_copy_may_be_missing: bool = False


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]
    total: int
    page: int
    page_size: int
    has_more: bool


class SessionModelCount(BaseModel):
    """One distinct model among the caller's visible sessions.

    ``model_id`` is ``None`` for the aggregated "unknown" bucket (sessions
    with a NULL or empty model id).
    """

    model_id: str | None
    count: int


class SessionModelsResponse(BaseModel):
    models: list[SessionModelCount]


class SessionUploadResponse(BaseModel):
    session_id: str
    etag: str
    blob_size_bytes: int
    uploaded_at: datetime


class SessionMetadataUpdate(BaseModel):
    title: str | None = None
    alias: str | None = None
    tags: list[str] | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if len(v) > 500:
            raise ValueError("Title must be 500 characters or fewer")
        if "\x00" in v:
            raise ValueError("Null bytes not allowed in title")
        v = re.sub(r"<[^>]*>", "", v).strip()
        if not v:
            raise ValueError("Title cannot be empty")
        return v

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        if len(v) > 20:
            raise ValueError("Maximum 20 tags allowed")
        for tag in v:
            if len(tag) > 50:
                raise ValueError("Each tag must be 50 characters or fewer")
            if "\x00" in tag:
                raise ValueError("Null bytes not allowed in tags")
        return v


class SetAliasRequest(BaseModel):
    alias: str

    @field_validator("alias")
    @classmethod
    def validate_alias(cls, v: str) -> str:
        if len(v) < 3:
            raise ValueError("Alias must be at least 3 characters")
        if len(v) > 100:
            raise ValueError("Alias must be 100 characters or fewer")
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", v):
            raise ValueError("Alias must be alphanumeric, hyphens, and underscores only (no spaces), starting with alphanumeric")
        return v


class SyncPushResponse(BaseModel):
    session_id: str
    etag: str
    blob_size_bytes: int
    synced_at: datetime


class SessionUploadInitiateRequest(BaseModel):
    """Initiate a chunked, resumable upload (GitHub #91)."""

    total_size: int
    sha256: str
    part_size: int | None = None


class SessionUploadInitiateResponse(BaseModel):
    upload_id: str
    part_size: int
    part_count: int
    expires_at: datetime


class SessionUploadStatusResponse(BaseModel):
    status: str
    part_size: int
    part_count: int
    received: list[int]
    expires_at: datetime


class MessageTarget(BaseModel):
    """Where ``?around=<n>`` landed.

    ``index`` is the requested 0-based ``messages.jsonl`` line number.
    ``status`` is ``found`` when that message is on the returned page,
    ``hidden`` when it exists but this view filters it out (sidechain or empty),
    and ``missing`` when no message has that line number.  For ``hidden`` and
    ``missing`` the page holds ``anchor_index`` instead — the nearest message the
    view does show — or ``anchor_index`` is null when the view shows nothing.
    """

    index: int
    status: Literal["found", "hidden", "missing"]
    anchor_index: int | None = None


class MessagesResponse(BaseModel):
    messages: list[dict]
    # 0-based ``messages.jsonl`` line number of each entry in ``messages`` —
    # the same index the session helper cites as ``msg:<n>``.
    message_indexes: list[int] = Field(default_factory=list)
    total: int
    page: int
    page_size: int
    has_more: bool
    target: MessageTarget | None = None


class WorkspaceResponse(BaseModel):
    workspace: dict


class ToolsResponse(BaseModel):
    tools: dict


class FileChange(BaseModel):
    path: str
    operation: Literal["created", "edited", "deleted", "unknown"]
    diff: str
    added_lines: int
    removed_lines: int
    message_indexes: list[int]
    truncated: bool = False


class SessionChangesResponse(BaseModel):
    files: list[FileChange]
    git_ref: str | None = None
    truncated: bool = False


class CreateShareLinkRequest(BaseModel):
    expires_in_hours: int = 24
    password: str | None = None


class ShareLinkResponse(BaseModel):
    link_id: str
    url: str
    public_url: str
    expires_at: datetime
    has_password: bool


class SearchMatch(BaseModel):
    snippet: str


class SearchResult(BaseModel):
    session_id: str
    title: str | None
    alias: str | None = None
    source_tool: str
    model_id: str | None
    message_count: int
    updated_at: datetime
    matches: list[SearchMatch]


class SearchResponse(BaseModel):
    results: list[SearchResult]
    total: int
    page: int
    page_size: int
    query: str

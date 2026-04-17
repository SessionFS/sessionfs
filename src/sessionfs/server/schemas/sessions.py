"""Session request/response schemas."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, field_validator


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
        # Strip HTML tags
        v = re.sub(r"<[^>]*>", "", v)
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


class MessagesResponse(BaseModel):
    messages: list[dict]
    total: int
    page: int
    page_size: int
    has_more: bool


class WorkspaceResponse(BaseModel):
    workspace: dict


class ToolsResponse(BaseModel):
    tools: dict


class CreateShareLinkRequest(BaseModel):
    expires_in_hours: int = 24
    password: str | None = None


class ShareLinkResponse(BaseModel):
    link_id: str
    url: str
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

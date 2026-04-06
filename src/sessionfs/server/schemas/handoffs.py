"""Handoff request/response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class CreateHandoffRequest(BaseModel):
    session_id: str
    recipient_email: str = Field(..., max_length=255)
    message: str | None = Field(None, max_length=2000)

    @field_validator("recipient_email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class HandoffResponse(BaseModel):
    id: str
    session_id: str
    recipient_session_id: str | None = None
    sender_email: str
    recipient_email: str
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

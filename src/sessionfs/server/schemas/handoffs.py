"""Handoff request/response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CreateHandoffRequest(BaseModel):
    session_id: str
    recipient_email: str = Field(..., max_length=255)
    message: str | None = Field(None, max_length=2000)


class HandoffResponse(BaseModel):
    id: str
    session_id: str
    sender_email: str
    recipient_email: str
    message: str | None
    status: str
    session_title: str | None
    session_tool: str | None
    created_at: datetime
    expires_at: datetime


class HandoffListResponse(BaseModel):
    handoffs: list[HandoffResponse]
    total: int

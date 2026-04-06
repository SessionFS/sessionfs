"""Auth request/response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator


class SignupRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class SignupResponse(BaseModel):
    user_id: str
    email: str
    raw_key: str
    key_id: str
    message: str = "Account created. Verify your email to enable cloud sync."


class CreateApiKeyRequest(BaseModel):
    name: str | None = None


class CreateApiKeyResponse(BaseModel):
    key_id: str
    raw_key: str
    name: str | None = None
    created_at: datetime


class ApiKeySummary(BaseModel):
    key_id: str
    name: str | None = None
    created_at: datetime
    last_used_at: datetime | None = None

"""Anonymous telemetry collection endpoint."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import TelemetryEvent

router = APIRouter(prefix="/api/v1", tags=["telemetry"])

# Simple PII detection patterns
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_IP_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


class TelemetryPayload(BaseModel):
    install_id: str
    version: str
    os: str
    tools_active: list[str] = []
    sessions_captured_24h: int = 0
    avg_session_size_bytes: int = 0
    features_used: list[str] = []
    errors_24h: int = 0
    tier: str = "free"

    @field_validator("install_id", "version", "os", "tier")
    @classmethod
    def validate_string_length(cls, v: str) -> str:
        if len(v) > 100:
            raise ValueError("Field value too long (max 100 characters)")
        return v

    @field_validator("tools_active", "features_used")
    @classmethod
    def validate_list_length(cls, v: list[str]) -> list[str]:
        if len(v) > 50:
            raise ValueError("List too long (max 50 items)")
        for item in v:
            if len(item) > 100:
                raise ValueError("List item too long (max 100 characters)")
        return v


def _contains_pii(data: TelemetryPayload) -> bool:
    """Check if the payload contains potential PII."""
    text = f"{data.install_id} {data.os} {' '.join(data.tools_active)} {' '.join(data.features_used)}"
    if _EMAIL_RE.search(text):
        return True
    if _IP_RE.search(text):
        return True
    return False


@router.post("/telemetry")
async def collect_telemetry(
    data: TelemetryPayload,
    db: AsyncSession = Depends(get_db),
):
    """Receive anonymous usage telemetry. No auth required. Opt-in only."""
    if _contains_pii(data):
        raise HTTPException(400, "Telemetry rejected: contains potential PII")

    import json

    event = TelemetryEvent(
        install_id=data.install_id,
        version=data.version,
        os=data.os,
        tools_active=json.dumps(data.tools_active),
        sessions_captured_24h=data.sessions_captured_24h,
        avg_session_size_bytes=data.avg_session_size_bytes,
        features_used=json.dumps(data.features_used),
        errors_24h=data.errors_24h,
        tier=data.tier,
    )
    db.add(event)
    await db.commit()

    return {"status": "ok"}

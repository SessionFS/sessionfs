"""Settings routes — user judge LLM configuration."""

from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import User, UserJudgeSettings

logger = logging.getLogger("sessionfs.server.routes.settings")

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


class JudgeSettingsRequest(BaseModel):
    provider: str
    model: str
    api_key: str


class JudgeSettingsResponse(BaseModel):
    provider: str
    model: str
    key_set: bool


def _get_fernet() -> Fernet:
    """Derive a Fernet key from the verification secret."""
    secret = os.environ.get("SFS_VERIFICATION_SECRET", "dev-secret")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


@router.put("/judge", response_model=JudgeSettingsResponse)
async def put_judge_settings(
    body: JudgeSettingsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JudgeSettingsResponse:
    """Store judge LLM settings with encrypted API key."""
    fernet = _get_fernet()
    encrypted_key = fernet.encrypt(body.api_key.encode()).decode()

    stmt = select(UserJudgeSettings).where(UserJudgeSettings.user_id == user.id)
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        existing.provider = body.provider
        existing.model = body.model
        existing.encrypted_api_key = encrypted_key
    else:
        settings = UserJudgeSettings(
            user_id=user.id,
            provider=body.provider,
            model=body.model,
            encrypted_api_key=encrypted_key,
        )
        db.add(settings)

    await db.commit()

    return JudgeSettingsResponse(provider=body.provider, model=body.model, key_set=True)


@router.get("/judge", response_model=JudgeSettingsResponse)
async def get_judge_settings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JudgeSettingsResponse:
    """Return judge settings (never returns the key itself)."""
    stmt = select(UserJudgeSettings).where(UserJudgeSettings.user_id == user.id)
    result = await db.execute(stmt)
    settings = result.scalar_one_or_none()

    if settings is None:
        return JudgeSettingsResponse(provider="", model="", key_set=False)

    return JudgeSettingsResponse(
        provider=settings.provider,
        model=settings.model,
        key_set=True,
    )


@router.delete("/judge")
async def delete_judge_settings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete judge settings."""
    stmt = delete(UserJudgeSettings).where(UserJudgeSettings.user_id == user.id)
    await db.execute(stmt)
    await db.commit()
    return {"deleted": True}

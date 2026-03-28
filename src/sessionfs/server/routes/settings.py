"""Settings routes — user judge LLM configuration."""

from __future__ import annotations

import base64
import hashlib
import logging
import os

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import GitHubInstallation, User, UserJudgeSettings

logger = logging.getLogger("sessionfs.server.routes.settings")

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


class JudgeSettingsRequest(BaseModel):
    provider: str
    model: str
    api_key: str = ""
    base_url: str | None = None


class JudgeSettingsResponse(BaseModel):
    provider: str
    model: str
    key_set: bool
    base_url: str | None = None


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
        existing.base_url = body.base_url
    else:
        settings = UserJudgeSettings(
            user_id=user.id,
            provider=body.provider,
            model=body.model,
            encrypted_api_key=encrypted_key,
            base_url=body.base_url,
        )
        db.add(settings)

    await db.commit()

    return JudgeSettingsResponse(provider=body.provider, model=body.model, key_set=True, base_url=body.base_url)


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
        base_url=settings.base_url,
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


@router.get("/judge/models")
async def discover_models(
    base_url: str = Query(..., description="OpenAI-compatible endpoint URL"),
    api_key: str = Query("", description="API key (optional for local endpoints)"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Discover available models from an OpenAI-compatible endpoint.

    Queries the /v1/models (or /models) endpoint and returns the model list.
    If no explicit API key provided, tries the user's saved key.
    """
    # Fall back to saved key if none provided
    if not api_key:
        stmt = select(UserJudgeSettings).where(UserJudgeSettings.user_id == user.id)
        result = await db.execute(stmt)
        settings = result.scalar_one_or_none()
        if settings and settings.encrypted_api_key:
            try:
                fernet = _get_fernet()
                api_key = fernet.decrypt(settings.encrypted_api_key.encode()).decode()
            except Exception:
                pass

    url = base_url.rstrip("/")
    if not url.endswith("/models"):
        if url.endswith("/v1"):
            url = f"{url}/models"
        else:
            url = f"{url}/v1/models"

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)

        if resp.status_code == 404:
            # Some endpoints use /models without /v1 prefix
            alt_url = base_url.rstrip("/") + "/models"
            if alt_url != url:
                resp = await httpx.AsyncClient(timeout=15).__aenter__()
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(alt_url, headers=headers)

        if resp.status_code >= 400:
            return {"models": [], "error": f"Endpoint returned {resp.status_code}"}

        data = resp.json()
        models_list = data.get("data", data.get("models", []))

        models = []
        for m in models_list:
            if isinstance(m, dict):
                model_id = m.get("id", m.get("model", ""))
                if model_id:
                    models.append({
                        "id": model_id,
                        "owned_by": m.get("owned_by", ""),
                    })
            elif isinstance(m, str):
                models.append({"id": m, "owned_by": ""})

        return {"models": models, "base_url": base_url}

    except httpx.TimeoutException:
        return {"models": [], "error": "Connection timed out"}
    except Exception as e:
        logger.warning("Model discovery failed for %s: %s", base_url, e)
        return {"models": [], "error": str(e)}


# --- GitHub installation settings ---


class GitHubInstallationResponse(BaseModel):
    account_login: str | None
    account_type: str | None
    auto_comment: bool
    include_trust_score: bool
    include_session_links: bool


class GitHubInstallationUpdate(BaseModel):
    auto_comment: bool | None = None
    include_trust_score: bool | None = None
    include_session_links: bool | None = None


@router.get("/github", response_model=GitHubInstallationResponse)
async def get_github_installation(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubInstallationResponse:
    """Return GitHub installation settings for the current user."""
    stmt = select(GitHubInstallation).where(GitHubInstallation.user_id == user.id)
    result = await db.execute(stmt)
    inst = result.scalar_one_or_none()

    if inst is None:
        return GitHubInstallationResponse(
            account_login=None,
            account_type=None,
            auto_comment=True,
            include_trust_score=True,
            include_session_links=True,
        )

    return GitHubInstallationResponse(
        account_login=inst.account_login,
        account_type=inst.account_type,
        auto_comment=inst.auto_comment,
        include_trust_score=inst.include_trust_score,
        include_session_links=inst.include_session_links,
    )


@router.put("/github", response_model=GitHubInstallationResponse)
async def update_github_installation(
    body: GitHubInstallationUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GitHubInstallationResponse:
    """Update GitHub installation preferences."""
    stmt = select(GitHubInstallation).where(GitHubInstallation.user_id == user.id)
    result = await db.execute(stmt)
    inst = result.scalar_one_or_none()

    if inst is None:
        return GitHubInstallationResponse(
            account_login=None,
            account_type=None,
            auto_comment=True,
            include_trust_score=True,
            include_session_links=True,
        )

    if body.auto_comment is not None:
        inst.auto_comment = body.auto_comment
    if body.include_trust_score is not None:
        inst.include_trust_score = body.include_trust_score
    if body.include_session_links is not None:
        inst.include_session_links = body.include_session_links

    await db.commit()
    await db.refresh(inst)

    return GitHubInstallationResponse(
        account_login=inst.account_login,
        account_type=inst.account_type,
        auto_comment=inst.auto_comment,
        include_trust_score=inst.include_trust_score,
        include_session_links=inst.include_session_links,
    )

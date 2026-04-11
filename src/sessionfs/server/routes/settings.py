"""Settings routes — user judge LLM configuration."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from datetime import datetime, timezone

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import GitHubInstallation, GitLabSettings, User, UserJudgeSettings

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


# ---- Audit Trigger Settings ----


class AuditTriggerRequest(BaseModel):
    trigger: str  # "manual", "on_sync", "on_pr"


@router.get("/audit-trigger")
async def get_audit_trigger(
    user: User = Depends(get_current_user),
) -> dict:
    """Get user's auto-audit trigger setting."""
    return {"trigger": user.audit_trigger or "manual"}


@router.put("/audit-trigger")
async def update_audit_trigger(
    body: AuditTriggerRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update auto-audit trigger."""
    if body.trigger not in ("manual", "on_sync", "on_pr"):
        from fastapi import HTTPException
        raise HTTPException(400, "Trigger must be 'manual', 'on_sync', or 'on_pr'")
    user.audit_trigger = body.trigger
    await db.commit()
    return {"trigger": body.trigger}


# ---- Summarize Trigger Settings ----


@router.get("/summarize-trigger")
async def get_summarize_trigger(
    user: User = Depends(get_current_user),
) -> dict:
    """Get user's auto-summarize trigger setting."""
    return {"trigger": getattr(user, "summarize_trigger", "manual") or "manual"}


@router.put("/summarize-trigger")
async def update_summarize_trigger(
    body: AuditTriggerRequest,  # Same schema: {trigger: str}
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update auto-summarize trigger."""
    if body.trigger not in ("manual", "on_sync", "on_pr"):
        from fastapi import HTTPException
        raise HTTPException(400, "Trigger must be 'manual', 'on_sync', or 'on_pr'")
    user.summarize_trigger = body.trigger
    await db.commit()
    return {"trigger": body.trigger}


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
    # Required when claiming an installation for the first time. The user's
    # browser receives this value as ?installation_id=... in the GitHub App
    # Setup URL callback after the user authorizes the app. Without it, the
    # server has no way to prove which installation the user owns, so blind
    # "claim the first unclaimed row" IDOR is refused.
    installation_id: int | None = None


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
        # Claim an installation the user just authorized via the GitHub App
        # Setup URL callback. We require a specific installation_id from the
        # client (parsed from GitHub's redirect query string) AND a recent
        # webhook-created row — this closes the blind "first unclaimed wins"
        # IDOR that previously let any authenticated user claim any pending
        # installation.
        if body.installation_id is None:
            # Nothing to claim, and no specific installation specified.
            # Return defaults so the dashboard can still render preferences.
            return GitHubInstallationResponse(
                account_login=None,
                account_type=None,
                auto_comment=body.auto_comment if body.auto_comment is not None else True,
                include_trust_score=body.include_trust_score if body.include_trust_score is not None else True,
                include_session_links=body.include_session_links if body.include_session_links is not None else True,
            )

        target_result = await db.execute(
            select(GitHubInstallation).where(GitHubInstallation.id == body.installation_id)
        )
        target = target_result.scalar_one_or_none()

        if target is None:
            raise HTTPException(
                404,
                detail=(
                    "Installation not found. Make sure the GitHub App is installed "
                    "and the webhook has delivered the installation event to SessionFS."
                ),
            )

        if target.user_id is not None and target.user_id != user.id:
            # Already claimed by someone else. Don't reveal by whom.
            raise HTTPException(403, detail="Installation already claimed")

        if target.user_id == user.id:
            # Idempotent: user is re-claiming their own installation.
            inst = target
        else:
            # Only allow claims within a short window after the webhook
            # created the row. GitHub installation IDs are predictable
            # integers, so an unbounded claim window makes brute-force
            # trivial even with a specific ID required.
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            created = target.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            max_age = timedelta(minutes=15)
            if created is None or (now - created) > max_age:
                raise HTTPException(
                    410,
                    detail=(
                        "Installation claim window expired. Re-install the "
                        "GitHub App to generate a fresh claim."
                    ),
                )
            target.user_id = user.id
            inst = target

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


# --- GitLab integration settings ---


class GitLabSettingsResponse(BaseModel):
    instance_url: str
    has_token: bool
    webhook_secret: str | None


class GitLabSettingsRequest(BaseModel):
    instance_url: str = "https://gitlab.com"
    access_token: str
    webhook_secret: str | None = None


@router.get("/gitlab", response_model=GitLabSettingsResponse)
async def get_gitlab_settings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GitLabSettingsResponse:
    """Return GitLab integration settings for the current user."""
    result = await db.execute(
        select(GitLabSettings).where(GitLabSettings.user_id == user.id)
    )
    settings = result.scalar_one_or_none()
    if settings is None:
        return GitLabSettingsResponse(
            instance_url="https://gitlab.com",
            has_token=False,
            webhook_secret=None,
        )
    return GitLabSettingsResponse(
        instance_url=settings.instance_url,
        has_token=bool(settings.encrypted_access_token),
        webhook_secret=settings.webhook_secret,
    )


@router.put("/gitlab", response_model=GitLabSettingsResponse)
async def update_gitlab_settings(
    body: GitLabSettingsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GitLabSettingsResponse:
    """Create or update GitLab integration settings."""
    import base64
    import hashlib as _hashlib
    import secrets as _secrets

    from cryptography.fernet import Fernet

    secret = os.environ.get("SFS_VERIFICATION_SECRET", "dev-secret")
    key = base64.urlsafe_b64encode(_hashlib.sha256(secret.encode()).digest())
    fernet = Fernet(key)
    encrypted = fernet.encrypt(body.access_token.encode()).decode()

    result = await db.execute(
        select(GitLabSettings).where(GitLabSettings.user_id == user.id)
    )
    settings = result.scalar_one_or_none()
    webhook_secret = body.webhook_secret or _secrets.token_hex(16)

    if settings is None:
        settings = GitLabSettings(
            user_id=user.id,
            instance_url=body.instance_url,
            encrypted_access_token=encrypted,
            webhook_secret=webhook_secret,
        )
        db.add(settings)
    else:
        settings.instance_url = body.instance_url
        settings.encrypted_access_token = encrypted
        if body.webhook_secret:
            settings.webhook_secret = body.webhook_secret

    await db.commit()
    await db.refresh(settings)

    return GitLabSettingsResponse(
        instance_url=settings.instance_url,
        has_token=True,
        webhook_secret=settings.webhook_secret,
    )


@router.delete("/gitlab", status_code=204)
async def delete_gitlab_settings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove GitLab integration settings."""
    await db.execute(
        delete(GitLabSettings).where(GitLabSettings.user_id == user.id)
    )
    await db.commit()

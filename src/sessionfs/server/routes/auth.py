"""Auth key management routes."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter

# Rate limit signups: 5 per IP per hour
_signup_limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=3600)
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pydantic import BaseModel

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import ApiKey, OrgMember, User
from sessionfs.server.schemas.auth import (
    ApiKeySummary,
    CreateApiKeyRequest,
    CreateApiKeyResponse,
    SignupRequest,
    SignupResponse,
)

SFS_VERIFICATION_SECRET = os.environ.get("SFS_VERIFICATION_SECRET", "dev-verification-secret")

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.get("/me")
async def get_me(user: User = Depends(get_current_user)):
    """Return the authenticated user's profile."""
    from sessionfs import __version__ as server_version

    return {
        "user_id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "email_verified": user.email_verified,
        "tier": user.tier,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_client_version": user.last_client_version,
        "last_client_platform": user.last_client_platform,
        "last_client_device": user.last_client_device,
        "last_sync_at": user.last_sync_at.isoformat() if user.last_sync_at else None,
        "latest_version": server_version,
        # v0.10.0 Phase 5 — `sfs project init` reads this when neither
        # --org nor --personal is passed to pick the new project's
        # scope. Server-side session sync routing does not consume it
        # today (uses git remote → Project lookup); a v0.10.x follow-up
        # may add a default-org fallback for unmatched remotes.
        "default_org_id": user.default_org_id,
    }


class SetDefaultOrgRequest(BaseModel):
    """Body for PUT /api/v1/auth/me/default-org.

    Passing `org_id=None` clears the preference (user falls back to
    personal scope for untracked workspaces). Passing a string requires
    the caller to be a member of that org — admin or plain.
    """

    org_id: str | None = None


@router.put("/me/default-org")
async def set_default_org(
    body: SetDefaultOrgRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Set the authenticated user's default org.

    Validation:
      - If `org_id` is None, clear the pointer (no membership check).
      - If `org_id` is a string, the user MUST be an active member of
        that org. The membership SELECT both validates existence AND
        access in a single shape — non-members and unknown org ids
        both surface as 403 "You are not a member of that org" (no
        404 leakage of which org ids exist).

    Phase 5 (v0.10.0) — surface consumed by the `sfs config default-org`
    CLI command. A dashboard default-org widget is planned but not
    shipped in v0.10.0; it would call this same endpoint.
    """
    if body.org_id is not None:
        membership = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.user_id == user.id,
                    OrgMember.org_id == body.org_id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(
                403, "You are not a member of that org"
            )
    user.default_org_id = body.org_id
    await db.commit()
    await db.refresh(user)
    return {"default_org_id": user.default_org_id}


@router.post("/signup", response_model=SignupResponse, status_code=201)
async def signup(
    body: SignupRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create a new user account and return the first API key.

    This is the only unauthenticated endpoint. It creates a user and
    generates the initial API key needed for all other operations.
    """
    # Rate limit by IP
    client_ip = request.client.host if request.client else "unknown"
    if not _signup_limiter.is_allowed(client_ip):
        raise HTTPException(429, "Too many signup attempts. Try again later.")

    # Check if email already exists
    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Email already registered")

    # Create user
    user_id = str(uuid.uuid4())
    user = User(id=user_id, email=body.email)
    db.add(user)
    await db.flush()  # Ensure user row exists before FK reference

    # Create first API key
    raw_key = generate_api_key()
    key_id = str(uuid.uuid4())
    api_key = ApiKey(
        id=key_id,
        user_id=user_id,
        key_hash=hash_api_key(raw_key),
        name="default",
    )
    db.add(api_key)

    await db.commit()

    # Check if email verification is required
    require_verification = os.environ.get(
        "SFS_REQUIRE_EMAIL_VERIFICATION", "true"
    ).lower() == "true"

    if require_verification:
        # Generate verification JWT
        verification_payload = {
            "user_id": user_id,
            "email": body.email,
            "exp": datetime.now(timezone.utc) + timedelta(hours=24),
        }
        verification_token = jwt.encode(
            verification_payload, SFS_VERIFICATION_SECRET, algorithm="HS256"
        )
        verification_link = (
            f"https://api.sessionfs.dev/api/v1/auth/verify?token={verification_token}"
        )

        # Send verification email if email service is available
        email_service = getattr(request.app.state, "email_service", None)
        if email_service is not None:
            await email_service.send_verification(body.email, verification_link)

        message = "Account created. Verify your email to enable cloud sync."
    else:
        # Auto-verify in environments without email
        user.email_verified = True
        await db.commit()
        message = "Account created and verified."

    return SignupResponse(
        user_id=user_id,
        email=body.email,
        raw_key=raw_key,
        key_id=key_id,
        message=message,
    )


@router.get("/verify", response_class=HTMLResponse)
async def verify_email(
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Verify a user's email address via JWT token."""
    try:
        payload = jwt.decode(token, SFS_VERIFICATION_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return HTMLResponse(
            content="<html><body><h1>Verification Failed</h1>"
            "<p>This verification link has expired. Please request a new one.</p>"
            "</body></html>",
            status_code=400,
        )
    except jwt.InvalidTokenError:
        return HTMLResponse(
            content="<html><body><h1>Verification Failed</h1>"
            "<p>Invalid verification link.</p>"
            "</body></html>",
            status_code=400,
        )

    user_id = payload.get("user_id")
    if not user_id:
        return HTMLResponse(
            content="<html><body><h1>Verification Failed</h1>"
            "<p>Invalid verification token payload.</p>"
            "</body></html>",
            status_code=400,
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        return HTMLResponse(
            content="<html><body><h1>Verification Failed</h1>"
            "<p>User not found.</p>"
            "</body></html>",
            status_code=404,
        )

    user.email_verified = True
    await db.commit()

    return HTMLResponse(
        content="<html><body><h1>Email Verified!</h1>"
        "<p>Email verified! You can now sync sessions to the cloud.</p>"
        "</body></html>",
        status_code=200,
    )


@router.post("/keys", response_model=CreateApiKeyResponse, status_code=201)
async def create_api_key(
    body: CreateApiKeyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new API key. The raw key is returned only once."""
    raw_key = generate_api_key()
    key_id = str(uuid.uuid4())

    api_key = ApiKey(
        id=key_id,
        user_id=user.id,
        key_hash=hash_api_key(raw_key),
        name=body.name,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return CreateApiKeyResponse(
        key_id=api_key.id,
        raw_key=raw_key,
        name=api_key.name,
        created_at=api_key.created_at,
    )


@router.get("/keys", response_model=list[ApiKeySummary])
async def list_api_keys(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all active API keys for the current user."""
    result = await db.execute(
        select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.is_active == True)  # noqa: E712
    )
    keys = result.scalars().all()
    return [
        ApiKeySummary(
            key_id=k.id,
            name=k.name,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
        )
        for k in keys
    ]


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke (soft deactivate) an API key."""
    result = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    api_key.is_active = False
    await db.commit()

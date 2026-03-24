"""FastAPI authentication dependencies."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import hash_api_key
from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import ApiKey, User

_rate_limiter: SlidingWindowRateLimiter | None = None


def get_rate_limiter() -> SlidingWindowRateLimiter:
    """Return the global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = SlidingWindowRateLimiter(max_requests=100)
    return _rate_limiter


def set_rate_limiter(limiter: SlidingWindowRateLimiter) -> None:
    """Override the rate limiter (for testing)."""
    global _rate_limiter
    _rate_limiter = limiter


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    limiter: SlidingWindowRateLimiter = Depends(get_rate_limiter),
) -> User:
    """Authenticate via Bearer token and return the current user."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    raw_key = auth_header[7:]
    key_hash = hash_api_key(raw_key)

    # Rate limit by key hash
    if not limiter.is_allowed(key_hash):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    # Look up key
    result = await db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
    api_key = result.scalar_one_or_none()
    if api_key is None or not api_key.is_active:
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Look up user
    result = await db.execute(select(User).where(User.id == api_key.user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=403, detail="User account is inactive")

    # Update last_used_at
    await db.execute(
        update(ApiKey).where(ApiKey.id == api_key.id).values(
            last_used_at=datetime.now(timezone.utc)
        )
    )
    await db.commit()

    return user


async def require_admin(
    user: User = Depends(get_current_user),
) -> User:
    """Require that the authenticated user has admin tier."""
    if user.tier != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def require_verified_user(
    user: User = Depends(get_current_user),
) -> User:
    """Require that the authenticated user has verified their email."""
    if not user.email_verified:
        raise HTTPException(
            status_code=403,
            detail="Email not verified. Check your inbox for the verification link.",
        )
    return user

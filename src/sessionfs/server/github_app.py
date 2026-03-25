"""GitHub App integration -- installation tokens, comment posting, remote normalization."""

from __future__ import annotations

import logging
import os
import re
import time

import httpx
import jwt  # PyJWT

logger = logging.getLogger("sessionfs.github_app")

GITHUB_API = "https://api.github.com"
# App credentials from environment / Secret Manager
GITHUB_APP_ID = os.environ.get("SFS_GITHUB_APP_ID", "")
GITHUB_APP_PRIVATE_KEY = os.environ.get("SFS_GITHUB_APP_PRIVATE_KEY", "")
GITHUB_WEBHOOK_SECRET = os.environ.get("SFS_GITHUB_WEBHOOK_SECRET", "")

# Cache installation tokens (they last 1 hour)
_token_cache: dict[int, tuple[str, float]] = {}


def normalize_git_remote(url: str) -> str:
    """Normalize git remote URL to 'owner/repo' format (lowercase).

    git@github.com:SessionFS/sessionfs.git -> sessionfs/sessionfs
    https://github.com/SessionFS/sessionfs.git -> sessionfs/sessionfs
    https://github.com/SessionFS/sessionfs -> sessionfs/sessionfs
    """
    if not url:
        return ""
    url = url.strip()
    # SSH format: git@github.com:owner/repo.git
    m = re.match(r"git@[^:]+:(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1).lower()
    # HTTPS format
    m = re.match(r"https?://[^/]+/(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1).lower()
    return url.lower()


async def get_installation_token(installation_id: int) -> str:
    """Get or refresh a GitHub App installation token."""
    now = time.time()
    cached = _token_cache.get(installation_id)
    if cached and cached[1] > now + 300:  # 5 min buffer
        return cached[0]

    # Generate JWT for the App
    payload = {
        "iat": int(now) - 60,
        "exp": int(now) + 600,  # 10 min
        "iss": GITHUB_APP_ID,
    }
    app_jwt = jwt.encode(payload, GITHUB_APP_PRIVATE_KEY, algorithm="RS256")

    # Exchange for installation token
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    token = data["token"]
    expires_at = time.time() + 3600  # tokens last 1 hour
    _token_cache[installation_id] = (token, expires_at)
    return token


async def post_or_update_comment(
    token: str,
    repo: str,
    pr_number: int,
    body: str,
    existing_comment_id: int | None = None,
) -> int:
    """Post a new comment or update an existing one. Returns comment ID."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient() as client:
        if existing_comment_id:
            resp = await client.patch(
                f"{GITHUB_API}/repos/{repo}/issues/comments/{existing_comment_id}",
                headers=headers,
                json={"body": body},
            )
        else:
            resp = await client.post(
                f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments",
                headers=headers,
                json={"body": body},
            )
        resp.raise_for_status()
        return resp.json()["id"]

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


async def verify_repo_ownership(
    db,
    user_id: str,
    owner: str,
    repo: str,
) -> tuple[bool, str | None, str | None, str | None]:
    """Verify repo ownership via the user's OWN GitHub App installations.

    Sentinel N1 (confused-deputy defense): scoped to the authenticated
    user's own installations only — never a global token or another
    user's installation. A user who installed the App on repo-A cannot
    use that installation's token to "verify" repo-B.

    Sentinel N2 (liveness): this function makes live HTTP calls to the
    GitHub API and is designed to be called OUTSIDE any swap transaction.
    The caller acquires DB locks only after this returns.

    Returns (verified, verification_method, provider, provider_repo_id).
    - verified=True, method='github_app' when an installation token can
      access the repo AND the repo metadata is returned (200).
    - verified=False, method='owner_attested' when no installation covers
      this repo OR the GitHub API call fails for any reason.
    """
    from sqlalchemy import select

    from sessionfs.server.db.models import GitHubInstallation

    if not GITHUB_APP_ID or not GITHUB_APP_PRIVATE_KEY:
        return (False, "owner_attested", None, None)

    # Find installations belonging to this user (N1: linker's OWN
    # installations only — never cross-user).
    result = await db.execute(
        select(GitHubInstallation).where(
            GitHubInstallation.user_id == user_id,
        )
    )
    installations = result.scalars().all()

    if not installations:
        return (False, "owner_attested", None, None)

    for install in installations:
        try:
            token = await get_installation_token(install.id)
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{GITHUB_API}/repos/{owner}/{repo}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return (
                        True,
                        "github_app",
                        "github",
                        str(data.get("id", "")),
                    )
        except Exception:
            # Best-effort per installation — try the next one
            continue

    # No installation could verify this repo
    return (False, "owner_attested", None, None)


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

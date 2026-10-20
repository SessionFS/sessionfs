"""GitHub App integration -- installation tokens, comment posting, remote normalization."""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

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


# Moved to the dependency-free `sessionfs.git_remote` (the CLI must not import
# this module, which needs PyJWT); re-exported for existing server imports.
from sessionfs.git_remote import normalize_git_remote  # noqa: E402,F401


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


async def get_verified_installations(
    db,
    user_id: str,
    *,
    require_admin: bool = False,
    max_age: timedelta | None = None,
) -> list:
    """Installations this user has PROVEN they control (via GitHub OAuth).

    The single trust gate for anything that acts on "the user's own"
    installation (repo verification, publisher verification). Legacy claims
    (``claim_verified_at IS NULL``) are never returned.

    require_admin: only claims where GitHub showed the user is the account
        itself (user installation) or an active admin of the org — use this
        for anything that speaks for the org, such as a verified-publisher
        badge.
    max_age: only proofs newer than this (org admin rights can be revoked
        on GitHub without SessionFS hearing about it; callers granting
        long-lived trust should require a recent proof).
    """
    from sqlalchemy import select

    from sessionfs.server.db.models import GitHubInstallation
    from sessionfs.server.services.github_oauth import PRIVILEGED_ROLES

    stmt = select(GitHubInstallation).where(
        GitHubInstallation.user_id == user_id,
        GitHubInstallation.claim_verified_at.is_not(None),
    )
    if require_admin:
        stmt = stmt.where(GitHubInstallation.claim_role.in_(sorted(PRIVILEGED_ROLES)))
    if max_age is not None:
        stmt = stmt.where(
            GitHubInstallation.claim_verified_at >= datetime.now(timezone.utc) - max_age
        )
    result = await db.execute(stmt.order_by(GitHubInstallation.id))
    return list(result.scalars().all())


async def installation_owned_by(
    db,
    user_id: str,
    installation_id: int,
    *,
    require_admin: bool = False,
    max_age: timedelta | None = None,
) -> bool:
    """True only when ``user_id`` holds a VERIFIED claim on the installation.

    Verified means the claim was made (or re-verified) through the GitHub
    OAuth flow, which sets ``claim_verified_at`` and
    ``claim_verified_github_user_id``. Legacy claims (NULL marker) return
    False. ``require_admin`` additionally demands that GitHub showed the
    claimant to be the account itself or an active org admin; ``max_age``
    demands a proof no older than the given age.
    """
    if isinstance(installation_id, bool) or not isinstance(installation_id, int):
        return False
    from sqlalchemy import select

    from sessionfs.server.db.models import GitHubInstallation
    from sessionfs.server.services.github_oauth import PRIVILEGED_ROLES

    stmt = select(GitHubInstallation.id).where(
        GitHubInstallation.id == installation_id,
        GitHubInstallation.user_id == user_id,
        GitHubInstallation.claim_verified_at.is_not(None),
        GitHubInstallation.claim_verified_github_user_id.is_not(None),
    )
    if require_admin:
        stmt = stmt.where(GitHubInstallation.claim_role.in_(sorted(PRIVILEGED_ROLES)))
    if max_age is not None:
        stmt = stmt.where(
            GitHubInstallation.claim_verified_at >= datetime.now(timezone.utc) - max_age
        )
    result = await db.execute(stmt.limit(1))
    return result.scalar_one_or_none() is not None


_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def github_owner_repo_from_remote(raw_remote: str) -> tuple[str, str] | None:
    """Return (owner, repo) only for an unambiguous github.com remote.

    The normalized remote (``normalize_git_remote``) drops the host, so it
    cannot tell ``gitlab.com/group/repo`` from ``github.com/group/repo``.
    Repository verification therefore looks at the RAW remote and accepts
    only a github.com host with exactly two path segments. Anything else —
    another host, no host at all, a GitLab-style subgroup path
    (``group/sub/repo``) — returns None and is never checked against
    GitHub.
    """
    if not isinstance(raw_remote, str):
        return None
    url = raw_remote.strip()
    if not url:
        return None
    host: str | None = None
    path: str | None = None
    m = re.match(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!//)(.+)$", url)
    if m and "://" not in url:
        # scp-like SSH form: [user@]host:owner/repo(.git)
        host, path = m.group(1), m.group(2)
    else:
        parsed_url = url if "://" in url else f"//{url}"
        try:
            parsed = urlsplit(parsed_url)
            hostname = parsed.hostname
            if parsed.port is not None and parsed.scheme in ("http", "https"):
                # GitHub never serves repositories on a custom port.
                return None
        except ValueError:
            return None
        if parsed.scheme not in ("", "http", "https", "ssh", "git"):
            return None
        if parsed.query or parsed.fragment:
            return None
        host, path = hostname, parsed.path
    if not host or host.lower() not in _GITHUB_HOSTS or path is None:
        return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = path.split("/")
    if len(parts) != 2:
        return None
    owner, repo = parts
    if not owner or not repo:
        return None
    if not _REPO_SEGMENT_RE.match(owner) or not _REPO_SEGMENT_RE.match(repo):
        return None
    if owner in (".", "..") or repo in (".", ".."):
        return None
    return owner, repo


# Hard cap on GET /installation/repositories pages (100 repos per page).
_MAX_INSTALLATION_REPO_PAGES = 50


def _has_next_link(link_header: str | None) -> bool:
    if not link_header:
        return False
    return any('rel="next"' in part for part in link_header.split(","))


async def _find_installation_repo(token: str, full_name: str) -> dict | None:
    """Return the installation's repository entry whose full_name matches.

    Only repositories actually granted to the installation are listed by
    ``GET /installation/repositories`` — unlike ``GET /repos/{o}/{r}``,
    which an installation token can call for ANY public repository.
    Raises on any HTTP / shape error so callers fail closed.
    """
    wanted = full_name.lower()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    seen = 0
    async with httpx.AsyncClient(timeout=15) as client:
        for page in range(1, _MAX_INSTALLATION_REPO_PAGES + 1):
            resp = await client.get(
                f"{GITHUB_API}/installation/repositories",
                headers=headers,
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("unexpected installation repositories payload")
            repos = data.get("repositories")
            if not isinstance(repos, list):
                raise ValueError("unexpected installation repositories payload")
            for entry in repos:
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("full_name"), str)
                    and entry["full_name"].lower() == wanted
                ):
                    return entry
            seen += len(repos)
            total = data.get("total_count")
            if not repos:
                return None
            if isinstance(total, int) and not isinstance(total, bool) and seen >= total:
                return None
            if not _has_next_link(resp.headers.get("link")) and len(repos) < 100:
                return None
    logger.warning(
        "installation repository listing exceeded %d pages; not verified",
        _MAX_INSTALLATION_REPO_PAGES,
    )
    return None


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

    ``owner``/``repo`` must come from ``github_owner_repo_from_remote``
    (a github.com remote with exactly two path segments).

    Returns (verified, verification_method, provider, provider_repo_id).
    - verified=True, method='github_app' only when the repository is in
      the installation's GRANT (listed by GET /installation/repositories).
      Readability alone proves nothing: an installation token can read
      every public repository on GitHub.
    - verified=False, method='owner_attested' when no installation grants
      this repo OR the GitHub API call fails for any reason.
    """
    if not GITHUB_APP_ID or not GITHUB_APP_PRIVATE_KEY:
        return (False, "owner_attested", None, None)
    if (
        not isinstance(owner, str)
        or not isinstance(repo, str)
        or not _REPO_SEGMENT_RE.match(owner)
        or not _REPO_SEGMENT_RE.match(repo)
    ):
        return (False, "owner_attested", None, None)

    # Find installations belonging to this user (N1: linker's OWN
    # installations only — never cross-user). An installation token covers
    # EVERY repository granted to the App, so only a recent, live proof of
    # control over the whole installation counts: the claimant is the user
    # account itself or an active org admin, proven within
    # CLAIM_PROOF_MAX_AGE. Member, installer, legacy and stale claims fall
    # back to owner_attested.
    from sessionfs.server.services.github_oauth import CLAIM_PROOF_MAX_AGE

    installations = await get_verified_installations(
        db, user_id, require_admin=True, max_age=CLAIM_PROOF_MAX_AGE
    )
    installation_ids = [install.id for install in installations]
    # End the read transaction BEFORE any GitHub call so no connection or
    # row lock is held across the network. Callers must not have pending
    # writes (link_repo defers its mutations until after verification).
    await db.commit()

    if not installation_ids:
        return (False, "owner_attested", None, None)

    full_name = f"{owner}/{repo}"
    for installation_id in installation_ids:
        try:
            token = await get_installation_token(installation_id)
            entry = await _find_installation_repo(token, full_name)
        except Exception:
            # Best-effort per installation — try the next one
            continue
        if entry is None:
            continue
        repo_id = entry.get("id")
        if isinstance(repo_id, bool) or not isinstance(repo_id, int):
            continue
        return (True, "github_app", "github", str(repo_id))

    # No installation grants this repo
    return (False, "owner_attested", None, None)


_MAX_COMMENT_PAGES = 10


def _is_own_app_comment(comment: dict) -> bool:
    """Only adopt comments this App posted (never a user's comment that
    happens to quote the marker: the App could not edit it anyway)."""
    via = comment.get("performed_via_github_app")
    if isinstance(via, dict) and GITHUB_APP_ID.isdigit():
        return via.get("id") == int(GITHUB_APP_ID)
    if GITHUB_APP_ID.isdigit():
        # The App id is known but this comment carries no matching
        # performed_via_github_app payload, so it was posted by something
        # else (a user, or another bot/app). Never adopt it — the Bot-type
        # fallback below is only for when the App id is not configured.
        return False
    user = comment.get("user")
    return isinstance(user, dict) and user.get("type") == "Bot"


async def find_marked_comment(token: str, repo: str, pr_number: int, marker: str) -> int | None:
    """Return the id of this App's PR comment carrying ``marker``, if any."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        for page in range(1, _MAX_COMMENT_PAGES + 1):
            resp = await client.get(
                f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments",
                headers=headers,
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            comments = resp.json()
            if not isinstance(comments, list):
                return None
            for comment in comments:
                if (
                    isinstance(comment, dict)
                    and isinstance(comment.get("body"), str)
                    and marker in comment["body"]
                    and _is_own_app_comment(comment)
                    and isinstance(comment.get("id"), int)
                ):
                    return comment["id"]
            if len(comments) < 100:
                return None
    return None


def _app_jwt() -> str:
    now = int(time.time())
    return jwt.encode(
        {"iat": now - 60, "exp": now + 600, "iss": GITHUB_APP_ID},
        GITHUB_APP_PRIVATE_KEY,
        algorithm="RS256",
    )


async def get_installation_account(installation_id: int) -> dict | None:
    """Live lookup of a GitHub App installation's account, as the App.

    Returns ``{"id": int, "login": str, "type": "User"|"Organization",
    "suspended": bool, "repository_selection": str}`` or None when the
    installation no longer exists (404). Raises on any other failure so a
    transient GitHub outage is never mistaken for an uninstall. Used by
    marketplace publisher verification: the NUMERIC account id is the
    verified subject, never the login (which can be renamed and re-registered).
    """
    if not GITHUB_APP_ID or not GITHUB_APP_PRIVATE_KEY:
        raise RuntimeError("GitHub App is not configured")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{GITHUB_API}/app/installations/{int(installation_id)}",
            headers={
                "Authorization": f"Bearer {_app_jwt()}",
                "Accept": "application/vnd.github+json",
            },
        )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    account = data.get("account") or {}
    account_id = account.get("id")
    if not isinstance(account_id, int):
        raise RuntimeError("GitHub installation response has no numeric account id")
    return {
        "id": account_id,
        "login": str(account.get("login") or ""),
        "type": str(account.get("type") or ""),
        "suspended": data.get("suspended_at") is not None,
        "repository_selection": str(data.get("repository_selection") or ""),
    }


async def resolve_repository(db, user_id: str, owner: str, repo: str) -> dict | None:
    """Resolve ``owner/repo`` to its numeric ids for trusted publishing.

    Tries the caller's OWN installations first (works for private repos;
    Sentinel N1), then the unauthenticated public endpoint. Returns
    ``{"id", "owner_id", "full_name"}`` or None when the repository cannot be
    seen. The numeric ids are what trusted publishing binds to."""
    from sqlalchemy import select

    from sessionfs.server.db.models import GitHubInstallation

    paths = f"{GITHUB_API}/repos/{owner}/{repo}"
    attempts: list[dict[str, str]] = []
    if GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY:
        installations = (
            await db.execute(
                select(GitHubInstallation.id).where(GitHubInstallation.user_id == user_id)
            )
        ).scalars().all()
        for installation_id in installations:
            try:
                token = await get_installation_token(installation_id)
            except Exception:
                continue
            attempts.append({"Authorization": f"Bearer {token}"})
    attempts.append({})
    async with httpx.AsyncClient(timeout=15) as client:
        for extra in attempts:
            try:
                resp = await client.get(
                    paths, headers={"Accept": "application/vnd.github+json", **extra}
                )
            except httpx.RequestError:
                continue
            if resp.status_code != 200:
                continue
            data = resp.json()
            owner_info = data.get("owner") or {}
            if isinstance(data.get("id"), int) and isinstance(owner_info.get("id"), int):
                return {
                    "id": data["id"],
                    "owner_id": owner_info["id"],
                    "full_name": str(data.get("full_name") or f"{owner}/{repo}"),
                }
    return None


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
    # Explicit timeout well under the comment-publishing lease TTL.
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
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

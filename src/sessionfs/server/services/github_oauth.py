"""GitHub App user-to-server OAuth, used to prove installation ownership.

A SessionFS user may claim a GitHub App installation only after GitHub
itself lists that installation for the GitHub account the user just
authenticated as (``GET /user/installations`` with a user access token).
The user token is used for the duration of the callback and then dropped;
it is never stored or logged.

Every request goes to fixed GitHub hosts (github.com, api.github.com) — no
URL is ever derived from request input, so there is no SSRF surface. Calls
use explicit timeouts, never follow redirects, and bound pagination and
response size.

Configuration is read from the environment at call time:

- ``SFS_GITHUB_APP_CLIENT_ID``       (required to enable claims)
- ``SFS_GITHUB_APP_CLIENT_SECRET``   (required; inject from Secret Manager /
                                      a Kubernetes Secret, never in code)
- ``SFS_DASHBOARD_URL``              (required; where the callback lands
                                      the browser afterwards)
- ``SFS_GITHUB_APP_SLUG``            (optional; enables the install flow)
- ``SFS_GITHUB_OAUTH_CALLBACK_URL``  (required; the PKCE callback,
                                      .../api/v1/settings/github/oauth/callback,
                                      sent as redirect_uri on every authorize
                                      AND token exchange)
- ``SFS_GITHUB_OAUTH_INSECURE_LOCALHOST`` (optional, development only: allow
                                      an http://localhost dashboard and a
                                      non-Secure claim cookie)

When any required value is missing, or the dashboard / callback URL is not
https (outside the explicit localhost development mode), claiming is
disabled outright. There is no fallback to the old unauthenticated claim
path.

Organization admin status is checked live with ``GET
/user/memberships/orgs/{org}``, which needs the App's organization
"Members: read" permission. Without it the call fails and the claimant is
treated as a plain member (fail safe).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode, urlparse

import httpx

logger = logging.getLogger("sessionfs.server.github_oauth")

GITHUB_WEB = "https://github.com"
GITHUB_API = "https://api.github.com"

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_INSTALLATION_PAGES = 10
_PER_PAGE = 100

ROLE_ACCOUNT_OWNER = "account_owner"
ROLE_ORG_ADMIN = "org_admin"
ROLE_INSTALLER = "installer"
ROLE_MEMBER = "member"
# Only LIVE proofs of control over the whole installation count as
# privileged: the claimant IS the user account, or GitHub reports an active
# admin membership of the organization at claim time.
# 'installer' (the signed webhook sender) is informational only: repository
# admins and outside collaborators can install Apps, and installers can be
# demoted later, so it never grants installation-wide authority.
PRIVILEGED_ROLES = frozenset({ROLE_ACCOUNT_OWNER, ROLE_ORG_ADMIN})

# How long a privileged ownership proof is trusted for installation-wide
# authority (repo verification, takeover protection, publisher badges).
# GitHub does not tell SessionFS when someone is demoted or removed, so the
# proof must be refreshed by signing in to GitHub again.
CLAIM_PROOF_MAX_AGE = timedelta(days=7)

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# The PKCE callback. Every authorize request and token exchange names it as
# redirect_uri. The install return path (Setup URL) is a DIFFERENT endpoint
# that never exchanges codes, and it must be the App's FIRST registered
# callback URL: if OAuth-during-installation were ever enabled, GitHub sends
# its non-PKCE code to the first callback, binding it to install-return so
# GitHub refuses to redeem it with redirect_uri=/oauth/callback.
PKCE_CALLBACK_PATH = "/api/v1/settings/github/oauth/callback"
INSTALL_RETURN_PATH = "/api/v1/settings/github/oauth/install-return"

# GitHub logins: alphanumerics and single hyphens, at most 39 characters.
_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


class GitHubOAuthError(Exception):
    """Any failure talking to GitHub. The message is for server logs only."""


@dataclass(frozen=True)
class GitHubOAuthConfig:
    client_id: str
    client_secret: str
    dashboard_url: str
    app_slug: str
    callback_url: str
    # False only in the explicit localhost development mode.
    secure_cookie: bool = True


@dataclass(frozen=True)
class GitHubUser:
    id: int
    login: str


@dataclass(frozen=True)
class UserInstallation:
    id: int
    account_id: int | None
    account_login: str | None
    account_type: str | None


def load_config() -> GitHubOAuthConfig | None:
    """Return the OAuth config, or None when claiming is not configured."""
    client_id = os.environ.get("SFS_GITHUB_APP_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SFS_GITHUB_APP_CLIENT_SECRET", "").strip()
    dashboard_url = os.environ.get("SFS_DASHBOARD_URL", "").strip().rstrip("/")
    callback_url = os.environ.get("SFS_GITHUB_OAUTH_CALLBACK_URL", "").strip()
    if not client_id or not client_secret or not dashboard_url or not callback_url:
        return None
    if urlparse(callback_url).path.rstrip("/") != PKCE_CALLBACK_PATH:
        return None
    insecure_dev = os.environ.get(
        "SFS_GITHUB_OAUTH_INSECURE_LOCALHOST", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    secure_cookie = True
    for url in filter(None, (dashboard_url, callback_url)):
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.hostname:
            continue
        if (
            insecure_dev
            and parsed.scheme == "http"
            and (parsed.hostname or "") in _LOCAL_HOSTS
        ):
            # The claim cookie cannot be Secure over plain http.
            secure_cookie = False
            continue
        return None
    return GitHubOAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        dashboard_url=dashboard_url,
        app_slug=os.environ.get("SFS_GITHUB_APP_SLUG", "").strip(),
        callback_url=callback_url,
        secure_cookie=secure_cookie,
    )


def build_authorize_url(
    config: GitHubOAuthConfig, *, state: str, code_challenge: str
) -> str:
    params = {
        "client_id": config.client_id,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "allow_signup": "false",
        "redirect_uri": config.callback_url,
    }
    return f"{GITHUB_WEB}/login/oauth/authorize?{urlencode(params)}"


def build_install_url(config: GitHubOAuthConfig, *, state: str) -> str:
    """Installation URL. GitHub returns the browser to the Setup URL (the
    install-return endpoint) with ``installation_id`` and this ``state``.
    "Request user authorization during installation" must stay OFF; if it
    were on, the resulting code would carry no PKCE challenge. The
    install-return endpoint never exchanges codes and starts a separate PKCE
    authorization for the proof."""
    return f"{GITHUB_WEB}/apps/{config.app_slug}/installations/new?{urlencode({'state': state})}"


def _json(resp: httpx.Response) -> object:
    if len(resp.content) > _MAX_RESPONSE_BYTES:
        raise GitHubOAuthError("response too large")
    try:
        return resp.json()
    except ValueError as exc:
        raise GitHubOAuthError("response was not JSON") from exc


async def exchange_code(
    config: GitHubOAuthConfig, *, code: str, code_verifier: str
) -> str:
    """Exchange an OAuth code for a user access token (never persisted)."""
    data = {
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "code": code,
        "code_verifier": code_verifier,
        # Must equal the authorize request's redirect_uri; GitHub refuses
        # codes issued for any other callback (e.g. the install return).
        "redirect_uri": config.callback_url,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            resp = await client.post(
                f"{GITHUB_WEB}/login/oauth/access_token",
                data=data,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise GitHubOAuthError(f"token exchange transport error: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise GitHubOAuthError(f"token exchange status {resp.status_code}")
    payload = _json(resp)
    if not isinstance(payload, dict):
        raise GitHubOAuthError("token exchange payload not an object")
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        # GitHub reports bad_verification_code etc. with HTTP 200.
        raise GitHubOAuthError(f"token exchange refused: {payload.get('error', 'unknown')}")
    return token


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def get_user(token: str) -> GitHubUser:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            resp = await client.get(f"{GITHUB_API}/user", headers=_api_headers(token))
    except httpx.HTTPError as exc:
        raise GitHubOAuthError(f"/user transport error: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise GitHubOAuthError(f"/user status {resp.status_code}")
    payload = _json(resp)
    if not isinstance(payload, dict):
        raise GitHubOAuthError("/user payload not an object")
    uid = payload.get("id")
    login = payload.get("login")
    if isinstance(uid, bool) or not isinstance(uid, int) or not isinstance(login, str):
        raise GitHubOAuthError("/user payload missing id/login")
    return GitHubUser(id=uid, login=login)


def _parse_installation(raw: object) -> UserInstallation | None:
    if not isinstance(raw, dict):
        return None
    iid = raw.get("id")
    if isinstance(iid, bool) or not isinstance(iid, int):
        return None
    account = raw.get("account")
    if not isinstance(account, dict):
        account = {}
    acc_id = account.get("id")
    login = account.get("login")
    acc_type = account.get("type")
    return UserInstallation(
        id=iid,
        account_id=acc_id if isinstance(acc_id, int) and not isinstance(acc_id, bool) else None,
        account_login=login if isinstance(login, str) else None,
        account_type=acc_type if isinstance(acc_type, str) else None,
    )


async def list_user_installations(token: str) -> list[UserInstallation]:
    """Installations of THIS App that GitHub says the user can access."""
    found: list[UserInstallation] = []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            for page in range(1, _MAX_INSTALLATION_PAGES + 1):
                resp = await client.get(
                    f"{GITHUB_API}/user/installations",
                    params={"per_page": _PER_PAGE, "page": page},
                    headers=_api_headers(token),
                )
                if resp.status_code != 200:
                    raise GitHubOAuthError(f"/user/installations status {resp.status_code}")
                payload = _json(resp)
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("installations"), list
                ):
                    raise GitHubOAuthError("/user/installations payload malformed")
                batch = payload["installations"]
                for raw in batch:
                    parsed = _parse_installation(raw)
                    if parsed is not None:
                        found.append(parsed)
                if len(batch) < _PER_PAGE:
                    break
    except httpx.HTTPError as exc:
        raise GitHubOAuthError(
            f"/user/installations transport error: {type(exc).__name__}"
        ) from exc
    return found


async def is_active_org_admin(token: str, org_login: str) -> bool:
    """Best effort: True only if GitHub confirms an ACTIVE admin membership.

    Needs the App's "Members: read" organization permission. Any failure
    (permission missing, network, odd payload) returns False — the claim then
    records the weaker 'member' role rather than failing.
    """
    if not _LOGIN_RE.fullmatch(org_login or ""):
        return False
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            resp = await client.get(
                f"{GITHUB_API}/user/memberships/orgs/{org_login}",
                headers=_api_headers(token),
            )
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    try:
        payload = _json(resp)
    except GitHubOAuthError:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("role") == "admin"
        and payload.get("state") == "active"
    )


async def determine_role(
    token: str,
    gh_user: GitHubUser,
    inst: UserInstallation,
    *,
    installer_github_user_id: int | None = None,
) -> str:
    if inst.account_type == "User":
        return ROLE_ACCOUNT_OWNER if inst.account_id == gh_user.id else ROLE_MEMBER
    if inst.account_type == "Organization" and inst.account_login:
        if await is_active_org_admin(token, inst.account_login):
            return ROLE_ORG_ADMIN
        if installer_github_user_id is not None and installer_github_user_id == gh_user.id:
            return ROLE_INSTALLER
    return ROLE_MEMBER

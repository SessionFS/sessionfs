"""GitHub App installation claims: OAuth-proven ownership only.

Covers:
- the old PUT {installation_id} path no longer links anything and returns
  one constant response for every id (no enumeration oracle);
- the OAuth start/callback flow: GitHub must list the installation for the
  authenticated GitHub user before it is bound;
- hijack attempts (user B claiming A's installation) are rejected;
- state / browser-nonce / PKCE binding and single-use attempts;
- legacy (unverified) claims are not trusted for repo verification and can
  be re-verified or taken over by a proven admin;
- claims are disabled, not insecure, when OAuth is not configured.
"""

from __future__ import annotations

import json
import uuid
from http.cookies import SimpleCookie
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, GitHubClaimAttempt, GitHubInstallation, User
from sessionfs.server.routes import settings as settings_routes
from sessionfs.server.services import github_oauth

DASHBOARD = "https://app.example.test"
API = "https://api.example.test"
PKCE_CALLBACK = API + github_oauth.PKCE_CALLBACK_PATH
INSTALL_RETURN = API + github_oauth.INSTALL_RETURN_PATH


@pytest.fixture(autouse=True)
def _reset_limiters():
    for limiter in (
        settings_routes._claim_start_limiter,
        settings_routes._claim_callback_limiter,
        settings_routes._claim_choose_limiter,
        settings_routes._legacy_claim_limiter,
    ):
        limiter._requests.clear()
    yield


@pytest.fixture
def oauth_env(monkeypatch):
    monkeypatch.setenv("SFS_GITHUB_APP_CLIENT_ID", "Iv1.testclient")
    monkeypatch.setenv("SFS_GITHUB_APP_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("SFS_DASHBOARD_URL", DASHBOARD)
    monkeypatch.setenv("SFS_GITHUB_APP_SLUG", "sessionfs-test")
    monkeypatch.setenv("SFS_GITHUB_OAUTH_CALLBACK_URL", PKCE_CALLBACK)
    monkeypatch.delenv("SFS_GITHUB_OAUTH_INSECURE_LOCALHOST", raising=False)


@pytest.fixture
def no_oauth_env(monkeypatch):
    for name in (
        "SFS_GITHUB_APP_CLIENT_ID",
        "SFS_GITHUB_APP_CLIENT_SECRET",
        "SFS_GITHUB_APP_SLUG",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SFS_DASHBOARD_URL", DASHBOARD)


class FakeGitHub:
    """Stands in for github.com: maps OAuth codes to GitHub identities.

    Models GitHub's two code bindings: a code is redeemable only with the
    redirect_uri it was issued for, and a code issued WITH a PKCE challenge
    needs the matching verifier (a code issued without one accepts any
    verifier, which is exactly why install-hop codes are dangerous).
    """

    def __init__(self) -> None:
        # code -> (GitHubUser, [UserInstallation], {org_login}, redirect_uri, verifier|None)
        self.codes: dict[str, tuple] = {}
        self.exchanged: list[tuple[str, str]] = []
        self.redeemed: list[str] = []
        # The App's registered callback URLs, in order (as documented).
        self.registered_callbacks = [INSTALL_RETURN, PKCE_CALLBACK]

    def add_code(
        self, code, gh_user, installations, admin_orgs=(), *,
        redirect_uri=PKCE_CALLBACK, required_verifier=None,
    ):
        self.codes[code] = (
            gh_user, installations, set(admin_orgs), redirect_uri, required_verifier,
        )

    def add_install_time_code(self, code, gh_user, installations, admin_orgs=()):
        """What GitHub issues with OAuth-during-installation ON: no PKCE
        challenge, bound to the app's FIRST registered callback URL."""
        self.add_code(
            code, gh_user, installations, admin_orgs,
            redirect_uri=self.registered_callbacks[0], required_verifier=None,
        )

    def install(self, monkeypatch) -> None:
        async def exchange_code(config, *, code, code_verifier):
            self.exchanged.append((code, code_verifier))
            if code not in self.codes:
                raise github_oauth.GitHubOAuthError("bad_verification_code")
            _, _, _, redirect_uri, required_verifier = self.codes[code]
            if config.callback_url != redirect_uri:
                raise github_oauth.GitHubOAuthError("redirect_uri_mismatch")
            if required_verifier is not None and code_verifier != required_verifier:
                raise github_oauth.GitHubOAuthError("bad_verification_code")
            self.redeemed.append(code)
            return f"token-for-{code}"

        def _lookup(token):
            return self.codes[token.removeprefix("token-for-")]

        async def get_user(token):
            return _lookup(token)[0]

        async def list_user_installations(token):
            return list(_lookup(token)[1])

        async def is_active_org_admin(token, org_login):
            return org_login in _lookup(token)[2]

        monkeypatch.setattr(github_oauth, "exchange_code", exchange_code)
        monkeypatch.setattr(github_oauth, "get_user", get_user)
        monkeypatch.setattr(github_oauth, "list_user_installations", list_user_installations)
        monkeypatch.setattr(github_oauth, "is_active_org_admin", is_active_org_admin)


@pytest.fixture
def fake_github(monkeypatch, oauth_env) -> FakeGitHub:
    fake = FakeGitHub()
    fake.install(monkeypatch)
    return fake


ALICE_GH = github_oauth.GitHubUser(id=1001, login="alice")
MALLORY_GH = github_oauth.GitHubUser(id=6666, login="mallory")
ACME_INSTALL = github_oauth.UserInstallation(
    id=500001, account_id=9001, account_login="acme", account_type="Organization"
)
MALLORY_INSTALL = github_oauth.UserInstallation(
    id=500099, account_id=6666, account_login="mallory", account_type="User"
)


async def _create_installation(
    db: AsyncSession,
    *,
    installation_id: int,
    user_id: str | None = None,
    account_login: str = "acme",
    account_type: str = "Organization",
    verified: bool = False,
    role: str | None = None,
) -> GitHubInstallation:
    inst = GitHubInstallation(
        id=installation_id,
        user_id=user_id,
        account_login=account_login,
        account_type=account_type,
        auto_comment=True,
        include_trust_score=True,
        include_session_links=True,
        created_at=datetime.now(timezone.utc),
        claim_verified_at=datetime.now(timezone.utc) if verified else None,
        claim_verified_github_user_id=1001 if verified else None,
        claim_verified_github_login="alice" if verified else None,
        claim_role=role if verified else None,
    )
    db.add(inst)
    await db.commit()
    return inst


async def _make_user(db: AsyncSession, email: str) -> tuple[User, dict[str, str]]:
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        tier="pro",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name="k",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, {"Authorization": f"Bearer {raw}"}


async def _start(client: AsyncClient, headers, **body) -> tuple[str, str]:
    """Run /oauth/start; return (state, raw cookie value)."""
    resp = await client.post("/api/v1/settings/github/oauth/start", headers=headers, json=body)
    assert resp.status_code == 200, resp.text
    url = resp.json()["redirect_url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    set_cookie = resp.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert "Path=/api/v1/settings/github/oauth" in set_cookie
    parsed = SimpleCookie()
    parsed.load(set_cookie)
    cookie = parsed[settings_routes.GITHUB_CLAIM_COOKIE].value  # unquoted JSON
    assert json.loads(cookie)["state"] == state
    client.cookies.clear()
    return state, cookie


async def _callback(client: AsyncClient, *, code, state, cookie, installation_id=None):
    params = {"state": state}
    if code is not None:
        params["code"] = code
    if installation_id is not None:
        params["installation_id"] = str(installation_id)
    resp = await client.get(
        "/api/v1/settings/github/oauth/callback",
        params=params,
        cookies={settings_routes.GITHUB_CLAIM_COOKIE: cookie} if cookie else None,
        follow_redirects=False,
    )
    client.cookies.clear()
    return resp


def _cookie_from(resp) -> str:
    parsed = SimpleCookie()
    parsed.load(resp.headers["set-cookie"])
    return parsed[settings_routes.GITHUB_CLAIM_COOKIE].value


async def _install_return(client: AsyncClient, *, state, cookie, installation_id=None, code=None):
    params = {"state": state}
    if installation_id is not None:
        params["installation_id"] = str(installation_id)
    if code is not None:
        params["code"] = code  # GitHub may add one; the endpoint ignores it
    resp = await client.get(
        "/api/v1/settings/github/oauth/install-return",
        params=params,
        cookies={settings_routes.GITHUB_CLAIM_COOKIE: cookie} if cookie else None,
        follow_redirects=False,
    )
    client.cookies.clear()
    return resp


async def _install_flow(
    client: AsyncClient, headers, *, installation_id, code, install_hop_code=None
):
    """Install return (code ignored) -> PKCE authorize -> callback with ``code``."""
    state, cookie = await _start(client, headers, flow="install")
    hop = await _install_return(
        client, code=install_hop_code, state=state, cookie=cookie,
        installation_id=installation_id,
    )
    assert hop.status_code == 302, hop.text
    url = urlparse(hop.headers["location"])
    assert url.netloc == "github.com" and url.path == "/login/oauth/authorize"
    q = parse_qs(url.query)
    assert q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == [PKCE_CALLBACK]
    new_state = q["state"][0]
    assert new_state != state
    new_cookie = _cookie_from(hop)
    assert json.loads(new_cookie)["state"] == new_state
    return await _callback(client, code=code, state=new_state, cookie=new_cookie)


def _outcome(resp) -> str:
    assert resp.status_code == 302, resp.text
    loc = resp.headers["location"]
    assert loc.startswith(f"{DASHBOARD}/settings?github=")
    return parse_qs(urlparse(loc).query)["github"][0]


async def _row(db: AsyncSession, installation_id: int) -> GitHubInstallation | None:
    return (
        await db.execute(
            select(GitHubInstallation)
            .where(GitHubInstallation.id == installation_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Legacy PUT path: no linking, no oracle
# ---------------------------------------------------------------------------


class TestLegacyPutPath:
    @pytest.mark.asyncio
    async def test_bare_installation_id_never_links(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession, oauth_env
    ):
        await _create_installation(db_session, installation_id=100001)
        resp = await client.put(
            "/api/v1/settings/github", headers=auth_headers, json={"installation_id": 100001}
        )
        assert resp.status_code == 409
        assert (await _row(db_session, 100001)).user_id is None

    @pytest.mark.asyncio
    async def test_enumeration_responses_identical(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession, oauth_env
    ):
        other, _ = await _make_user(db_session, "other@example.com")
        await _create_installation(db_session, installation_id=200001)  # unclaimed
        await _create_installation(db_session, installation_id=200002, user_id=other.id)
        old = await _create_installation(db_session, installation_id=200003)
        old.created_at = datetime.now(timezone.utc) - timedelta(days=2)
        await db_session.commit()

        bodies = set()
        statuses = set()
        for iid in (200001, 200002, 200003, 999999999):
            resp = await client.put(
                "/api/v1/settings/github", headers=auth_headers, json={"installation_id": iid}
            )
            statuses.add(resp.status_code)
            bodies.add(resp.text)
        assert statuses == {409}
        assert len(bodies) == 1

    @pytest.mark.asyncio
    async def test_preferences_without_installation_return_defaults(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession, oauth_env
    ):
        await _create_installation(db_session, installation_id=100002)
        resp = await client.put(
            "/api/v1/settings/github", headers=auth_headers, json={"auto_comment": False}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["account_login"] is None
        assert data["auto_comment"] is False
        assert data["claim_available"] is True

    @pytest.mark.asyncio
    async def test_legacy_put_rate_limited(
        self, client: AsyncClient, auth_headers, oauth_env
    ):
        codes = []
        for _ in range(25):
            resp = await client.put(
                "/api/v1/settings/github", headers=auth_headers, json={"installation_id": 1}
            )
            codes.append(resp.status_code)
        assert codes[0] == 409
        assert codes[-1] == 429


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------


class TestOAuthClaim:
    @pytest.mark.asyncio
    async def test_install_flow_links_listed_installation(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        resp = await _install_flow(
            client, auth_headers, installation_id=ACME_INSTALL.id, code="c1"
        )
        assert _outcome(resp) == "connected"
        # Only the PKCE-bound code was exchanged, never the install-hop code.
        assert [c for c, _ in fake_github.exchanged] == ["c1"]

        row = await _row(db_session, ACME_INSTALL.id)
        assert row is not None  # created even before the webhook arrived
        assert row.user_id == test_user.id
        assert row.claim_verified_at is not None
        assert row.claim_verified_github_user_id == ALICE_GH.id
        assert row.claim_role == "org_admin"
        assert row.account_id == 9001

        data = (await client.get("/api/v1/settings/github", headers=auth_headers)).json()
        assert data["account_login"] == "acme"
        assert data["ownership_verified"] is True
        assert data["needs_reverification"] is False
        assert data["claim_verified_github_login"] == "alice"

    @pytest.mark.asyncio
    async def test_install_url_and_authorize_url_shape(
        self, client: AsyncClient, auth_headers, fake_github
    ):
        resp = await client.post(
            "/api/v1/settings/github/oauth/start", headers=auth_headers, json={"flow": "install"}
        )
        assert resp.json()["redirect_url"].startswith(
            "https://github.com/apps/sessionfs-test/installations/new?state="
        )
        resp = await client.post(
            "/api/v1/settings/github/oauth/start",
            headers=auth_headers,
            json={"flow": "authorize"},
        )
        url = urlparse(resp.json()["redirect_url"])
        q = parse_qs(url.query)
        assert url.netloc == "github.com" and url.path == "/login/oauth/authorize"
        assert q["client_id"] == ["Iv1.testclient"]
        assert q["code_challenge_method"] == ["S256"]
        assert "test-client-secret" not in resp.text

    @pytest.mark.asyncio
    async def test_hijack_user_b_cannot_claim_user_as_installation(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        """Mallory (SessionFS user B) races to claim Acme's fresh install.

        GitHub does not list Acme's installation for Mallory's GitHub
        account, so the claim fails and the row stays unclaimed.
        """
        await _create_installation(db_session, installation_id=ACME_INSTALL.id)
        _, mallory_headers = await _make_user(db_session, "mallory@example.com")
        fake_github.add_code("cm", MALLORY_GH, [MALLORY_INSTALL])

        state, cookie = await _start(
            client, mallory_headers, flow="authorize", installation_id=ACME_INSTALL.id
        )
        resp = await _callback(
            client, code="cm", state=state, cookie=cookie, installation_id=ACME_INSTALL.id
        )
        assert _outcome(resp) == "failed"
        row = await _row(db_session, ACME_INSTALL.id)
        assert row.user_id is None
        assert row.claim_verified_at is None

    @pytest.mark.asyncio
    async def test_failure_outcomes_identical(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        """Unknown id, id held by someone else, id not listed: same redirect."""
        other, _ = await _make_user(db_session, "other@example.com")
        held = github_oauth.UserInstallation(
            id=700002, account_id=9002, account_login="beta", account_type="Organization"
        )
        await _create_installation(
            db_session, installation_id=700002, user_id=other.id, verified=True, role="org_admin"
        )
        # alice is listed on 700002 but only as a member (not admin).
        fake_github.add_code("u1", ALICE_GH, [])
        fake_github.add_code("u2", ALICE_GH, [held])
        fake_github.add_code("u3", ALICE_GH, [])

        locations = set()
        for code, iid in (("u1", 999999), ("u2", 700002), ("u3", 700002)):
            state, cookie = await _start(client, auth_headers, flow="authorize")
            resp = await _callback(client, code=code, state=state, cookie=cookie, installation_id=iid)
            assert resp.status_code == 302
            locations.add(resp.headers["location"])
        assert locations == {f"{DASHBOARD}/settings?github=failed"}
        assert (await _row(db_session, 700002)).user_id == other.id

    @pytest.mark.asyncio
    async def test_state_mismatch_with_cookie_rejected(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        """Login CSRF: attacker's state in the URL, victim's own cookie."""
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        attacker_state, _ = await _start(client, auth_headers, flow="install")
        _, victim_cookie = await _start(client, auth_headers, flow="install")
        resp = await _callback(
            client, code="c1", state=attacker_state, cookie=victim_cookie,
            installation_id=ACME_INSTALL.id,
        )
        assert _outcome(resp) == "failed"
        assert await _row(db_session, ACME_INSTALL.id) is None
        assert fake_github.exchanged == []

    @pytest.mark.asyncio
    async def test_missing_cookie_rejected(self, client: AsyncClient, auth_headers, fake_github):
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL])
        state, _ = await _start(client, auth_headers, flow="install")
        resp = await _callback(client, code="c1", state=state, cookie=None)
        assert _outcome(resp) == "failed"
        assert fake_github.exchanged == []

    @pytest.mark.asyncio
    async def test_forged_cookie_nonce_rejected(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL])
        state, cookie = await _start(client, auth_headers, flow="authorize")
        forged = json.loads(cookie)
        forged["nonce"] = "attacker-nonce"
        resp = await _callback(
            client, code="c1", state=state, cookie=json.dumps(forged),
            installation_id=ACME_INSTALL.id,
        )
        assert _outcome(resp) == "failed"
        assert fake_github.exchanged == []
        # The cookie parsed and the state matched: the attempt was consumed
        # (burned into 'processing') and the nonce check is what refused it.
        attempt = (
            await db_session.execute(
                select(GitHubClaimAttempt).execution_options(populate_existing=True)
            )
        ).scalars().one()
        assert attempt.status == "processing"

    @pytest.mark.asyncio
    async def test_attempt_is_single_use(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        state, cookie = await _start(
            client, auth_headers, flow="authorize", installation_id=ACME_INSTALL.id
        )
        first = await _callback(client, code="c1", state=state, cookie=cookie)
        assert _outcome(first) == "connected"
        replay = await _callback(client, code="c1", state=state, cookie=cookie)
        assert _outcome(replay) == "failed"
        assert len(fake_github.exchanged) == 1

    @pytest.mark.asyncio
    async def test_expired_attempt_rejected(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL])
        state, cookie = await _start(client, auth_headers, flow="install")
        attempt = (await db_session.execute(select(GitHubClaimAttempt))).scalars().one()
        attempt.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db_session.commit()
        resp = await _callback(
            client, code="c1", state=state, cookie=cookie, installation_id=ACME_INSTALL.id
        )
        assert _outcome(resp) == "failed"

    @pytest.mark.asyncio
    async def test_target_mismatch_rejected(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        """installation_id in the callback must match the one start was given."""
        other_inst = github_oauth.UserInstallation(
            id=500002, account_id=9003, account_login="gamma", account_type="Organization"
        )
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL, other_inst], admin_orgs=["acme", "gamma"])
        state, cookie = await _start(
            client, auth_headers, flow="authorize", installation_id=ACME_INSTALL.id
        )
        resp = await _callback(
            client, code="c1", state=state, cookie=cookie, installation_id=other_inst.id
        )
        assert _outcome(resp) == "failed"

    @pytest.mark.asyncio
    async def test_bad_github_code_fails_cleanly(
        self, client: AsyncClient, auth_headers, fake_github
    ):
        resp = await _install_flow(
            client, auth_headers, installation_id=ACME_INSTALL.id, code="nope"
        )
        assert _outcome(resp) == "failed"

    @pytest.mark.asyncio
    async def test_start_rejects_bool_installation_id(
        self, client: AsyncClient, auth_headers, fake_github
    ):
        resp = await client.post(
            "/api/v1/settings/github/oauth/start",
            headers=auth_headers,
            json={"flow": "authorize", "installation_id": True},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_start_requires_auth(self, client: AsyncClient, fake_github):
        resp = await client.post("/api/v1/settings/github/oauth/start", json={"flow": "install"})
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_start_rate_limited(self, client: AsyncClient, auth_headers, fake_github):
        codes = [
            (
                await client.post(
                    "/api/v1/settings/github/oauth/start",
                    headers=auth_headers,
                    json={"flow": "authorize"},
                )
            ).status_code
            for _ in range(22)
        ]
        assert codes[0] == 200
        assert codes[-1] == 429


# ---------------------------------------------------------------------------
# Installer signal (signed webhook sender)
# ---------------------------------------------------------------------------


def _signed_webhook(secret: str, payload: dict) -> tuple[bytes, dict[str, str]]:
    import hashlib
    import hmac

    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {
        "X-Hub-Signature-256": sig,
        "X-GitHub-Event": "installation", "X-GitHub-Delivery": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


_INSTALL_PAYLOAD = {
    "action": "created",
    "installation": {
        "id": ACME_INSTALL.id,
        "account": {"id": 9001, "login": "acme", "type": "Organization"},
        "app_id": 1, "target_type": "Organization",
    },
    "sender": {"id": ALICE_GH.id, "login": "alice"},
}


class TestInstallerSignal:
    @pytest.mark.asyncio
    async def test_signed_webhook_records_installer(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        from sessionfs.server.routes import webhooks

        monkeypatch.delenv("SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED", raising=False)

        monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "whsec")
        body, headers = _signed_webhook("whsec", _INSTALL_PAYLOAD)
        resp = await client.post("/webhooks/github", content=body, headers=headers)
        assert resp.status_code == 200
        row = await _row(db_session, ACME_INSTALL.id)
        assert row.installer_github_user_id == ALICE_GH.id
        assert row.account_id == 9001
        assert row.user_id is None

    @pytest.mark.asyncio
    async def test_unsigned_webhook_does_not_record_installer(
        self, client: AsyncClient, db_session, monkeypatch
    ):
        from sessionfs.server.routes import webhooks

        monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "")
        monkeypatch.setenv("SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED", "1")  # local-dev opt-in
        resp = await client.post(
            "/webhooks/github",
            content=json.dumps(_INSTALL_PAYLOAD).encode(),
            headers={"X-GitHub-Event": "installation", "X-GitHub-Delivery": "test-delivery-4", "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        row = await _row(db_session, ACME_INSTALL.id)
        assert row.installer_github_user_id is None

    @pytest.mark.asyncio
    async def test_installer_role_is_informational_only(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        """Finding 3: the historical installer is NOT treated as an admin."""
        inst = await _create_installation(db_session, installation_id=ACME_INSTALL.id)
        inst.installer_github_user_id = ALICE_GH.id
        await db_session.commit()
        # GitHub cannot confirm admin (no Members permission): admin_orgs empty.
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL])
        resp = await _install_flow(
            client, auth_headers, installation_id=ACME_INSTALL.id, code="c1"
        )
        assert _outcome(resp) == "connected"
        row = await _row(db_session, ACME_INSTALL.id)
        assert row.claim_role == "installer"

        from sessionfs.server.github_app import installation_owned_by

        assert await installation_owned_by(db_session, test_user.id, ACME_INSTALL.id)
        assert not await installation_owned_by(
            db_session, test_user.id, ACME_INSTALL.id, require_admin=True
        )

    @pytest.mark.asyncio
    async def test_non_installer_member_gets_member_role(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        inst = await _create_installation(db_session, installation_id=ACME_INSTALL.id)
        inst.installer_github_user_id = 4242  # someone else installed it
        await db_session.commit()
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL])
        resp = await _install_flow(
            client, auth_headers, installation_id=ACME_INSTALL.id, code="c1"
        )
        assert _outcome(resp) == "connected"
        row = await _row(db_session, ACME_INSTALL.id)
        assert row.claim_role == "member"


# ---------------------------------------------------------------------------
# Legacy claims
# ---------------------------------------------------------------------------


class TestLegacyClaims:
    @pytest.mark.asyncio
    async def test_legacy_claim_flagged_for_reverification(
        self, client: AsyncClient, auth_headers, test_user, db_session, oauth_env
    ):
        await _create_installation(db_session, installation_id=300001, user_id=test_user.id)
        data = (await client.get("/api/v1/settings/github", headers=auth_headers)).json()
        assert data["account_login"] == "acme"
        assert data["ownership_verified"] is False
        assert data["needs_reverification"] is True

    @pytest.mark.asyncio
    async def test_legacy_claim_preferences_still_editable(
        self, client: AsyncClient, auth_headers, test_user, db_session, oauth_env
    ):
        await _create_installation(db_session, installation_id=300002, user_id=test_user.id)
        resp = await client.put(
            "/api/v1/settings/github", headers=auth_headers, json={"auto_comment": False}
        )
        assert resp.status_code == 200
        assert resp.json()["auto_comment"] is False

    @pytest.mark.asyncio
    async def test_legacy_claim_not_trusted_by_helpers(self, db_session, test_user):
        from sessionfs.server.github_app import get_verified_installations, installation_owned_by

        # One linked installation per user, so use three users.
        legacy_user, _ = await _make_user(db_session, "legacy@example.com")
        member_user, _ = await _make_user(db_session, "member@example.com")
        await _create_installation(db_session, installation_id=300003, user_id=legacy_user.id)
        await _create_installation(
            db_session, installation_id=300004, user_id=member_user.id, verified=True,
            role="member",
        )
        await _create_installation(
            db_session, installation_id=300005, user_id=test_user.id, verified=True,
            role="org_admin",
        )
        assert await installation_owned_by(db_session, legacy_user.id, 300003) is False
        assert await installation_owned_by(db_session, member_user.id, 300004) is True
        assert (
            await installation_owned_by(db_session, member_user.id, 300004, require_admin=True)
            is False
        )
        assert (
            await installation_owned_by(db_session, test_user.id, 300005, require_admin=True)
            is True
        )
        assert await installation_owned_by(db_session, "someone-else", 300005) is False
        assert await installation_owned_by(db_session, test_user.id, True) is False  # type: ignore[arg-type]
        assert (
            await installation_owned_by(
                db_session, test_user.id, 300005, max_age=timedelta(seconds=-1)
            )
            is False
        )
        assert await get_verified_installations(db_session, legacy_user.id) == []
        ids = [i.id for i in await get_verified_installations(db_session, member_user.id)]
        assert ids == [300004]

    @pytest.mark.asyncio
    async def test_verify_repo_ownership_ignores_legacy_claims(
        self, db_session, test_user, monkeypatch
    ):
        from sessionfs.server import github_app

        monkeypatch.setattr(github_app, "GITHUB_APP_ID", "123")
        monkeypatch.setattr(github_app, "GITHUB_APP_PRIVATE_KEY", "pem")
        called: list[int] = []

        async def fake_token(installation_id):
            called.append(installation_id)
            raise RuntimeError("no network in tests")

        monkeypatch.setattr(github_app, "get_installation_token", fake_token)
        await _create_installation(db_session, installation_id=300006, user_id=test_user.id)
        result = await github_app.verify_repo_ownership(db_session, test_user.id, "acme", "repo")
        assert result == (False, "owner_attested", None, None)
        assert called == []  # the legacy installation was never used

    @pytest.mark.asyncio
    async def test_owner_reverifies_legacy_claim(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        await _create_installation(
            db_session, installation_id=ACME_INSTALL.id, user_id=test_user.id
        )
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        state, cookie = await _start(client, auth_headers, flow="authorize")
        resp = await _callback(client, code="c1", state=state, cookie=cookie)
        assert _outcome(resp) == "connected"
        row = await _row(db_session, ACME_INSTALL.id)
        assert row.user_id == test_user.id
        assert row.claim_verified_at is not None

    @pytest.mark.asyncio
    async def test_admin_takes_over_hijacked_legacy_claim(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        """Mallory hijacked Acme's install under the old window; Acme's
        admin proves ownership and the unverified claim moves to them."""
        mallory, _ = await _make_user(db_session, "mallory@example.com")
        await _create_installation(db_session, installation_id=ACME_INSTALL.id, user_id=mallory.id)
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        state, cookie = await _start(
            client, auth_headers, flow="authorize", installation_id=ACME_INSTALL.id
        )
        resp = await _callback(client, code="c1", state=state, cookie=cookie)
        assert _outcome(resp) == "connected"
        row = await _row(db_session, ACME_INSTALL.id)
        assert row.user_id == test_user.id
        assert row.claim_role == "org_admin"

    @pytest.mark.asyncio
    async def test_member_cannot_take_over_legacy_claim(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        other, _ = await _make_user(db_session, "other@example.com")
        await _create_installation(db_session, installation_id=ACME_INSTALL.id, user_id=other.id)
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL])  # listed, not admin
        state, cookie = await _start(
            client, auth_headers, flow="authorize", installation_id=ACME_INSTALL.id
        )
        resp = await _callback(client, code="c1", state=state, cookie=cookie)
        assert _outcome(resp) == "failed"
        assert (await _row(db_session, ACME_INSTALL.id)).user_id == other.id

    @pytest.mark.asyncio
    async def test_verified_admin_claim_not_displaced(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        other, _ = await _make_user(db_session, "other@example.com")
        await _create_installation(
            db_session, installation_id=ACME_INSTALL.id, user_id=other.id, verified=True,
            role="org_admin",
        )
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        state, cookie = await _start(
            client, auth_headers, flow="authorize", installation_id=ACME_INSTALL.id
        )
        resp = await _callback(client, code="c1", state=state, cookie=cookie)
        assert _outcome(resp) == "failed"
        assert (await _row(db_session, ACME_INSTALL.id)).user_id == other.id


# ---------------------------------------------------------------------------
# Configuration unset
# ---------------------------------------------------------------------------


class TestClaimsDisabledWhenUnconfigured:
    @pytest.mark.asyncio
    async def test_start_refused(self, client: AsyncClient, auth_headers, no_oauth_env):
        resp = await client.post(
            "/api/v1/settings/github/oauth/start", headers=auth_headers, json={"flow": "install"}
        )
        assert resp.status_code == 503
        assert "github_claim_disabled" in resp.text

    @pytest.mark.asyncio
    async def test_legacy_put_does_not_fall_back(
        self, client: AsyncClient, auth_headers, db_session, no_oauth_env
    ):
        await _create_installation(db_session, installation_id=400001)
        resp = await client.put(
            "/api/v1/settings/github", headers=auth_headers, json={"installation_id": 400001}
        )
        assert resp.status_code == 409
        assert "github_claim_disabled" in resp.text
        assert (await _row(db_session, 400001)).user_id is None

    @pytest.mark.asyncio
    async def test_callback_refused(self, client: AsyncClient, no_oauth_env):
        resp = await client.get(
            "/api/v1/settings/github/oauth/callback",
            params={"code": "x", "state": "y"},
            follow_redirects=False,
        )
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_get_reports_unavailable(self, client: AsyncClient, auth_headers, no_oauth_env):
        data = (await client.get("/api/v1/settings/github", headers=auth_headers)).json()
        assert data["claim_available"] is False
        assert data["install_available"] is False

    @pytest.mark.asyncio
    async def test_install_flow_needs_slug(self, client: AsyncClient, auth_headers, oauth_env, monkeypatch):
        monkeypatch.delenv("SFS_GITHUB_APP_SLUG", raising=False)
        resp = await client.post(
            "/api/v1/settings/github/oauth/start", headers=auth_headers, json={"flow": "install"}
        )
        assert resp.status_code == 503
        resp = await client.post(
            "/api/v1/settings/github/oauth/start", headers=auth_headers, json={"flow": "authorize"}
        )
        assert resp.status_code == 200

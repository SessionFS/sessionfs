# ruff: noqa: F811 - fixtures are imported from test_github_claim and used as parameters
"""Regression tests for the review findings on GitHub installation claims.

One test (or small group) per finding:
 1. member claims never become installation-wide repo authority
 2. privileged proofs expire (CLAIM_PROOF_MAX_AGE) and can be re-verified
 3. the historical installer is not an admin
 4. missing Members permission fails safe to 'member'
 5. the install-hop code is never exchanged; only the PKCE-bound code is
 6. targetless authorize can take over a legacy/member claim as an admin
 7. a signed installation webhook upserts into a callback-created row
 8. the callback limiter is not a shared per-address bucket
 9. claims are only available over https (or explicit localhost dev mode)
10. exactly one installation is linked per flow (server-side chooser)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from sessionfs.server.db.models import GitHubClaimAttempt, GitHubInstallation
from sessionfs.server.routes import settings as settings_routes
from sessionfs.server.services import github_oauth
from tests.server.integration.test_github_claim import (  # noqa: F401 - fixtures
    ACME_INSTALL,
    ALICE_GH,
    DASHBOARD,
    _callback,
    _create_installation,
    _install_flow,
    _install_return,
    INSTALL_RETURN,
    PKCE_CALLBACK,
    _make_user,
    _outcome,
    _reset_limiters,
    _row,
    _signed_webhook,
    _start,
    fake_github,
    oauth_env,
)

BETA_INSTALL = github_oauth.UserInstallation(
    id=500003, account_id=9004, account_login="beta", account_type="Organization"
)


async def _fake_token_recorder(monkeypatch):
    from sessionfs.server import github_app

    monkeypatch.setattr(github_app, "GITHUB_APP_ID", "123")
    monkeypatch.setattr(github_app, "GITHUB_APP_PRIVATE_KEY", "pem")
    called: list[int] = []

    async def fake_token(installation_id):
        called.append(installation_id)
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(github_app, "get_installation_token", fake_token)
    return github_app, called


# --- 1 ---------------------------------------------------------------------


class TestFinding1MemberNotRepoAuthority:
    @pytest.mark.asyncio
    async def test_fresh_member_claim_not_used_for_repo_verification(
        self, db_session, test_user, monkeypatch
    ):
        github_app, called = await _fake_token_recorder(monkeypatch)
        await _create_installation(
            db_session, installation_id=601, user_id=test_user.id, verified=True, role="member"
        )
        result = await github_app.verify_repo_ownership(db_session, test_user.id, "acme", "r")
        assert result == (False, "owner_attested", None, None)
        assert called == []

    @pytest.mark.asyncio
    async def test_fresh_admin_claim_is_used(self, db_session, test_user, monkeypatch):
        github_app, called = await _fake_token_recorder(monkeypatch)
        await _create_installation(
            db_session, installation_id=602, user_id=test_user.id, verified=True,
            role="org_admin",
        )
        await github_app.verify_repo_ownership(db_session, test_user.id, "acme", "r")
        assert called == [602]


# --- 2 ---------------------------------------------------------------------


class TestFinding2ProofExpiry:
    @pytest.mark.asyncio
    async def test_stale_admin_proof_falls_back_and_is_flagged(
        self, client: AsyncClient, auth_headers, db_session, test_user, monkeypatch, oauth_env
    ):
        github_app, called = await _fake_token_recorder(monkeypatch)
        inst = await _create_installation(
            db_session, installation_id=603, user_id=test_user.id, verified=True,
            role="org_admin",
        )
        inst.claim_verified_at = datetime.now(timezone.utc) - (
            github_oauth.CLAIM_PROOF_MAX_AGE + timedelta(hours=1)
        )
        await db_session.commit()

        result = await github_app.verify_repo_ownership(db_session, test_user.id, "acme", "r")
        assert result[1] == "owner_attested"
        assert called == []

        data = (await client.get("/api/v1/settings/github", headers=auth_headers)).json()
        assert data["proof_stale"] is True
        assert data["repo_verification_active"] is False
        assert data["installation_id"] == 603

    @pytest.mark.asyncio
    async def test_fresh_admin_proof_reported_active(
        self, client: AsyncClient, auth_headers, db_session, test_user, oauth_env
    ):
        await _create_installation(
            db_session, installation_id=604, user_id=test_user.id, verified=True,
            role="account_owner",
        )
        data = (await client.get("/api/v1/settings/github", headers=auth_headers)).json()
        assert data["repo_verification_active"] is True
        assert data["proof_stale"] is False

    def test_max_age_is_bounded(self):
        assert timedelta(0) < github_oauth.CLAIM_PROOF_MAX_AGE <= timedelta(days=7)

    @pytest.mark.asyncio
    async def test_new_admin_can_take_over_stale_admin_claim(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        former, _ = await _make_user(db_session, "former@example.com")
        inst = await _create_installation(
            db_session, installation_id=ACME_INSTALL.id, user_id=former.id, verified=True,
            role="org_admin",
        )
        inst.claim_verified_at = datetime.now(timezone.utc) - timedelta(days=30)
        await db_session.commit()
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        state, cookie = await _start(
            client, auth_headers, flow="authorize", installation_id=ACME_INSTALL.id
        )
        assert _outcome(await _callback(client, code="c1", state=state, cookie=cookie)) == "connected"
        assert (await _row(db_session, ACME_INSTALL.id)).user_id == test_user.id


# --- 3 ---------------------------------------------------------------------


class TestFinding3InstallerNotAdmin:
    def test_installer_not_privileged(self):
        assert github_oauth.ROLE_INSTALLER not in github_oauth.PRIVILEGED_ROLES

    @pytest.mark.asyncio
    async def test_installer_cannot_take_over_legacy_claim(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        holder, _ = await _make_user(db_session, "holder@example.com")
        inst = await _create_installation(
            db_session, installation_id=ACME_INSTALL.id, user_id=holder.id
        )
        inst.installer_github_user_id = ALICE_GH.id
        await db_session.commit()
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL])  # not an admin
        state, cookie = await _start(
            client, auth_headers, flow="authorize", installation_id=ACME_INSTALL.id
        )
        assert _outcome(await _callback(client, code="c1", state=state, cookie=cookie)) == "failed"
        assert (await _row(db_session, ACME_INSTALL.id)).user_id == holder.id


# --- 4 ---------------------------------------------------------------------


class TestFinding4MembersPermission:
    @pytest.mark.asyncio
    async def test_membership_call_failure_means_member(self, monkeypatch):
        async def refuse(token, org_login):  # what a 403 from GitHub becomes
            return False

        monkeypatch.setattr(github_oauth, "is_active_org_admin", refuse)
        role = await github_oauth.determine_role("t", ALICE_GH, ACME_INSTALL)
        assert role == github_oauth.ROLE_MEMBER

    def test_docs_require_members_read(self):
        from pathlib import Path

        doc = (Path(__file__).resolve().parents[3] / "docs" / "github-app.md").read_text()
        assert "Members" in doc and "Read" in doc
        assert "approve" in doc.lower()


# --- 5 ---------------------------------------------------------------------


class TestFinding5InstallCodeNotExchanged:
    @pytest.mark.asyncio
    async def test_install_hop_code_never_exchanged(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        # A code on the install return means OAuth-during-install is ON:
        # the flow is refused and the code is never exchanged.
        fake_github.add_code("hop", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        state, cookie = await _start(client, auth_headers, flow="install")
        hop = await _install_return(
            client, code="hop", state=state, cookie=cookie, installation_id=ACME_INSTALL.id
        )
        assert _outcome(hop) == "failed"
        assert fake_github.exchanged == []
        assert await _row(db_session, ACME_INSTALL.id) is None

    @pytest.mark.asyncio
    async def test_install_return_without_code_starts_pkce_authorize(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        state, cookie = await _start(client, auth_headers, flow="install")
        hop = await _install_return(
            client, state=state, cookie=cookie, installation_id=ACME_INSTALL.id
        )
        assert hop.status_code == 302
        assert hop.headers["location"].startswith("https://github.com/login/oauth/authorize?")
        assert fake_github.exchanged == []
        attempts = (
            await db_session.execute(
                select(GitHubClaimAttempt).where(GitHubClaimAttempt.status == "pending")
            )
        ).scalars().all()
        assert [(a.flow, a.target_installation_id) for a in attempts] == [
            ("authorize", ACME_INSTALL.id)
        ]

    @pytest.mark.asyncio
    async def test_authorize_callback_without_code_fails(
        self, client: AsyncClient, auth_headers, fake_github
    ):
        state, cookie = await _start(client, auth_headers, flow="authorize")
        assert _outcome(await _callback(client, code=None, state=state, cookie=cookie)) == "failed"

    @pytest.mark.asyncio
    async def test_install_hop_code_cannot_be_replayed_into_an_authorize_attempt(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        """An intercepted install-hop code would only work if exchanged with
        a verifier it was never bound to. The exchange always sends the
        verifier of THIS attempt; the fake rejects codes not issued for a
        PKCE authorize (GitHub binds the challenge to the code)."""
        state, cookie = await _start(client, auth_headers, flow="authorize")
        resp = await _callback(client, code="intercepted-install-code", state=state, cookie=cookie)
        assert _outcome(resp) == "failed"
        assert await _row(db_session, ACME_INSTALL.id) is None


# --- 6 ---------------------------------------------------------------------


class TestFinding6TargetlessTakeover:
    @pytest.mark.asyncio
    async def test_admin_takes_over_legacy_claim_without_target(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        mallory, _ = await _make_user(db_session, "mallory@example.com")
        await _create_installation(db_session, installation_id=ACME_INSTALL.id, user_id=mallory.id)
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        state, cookie = await _start(client, auth_headers, flow="authorize")
        assert _outcome(await _callback(client, code="c1", state=state, cookie=cookie)) == "connected"
        row = await _row(db_session, ACME_INSTALL.id)
        assert row.user_id == test_user.id
        assert row.claim_role == "org_admin"

    @pytest.mark.asyncio
    async def test_member_targetless_does_not_take_over(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        other, _ = await _make_user(db_session, "other@example.com")
        await _create_installation(db_session, installation_id=ACME_INSTALL.id, user_id=other.id)
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL])
        state, cookie = await _start(client, auth_headers, flow="authorize")
        assert _outcome(await _callback(client, code="c1", state=state, cookie=cookie)) == "failed"
        assert (await _row(db_session, ACME_INSTALL.id)).user_id == other.id


# --- 7 ---------------------------------------------------------------------


class TestFinding7WebhookUpsert:
    @pytest.mark.asyncio
    async def test_signed_webhook_after_callback_fills_provenance(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github, monkeypatch
    ):
        from sessionfs.server.routes import webhooks

        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        resp = await _install_flow(client, auth_headers, installation_id=ACME_INSTALL.id, code="c1")
        assert _outcome(resp) == "connected"
        assert (await _row(db_session, ACME_INSTALL.id)).installer_github_user_id is None

        monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "whsec")
        payload = {
            "action": "created",
            "installation": {
                "id": ACME_INSTALL.id,
                "account": {"id": 9001, "login": "acme-renamed", "type": "Organization"},
                "app_id": 1, "target_type": "Organization",
            },
            "sender": {"id": ALICE_GH.id, "login": "alice"},
        }
        body, headers = _signed_webhook("whsec", payload)
        assert (await client.post("/webhooks/github", content=body, headers=headers)).status_code == 200
        row = await _row(db_session, ACME_INSTALL.id)
        assert row.installer_github_user_id == ALICE_GH.id
        assert row.account_login == "acme-renamed"
        # Provenance only: the claim is untouched.
        assert row.user_id == test_user.id
        assert row.claim_role == "org_admin"


# --- 8 ---------------------------------------------------------------------


class TestFinding8CallbackLimiter:
    @pytest.mark.asyncio
    async def test_flood_of_bogus_callbacks_does_not_block_real_users(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        for i in range(60):
            resp = await _callback(
                client, code="x", state=f"bogus-{i}", cookie=json.dumps(
                    {"state": f"bogus-{i}", "nonce": "n", "code_verifier": "v"}
                ),
            )
            assert _outcome(resp) == "failed"
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        state, cookie = await _start(
            client, auth_headers, flow="authorize", installation_id=ACME_INSTALL.id
        )
        assert _outcome(await _callback(client, code="c1", state=state, cookie=cookie)) == "connected"

    def test_limiter_not_keyed_on_client_address(self):
        import inspect

        src = inspect.getsource(settings_routes.github_claim_callback)
        assert "request.client" not in src
        assert "x-forwarded-for" not in src.lower()


# --- 9 ---------------------------------------------------------------------


class TestFinding9HttpsOnly:
    @pytest.fixture
    def base_env(self, monkeypatch):
        monkeypatch.setenv("SFS_GITHUB_APP_CLIENT_ID", "Iv1.x")
        monkeypatch.setenv("SFS_GITHUB_APP_CLIENT_SECRET", "s")
        monkeypatch.setenv("SFS_GITHUB_OAUTH_CALLBACK_URL", PKCE_CALLBACK)
        monkeypatch.delenv("SFS_GITHUB_OAUTH_INSECURE_LOCALHOST", raising=False)

    def test_http_dashboard_disables_claims(self, base_env, monkeypatch):
        monkeypatch.setenv("SFS_DASHBOARD_URL", "http://sessionfs.internal")
        assert github_oauth.load_config() is None
        monkeypatch.setenv("SFS_GITHUB_OAUTH_INSECURE_LOCALHOST", "1")
        assert github_oauth.load_config() is None  # dev mode is localhost-only

    def test_http_callback_disables_claims(self, base_env, monkeypatch):
        monkeypatch.setenv("SFS_DASHBOARD_URL", "https://app.test")
        monkeypatch.setenv(
            "SFS_GITHUB_OAUTH_CALLBACK_URL", "http://api.test" + github_oauth.PKCE_CALLBACK_PATH
        )
        assert github_oauth.load_config() is None

    def test_localhost_needs_explicit_dev_flag(self, base_env, monkeypatch):
        monkeypatch.setenv("SFS_DASHBOARD_URL", "http://localhost:5173")
        monkeypatch.setenv(
            "SFS_GITHUB_OAUTH_CALLBACK_URL", "http://localhost:8000" + github_oauth.PKCE_CALLBACK_PATH
        )
        assert github_oauth.load_config() is None
        monkeypatch.setenv("SFS_GITHUB_OAUTH_INSECURE_LOCALHOST", "1")
        cfg = github_oauth.load_config()
        assert cfg is not None and cfg.secure_cookie is False

    def test_https_keeps_secure_cookie(self, base_env, monkeypatch):
        monkeypatch.setenv("SFS_DASHBOARD_URL", "https://app.test")
        cfg = github_oauth.load_config()
        assert cfg is not None and cfg.secure_cookie is True

    @pytest.mark.asyncio
    async def test_localhost_dev_cookie_not_secure(
        self, client: AsyncClient, auth_headers, base_env, monkeypatch
    ):
        monkeypatch.setenv("SFS_DASHBOARD_URL", "http://localhost:5173")
        monkeypatch.setenv(
            "SFS_GITHUB_OAUTH_CALLBACK_URL", "http://localhost:8000" + github_oauth.PKCE_CALLBACK_PATH
        )
        monkeypatch.setenv("SFS_GITHUB_OAUTH_INSECURE_LOCALHOST", "1")
        resp = await client.post(
            "/api/v1/settings/github/oauth/start", headers=auth_headers, json={"flow": "authorize"}
        )
        assert resp.status_code == 200
        assert "secure" not in resp.headers["set-cookie"].lower()

    @pytest.mark.asyncio
    async def test_get_reports_unavailable_over_http(
        self, client: AsyncClient, auth_headers, base_env, monkeypatch
    ):
        monkeypatch.setenv("SFS_DASHBOARD_URL", "http://sessionfs.internal")
        data = (await client.get("/api/v1/settings/github", headers=auth_headers)).json()
        assert data["claim_available"] is False


# --- 10 --------------------------------------------------------------------


class TestFinding10OnePerFlow:
    async def _choose_flow(self, client, auth_headers, fake_github):
        fake_github.add_code(
            "c1", ALICE_GH, [ACME_INSTALL, BETA_INSTALL], admin_orgs=["acme", "beta"]
        )
        state, cookie = await _start(client, auth_headers, flow="authorize")
        return await _callback(client, code="c1", state=state, cookie=cookie)

    @pytest.mark.asyncio
    async def test_several_eligible_links_none_and_offers_chooser(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        resp = await self._choose_flow(client, auth_headers, fake_github)
        assert _outcome(resp) == "choose"
        assert await _row(db_session, ACME_INSTALL.id) is None
        assert await _row(db_session, BETA_INSTALL.id) is None
        data = (
            await client.get("/api/v1/settings/github/oauth/choices", headers=auth_headers)
        ).json()
        assert sorted(c["installation_id"] for c in data["choices"]) == [
            ACME_INSTALL.id, BETA_INSTALL.id,
        ]
        # The parked proof never contains a token.
        attempt = (
            await db_session.execute(
                select(GitHubClaimAttempt).where(GitHubClaimAttempt.status == "choosing")
            )
        ).scalars().one()
        assert "token" not in attempt.choices_json

    @pytest.mark.asyncio
    async def test_choose_links_exactly_one_and_is_single_use(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        await self._choose_flow(client, auth_headers, fake_github)
        resp = await client.post(
            "/api/v1/settings/github/oauth/choose",
            headers=auth_headers,
            json={"installation_id": BETA_INSTALL.id},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["account_login"] == "beta"
        assert (await _row(db_session, BETA_INSTALL.id)).user_id == test_user.id
        assert await _row(db_session, ACME_INSTALL.id) is None
        again = await client.post(
            "/api/v1/settings/github/oauth/choose",
            headers=auth_headers,
            json={"installation_id": ACME_INSTALL.id},
        )
        assert again.status_code == 409
        assert await _row(db_session, ACME_INSTALL.id) is None

    @pytest.mark.asyncio
    async def test_choose_rejects_ids_outside_the_proof(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        await self._choose_flow(client, auth_headers, fake_github)
        resp = await client.post(
            "/api/v1/settings/github/oauth/choose",
            headers=auth_headers,
            json={"installation_id": 999999},
        )
        assert resp.status_code == 409
        assert await _row(db_session, 999999) is None

    @pytest.mark.asyncio
    async def test_other_user_cannot_use_my_chooser(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        await self._choose_flow(client, auth_headers, fake_github)
        _, other_headers = await _make_user(db_session, f"o{uuid.uuid4().hex[:6]}@example.com")
        data = (
            await client.get("/api/v1/settings/github/oauth/choices", headers=other_headers)
        ).json()
        assert data["choices"] == []
        resp = await client.post(
            "/api/v1/settings/github/oauth/choose",
            headers=other_headers,
            json={"installation_id": ACME_INSTALL.id},
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_expired_chooser_refused(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        await self._choose_flow(client, auth_headers, fake_github)
        attempt = (
            await db_session.execute(
                select(GitHubClaimAttempt).where(GitHubClaimAttempt.status == "choosing")
            )
        ).scalars().one()
        attempt.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db_session.commit()
        resp = await client.post(
            "/api/v1/settings/github/oauth/choose",
            headers=auth_headers,
            json={"installation_id": ACME_INSTALL.id},
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_targeted_flow_links_only_the_target(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        fake_github.add_code(
            "c1", ALICE_GH, [ACME_INSTALL, BETA_INSTALL], admin_orgs=["acme", "beta"]
        )
        state, cookie = await _start(
            client, auth_headers, flow="authorize", installation_id=BETA_INSTALL.id
        )
        assert _outcome(await _callback(client, code="c1", state=state, cookie=cookie)) == "connected"
        assert await _row(db_session, ACME_INSTALL.id) is None
        assert (await _row(db_session, BETA_INSTALL.id)) is not None


class TestFinding11ChooserProofTimestamp:
    @pytest.mark.asyncio
    async def test_choose_stamps_claim_with_callback_proof_time(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        """The chooser path must stamp claim_verified_at with the callback-time
        proof timestamp, not bind time: a privileged role that sat in the
        chooser for minutes must not look like a fresh proof at choose time."""
        fake_github.add_code(
            "c1", ALICE_GH, [ACME_INSTALL, BETA_INSTALL], admin_orgs=["acme", "beta"]
        )
        state, cookie = await _start(client, auth_headers, flow="authorize")
        assert _outcome(await _callback(client, code="c1", state=state, cookie=cookie)) == "choose"

        attempt = (
            await db_session.execute(
                select(GitHubClaimAttempt).where(GitHubClaimAttempt.status == "choosing")
            )
        ).scalars().one()
        data = json.loads(attempt.choices_json)
        assert "proof_verified_at" in data
        # Rewind the parked proof timestamp to simulate the chooser sitting
        # for most of its TTL before the user picks.
        proof_time = datetime.now(timezone.utc) - timedelta(minutes=9)
        data["proof_verified_at"] = proof_time.isoformat()
        attempt.choices_json = json.dumps(data)
        await db_session.commit()

        resp = await client.post(
            "/api/v1/settings/github/oauth/choose",
            headers=auth_headers,
            json={"installation_id": BETA_INSTALL.id},
        )
        assert resp.status_code == 200, resp.text
        row = await _row(db_session, BETA_INSTALL.id)
        assert row is not None
        assert row.claim_verified_at is not None
        stamped = settings_routes._as_aware(row.claim_verified_at)
        # claim_verified_at reflects the callback-time proof, not bind time.
        assert abs((stamped - proof_time).total_seconds()) < 5
        assert (datetime.now(timezone.utc) - stamped).total_seconds() > 60


def test_dashboard_constant_is_https():
    assert DASHBOARD.startswith("https://")
    assert parse_qs(urlparse(f"{DASHBOARD}/settings?github=choose").query)["github"] == ["choose"]
    assert GitHubInstallation.__tablename__ == "github_installations"


# --- Round 2 ---------------------------------------------------------------


class TestR2InstallHopCodeStructurallyUnredeemable:
    @pytest.mark.asyncio
    async def test_leaked_install_hop_code_refused_in_authorize_attempt(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        """GitHub would accept this non-PKCE code with ANY verifier, but only
        for the install redirect_uri. Replayed through an attacker's own
        authorize attempt, it is exchanged with the PKCE callback as
        redirect_uri and refused."""
        fake_github.add_code(
            "leaked", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"],
            redirect_uri=INSTALL_RETURN, required_verifier=None,
        )
        state, cookie = await _start(client, auth_headers, flow="authorize")
        resp = await _callback(client, code="leaked", state=state, cookie=cookie)
        assert _outcome(resp) == "failed"
        assert fake_github.redeemed == []
        assert await _row(db_session, ACME_INSTALL.id) is None

    @pytest.mark.asyncio
    async def test_control_same_code_bound_to_pkce_callback_would_link(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        """Control for the test above: the fake really does accept a
        non-PKCE code when the redirect_uri matches, so the refusal there
        comes from the redirect_uri separation."""
        fake_github.add_code(
            "unbound", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"],
            redirect_uri=PKCE_CALLBACK, required_verifier=None,
        )
        state, cookie = await _start(client, auth_headers, flow="authorize")
        resp = await _callback(client, code="unbound", state=state, cookie=cookie)
        assert _outcome(resp) == "connected"

    @pytest.mark.asyncio
    async def test_install_return_never_exchanges(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        fake_github.add_install_time_code("hop", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        state, cookie = await _start(client, auth_headers, flow="install")
        resp = await _install_return(
            client, state=state, cookie=cookie, installation_id=ACME_INSTALL.id, code="hop"
        )
        assert _outcome(resp) == "failed"  # misconfiguration refused
        assert fake_github.exchanged == []
        # Without a code the hop continues to a PKCE authorize on /oauth/callback.
        state, cookie = await _start(client, auth_headers, flow="install")
        resp = await _install_return(
            client, state=state, cookie=cookie, installation_id=ACME_INSTALL.id
        )
        q = parse_qs(urlparse(resp.headers["location"]).query)
        assert q["redirect_uri"] == [PKCE_CALLBACK]

    @pytest.mark.asyncio
    async def test_install_time_code_bound_to_first_callback_refused_at_pkce_callback(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        """R4 finding 1: with OAuth-on-install ON, GitHub sends the non-PKCE
        code to the FIRST registered callback. With install-return first (as
        documented), the code is bound there and refused at /oauth/callback."""
        assert fake_github.registered_callbacks[0] == INSTALL_RETURN
        fake_github.add_install_time_code("leak", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        state, cookie = await _start(client, auth_headers, flow="authorize")
        resp = await _callback(client, code="leak", state=state, cookie=cookie)
        assert _outcome(resp) == "failed"
        assert fake_github.redeemed == []
        assert await _row(db_session, ACME_INSTALL.id) is None

    @pytest.mark.asyncio
    async def test_misordered_callbacks_would_be_exploitable(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        """Why the documented order matters: with /oauth/callback first, the
        same install-time code is redeemable there."""
        fake_github.registered_callbacks = [PKCE_CALLBACK, INSTALL_RETURN]
        fake_github.add_install_time_code("leak", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        state, cookie = await _start(client, auth_headers, flow="authorize")
        resp = await _callback(client, code="leak", state=state, cookie=cookie)
        assert _outcome(resp) == "connected"

    def test_docs_require_install_return_first(self):
        from pathlib import Path

        doc = (Path(__file__).resolve().parents[3] / "docs" / "github-app.md").read_text()
        i = doc.index("oauth/install-return` 2.")
        assert doc.index("oauth/callback", i) > i
        assert "in this order" in doc

    @pytest.mark.asyncio
    async def test_install_attempt_refused_at_pkce_callback(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        state, cookie = await _start(client, auth_headers, flow="install")
        resp = await _callback(
            client, code="c1", state=state, cookie=cookie, installation_id=ACME_INSTALL.id
        )
        assert _outcome(resp) == "failed"
        assert fake_github.exchanged == []
        # Not consumed by the wrong endpoint: the real install return still works.
        hop = await _install_return(
            client, state=state, cookie=cookie, installation_id=ACME_INSTALL.id
        )
        assert hop.headers["location"].startswith("https://github.com/login/oauth/authorize?")

    @pytest.mark.asyncio
    async def test_authorize_attempt_refused_at_install_return(
        self, client: AsyncClient, auth_headers, db_session, fake_github
    ):
        state, cookie = await _start(client, auth_headers, flow="authorize")
        resp = await _install_return(client, state=state, cookie=cookie)
        assert _outcome(resp) == "failed"

    def test_docs_say_oauth_during_install_stays_off(self):
        from pathlib import Path

        doc = (Path(__file__).resolve().parents[3] / "docs" / "github-app.md").read_text()
        assert "Request user authorization (OAuth) during installation" in doc
        assert "OFF" in doc
        assert "install-return" in doc


class TestR2MemberReverify:
    @pytest.mark.asyncio
    async def test_member_claim_reports_inactive_and_can_upgrade(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        await _create_installation(
            db_session, installation_id=ACME_INSTALL.id, user_id=test_user.id, verified=True,
            role="member",
        )
        data = (await client.get("/api/v1/settings/github", headers=auth_headers)).json()
        assert data["repo_verification_active"] is False
        assert data["proof_stale"] is False
        assert data["installation_id"] == ACME_INSTALL.id
        # Members:read approved since: the targeted re-check upgrades the role.
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        state, cookie = await _start(
            client, auth_headers, flow="authorize", installation_id=ACME_INSTALL.id
        )
        assert _outcome(await _callback(client, code="c1", state=state, cookie=cookie)) == "connected"
        data = (await client.get("/api/v1/settings/github", headers=auth_headers)).json()
        assert data["claim_role"] == "org_admin"
        assert data["repo_verification_active"] is True


class TestR2WebhookUpsertRace:
    @pytest.mark.asyncio
    async def test_upsert_merges_into_existing_row(self, db_session):
        from sessionfs.server.routes.webhooks import _upsert_installation_metadata

        db_session.add(GitHubInstallation(id=700100, account_login="old", user_id=None))
        await db_session.commit()
        await _upsert_installation_metadata(
            db_session, installation_id=700100, account_login="acme",
            account_type="Organization", account_id=9001, installer_github_user_id=1001,
        )
        row = await _row(db_session, 700100)
        assert (row.account_login, row.account_id, row.installer_github_user_id) == (
            "acme", 9001, 1001,
        )
        # Nulls never erase stored values.
        await _upsert_installation_metadata(
            db_session, installation_id=700100, account_login=None,
            account_type=None, account_id=None, installer_github_user_id=None,
        )
        row = await _row(db_session, 700100)
        assert row.installer_github_user_id == 1001 and row.account_login == "acme"

    @pytest.mark.asyncio
    async def test_upsert_never_touches_the_claim(self, db_session, test_user):
        from sessionfs.server.routes.webhooks import _upsert_installation_metadata

        await _create_installation(
            db_session, installation_id=700101, user_id=test_user.id, verified=True,
            role="org_admin",
        )
        await _upsert_installation_metadata(
            db_session, installation_id=700101, account_login="x",
            account_type="Organization", account_id=1, installer_github_user_id=666,
        )
        row = await _row(db_session, 700101)
        assert row.user_id == test_user.id and row.claim_role == "org_admin"
        assert row.claim_verified_github_user_id == 1001

    @pytest.mark.asyncio
    async def test_concurrent_callback_insert_and_webhook_keep_installer(
        self, db_engine
    ):
        """The callback's insert and the webhook race; whichever lands first,
        the installer id survives (no select-then-insert, no swallowed
        IntegrityError)."""
        import asyncio

        from sqlalchemy.ext.asyncio import async_sessionmaker

        from sessionfs.server.routes.settings import _ensure_installation_row
        from sessionfs.server.routes.webhooks import _upsert_installation_metadata

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        for order in ("callback_first", "webhook_first"):
            iid = 700200 if order == "callback_first" else 700201
            info = github_oauth.UserInstallation(
                id=iid, account_id=9001, account_login="acme", account_type="Organization"
            )

            async def callback():
                async with factory() as db:
                    await _ensure_installation_row(db, info)

            async def webhook():
                async with factory() as db:
                    await _upsert_installation_metadata(
                        db, installation_id=iid, account_login="acme",
                        account_type="Organization", account_id=9001,
                        installer_github_user_id=1001,
                    )

            first, second = (callback, webhook) if order == "callback_first" else (webhook, callback)
            await asyncio.gather(first(), second())
            async with factory() as db:
                row = await db.get(GitHubInstallation, iid)
                assert row is not None
                assert row.installer_github_user_id == 1001, order


class TestR4OneInstallationPerUser:
    @pytest.mark.asyncio
    async def test_linking_second_installation_detaches_first(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        await _create_installation(
            db_session, installation_id=ACME_INSTALL.id, user_id=test_user.id, verified=True,
            role="org_admin",
        )
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL, BETA_INSTALL], admin_orgs=["beta"])
        state, cookie = await _start(
            client, auth_headers, flow="authorize", installation_id=BETA_INSTALL.id
        )
        assert _outcome(await _callback(client, code="c1", state=state, cookie=cookie)) == "connected"

        old = await _row(db_session, ACME_INSTALL.id)
        assert old.user_id is None
        assert old.auto_comment is False  # its PR comments stop
        assert old.claim_verified_at is None and old.claim_role is None
        new = await _row(db_session, BETA_INSTALL.id)
        assert new.user_id == test_user.id
        data = (await client.get("/api/v1/settings/github", headers=auth_headers)).json()
        assert data["installation_id"] == BETA_INSTALL.id
        rows = (
            await db_session.execute(
                select(GitHubInstallation)
                .where(GitHubInstallation.user_id == test_user.id)
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        assert [r.id for r in rows] == [BETA_INSTALL.id]

    @pytest.mark.asyncio
    async def test_reverifying_same_installation_keeps_it(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        await _create_installation(
            db_session, installation_id=ACME_INSTALL.id, user_id=test_user.id
        )
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        state, cookie = await _start(
            client, auth_headers, flow="authorize", installation_id=ACME_INSTALL.id
        )
        assert _outcome(await _callback(client, code="c1", state=state, cookie=cookie)) == "connected"
        row = await _row(db_session, ACME_INSTALL.id)
        assert row.user_id == test_user.id and row.auto_comment is True

    @pytest.mark.asyncio
    async def test_detach_does_not_touch_other_users(
        self, client: AsyncClient, auth_headers, test_user, db_session, fake_github
    ):
        other, _ = await _make_user(db_session, "other-detach@example.com")
        await _create_installation(
            db_session, installation_id=BETA_INSTALL.id, user_id=other.id, verified=True,
            role="org_admin",
        )
        fake_github.add_code("c1", ALICE_GH, [ACME_INSTALL], admin_orgs=["acme"])
        state, cookie = await _start(
            client, auth_headers, flow="authorize", installation_id=ACME_INSTALL.id
        )
        assert _outcome(await _callback(client, code="c1", state=state, cookie=cookie)) == "connected"
        assert (await _row(db_session, BETA_INSTALL.id)).user_id == other.id

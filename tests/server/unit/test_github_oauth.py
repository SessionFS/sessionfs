"""Unit tests for the GitHub App OAuth helpers used by installation claims."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from sessionfs.server.services import github_oauth


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SFS_GITHUB_APP_CLIENT_ID", "Iv1.abc")
    monkeypatch.setenv("SFS_GITHUB_APP_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("SFS_DASHBOARD_URL", "https://app.example.test/")
    monkeypatch.delenv("SFS_GITHUB_APP_SLUG", raising=False)
    monkeypatch.setenv(
        "SFS_GITHUB_OAUTH_CALLBACK_URL",
        "https://api.test/api/v1/settings/github/oauth/callback",
    )


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _mock_transport(monkeypatch, handler):
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(github_oauth.httpx, "AsyncClient", factory)


def test_config_requires_id_secret_dashboard(env, monkeypatch):
    cfg = github_oauth.load_config()
    assert cfg is not None
    assert cfg.dashboard_url == "https://app.example.test"
    for name in (
        "SFS_GITHUB_APP_CLIENT_ID",
        "SFS_GITHUB_APP_CLIENT_SECRET",
        "SFS_DASHBOARD_URL",
        "SFS_GITHUB_OAUTH_CALLBACK_URL",
    ):
        saved = github_oauth.os.environ[name]
        monkeypatch.delenv(name)
        assert github_oauth.load_config() is None
        monkeypatch.setenv(name, saved)


def test_callback_url_must_be_the_pkce_callback(env, monkeypatch):
    for bad in (
        "https://api.test/api/v1/settings/github/oauth/install-return",
        "https://api.test/cb",
    ):
        monkeypatch.setenv("SFS_GITHUB_OAUTH_CALLBACK_URL", bad)
        assert github_oauth.load_config() is None


def test_dashboard_url_must_be_http(env, monkeypatch):
    monkeypatch.setenv("SFS_DASHBOARD_URL", "javascript:alert(1)")
    assert github_oauth.load_config() is None


def test_authorize_url_has_no_secret(env, monkeypatch):
    cfg = github_oauth.load_config()
    assert cfg is not None
    url = github_oauth.build_authorize_url(cfg, state="st", code_challenge="cc")
    q = parse_qs(urlparse(url).query)
    assert q["redirect_uri"] == ["https://api.test/api/v1/settings/github/oauth/callback"]
    assert q["state"] == ["st"]
    assert "s3cret" not in url


@pytest.mark.asyncio
async def test_exchange_code_error_payload_raises(env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "github.com"
        body = request.content.decode()
        assert "code_verifier=v" in body
        # Always bound to the PKCE callback, never left to GitHub's default.
        assert "redirect_uri=https%3A%2F%2Fapi.test%2Fapi%2Fv1%2Fsettings%2Fgithub%2Foauth%2Fcallback" in body
        return httpx.Response(200, json={"error": "bad_verification_code"})

    _mock_transport(monkeypatch, handler)
    cfg = github_oauth.load_config()
    assert cfg is not None
    with pytest.raises(github_oauth.GitHubOAuthError):
        await github_oauth.exchange_code(cfg, code="c", code_verifier="v")


@pytest.mark.asyncio
async def test_exchange_code_success(env, monkeypatch):
    _mock_transport(
        monkeypatch, lambda r: httpx.Response(200, json={"access_token": "ghu_x"})
    )
    cfg = github_oauth.load_config()
    assert cfg is not None
    assert await github_oauth.exchange_code(cfg, code="c", code_verifier="v") == "ghu_x"


@pytest.mark.asyncio
async def test_list_installations_paginates_and_skips_junk(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com"
        assert request.headers["authorization"] == "Bearer tok"
        page = int(request.url.params["page"])
        if page == 1:
            items = [
                {"id": i, "account": {"id": 10, "login": "acme", "type": "Organization"}}
                for i in range(1, 101)
            ]
            items[0] = {"id": True}  # bool is not an id
            return httpx.Response(200, json={"installations": items})
        return httpx.Response(
            200, json={"installations": [{"id": 500, "account": "not-a-dict"}]}
        )

    _mock_transport(monkeypatch, handler)
    found = await github_oauth.list_user_installations("tok")
    ids = [i.id for i in found]
    assert 1 not in ids and 2 in ids and 500 in ids
    assert len(found) == 100


@pytest.mark.asyncio
async def test_list_installations_non_200_raises(monkeypatch):
    _mock_transport(monkeypatch, lambda r: httpx.Response(401, json={}))
    with pytest.raises(github_oauth.GitHubOAuthError):
        await github_oauth.list_user_installations("tok")


@pytest.mark.asyncio
async def test_get_user_rejects_bool_id(monkeypatch):
    _mock_transport(monkeypatch, lambda r: httpx.Response(200, json={"id": True, "login": "x"}))
    with pytest.raises(github_oauth.GitHubOAuthError):
        await github_oauth.get_user("tok")


@pytest.mark.asyncio
async def test_determine_role(monkeypatch):
    gh = github_oauth.GitHubUser(id=7, login="alice")
    own = github_oauth.UserInstallation(id=1, account_id=7, account_login="alice", account_type="User")
    other = github_oauth.UserInstallation(id=2, account_id=8, account_login="bob", account_type="User")
    org = github_oauth.UserInstallation(id=3, account_id=9, account_login="acme", account_type="Organization")
    assert await github_oauth.determine_role("t", gh, own) == "account_owner"
    assert await github_oauth.determine_role("t", gh, other) == "member"

    _mock_transport(
        monkeypatch, lambda r: httpx.Response(200, json={"role": "admin", "state": "active"})
    )
    assert await github_oauth.determine_role("t", gh, org) == "org_admin"
    _mock_transport(
        monkeypatch, lambda r: httpx.Response(200, json={"role": "admin", "state": "pending"})
    )
    assert await github_oauth.determine_role("t", gh, org) == "member"
    _mock_transport(monkeypatch, lambda r: httpx.Response(403, json={}))
    assert await github_oauth.determine_role("t", gh, org) == "member"


@pytest.mark.asyncio
async def test_org_admin_check_refuses_odd_logins(monkeypatch):
    def handler(request):  # pragma: no cover - must not be called
        raise AssertionError("no request expected")

    _mock_transport(monkeypatch, handler)
    assert await github_oauth.is_active_org_admin("t", "../../admin") is False
    assert await github_oauth.is_active_org_admin("t", "") is False

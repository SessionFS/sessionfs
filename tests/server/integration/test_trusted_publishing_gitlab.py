"""Trusted publishing over GitLab CI ID tokens (GitHub #82): issuer
allowlist (no fetch for anything else), token validation, binding to the
numeric project id / protected ref / ref pattern / environment / pipeline
source, replay protection, provenance, and GitHub trusts left unchanged."""

from __future__ import annotations

import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import SkillPackageVersion, SkillPublishToken, SkillTrustedPublisher
from sessionfs.server.services import github_oidc, gitlab_oidc
from sessionfs.server.services import publishers as svc
from sessionfs.server.services.oidc_token import GITHUB_ACTIONS_ISSUER
from tests.server.integration.test_marketplace_publishers import (  # noqa: F401
    _fresh_publisher_limiters,
    _claim,
    _error,
    _pkg,
)
from tests.server.integration.test_trusted_publishing import (
    AUDIENCE,
    _Rel,
    _claims as _github_claims,
    _jwks as _github_jwks,
    _sign as _github_sign,
)

pytestmark = pytest.mark.asyncio

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
GITLAB = "https://gitlab.com"
SELF_HOSTED = "https://gitlab.acme.example"
PROJECT_ID, NAMESPACE_ID = "4242", "77"
SHA = "89abcdef0123456789abcdef0123456789abcdef"
_REAL_FETCH_JWKS = gitlab_oidc.fetch_jwks


def _jwks(key=None) -> dict:
    jwk = json.loads(RSAAlgorithm.to_jwk((key or _KEY).public_key()))
    jwk.update({"kid": "gl1", "alg": "RS256", "use": "sig"})
    return {"keys": [jwk]}


@pytest.fixture(autouse=True)
def _gitlab_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SFS_TRUSTED_PUBLISHING_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("SFS_GITLAB_OIDC_ISSUERS", f"{GITLAB},{SELF_HOSTED}")
    fetched: list[str] = []

    async def no_network(issuer: str) -> dict:
        fetched.append(issuer)
        raise AssertionError(f"unexpected JWKS fetch for {issuer}")

    monkeypatch.setattr(gitlab_oidc, "fetch_jwks", no_network)
    gitlab_oidc.set_jwks_for_tests(None)
    gitlab_oidc.set_jwks_for_tests(GITLAB, _jwks())
    gitlab_oidc.set_jwks_for_tests(SELF_HOSTED, _jwks())
    github_oidc.set_jwks_for_tests(_github_jwks())
    yield fetched
    gitlab_oidc.set_jwks_for_tests(None)
    github_oidc.set_jwks_for_tests(None)


def _claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": GITLAB,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now - 5,
        "exp": now + 3600,
        "jti": str(uuid.uuid4()),
        "sub": "project_path:acme/skills:ref_type:tag:ref:v1.0.0",
        "namespace_id": NAMESPACE_ID,
        "namespace_path": "acme",
        "project_id": PROJECT_ID,
        "project_path": "acme/skills",
        "user_id": "9",
        "user_login": "dev",
        "user_email": "dev@acme.example",
        "pipeline_id": "1001",
        "pipeline_source": "push",
        "job_id": "2002",
        "ref": "v1.0.0",
        "ref_type": "tag",
        "ref_path": "refs/tags/v1.0.0",
        "ref_protected": "true",
        "environment": "production",
        "environment_protected": "true",
        "runner_environment": "gitlab-hosted",
        "sha": SHA,
        "ci_config_ref_uri": "gitlab.com/acme/skills//.gitlab-ci.yml@refs/tags/v1.0.0",
        "ci_config_sha": SHA,
        "project_visibility": "private",
    }
    for key, value in overrides.items():
        if isinstance(value, _Rel):
            claims[key] = now + value.seconds
        elif value is None:
            claims.pop(key, None)
        else:
            claims[key] = value
    return claims


def _sign(claims: dict, *, key=None, kid: str = "gl1", alg: str = "RS256") -> str:
    return jwt.encode(claims, key or _KEY, algorithm=alg, headers={"kid": kid})


async def _trust(client: AsyncClient, headers: dict, publisher_id: str, **extra) -> dict:
    body = {
        "provider": "gitlab",
        "gitlab_project_id": int(PROJECT_ID),
        "repository": "acme/skills",
        "environment": "production",
        "ref_pattern": "refs/tags/v*",
        **extra,
    }
    body = {key: value for key, value in body.items() if value is not None}
    response = await client.post(f"/api/v1/publishers/{publisher_id}/trusted-publishers", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _exchange(client: AsyncClient, token: str, **extra):
    body = {"token": token, "provider": "gitlab", **extra}
    return await client.post("/api/v1/marketplace/trusted-publishing/token", json=body)


async def _setup(client: AsyncClient, headers: dict, namespace: str = "acme", **trust) -> tuple[dict, dict]:
    publisher = await _claim(client, headers, namespace)
    trusted = await _trust(client, headers, publisher["id"], **trust)
    return publisher, trusted


# ---------------------------------------------------------------------------
# Happy path, provenance, replay
# ---------------------------------------------------------------------------


async def test_exchange_and_publish_records_gitlab_provenance(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    publisher, trusted = await _setup(client, auth_headers)
    assert trusted["provider"] == "gitlab" and trusted["gitlab_host"] == GITLAB
    assert trusted["gitlab_project_id"] == int(PROJECT_ID) and trusted["repository_id"] is None
    assert trusted["workflow"] is None

    exchanged = await _exchange(client, _sign(_claims()), namespace="acme")
    assert exchanged.status_code == 200, exchanged.text
    body = exchanged.json()
    assert body["token"].startswith("sfs_tp_") and body["trusted_publisher_id"] == trusted["id"]
    row = (await db_session.execute(select(SkillPublishToken))).scalar_one()
    assert row.oidc_issuer == GITLAB
    stored = json.loads(row.claims)
    # No e-mail address or login is kept.
    assert "user_email" not in stored and "user_login" not in stored
    assert stored["project_id"] == PROJECT_ID

    published = await client.post(
        "/api/v1/marketplace/trusted-publishing/publish",
        headers={"Authorization": f"Bearer {body['token']}"},
        files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
    )
    assert published.status_code == 201, published.text
    result = published.json()
    assert result["publisher_id"] == publisher["id"]
    assert result["source"]["locator"] == "https://gitlab.com/acme/skills"
    signature = result["signature"]
    assert signature["kind"] == "gitlab_oidc" and signature["verification"] == "oidc_token"
    assert signature["issuer"] == GITLAB
    assert signature["published_from_repo"] == "gitlab.com/acme/skills"
    assert signature["published_from_project_id"] == int(PROJECT_ID)
    assert signature["published_from_namespace_id"] == int(NAMESPACE_ID)
    # A GitLab project id is never recorded where a GitHub repository id goes.
    assert "published_from_repo_id" not in signature
    assert signature["publishing_ref"] == "refs/tags/v1.0.0"
    assert signature["publishing_ref_protected"] is True
    assert signature["publishing_environment"] == "production"
    assert signature["publishing_environment_protected"] is True
    assert signature["publishing_event"] == "push"
    assert signature["publishing_run_sha"] == SHA
    assert signature["publishing_run_url"] == "https://gitlab.com/acme/skills/-/jobs/2002"
    version = await db_session.get(SkillPackageVersion, result["version_id"])

    from sessionfs.server.services.skill_policy import verified_source
    from sessionfs.skills.discovery import provenance_attested

    # The org policy's GitHub-repository allowlist is not satisfied by a
    # GitLab publish, while discovery still counts it as attested.
    assert verified_source(version) == (None, None)
    assert provenance_attested(version.signature_metadata) is True


async def test_jti_replay_is_refused(client: AsyncClient, auth_headers: dict) -> None:
    await _setup(client, auth_headers)
    token = _sign(_claims())
    assert (await _exchange(client, token)).status_code == 200
    replay = await _exchange(client, token)
    assert replay.status_code == 401
    assert _error(replay)["code"] == "oidc_token_replayed"


@pytest.mark.parametrize("source", ["push", "web", "api"])
async def test_publishing_pipeline_sources(client: AsyncClient, auth_headers: dict, source: str) -> None:
    await _setup(client, auth_headers)
    response = await _exchange(client, _sign(_claims(pipeline_source=source)))
    assert response.status_code == 200, response.text


async def test_branch_ref_and_unconstrained_trust(client: AsyncClient, auth_headers: dict) -> None:
    await _setup(client, auth_headers, environment=None, ref_pattern=None)
    response = await _exchange(client, _sign(_claims(
        ref="main", ref_type="branch", ref_path="refs/heads/main", environment=None,
    )))
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claims,reason",
    [
        ({"aud": "https://other.example"}, "invalid_aud"),
        ({"aud": "sessionfs"}, "invalid_aud"),
        ({"aud": [AUDIENCE, "other"]}, "invalid_aud"),
        ({"exp": _Rel(-120), "iat": _Rel(-400), "nbf": _Rel(-400)}, "token_expired"),
        ({"iat": _Rel(-4000), "nbf": _Rel(-4000), "exp": _Rel(600)}, "token_too_old"),
        ({"exp": _Rel(200000)}, "token_lifetime"),
        ({"jti": None}, "claim_missing"),
        ({"project_id": None}, "claim_missing"),
        ({"project_id": "12a"}, "claim_invalid"),
        ({"project_id": 4242}, "claim_missing"),
        ({"namespace_id": "x"}, "claim_invalid"),
        ({"ref_path": None}, "claim_missing"),
        ({"ref_protected": None}, "claim_missing"),
        ({"pipeline_source": None}, "claim_missing"),
        ({"ref_type": "merge_request"}, "claim_invalid"),
        ({"ref_path": "refs/heads/v1.0.0"}, "claim_invalid"),
        ({"ref_path": "refs/tags/v1.0.0-evil"}, "claim_invalid"),
        ({"ref_protected": True}, "claim_missing"),
        ({"ref_protected": "yes"}, "claim_invalid"),
        ({"sha": "abc"}, "claim_invalid"),
        ({"environment": 7}, "claim_invalid"),
    ],
)
async def test_invalid_tokens(client: AsyncClient, auth_headers: dict, claims: dict, reason: str) -> None:
    await _setup(client, auth_headers)
    response = await _exchange(client, _sign(_claims(**claims)))
    assert response.status_code == 401, response.text
    assert _error(response)["code"] == "invalid_oidc_token"
    assert _error(response)["details"]["reason"] == reason


async def test_algorithm_and_key_attacks(client: AsyncClient, auth_headers: dict) -> None:
    await _setup(client, auth_headers)
    unsigned = jwt.encode(_claims(), key=None, algorithm="none", headers={"kid": "gl1"})
    hmac_forged = jwt.encode(_claims(), "s" * 48, algorithm="HS256", headers={"kid": "gl1"})
    wrong_key = _sign(_claims(), key=_OTHER_KEY)
    for token, reason in (
        (unsigned, "alg_not_allowed"),
        (hmac_forged, "alg_not_allowed"),
        (wrong_key, "invalid_signature"),
        ("not-a-jwt", "token_parse_error"),
    ):
        response = await _exchange(client, token)
        assert response.status_code == 401, (reason, response.text)
        assert _error(response)["details"]["reason"] == reason


async def test_unknown_kid_refreshes_only_the_allowlisted_issuer(
    client: AsyncClient, auth_headers: dict, _gitlab_env: list[str]
) -> None:
    await _setup(client, auth_headers)
    response = await _exchange(client, _sign(_claims(), kid="rotated"))
    assert response.status_code == 401
    # One forced refresh attempt, against the token's (allowlisted) issuer.
    assert _gitlab_env == [GITLAB]


@pytest.mark.parametrize(
    "issuer",
    [
        "https://gitlab.evil.example",
        "https://gitlab.com.evil.example",
        "https://gitlab.com/",  # not byte-identical to the allowlisted origin
        "http://gitlab.com",
        "https://GITLAB.com",
        GITHUB_ACTIONS_ISSUER,
        "https://169.254.169.254",
    ],
)
async def test_unlisted_issuer_is_refused_without_any_fetch(
    client: AsyncClient, auth_headers: dict, _gitlab_env: list[str], issuer: str
) -> None:
    await _setup(client, auth_headers)
    calls: list[str] = []

    async def record(url: str, **_kwargs) -> dict:
        calls.append(url)
        raise AssertionError("no fetch for an unlisted issuer")

    import sessionfs.server.services.gitlab_oidc as module

    original = module.oidc_fetch_json
    module.oidc_fetch_json = record  # type: ignore[assignment]
    try:
        response = await _exchange(client, _sign(_claims(iss=issuer)))
    finally:
        module.oidc_fetch_json = original  # type: ignore[assignment]
    assert response.status_code == 401, response.text
    assert _error(response)["details"]["reason"] == "issuer_not_allowed"
    assert calls == [] and _gitlab_env == []
    assert issuer not in gitlab_oidc._caches


async def test_jwks_url_is_derived_from_the_allowlisted_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []

    async def fake(url: str, **_kwargs) -> dict:
        urls.append(url)
        return _jwks()

    monkeypatch.setattr(gitlab_oidc, "oidc_fetch_json", fake)
    monkeypatch.setattr(gitlab_oidc, "fetch_jwks", _REAL_FETCH_JWKS)
    gitlab_oidc.set_jwks_for_tests(None)
    assert await gitlab_oidc._jwks(SELF_HOSTED) == _jwks()
    # Cached: a second call does not fetch again.
    assert await gitlab_oidc._jwks(SELF_HOSTED) == _jwks()
    assert urls == ["https://gitlab.acme.example/oauth/discovery/keys"]


async def test_jwks_failure_is_negative_cached_per_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    from sessionfs.server.services.oidc_token import TokenValidationError

    calls: list[str] = []

    async def down(issuer: str) -> dict:
        calls.append(issuer)
        raise RuntimeError("GitLab unreachable")

    monkeypatch.setattr(gitlab_oidc, "fetch_jwks", down)
    gitlab_oidc.set_jwks_for_tests(None)
    for _ in range(3):
        with pytest.raises(TokenValidationError) as excinfo:
            await gitlab_oidc._jwks(GITLAB)
        assert excinfo.value.reason == "jwks_unavailable"
    assert calls == [GITLAB]


async def test_github_token_sent_as_gitlab_and_back(
    client: AsyncClient, auth_headers: dict, _gitlab_env: list[str]
) -> None:
    await _setup(client, auth_headers)
    github_token = _github_sign(_github_claims())
    as_gitlab = await _exchange(client, github_token)
    assert as_gitlab.status_code == 401
    assert _error(as_gitlab)["details"]["reason"] == "issuer_not_allowed"
    assert _gitlab_env == []
    gitlab_token = _sign(_claims())
    as_github = await client.post("/api/v1/marketplace/trusted-publishing/token", json={"token": gitlab_token})
    assert as_github.status_code == 401


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claims",
    [
        # Another project (same path after a delete-and-recreate, or a fork).
        {"project_id": "9999"},
        # Ref outside the pattern / branch instead of tag.
        {"ref": "main", "ref_type": "branch", "ref_path": "refs/heads/main"},
        {"ref": "x1.0", "ref_path": "refs/tags/x1.0"},
        # Unprotected ref: anyone with push access could run this pipeline.
        {"ref_protected": "false"},
        # Environment missing or different (exact, case-sensitive).
        {"environment": None},
        {"environment": "staging"},
        {"environment": "Production"},
        # Merge request / fork / downstream / other pipeline sources.
        {"pipeline_source": "merge_request_event"},
        {"pipeline_source": "external_pull_request_event"},
        {"pipeline_source": "parent_pipeline"},
        {"pipeline_source": "pipeline"},
        {"pipeline_source": "trigger"},
        {"pipeline_source": "schedule"},
        {"pipeline_source": "chat"},
        {"pipeline_source": "webide"},
        {"pipeline_source": "something_new"},
    ],
)
async def test_binding_mismatches_are_refused(client: AsyncClient, auth_headers: dict, claims: dict) -> None:
    await _setup(client, auth_headers)
    response = await _exchange(client, _sign(_claims(**claims)))
    assert response.status_code == 403, response.text
    assert _error(response)["code"] == "trusted_publisher_mismatch"


async def test_namespace_pin(client: AsyncClient, auth_headers: dict) -> None:
    await _setup(client, auth_headers, gitlab_namespace_id=int(NAMESPACE_ID))
    assert (await _exchange(client, _sign(_claims()))).status_code == 200
    # The project moved to another group: the pinned trust no longer matches.
    moved = await _exchange(client, _sign(_claims(namespace_id="78", namespace_path="evil")))
    assert moved.status_code == 403


async def test_unpinned_trust_warns_and_pinned_does_not(client: AsyncClient, auth_headers: dict) -> None:
    _, unpinned = await _setup(client, auth_headers)
    assert unpinned["gitlab_namespace_id"] is None
    assert any("not pinned" in warning for warning in unpinned["warnings"])
    _, pinned = await _setup(client, auth_headers, namespace="acme-pin", gitlab_namespace_id=int(NAMESPACE_ID))
    assert pinned["gitlab_namespace_id"] == int(NAMESPACE_ID)
    assert "warnings" not in pinned


async def test_require_protected_environment(client: AsyncClient, auth_headers: dict) -> None:
    # Off by default: an unprotected environment with the right name matches
    # (the documented weakness the opt-in closes).
    await _setup(client, auth_headers)
    assert (await _exchange(client, _sign(_claims(environment_protected="false")))).status_code == 200
    _, trusted = await _setup(client, auth_headers, namespace="acme-prot", require_protected_environment=True)
    assert trusted["require_protected_environment"] is True
    for claims in ({"environment_protected": "false"}, {"environment_protected": None}):
        refused = await _exchange(client, _sign(_claims(**claims)), namespace="acme-prot")
        assert refused.status_code == 403, refused.text
    ok = await _exchange(client, _sign(_claims()), namespace="acme-prot")
    assert ok.status_code == 200, ok.text


async def test_unprotected_environment_is_recorded(
    client: AsyncClient, auth_headers: dict
) -> None:
    await _setup(client, auth_headers)
    token = (await _exchange(client, _sign(_claims(environment_protected="false")))).json()["token"]
    published = await client.post(
        "/api/v1/marketplace/trusted-publishing/publish",
        headers={"Authorization": f"Bearer {token}"},
        files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
    )
    assert published.status_code == 201, published.text
    assert published.json()["signature"]["publishing_environment_protected"] is False


async def test_issuer_removed_after_minting_stops_the_publish_token(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    from sessionfs.server.config import ServerConfig

    await _setup(client, auth_headers, gitlab_host=SELF_HOSTED)
    exchanged = await _exchange(client, _sign(_claims(iss=SELF_HOSTED)))
    assert exchanged.status_code == 200, exchanged.text
    app = client._transport.app  # type: ignore[attr-defined]
    app.state.trusted_publishing = github_oidc.resolve_trusted_publishing(
        ServerConfig(database_url="sqlite+aiosqlite://", gitlab_oidc_issuers=GITLAB)
    )
    published = await client.post(
        "/api/v1/marketplace/trusted-publishing/publish",
        headers={"Authorization": f"Bearer {exchanged.json()['token']}"},
        files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
    )
    assert published.status_code == 401, published.text
    assert _error(published)["code"] == "invalid_publish_token"
    assert (await db_session.execute(select(SkillPackageVersion))).scalars().all() == []


def test_reauthorization_rechecks_the_issuer_allowlist() -> None:
    from types import SimpleNamespace

    from sessionfs.server.routes.marketplace_publishers import _trust_still_valid

    publisher = SimpleNamespace(id="p", status="active", auth_generation=0)
    gitlab = SimpleNamespace(provider="gitlab", issuer=SELF_HOSTED, revoked_at=None, publisher_id="p")
    github = SimpleNamespace(provider="github", issuer=None, revoked_at=None, publisher_id="p")
    assert _trust_still_valid(publisher, gitlab, 0, (GITLAB, SELF_HOSTED))  # type: ignore[arg-type]
    assert not _trust_still_valid(publisher, gitlab, 0, (GITLAB,))  # type: ignore[arg-type]
    assert _trust_still_valid(publisher, github, 0, ())  # type: ignore[arg-type]


@pytest.mark.parametrize("issuer", ["https://GitLab.com", "https://gitlab.com:443", "https://gitlab.com/"])
async def test_near_miss_issuer_names_the_expected_form(
    client: AsyncClient, auth_headers: dict, _gitlab_env: list[str], issuer: str
) -> None:
    await _setup(client, auth_headers)
    response = await _exchange(client, _sign(_claims(iss=issuer)))
    assert response.status_code == 401
    details = _error(response)["details"]
    assert details["reason"] == "issuer_not_allowed"
    assert "'https://gitlab.com'" in details["hint"] and "external_url" in details["hint"]
    assert _gitlab_env == []


async def test_unrelated_issuer_gets_no_hint(client: AsyncClient, auth_headers: dict) -> None:
    await _setup(client, auth_headers)
    response = await _exchange(client, _sign(_claims(iss="https://gitlab.evil.example")))
    assert response.status_code == 401
    assert "hint" not in _error(response)["details"]


async def test_trust_is_bound_to_its_own_instance(client: AsyncClient, auth_headers: dict) -> None:
    """The same numeric project id on another (allowlisted) GitLab instance is
    a different project."""
    await _setup(client, auth_headers)
    other = await _exchange(client, _sign(_claims(iss=SELF_HOSTED)))
    assert other.status_code == 403
    await _setup(client, auth_headers, namespace="acme-sh", gitlab_host=SELF_HOSTED)
    ok = await _exchange(client, _sign(_claims(iss=SELF_HOSTED)), namespace="acme-sh")
    assert ok.status_code == 200, ok.text
    assert ok.json()["namespace"] == "acme-sh"


async def test_issuer_removed_from_the_allowlist_stops_exchanges(
    client: AsyncClient, auth_headers: dict
) -> None:
    from sessionfs.server.config import ServerConfig

    await _setup(client, auth_headers, gitlab_host=SELF_HOSTED)
    app = client._transport.app  # type: ignore[attr-defined]
    app.state.trusted_publishing = github_oidc.resolve_trusted_publishing(
        ServerConfig(database_url="sqlite+aiosqlite://", gitlab_oidc_issuers=GITLAB)
    )
    response = await _exchange(client, _sign(_claims(iss=SELF_HOSTED)))
    assert response.status_code == 401
    assert _error(response)["details"]["reason"] == "issuer_not_allowed"
    # Even with verified claims, the service refuses a row whose issuer is
    # no longer allowlisted.
    with pytest.raises(Exception) as excinfo:
        await svc.exchange_gitlab_claims(
            None,  # type: ignore[arg-type]
            _claims(iss=SELF_HOSTED),
            issuers=(GITLAB,),
            requested_package=None,
        )
    assert getattr(excinfo.value, "status_code", None) == 403


async def test_gitlab_disabled(client: AsyncClient, auth_headers: dict) -> None:
    from sessionfs.server.config import ServerConfig

    publisher = await _claim(client, auth_headers, "acme")
    app = client._transport.app  # type: ignore[attr-defined]
    app.state.trusted_publishing = github_oidc.resolve_trusted_publishing(
        ServerConfig(database_url="sqlite+aiosqlite://", gitlab_oidc_issuers="none")
    )
    response = await _exchange(client, _sign(_claims()))
    assert response.status_code == 503
    assert _error(response)["code"] == "trusted_publishing_not_configured"
    created = await client.post(
        f"/api/v1/publishers/{publisher['id']}/trusted-publishers",
        headers=auth_headers,
        json={"provider": "gitlab", "gitlab_project_id": 1},
    )
    assert created.status_code == 422
    assert _error(created)["code"] == "gitlab_host_not_allowed"
    config = (await client.get("/api/v1/marketplace/trusted-publishing/config")).json()
    assert config["gitlab"]["enabled"] is False and config["gitlab"]["issuers"] == []


async def test_config_lists_gitlab_issuers(client: AsyncClient) -> None:
    config = (await client.get("/api/v1/marketplace/trusted-publishing/config")).json()
    assert config["audience"] == AUDIENCE
    assert config["gitlab"] == {
        "enabled": True,
        "issuers": [GITLAB, SELF_HOSTED],
        "allowed_pipeline_sources": ["api", "push", "web"],
        "requires_protected_ref": True,
    }


# ---------------------------------------------------------------------------
# GitHub trusts are unaffected
# ---------------------------------------------------------------------------


async def test_github_and_gitlab_trusts_side_by_side(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    publisher = await _claim(client, auth_headers, "acme")
    github = await client.post(
        f"/api/v1/publishers/{publisher['id']}/trusted-publishers",
        headers=auth_headers,
        json={
            "repository": "acme/skills", "workflow": "release.yml",
            # The GitHub repository id equals the GitLab project id on purpose.
            "repository_id": int(PROJECT_ID), "repository_owner_id": 654321,
            "environment": "release", "ref_pattern": "refs/tags/v*",
        },
    )
    assert github.status_code == 201, github.text
    assert github.json()["provider"] == "github" and github.json()["gitlab_project_id"] is None
    gitlab = await _trust(client, auth_headers, publisher["id"])

    from_github = await client.post(
        "/api/v1/marketplace/trusted-publishing/token",
        json={"token": _github_sign(_github_claims(repository_id=PROJECT_ID))},
    )
    assert from_github.status_code == 200, from_github.text
    assert from_github.json()["trusted_publisher_id"] == github.json()["id"]
    from_gitlab = await _exchange(client, _sign(_claims()))
    assert from_gitlab.status_code == 200, from_gitlab.text
    assert from_gitlab.json()["trusted_publisher_id"] == gitlab["id"]
    rows = (await db_session.execute(select(SkillTrustedPublisher))).scalars().all()
    assert {row.provider for row in rows} == {"github", "gitlab"}


# ---------------------------------------------------------------------------
# Trust configuration
# ---------------------------------------------------------------------------


async def test_trust_configuration_validation(client: AsyncClient, auth_headers: dict) -> None:
    publisher = await _claim(client, auth_headers, "configco")
    url = f"/api/v1/publishers/{publisher['id']}/trusted-publishers"
    for bad, code in (
        ({"gitlab_host": "https://gitlab.evil.example"}, "gitlab_host_not_allowed"),
        ({"gitlab_host": "http://gitlab.com"}, "gitlab_host_not_allowed"),
        ({"gitlab_host": "https://user:pw@gitlab.com"}, "gitlab_host_not_allowed"),
        ({"gitlab_host": "https://gitlab.com?x=1"}, "gitlab_host_not_allowed"),
        ({"gitlab_host": "https://10.0.0.5"}, "gitlab_host_not_allowed"),
        ({"gitlab_project_id": None}, "gitlab_project_required"),
        ({"workflow": "release.yml"}, "invalid_trusted_publisher_fields"),
        ({"repository_id": 1}, "invalid_trusted_publisher_fields"),
        ({"allow_workflow_run": True}, "invalid_trusted_publisher_fields"),
        ({"reusable_workflow": "o/r/.github/workflows/x.yml"}, "invalid_trusted_publisher_fields"),
        ({"ref_pattern": "v*"}, "invalid_ref_pattern"),
        ({"ref_pattern": "refs/tags/**"}, "invalid_ref_pattern"),
        ({"environment": "prod${x}"}, "invalid_environment"),
        ({"repository": "../etc"}, "invalid_project_path"),
        ({"package_name": "Bad_Name"}, "invalid_package_name"),
        ({"require_protected_environment": True}, "environment_required"),
    ):
        body = {"provider": "gitlab", "gitlab_project_id": 5, **bad}
        body = {key: value for key, value in body.items() if value is not None}
        response = await client.post(url, headers=auth_headers, json=body)
        assert response.status_code == 422, (bad, response.text)
        assert _error(response)["code"] == code, (bad, response.text)
    # GitLab fields on a GitHub trust are refused too.
    github = await client.post(url, headers=auth_headers, json={
        "repository": "o/r", "workflow": "release.yml", "repository_id": 1,
        "repository_owner_id": 2, "gitlab_project_id": 5,
    })
    assert github.status_code == 422
    assert _error(github)["code"] == "invalid_trusted_publisher_fields"
    github_protected = await client.post(url, headers=auth_headers, json={
        "repository": "o/r", "workflow": "release.yml", "repository_id": 1,
        "repository_owner_id": 2, "environment": "release", "require_protected_environment": True,
    })
    assert github_protected.status_code == 422
    assert _error(github_protected)["code"] == "invalid_trusted_publisher_fields"
    # A pydantic-level refusal for a non-positive project id.
    zero = await client.post(url, headers=auth_headers, json={"provider": "gitlab", "gitlab_project_id": 0})
    assert zero.status_code == 422
    # Host normalization: case and a trailing slash are canonicalized.
    created = await client.post(url, headers=auth_headers, json={
        "provider": "gitlab", "gitlab_project_id": 5, "gitlab_host": "https://GitLab.com/",
    })
    assert created.status_code == 201, created.text
    assert created.json()["gitlab_host"] == GITLAB
    assert created.json()["repository"] == "project 5"


# ---------------------------------------------------------------------------
# Allowlist parsing, startup, provenance helpers
# ---------------------------------------------------------------------------


def test_parse_issuers() -> None:
    assert gitlab_oidc.parse_issuers("") == ("https://gitlab.com",)
    assert gitlab_oidc.parse_issuers(None) == ("https://gitlab.com",)
    assert gitlab_oidc.parse_issuers("none") == ()
    assert gitlab_oidc.parse_issuers(
        " https://Git.Acme.example/ , https://gitlab.com,https://git.acme.example"
    ) == ("https://git.acme.example", "https://gitlab.com")
    assert gitlab_oidc.parse_issuers("https://acme.example:8443/gitlab/") == ("https://acme.example:8443/gitlab",)
    assert gitlab_oidc.parse_issuers("https://gitlab.com:443") == ("https://gitlab.com",)
    for bad in (
        "http://gitlab.acme.example",
        "gitlab.acme.example",
        "https://u:p@gitlab.com",
        "https://gitlab.com/?a=1",
        "https://gitlab.com/#x",
        "https://gitlab.com/a/../b",
        GITHUB_ACTIONS_ISSUER,
        "https://",
    ):
        with pytest.raises(ValueError):
            gitlab_oidc.parse_issuers(bad)


def test_invalid_issuer_list_refuses_to_start() -> None:
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig

    with pytest.raises(RuntimeError):
        create_app(ServerConfig(database_url="sqlite+aiosqlite://", gitlab_oidc_issuers="http://gitlab.internal"))
    app = create_app(ServerConfig(database_url="sqlite+aiosqlite://", gitlab_oidc_issuers="https://git.acme.example"))
    assert app.state.trusted_publishing.gitlab_issuers == ("https://git.acme.example",)


def test_provider_change_is_a_widening_and_both_kinds_are_trusted() -> None:
    github = {"kind": "github_oidc"}
    gitlab = {"kind": "gitlab_oidc"}
    assert svc.widening_reasons({}, gitlab, {}, github) == ["trust_change:provider_changed"]
    assert svc.widening_reasons({}, {}, {}, gitlab) == ["trust_downgrade:unattested_after_trusted_publish"]
    assert svc.widening_reasons({}, gitlab, {}, gitlab) == []
    assert svc.widening_reasons({}, github, {}, github) == []

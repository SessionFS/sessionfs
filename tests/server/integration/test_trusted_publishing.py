"""Trusted publishing over GitHub Actions OIDC (§4.4): token validation,
binding to numeric repository / owner ids, workflow, environment and ref,
replay protection, single-use publish tokens, and recorded provenance."""

from __future__ import annotations

import hashlib
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

from sessionfs.server import github_app
from sessionfs.server.db.models import SkillPackageVersion, SkillPublisher, SkillPublishToken, User
from sessionfs.server.services import github_oidc
from sessionfs.server.services import publishers as svc
from sessionfs.server.services.oidc_token import GITHUB_ACTIONS_ISSUER
from tests.server.integration.test_marketplace_moderation import _org, _user
from tests.server.integration.test_marketplace_publishers import (  # noqa: F401
    _fresh_publisher_limiters,
    _claim,
    _enable_public,
    _error,
    _pkg,
    _verify_dns,
)

pytestmark = pytest.mark.asyncio

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
REPO_ID, OWNER_ID = "123456", "654321"
SHA = "0123456789abcdef0123456789abcdef01234567"
AUDIENCE = "https://api.test.sessionfs.example"
CALLER = "acme/skills/.github/workflows/release.yml@refs/tags/v1.0.0"


def _jwks() -> dict:
    jwk = json.loads(RSAAlgorithm.to_jwk(_KEY.public_key()))
    jwk.update({"kid": "k1", "alg": "RS256", "use": "sig"})
    return {"keys": [jwk]}


@pytest.fixture(autouse=True)
def _github_jwks(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SFS_TRUSTED_PUBLISHING_AUDIENCE", AUDIENCE)
    github_oidc.set_jwks_for_tests(_jwks())
    yield
    github_oidc.set_jwks_for_tests(None)


class _Rel:
    """A timestamp relative to signing time (parametrize runs at collection,
    which can be many minutes before the test in a full run)."""

    def __init__(self, seconds: int) -> None:
        self.seconds = seconds


def _claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": GITHUB_ACTIONS_ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "jti": str(uuid.uuid4()),
        "sub": "repo:acme/skills:environment:release",
        "repository": "acme/skills",
        "repository_id": REPO_ID,
        "repository_owner": "acme",
        "repository_owner_id": OWNER_ID,
        # A plain (non-reusable) workflow: caller and job workflow are the same file.
        "workflow_ref": CALLER,
        "job_workflow_ref": CALLER,
        "ref": "refs/tags/v1.0.0",
        "ref_type": "tag",
        "sha": SHA,
        "environment": "release",
        "run_id": "4242",
        "run_attempt": "1",
        "event_name": "push",
        "runner_environment": "github-hosted",
    }
    for key, value in overrides.items():
        if isinstance(value, _Rel):
            claims[key] = now + value.seconds
        elif value is None:
            claims.pop(key, None)
        else:
            claims[key] = value
    return claims


def _sign(claims: dict, *, key=None, kid: str = "k1", alg: str = "RS256") -> str:
    return jwt.encode(claims, key or _KEY, algorithm=alg, headers={"kid": kid})


async def _trust(client: AsyncClient, headers: dict, publisher_id: str, **extra) -> dict:
    body = {
        "repository": "acme/skills",
        "workflow": "release.yml",
        "repository_id": int(REPO_ID),
        "repository_owner_id": int(OWNER_ID),
        "environment": "release",
        "ref_pattern": "refs/tags/v*",
        **extra,
    }
    response = await client.post(f"/api/v1/publishers/{publisher_id}/trusted-publishers", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _exchange(client: AsyncClient, token: str, **extra):
    return await client.post("/api/v1/marketplace/trusted-publishing/token", json={"token": token, **extra})


async def _publish_with(client: AsyncClient, publish_token: str, *, package: bytes | None = None, **data):
    return await client.post(
        "/api/v1/marketplace/trusted-publishing/publish",
        headers={"Authorization": f"Bearer {publish_token}"},
        files={"package": ("p.tar.gz", package or _pkg(), "application/gzip")},
        data=data,
    )


async def _setup(client: AsyncClient, headers: dict, namespace: str = "acme", **trust) -> tuple[dict, dict]:
    publisher = await _claim(client, headers, namespace)
    trusted = await _trust(client, headers, publisher["id"], **trust)
    return publisher, trusted


# ---------------------------------------------------------------------------
# Happy path, provenance, single use
# ---------------------------------------------------------------------------


async def test_exchange_and_publish_records_provenance(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    publisher, trusted = await _setup(client, auth_headers)
    exchanged = await _exchange(client, _sign(_claims()), namespace="acme")
    assert exchanged.status_code == 200, exchanged.text
    body = exchanged.json()
    assert body["token"].startswith("sfs_tp_")
    assert body["namespace"] == "acme" and body["trusted_publisher_id"] == trusted["id"]
    # Only the hash is stored.
    rows = (await db_session.execute(select(SkillPublishToken))).scalars().all()
    assert [row.token_hash for row in rows] == [hashlib.sha256(body["token"].encode()).hexdigest()]
    assert body["token"] not in json.dumps([row.claims for row in rows])

    published = await _publish_with(client, body["token"])
    assert published.status_code == 201, published.text
    result = published.json()
    assert result["namespace"] == "acme" and result["visibility"] == "user"
    assert result["publisher_id"] == publisher["id"]
    # Publication provenance never rewrites the declared source commit.
    assert result["source"]["commit"] is None
    assert result["source"]["locator"] == "https://github.com/acme/skills"
    signature = result["signature"]
    assert signature["kind"] == "github_oidc" and signature["claim"] == "publication"
    assert signature["build_provenance"] is None and signature["attestation"] is None
    assert "commit" not in signature and "repository" not in signature
    assert signature["published_from_repo"] == "github.com/acme/skills"
    assert signature["published_from_repo_id"] == int(REPO_ID)
    assert signature["published_from_owner_id"] == int(OWNER_ID)
    assert signature["publishing_workflow"] == ".github/workflows/release.yml"
    assert signature["publishing_workflow_ref"] == CALLER
    assert signature["publishing_ref"] == "refs/tags/v1.0.0"
    assert signature["publishing_environment"] == "release"
    assert signature["publishing_event"] == "push"
    assert signature["publishing_run_url"] == "https://github.com/acme/skills/actions/runs/4242"
    version = await db_session.get(SkillPackageVersion, result["version_id"])
    assert json.loads(version.signature_metadata)["publishing_run_sha"] == SHA
    assert version.source_commit is None
    token_row = (await db_session.execute(select(SkillPublishToken))).scalar_one()
    await db_session.refresh(token_row)
    assert token_row.used_at is not None and token_row.used_for_version_id == result["version_id"]

    again = await _publish_with(client, body["token"], package=_pkg(body="second"))
    assert again.status_code == 401
    assert _error(again)["code"] == "invalid_publish_token"


async def test_oidc_token_replay_is_refused(client: AsyncClient, auth_headers: dict) -> None:
    await _setup(client, auth_headers)
    token = _sign(_claims())
    assert (await _exchange(client, token)).status_code == 200
    replay = await _exchange(client, token)
    assert replay.status_code == 401
    assert _error(replay)["code"] == "oidc_token_replayed"


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claims,reason",
    [
        ({"aud": "pypi"}, "invalid_aud"),
        ({"aud": "sessionfs"}, "invalid_aud"),
        ({"aud": [AUDIENCE, "other"]}, "invalid_aud"),
        ({"iss": "https://token.actions.githubusercontent.com.evil.com"}, "issuer_mismatch"),
        ({"exp": _Rel(-120), "iat": _Rel(-400)}, "token_expired"),
        ({"iat": _Rel(-1200), "exp": _Rel(600)}, "token_too_old"),
        ({"exp": _Rel(7200)}, "token_lifetime"),
        ({"jti": None}, "claim_missing"),
        ({"repository_id": None}, "claim_missing"),
        ({"repository_id": "12a"}, "claim_invalid"),
        ({"sha": "abc"}, "claim_invalid"),
        ({"workflow_ref": None}, "claim_missing"),
        ({"event_name": None}, "claim_missing"),
        ({"job_workflow_ref": 42}, "claim_invalid"),
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
    unsigned = jwt.encode(_claims(), key=None, algorithm="none", headers={"kid": "k1"})
    hmac_forged = jwt.encode(_claims(), "s" * 48, algorithm="HS256", headers={"kid": "k1"})
    wrong_key = _sign(_claims(), key=_OTHER_KEY)
    unknown_kid = _sign(_claims(), kid="nope")
    for token, reason in (
        (unsigned, "alg_not_allowed"),
        (hmac_forged, "alg_not_allowed"),
        (wrong_key, "invalid_signature"),
        (unknown_kid, "kid_not_found"),
        ("not-a-jwt", "token_parse_error"),
    ):
        response = await _exchange(client, token)
        assert response.status_code == 401, (reason, response.text)
        assert _error(response)["details"]["reason"] == reason


def _use_settings(client: AsyncClient, **config) -> github_oidc.TrustedPublishingSettings:
    """Re-resolve the app's trusted-publishing settings from a ServerConfig,
    the way create_app does at startup (env is read once, there)."""
    from sessionfs.server.config import ServerConfig

    settings = github_oidc.resolve_trusted_publishing(ServerConfig(**config))
    client._transport.app.state.trusted_publishing = settings  # type: ignore[attr-defined]
    return settings


async def test_custom_audience_for_self_hosted(client: AsyncClient, auth_headers: dict) -> None:
    await _setup(client, auth_headers)
    _use_settings(
        client,
        database_url="postgresql+asyncpg://u@h/db",
        environment="production",
        trusted_publishing_audience="https://sessionfs.acme.internal",
    )
    assert (await _exchange(client, _sign(_claims()))).status_code == 401
    assert (await _exchange(client, _sign(_claims(aud="https://sessionfs.acme.internal")))).status_code == 200
    config = (await client.get("/api/v1/marketplace/trusted-publishing/config")).json()
    assert config["enabled"] is True and config["audience"] == "https://sessionfs.acme.internal"


async def test_audience_defaults_to_public_origin_and_is_disabled_without_one(
    client: AsyncClient, auth_headers: dict
) -> None:
    await _setup(client, auth_headers)
    settings = _use_settings(
        client,
        database_url="postgresql+asyncpg://u@h/db",
        environment="production",
        trusted_publishing_audience="",
        public_api_url="https://api.acme.example/",
    )
    assert settings.audience == "https://api.acme.example" and settings.source == "public_api_url"
    assert (await _exchange(client, _sign(_claims(aud="https://api.acme.example")))).status_code == 200
    _use_settings(
        client, database_url="postgresql+asyncpg://u@h/db", environment="production",
        trusted_publishing_audience="", public_api_url="",
    )
    config = (await client.get("/api/v1/marketplace/trusted-publishing/config")).json()
    assert config == {
        "enabled": False, "audience": None, "issuer": GITHUB_ACTIONS_ISSUER,
        "allowed_events": ["push", "release", "workflow_dispatch"],
    }
    response = await _exchange(client, _sign(_claims()))
    assert response.status_code == 503
    assert _error(response)["code"] == "trusted_publishing_not_configured"


def test_generic_audience_refuses_to_start_whatever_its_source(monkeypatch: pytest.MonkeyPatch) -> None:
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig

    pg = "postgresql+asyncpg://u@h/db"
    for override, origin in (
        ("sessionfs", ""),
        ("SessionFS", ""),
        ("sessionfs-development", ""),
        ("", "sessionfs"),  # generic value through SFS_PUBLIC_API_URL
        ("", "http://api.acme.example"),  # origin must be https
    ):
        with pytest.raises(RuntimeError):
            create_app(ServerConfig(
                database_url=pg, environment="production",
                trusted_publishing_audience=override, public_api_url=origin,
            ))
    app = create_app(ServerConfig(
        database_url=pg, environment="production",
        trusted_publishing_audience="", public_api_url="https://api.acme.example",
    ))
    assert app.state.trusted_publishing.audience == "https://api.acme.example"
    # Development keeps the development audience...
    dev = create_app(ServerConfig(database_url="sqlite+aiosqlite://", trusted_publishing_audience=""))
    assert dev.state.trusted_publishing.audience == "sessionfs-development"


def test_environment_and_audience_resolved_once_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PostgreSQL ServerConfig is production even when the process env says
    nothing (or points at SQLite): the startup decision and the audience the
    routes use come from the same resolution, never a second env read."""
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig

    monkeypatch.delenv("SFS_TRUSTED_PUBLISHING_AUDIENCE", raising=False)
    monkeypatch.delenv("SFS_ENVIRONMENT", raising=False)
    monkeypatch.setenv("SFS_DATABASE_URL", "sqlite+aiosqlite://")
    app = create_app(ServerConfig(database_url="postgresql+asyncpg://u@h/db"))
    settings = app.state.trusted_publishing
    assert settings.development is False
    assert settings.audience is None  # disabled, NOT the development audience


# ---------------------------------------------------------------------------
# Binding: repository ids, workflow, environment, ref
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claims",
    [
        # Same NAME, different numeric id: a deleted-and-recreated repo.
        {"repository_id": "999999"},
        # Same repo name under a re-registered owner account.
        {"repository_owner_id": "111"},
        # Another CALLER workflow in the same repository (plain workflow).
        {"workflow_ref": "acme/skills/.github/workflows/ci.yml@refs/tags/v1.0.0",
         "job_workflow_ref": "acme/skills/.github/workflows/ci.yml@refs/tags/v1.0.0"},
        # ci.yml CALLING release.yml as a reusable workflow: job_workflow_ref
        # names the configured file, but the caller is ci.yml. Refused (the
        # old job_workflow_ref-only binding accepted this).
        {"workflow_ref": "acme/skills/.github/workflows/ci.yml@refs/heads/feature",
         "job_workflow_ref": CALLER},
        # The configured file name in ANOTHER repository.
        {"workflow_ref": "evil/tools/.github/workflows/release.yml@refs/tags/v1.0.0"},
        # Same file name in a different CASE is a different workflow file.
        {"workflow_ref": "acme/skills/.github/workflows/Release.yml@refs/tags/v1.0.0",
         "job_workflow_ref": "acme/skills/.github/workflows/Release.yml@refs/tags/v1.0.0"},
        {"workflow_ref": "acme/skills/.GitHub/workflows/release.yml@refs/tags/v1.0.0"},
        # Prefix trick on the workflow path.
        {"workflow_ref": "acme/skills/.github/workflows/release.yml.bak@refs/tags/v1.0.0"},
        # Pull-request-derived and other non-publishing events, even on a
        # branch / tag ref (pull_request_target runs with the BASE ref).
        {"event_name": "pull_request_target"},
        {"event_name": "pull_request"},
        {"event_name": "pull_request_review"},
        {"event_name": "issue_comment"},
        {"event_name": "schedule"},
        {"event_name": "workflow_run"},
        # Environment missing or different.
        {"environment": None},
        {"environment": "staging"},
        # Ref outside the pattern; pull-request refs never match.
        {"ref": "refs/heads/main"},
        {"ref": "refs/pull/7/merge"},
    ],
)
async def test_binding_mismatches_are_refused(client: AsyncClient, auth_headers: dict, claims: dict) -> None:
    await _setup(client, auth_headers)
    response = await _exchange(client, _sign(_claims(**claims)))
    assert response.status_code == 403, response.text
    assert _error(response)["code"] == "trusted_publisher_mismatch"


async def test_publishing_events_and_workflow_run_opt_in(client: AsyncClient, auth_headers: dict) -> None:
    await _setup(client, auth_headers, ref_pattern=None)
    for event in ("push", "release", "workflow_dispatch"):
        response = await _exchange(client, _sign(_claims(event_name=event)), namespace="acme")
        assert response.status_code == 200, (event, response.text)
    await _setup(client, auth_headers, namespace="acme-run", ref_pattern=None, allow_workflow_run=True)
    allowed = await _exchange(client, _sign(_claims(event_name="workflow_run")), namespace="acme-run")
    assert allowed.status_code == 200, allowed.text
    still = await _exchange(client, _sign(_claims(event_name="pull_request_target")), namespace="acme-run")
    assert still.status_code == 403


async def test_reusable_workflow_binds_caller_and_callee_independently(
    client: AsyncClient, auth_headers: dict
) -> None:
    callee = "acme/shared-ci/.github/workflows/publish.yml"
    await _setup(client, auth_headers, reusable_workflow=callee)
    reusable_run = {"workflow_ref": CALLER, "job_workflow_ref": f"{callee}@refs/heads/main"}
    ok = await _exchange(client, _sign(_claims(**reusable_run)))
    assert ok.status_code == 200, ok.text
    for bad in (
        {"workflow_ref": CALLER, "job_workflow_ref": "acme/shared-ci/.github/workflows/other.yml@refs/heads/main"},
        {"workflow_ref": CALLER, "job_workflow_ref": "evil/shared-ci/.github/workflows/publish.yml@refs/heads/main"},
        {"workflow_ref": CALLER, "job_workflow_ref": None},
        {"workflow_ref": "acme/skills/.github/workflows/ci.yml@refs/tags/v1.0.0",
         "job_workflow_ref": f"{callee}@refs/heads/main"},
    ):
        response = await _exchange(client, _sign(_claims(**bad)))
        assert response.status_code == 403, (bad, response.text)
    # Without a configured callee, the caller binding alone decides: a release
    # workflow that delegates to any reusable workflow still matches.
    await _setup(client, auth_headers, namespace="acme-plain")
    plain = await _exchange(client, _sign(_claims(**reusable_run)), namespace="acme-plain")
    assert plain.status_code == 200, plain.text
    token = (await _exchange(client, _sign(_claims(**reusable_run)), namespace="acme")).json()["token"]
    published = (await _publish_with(client, token)).json()
    assert published["signature"]["publishing_workflow_ref"] == CALLER
    assert published["signature"]["publishing_job_workflow_ref"] == f"{callee}@refs/heads/main"


async def test_environment_is_case_insensitive_and_optional_constraints(
    client: AsyncClient, auth_headers: dict
) -> None:
    await _setup(client, auth_headers, environment=None, ref_pattern=None)
    assert (await _exchange(client, _sign(_claims(environment=None, ref="refs/heads/main")))).status_code == 200
    await _setup(client, auth_headers, namespace="acme-two", environment="Release")
    response = await _exchange(client, _sign(_claims(environment="release")), namespace="acme-two")
    assert response.status_code == 200, response.text


async def test_revoked_trust_and_suspended_publisher(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    publisher, trusted = await _setup(client, auth_headers)
    minted = (await _exchange(client, _sign(_claims()))).json()["token"]
    removed = await client.delete(
        f"/api/v1/publishers/{publisher['id']}/trusted-publishers/{trusted['id']}", headers=auth_headers
    )
    assert removed.status_code == 200
    # Revoking the trust kills tokens already minted under it, and new ones.
    assert (await _publish_with(client, minted)).status_code == 401
    assert (await _exchange(client, _sign(_claims()))).status_code == 403
    trusted2 = await _trust(client, auth_headers, publisher["id"])
    minted = (await _exchange(client, _sign(_claims()))).json()["token"]
    _, admin_headers = await _user(db_session, tier="admin")
    suspended = await client.post(
        f"/api/v1/admin/marketplace/publishers/{publisher['id']}/suspend", headers=admin_headers,
        json={"reason": "investigating"},
    )
    assert suspended.status_code == 200
    assert (await _publish_with(client, minted)).status_code == 401
    assert (await _exchange(client, _sign(_claims()))).status_code == 403
    assert trusted2["id"] != trusted["id"]


async def test_package_scoped_token(client: AsyncClient, auth_headers: dict) -> None:
    await _setup(client, auth_headers, package_name="review-helper")
    other = await _exchange(client, _sign(_claims()), package="other-skill")
    assert other.status_code == 403
    token = (await _exchange(client, _sign(_claims()))).json()
    assert token["package"] == "review-helper"
    wrong = await _publish_with(client, token["token"], package=_pkg(name="other-skill"))
    assert wrong.status_code == 403
    assert _error(wrong)["code"] == "publish_token_package_mismatch"


async def test_ambiguity_and_namespace_filter(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    await _setup(client, auth_headers, namespace="realacme")
    # Someone else registers the SAME repository and workflow for their own
    # namespace: naming the namespace keeps the real publisher working.
    _, other = await _user(db_session)
    await _setup(client, other, namespace="copycat")
    ambiguous = await _exchange(client, _sign(_claims()))
    assert ambiguous.status_code == 409
    assert _error(ambiguous)["code"] == "trusted_publisher_ambiguous"
    ok = await _exchange(client, _sign(_claims()), namespace="realacme")
    assert ok.status_code == 200 and ok.json()["namespace"] == "realacme"


async def test_publish_token_is_not_an_api_key_and_api_key_is_not_a_publish_token(
    client: AsyncClient, auth_headers: dict
) -> None:
    await _setup(client, auth_headers)
    token = (await _exchange(client, _sign(_claims()))).json()["token"]
    as_api_key = await client.get("/api/v1/marketplace/packages", headers={"Authorization": f"Bearer {token}"})
    assert as_api_key.status_code == 401
    personal = await client.post(
        "/api/v1/users/me/skills/packages",
        headers={"Authorization": f"Bearer {token}"},
        files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
        data={"namespace": "acme"},
    )
    assert personal.status_code == 401
    api_key = auth_headers["Authorization"].split(" ", 1)[1]
    assert (await _publish_with(client, api_key)).status_code == 401
    assert (await _publish_with(client, "sfs_tp_" + "x" * 43)).status_code == 401
    # The genuine token still works afterwards (nothing above consumed it).
    assert (await _publish_with(client, token)).status_code == 201


async def test_expired_publish_token(client: AsyncClient, auth_headers: dict, db_session: AsyncSession) -> None:
    await _setup(client, auth_headers)
    token = (await _exchange(client, _sign(_claims()))).json()["token"]
    row = (await db_session.execute(select(SkillPublishToken))).scalar_one()
    row.expires_at = svc.now_utc().replace(year=2020)
    await db_session.commit()
    assert (await _publish_with(client, token)).status_code == 401


async def test_org_publisher_trusted_publish_and_trust_downgrade(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, _ = await _org(db_session, test_user)
    publisher = await _claim(client, auth_headers, "orgpub", org_id=org.id)
    await _trust(client, auth_headers, publisher["id"])
    await _verify_dns(client, auth_headers, publisher["id"], "orgpub.example", monkeypatch)
    _, admin_headers = await _user(db_session, tier="admin")
    await _enable_public(client, admin_headers, publisher["id"])

    token = (await _exchange(client, _sign(_claims()))).json()["token"]
    published = await _publish_with(client, token, visibility="public")
    assert published.status_code == 201, published.text
    first = published.json()
    assert first["visibility"] == "public" and first["moderation_state"] == "pending_review"
    assert first["signature"]["kind"] == "github_oidc"
    await client.post(f"/api/v1/admin/marketplace/listings/{first['listing_id']}/approve", headers=admin_headers)

    # A text-only, trusted-published update auto-approves...
    token = (await _exchange(client, _sign(_claims()))).json()["token"]
    second = (await _publish_with(client, token, visibility="public", package=_pkg(body="v2"))).json()
    assert second["moderation_state"] == "approved"
    # ...but the same kind of update uploaded with an API key is a trust
    # downgrade and goes to review.
    downgrade = await client.post(
        f"/api/v1/orgs/{org.id}/skills/packages", headers=auth_headers,
        files={"package": ("p.tar.gz", _pkg(body="v3"), "application/gzip")},
        data={"namespace": "orgpub", "collection": "orgpub", "visibility": "public",
              "source_locator": "https://github.com/acme/skills"},
    )
    assert downgrade.status_code == 201, downgrade.text
    assert downgrade.json()["moderation_state"] == "pending_review"
    queue = (await client.get("/api/v1/admin/marketplace/review-queue", headers=admin_headers)).json()
    assert any("trust_downgrade" in (item["moderation_reason"] or "") for item in queue["items"])

    # The org publisher cannot target a personal listing.
    token = (await _exchange(client, _sign(_claims()))).json()["token"]
    wrong = await _publish_with(client, token, visibility="user")
    assert wrong.status_code == 422


async def test_declared_source_commit_is_kept_separate_from_publishing_sha(
    client: AsyncClient, auth_headers: dict
) -> None:
    await _setup(client, auth_headers)
    token = (await _exchange(client, _sign(_claims()))).json()["token"]
    response = await _publish_with(client, token, source_commit="f" * 40)
    assert response.status_code == 201, response.text
    assert response.json()["source"]["commit"] == "f" * 40
    assert response.json()["signature"]["publishing_run_sha"] == SHA


async def test_trusted_publisher_configuration_validation(
    client: AsyncClient, auth_headers: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = await _claim(client, auth_headers, "configco")
    url = f"/api/v1/publishers/{publisher['id']}/trusted-publishers"
    for bad in (
        {"repository": "not-a-repo", "workflow": "release.yml"},
        {"repository": "o/r", "workflow": "../../etc/passwd"},
        {"repository": "o/r", "workflow": ".github/workflows/release.sh"},
        {"repository": "o/r", "workflow": "release.yml", "ref_pattern": "main"},
        {"repository": "o/r", "workflow": "release.yml", "ref_pattern": "refs/tags/**"},
        {"repository": "o/r", "workflow": "release.yml", "package_name": "Bad_Name"},
    ):
        response = await client.post(url, headers=auth_headers, json={**bad, "repository_id": 1, "repository_owner_id": 2})
        assert response.status_code == 422, (bad, response.text)

    calls: list[tuple[str, str]] = []

    async def resolve(_db, _user_id, owner, repo):
        calls.append((owner, repo))
        return {"id": 77, "owner_id": 88, "full_name": "Owner/Repo"} if repo == "repo" else None

    monkeypatch.setattr(github_app, "resolve_repository", resolve)
    created = await client.post(url, headers=auth_headers, json={"repository": "owner/repo", "workflow": "publish.yaml"})
    assert created.status_code == 201, created.text
    assert created.json()["repository_id"] == 77 and created.json()["repository_owner_id"] == 88
    assert created.json()["workflow"] == ".github/workflows/publish.yaml"
    missing = await client.post(url, headers=auth_headers, json={"repository": "owner/missing", "workflow": "x.yml"})
    assert missing.status_code == 422
    assert _error(missing)["code"] == "repository_not_found"


async def test_ref_matching_is_anchored() -> None:
    assert svc.ref_matches("refs/tags/v*", "refs/tags/v1.2.3")
    assert not svc.ref_matches("refs/tags/v*", "refs/heads/refs/tags/v1")
    assert not svc.ref_matches("refs/heads/main", "refs/heads/main-evil")
    assert svc.ref_matches(None, "anything")
    assert not svc.ref_matches("refs/tags/v1.?", "refs/tags/v1.x")  # ? is literal


async def test_owner_repo_case_insensitive_but_paths_exact(client: AsyncClient, auth_headers: dict) -> None:
    callee = "acme/shared-ci/.github/workflows/publish.yml"
    await _setup(client, auth_headers, reusable_workflow=callee)
    # owner/repo casing follows GitHub (case-insensitive; ids already bound).
    renamed_case = {
        "repository": "Acme/Skills",
        "workflow_ref": "Acme/Skills/.github/workflows/release.yml@refs/tags/v1.0.0",
        "job_workflow_ref": "ACME/Shared-CI/.github/workflows/publish.yml@refs/heads/main",
    }
    ok = await _exchange(client, _sign(_claims(**renamed_case)))
    assert ok.status_code == 200, ok.text
    # The callee FILE path is exact.
    bad = await _exchange(client, _sign(_claims(
        workflow_ref=CALLER,
        job_workflow_ref="acme/shared-ci/.github/workflows/Publish.yml@refs/heads/main",
    )))
    assert bad.status_code == 403, bad.text


def test_split_workflow_ref() -> None:
    assert svc.split_workflow_ref("o/r/.github/workflows/a.yml@refs/tags/v@1") == ("o/r", ".github/workflows/a.yml")
    assert svc.split_workflow_ref("o/r@x") is None
    assert svc.split_workflow_ref("no-at-sign") is None
    assert svc.split_workflow_ref(None) is None


async def test_removing_any_trust_invalidates_tokens_minted_before(
    client: AsyncClient, auth_headers: dict
) -> None:
    publisher, first = await _setup(client, auth_headers)
    second = await _trust(client, auth_headers, publisher["id"], workflow="other.yml")
    token = (await _exchange(client, _sign(_claims()))).json()["token"]  # minted under `first`
    removed = await client.delete(
        f"/api/v1/publishers/{publisher['id']}/trusted-publishers/{second['id']}", headers=auth_headers
    )
    assert removed.status_code == 200
    # Generation moved on: the earlier token no longer publishes...
    assert (await _publish_with(client, token)).status_code == 401
    # ...a fresh one does.
    fresh = (await _exchange(client, _sign(_claims()))).json()["token"]
    assert (await _publish_with(client, fresh)).status_code == 201
    assert first["id"] != second["id"]


async def test_suspension_after_token_check_wins_over_the_publish(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A suspension that commits between the token check and the burn is
    seen when the route re-locks and reloads the publisher."""
    from sessionfs.server.routes import marketplace_publishers as routes

    publisher, _ = await _setup(client, auth_headers)
    token = (await _exchange(client, _sign(_claims()))).json()["token"]
    original_burn = svc.burn_publish_token

    async def burn_then_suspend(db, token_id):
        await original_burn(db, token_id)
        await svc.suspend_publisher(db, publisher["id"], "suspended mid-publish")
        await db.commit()

    monkeypatch.setattr(routes.svc, "burn_publish_token", burn_then_suspend)
    response = await _publish_with(client, token)
    assert response.status_code == 401, response.text
    monkeypatch.setattr(routes.svc, "burn_publish_token", original_burn)
    rows = (await db_session.execute(select(SkillPackageVersion))).scalars().all()
    assert rows == []


async def test_concurrent_first_publish_is_adopted_under_the_publisher_lock(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another publish creates the same package identity between our
    unlocked lookup and the publisher lock: it is adopted (re-queried under
    the lock) instead of failing on the unique key after the token burned."""
    from sessionfs.server.db.models import SkillPackage
    from sessionfs.server.routes import marketplace as mp

    publisher, _ = await _setup(client, auth_headers)
    token = (await _exchange(client, _sign(_claims()))).json()["token"]
    original = mp.publishers_service.resolve_publish_publisher
    inserted: list[str] = []

    async def resolve_and_race(db, **kwargs):
        result = await original(db, **kwargs)
        if not inserted:
            pub = await db_session.get(SkillPublisher, publisher["id"])
            db_session.add(SkillPackage(
                id="spk_racer", owner_type="user", owner_id=pub.user_id, namespace="acme",
                name="review-helper", display_name="Review Helper", source_type="hosted-upload",
                source_locator="https://github.com/acme/skills", source_subdir="",
                publisher_id=pub.id,
            ))
            await db_session.commit()
            inserted.append("spk_racer")
        return result

    monkeypatch.setattr(mp.publishers_service, "resolve_publish_publisher", resolve_and_race)
    response = await _publish_with(client, token)
    assert response.status_code == 201, response.text
    assert response.json()["package_id"] == "spk_racer"


async def test_exchange_reports_the_default_visibility(client: AsyncClient, auth_headers: dict) -> None:
    await _setup(client, auth_headers)
    body = (await _exchange(client, _sign(_claims()))).json()
    assert body["owner_type"] == "user" and body["default_visibility"] == "user"


async def test_trust_from_a_departed_admin_no_longer_publishes(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    from sqlalchemy import delete as sa_delete

    from sessionfs.server.db.models import OrgMember
    from tests.server.integration.test_marketplace_moderation import _join

    org, _ = await _org(db_session, test_user)
    admin_user, admin_headers = await _user(db_session)
    await _join(db_session, org, admin_user, "admin")
    publisher = await _claim(client, auth_headers, "departco", org_id=org.id)
    await _trust(client, admin_headers, publisher["id"])  # created by the admin
    await db_session.execute(
        sa_delete(OrgMember).where(OrgMember.org_id == org.id, OrgMember.user_id == admin_user.id)
    )
    await db_session.commit()
    token = (await _exchange(client, _sign(_claims()))).json()["token"]
    response = await _publish_with(client, token)
    assert response.status_code == 403, response.text
    assert _error(response)["code"] == "trust_creator_not_authorized"


async def test_token_is_linked_even_when_the_publish_recovers_from_a_version_race(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sessionfs.server.db.models import SkillPackage
    from sessionfs.server.routes import marketplace as mp

    await _setup(client, auth_headers)
    first = (await _publish_with(client, (await _exchange(client, _sign(_claims()))).json()["token"])).json()
    token = (await _exchange(client, _sign(_claims()))).json()["token"]
    original = mp._locked_existing_version
    calls = {"n": 0}

    async def racing_lookup(db, package_id, content_hash):
        found = await original(db, package_id, content_hash)
        calls["n"] += 1
        if found is None and calls["n"] == 2:
            # A concurrent identical upload commits this exact version now.
            pkg = await db_session.get(SkillPackage, package_id)
            db_session.add(SkillPackageVersion(
                id="skv_winner", package_id=pkg.id, content_hash=content_hash, agent_plugin_schema="s",
                manifest_json="{}", dlp_status="passed", blob_key="winner-key", blob_size_bytes=1,
            ))
            await db_session.commit()
        return found

    monkeypatch.setattr(mp, "_locked_existing_version", racing_lookup)
    response = await _publish_with(client, token, package=_pkg(body="raced version"))
    assert response.status_code == 201, response.text
    assert response.json()["version_id"] == "skv_winner"
    assert first["version_id"] != "skv_winner"
    row = (await db_session.execute(
        select(SkillPublishToken).where(SkillPublishToken.token_hash == hashlib.sha256(token.encode()).hexdigest())
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert row.used_for_version_id == "skv_winner"


async def test_stale_jwks_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as _time

    from sessionfs.server.services.oidc_token import TokenValidationError

    async def down() -> dict:
        raise RuntimeError("GitHub unreachable")

    monkeypatch.setattr(github_oidc, "fetch_jwks", down)
    github_oidc.set_jwks_for_tests(_jwks())
    # Past the TTL but within stale-if-error: the cached set still serves.
    github_oidc._cache["fetched_at"] = _time.monotonic() - github_oidc._CACHE_TTL_SECONDS - 60
    assert await github_oidc._jwks(force=True) == _jwks()
    # Beyond it: fail closed.
    github_oidc._cache["fetched_at"] = (
        _time.monotonic() - github_oidc._CACHE_TTL_SECONDS - github_oidc._STALE_IF_ERROR_SECONDS - 1
    )
    github_oidc._cache["last_attempt"] = 0.0
    with pytest.raises(TokenValidationError) as excinfo:
        await github_oidc._jwks()
    assert excinfo.value.reason == "jwks_unavailable"


async def test_creator_removed_after_the_token_burn_cannot_publish(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attribution is re-checked (with locks) inside the final publish
    transaction, not only before the token is burned."""
    from sqlalchemy import delete as sa_delete

    from sessionfs.server.db.models import OrgMember
    from sessionfs.server.routes import marketplace_publishers as routes
    from tests.server.integration.test_marketplace_moderation import _join

    org, _ = await _org(db_session, test_user)
    admin_user, admin_headers = await _user(db_session)
    await _join(db_session, org, admin_user, "admin")
    publisher = await _claim(client, auth_headers, "lateremove", org_id=org.id)
    await _trust(client, admin_headers, publisher["id"])
    token = (await _exchange(client, _sign(_claims()))).json()["token"]
    original_burn = svc.burn_publish_token

    async def burn_then_remove(db, token_id):
        await original_burn(db, token_id)
        await db_session.execute(
            sa_delete(OrgMember).where(OrgMember.org_id == org.id, OrgMember.user_id == admin_user.id)
        )
        await db_session.commit()

    monkeypatch.setattr(routes.svc, "burn_publish_token", burn_then_remove)
    response = await _publish_with(client, token)
    assert response.status_code == 403, response.text
    assert _error(response)["code"] == "trust_creator_not_authorized"
    assert (await db_session.execute(select(SkillPackageVersion))).scalars().all() == []


async def test_cold_jwks_fetch_is_single_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent exchanges against an empty cache produce ONE outbound JWKS
    fetch; the rest wait on the single-flight lock and re-use the result."""
    import asyncio

    calls = 0

    async def slow_fetch() -> dict:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.2)
        return _jwks()

    monkeypatch.setattr(github_oidc, "fetch_jwks", slow_fetch)
    github_oidc.set_jwks_for_tests(None)
    results = await asyncio.gather(*(github_oidc._jwks() for _ in range(5)))
    assert results == [_jwks()] * 5
    assert calls == 1


async def test_cold_jwks_failure_is_negative_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed cold fetch is remembered for the cooldown: concurrent and
    subsequent exchanges fail fast instead of fetching again."""
    import asyncio

    from sessionfs.server.services.oidc_token import TokenValidationError

    calls = 0

    async def down() -> dict:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        raise RuntimeError("GitHub unreachable")

    monkeypatch.setattr(github_oidc, "fetch_jwks", down)
    github_oidc.set_jwks_for_tests(None)
    results = await asyncio.gather(*(github_oidc._jwks() for _ in range(5)), return_exceptions=True)
    assert all(
        isinstance(result, TokenValidationError) and result.reason == "jwks_unavailable"
        for result in results
    )
    assert calls == 1
    # Within the cooldown: fast failure, still no new fetch.
    with pytest.raises(TokenValidationError):
        await github_oidc._jwks()
    assert calls == 1
    # After the cooldown the next exchange retries the fetch.
    github_oidc._cache["last_failure"] = None
    with pytest.raises(TokenValidationError):
        await github_oidc._jwks()
    assert calls == 2


async def test_empty_jwks_cache_is_not_negative_cached_on_cold_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold, empty JWKS cache must trigger a fetch, not fail fast: an unset
    last_failure is None (not 0.0), so the first minute of uptime never reads
    as a fetch that just failed."""
    calls = 0

    async def up() -> dict:
        nonlocal calls
        calls += 1
        return _jwks()

    monkeypatch.setattr(github_oidc, "fetch_jwks", up)
    monkeypatch.setattr(github_oidc.time, "monotonic", lambda: 5.0)
    github_oidc.set_jwks_for_tests(None)
    assert await github_oidc._jwks() == _jwks()
    assert calls == 1


# ---------------------------------------------------------------------------
# No lock is held across validation or the blob upload
# ---------------------------------------------------------------------------


def _skill_blobs(blob_store) -> list[str]:
    root = blob_store.root / "skills"
    if not root.exists():
        return []
    return sorted(str(path.relative_to(blob_store.root)) for path in root.rglob("*") if path.is_file())


async def test_trusted_public_publish_takes_no_lock_before_validation_and_upload(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    blob_store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The global promotion lock, the namespace and publisher locks and the
    org's entitlement row are taken only after the upload is validated and the
    blob is stored. A namespace claim proceeds while the upload is in flight."""
    from sessionfs.server.routes import marketplace as mp

    org, _ = await _org(db_session, test_user)
    publisher = await _claim(client, auth_headers, "slowpub", org_id=org.id)
    await _trust(client, auth_headers, publisher["id"])
    await _verify_dns(client, auth_headers, publisher["id"], "slowpub.example", monkeypatch)
    _, admin_headers = await _user(db_session, tier="admin")
    await _enable_public(client, admin_headers, publisher["id"])
    token = (await _exchange(client, _sign(_claims()))).json()["token"]

    events: list[str] = []
    claiming = {"on": False}

    def _record(name, original):
        async def wrapper(*args, **kwargs):
            if not claiming["on"]:  # only the publish's own calls
                events.append(name)
            return await original(*args, **kwargs)
        return wrapper

    for name in ("lock_confusable_promotion", "lock_namespace", "lock_publisher"):
        monkeypatch.setattr(mp.publishers_service, name, _record(name, getattr(mp.publishers_service, name)))
    monkeypatch.setattr(mp, "lock_active_entitlement", _record("lock_active_entitlement", mp.lock_active_entitlement))
    monkeypatch.setattr(
        mp, "_validate_hosted_package_off_loop", _record("validate", mp._validate_hosted_package_off_loop)
    )
    real_put = blob_store.put
    claimed_during_upload: dict = {}

    async def slow_put(key, data):
        events.append("put")
        if "claimed" not in claimed_during_upload:
            # Another caller's namespace claim (global promotion + namespace
            # locks) completes while this upload is still in flight.
            claiming["on"] = True
            try:
                response = await client.post(
                    "/api/v1/publishers", headers=admin_headers, json={"namespace": "unrelatedns"}
                )
            finally:
                claiming["on"] = False
            claimed_during_upload["claimed"] = response.status_code
        await real_put(key, data)

    monkeypatch.setattr(blob_store, "put", slow_put)
    published = await _publish_with(client, token, visibility="public")
    assert published.status_code == 201, published.text
    assert claimed_during_upload["claimed"] == 201
    put_at = events.index("put")
    assert events.index("validate") < put_at
    before_upload = events[:put_at]
    assert not [event for event in before_upload if event.startswith("lock")], events
    # ...and every lock is still taken after the upload, in order.
    after_upload = events[put_at + 1:]
    publish_locks = [event for event in after_upload if event.startswith("lock")]
    for name in ("lock_confusable_promotion", "lock_namespace", "lock_publisher", "lock_active_entitlement"):
        assert name in publish_locks, publish_locks
    assert publish_locks.index("lock_confusable_promotion") < publish_locks.index("lock_namespace")
    assert publish_locks.index("lock_namespace") < publish_locks.index("lock_publisher")
    assert publish_locks.index("lock_publisher") < publish_locks.index("lock_active_entitlement")


async def _revoke_during_upload(
    client: AsyncClient, blob_store, monkeypatch: pytest.MonkeyPatch, action
) -> None:
    real_put = blob_store.put
    done: dict = {}

    async def put_then_change(key, data):
        await real_put(key, data)
        if not done:
            done["x"] = await action()

    monkeypatch.setattr(blob_store, "put", put_then_change)


async def test_trust_revoked_between_burn_and_commit_refuses_the_publish(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, blob_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token's trust is revoked after the burn (while the upload is
    stored, before any lock): the fence under the publisher lock refuses the
    publish, and the uploaded blob is removed."""
    publisher, trusted = await _setup(client, auth_headers)
    token = (await _exchange(client, _sign(_claims()))).json()["token"]

    async def revoke():
        response = await client.delete(
            f"/api/v1/publishers/{publisher['id']}/trusted-publishers/{trusted['id']}", headers=auth_headers
        )
        assert response.status_code == 200, response.text
        return response.status_code

    await _revoke_during_upload(client, blob_store, monkeypatch, revoke)
    response = await _publish_with(client, token)
    assert response.status_code == 401, response.text
    assert _error(response)["code"] == "invalid_publish_token"
    assert (await db_session.execute(select(SkillPackageVersion))).scalars().all() == []
    assert _skill_blobs(blob_store) == []


async def test_trusted_generation_change_between_burn_and_commit_refuses_the_publish(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, blob_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different trust of the same publisher is removed mid-publish: the
    token's trust is intact, but the publisher's authorization generation moved
    on, so the publish is refused."""
    publisher, _ = await _setup(client, auth_headers)
    other = await _trust(client, auth_headers, publisher["id"], workflow="other.yml")
    token = (await _exchange(client, _sign(_claims()))).json()["token"]

    async def bump_generation():
        response = await client.delete(
            f"/api/v1/publishers/{publisher['id']}/trusted-publishers/{other['id']}", headers=auth_headers
        )
        assert response.status_code == 200, response.text
        return response.status_code

    await _revoke_during_upload(client, blob_store, monkeypatch, bump_generation)
    response = await _publish_with(client, token)
    assert response.status_code == 401, response.text
    assert (await db_session.execute(select(SkillPackageVersion))).scalars().all() == []
    assert _skill_blobs(blob_store) == []


async def test_failed_publish_removes_only_its_own_blob(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, blob_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A publish refused after its blob was written removes that blob, never
    the blob of a version another listing references - including a version
    of the same bytes that a concurrent publish committed while this one
    waited for its locks."""
    from sessionfs.server.routes import marketplace as mp

    await _setup(client, auth_headers)
    first = await _publish_with(client, (await _exchange(client, _sign(_claims()))).json()["token"])
    assert first.status_code == 201, first.text
    kept = _skill_blobs(blob_store)
    assert len(kept) == 1

    # 1. Refused after the upload (version cap): only its own blob goes.
    monkeypatch.setattr(mp, "MAX_PACKAGE_VERSIONS", 1)
    token = (await _exchange(client, _sign(_claims()))).json()["token"]
    refused = await _publish_with(client, token, package=_pkg(body="over the cap"))
    assert refused.status_code == 429, refused.text
    assert _skill_blobs(blob_store) == kept
    monkeypatch.setattr(mp, "MAX_PACKAGE_VERSIONS", 100)

    # 2. A concurrent identical publish commits the version while this one
    # uploads: this publish adopts it and drops its own (unreferenced) blob;
    # the winner's blob stays.
    from sessionfs.server.services.skill_packages import validate_hosted_package

    raced_bytes = _pkg(body="raced")
    real_put = blob_store.put
    state: dict = {}

    async def put_while_other_wins(key, data):
        await real_put(key, data)
        if not state:
            state["own"] = key
            winner = await client.post(
                "/api/v1/users/me/skills/packages",
                headers=auth_headers,
                files={"package": ("p.tar.gz", raced_bytes, "application/gzip")},
                data={"namespace": "acme", "collection": "other",
                      "source_locator": "https://github.com/acme/skills"},
            )
            assert winner.status_code == 201, winner.text
            state["winner"] = winner.json()["version_id"]

    monkeypatch.setattr(blob_store, "put", put_while_other_wins)
    token = (await _exchange(client, _sign(_claims()))).json()["token"]
    adopted = await _publish_with(client, token, package=raced_bytes)
    assert adopted.status_code == 201, adopted.text
    assert adopted.json()["version_id"] == state["winner"]
    winner_row = await db_session.get(SkillPackageVersion, state["winner"])
    await db_session.refresh(winner_row)
    assert winner_row.blob_key != state["own"]
    assert await blob_store.get(winner_row.blob_key) == validate_hosted_package(
        raced_bytes, source_locator="https://github.com/acme/skills"
    ).canonical_archive
    assert await blob_store.get(state["own"]) is None
    assert sorted(_skill_blobs(blob_store)) == sorted([*kept, winner_row.blob_key])

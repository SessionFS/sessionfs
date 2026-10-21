"""Org skills policy: allowlist ``source`` entries match only VERIFIED
provenance (trusted publishing), never a publisher's declared
source_locator / source_commit (marketplace v2 §8.2)."""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server import github_app
from sessionfs.server.db.models import SkillPackageVersion, User
from sessionfs.server.services import skill_policy
from tests.server.integration.test_marketplace_publishers import _pkg
from tests.server.integration.test_skill_policy_api import _enterprise
from tests.server.integration.test_trusted_publishing import (  # noqa: F401 - autouse fixtures
    REPO_ID,
    SHA,
    _claims,
    _exchange,
    _fresh_publisher_limiters,
    _github_jwks,
    _publish_with,
    _setup,
    _sign,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _github_repos(monkeypatch: pytest.MonkeyPatch) -> dict:
    """GitHub as seen by the policy write: name -> numeric repository id."""
    repos = {("acme", "skills"): int(REPO_ID)}

    async def resolve(db, user_id, owner, repo):
        repo_id = repos.get((owner.lower(), repo.lower()))
        return None if repo_id is None else {"id": repo_id, "owner_id": 1, "full_name": f"{owner}/{repo}"}

    monkeypatch.setattr(github_app, "resolve_repository", resolve)
    return repos


async def _put_acknowledged(client: AsyncClient, headers: dict, org_id: str, policy: dict):
    url = f"/api/v1/orgs/{org_id}/skills/policy"
    preview = await client.post(f"{url}/preview", headers=headers, json={"policy": {"skills": policy}})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    return await client.put(
        url,
        headers={**headers, "If-Match": f'"osp-{body["current_revision"]}"'},
        json={"policy": {"skills": policy}, "acknowledge_warnings": [w["code"] for w in body["warnings"]]},
    )


async def test_source_allowlist_matches_trusted_publishing_not_declared_provenance(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _enterprise(db_session, test_user)
    policy = {
        "mode": "allowlist",
        "allowlist": [{"source": "github.com/acme/skills", "sha": SHA}],
        "allow_hooks": True,
    }
    saved = await _put_acknowledged(client, auth_headers, org.id, policy)
    assert saved.status_code == 200, saved.text
    # The entry is stored pinned to the repository id resolved on write.
    [entry] = saved.json()["policy"]["skills"]["allowlist"]
    assert entry["repository_id"] == int(REPO_ID)

    # A personal upload that merely DECLARES the allowlisted repo and commit.
    declared = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=auth_headers,
        files={"package": ("p.tar.gz", _pkg(name="declared-only"), "application/gzip")},
        data={
            "namespace": "kit",
            "collection": "kit",
            "source_locator": "https://github.com/acme/skills",
            "source_commit": SHA,
        },
    )
    assert declared.status_code == 201, declared.text
    assert declared.json()["source"]["commit"] == SHA
    declared_version = await db_session.get(SkillPackageVersion, declared.json()["version_id"])
    with pytest.raises(HTTPException) as refused:
        await skill_policy.enforce_for_org(db_session, org.id, declared_version)
    assert refused.value.status_code == 403
    assert {v["code"] for v in refused.value.detail["violations"]} == {"not_allowlisted"}

    # The same repository published through trusted publishing (the OIDC
    # token proves the repository and the run's commit) is allowed.
    await _setup(client, auth_headers)
    token = (await _exchange(client, _sign(_claims()), namespace="acme")).json()["token"]
    trusted = await _publish_with(client, token)
    assert trusted.status_code == 201, trusted.text
    trusted_version = await db_session.get(SkillPackageVersion, trusted.json()["version_id"])
    await skill_policy.enforce_for_org(db_session, org.id, trusted_version)  # no raise

    # A commit pin the verified run does not match is refused.
    other = {**policy, "allowlist": [{"source": "github.com/acme/skills", "sha": "f" * 40}]}
    assert (await _put_acknowledged(client, auth_headers, org.id, other)).status_code == 200
    with pytest.raises(HTTPException):
        await skill_policy.enforce_for_org(db_session, org.id, trusted_version)


async def test_verified_source_ignores_anything_but_oidc_publication_metadata() -> None:
    def version(metadata: dict | str) -> SkillPackageVersion:
        raw = metadata if isinstance(metadata, str) else json.dumps(metadata)
        return SkillPackageVersion(signature_metadata=raw, source_commit=SHA)

    real = {
        "kind": "github_oidc", "verification": "oidc_token",
        "published_from_repo": "github.com/acme/skills", "published_from_repo_id": 123456,
        "publishing_run_sha": SHA,
    }
    assert skill_policy.verified_source(version("{}")) == (None, None)
    assert skill_policy.verified_source(version("not json")) == (None, None)
    forged = {k: v for k, v in real.items() if k not in ("kind", "verification")}
    assert skill_policy.verified_source(version(forged)) == (None, None)
    assert skill_policy.verified_source(version(real)) == (123456, SHA)
    # Provenance recorded without the repository id matches nothing.
    no_id = {k: v for k, v in real.items() if k != "published_from_repo_id"}
    assert skill_policy.verified_source(version(no_id)) == (None, None)


async def test_a_resurrected_repository_name_does_not_match(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """The allowlisted repository was deleted and someone re-registered its
    name: their trusted-published version carries a different repository id
    and is refused, though the name matches."""
    org, _ = await _enterprise(db_session, test_user)
    policy = {"mode": "allowlist", "allowlist": [{"source": "github.com/acme/skills", "sha": "any"}], "allow_hooks": True}
    assert (await _put_acknowledged(client, auth_headers, org.id, policy)).status_code == 200
    await _setup(client, auth_headers)
    token = (await _exchange(client, _sign(_claims()), namespace="acme")).json()["token"]
    published = await _publish_with(client, token)
    assert published.status_code == 201, published.text
    version = await db_session.get(SkillPackageVersion, published.json()["version_id"])
    metadata = json.loads(version.signature_metadata)
    assert metadata["published_from_repo"] == "github.com/acme/skills"
    metadata["published_from_repo_id"] = int(REPO_ID) + 1  # the impostor's repository
    version.signature_metadata = json.dumps(metadata)
    await db_session.commit()
    with pytest.raises(HTTPException) as refused:
        await skill_policy.enforce_for_org(db_session, org.id, version)
    assert {v["code"] for v in refused.value.detail["violations"]} == {"not_allowlisted"}


async def test_unresolvable_source_is_refused_on_write(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _enterprise(db_session, test_user)
    url = f"/api/v1/orgs/{org.id}/skills/policy"
    for source in ("github.com/ghost/missing", "gitlab.com/acme/skills", "github.com/acme"):
        policy = {"skills": {"mode": "allowlist", "allowlist": [{"source": source}], "allow_hooks": True}}
        preview = await client.post(f"{url}/preview", headers=auth_headers, json={"policy": policy})
        assert preview.status_code == 422, preview.text
        assert preview.json()["error"]["code"] == "source_unresolved"
        put = await client.put(url, headers={**auth_headers, "If-Match": '"osp-0"'}, json={"policy": policy})
        assert put.status_code == 422 and put.json()["error"]["code"] == "source_unresolved"
    # An explicit repository_id is kept without a lookup (as for trusted publishers).
    explicit = {"mode": "allowlist", "allowlist": [{"source": "github.com/ghost/missing", "repository_id": 77}],
                "allow_hooks": True}
    saved = await _put_acknowledged(client, auth_headers, org.id, explicit)
    assert saved.status_code == 200, saved.text
    assert saved.json()["policy"]["skills"]["allowlist"][0]["repository_id"] == 77

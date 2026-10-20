"""Webhook endpoints fail closed when their secrets are not configured.

GitHub: with SFS_GITHUB_WEBHOOK_SECRET unset, every delivery is refused
with 503 before the payload is parsed, so a forged installation.deleted or
installation.created event cannot change github_installations. Local
development opts in explicitly with SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED=1.

GitLab: with no global SFS_GITLAB_WEBHOOK_SECRET and no matching per-user
secret, deliveries are refused with 401 and nothing is scheduled.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import GitHubInstallation, GitLabSettings, User
from sessionfs.server.routes import webhooks


@pytest.fixture(autouse=True)
def _reset_log_once(monkeypatch):
    monkeypatch.setattr(webhooks, "_missing_secret_logged", False)
    monkeypatch.setattr(webhooks, "_unsigned_opt_in_logged", False)
    monkeypatch.delenv("SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED", raising=False)
    monkeypatch.delenv("SFS_GITLAB_WEBHOOK_SECRET", raising=False)


def _install_payload(action: str, installation_id: int) -> bytes:
    return json.dumps(
        {
            "action": action,
            "installation": {
                "id": installation_id,
                "account": {"id": 1, "login": "victim", "type": "Organization"},
                "app_id": 1,
                "target_type": "Organization",
            },
            "sender": {"id": 666, "login": "attacker"},
        }
    ).encode()


async def _row(db: AsyncSession, installation_id: int) -> GitHubInstallation | None:
    return (
        await db.execute(
            select(GitHubInstallation)
            .where(GitHubInstallation.id == installation_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


GH_HEADERS = {"X-GitHub-Event": "installation", "X-GitHub-Delivery": "test-delivery-5", "Content-Type": "application/json"}


class TestGitHubFailsClosed:
    @pytest.mark.asyncio
    async def test_forged_delete_refused_without_secret(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch
    ):
        monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "")
        db_session.add(GitHubInstallation(id=810001, account_login="victim"))
        await db_session.commit()

        resp = await client.post(
            "/webhooks/github", content=_install_payload("deleted", 810001), headers=GH_HEADERS
        )
        assert resp.status_code == 503
        assert await _row(db_session, 810001) is not None

    @pytest.mark.asyncio
    async def test_forged_create_refused_without_secret(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch
    ):
        monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "")
        resp = await client.post(
            "/webhooks/github", content=_install_payload("created", 810002), headers=GH_HEADERS
        )
        assert resp.status_code == 503
        assert await _row(db_session, 810002) is None

    @pytest.mark.asyncio
    async def test_refused_before_parsing(self, client: AsyncClient, monkeypatch):
        """Even garbage bodies get the 503, not a JSON parse error."""
        monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "")
        resp = await client.post("/webhooks/github", content=b"{not json", headers=GH_HEADERS)
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_pull_request_refused_without_secret(self, client: AsyncClient, monkeypatch):
        monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "")
        called: list[dict] = []

        async def fake_handle(payload, fence=None):
            called.append(payload)

        monkeypatch.setattr(webhooks, "_handle_pr_event", fake_handle)
        resp = await client.post(
            "/webhooks/github",
            content=json.dumps(
                {"action": "opened", "pull_request": {}, "installation": {"id": 1}}
            ).encode(),
            headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "test-delivery-6", "Content-Type": "application/json"},
        )
        assert resp.status_code == 503
        assert called == []

    @pytest.mark.asyncio
    async def test_misconfiguration_logged_once(self, client: AsyncClient, monkeypatch, caplog):
        monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "")
        with caplog.at_level(logging.ERROR, logger="sessionfs.webhooks"):
            for _ in range(3):
                resp = await client.post(
                    "/webhooks/github", content=b"{}", headers=GH_HEADERS
                )
                assert resp.status_code == 503
        refusals = [r for r in caplog.records if "SFS_GITHUB_WEBHOOK_SECRET is not set" in r.message]
        assert len(refusals) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["", "0", "false", "no"])
    async def test_opt_in_must_be_explicit(self, client: AsyncClient, monkeypatch, value):
        monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "")
        monkeypatch.setenv("SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED", value)
        resp = await client.post("/webhooks/github", content=b"{}", headers=GH_HEADERS)
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_explicit_opt_in_accepts_unsigned(self, client: AsyncClient, monkeypatch):
        monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "")
        monkeypatch.setenv("SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED", "1")
        resp = await client.post(
            "/webhooks/github", content=b"{}", headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "test-delivery-7"}
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_secret_set_unsigned_rejected_even_with_opt_in(
        self, client: AsyncClient, monkeypatch
    ):
        """The opt-in never weakens a configured secret."""
        monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "whsec")
        monkeypatch.setenv("SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED", "1")
        resp = await client.post("/webhooks/github", content=b"{}", headers=GH_HEADERS)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_secret_set_valid_signature_accepted(self, client: AsyncClient, monkeypatch):
        monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", "whsec")
        body = b'{"zen": "hi"}'
        sig = "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
        resp = await client.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "test-delivery-8", "X-Hub-Signature-256": sig},
        )
        assert resp.status_code == 200


def _mr_payload() -> bytes:
    return json.dumps(
        {
            "object_kind": "merge_request",
            "object_attributes": {"action": "open", "iid": 1, "source_branch": "x"},
            "project": {"id": 1, "git_http_url": "https://gitlab.com/a/b.git"},
        }
    ).encode()


class TestGitLabFailsClosed:
    @pytest.fixture
    def scheduled(self, monkeypatch) -> list:
        calls: list = []

        async def fake_handle(payload, request, auth_user_id):
            calls.append(auth_user_id)

        monkeypatch.setattr(webhooks, "_handle_gitlab_mr", fake_handle)
        return calls

    @pytest.mark.asyncio
    async def test_no_secrets_configured_refused(self, client: AsyncClient, scheduled):
        resp = await client.post(
            "/webhooks/gitlab", content=_mr_payload(), headers={"X-Gitlab-Token": "anything"}
        )
        assert resp.status_code == 401
        assert scheduled == []

    @pytest.mark.asyncio
    async def test_missing_token_refused(self, client: AsyncClient, scheduled, monkeypatch):
        monkeypatch.setenv("SFS_GITLAB_WEBHOOK_SECRET", "glsec")
        resp = await client.post("/webhooks/gitlab", content=_mr_payload())
        assert resp.status_code == 401
        assert scheduled == []

    @pytest.mark.asyncio
    async def test_empty_per_user_secret_never_matches(
        self, client: AsyncClient, db_session: AsyncSession, scheduled
    ):
        user = User(
            id=str(uuid.uuid4()),
            email="gl@example.com",
            tier="pro",
            email_verified=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.add(
            GitLabSettings(
                user_id=user.id,
                instance_url="https://gitlab.com",
                encrypted_access_token="x",
                webhook_secret="",
            )
        )
        await db_session.commit()
        resp = await client.post(
            "/webhooks/gitlab", content=_mr_payload(), headers={"X-Gitlab-Token": " "}
        )
        assert resp.status_code == 401
        assert scheduled == []

    @pytest.mark.asyncio
    async def test_wrong_token_refused(self, client: AsyncClient, scheduled, monkeypatch):
        monkeypatch.setenv("SFS_GITLAB_WEBHOOK_SECRET", "glsec")
        resp = await client.post(
            "/webhooks/gitlab", content=_mr_payload(), headers={"X-Gitlab-Token": "nope"}
        )
        assert resp.status_code == 401
        assert scheduled == []

    @pytest.mark.asyncio
    async def test_global_secret_accepted(self, client: AsyncClient, scheduled, monkeypatch):
        monkeypatch.setenv("SFS_GITLAB_WEBHOOK_SECRET", "glsec")
        resp = await client.post(
            "/webhooks/gitlab", content=_mr_payload(), headers={"X-Gitlab-Token": "glsec"}
        )
        assert resp.status_code == 200

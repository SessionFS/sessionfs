"""Integration tests for GitHub webhook handler and related utilities."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import tarfile
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from sessionfs.server.github_app import normalize_git_remote
from sessionfs.server.pr_comment import build_pr_comment


# --- normalize_git_remote tests ---


class TestNormalizeGitRemote:
    def test_https_with_dot_git(self):
        assert normalize_git_remote("https://github.com/SessionFS/sessionfs.git") == "sessionfs/sessionfs"

    def test_https_without_dot_git(self):
        assert normalize_git_remote("https://github.com/SessionFS/sessionfs") == "sessionfs/sessionfs"

    def test_ssh_format(self):
        assert normalize_git_remote("git@github.com:SessionFS/sessionfs.git") == "sessionfs/sessionfs"

    def test_ssh_without_dot_git(self):
        assert normalize_git_remote("git@github.com:SessionFS/sessionfs") == "sessionfs/sessionfs"

    def test_lowercase_normalization(self):
        assert normalize_git_remote("https://github.com/MyOrg/MyRepo.git") == "myorg/myrepo"

    def test_empty_string(self):
        assert normalize_git_remote("") == ""

    def test_whitespace_stripped(self):
        assert normalize_git_remote("  https://github.com/org/repo.git  ") == "org/repo"

    def test_gitlab_ssh(self):
        assert normalize_git_remote("git@gitlab.com:team/project.git") == "team/project"

    def test_gitlab_https(self):
        assert normalize_git_remote("https://gitlab.com/team/project") == "team/project"


# --- PR comment builder tests ---


class TestBuildPRComment:
    def test_single_session(self):
        sessions = [
            {
                "session_id": "ses_abc123",
                "title": "Fix login bug",
                "source_tool": "claude-code",
                "model_id": "claude-opus-4-6",
                "message_count": 15,
                "trust_score": None,
            }
        ]
        comment = build_pr_comment(sessions)
        assert "AI Context (via SessionFS)" in comment
        assert "Fix login bug" in comment
        assert "claude-code (claude-opus-4-6)" in comment
        assert "15" in comment
        assert "ses_abc123" in comment
        assert "sessionfs.dev" in comment

    def test_single_session_with_trust_score(self):
        sessions = [
            {
                "session_id": "ses_xyz789",
                "title": "Add tests",
                "source_tool": "codex",
                "model_id": None,
                "message_count": 8,
                "trust_score": 0.95,
            }
        ]
        comment = build_pr_comment(sessions, include_trust=True)
        assert "95%" in comment
        assert "pass" in comment

    def test_single_session_low_trust_score(self):
        sessions = [
            {
                "session_id": "ses_low1",
                "title": "Risky refactor",
                "source_tool": "cursor",
                "model_id": None,
                "message_count": 3,
                "trust_score": 0.55,
            }
        ]
        comment = build_pr_comment(sessions, include_trust=True)
        assert "55%" in comment
        assert "fail" in comment

    def test_single_session_no_links(self):
        sessions = [
            {
                "session_id": "ses_nolink1",
                "title": "Quick fix",
                "source_tool": "claude-code",
                "model_id": None,
                "message_count": 2,
                "trust_score": None,
            }
        ]
        comment = build_pr_comment(sessions, include_links=False)
        assert "Quick fix" in comment
        assert "app.sessionfs.dev/sessions/ses_nolink1" not in comment

    def test_multi_session(self):
        sessions = [
            {
                "session_id": "ses_a1",
                "title": "Session A",
                "source_tool": "claude-code",
                "model_id": None,
                "message_count": 10,
                "trust_score": None,
            },
            {
                "session_id": "ses_b2",
                "title": "Session B",
                "source_tool": "codex",
                "model_id": None,
                "message_count": 5,
                "trust_score": None,
            },
        ]
        comment = build_pr_comment(sessions)
        assert "2 sessions" in comment
        assert "Session A" in comment
        assert "Session B" in comment
        assert "15 messages" in comment

    def test_multi_session_with_trust(self):
        sessions = [
            {
                "session_id": "ses_t1",
                "title": "Session 1",
                "source_tool": "claude-code",
                "model_id": None,
                "message_count": 10,
                "trust_score": 0.85,
            },
            {
                "session_id": "ses_t2",
                "title": "Session 2",
                "source_tool": "codex",
                "model_id": None,
                "message_count": 5,
                "trust_score": None,
            },
        ]
        comment = build_pr_comment(sessions, include_trust=True)
        assert "85%" in comment
        assert "warn" in comment
        # Session 2 has no trust score
        assert "\u2014" in comment


# --- Webhook signature verification tests ---


class TestWebhookSignature:
    def test_valid_signature(self):
        from sessionfs.server.routes.webhooks import _verify_signature

        secret = "test-secret"
        body = b'{"action": "opened"}'
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        with patch("sessionfs.server.routes.webhooks.GITHUB_WEBHOOK_SECRET", secret):
            # Should not raise
            _verify_signature(body, sig)

    def test_invalid_signature(self):
        from sessionfs.server.routes.webhooks import _verify_signature

        secret = "test-secret"
        body = b'{"action": "opened"}'
        bad_sig = "sha256=0000000000000000000000000000000000000000000000000000000000000000"

        with patch("sessionfs.server.routes.webhooks.GITHUB_WEBHOOK_SECRET", secret):
            with pytest.raises(Exception):  # HTTPException
                _verify_signature(body, bad_sig)

    def test_no_secret_skips_verification(self):
        from sessionfs.server.routes.webhooks import _verify_signature

        with patch("sessionfs.server.routes.webhooks.GITHUB_WEBHOOK_SECRET", ""):
            # Should not raise even with no signature
            _verify_signature(b"body", None)


# --- Webhook endpoint integration tests ---


@pytest.mark.asyncio
async def test_webhook_returns_ok(client: AsyncClient):
    """Webhook endpoint returns ok for valid events."""
    payload = {"action": "opened", "pull_request": {}, "installation": {"id": 1}}
    resp = await client.post(
        "/webhooks/github",
        content=json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "ping",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_webhook_installation_event(client: AsyncClient):
    """Installation events are accepted."""
    payload = {
        "action": "created",
        "installation": {
            "id": 12345,
            "account": {"login": "test-org", "type": "Organization"},
        },
    }
    resp = await client.post(
        "/webhooks/github",
        content=json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "installation",
        },
    )
    assert resp.status_code == 200


# --- Workspace extraction tests ---


class TestExtractWorkspaceFromArchive:
    def test_extracts_git_metadata(self):
        from sessionfs.server.routes.sessions import _extract_workspace_from_archive

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            ws = json.dumps({
                "root_path": "/home/user/project",
                "git": {
                    "remote_url": "https://github.com/MyOrg/MyRepo.git",
                    "branch": "feature/cool-thing",
                    "commit_sha": "abc123def456",
                },
            }).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(ws)
            tar.addfile(info, io.BytesIO(ws))

        result = _extract_workspace_from_archive(buf.getvalue())
        assert result is not None
        assert result["git_remote"] == "https://github.com/MyOrg/MyRepo.git"
        assert result["git_branch"] == "feature/cool-thing"
        assert result["git_commit"] == "abc123def456"

    def test_missing_workspace_returns_none(self):
        from sessionfs.server.routes.sessions import _extract_workspace_from_archive

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            manifest = json.dumps({"sfs_version": "0.1.0"}).encode()
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(manifest)
            tar.addfile(info, io.BytesIO(manifest))

        result = _extract_workspace_from_archive(buf.getvalue())
        assert result is None

    def test_no_git_section_returns_empty_strings(self):
        from sessionfs.server.routes.sessions import _extract_workspace_from_archive

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            ws = json.dumps({"root_path": "/tmp/project"}).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(ws)
            tar.addfile(info, io.BytesIO(ws))

        result = _extract_workspace_from_archive(buf.getvalue())
        assert result is not None
        assert result["git_remote"] == ""
        assert result["git_branch"] == ""
        assert result["git_commit"] == ""

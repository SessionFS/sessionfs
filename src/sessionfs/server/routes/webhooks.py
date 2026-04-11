"""GitHub and GitLab webhook handlers for PR/MR events."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sessionfs.server.db.models import GitHubInstallation, GitLabSettings, PRComment, Session
from sessionfs.server.github_app import (
    GITHUB_WEBHOOK_SECRET,
    get_installation_token,
    normalize_git_remote,
    post_or_update_comment,
)
from sessionfs.server.pr_comment import build_pr_comment

logger = logging.getLogger("sessionfs.webhooks")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _verify_signature(body: bytes, signature: str | None) -> None:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not GITHUB_WEBHOOK_SECRET:
        return  # No secret configured — dev mode
    if not signature:
        raise HTTPException(status_code=401, detail="Missing webhook signature")
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


@router.post("/github")
async def github_webhook(request: Request):
    """Receive GitHub webhook events."""
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    _verify_signature(body, signature)

    event = request.headers.get("X-GitHub-Event")
    payload = json.loads(body)

    if event == "pull_request":
        action = payload.get("action")
        if action in ("opened", "synchronize", "reopened"):
            # Run in background -- don't block the webhook response
            asyncio.create_task(_handle_pr_event(payload))

    elif event == "installation":
        action = payload.get("action")
        installation_data = payload.get("installation", {})
        installation_id = installation_data.get("id")
        account = installation_data.get("account", {})

        if action == "created" and installation_id:
            logger.info("GitHub App installed: %s (id=%s)", account.get("login"), installation_id)
            from sessionfs.server.db.engine import _session_factory
            if _session_factory:
                try:
                    async with _session_factory() as db:
                        existing = await db.execute(
                            select(GitHubInstallation).where(GitHubInstallation.id == installation_id)
                        )
                        if not existing.scalar_one_or_none():
                            db.add(GitHubInstallation(
                                id=installation_id,
                                account_login=account.get("login"),
                                account_type=account.get("type"),
                                # user_id left null — claimed when user links via settings
                            ))
                            await db.commit()
                except Exception:
                    logger.warning("Failed to persist GitHub installation %s", installation_id, exc_info=True)

        elif action == "deleted" and installation_id:
            logger.info("GitHub App uninstalled: %s (id=%s)", account.get("login"), installation_id)
            from sessionfs.server.db.engine import _session_factory
            if _session_factory:
                try:
                    async with _session_factory() as db:
                        from sqlalchemy import delete as sa_delete
                        await db.execute(
                            sa_delete(GitHubInstallation).where(GitHubInstallation.id == installation_id)
                        )
                        await db.commit()
                except Exception:
                    logger.warning("Failed to remove GitHub installation %s", installation_id, exc_info=True)

    return {"status": "ok"}


async def _handle_pr_event(payload: dict) -> None:
    """Match PR to sessions and post/update comment."""
    try:
        pr = payload["pull_request"]
        repo = payload["repository"]["full_name"]
        branch = pr["head"]["ref"]
        installation_id = payload["installation"]["id"]
        pr_number = pr["number"]

        clone_url = payload["repository"]["clone_url"]
        normalized = normalize_git_remote(clone_url)

        # Get DB session
        from sessionfs.server.db.engine import _session_factory as async_session_factory

        if async_session_factory is None:
            logger.error("DB session factory not initialized")
            return

        async with async_session_factory() as db:
            # Check installation settings
            inst = await db.execute(
                select(GitHubInstallation).where(
                    GitHubInstallation.id == installation_id
                )
            )
            installation = inst.scalar_one_or_none()
            if installation and not installation.auto_comment:
                return

            include_trust = (
                installation.include_trust_score if installation else True
            )
            include_links = (
                installation.include_session_links if installation else True
            )

            # Find matching sessions
            result = await db.execute(
                select(Session)
                .where(
                    Session.git_remote_normalized == normalized,
                    Session.git_branch == branch,
                    Session.is_deleted == False,  # noqa: E712
                )
                .order_by(Session.created_at.desc())
                .limit(5)
            )
            sessions = list(result.scalars().all())

            if not sessions:
                return

            # Auto-audit on PR if configured
            for s in sessions:
                try:
                    from sessionfs.server.db.models import User, UserJudgeSettings
                    user_result = await db.execute(select(User).where(User.id == s.user_id))
                    session_owner = user_result.scalar_one_or_none()
                    if session_owner and getattr(session_owner, "audit_trigger", "manual") == "on_pr":
                        judge_result = await db.execute(
                            select(UserJudgeSettings).where(UserJudgeSettings.user_id == s.user_id)
                        )
                        judge_settings = judge_result.scalar_one_or_none()
                        if judge_settings and judge_settings.encrypted_api_key:
                                # TODO(v0.9.8): Wire blob store access into background
                            # task so audit can actually run. For now, mark as pending.
                            logger.info(
                                "Auto-audit on PR requested for session %s (user %s has on_pr trigger + judge key)",
                                s.id, s.user_id,
                            )
                except Exception:
                    logger.debug("Auto-audit check failed for session %s", s.id, exc_info=True)

            # Build session data for comment
            session_data = []
            for s in sessions:
                d = {
                    "session_id": s.id,
                    "title": s.title,
                    "source_tool": s.source_tool,
                    "model_id": s.model_id,
                    "message_count": s.message_count,
                    "trust_score": None,
                }
                session_data.append(d)

            comment_body = build_pr_comment(
                session_data, include_trust, include_links
            )

            # Get installation token
            token = await get_installation_token(installation_id)

            # Check for existing comment (scoped to this GitHub installation)
            existing = await db.execute(
                select(PRComment).where(
                    PRComment.repo_full_name == repo,
                    PRComment.pr_number == pr_number,
                    PRComment.installation_id == installation_id,
                )
            )
            pr_comment = existing.scalar_one_or_none()

            existing_comment_id = (
                pr_comment.comment_id if pr_comment else None
            )

            # Post or update
            comment_id = await post_or_update_comment(
                token,
                repo,
                pr_number,
                comment_body,
                existing_comment_id,
            )

            # Store/update tracking
            session_id_list = json.dumps([s.id for s in sessions])
            if pr_comment:
                pr_comment.comment_id = comment_id
                pr_comment.session_ids = session_id_list
                pr_comment.updated_at = datetime.now(timezone.utc)
            else:
                db.add(
                    PRComment(
                        id=str(uuid.uuid4()),
                        installation_id=installation_id,
                        repo_full_name=repo,
                        pr_number=pr_number,
                        comment_id=comment_id,
                        session_ids=session_id_list,
                    )
                )
            await db.commit()

            logger.info(
                "Posted AI Context comment on %s#%d (%d sessions)",
                repo,
                pr_number,
                len(sessions),
            )

    except Exception as exc:
        logger.error("Failed to handle PR event: %s", exc)


# ---- GitLab Webhooks ----


@router.post("/gitlab")
async def gitlab_webhook(request: Request):
    """Handle GitLab webhook events (merge request).

    Authentication:
      - Global secret from SFS_GITLAB_WEBHOOK_SECRET authorizes ALL sessions
        (trusted deployment where the secret is a shared system-wide value).
      - Per-user secret from GitLabSettings.webhook_secret authorizes ONLY
        sessions belonging to that specific user. We bind the request to the
        matching user's id so a forged payload for a repo belonging to a
        different user cannot use another user's credentials.
    """
    token = request.headers.get("X-Gitlab-Token", "")
    if not token:
        raise HTTPException(401, "Missing webhook token")

    # auth_user_id == None means "global secret matched, trust sender";
    # otherwise it is the id of the user whose per-user secret matched and all
    # downstream session/credential lookups MUST be scoped to that user.
    auth_user_id: str | None = None

    global_secret = os.environ.get("SFS_GITLAB_WEBHOOK_SECRET", "")
    if global_secret and hmac.compare_digest(token, global_secret):
        pass  # Global secret matches — auth_user_id stays None
    else:
        from sessionfs.server.db.engine import _session_factory
        if _session_factory is None:
            raise HTTPException(503, "Database not initialized")
        async with _session_factory() as db:
            from sessionfs.server.db.models import GitLabSettings as _GLS
            # Compare all rows in constant time to avoid leaking which
            # user's secret is being probed via response-time side channels.
            rows = (
                await db.execute(select(_GLS.user_id, _GLS.webhook_secret))
            ).all()
            for row_user_id, row_secret in rows:
                if row_secret and hmac.compare_digest(token, row_secret):
                    auth_user_id = row_user_id
                    # Don't break — finish the loop so timing is constant.
        if auth_user_id is None:
            raise HTTPException(401, "Invalid webhook token")

    payload = await request.json()
    event_type = payload.get("object_kind")

    if event_type != "merge_request":
        return {"status": "ignored", "reason": f"Not a merge_request event: {event_type}"}

    action = payload.get("object_attributes", {}).get("action", "")
    if action not in ("open", "update", "reopen"):
        return {"status": "ignored", "reason": f"Action not handled: {action}"}

    asyncio.ensure_future(_handle_gitlab_mr(payload, request, auth_user_id))
    return {"status": "processing"}


async def _handle_gitlab_mr(
    payload: dict,
    request: Request,
    auth_user_id: str | None,
):
    """Match MR to sessions and post comment."""
    try:
        from sessionfs.server.db.engine import _session_factory
        if _session_factory is None:
            logger.error("GitLab webhook: DB session factory not initialized")
            return
        async with _session_factory() as db:
            mr = payload.get("object_attributes", {})
            project = payload.get("project", {})

            repo_url = project.get("git_http_url", "")
            ssh_url = project.get("git_ssh_url", "")
            branch = mr.get("source_branch", "")
            mr_iid = mr.get("iid")
            project_id = project.get("id")

            if not branch or not mr_iid or not project_id:
                return

            # Normalize both URL formats
            normalized = normalize_git_remote(repo_url)
            if not normalized:
                normalized = normalize_git_remote(ssh_url)
            if not normalized:
                return

            # Find matching sessions. When the webhook was authenticated via
            # a per-user secret, scope to that user so a forged payload for
            # a repo belonging to a DIFFERENT user can't use another user's
            # credentials. Global-secret auth (auth_user_id is None) is
            # trusted and can match across all users' sessions.
            stmt = select(Session).where(
                Session.git_remote_normalized == normalized,
                Session.git_branch == branch,
                Session.is_deleted == False,  # noqa: E712
            )
            if auth_user_id is not None:
                stmt = stmt.where(Session.user_id == auth_user_id)
            stmt = stmt.order_by(Session.created_at.desc()).limit(10)

            result = await db.execute(stmt)
            sessions = result.scalars().all()

            if not sessions:
                return

            # Build session data for comment
            session_data = []
            for s in sessions:
                session_data.append({
                    "session_id": s.id,
                    "title": s.title,
                    "source_tool": s.source_tool,
                    "model_id": s.model_id,
                    "message_count": s.message_count,
                    "trust_score": None,
                })

            comment_body = build_pr_comment(session_data)

            # Find GitLab token. For per-user secret auth, use that user's
            # settings directly (not sessions[0].user_id which could belong
            # to another user if the session filter found more than one owner
            # in global mode).
            owner_id = auth_user_id if auth_user_id is not None else sessions[0].user_id
            gitlab_stmt = select(GitLabSettings).where(GitLabSettings.user_id == owner_id)
            gitlab_result = await db.execute(gitlab_stmt)
            gitlab_settings = gitlab_result.scalar_one_or_none()

            if not gitlab_settings:
                logger.warning("No GitLab token for user %s, skipping MR comment", owner_id)
                return

            # Decrypt token
            import base64
            import hashlib as _hashlib

            from cryptography.fernet import Fernet

            secret = os.environ.get("SFS_VERIFICATION_SECRET", "dev-secret")
            key = base64.urlsafe_b64encode(_hashlib.sha256(secret.encode()).digest())
            fernet = Fernet(key)
            access_token = fernet.decrypt(gitlab_settings.encrypted_access_token.encode()).decode()

            # Post or update comment (avoid duplicates on MR updates)
            from sessionfs.server.gitlab_client import GitLabClient

            client = GitLabClient(gitlab_settings.instance_url, access_token)

            # Check for existing comment tracking (scoped to GitLab via installation_id=0)
            existing_comment = await db.execute(
                select(PRComment).where(
                    PRComment.repo_full_name == normalized,
                    PRComment.pr_number == mr_iid,
                    PRComment.installation_id == 0,
                )
            )
            mr_comment = existing_comment.scalar_one_or_none()

            if mr_comment and mr_comment.comment_id:
                await client.update_mr_comment(project_id, mr_iid, mr_comment.comment_id, comment_body)
                mr_comment.session_ids = json.dumps([s.id for s in sessions])
                mr_comment.updated_at = datetime.now(timezone.utc)
            else:
                note_resp = await client.post_mr_comment(project_id, mr_iid, comment_body)
                note_id = note_resp.get("id")
                if mr_comment:
                    mr_comment.comment_id = note_id
                    mr_comment.session_ids = json.dumps([s.id for s in sessions])
                    mr_comment.updated_at = datetime.now(timezone.utc)
                else:
                    db.add(PRComment(
                        id=str(uuid.uuid4()),
                        installation_id=0,
                        repo_full_name=normalized,
                        pr_number=mr_iid,
                        comment_id=note_id,
                        session_ids=json.dumps([s.id for s in sessions]),
                    ))
            await db.commit()

            logger.info("Posted AI Context comment on GitLab MR %s!%d (%d sessions)", normalized, mr_iid, len(sessions))

    except Exception as exc:
        logger.error("Failed to handle GitLab MR event: %s", exc)

"""GitHub webhook handler for PR events."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from sessionfs.server.db.models import GitHubInstallation, PRComment, Session
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
    if not GITHUB_WEBHOOK_SECRET or not signature:
        return  # Skip in dev
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
        if action == "created":
            logger.info(
                "GitHub App installed: %s",
                payload["installation"]["account"]["login"],
            )
        elif action == "deleted":
            logger.info(
                "GitHub App uninstalled: %s",
                payload["installation"]["account"]["login"],
            )

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

            # Check for existing comment
            existing = await db.execute(
                select(PRComment).where(
                    PRComment.repo_full_name == repo,
                    PRComment.pr_number == pr_number,
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

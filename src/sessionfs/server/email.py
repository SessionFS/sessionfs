"""Transactional email via Resend API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RESEND_API = "https://api.resend.com/emails"


class EmailService:
    """Sends transactional email via Resend."""

    def __init__(
        self,
        api_key: str,
        from_email: str = "SessionFS <noreply@sessionfs.dev>",
    ) -> None:
        self._api_key = api_key
        self._from_email = from_email

    async def send_verification(self, to_email: str, verify_url: str) -> dict[str, Any]:
        """Send email verification link."""
        html = (
            "<div style='font-family: system-ui, sans-serif; max-width: 480px; margin: 0 auto;'>"
            "<h2 style='margin-bottom: 24px;'>Verify your SessionFS account</h2>"
            "<p>Click the link below to verify your email and enable cloud sync:</p>"
            f"<p><a href='{verify_url}' style='display: inline-block; background: #58a6ff; "
            "color: white; padding: 10px 24px; border-radius: 6px; text-decoration: none; "
            f"font-weight: 500;'>Verify Email</a></p>"
            "<p style='color: #8b949e; font-size: 13px; margin-top: 24px;'>"
            "This link expires in 24 hours. If you didn't create a SessionFS account, "
            "ignore this email.</p>"
            "</div>"
        )
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                RESEND_API,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "from": self._from_email,
                    "to": to_email,
                    "subject": "Verify your SessionFS account",
                    "html": html,
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def send_handoff(
        self,
        to_email: str,
        sender_email: str,
        session_title: str | None,
        source_tool: str | None,
        model_id: str | None,
        message_count: int,
        total_tokens: int,
        git_remote: str | None,
        git_branch: str | None,
        sender_message: str | None,
        handoff_id: str,
        dashboard_url: str | None = None,
    ) -> dict[str, Any]:
        """Send a handoff notification email."""
        from sessionfs.server.email_templates import handoff_email

        pull_command = f"sfs pull --handoff {handoff_id}"
        html = handoff_email(
            sender_email=sender_email,
            session_title=session_title,
            source_tool=source_tool,
            model_id=model_id,
            message_count=message_count,
            total_tokens=total_tokens,
            git_remote=git_remote,
            git_branch=git_branch,
            sender_message=sender_message,
            handoff_id=handoff_id,
            pull_command=pull_command,
            dashboard_url=dashboard_url,
        )
        title = session_title or "a session"
        subject = f"SessionFS: {sender_email} handed off {title}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                RESEND_API,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "from": self._from_email,
                    "to": to_email,
                    "subject": subject,
                    "html": html,
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()

    async def send_retention_notice(
        self, to_email: str, purged_count: int, session_titles: list[str],
    ) -> dict[str, Any]:
        """Notify free-tier user about purged sessions."""
        titles_html = "".join(f"<li>{t}</li>" for t in session_titles[:5])
        if purged_count > 5:
            titles_html += f"<li>...and {purged_count - 5} more</li>"
        html = (
            "<div style='font-family: system-ui, sans-serif; max-width: 480px; margin: 0 auto;'>"
            f"<h2>{purged_count} session(s) archived from cloud</h2>"
            "<p>These sessions were older than 14 days and have been removed from "
            "cloud storage (they're still on your local machine):</p>"
            f"<ul>{titles_html}</ul>"
            "<p>Upgrade to Pro for unlimited cloud retention.</p>"
            "</div>"
        )
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                RESEND_API,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "from": self._from_email,
                    "to": to_email,
                    "subject": f"SessionFS: {purged_count} session(s) archived from cloud",
                    "html": html,
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()

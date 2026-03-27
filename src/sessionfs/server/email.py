"""Multi-provider transactional email.

Supports Resend API, SMTP, or a null provider for air-gapped deployments.
Provider selection is automatic based on which env vars are configured,
or can be forced via SFS_EMAIL_PROVIDER.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from abc import ABC, abstractmethod
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import httpx

logger = logging.getLogger("sessionfs.email")

RESEND_API = "https://api.resend.com/emails"


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class EmailProvider(ABC):
    """Abstract email provider interface."""

    @abstractmethod
    async def send(self, to: str, subject: str, html: str) -> bool:
        """Send an email. Returns True on success."""
        ...

    # Convenience methods used by the rest of the codebase

    async def send_verification(self, to_email: str, verify_url: str) -> dict[str, Any]:
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
        ok = await self.send(to_email, "Verify your SessionFS account", html)
        return {"status": "sent" if ok else "failed"}

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
        from sessionfs.server.email_templates import handoff_email

        pull_command = f"sfs pull-handoff {handoff_id}"
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
        ok = await self.send(to_email, subject, html)
        return {"status": "sent" if ok else "failed"}

    async def send_retention_notice(
        self, to_email: str, purged_count: int, session_titles: list[str],
    ) -> dict[str, Any]:
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
        ok = await self.send(
            to_email,
            f"SessionFS: {purged_count} session(s) archived from cloud",
            html,
        )
        return {"status": "sent" if ok else "failed"}


# ---------------------------------------------------------------------------
# Resend provider
# ---------------------------------------------------------------------------


class ResendProvider(EmailProvider):
    """Send email via Resend API."""

    def __init__(self, api_key: str, from_email: str) -> None:
        self._api_key = api_key
        self._from_email = from_email

    async def send(self, to: str, subject: str, html: str) -> bool:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    RESEND_API,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "from": self._from_email,
                        "to": [to],
                        "subject": subject,
                        "html": html,
                    },
                    timeout=30.0,
                )
                resp.raise_for_status()
                return True
        except Exception:
            logger.exception("Resend send failed to %s", to)
            return False


# ---------------------------------------------------------------------------
# SMTP provider
# ---------------------------------------------------------------------------


class SMTPProvider(EmailProvider):
    """Send email via SMTP."""

    def __init__(
        self,
        host: str,
        port: int = 587,
        username: str = "",
        password: str = "",
        from_email: str = "SessionFS <noreply@sessionfs.dev>",
        use_tls: bool = True,
        use_ssl: bool = False,
        verify_ssl: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_email = from_email
        self._use_tls = use_tls
        self._use_ssl = use_ssl
        self._verify_ssl = verify_ssl

    async def send(self, to: str, subject: str, html: str) -> bool:
        msg = MIMEMultipart("alternative")
        msg["From"] = self._from_email
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(html, "html"))
        return await asyncio.to_thread(self._send_sync, msg)

    def _send_sync(self, msg: MIMEMultipart) -> bool:
        import smtplib

        try:
            if self._use_ssl:
                context = ssl.create_default_context()
                if not self._verify_ssl:
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                with smtplib.SMTP_SSL(self._host, self._port, context=context) as srv:
                    if self._username:
                        srv.login(self._username, self._password)
                    srv.send_message(msg)
            else:
                with smtplib.SMTP(self._host, self._port, timeout=30) as srv:
                    srv.ehlo()
                    if self._use_tls:
                        context = ssl.create_default_context()
                        if not self._verify_ssl:
                            context.check_hostname = False
                            context.verify_mode = ssl.CERT_NONE
                        srv.starttls(context=context)
                        srv.ehlo()
                    if self._username:
                        srv.login(self._username, self._password)
                    srv.send_message(msg)
            logger.info("Email sent via SMTP to %s", msg["To"])
            return True
        except Exception:
            logger.exception("SMTP send failed to %s", msg["To"])
            return False


# ---------------------------------------------------------------------------
# Null provider (air-gapped / no email)
# ---------------------------------------------------------------------------


class NullProvider(EmailProvider):
    """No-op email provider — logs instead of sending."""

    async def send(self, to: str, subject: str, html: str) -> bool:
        logger.info("Email suppressed (no provider): to=%s subject=%s", to, subject)
        return True


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_email_provider(config) -> EmailProvider:
    """Create the appropriate email provider from ServerConfig.

    Selection order:
    1. SFS_EMAIL_PROVIDER value ("resend", "smtp", "none")
    2. Auto-detect: if resend_api_key set → Resend
    3. Auto-detect: if smtp_host set → SMTP
    4. Fallback: NullProvider
    """
    provider = config.email_provider.lower()

    if provider == "resend":
        return ResendProvider(api_key=config.resend_api_key, from_email=config.email_from)
    elif provider == "smtp":
        return SMTPProvider(
            host=config.smtp_host,
            port=config.smtp_port,
            username=config.smtp_username,
            password=config.smtp_password,
            from_email=config.email_from,
            use_tls=config.smtp_tls,
            use_ssl=config.smtp_ssl,
            verify_ssl=config.smtp_verify_ssl,
        )
    elif provider == "none":
        return NullProvider()
    elif provider == "auto":
        if config.resend_api_key:
            return ResendProvider(api_key=config.resend_api_key, from_email=config.email_from)
        if config.smtp_host:
            return SMTPProvider(
                host=config.smtp_host,
                port=config.smtp_port,
                username=config.smtp_username,
                password=config.smtp_password,
                from_email=config.email_from,
                use_tls=config.smtp_tls,
                use_ssl=config.smtp_ssl,
            )
        return NullProvider()
    else:
        logger.warning("Unknown email provider '%s', using NullProvider", provider)
        return NullProvider()


# Legacy aliases for backwards compatibility
EmailService = ResendProvider
SMTPEmailService = SMTPProvider

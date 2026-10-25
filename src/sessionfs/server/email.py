"""Multi-provider transactional email.

Supports Resend API, SMTP, or a null provider for air-gapped deployments.
Provider selection is automatic based on which env vars are configured,
or can be forced via SFS_EMAIL_PROVIDER.
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
from abc import ABC, abstractmethod
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import httpx

logger = logging.getLogger("sessionfs.email")

# C0 control characters (and DEL) that must never reach an email header such
# as the subject: newlines and other controls enable header/smuggling attacks.
_SUBJECT_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def strip_subject_controls(value: str) -> str:
    """Remove C0 control characters from a value used in an email subject."""
    return _SUBJECT_CTRL_RE.sub("", value)


def mask_address(address: object) -> str:
    """Hide the local part of a recipient address in logs (keep the domain)."""
    text = str(address or "")
    local, sep, domain = text.rpartition("@")
    if not sep:
        return "***"
    return f"{local[:1]}***@{domain}"

RESEND_API = "https://api.resend.com/emails"


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class EmailProvider(ABC):
    """Abstract email provider interface."""

    @abstractmethod
    async def send(self, to: str, subject: str, html: str, text: str | None = None) -> bool:
        """Send an email. Returns True on success.

        ``text`` is an optional plain-text alternative; providers that
        support a text part attach it, others ignore it."""
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
        preview_token: str | None = None,
    ) -> dict[str, Any]:
        from sessionfs.server.email_templates import handoff_email

        pull_command = f"sfs pull-handoff {handoff_id}"
        # Build the pre-signup landing page URL (P1 — tk_d1de83ad4a5d4187).
        # The token travels in the fragment so the browser can read it
        # without it hitting server access logs or Referer headers.
        landing_url: str | None = None
        if dashboard_url and preview_token:
            landing_url = (
                f"{dashboard_url}/handoffs/claim/{handoff_id}#t={preview_token}"
            )
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
            landing_url=landing_url,
        )
        title = session_title or "a session"
        subject = f"SessionFS: {sender_email} handed off {title}"
        ok = await self.send(to_email, subject, html)
        return {"status": "sent" if ok else "failed"}

    # v0.10.9 — handoff lifecycle notifications

    async def send_handoff_claimed(
        self,
        to_email: str,
        recipient_email: str,
        session_title: str | None,
        handoff_id: str,
    ) -> dict[str, Any]:
        from sessionfs.server.email_templates import handoff_claimed_email

        html = handoff_claimed_email(
            recipient_email=recipient_email,
            session_title=session_title,
            handoff_id=handoff_id,
        )
        title = session_title or "a session"
        subject = f"SessionFS: {recipient_email} claimed your handoff of {title}"
        ok = await self.send(to_email, subject, html)
        return {"status": "sent" if ok else "failed"}

    async def send_handoff_revoked(
        self,
        to_email: str,
        sender_email: str,
        session_title: str | None,
        reason: str,
        handoff_id: str,
    ) -> dict[str, Any]:
        from sessionfs.server.email_templates import handoff_revoked_email

        html = handoff_revoked_email(
            sender_email=sender_email,
            session_title=session_title,
            reason=reason,
            handoff_id=handoff_id,
        )
        title = session_title or "a session"
        subject = f"SessionFS: {sender_email} revoked their handoff of {title}"
        ok = await self.send(to_email, subject, html)
        return {"status": "sent" if ok else "failed"}

    async def send_handoff_declined(
        self,
        to_email: str,
        recipient_email: str,
        session_title: str | None,
        reason: str | None,
        handoff_id: str,
    ) -> dict[str, Any]:
        from sessionfs.server.email_templates import handoff_declined_email

        html = handoff_declined_email(
            recipient_email=recipient_email,
            session_title=session_title,
            reason=reason,
            handoff_id=handoff_id,
        )
        title = session_title or "a session"
        subject = f"SessionFS: {recipient_email} declined your handoff of {title}"
        ok = await self.send(to_email, subject, html)
        return {"status": "sent" if ok else "failed"}

    async def send_handoff_comment(
        self,
        to_email: str,
        author_email: str,
        session_title: str | None,
        content: str,
        handoff_id: str,
    ) -> dict[str, Any]:
        from sessionfs.server.email_templates import handoff_comment_email

        html = handoff_comment_email(
            author_email=author_email,
            session_title=session_title,
            content=content,
            handoff_id=handoff_id,
        )
        title = session_title or "a session"
        subject = f"SessionFS: {author_email} commented on handoff of {title}"
        ok = await self.send(to_email, subject, html)
        return {"status": "sent" if ok else "failed"}

    async def send_org_invite(
        self,
        to_email: str,
        org_name: str,
        inviter_name: str,
        inviter_email: str,
        role: str,
        accept_url: str,
        expires_at_human: str,
    ) -> dict[str, Any]:
        from sessionfs.server.email_templates import org_invite_email

        html = org_invite_email(
            org_name=org_name,
            inviter_name=inviter_name,
            inviter_email=inviter_email,
            role=role,
            accept_url=accept_url,
            expires_at_human=expires_at_human,
        )
        subject = f"SessionFS: {inviter_name or inviter_email} invited you to {org_name}"
        ok = await self.send(to_email, subject, html)
        return {"status": "sent" if ok else "failed"}

    async def send_org_digest(
        self,
        to_email: str,
        org_name: str,
        week_label: str,
        digest: dict,
        dashboard_url: str,
    ) -> dict[str, Any]:
        from sessionfs.server.email_templates import org_digest_email, org_digest_text

        html = org_digest_email(
            org_name=org_name,
            week_label=week_label,
            digest=digest,
            dashboard_url=dashboard_url,
        )
        text = org_digest_text(
            org_name=org_name,
            week_label=week_label,
            digest=digest,
            dashboard_url=dashboard_url,
        )
        subject = f"SessionFS: {strip_subject_controls(org_name)} weekly activity ({week_label})"
        ok = await self.send(to_email, subject, html, text=text)
        return {"status": "sent" if ok else "failed"}

    async def send_transfer_proposed(
        self,
        to_email: str,
        project_name: str,
        org_name: str,
        proposer_name: str,
        reason: str,
        expires_at_human: str,
    ) -> dict[str, Any]:
        from sessionfs.server.email_templates import transfer_proposed_email

        html = transfer_proposed_email(
            project_name=project_name,
            org_name=org_name,
            proposer_name=proposer_name,
            reason=reason,
            expires_at_human=expires_at_human,
        )
        subject = f"SessionFS: transfer of {strip_subject_controls(project_name or 'a project')} proposed"
        ok = await self.send(to_email, subject, html)
        return {"status": "sent" if ok else "failed"}

    async def send_transfer_proposal_declined(
        self,
        to_email: str,
        project_name: str,
        org_name: str,
        owner_name: str,
    ) -> dict[str, Any]:
        from sessionfs.server.email_templates import transfer_proposal_declined_email

        html = transfer_proposal_declined_email(
            project_name=project_name,
            org_name=org_name,
            owner_name=owner_name,
        )
        subject = f"SessionFS: transfer of {strip_subject_controls(project_name or 'a project')} declined"
        ok = await self.send(to_email, subject, html)
        return {"status": "sent" if ok else "failed"}

    async def send_transfer_proposal_expired(
        self,
        to_email: str,
        project_name: str,
        org_name: str,
        owner_name: str,
    ) -> dict[str, Any]:
        from sessionfs.server.email_templates import transfer_proposal_expired_email

        html = transfer_proposal_expired_email(
            project_name=project_name,
            org_name=org_name,
            owner_name=owner_name,
        )
        subject = f"SessionFS: transfer of {strip_subject_controls(project_name or 'a project')} expired"
        ok = await self.send(to_email, subject, html)
        return {"status": "sent" if ok else "failed"}

    async def send_ticket_notification(
        self,
        to_email: str,
        notifications: list[dict],
        dashboard_url: str,
    ) -> dict[str, Any]:
        from sessionfs.server.email_templates import (
            ticket_notification_email,
            ticket_notification_text,
        )

        html = ticket_notification_email(
            notifications=notifications,
            dashboard_url=dashboard_url,
        )
        text = ticket_notification_text(
            notifications=notifications,
            dashboard_url=dashboard_url,
        )
        first_title = notifications[0]["ticket_title"] if notifications else None
        count = len(notifications)
        subject = (
            f"SessionFS: {count} ticket update(s) — "
            f"{strip_subject_controls(first_title or 'tickets you follow')}"
        )
        ok = await self.send(to_email, subject, html, text=text)
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

    async def send(self, to: str, subject: str, html: str, text: str | None = None) -> bool:
        try:
            payload: dict[str, Any] = {
                "from": self._from_email,
                "to": [to],
                "subject": subject,
                "html": html,
            }
            if text is not None:
                payload["text"] = text
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    RESEND_API,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                    timeout=30.0,
                )
                resp.raise_for_status()
                return True
        except Exception:
            logger.exception("Resend send failed to %s", mask_address(to))
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

    async def send(self, to: str, subject: str, html: str, text: str | None = None) -> bool:
        msg = MIMEMultipart("alternative")
        msg["From"] = self._from_email
        msg["To"] = to
        msg["Subject"] = subject
        if text is not None:
            msg.attach(MIMEText(text, "plain"))
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
            logger.info("Email sent via SMTP to %s", mask_address(msg["To"]))
            return True
        except Exception as exc:
            # No traceback: smtplib's refusal errors carry the cleartext
            # sender / recipient addresses in their args.
            logger.error(
                "SMTP send failed to %s: %s", mask_address(msg["To"]), type(exc).__name__
            )
            return False


# ---------------------------------------------------------------------------
# Null provider (air-gapped / no email)
# ---------------------------------------------------------------------------


class NullProvider(EmailProvider):
    """No-op email provider — logs instead of sending."""

    async def send(self, to: str, subject: str, html: str, text: str | None = None) -> bool:
        logger.info("Email suppressed (no provider): to=%s", mask_address(to))
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

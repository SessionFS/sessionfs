"""Org-invite email + dispatch helpers — v0.10.22 (tk_6afbcfefe5804c1d).

Both the legacy single-org invite endpoint (routes/org.py) and the
multi-org invite endpoint (routes/org_members.py) need to fire the
recipient email after creating an OrgInvite row, and the resend
endpoint (routes/org_members.py) needs the same wiring. Centralizing
here keeps the three call sites in lockstep — the SessionFS convention
is "one helper, three importers" rather than three near-identical
try/except blocks.

Service-only — no routes here. The helper takes the already-resolved
OrgInvite, Organization, and inviter User so the route layer keeps
ownership of the auth + invite-creation transaction.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Organization, OrgInvite, User

logger = logging.getLogger("sessionfs.api")


def invite_accept_url(request: Request, invite_id: str) -> str:
    """The dashboard link an invitee opens to accept ``invite_id``.

    Dashboard route is `/invites` (list); highlight= query param so the
    InvitesPage scrolls/outlines the specific invite. Using a query param
    means the link works whether the user has a logged-in session or has to
    log in first — the dashboard preserves the query string across the
    login redirect. The link carries no secret: accepting still requires
    signing in as the invited email address, so the invite routes also
    return it to the admin for servers with no email provider.
    """
    config = getattr(request.app.state, "config", None)
    base_url = (
        config.app_url.rstrip("/")
        if config and getattr(config, "app_url", None)
        else "https://app.sessionfs.dev"
    )
    return f"{base_url}/invites?highlight={invite_id}"


async def dispatch_invite_email(
    *,
    request: Request,
    db: AsyncSession,
    invite: OrgInvite,
    org: Organization,
    inviter: User,
) -> bool:
    """Best-effort send the org-invite email.

    Returns True on successful send, False otherwise. Never raises —
    a transient SMTP failure must not 500 the invite endpoint. On
    success, stamps `invite.last_emailed_at` and commits so the
    dashboard / resend endpoint can show the last nudge.
    """
    email_service = getattr(request.app.state, "email_service", None)
    if email_service is None:
        return False

    accept_url = invite_accept_url(request, invite.id)
    expires_human = invite.expires_at.strftime("%B %d, %Y")

    inviter_name = inviter.display_name or inviter.email
    try:
        result = await email_service.send_org_invite(
            to_email=invite.email,
            org_name=org.name,
            inviter_name=inviter_name,
            inviter_email=inviter.email,
            role=invite.role,
            accept_url=accept_url,
            expires_at_human=expires_human,
        )
    except Exception:
        logger.exception("Org invite %s email dispatch raised", invite.id)
        return False

    if result.get("status") == "sent":
        invite.last_emailed_at = datetime.now(timezone.utc)
        try:
            await db.commit()
        except Exception:
            logger.exception(
                "Org invite %s last_emailed_at commit failed (send already done)",
                invite.id,
            )
        return True

    logger.warning(
        "Org invite %s email send to %s failed (provider returned %s)",
        invite.id,
        invite.email,
        result.get("status"),
    )
    return False

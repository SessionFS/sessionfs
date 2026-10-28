"""Link a ticket to an existing Jira issue (GitHub #112 phase 2).

Shared by the ``POST /api/v1/projects/{pid}/tickets/{tid}/jira/link`` route and
the session helper's ``propose_jira_link`` confirm path. Linking is a read of
one remote issue plus one local insert — it never pushes anything to Jira.

The single entry point raises :class:`JiraLinkError` carrying a stable machine
``code`` and an HTTP ``status_code`` so both callers surface the same failure
strings without re-implementing the checks.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    OrgAuditEvent,
    Organization,
    OrgTrackerConnection,
    Project,
    Ticket,
    TicketTrackerLink,
    TrackerProjectMapping,
    User,
)
from sessionfs.server.integrations.trackers import JiraClient
from sessionfs.server.integrations.trackers.base import TrackerError, TrackerIssue
from sessionfs.server.llm_keys import decrypt_secret
from sessionfs.server.services.ticket_events import record_ticket_event

# Stricter than the client's generic key regex: a real, fully-qualified key the
# caller intends to link, with no leading zero and a bounded project/number
# length. Uppercase + trimmed by ``normalize_link_issue_key`` first.
LINK_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,49}-[1-9][0-9]{0,9}$")

# Bounded outbound read. A link only reads one issue, so this is far tighter
# than the publish route's 60 s create/update ceiling.
_LINK_FETCH_TIMEOUT_SECONDS = 30.0

# Mirrors ``routes/jira.py``'s status truncation for the column's width.
_LAST_STATUS_MAX = 50


class JiraLinkError(Exception):
    """A typed, user-safe failure from ``link_ticket_to_jira``.

    ``code`` is the stable machine string the route's error envelope and the
    helper's per-target failure code both use (``jira_already_linked``,
    ``jira_issue_not_found``, ``jira_project_not_mapped``, ...).
    """

    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def normalize_link_issue_key(issue_key: str) -> str:
    """Trim + uppercase and validate a caller-supplied key, or raise 422.

    The link is by explicit user intent, so the tighter format is enforced here
    (unlike promotion, where the client already validated before asking Jira to
    create it).
    """
    normalized = (issue_key or "").strip().upper()
    if not LINK_ISSUE_KEY_RE.match(normalized):
        raise JiraLinkError(
            "invalid_issue_key",
            f"Invalid Jira issue key: {issue_key!r}",
            status_code=422,
        )
    return normalized


async def _get_link(db: AsyncSession, ticket_id: str) -> TicketTrackerLink | None:
    return (
        await db.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket_id)
        )
    ).scalar_one_or_none()


async def _assert_issue_not_linked_elsewhere(
    db: AsyncSession,
    *,
    org_id: str,
    site_url: str,
    issue: TrackerIssue,
    issue_key: str,
    ticket_id: str,
) -> None:
    """409 ``jira_issue_linked_elsewhere`` when the same issue already links to
    a different ticket on this org/site. Never reveals the other ticket's id or
    title, so a caller can't probe which ticket owns a promoted issue."""
    key_matches = [TicketTrackerLink.issue_key == issue_key]
    if issue.id:
        key_matches.append(TicketTrackerLink.issue_id == issue.id)
    row = (
        await db.execute(
            select(TicketTrackerLink.ticket_id).where(
                TicketTrackerLink.org_id == org_id,
                TicketTrackerLink.site_url == site_url,
                TicketTrackerLink.status == "linked",
                TicketTrackerLink.ticket_id != ticket_id,
                or_(*key_matches),
            )
        )
    ).first()
    if row is not None:
        raise JiraLinkError(
            "jira_issue_linked_elsewhere",
            "This Jira issue is already linked to another ticket.",
            status_code=409,
        )


async def link_ticket_to_jira(
    db: AsyncSession,
    *,
    project: Project,
    ticket: Ticket,
    conn: OrgTrackerConnection,
    mapping: TrackerProjectMapping,
    issue_key: str,
    actor_user_id: str,
    actor_type: str = "user",
    actor_label: str | None = None,
) -> TicketTrackerLink:
    """Link ``ticket`` to the existing Jira issue named by ``issue_key``.

    Caller has already authorized the action (editor standing, feature gate,
    connection + mapping). The steps are strictly ordered: refuse an existing
    link row, read the issue, check its project, check it isn't linked
    elsewhere, then serialize per-org under ``SELECT ... FOR UPDATE`` and
    re-check before inserting. Does not commit — the caller owns the
    transaction so the helper can commit the ticket create + link + audit
    together.

    ``actor_type`` uses the AuthContext vocabulary (``user`` | ``service_key``)
    and ``actor_label`` names a service key; the ticket event recorded here maps
    ``service_key`` onto the timeline's ``service`` actor type, exactly like the
    route's ``_timeline_actor`` helper.
    """
    normalized = normalize_link_issue_key(issue_key)

    # 1. Any link row — linked, pending or unknown — means this ticket already
    #    has (or is acquiring) a Jira issue.
    if await _get_link(db, ticket.id) is not None:
        raise JiraLinkError(
            "jira_already_linked",
            "This ticket already has a Jira link.",
            status_code=409,
        )

    # 2. Read the issue through the org's connection, bounded.
    client = JiraClient(
        conn.site_url, conn.account_email, decrypt_secret(conn.encrypted_api_token)
    )
    try:
        try:
            issue = await asyncio.wait_for(
                client.get_issue(
                    normalized,
                    sprint_field_id=conn.sprint_field_id,
                    epic_link_field_id=conn.epic_link_field_id,
                ),
                timeout=_LINK_FETCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise JiraLinkError(
                "jira_timeout",
                f"Timed out reading Jira issue {normalized}.",
                status_code=504,
            ) from exc
        except TrackerError as exc:
            # A missing issue (404) and a connection that cannot see it (403)
            # are indistinguishable to the caller — both mean "not linkable".
            if exc.status_code in (403, 404):
                raise JiraLinkError(
                    "jira_issue_not_found",
                    f"Jira issue {normalized} was not found.",
                    status_code=404,
                ) from exc
            raise JiraLinkError(
                "jira_upstream_error",
                str(exc),
                status_code=502,
            ) from exc
    finally:
        await client.aclose()

    # 3. The issue's Jira project must match the project mapping.
    issue_project = (issue.project_key or "").upper()
    if issue_project != mapping.jira_project_key.upper():
        raise JiraLinkError(
            "jira_project_not_mapped",
            (
                f"Jira issue {issue.key} is in project {issue_project or 'unknown'}, "
                f"but this project maps to {mapping.jira_project_key}."
            ),
            status_code=422,
        )

    # 4. The same issue must not already be linked to another ticket.
    await _assert_issue_not_linked_elsewhere(
        db,
        org_id=project.org_id,
        site_url=conn.site_url,
        issue=issue,
        issue_key=normalized,
        ticket_id=ticket.id,
    )

    # 5. Serialize concurrent link attempts per org by locking the org's
    #    tracker connection row, then re-check 1 & 4 under the lock.
    locked = (
        await db.execute(
            select(OrgTrackerConnection.org_id)
            .where(OrgTrackerConnection.org_id == project.org_id)
            .with_for_update()
        )
    ).first()
    if locked is None:
        raise JiraLinkError(
            "jira_not_connected",
            "This organization has not connected Jira.",
            status_code=409,
        )
    if await _get_link(db, ticket.id) is not None:
        raise JiraLinkError(
            "jira_already_linked",
            "This ticket already has a Jira link.",
            status_code=409,
        )
    await _assert_issue_not_linked_elsewhere(
        db,
        org_id=project.org_id,
        site_url=conn.site_url,
        issue=issue,
        issue_key=normalized,
        ticket_id=ticket.id,
    )

    now = datetime.now(timezone.utc)
    link = TicketTrackerLink(
        ticket_id=ticket.id,
        org_id=project.org_id,
        provider="jira",
        status="linked",
        claim_token=None,
        site_url=conn.site_url,
        issue_id=issue.id,
        issue_key=issue.key or normalized,
        issue_url=issue.url,
        last_pushed_at=None,
        last_pushed_by_user_id=None,
        last_known_status=(issue.status or "")[:_LAST_STATUS_MAX] or None,
        last_status_checked_at=now,
        payload_hash=None,
        last_published_text=None,
        last_push_failed_at=None,
        last_push_failure=None,
        template_slug=None,
        template_version=None,
        jira_sprint_id=issue.sprint_id,
        jira_sprint_name=issue.sprint_name,
        jira_sprint_state=issue.sprint_state,
        jira_parent_key=issue.parent_key,
    )
    try:
        async with db.begin_nested():
            db.add(link)
            await db.flush()
    except IntegrityError as exc:
        # A savepoint, not a full rollback: the caller may have other work in
        # this transaction (the helper confirm transitions the action row in
        # the same commit).
        raise JiraLinkError(
            "jira_already_linked",
            "This ticket already has a Jira link.",
            status_code=409,
        ) from exc

    # 6. Org-level audit: who linked what, before/after.
    org = await db.get(Organization, project.org_id)
    actor = await db.get(User, actor_user_id)
    db.add(
        OrgAuditEvent(
            id=f"oae_{uuid.uuid4().hex[:24]}",
            org_id=project.org_id,
            org_name_snapshot=org.name if org else "",
            event_type="jira_linked",
            actor_user_id=actor_user_id,
            actor_email_snapshot=actor.email if actor else "",
            actor_role_at_time=actor_type,
            target_type="ticket",
            target_id=ticket.id,
            before=None,
            after=json.dumps(
                {"issue_key": link.issue_key, "issue_url": link.issue_url}
            ),
        )
    )

    # 7. Ticket timeline: record the link in the same transaction so the
    #    route and the helper both emit exactly one ``jira_linked`` event.
    await record_ticket_event(
        db,
        ticket,
        "jira_linked",
        after={"issue_key": link.issue_key},
        actor_type="service" if actor_type == "service_key" else actor_type,
        actor_user_id=None if actor_type == "service_key" else actor_user_id,
        actor_label=actor_label if actor_type == "service_key" else None,
    )
    return link

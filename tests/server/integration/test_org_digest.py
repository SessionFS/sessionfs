"""Weekly org activity digest (GitHub #97).

Covers the digest builder (numbers on seeded data, personal/other-org
projects excluded), the sweep endpoint (idempotency, opt-out, provider
"none" no-op, auth), the HTML escaping of the email template, and the
per-admin GET/PUT opt-out API.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.config import ServerConfig
from sessionfs.server.db.models import (
    AdminAction,
    AgentRun,
    Handoff,
    KnowledgeEntry,
    OrgAuditEvent,
    OrgDigestRun,
    Organization,
    OrgMember,
    Project,
    Session,
    Ticket,
    User,
)
from sessionfs.server.email import EmailProvider, strip_subject_controls
from sessionfs.server.email_templates import org_digest_email, org_digest_text
from sessionfs.server.services.org_digest import build_org_digest, digest_week
from tests.server.integration.test_skill_marketplace import _platform_admin_headers

pytestmark = pytest.mark.asyncio

UTC = timezone.utc
WEEK_START = date(2026, 10, 19)  # a Monday


def _ts(day: int, hour: int = 12) -> datetime:
    return datetime.combine(WEEK_START, time(hour), tzinfo=UTC) + timedelta(days=day)


async def _add_user(db: AsyncSession, email: str, name: str) -> User:
    user = User(
        id=f"u_{email.split('@')[0]}",
        email=email,
        display_name=name,
        tier="team",
        created_at=datetime.now(UTC),
        email_verified=True,
    )
    db.add(user)
    return user


async def _add_org(db: AsyncSession, *, tier: str = "team") -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name=f"Org {uuid.uuid4().hex[:6]}",
        slug=f"org-{uuid.uuid4().hex[:8]}",
        tier=tier,
    )
    db.add(org)
    return org


async def _add_project(
    db: AsyncSession, org: Organization | None, owner: User, name: str
) -> Project:
    project = Project(
        id=f"prj_{uuid.uuid4().hex[:12]}",
        name=name,
        owner_id=owner.id,
        org_id=org.id if org else None,
    )
    db.add(project)
    return project


async def _add_session(
    db: AsyncSession,
    user: User,
    project: Project | None,
    *,
    created_at: datetime,
    deleted: bool = False,
) -> Session:
    session = Session(
        id=f"ses_{uuid.uuid4().hex[:12]}",
        user_id=user.id,
        source_tool="claude-code",
        blob_key=f"sessions/{user.id}/x.tar.gz",
        etag=f"e{uuid.uuid4().hex[:12]}",
        project_id=project.id if project else None,
        created_at=created_at,
        updated_at=created_at,
        uploaded_at=created_at,
        is_deleted=deleted,
    )
    db.add(session)
    return session


# ── Builder ──


async def test_builder_counts_only_org_projects_within_the_week(
    db_session: AsyncSession,
) -> None:
    db = db_session
    u1 = await _add_user(db_session, "alice@example.com", "Alice")
    u2 = await _add_user(db_session, "bob@example.com", "Bob")

    org_a = await _add_org(db_session)
    org_b = await _add_org(db_session)
    db.add(OrgMember(org_id=org_a.id, user_id=u1.id, role="member"))
    db.add(OrgMember(org_id=org_a.id, user_id=u2.id, role="member"))
    p1 = await _add_project(db_session, org_a, u1, "P1")
    p2 = await _add_project(db_session, org_a, u1, "P2")
    p_other = await _add_project(db_session, org_b, u2, "Other")
    p_personal = await _add_project(db_session, None, u1, "Personal")
    await db_session.flush()

    # Sessions: 4 org-A sessions in the window; deleted / other-org /
    # personal / out-of-window rows must be excluded.
    for _ in range(2):
        await _add_session(db_session, u1, p1, created_at=_ts(1))
    await _add_session(db_session, u2, p1, created_at=_ts(2))
    await _add_session(db_session, u1, p2, created_at=_ts(1))
    await _add_session(db_session, u1, p1, created_at=_ts(1), deleted=True)
    await _add_session(db_session, u1, p_personal, created_at=_ts(1))
    await _add_session(db_session, u1, p_other, created_at=_ts(1))
    await _add_session(db_session, u1, p1, created_at=_ts(-5))

    # Tickets.
    for persona in ("alice", "alice"):
        db.add(Ticket(
            id=f"tk_{uuid.uuid4().hex[:12]}",
            project_id=p1.id,
            title="t",
            created_by_user_id=u1.id,
            created_by_persona=persona,
            created_at=_ts(1),
        ))
    db.add(Ticket(
        id=f"tk_{uuid.uuid4().hex[:12]}",
        project_id=p1.id,
        title="t",
        created_by_user_id=u1.id,
        created_at=_ts(1),
    ))
    db.add(Ticket(  # other org — excluded
        id=f"tk_{uuid.uuid4().hex[:12]}",
        project_id=p_other.id,
        title="t",
        created_by_user_id=u2.id,
        created_at=_ts(1),
    ))
    db.add(Ticket(  # outside window — excluded
        id=f"tk_{uuid.uuid4().hex[:12]}",
        project_id=p1.id,
        title="t",
        created_by_user_id=u1.id,
        created_at=_ts(-5),
    ))
    # Completed. created_at is deliberately out-of-window so these rows only
    # count toward "completed", never "opened".
    db.add(Ticket(
        id=f"tk_{uuid.uuid4().hex[:12]}",
        project_id=p1.id,
        title="t",
        created_by_user_id=u1.id,
        status="done",
        assigned_to="alice",
        created_at=_ts(-5),
        resolved_at=_ts(1),
    ))
    db.add(Ticket(
        id=f"tk_{uuid.uuid4().hex[:12]}",
        project_id=p1.id,
        title="t",
        created_by_user_id=u1.id,
        status="done",
        created_at=_ts(-5),
        resolved_at=_ts(1),
    ))
    db.add(Ticket(  # done but resolved outside window — excluded
        id=f"tk_{uuid.uuid4().hex[:12]}",
        project_id=p1.id,
        title="t",
        created_by_user_id=u1.id,
        status="done",
        created_at=_ts(-5),
        resolved_at=_ts(-5),
    ))

    # Agent runs.
    db.add(AgentRun(
        id=f"run_{uuid.uuid4().hex[:12]}",
        project_id=p1.id,
        persona_name="alice",
        status="passed",
        severity="high",
        findings_count=3,
        created_at=_ts(1),
    ))
    db.add(AgentRun(
        id=f"run_{uuid.uuid4().hex[:12]}",
        project_id=p1.id,
        persona_name="bob",
        status="failed",
        severity=None,
        findings_count=2,
        created_at=_ts(1),
    ))
    db.add(AgentRun(
        id=f"run_{uuid.uuid4().hex[:12]}",
        project_id=p2.id,
        persona_name="alice",
        status="passed",
        severity="low",
        findings_count=1,
        created_at=_ts(1),
    ))
    db.add(AgentRun(  # other org — excluded
        id=f"run_{uuid.uuid4().hex[:12]}",
        project_id=p_other.id,
        persona_name="alice",
        status="passed",
        severity="high",
        findings_count=5,
        created_at=_ts(1),
    ))

    # Knowledge.
    db.add(KnowledgeEntry(
        project_id=p1.id,
        session_id="ses_x",
        user_id=u1.id,
        entry_type="claim",
        content="c",
        claim_class="claim",
        created_at=_ts(1),
    ))
    db.add(KnowledgeEntry(  # note — not a claim
        project_id=p1.id,
        session_id="ses_x",
        user_id=u1.id,
        entry_type="note",
        content="c",
        claim_class="note",
        created_at=_ts(1),
    ))
    db.add(KnowledgeEntry(  # promoted this week
        project_id=p1.id,
        session_id="ses_x",
        user_id=u1.id,
        entry_type="claim",
        content="c",
        claim_class="claim",
        created_at=_ts(-10),
        promoted_at=_ts(1),
    ))
    db.add(KnowledgeEntry(  # dismissed this week
        project_id=p1.id,
        session_id="ses_x",
        user_id=u1.id,
        entry_type="claim",
        content="c",
        claim_class="claim",
        created_at=_ts(-10),
        dismissed_at=_ts(1),
    ))
    db.add(KnowledgeEntry(  # other org — excluded
        project_id=p_other.id,
        session_id="ses_x",
        user_id=u2.id,
        entry_type="claim",
        content="c",
        claim_class="claim",
        created_at=_ts(1),
    ))

    # Handoffs (need a real session FK; keep it out of the week so it does
    # not inflate sessions.total).
    hf_session = await _add_session(db_session, u1, p1, created_at=_ts(-5))
    await db_session.flush()
    db.add(Handoff(
        id=f"hf_{uuid.uuid4().hex[:10]}",
        session_id=hf_session.id,
        sender_id=u1.id,
        source_project_id=p1.id,
        expires_at=_ts(10),
        created_at=_ts(1),
    ))
    db.add(Handoff(
        id=f"hf_{uuid.uuid4().hex[:10]}",
        session_id=hf_session.id,
        sender_id=u1.id,
        source_project_id=p1.id,
        expires_at=_ts(10),
        created_at=_ts(1),
        claimed_at=_ts(2),
    ))
    db.add(Handoff(  # other org — excluded
        id=f"hf_{uuid.uuid4().hex[:10]}",
        session_id=hf_session.id,
        sender_id=u1.id,
        source_project_id=p_other.id,
        expires_at=_ts(10),
        created_at=_ts(1),
    ))

    # Org audit events.
    db.add(OrgAuditEvent(
        id=f"oae_{uuid.uuid4().hex[:12]}",
        org_id=org_a.id,
        org_name_snapshot=org_a.name,
        event_type="member_invited",
        created_at=_ts(1),
    ))
    db.add(OrgAuditEvent(
        id=f"oae_{uuid.uuid4().hex[:12]}",
        org_id=org_a.id,
        org_name_snapshot=org_a.name,
        event_type="settings_updated",
        created_at=_ts(1),
    ))
    db.add(OrgAuditEvent(  # other org — excluded
        id=f"oae_{uuid.uuid4().hex[:12]}",
        org_id=org_b.id,
        org_name_snapshot=org_b.name,
        event_type="member_invited",
        created_at=_ts(1),
    ))
    await db_session.commit()

    digest = await build_org_digest(db_session, org_a.id, WEEK_START)

    assert digest["sessions"]["total"] == 4
    assert digest["sessions"]["top_users"] == [
        {"user_id": u1.id, "display_name": "Alice", "email": "alice@example.com", "sessions": 3},
        {"user_id": u2.id, "display_name": "Bob", "email": "bob@example.com", "sessions": 1},
    ]
    assert [p["project_id"] for p in digest["sessions"]["top_projects"]] == [p1.id, p2.id]
    assert [p["sessions"] for p in digest["sessions"]["top_projects"]] == [3, 1]
    assert digest["sessions"]["top_projects"][0]["name"] == "P1"

    assert digest["tickets"]["opened"] == 3
    assert digest["tickets"]["opened_by_persona"] == {"alice": 2}
    assert digest["tickets"]["completed"] == 2
    assert digest["tickets"]["completed_by_persona"] == {"alice": 1}

    assert digest["agent_runs"]["total"] == 3
    assert digest["agent_runs"]["by_status"] == {"passed": 2, "failed": 1}
    assert digest["agent_runs"]["severity_rollup"] == {"high": 3, "none": 2, "low": 1}

    assert digest["knowledge"] == {
        "claims_added": 1,
        "claims_promoted": 1,
        "claims_dismissed": 1,
    }

    assert digest["handoffs"] == {"created": 2, "claimed": 1}
    assert digest["admin_actions"] == {
        "total": 2,
        "by_action": {"member_invited": 1, "settings_updated": 1},
    }


async def test_builder_returns_empty_for_org_without_projects(
    db_session: AsyncSession,
) -> None:
    org = await _add_org(db_session)
    await db_session.commit()
    digest = await build_org_digest(db_session, org.id, WEEK_START)
    assert digest == {
        "org_id": org.id,
        "week_start": "2026-10-19",
        "sessions": {"total": 0, "top_users": [], "top_projects": [], "former_members": 0},
        "tickets": {
            "opened": 0,
            "completed": 0,
            "opened_by_persona": {},
            "completed_by_persona": {},
        },
        "agent_runs": {"total": 0, "by_status": {}, "severity_rollup": {}},
        "knowledge": {"claims_added": 0, "claims_promoted": 0, "claims_dismissed": 0},
        "handoffs": {"created": 0, "claimed": 0},
        "admin_actions": {"total": 0, "by_action": {}},
    }


# ── Email escaping ──


def _hostile_digest() -> dict:
    return {
        "org_id": "org_x",
        "week_start": "2026-10-19",
        "sessions": {
            "total": 1,
            "top_users": [
                {"user_id": "u1", "display_name": "<b>Bob</b>",
                 "email": "b@example.com", "sessions": 1}
            ],
            "top_projects": [
                {"project_id": "p1", "name": "<img src=x onerror=alert(1)>", "sessions": 1}
            ],
        },
        "tickets": {
            "opened": 1, "completed": 0,
            "opened_by_persona": {"<svg/onload=alert(1)>": 1},
            "completed_by_persona": {},
        },
        "agent_runs": {"total": 1, "by_status": {"passed": 1}, "severity_rollup": {"high": 1}},
        "knowledge": {"claims_added": 0, "claims_promoted": 0, "claims_dismissed": 0},
        "handoffs": {"created": 0, "claimed": 0},
        "admin_actions": {"total": 1, "by_action": {"<script>x</script>": 1}},
    }


async def test_org_digest_email_escapes_user_controlled_strings() -> None:
    html = org_digest_email(
        org_name="<script>alert('x')</script>",
        week_label="2026-10-19 – 2026-10-25",
        digest=_hostile_digest(),
        dashboard_url="https://app.sessionfs.dev",
    )
    for raw in (
        "<script>alert('x')</script>",
        "<b>Bob</b>",
        "<img src=x onerror=alert(1)>",
        "<svg/onload=alert(1)>",
        "<script>x</script>",
    ):
        assert raw not in html
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in html
    assert "&lt;b&gt;Bob&lt;/b&gt;" in html
    # The project link is an escaped URL to the filtered view.
    assert "/projects/p1?tab=work" in html


async def test_org_digest_text_is_plaintext_alternative() -> None:
    text = org_digest_text(
        org_name="ACME <Org>",
        week_label="2026-10-19 – 2026-10-25",
        digest=_hostile_digest(),
        dashboard_url="https://app.sessionfs.dev",
    )
    assert "SessionFS — ACME <Org> weekly activity" in text
    assert "findings (high): 1" in text
    assert "/settings/organization" in text


# ── Sweep ──


async def _seed_sweep_org(
    db: AsyncSession,
    *,
    owner: User,
    admin: User | None = None,
    admin_opt_out: bool = False,
) -> Organization:
    org = await _add_org(db)
    project = await _add_project(db, org, owner, "Sweep project")
    db.add(OrgMember(org_id=org.id, user_id=owner.id, role="owner"))
    if admin is not None:
        db.add(OrgMember(
            org_id=org.id, user_id=admin.id, role="admin",
            digest_opt_out=admin_opt_out,
        ))
    await db.flush()
    # Activity within the completed week under test (the sweep computes its
    # own week from the real clock; use digest_week to stay in-window).
    _, start_dt, _ = digest_week(datetime.now(UTC))
    await _add_session(db, owner, project, created_at=start_dt + timedelta(days=2, hours=6))
    await db.commit()
    return org


class _RecordingProvider:
    def __init__(self) -> None:
        self.sends: list[dict] = []

    async def send_org_digest(
        self, *, to_email, org_name, week_label, digest, dashboard_url
    ) -> dict:
        self.sends.append({"to": to_email, "org": org_name})
        return {"status": "sent"}


async def test_sweep_is_idempotent_per_org_per_week(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin, headers = await _platform_admin_headers(db_session)
    owner = await _add_user(db_session, "owner@example.com", "Owner")
    await _seed_sweep_org(db_session, owner=owner)

    recorder = _RecordingProvider()
    import sessionfs.server.email as email_mod

    monkeypatch.setattr(email_mod, "create_email_provider", lambda config: recorder)

    first = await client.post("/api/v1/admin/sweeps/org-digest", headers=headers, json={})
    assert first.status_code == 200, first.text
    assert first.json()["orgs_processed"] == 1
    assert first.json()["recipients"] == 1
    assert len(recorder.sends) == 1
    assert recorder.sends[0]["to"] == "owner@example.com"

    recorder.sends.clear()
    second = await client.post("/api/v1/admin/sweeps/org-digest", headers=headers, json={})
    assert second.status_code == 200, second.text
    assert second.json()["orgs_processed"] == 0
    assert second.json()["orgs_skipped"] == 1
    assert second.json()["skipped"][0]["reason"] == "already_sent"
    assert recorder.sends == []


async def test_sweep_respects_per_admin_opt_out(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin, headers = await _platform_admin_headers(db_session)
    owner = await _add_user(db_session, "owner@example.com", "Owner")
    opt_out = await _add_user(db_session, "opted@example.com", "Opted Out")
    await _seed_sweep_org(db_session, owner=owner, admin=opt_out, admin_opt_out=True)

    recorder = _RecordingProvider()
    import sessionfs.server.email as email_mod

    monkeypatch.setattr(email_mod, "create_email_provider", lambda config: recorder)

    response = await client.post("/api/v1/admin/sweeps/org-digest", headers=headers, json={})
    assert response.status_code == 200, response.text
    assert response.json()["orgs_processed"] == 1
    assert [s["to"] for s in recorder.sends] == ["owner@example.com"]


async def test_sweep_with_provider_none_records_the_run(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, headers = await _platform_admin_headers(db_session)
    owner = await _add_user(db_session, "owner@example.com", "Owner")
    org = await _seed_sweep_org(db_session, owner=owner)

    client._transport.app.state.config = ServerConfig(  # type: ignore[attr-defined]
        email_provider="none", database_url="sqlite+aiosqlite://"
    )

    response = await client.post("/api/v1/admin/sweeps/org-digest", headers=headers, json={})
    assert response.status_code == 200, response.text
    assert response.json()["orgs_processed"] == 1
    assert response.json()["recipients"] == 1

    week_start, _, _ = digest_week(datetime.now(UTC))
    run = (
        await db_session.execute(
            select(OrgDigestRun).where(
                OrgDigestRun.org_id == org.id, OrgDigestRun.week_start == week_start
            )
        )
    ).scalar_one()
    assert run.recipient_count == 1


# ── Opt-out API ──


async def test_digest_prefs_owner_can_read_and_update(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
) -> None:
    org = await _add_org(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="owner"))
    await db_session.commit()

    got = await client.get(f"/api/v1/orgs/{org.id}/members/me/digest", headers=auth_headers)
    assert got.status_code == 200, got.text
    assert got.json() == {"org_id": org.id, "opt_out": False}

    put = await client.put(
        f"/api/v1/orgs/{org.id}/members/me/digest",
        headers=auth_headers,
        json={"opt_out": True},
    )
    assert put.status_code == 200, put.text
    assert put.json() == {"org_id": org.id, "opt_out": True}

    again = await client.get(f"/api/v1/orgs/{org.id}/members/me/digest", headers=auth_headers)
    assert again.json()["opt_out"] is True


async def test_digest_prefs_member_is_refused(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
) -> None:
    org = await _add_org(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="member"))
    await db_session.commit()

    got = await client.get(f"/api/v1/orgs/{org.id}/members/me/digest", headers=auth_headers)
    assert got.status_code == 403, got.text
    put = await client.put(
        f"/api/v1/orgs/{org.id}/members/me/digest",
        headers=auth_headers,
        json={"opt_out": True},
    )
    assert put.status_code == 403, put.text


async def test_digest_prefs_unknown_org_is_404(
    client: AsyncClient, auth_headers: dict
) -> None:
    got = await client.get("/api/v1/orgs/org_missing/members/me/digest", headers=auth_headers)
    assert got.status_code == 404, got.text


# ── Review round 1 regressions ──


async def test_builder_aggregates_departed_users_as_former_members(
    db_session: AsyncSession,
) -> None:
    """Finding #5: sessions from users no longer in the org are never named
    in the top-users leaderboard; they collapse into a former-members count."""
    db = db_session
    member = await _add_user(db_session, "member@example.com", "Member")
    departed = await _add_user(db_session, "left@example.com", "Left")
    org = await _add_org(db_session)
    db.add(OrgMember(org_id=org.id, user_id=member.id, role="member"))
    project = await _add_project(db_session, org, member, "P")
    await db_session.flush()

    await _add_session(db_session, member, project, created_at=_ts(1))
    await _add_session(db_session, member, project, created_at=_ts(2))
    for _ in range(3):
        await _add_session(db_session, departed, project, created_at=_ts(1))
    await db_session.commit()

    digest = await build_org_digest(db_session, org.id, WEEK_START)

    assert digest["sessions"]["total"] == 5
    assert [u["user_id"] for u in digest["sessions"]["top_users"]] == [member.id]
    assert digest["sessions"]["former_members"] == 3


async def test_strip_subject_controls_removes_c0_controls() -> None:
    assert strip_subject_controls("ACME\r\nBcc: evil@example.com") == "ACMEBcc: evil@example.com"
    assert strip_subject_controls("ok\x00\x1f\x7fname") == "okname"
    assert strip_subject_controls("plain") == "plain"


class _SubjectCapture(EmailProvider):
    def __init__(self) -> None:
        self.subject: str | None = None

    async def send(self, to: str, subject: str, html: str, text: str | None = None) -> bool:
        self.subject = subject
        return True


async def test_send_org_digest_strips_controls_from_subject() -> None:
    """Finding #7: newlines / C0 controls in an org name must never reach the
    email subject (header injection), but non-control characters survive."""
    provider = _SubjectCapture()
    await provider.send_org_digest(
        to_email="a@example.com",
        org_name="Evil\r\nOrg\x00",
        week_label="2026-10-19 – 2026-10-25",
        digest={},
        dashboard_url="",
    )
    assert "\r" not in provider.subject
    assert "\n" not in provider.subject
    assert "\x00" not in provider.subject
    assert provider.subject == "SessionFS: EvilOrg weekly activity (2026-10-19 – 2026-10-25)"


async def test_org_digest_opt_out_links_to_policies_tab_only() -> None:
    """Finding #6: OrgPage supports ?tab= but not ?org=, so the opt-out link
    must point at the policies tab without an org query parameter."""
    html = org_digest_email(
        org_name="ACME",
        week_label="2026-10-19 – 2026-10-25",
        digest=_hostile_digest(),
        dashboard_url="https://app.sessionfs.dev",
    )
    text = org_digest_text(
        org_name="ACME",
        week_label="2026-10-19 – 2026-10-25",
        digest=_hostile_digest(),
        dashboard_url="https://app.sessionfs.dev",
    )
    assert "/settings/organization?tab=policies" in html
    assert "/settings/organization?tab=policies" in text
    assert "?org=" not in html
    assert "?org=" not in text


async def test_digest_dashboard_url_resolution() -> None:
    """Finding #4: the digest base URL is SFS_DASHBOARD_URL, then app_url,
    then '' (skip links) — never a hardcoded hosted-dashboard fallback."""
    from types import SimpleNamespace

    from sessionfs.server.routes.admin import _digest_dashboard_url

    assert _digest_dashboard_url(
        SimpleNamespace(dashboard_url="https://dash.example.com/", app_url="https://app.example.com")
    ) == "https://dash.example.com"
    assert _digest_dashboard_url(
        SimpleNamespace(
            dashboard_url="", app_url="https://app.example.com/", model_fields_set={"app_url"}
        )
    ) == "https://app.example.com"
    # app_url left at its hosted default (not explicitly configured) is ignored.
    assert _digest_dashboard_url(
        SimpleNamespace(dashboard_url="", app_url="https://app.sessionfs.dev", model_fields_set=set())
    ) == ""
    assert _digest_dashboard_url(SimpleNamespace(dashboard_url="", app_url="")) == ""
    assert _digest_dashboard_url(None) == ""


async def test_org_digest_email_omits_links_without_dashboard_url() -> None:
    html = org_digest_email(
        org_name="ACME",
        week_label="2026-10-19 – 2026-10-25",
        digest=_hostile_digest(),
        dashboard_url="",
    )
    text = org_digest_text(
        org_name="ACME",
        week_label="2026-10-19 – 2026-10-25",
        digest=_hostile_digest(),
        dashboard_url="",
    )
    assert "href=" not in html
    assert "Manage email preferences" not in text


class _FailingProvider:
    async def send_org_digest(self, *, to_email, org_name, week_label, digest, dashboard_url):
        return {"status": "failed"}


class _SelectiveProvider:
    def __init__(self, fail_emails: set[str]) -> None:
        self._fail = fail_emails

    async def send_org_digest(self, *, to_email, org_name, week_label, digest, dashboard_url):
        return {"status": "failed" if to_email in self._fail else "sent"}


async def test_sweep_deletes_reservation_when_all_sends_fail(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding #3: when every recipient fails, the run reservation is deleted
    so a later run this week retries instead of losing the week."""
    admin, headers = await _platform_admin_headers(db_session)
    owner = await _add_user(db_session, "owner@example.com", "Owner")
    org = await _seed_sweep_org(db_session, owner=owner)

    import sessionfs.server.email as email_mod

    monkeypatch.setattr(email_mod, "create_email_provider", lambda config: _FailingProvider())

    response = await client.post("/api/v1/admin/sweeps/org-digest", headers=headers, json={})
    assert response.status_code == 200, response.text
    assert response.json()["orgs_processed"] == 0
    assert response.json()["orgs_skipped"] == 1
    assert response.json()["skipped"][0]["reason"] == "send_failed"
    assert response.json()["recipients"] == 0

    week_start, _, _ = digest_week(datetime.now(UTC))
    run = (
        await db_session.execute(
            select(OrgDigestRun).where(
                OrgDigestRun.org_id == org.id, OrgDigestRun.week_start == week_start
            )
        )
    ).scalars().all()
    assert run == []


async def test_sweep_records_sent_count_after_partial_failure(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding #3: sent_count reflects what actually went out, distinct from
    recipient_count, so the ledger never overstates delivery."""
    admin, headers = await _platform_admin_headers(db_session)
    owner = await _add_user(db_session, "owner@example.com", "Owner")
    other_admin = await _add_user(db_session, "admin@example.com", "Admin")
    org = await _seed_sweep_org(db_session, owner=owner, admin=other_admin)

    import sessionfs.server.email as email_mod

    monkeypatch.setattr(
        email_mod, "create_email_provider",
        lambda config: _SelectiveProvider({"admin@example.com"}),
    )

    response = await client.post("/api/v1/admin/sweeps/org-digest", headers=headers, json={})
    assert response.status_code == 200, response.text
    assert response.json()["orgs_processed"] == 1
    assert response.json()["recipients"] == 1

    week_start, _, _ = digest_week(datetime.now(UTC))
    run = (
        await db_session.execute(
            select(OrgDigestRun).where(
                OrgDigestRun.org_id == org.id, OrgDigestRun.week_start == week_start
            )
        )
    ).scalar_one()
    assert run.recipient_count == 2
    assert run.sent_count == 1


async def test_sweep_isolates_one_org_failure_and_still_logs_audit(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding #2: one org's builder failure skips only that org (reason
    "error"), the sweep continues, and the final audit row is still written."""
    admin, headers = await _platform_admin_headers(db_session)
    owner1 = await _add_user(db_session, "owner1@example.com", "Owner One")
    org1 = await _seed_sweep_org(db_session, owner=owner1)
    owner2 = await _add_user(db_session, "owner2@example.com", "Owner Two")
    await _seed_sweep_org(db_session, owner=owner2)

    import sessionfs.server.email as email_mod
    import sessionfs.server.services.org_digest as digest_mod

    recorder = _RecordingProvider()
    monkeypatch.setattr(email_mod, "create_email_provider", lambda config: recorder)

    real_build = digest_mod.build_org_digest

    async def flaky(db, org_id, week_start):
        if org_id == org1.id:
            raise RuntimeError("boom")
        return await real_build(db, org_id, week_start)

    monkeypatch.setattr(digest_mod, "build_org_digest", flaky)

    response = await client.post("/api/v1/admin/sweeps/org-digest", headers=headers, json={})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["orgs_processed"] == 1
    assert payload["orgs_skipped"] == 1
    assert payload["skipped"][0]["org_id"] == org1.id
    assert payload["skipped"][0]["reason"] == "error"

    audit = (
        await db_session.execute(
            select(AdminAction).where(AdminAction.action == "run_org_digest")
        )
    ).scalars().all()
    assert len(audit) == 1


async def test_sweep_releases_reservation_when_send_raises(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception after the reservation commit must not strand the week:
    nobody got the email, so the reservation is released for a retry."""
    admin, headers = await _platform_admin_headers(db_session)
    owner = await _add_user(db_session, "owner-raise@example.com", "Owner Raise")
    await _seed_sweep_org(db_session, owner=owner)

    import sessionfs.server.email as email_mod

    class _Raising(_RecordingProvider):
        async def send_org_digest(self, **kwargs):
            raise RuntimeError("template broke")

    monkeypatch.setattr(email_mod, "create_email_provider", lambda config: _Raising())
    response = await client.post("/api/v1/admin/sweeps/org-digest", headers=headers, json={})
    assert response.status_code == 200, response.text
    assert response.json()["skipped"][0]["reason"] == "error"
    rows = (await db_session.execute(select(OrgDigestRun))).scalars().all()
    assert rows == []

    recorder = _RecordingProvider()
    monkeypatch.setattr(email_mod, "create_email_provider", lambda config: recorder)
    response = await client.post("/api/v1/admin/sweeps/org-digest", headers=headers, json={})
    assert response.status_code == 200, response.text
    assert response.json()["orgs_processed"] == 1


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
async def test_build_org_digest_on_postgres_groups_severity_without_coalesce() -> None:
    """Finding #1: coalesce(severity, 'none') in GROUP BY binds the literal
    twice, which PostgreSQL rejects as a GroupingError. Grouping the raw
    column and mapping NULL -> 'none' in Python must produce the same rollup."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sessionfs.server.db.models import Base
    from tests.pg_helpers import reset_pg_schema_async

    url = os.environ["SFS_TEST_PG_URL"]
    await reset_pg_schema_async(url)
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as db:
            owner = await _add_user(db, "pg@example.com", "PG Owner")
            org = await _add_org(db)
            await db.flush()  # persist user + org before their FKs reference them
            db.add(OrgMember(org_id=org.id, user_id=owner.id, role="owner"))
            project = await _add_project(db, org, owner, "P")
            await db.flush()
            db.add(AgentRun(
                id=f"run_{uuid.uuid4().hex[:12]}",
                project_id=project.id,
                persona_name="alice",
                status="passed",
                severity="high",
                findings_count=3,
                created_at=_ts(1),
            ))
            db.add(AgentRun(
                id=f"run_{uuid.uuid4().hex[:12]}",
                project_id=project.id,
                persona_name="bob",
                status="passed",
                severity=None,
                findings_count=2,
                created_at=_ts(1),
            ))
            await db.commit()

            digest = await build_org_digest(db, org.id, WEEK_START)
            assert digest["agent_runs"]["severity_rollup"] == {"high": 3, "none": 2}
    finally:
        await engine.dispose()

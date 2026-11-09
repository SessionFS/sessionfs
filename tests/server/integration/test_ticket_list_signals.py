"""Ticket list row signals for the dashboard list (tickets redesign).

GET /projects/{p}/tickets carries, per row, ``review_summary`` (latest
trusted round, verdict, open findings), ``jira_key`` (live link only),
``open_dependency_count`` and, on Issues, ``child_counts``. Everything is
batched: the statement count must not grow with the number of rows.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    Organization,
    OrgMember,
    Project,
    Session as SessionRow,
    Ticket,
    TicketComment,
    TicketDependency,
    TicketTrackerLink,
    User,
)

R1_CHANGES = (
    "Codex R1 review on tk_sig: CHANGES REQUESTED\n\n"
    "Findings:\n\n"
    " • HIGH — services/foo.py:12: unbounded loop.\n"
    " • LOW — naming.\n\n"
    "Verified clean / no change needed:\n\n"
    " • Nothing else.\n"
)
R2_CLEAN = "Codex R2 review on tk_sig: VERIFIED-CLEAN\n\nFindings: none.\n"

_BASE = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _user(db: AsyncSession, name: str) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{name}-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


async def _project(db: AsyncSession, owner: User, org: Organization | None = None) -> Project:
    p = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"sig-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/sig-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
    )
    db.add(p)
    await db.commit()
    return p


def _ticket(project: Project, owner: User, *, status: str = "open", kind: str = "task",
            parent: str | None = None, title: str = "t") -> Ticket:
    return Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title=title,
        kind=kind,
        status=status,
        parent_ticket_id=parent,
        created_by_user_id=owner.id,
    )


def _comment(ticket: Ticket, owner: User, content: str, minutes: int, trusted: bool) -> TicketComment:
    return TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket.id,
        author_user_id=owner.id,
        author_persona="codex-reviewer",
        content=content,
        created_at=_BASE + timedelta(minutes=minutes),
        verdict_trusted=trusted,
    )


async def _seed(db: AsyncSession, project: Project, owner: User, n_extra: int = 0) -> dict[str, Ticket]:
    issue = _ticket(project, owner, kind="issue", status="in_progress", title="issue")
    db.add(issue)
    await db.flush()
    done_child = _ticket(project, owner, status="done", parent=issue.id, title="done child")
    cancelled_child = _ticket(project, owner, status="cancelled", parent=issue.id, title="cancelled child")
    open_child = _ticket(project, owner, status="review", parent=issue.id, title="review child")
    clean = _ticket(project, owner, status="review", title="clean")
    untrusted = _ticket(project, owner, status="review", title="untrusted")
    blocked = _ticket(project, owner, status="blocked", title="blocked")
    db.add_all([done_child, cancelled_child, open_child, clean, untrusted, blocked])
    extras = [_ticket(project, owner, title=f"extra {i}") for i in range(n_extra)]
    db.add_all(extras)
    await db.flush()
    # open_child: R1 changes requested (2 open findings).
    db.add(_comment(open_child, owner, R1_CHANGES, 1, True))
    # clean: R1 changes then R2 clean, so the findings are closed.
    db.add(_comment(clean, owner, R1_CHANGES, 1, True))
    db.add(_comment(clean, owner, R2_CLEAN, 2, True))
    # untrusted: a forged clean verdict never makes a summary.
    db.add(_comment(untrusted, owner, R2_CLEAN, 1, False))
    # blocked waits on open_child (open) and done_child (finished).
    db.add(TicketDependency(ticket_id=blocked.id, depends_on_id=open_child.id))
    db.add(TicketDependency(ticket_id=blocked.id, depends_on_id=done_child.id))
    # Jira: one live link, one pending publish (never a key).
    db.add(TicketTrackerLink(ticket_id=clean.id, org_id="org_x", status="linked",
                             issue_key="WID-43", issue_url="https://acme.atlassian.net/browse/WID-43"))
    db.add(TicketTrackerLink(ticket_id=blocked.id, org_id="org_x", status="pending"))
    # Extras each get a review round, a dependency and a Jira link, so a
    # per-row query anywhere would show in the statement count.
    for i, t in enumerate(extras):
        db.add(_comment(t, owner, R1_CHANGES, 10 + i, True))
        db.add(TicketDependency(ticket_id=t.id, depends_on_id=open_child.id))
        db.add(TicketTrackerLink(ticket_id=t.id, org_id="org_x", status="linked",
                                 issue_key=f"WID-{100 + i}", issue_url=f"https://acme.atlassian.net/browse/WID-{100 + i}"))
    await db.commit()
    return {
        "issue": issue, "done_child": done_child, "open_child": open_child,
        "clean": clean, "untrusted": untrusted, "blocked": blocked,
    }


async def _list(client: AsyncClient, project: Project, key: str) -> dict[str, dict]:
    resp = await client.get(f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    return {row["id"]: row for row in resp.json()}


@pytest.mark.asyncio
async def test_list_rows_carry_signals(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _user(db_session, "owner")
    project = await _project(db_session, owner)
    t = await _seed(db_session, project, owner)
    rows = await _list(client, project, key)

    issue = rows[t["issue"].id]
    assert issue["child_counts"] == {"total": 3, "finished": 2}
    assert rows[t["clean"].id]["child_counts"] is None

    assert rows[t["open_child"].id]["review_summary"] == {
        "last_round": 1, "last_verdict": "changes", "open_findings": 2, "trusted": True,
    }
    assert rows[t["clean"].id]["review_summary"] == {
        "last_round": 2, "last_verdict": "clean", "open_findings": 0, "trusted": True,
    }
    assert rows[t["untrusted"].id]["review_summary"] is None
    assert rows[t["blocked"].id]["review_summary"] is None

    assert rows[t["blocked"].id]["open_dependency_count"] == 1
    assert rows[t["clean"].id]["open_dependency_count"] == 0

    assert rows[t["clean"].id]["jira_key"] == "WID-43"
    assert rows[t["blocked"].id]["jira_key"] is None


@pytest.mark.asyncio
async def test_summary_matches_review_state_endpoint(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _user(db_session, "owner")
    project = await _project(db_session, owner)
    t = await _seed(db_session, project, owner)
    rows = await _list(client, project, key)
    for name in ("open_child", "clean", "untrusted"):
        tid = t[name].id
        resp = await client.get(
            f"/api/v1/projects/{project.id}/tickets/{tid}/review-state", headers=_hdrs(key)
        )
        state = resp.json()["review_state"]
        summary = rows[tid]["review_summary"]
        if state is None:
            assert summary is None
            continue
        assert summary["last_round"] == state["rounds"][-1]["round"]
        assert summary["open_findings"] == len(state["open_findings"])
        assert (summary["last_verdict"] == "clean") == (state["last_verdict"] == "VERIFIED-CLEAN")


@pytest.mark.asyncio
async def test_detail_carries_rollup_jira_and_dependencies(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _user(db_session, "owner")
    project = await _project(db_session, owner)
    t = await _seed(db_session, project, owner)
    issue = (await client.get(f"/api/v1/projects/{project.id}/tickets/{t['issue'].id}", headers=_hdrs(key))).json()
    assert issue["child_counts"] == {"total": 3, "finished": 2}
    blocked = (await client.get(f"/api/v1/projects/{project.id}/tickets/{t['blocked'].id}", headers=_hdrs(key))).json()
    assert blocked["open_dependency_count"] == 1
    assert blocked["jira_key"] is None
    clean = (await client.get(f"/api/v1/projects/{project.id}/tickets/{t['clean'].id}", headers=_hdrs(key))).json()
    assert clean["jira_key"] == "WID-43"


@pytest.mark.asyncio
async def test_dependency_in_another_project_never_counts(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _user(db_session, "owner")
    project = await _project(db_session, owner)
    other = await _project(db_session, owner)
    mine = _ticket(project, owner)
    foreign = _ticket(other, owner, status="open")
    db_session.add_all([mine, foreign])
    await db_session.flush()
    db_session.add(TicketDependency(ticket_id=mine.id, depends_on_id=foreign.id))
    await db_session.commit()
    rows = await _list(client, project, key)
    assert rows[mine.id]["open_dependency_count"] == 0


@pytest.mark.asyncio
async def test_list_statement_count_is_bounded(
    client: AsyncClient, db_session: AsyncSession, db_engine
):
    owner, key = await _user(db_session, "owner")
    small = await _project(db_session, owner)
    large = await _project(db_session, owner)
    await _seed(db_session, small, owner, n_extra=0)
    await _seed(db_session, large, owner, n_extra=25)

    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", _capture)
    try:
        small_rows = await _list(client, small, key)
        n_small = len(captured)
        captured.clear()
        large_rows = await _list(client, large, key)
        n_large = len(captured)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _capture)
    assert len(large_rows) == len(small_rows) + 25
    assert sum(1 for r in large_rows.values() if r["review_summary"]) == 25 + 2
    assert sum(1 for r in large_rows.values() if r["jira_key"]) == 25 + 1
    assert n_small == n_large, (n_small, n_large)


@pytest.mark.asyncio
async def test_service_key_sees_signals(client: AsyncClient, db_session: AsyncSession):
    owner, _ = await _user(db_session, "owner")
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}", name="Org", slug=f"org-{uuid.uuid4().hex[:8]}", tier="team",
    )
    db_session.add(org)
    await db_session.flush()
    db_session.add(OrgMember(org_id=org.id, user_id=owner.id, role="admin", joined_at=datetime.now(timezone.utc)))
    await db_session.commit()
    project = await _project(db_session, owner, org)
    t = await _seed(db_session, project, owner)
    raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()), user_id=owner.id, key_hash=hash_api_key(raw), name="svc",
            is_active=True, key_kind="service", org_id=org.id, scopes=json.dumps(["tickets:read"]),
            created_by_user_id=owner.id, service_key_name="svc", created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    rows = await _list(client, project, raw)
    assert rows[t["clean"].id]["review_summary"]["last_verdict"] == "clean"
    assert rows[t["issue"].id]["child_counts"] == {"total": 3, "finished": 2}

    # A service key of another org gets nothing.
    other_org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}", name="Other", slug=f"org-{uuid.uuid4().hex[:8]}", tier="team",
    )
    db_session.add(other_org)
    await db_session.flush()
    raw2 = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()), user_id=owner.id, key_hash=hash_api_key(raw2), name="svc2",
            is_active=True, key_kind="service", org_id=other_org.id, scopes=json.dumps(["tickets:read"]),
            created_by_user_id=owner.id, service_key_name="svc2", created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    resp = await client.get(f"/api/v1/projects/{project.id}/tickets", headers=_hdrs(raw2))
    assert resp.status_code in (403, 404)


@pytest.mark.asyncio
async def test_captured_session_reader_sees_signals_with_masked_emails(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _user(db_session, "owner")
    reader, reader_key = await _user(db_session, "reader")
    project = await _project(db_session, owner)
    t = await _seed(db_session, project, owner)
    db_session.add(
        SessionRow(
            id=f"ses_{uuid.uuid4().hex[:24]}", user_id=reader.id, source_tool="claude-code",
            title="s", message_count=0, total_input_tokens=0, total_output_tokens=0, tags="[]",
            blob_key="b", blob_size_bytes=0, etag="e",
            git_remote_normalized=project.git_remote_normalized,
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    rows = await _list(client, project, reader_key)
    row = rows[t["open_child"].id]
    assert row["review_summary"]["open_findings"] == 2
    # The creator email stays masked for a captured-session reader.
    assert row["created_by"]["email"] != owner.email

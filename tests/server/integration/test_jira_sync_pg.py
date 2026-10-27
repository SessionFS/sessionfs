"""Jira two-way sync on real PostgreSQL.

The SQLite suite (``test_jira_sync.py``) covers behaviour; this file proves
the parts that depend on the dialect:

* the evidence KB lookup binds integer ids against ``knowledge_entries.id``
  (``INTEGER``) — string ids from the ticket's JSON must be cast first, or
  PostgreSQL refuses ``integer = varchar``;
* the evidence session / KB lookups stay inside the ticket's project;
* the evidence claim relies on the ``(link_id, dedupe_key)`` unique index and
  a SAVEPOINT, so a duplicate claim leaves the surrounding transaction usable;
* an inbound Done resolves the ticket through the shared transitions.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.db.models import (
    Base,
    KnowledgeEntry,
    Organization,
    Project,
    Session,
    Ticket,
    TicketDependency,
    TicketTrackerLink,
    TrackerSyncEvent,
    User,
)
from sessionfs.server.services import jira_sync

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]


@pytest.fixture
async def pg_db():
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"], pool_size=1)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            yield db
    finally:
        await engine.dispose()


async def _world(db):
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex}@example.com")
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:10]}", name="O", slug=f"o-{uuid.uuid4().hex[:8]}", tier="enterprise"
    )
    db.add_all([user, org])
    await db.flush()
    project = Project(id=f"proj_{uuid.uuid4().hex[:12]}", name="p", owner_id=user.id, org_id=org.id)
    other = Project(id=f"proj_{uuid.uuid4().hex[:12]}", name="q", owner_id=user.id, org_id=org.id)
    db.add_all([project, other])
    await db.flush()
    return user, org, project, other


def _ticket(project: Project, user: User, **kw) -> Ticket:
    return Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="pg ticket",
        created_by_user_id=user.id,
        **kw,
    )


async def test_evidence_lookups_cast_ids_and_stay_in_project(pg_db) -> None:
    db = pg_db
    user, _org, project, other = await _world(db)

    def session(project_id: str, deleted: bool = False) -> Session:
        return Session(
            id=f"ses_{uuid.uuid4().hex[:16]}",
            user_id=user.id,
            source_tool="claude-code",
            blob_key="k",
            etag="e",
            project_id=project_id,
            is_deleted=deleted,
        )

    own, foreign, gone = session(project.id), session(other.id), session(project.id, True)
    db.add_all([own, foreign, gone])

    def claim(project_id: str, content: str, **kw) -> KnowledgeEntry:
        return KnowledgeEntry(
            project_id=project_id, session_id="s", user_id=user.id, entry_type="decision", content=content, **kw
        )

    mine, theirs, dismissed = claim(project.id, "Mine"), claim(other.id, "Theirs"), claim(project.id, "D", dismissed=True)
    db.add_all([mine, theirs, dismissed])
    await db.flush()
    ticket = _ticket(
        project,
        user,
        status="done",
        resolver_session_id=foreign.id,
        related_sessions=json.dumps([own.id, gone.id]),
        knowledge_entry_ids=json.dumps([str(mine.id), theirs.id, dismissed.id, "abc", True]),
    )
    db.add(ticket)
    await db.commit()

    assert [s.id for s in await jira_sync.linked_sessions(db, ticket)] == [own.id]
    assert await jira_sync.ticket_claims(db, ticket) == ["Mine"]
    # The whole evidence build runs on PostgreSQL without a type error.
    adf = await jira_sync.build_evidence_adf(db, ticket, None)
    assert "Mine" in json.dumps(adf) and "Theirs" not in json.dumps(adf)


async def test_duplicate_evidence_claim_keeps_transaction_usable(pg_db) -> None:
    db = pg_db
    user, org, project, _other = await _world(db)
    ticket = _ticket(project, user, status="done", resolved_at=datetime.now(timezone.utc))
    db.add(ticket)
    await db.flush()
    link = TicketTrackerLink(
        ticket_id=ticket.id, org_id=org.id, provider="jira", status="linked", issue_key="K-1"
    )
    db.add(link)
    await db.commit()

    first = await jira_sync.enqueue_evidence(db, ticket, link)
    assert first is not None
    assert await jira_sync.enqueue_evidence(db, ticket, link) is None
    # Still usable after the refused savepoint.
    await db.commit()
    rows = (
        await db.execute(select(TrackerSyncEvent).where(TrackerSyncEvent.link_id == ticket.id))
    ).scalars().all()
    assert [r.id for r in rows] == [first]


async def test_inbound_resolve_unblocks_dependents_on_postgres(pg_db) -> None:
    db = pg_db
    user, _org, project, _other = await _world(db)
    ticket = _ticket(project, user, status="review")
    dependent = _ticket(project, user, status="blocked")
    db.add_all([ticket, dependent])
    await db.flush()
    db.add(TicketDependency(ticket_id=dependent.id, depends_on_id=ticket.id))
    await db.commit()

    outcome, error = await jira_sync.apply_inbound_action(db, project, ticket, "resolve")
    assert (outcome, error) == ("applied", None)
    await db.commit()
    refreshed = await db.get(Ticket, ticket.id, populate_existing=True)
    dep = await db.get(Ticket, dependent.id, populate_existing=True)
    assert refreshed.status == "done" and refreshed.resolved_at is not None
    assert dep.status == "in_progress"

    # Refused transitions write nothing and leave the transaction healthy.
    outcome, error = await jira_sync.apply_inbound_action(db, project, dep, "resolve")
    assert outcome == "refused" and error
    await db.commit()
